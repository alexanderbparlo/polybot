"""
Strategy 1 — Sub-$1 Rebalancing Arbitrage.

On a Polymarket binary market, YES + NO must settle to exactly $1.00. When
the sum of the current ASK prices (what we'd pay to buy both sides) drops
below 1 - edge, we can buy both legs and lock in a risk-free payoff.

Pipeline
--------
1. Pull top-N active, non-negRisk markets from Gamma (filtered by volume)
2. For each pair of (YES, NO) tokens, fetch order books concurrently
3. Require depth on each side ≥ ``arb_min_liquidity`` USDC at best ask
4. If YES.ask + NO.ask < 1 - edge, size the smaller of the two levels so
   both legs can be filled, capped by PositionManager / MAX_TRADE_SIZE
5. Fire both limit orders at the respective best ask (as maker-replacing
   takes if the book hasn't moved) concurrently via asyncio.gather
6. Log to DB + Telegram

Run standalone
--------------
    python strategy1_arbitrage.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from config import HybridConfig
from database import HybridDatabase
from logger import TradeLogger
from polymarket_client import HybridPolymarketClient
from polymarket.models import Market, OrderBook
from position_manager import PositionManager
from telegram_bot import HybridTelegram

logger = logging.getLogger(__name__)

STRATEGY_NAME = "arbitrage"


def _best_ask_with_depth(ob: OrderBook, min_usd: float) -> Optional[tuple[float, float]]:
    """Walk the ask ladder; return (price, shares_available) such that the
    cumulative USDC value at or below that price is ≥ ``min_usd``.

    Returns None if no price level can satisfy the depth requirement.
    We quote all legs at ``best_ask`` for simplicity — if depth at best_ask
    alone covers min_usd we use it; otherwise we skip. (Sweeping multiple
    levels would worsen fill price and eat the edge.)
    """
    if not ob.asks:
        return None
    best = ob.asks[0]
    depth_usd = best.price * best.size
    if depth_usd < min_usd:
        return None
    return best.price, best.size


class ArbitrageStrategy:
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
        # Markets we've already hit this cycle so we don't re-trigger during
        # repricing lag.
        self._cooldown: dict[str, datetime] = {}
        self._cooldown_secs = 60

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    async def run(self) -> None:
        logger.info("[%s] starting (paper=%s, edge=%.2f%%, liq=$%.0f)",
                    STRATEGY_NAME, self.cfg.paper_mode,
                    self.cfg.arb_min_edge * 100, self.cfg.arb_min_liquidity)
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("[%s] scan error: %s", STRATEGY_NAME, exc)
                self.trade_logger.log_event("error", f"{STRATEGY_NAME} scan failed", str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.arb_scan_interval)
            except asyncio.TimeoutError:
                pass
        logger.info("[%s] stopped", STRATEGY_NAME)

    # ------------------------------------------------------------------
    async def _scan_once(self) -> None:
        if self.pm.is_halted:
            logger.debug("[%s] skipped — risk halted", STRATEGY_NAME)
            return

        markets = await self.client.get_markets(
            limit=self.cfg.arb_market_limit,
            min_volume=1000.0,
            active_only=True,
            exclude_neg_risk=True,
        )
        if not markets:
            return

        # Gather token_ids from every YES/NO pair we can arb.
        pairs: list[tuple[Market, str, str]] = []
        token_ids: list[str] = []
        for m in markets:
            yes = m.yes_token
            no = m.no_token
            if not yes or not no:
                continue
            # cooldown
            last = self._cooldown.get(m.condition_id)
            if last and (datetime.utcnow() - last).total_seconds() < self._cooldown_secs:
                continue
            pairs.append((m, yes.token_id, no.token_id))
            token_ids.append(yes.token_id)
            token_ids.append(no.token_id)

        if not pairs:
            return

        books = await self.client.get_order_books(token_ids, concurrency=10)

        for market, yes_id, no_id in pairs:
            yes_ob = books.get(yes_id)
            no_ob = books.get(no_id)
            if not yes_ob or not no_ob:
                continue
            await self._eval_and_execute(market, yes_ob, no_ob)

    # ------------------------------------------------------------------
    async def _eval_and_execute(
        self, market: Market, yes_ob: OrderBook, no_ob: OrderBook,
    ) -> None:
        yes_best = _best_ask_with_depth(yes_ob, self.cfg.arb_min_liquidity)
        no_best = _best_ask_with_depth(no_ob, self.cfg.arb_min_liquidity)
        if yes_best is None or no_best is None:
            return

        yes_price, yes_shares = yes_best
        no_price, no_shares = no_best
        combined = yes_price + no_price
        if combined >= 1 - self.cfg.arb_min_edge:
            return  # no edge

        # Max shares we can fill on both legs at their respective best-ask levels
        max_shares = min(yes_shares, no_shares)

        # Per-share cost is ``combined``, per-share payoff is $1 guaranteed,
        # so per-share gross edge is (1 - combined). Fee model: winner-side
        # fee only (``arb_fee_rate`` applied to the winning $1 payout).
        gross_per_share = 1.0 - combined
        fee_per_share = self.cfg.arb_fee_rate * 1.0      # worst-case: winning leg only
        net_per_share = gross_per_share - fee_per_share
        if net_per_share <= 0:
            return

        # Size via PositionManager: need each LEG's capital (yes_price * shares)
        # and (no_price * shares) both to pass the risk check.
        # We scale down shares until total capital fits MAX_TRADE_SIZE and
        # global exposure cap.
        total_per_share = combined
        max_by_trade_size = self.cfg.max_trade_size / total_per_share
        headroom = max(0.0, self.pm.max_exposure_usd - self.pm.total_exposure())
        max_by_exposure = headroom / total_per_share if total_per_share > 0 else 0

        shares = min(max_shares, max_by_trade_size, max_by_exposure)
        if shares < 1:    # Polymarket shares are discrete; skip dust
            return

        capital = shares * combined
        gross_profit = shares * gross_per_share
        fee_cost = shares * fee_per_share
        net_profit = gross_profit - fee_cost

        # Gate via PositionManager (counts total capital)
        ok, why = self.pm.can_trade(
            STRATEGY_NAME, market.condition_id, market.question, capital,
        )
        if not ok:
            logger.info("[%s] skipped %s: %s", STRATEGY_NAME, market.question[:50], why)
            return

        spread_pct = (1.0 - combined)  # i.e. edge before fees
        logger.info(
            "[%s] ARB on %s: YES=%.3f NO=%.3f combined=%.3f shares=%.2f "
            "capital=$%.2f net=$%+.3f",
            STRATEGY_NAME, market.question[:50],
            yes_price, no_price, combined, shares, capital, net_profit,
        )

        await self._execute_both_legs(
            market, yes_ob.token_id, yes_price, no_ob.token_id, no_price, shares,
        )

        self.db.log_arb_trade(
            condition_id=market.condition_id,
            market_q=market.question,
            yes_price=yes_price,
            no_price=no_price,
            capital=capital,
            shares=shares,
            fee_cost=fee_cost,
            paper=self.cfg.paper_mode,
        )
        await self.pm.book_pnl(net_profit, strategy=STRATEGY_NAME)
        self.telegram.arb_opportunity(
            market=market.question,
            yes_price=yes_price,
            no_price=no_price,
            spread_pct=spread_pct,
            capital=capital,
            expected_profit=net_profit,
            paper=self.cfg.paper_mode,
        )
        self._cooldown[market.condition_id] = datetime.utcnow()

    # ------------------------------------------------------------------
    async def _execute_both_legs(
        self,
        market: Market,
        yes_token: str,
        yes_price: float,
        no_token: str,
        no_price: float,
        shares: float,
    ) -> None:
        """Fire both BUY orders as close to simultaneously as possible."""
        yes_task = asyncio.create_task(
            self.client.place_limit_order(
                token_id=yes_token, price=yes_price, size=shares, side="BUY",
                order_type="GTC",
            )
        )
        no_task = asyncio.create_task(
            self.client.place_limit_order(
                token_id=no_token, price=no_price, size=shares, side="BUY",
                order_type="GTC",
            )
        )
        yes_res, no_res = await asyncio.gather(yes_task, no_task, return_exceptions=True)

        for leg, res, tok, px in (
            ("YES", yes_res, yes_token, yes_price),
            ("NO", no_res, no_token, no_price),
        ):
            if isinstance(res, Exception):
                logger.error("[%s] %s leg failed: %s", STRATEGY_NAME, leg, res)
                self.trade_logger.log_event(
                    "error", f"arb {leg} leg failed", str(res),
                )
                continue
            self.trade_logger.log_order(
                order_id=getattr(res, "order_id", "paper") or "paper",
                token_id=tok,
                market_q=market.question,
                side=f"BUY_{leg}",
                price=px,
                size=shares,
                status=getattr(res, "status", "PAPER") or "PAPER",
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
        strat = ArbitrageStrategy(cfg, client, db, trade_logger, telegram, pm)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, strat.request_stop)
            except NotImplementedError:
                pass

        telegram.hybrid_started([STRATEGY_NAME], cfg.paper_mode)
        await strat.run()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket sub-$1 arbitrage strategy")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(_run_standalone(_parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
