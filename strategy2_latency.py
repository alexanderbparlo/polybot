"""
Strategy 2 — Latency arb on Polymarket's 15-minute crypto up/down markets.

Plan
----
* Connect to Binance WebSocket (combined ``!ticker@arr`` / per-symbol trade
  streams) for BTC/ETH/SOL spot.
* Keep a 30-second rolling buffer of (ts, price, traded_volume).
* Detect momentum:
    momentum = (price_now - price_30s_ago) / price_30s_ago
    |momentum| >= LATENCY_MOMENTUM_PCT AND
    cumulative traded volume in window >= volume_threshold (adaptive, see code)
* On signal:
    - find the current 15-min up/down Polymarket market for that asset
    - read the YES price on the UP leg (for an UP signal) or DOWN leg
    - if the leg hasn't repriced yet (YES < latency_up_max_price for UP,
      or YES > latency_down_min_price ("UP leg price still cheap") for DOWN)
      place a BUY at current best ask for ``latency_trade_size`` USDC
* Positions auto-settle at the 15-min mark. We schedule a ``settle_after``
  task that queries the book ~16 minutes later and records WIN/LOSS + PnL.

Run standalone:
    python strategy2_latency.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections import deque
from datetime import datetime
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

from config import HybridConfig
from database import HybridDatabase
from logger import TradeLogger
from polymarket_client import HybridPolymarketClient
from polymarket.models import Market
from position_manager import PositionManager
from telegram_bot import HybridTelegram

logger = logging.getLogger(__name__)

STRATEGY_NAME = "latency"


_SYM_TO_ASSET = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
}


class PriceBuffer:
    """Rolling tick buffer for a single symbol, keyed on monotonic time."""

    def __init__(self, window_secs: int):
        self.window = window_secs
        self._ticks: deque[tuple[float, float, float]] = deque()  # (ts, price, qty)

    def add(self, ts: float, price: float, qty: float) -> None:
        self._ticks.append((ts, price, qty))
        cutoff = ts - self.window
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.popleft()

    def momentum(self) -> Optional[tuple[float, float, float]]:
        """Return (pct_change, latest_price, window_volume) or None."""
        if len(self._ticks) < 2:
            return None
        first_ts, first_px, _ = self._ticks[0]
        last_ts, last_px, _ = self._ticks[-1]
        if last_ts - first_ts < self.window * 0.5:
            # not enough window filled yet
            return None
        if first_px <= 0:
            return None
        vol = sum(q for _, _, q in self._ticks)
        return (last_px - first_px) / first_px, last_px, vol


class LatencyStrategy:
    def __init__(
        self,
        cfg: HybridConfig,
        client: HybridPolymarketClient,
        db: HybridDatabase,
        trade_logger: TradeLogger,
        telegram: HybridTelegram,
        position_manager: PositionManager,
    ):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.trade_logger = trade_logger
        self.telegram = telegram
        self.pm = position_manager
        self._stop = asyncio.Event()
        self._buffers: dict[str, PriceBuffer] = {
            sym: PriceBuffer(cfg.latency_window_secs) for sym in cfg.latency_symbols
        }
        # Per-asset cooldown so we don't re-fire the same signal every tick.
        self._last_signal_ts: dict[str, float] = {}
        self._signal_cooldown_s = 60
        # Track rolling-window volume floor per symbol (adaptive).
        self._volume_floor: dict[str, float] = {}

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        logger.info(
            "[%s] starting (paper=%s, symbols=%s, momentum>=%.2f%% over %ds)",
            STRATEGY_NAME, self.cfg.paper_mode, self.cfg.latency_symbols,
            self.cfg.latency_momentum_pct * 100, self.cfg.latency_window_secs,
        )
        # Keep reconnecting forever until _stop is set.
        while not self._stop.is_set():
            try:
                await self._ws_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] ws loop error: %s", STRATEGY_NAME, exc)
                self.trade_logger.log_event("error", f"{STRATEGY_NAME} ws failed", str(exc))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        logger.info("[%s] stopped", STRATEGY_NAME)

    # ------------------------------------------------------------------
    async def _ws_loop(self) -> None:
        streams = "/".join(f"{s}@trade" for s in self.cfg.latency_symbols)
        url = f"{self.cfg.binance_ws_url}/{streams}"
        logger.info("[%s] connecting: %s", STRATEGY_NAME, url)
        timeout = aiohttp.ClientTimeout(total=None, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.ws_connect(url, heartbeat=20) as ws:
                logger.info("[%s] connected", STRATEGY_NAME)
                async for msg in ws:
                    if self._stop.is_set():
                        await ws.close()
                        return
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_tick(payload)

    async def _handle_tick(self, payload: dict) -> None:
        # Binance trade event: { e, E, s, t, p, q, b, a, T, m, ... }
        sym = payload.get("s", "").lower()
        if sym not in self._buffers:
            # Combined streams wrap in {stream, data}; unwrap if needed.
            if "stream" in payload and "data" in payload:
                return await self._handle_tick(payload["data"])
            return
        try:
            price = float(payload["p"])
            qty = float(payload["q"])
        except (KeyError, ValueError, TypeError):
            return
        ts = time.time()
        self._buffers[sym].add(ts, price, qty)

        m = self._buffers[sym].momentum()
        if m is None:
            return
        mom, last_px, vol = m

        # Adaptive volume floor: median of the window's historical volume ×0.8.
        # We approximate by tracking max seen * 0.2 floor so early ticks don't
        # fire with tiny windows.
        floor = self._volume_floor.get(sym, 0.0)
        if vol > floor:
            self._volume_floor[sym] = vol * 0.9 + floor * 0.1  # smooth upward
        volume_ok = vol >= max(floor * 0.5, 1e-9)

        if abs(mom) < self.cfg.latency_momentum_pct or not volume_ok:
            return

        now = time.time()
        if now - self._last_signal_ts.get(sym, 0) < self._signal_cooldown_s:
            return
        self._last_signal_ts[sym] = now

        direction = "UP" if mom > 0 else "DOWN"
        asset = _SYM_TO_ASSET.get(sym, sym.upper())
        await self._on_signal(asset, direction, mom, last_px)

    # ------------------------------------------------------------------
    async def _on_signal(
        self, asset: str, direction: str, mom: float, spot: float,
    ) -> None:
        logger.info(
            "[%s] signal %s %s momentum=%+.3f%% spot=%.2f",
            STRATEGY_NAME, asset, direction, mom * 100, spot,
        )

        if self.pm.is_halted:
            self.db.log_latency_signal(
                asset, direction, mom, spot, 0.0, None, None,
                self.cfg.latency_trade_size, acted=False,
                skip_reason="risk halted",
            )
            return

        market: Optional[Market] = await self.client.find_crypto_15m_market(asset, direction)
        if not market:
            self.db.log_latency_signal(
                asset, direction, mom, spot, 0.0, None, None,
                self.cfg.latency_trade_size, acted=False,
                skip_reason="no matching market",
            )
            return

        # We want to buy YES on the matching market (UP market for UP signal,
        # DOWN market for DOWN signal). Polymarket returns one active market
        # per direction — its YES token encodes that direction.
        yes = market.yes_token
        if not yes:
            self.db.log_latency_signal(
                asset, direction, mom, spot, 0.0, None, market.question,
                self.cfg.latency_trade_size, acted=False,
                skip_reason="no YES token",
            )
            return

        try:
            book = await self.client.get_order_book(yes.token_id)
        except Exception as exc:
            logger.warning("[%s] book fetch failed: %s", STRATEGY_NAME, exc)
            return
        poly_yes = book.best_ask or yes.price
        if poly_yes is None:
            return

        # Repricing guard: skip if the book has already moved past our threshold.
        if direction == "UP" and poly_yes >= self.cfg.latency_up_max_price:
            self.db.log_latency_signal(
                asset, direction, mom, spot, poly_yes, yes.token_id,
                market.question, self.cfg.latency_trade_size, acted=False,
                skip_reason=f"YES {poly_yes:.3f} already above threshold",
            )
            return
        if direction == "DOWN" and poly_yes <= self.cfg.latency_down_min_price:
            self.db.log_latency_signal(
                asset, direction, mom, spot, poly_yes, yes.token_id,
                market.question, self.cfg.latency_trade_size, acted=False,
                skip_reason=f"YES {poly_yes:.3f} already below threshold",
            )
            return

        size = min(self.cfg.latency_trade_size, self.cfg.max_trade_size)
        ok, why = self.pm.can_trade(STRATEGY_NAME, market.condition_id, market.question, size)
        if not ok:
            self.db.log_latency_signal(
                asset, direction, mom, spot, poly_yes, yes.token_id,
                market.question, size, acted=False, skip_reason=why,
            )
            return

        # Shares = usd size / price
        shares = size / max(poly_yes, 0.01)

        # Place order
        try:
            res = await self.client.place_limit_order(
                token_id=yes.token_id, price=poly_yes, size=shares,
                side="BUY", order_type="GTC",
            )
        except Exception as exc:
            logger.error("[%s] order failed: %s", STRATEGY_NAME, exc)
            self.trade_logger.log_event("error", f"{STRATEGY_NAME} order failed", str(exc))
            return

        pos, err = await self.pm.try_open(
            strategy=STRATEGY_NAME,
            market_id=market.condition_id,
            market_q=market.question,
            side=f"BUY_YES_{direction}",
            size=size,
            entry_price=poly_yes,
            token_id=yes.token_id,
            condition_id=market.condition_id,
            meta={"asset": asset, "direction": direction, "momentum": mom},
        )
        if pos is None:
            logger.warning("[%s] pm.try_open failed post-submit: %s", STRATEGY_NAME, err)

        self.trade_logger.log_order(
            order_id=getattr(res, "order_id", "paper") or "paper",
            token_id=yes.token_id,
            market_q=market.question,
            side=f"BUY_YES_{direction}",
            price=poly_yes,
            size=shares,
            status=getattr(res, "status", "PAPER") or "PAPER",
            paper=self.cfg.paper_mode,
        )
        # Rough expected edge: direction vs. current implied prob.
        expected_edge = (1 - poly_yes) * 0.5  # heuristic
        sig_id = self.db.log_latency_signal(
            asset=asset, direction=direction, momentum_pct=mom, spot_price=spot,
            poly_yes_price=poly_yes, poly_token_id=yes.token_id,
            market_q=market.question, entry_size=size, acted=True, skip_reason=None,
        )

        self.telegram.latency_signal(
            asset=asset, direction=direction, momentum_pct=mom,
            poly_yes_price=poly_yes, size=size, expected_edge=expected_edge,
            paper=self.cfg.paper_mode,
        )

        # Schedule settlement ~16 min later.
        asyncio.create_task(self._settle_after(
            signal_id=sig_id,
            pos=pos,
            entry_price=poly_yes,
            shares=shares,
            size=size,
            asset=asset,
            direction=direction,
            token_id=yes.token_id,
        ))

    # ------------------------------------------------------------------
    async def _settle_after(
        self,
        signal_id: int,
        pos,
        entry_price: float,
        shares: float,
        size: float,
        asset: str,
        direction: str,
        token_id: str,
        wait_s: float = 16 * 60,
    ) -> None:
        """Wait until the 15-min contract should have settled, then mark PnL."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            return  # stopped
        except asyncio.TimeoutError:
            pass

        try:
            book = await self.client.get_order_book(token_id)
            settle_px = book.best_bid or 0.0
        except Exception:
            settle_px = 0.0

        # YES resolves 0 or 1 — assume whichever side is closer (best_bid as
        # proxy; if market closed this will be 0 or 1 exactly).
        final = 1.0 if settle_px > 0.5 else 0.0
        pnl = shares * (final - entry_price)
        outcome = "WIN" if pnl > 0 else "LOSS"

        self.db.settle_latency_signal(signal_id, pnl, outcome)
        if pos is not None:
            await self.pm.close(pos, pnl)
        self.telegram.latency_settled(
            asset=asset, direction=direction, pnl=pnl, outcome=outcome,
            paper=self.cfg.paper_mode,
        )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def _run_standalone(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = HybridConfig.from_env()
    for w in cfg.sanity_check():
        logger.warning("config: %s", w)

    db = HybridDatabase(cfg.db_path)
    trade_logger = TradeLogger()
    telegram = HybridTelegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    pm = PositionManager(
        total_capital=cfg.total_capital,
        max_exposure_pct=cfg.max_exposure_pct,
        max_trade_size=cfg.max_trade_size,
        daily_loss_limit=cfg.daily_loss_limit,
        max_correlated_open=cfg.max_correlated_open,
        initial_daily_pnl=db.combined_pnl_today(),
    )

    async with HybridPolymarketClient(cfg.polymarket) as client:
        strat = LatencyStrategy(cfg, client, db, trade_logger, telegram, pm)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, strat.request_stop)
            except NotImplementedError:
                pass

        telegram.hybrid_started([STRATEGY_NAME], cfg.paper_mode)
        await strat.run()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Binance→Polymarket latency arb")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(_run_standalone(_parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
