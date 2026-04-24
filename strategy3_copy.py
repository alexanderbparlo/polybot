"""
Strategy 3 — Wallet Copy Trading.

Watches a configurable list of target wallets on Polymarket. Every
``COPY_POLL_INTERVAL`` seconds we pull each wallet's recent activity from
the Polymarket Data API, detect new trades by timestamp/id, score the
wallet, apply a copy-filter, and mirror qualifying trades at
``COPY_SIZE_FRACTION`` of their size (capped by ``COPY_SIZE_CAP`` and
MAX_TRADE_SIZE).

Components
----------
* ``WalletScanner``   — fetches activity + computes raw metrics per wallet
* ``WalletScorer``    — combines ROI / win-rate / diversity / recency / n
* ``CopyFilter``      — pre-trade gating (liquidity, price drift, streak,
                        per-market exposure cap)
* ``CopyStrategy``    — main loop: rescore hourly, poll activity every N s

Schema for a "trade" as returned by the Data API is best-effort; we tolerate
missing fields (type, side, price, size, market question/condition_id,
timestamp) and skip records we can't fully parse.

Run standalone:
    python strategy3_copy.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

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

STRATEGY_NAME = "copy"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class WalletActivity:
    trade_id: str
    timestamp: float          # unix seconds
    side: str                 # BUY / SELL
    token_id: Optional[str]
    condition_id: Optional[str]
    market_q: str
    price: float
    size: float               # USDC value


def _parse_activity(raw: dict) -> Optional[WalletActivity]:
    """Best-effort parse of a Data-API activity entry."""
    try:
        trade_id = str(raw.get("transactionHash") or raw.get("id") or raw.get("txHash") or "")
        if not trade_id:
            return None
        side = str(raw.get("side") or raw.get("action") or "BUY").upper()
        token_id = raw.get("asset") or raw.get("tokenId") or raw.get("token_id")
        condition_id = raw.get("conditionId") or raw.get("marketId")
        market_q = (
            raw.get("title")
            or raw.get("question")
            or raw.get("marketQuestion")
            or raw.get("eventTitle")
            or ""
        )
        price = float(raw.get("price") or raw.get("pricePerShare") or 0.0)
        size = float(raw.get("usdcSize") or raw.get("size") or raw.get("shares") or 0.0)
        if price <= 0 or size <= 0:
            return None
        ts_raw = raw.get("timestamp") or raw.get("ts") or raw.get("createdAt")
        ts = _to_epoch(ts_raw)
        return WalletActivity(
            trade_id=trade_id, timestamp=ts, side=side,
            token_id=str(token_id) if token_id else None,
            condition_id=str(condition_id) if condition_id else None,
            market_q=market_q, price=price, size=size,
        )
    except Exception:
        return None


def _to_epoch(ts: Any) -> float:
    if ts is None:
        return time.time()
    if isinstance(ts, (int, float)):
        # Heuristic: values > 10^12 are ms
        return float(ts) / 1000.0 if float(ts) > 1e12 else float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


# ---------------------------------------------------------------------------
# Scanner + Scorer + Filter
# ---------------------------------------------------------------------------

class WalletScanner:
    """Pulls raw activity for a list of wallets via the Polymarket Data API."""

    def __init__(self, client: HybridPolymarketClient):
        self.client = client

    async def fetch(self, address: str, limit: int = 200) -> list[WalletActivity]:
        raw = await self.client.get_wallet_activity(address, limit=limit)
        acts = [_parse_activity(r) for r in raw]
        return [a for a in acts if a]


class WalletScorer:
    """Scores a wallet on ROI / win-rate / activity / diversity / recency."""

    def __init__(self, cfg: HybridConfig):
        self.cfg = cfg

    def score(self, acts: list[WalletActivity]) -> dict[str, Any]:
        """Compute score components. Returns {} if the wallet is unrankable."""
        if not acts or len(acts) < self.cfg.copy_scorer_min_trades:
            return {
                "score": 0.0,
                "roi_30d": 0.0,
                "win_rate": 0.0,
                "n_trades": len(acts),
                "last_active": _iso(max((a.timestamp for a in acts), default=0)),
                "diversity": 0.0,
                "losing_streak": 0,
                "reason": "insufficient trade history",
            }

        cutoff = time.time() - self.cfg.copy_scorer_lookback_days * 86400
        recent = [a for a in acts if a.timestamp >= cutoff]
        n = len(recent)
        if n == 0:
            recent = acts
            n = len(recent)

        # ROI proxy: sum of (sell_size - buy_size_same_market) is hard from
        # activity alone. We approximate with (wins-losses)*avg_size / total_cap.
        # Wins = trades where current market implied price moved favourably
        # after their entry — we don't know the outcome here, so we use
        # a proxy: BUY < 0.5 → "bullish" side; if subsequent SELL at higher
        # price on same market exists, count as win.
        wins, losses = 0, 0
        bought: dict[str, list[WalletActivity]] = {}
        for a in sorted(recent, key=lambda x: x.timestamp):
            key = a.condition_id or a.token_id or a.market_q
            if not key:
                continue
            if a.side == "BUY":
                bought.setdefault(key, []).append(a)
            elif a.side == "SELL" and key in bought and bought[key]:
                prev = bought[key].pop(0)
                if a.price > prev.price:
                    wins += 1
                else:
                    losses += 1
        closed = wins + losses
        win_rate = wins / closed if closed else 0.0

        # ROI approximation: sum of realised PnL / total capital used.
        pnl = 0.0
        cap = 0.0
        for a in recent:
            cap += a.size
        # Using same pairing as above:
        for key, lots in bought.items():
            pass  # remaining lots open — skip
        # Rebuild pnl from pairs:
        pnl_bought: dict[str, list[WalletActivity]] = {}
        for a in sorted(recent, key=lambda x: x.timestamp):
            key = a.condition_id or a.token_id or a.market_q
            if not key:
                continue
            if a.side == "BUY":
                pnl_bought.setdefault(key, []).append(a)
            elif a.side == "SELL" and pnl_bought.get(key):
                prev = pnl_bought[key].pop(0)
                pnl += (a.price - prev.price) * (a.size / max(a.price, 0.01))
        roi = pnl / cap if cap > 0 else 0.0

        # Diversity: unique markets / trades (bounded [0, 1])
        unique_markets = {a.condition_id or a.token_id or a.market_q for a in recent}
        diversity = min(1.0, len(unique_markets) / max(n / 3, 1))

        # Recency: last activity within half the lookback window → 1.0; older → 0
        last_ts = max((a.timestamp for a in recent), default=0)
        age_days = max(0.0, (time.time() - last_ts) / 86400)
        recency = max(0.0, 1.0 - age_days / max(self.cfg.copy_scorer_lookback_days, 1))

        # Losing streak: count trailing LOSS events by timestamp
        streak = 0
        for a in reversed(sorted(recent, key=lambda x: x.timestamp)):
            if a.side != "SELL":
                continue
            key = a.condition_id or a.token_id or a.market_q
            # crude: if any prior BUY at higher price in same market → loss
            prior_buys = [
                x for x in recent
                if x.side == "BUY" and (x.condition_id or x.token_id or x.market_q) == key
                and x.timestamp < a.timestamp
            ]
            if prior_buys and a.price < prior_buys[-1].price:
                streak += 1
            else:
                break

        # Score weights: ROI 35%, win_rate 25%, volume(n) 15%, recency 15%, diversity 10%
        n_score = min(1.0, n / max(self.cfg.copy_scorer_min_trades * 3, 1))
        composite = (
            0.35 * max(-1.0, min(1.0, roi))
            + 0.25 * win_rate
            + 0.15 * n_score
            + 0.15 * recency
            + 0.10 * diversity
        )
        composite = max(0.0, composite)  # no negative scores in the table

        return {
            "score": composite,
            "roi_30d": roi,
            "win_rate": win_rate,
            "n_trades": n,
            "last_active": _iso(last_ts),
            "diversity": diversity,
            "losing_streak": streak,
        }


class CopyFilter:
    """Pre-trade filter — returns (ok, reason_if_not)."""

    def __init__(
        self,
        cfg: HybridConfig,
        client: HybridPolymarketClient,
        db: HybridDatabase,
        pm: PositionManager,
    ):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.pm = pm

    async def check(
        self,
        wallet: str,
        act: WalletActivity,
    ) -> tuple[bool, Optional[str], Optional[Market], Optional[float]]:
        """Returns (allowed, reason, market, current_ask_price)."""
        if act.side != "BUY":
            return False, "not a BUY", None, None

        # Resolve market
        if not act.condition_id:
            return False, "missing condition_id", None, None
        try:
            market = await self.client.base.gamma.get_market(act.condition_id)
        except Exception as exc:
            return False, f"market lookup failed: {exc}", None, None
        if market is None or not market.active:
            return False, "market inactive/missing", None, None

        # Figure out the token we'd buy. Default: match the side the wallet
        # took (if we can tell); fallback: YES token.
        token = None
        if act.token_id:
            for t in market.tokens:
                if t.token_id == act.token_id:
                    token = t
                    break
        if token is None:
            token = market.yes_token
        if token is None:
            return False, "cannot resolve token to copy", market, None

        # Liquidity check (>$1,000 each side)
        try:
            book = await self.client.get_order_book(token.token_id)
        except Exception as exc:
            return False, f"book fetch failed: {exc}", market, None

        def _side_usd(levels) -> float:
            return sum(l.price * l.size for l in levels[:5])
        if _side_usd(book.bids) < self.cfg.copy_market_min_liq:
            return False, f"bid depth < ${self.cfg.copy_market_min_liq:.0f}", market, None
        if _side_usd(book.asks) < self.cfg.copy_market_min_liq:
            return False, f"ask depth < ${self.cfg.copy_market_min_liq:.0f}", market, None

        best_ask = book.best_ask
        if best_ask is None:
            return False, "no ask", market, None

        # Price drift check: skip if moved > max_price_slip from their fill.
        drift = abs(best_ask - act.price)
        if drift > self.cfg.copy_max_price_slip:
            return False, f"price drifted {drift:.3f} from wallet entry", market, best_ask

        # Losing-streak check on wallet
        ws = self.db.get_wallet_score(wallet)
        if ws and ws.get("losing_streak", 0) >= 3:
            return False, f"wallet on {ws['losing_streak']}-trade losing streak", market, best_ask

        # Per-market exposure cap
        market_cap = self.cfg.total_capital * self.cfg.copy_market_max_exposure_pct
        if self.pm.exposure_in_market(market.condition_id) >= market_cap:
            return (
                False,
                f"market exposure already ≥ {self.cfg.copy_market_max_exposure_pct*100:.0f}% of capital",
                market,
                best_ask,
            )

        return True, None, market, best_ask


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _iso(ts: float) -> str:
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


class CopyStrategy:
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
        self.scanner = WalletScanner(client)
        self.scorer = WalletScorer(cfg)
        self.filter = CopyFilter(cfg, client, db, position_manager)
        # In-memory highwater marks so we don't re-copy the same trade.
        self._seen_trades: dict[str, set[str]] = {}

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        if not self.cfg.copy_wallets:
            logger.warning(
                "[%s] no COPY_WALLETS configured — strategy will idle", STRATEGY_NAME,
            )
        else:
            logger.info(
                "[%s] starting (paper=%s, wallets=%d, poll=%.0fs)",
                STRATEGY_NAME, self.cfg.paper_mode,
                len(self.cfg.copy_wallets), self.cfg.copy_poll_interval,
            )

        # Initial scoring pass
        await self._rescore_all()
        last_rescore = time.time()
        rescore_interval = 3600.0  # hourly

        while not self._stop.is_set():
            try:
                if time.time() - last_rescore > rescore_interval:
                    await self._rescore_all()
                    last_rescore = time.time()
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] poll error: %s", STRATEGY_NAME, exc)
                self.trade_logger.log_event(
                    "error", f"{STRATEGY_NAME} poll failed", str(exc),
                )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.copy_poll_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("[%s] stopped", STRATEGY_NAME)

    # ------------------------------------------------------------------
    async def _rescore_all(self) -> None:
        for w in self.cfg.copy_wallets:
            try:
                acts = await self.scanner.fetch(w, limit=300)
            except Exception as exc:
                logger.warning("[%s] scanner failed for %s: %s", STRATEGY_NAME, w, exc)
                continue
            s = self.scorer.score(acts)
            self.db.upsert_wallet_score(
                address=w,
                score=s.get("score", 0.0),
                roi_30d=s.get("roi_30d", 0.0),
                win_rate=s.get("win_rate", 0.0),
                n_trades=s.get("n_trades", 0),
                last_active=s.get("last_active", ""),
                diversity=s.get("diversity", 0.0),
                losing_streak=s.get("losing_streak", 0),
                metadata={k: v for k, v in s.items() if k == "reason"},
            )
        top = self.db.top_wallets(10)
        self.telegram.wallet_ranked(top)

    # ------------------------------------------------------------------
    async def _poll_once(self) -> None:
        for wallet in self.cfg.copy_wallets:
            if self.pm.is_halted:
                return
            try:
                acts = await self.scanner.fetch(wallet, limit=25)
            except Exception as exc:
                logger.debug("[%s] fetch %s failed: %s", STRATEGY_NAME, wallet, exc)
                continue

            seen = self._seen_trades.setdefault(wallet, set())
            # Process oldest first so chronology makes sense
            for act in sorted(acts, key=lambda a: a.timestamp):
                if act.trade_id in seen:
                    continue
                seen.add(act.trade_id)
                # Only act on very fresh trades (<30 s old by default)
                if time.time() - act.timestamp > 2 * self.cfg.copy_poll_interval:
                    continue
                await self._consider_copy(wallet, act)

    # ------------------------------------------------------------------
    async def _consider_copy(self, wallet: str, act: WalletActivity) -> None:
        ok, reason, market, ask_px = await self.filter.check(wallet, act)
        market_q = market.question if market else act.market_q
        cid = market.condition_id if market else act.condition_id

        if not ok:
            self.db.log_copy_decision(
                wallet=wallet, condition_id=cid, market_q=market_q,
                their_price=act.price, their_size=act.size,
                our_price=ask_px, our_size=None,
                executed=False, skip_reason=reason,
                paper=self.cfg.paper_mode,
            )
            self.telegram.copy_skipped(wallet, market_q, reason or "?")
            return

        # Sizing
        our_size_usd = min(
            act.size * self.cfg.copy_size_fraction,
            self.cfg.copy_size_cap,
            self.cfg.max_trade_size,
        )
        if our_size_usd < 1:
            self.db.log_copy_decision(
                wallet=wallet, condition_id=cid, market_q=market_q,
                their_price=act.price, their_size=act.size,
                our_price=ask_px, our_size=our_size_usd,
                executed=False, skip_reason="size below $1",
                paper=self.cfg.paper_mode,
            )
            return

        gate_ok, why = self.pm.can_trade(STRATEGY_NAME, cid or "", market_q, our_size_usd)
        if not gate_ok:
            self.db.log_copy_decision(
                wallet=wallet, condition_id=cid, market_q=market_q,
                their_price=act.price, their_size=act.size,
                our_price=ask_px, our_size=our_size_usd,
                executed=False, skip_reason=why or "pm rejected",
                paper=self.cfg.paper_mode,
            )
            return

        # Execute
        token = None
        if act.token_id and market:
            token = next((t for t in market.tokens if t.token_id == act.token_id), None)
        if token is None and market:
            token = market.yes_token
        if token is None:
            self.db.log_copy_decision(
                wallet=wallet, condition_id=cid, market_q=market_q,
                their_price=act.price, their_size=act.size,
                our_price=ask_px, our_size=our_size_usd,
                executed=False, skip_reason="no token to buy",
                paper=self.cfg.paper_mode,
            )
            return

        shares = our_size_usd / max(ask_px or 0.01, 0.01)
        try:
            res = await self.client.place_limit_order(
                token_id=token.token_id, price=ask_px or 0.01, size=shares,
                side="BUY", order_type="GTC",
            )
        except Exception as exc:
            logger.error("[%s] copy order failed: %s", STRATEGY_NAME, exc)
            self.trade_logger.log_event("error", f"{STRATEGY_NAME} order failed", str(exc))
            self.db.log_copy_decision(
                wallet=wallet, condition_id=cid, market_q=market_q,
                their_price=act.price, their_size=act.size,
                our_price=ask_px, our_size=our_size_usd,
                executed=False, skip_reason=f"order err: {exc}",
                paper=self.cfg.paper_mode,
            )
            return

        await self.pm.try_open(
            strategy=STRATEGY_NAME,
            market_id=cid or token.token_id,
            market_q=market_q,
            side="BUY",
            size=our_size_usd,
            entry_price=ask_px or 0.01,
            token_id=token.token_id,
            condition_id=cid,
            meta={"wallet": wallet, "source_trade_id": act.trade_id},
        )
        self.trade_logger.log_order(
            order_id=getattr(res, "order_id", "paper") or "paper",
            token_id=token.token_id,
            market_q=market_q,
            side="BUY_COPY",
            price=ask_px or 0.0,
            size=shares,
            status=getattr(res, "status", "PAPER") or "PAPER",
            paper=self.cfg.paper_mode,
        )
        self.db.log_copy_decision(
            wallet=wallet, condition_id=cid, market_q=market_q,
            their_price=act.price, their_size=act.size,
            our_price=ask_px, our_size=our_size_usd,
            executed=True, skip_reason=None,
            paper=self.cfg.paper_mode,
        )
        self.telegram.copy_trade(
            wallet=wallet, market=market_q,
            their_size=act.size, our_size=our_size_usd,
            price=ask_px or 0.0, paper=self.cfg.paper_mode,
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
        strat = CopyStrategy(cfg, client, db, trade_logger, telegram, pm)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, strat.request_stop)
            except NotImplementedError:
                pass

        telegram.hybrid_started([STRATEGY_NAME], cfg.paper_mode)
        await strat.run()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket wallet copy-trading strategy")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(_run_standalone(_parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
