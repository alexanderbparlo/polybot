from __future__ import annotations
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from polymarket import PolymarketClient, SimPosition
from polymarket.models import Market, MarketToken, OrderBook
from logger import TradeLogger, TelegramAlert

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class ScalperConfig:
    # Market filters
    min_spread_cents: float = 4.0
    min_volume_24h: float = 10_000
    min_book_depth: float = 50        # USDC value
    max_markets_watched: int = 20
    scan_interval_secs: float = 2.0

    # Entry
    entry_offset_cents: float = 0.5
    min_momentum_score: float = 0.60
    min_composite_score: float = 0.55

    # Exit
    target_profit_cents: float = 2.5
    stop_loss_cents: float = 3.5
    max_hold_seconds: int = 300
    exit_offset_cents: float = 0.5
    stale_order_secs: float = 60.0

    # Sizing
    bankroll_pct_per_trade: float = 0.03
    max_position_usd: float = 100.0

    # Risk
    max_open_positions: int = 3
    daily_loss_limit_usd: float = 50.0
    max_consecutive_losses: int = 4

    # Fee model
    fee_rate: float = 0.02

    # Signal weights
    weight_spread: float = 0.35
    weight_momentum: float = 0.45
    weight_liquidity: float = 0.20

    @classmethod
    def conservative(cls) -> "ScalperConfig":
        return cls(
            min_spread_cents=5.0,
            max_position_usd=50.0,
            bankroll_pct_per_trade=0.02,
            max_open_positions=2,
            daily_loss_limit_usd=25.0,
            max_consecutive_losses=3,
        )

    @classmethod
    def aggressive(cls) -> "ScalperConfig":
        return cls(
            min_spread_cents=3.5,
            max_position_usd=200.0,
            bankroll_pct_per_trade=0.05,
            max_open_positions=5,
            daily_loss_limit_usd=150.0,
        )


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    spread_score: float
    momentum_score: float
    liquidity_score: float
    composite_score: float
    recommended_side: str   # "BUY" or "SELL"
    entry_price: float


def score_spread(book: OrderBook, cfg: ScalperConfig) -> float:
    sc = book.spread_cents
    if sc < cfg.min_spread_cents:
        return 0.0
    # Linear scale: 4¢ → 0.0, 15¢ → 1.0
    return min((sc - cfg.min_spread_cents) / (15.0 - cfg.min_spread_cents), 1.0)


def score_momentum(
    history: deque,     # deque of OrderBook snapshots (newest last)
    book: OrderBook,
    cfg: ScalperConfig,
) -> tuple[float, str]:
    """
    Analyse rolling window of book depth snapshots.
    Returns (score 0–1, recommended_side "BUY"/"SELL").
    """
    if not book.best_bid or not book.best_ask:
        return 0.0, "BUY"

    total_depth = book.bid_depth + book.ask_depth
    if total_depth == 0:
        return 0.0, "BUY"

    # Instantaneous imbalance: bid-heavy → bullish (BUY YES)
    imbalance = book.bid_depth / total_depth  # 0–1; >0.5 = bid-heavy

    # Rolling window trend: compare oldest vs newest snapshot depths
    bid_trend = 0.0
    ask_trend = 0.0
    if len(history) >= 2:
        old = history[0]
        new_snap = history[-1]
        bid_change = new_snap.bid_depth - old.bid_depth
        ask_change = new_snap.ask_depth - old.ask_depth
        scale = max(old.bid_depth + old.ask_depth, 1.0)
        bid_trend = bid_change / scale     # positive = growing bids
        ask_trend = ask_change / scale     # positive = growing asks

    # Bullish: growing bids, thinning asks + bid-heavy imbalance
    bull_signal = max(bid_trend, 0) - min(ask_trend, 0) + (imbalance - 0.5)
    # Bearish: growing asks, thinning bids + ask-heavy imbalance
    bear_signal = max(ask_trend, 0) - min(bid_trend, 0) + (0.5 - imbalance)

    if bull_signal >= bear_signal:
        raw_score = min(bull_signal + 0.5, 1.0)
        side = "BUY"
    else:
        raw_score = min(bear_signal + 0.5, 1.0)
        side = "SELL"

    return max(0.0, min(raw_score, 1.0)), side


def score_liquidity(book: OrderBook, target_size: float, cfg: ScalperConfig) -> float:
    if book.bid_depth < cfg.min_book_depth or book.ask_depth < cfg.min_book_depth:
        return 0.0
    target_depth = target_size * 3  # 3× target at each level
    bid_score = min(book.bid_depth / max(target_depth, 1.0), 1.0)
    ask_score = min(book.ask_depth / max(target_depth, 1.0), 1.0)
    return (bid_score + ask_score) / 2


def compute_signal(
    book: OrderBook,
    history: deque,
    target_size: float,
    cfg: ScalperConfig,
) -> Optional[SignalResult]:
    spread_s = score_spread(book, cfg)
    if spread_s == 0.0:
        return None

    momentum_s, side = score_momentum(history, book, cfg)
    liquidity_s = score_liquidity(book, target_size, cfg)

    composite = (
        cfg.weight_spread * spread_s
        + cfg.weight_momentum * momentum_s
        + cfg.weight_liquidity * liquidity_s
    )

    if composite < cfg.min_composite_score or momentum_s < cfg.min_momentum_score:
        return None

    # Entry price: inside the spread, offset from best bid
    offset = cfg.entry_offset_cents / 100
    if side == "BUY":
        entry_price = round(min(book.best_bid + offset, book.best_ask - 0.01), 2)
    else:
        entry_price = round(max(book.best_ask - offset, book.best_bid + 0.01), 2)

    return SignalResult(
        spread_score=spread_s,
        momentum_score=momentum_s,
        liquidity_score=liquidity_s,
        composite_score=composite,
        recommended_side=side,
        entry_price=entry_price,
    )


# ---------------------------------------------------------------------------
# Main scalper engine
# ---------------------------------------------------------------------------

class SpreadScalper:
    """
    Core strategy engine. Runs the market scan → signal → order → exit loop.
    """

    BOOK_HISTORY_SIZE = 5   # Rolling window length

    def __init__(
        self,
        client: PolymarketClient,
        cfg: ScalperConfig,
        trade_logger: TradeLogger,
        telegram: TelegramAlert,
    ):
        self.client = client
        self.cfg = cfg
        self.db = trade_logger
        self.tg = telegram
        self.paper = client.config.paper_mode

        # State
        self.open_positions: list[SimPosition] = []
        self.book_history: dict[str, deque] = {}    # token_id → deque[OrderBook]
        self.circuit_open = False
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self._watched_tokens: list[tuple[Market, MarketToken]] = []

    async def run(self) -> None:
        """Main loop — never returns unless circuit breaker fires or cancelled."""
        mode = "[PAPER]" if self.paper else "[LIVE]"
        logger.info("%s SpreadScalper starting", mode)

        balance = await self.client.get_balance()
        markets = await self._scan_markets()
        self.tg.bot_started(bankroll=balance, markets_watching=len(markets))

        while True:
            if self.circuit_open:
                logger.warning("Circuit breaker open — sleeping 60s")
                await asyncio.sleep(60)
                continue

            await self._check_exits()
            await self._scan_and_enter(balance)
            await asyncio.sleep(self.cfg.scan_interval_secs)

    async def _scan_markets(self) -> list[Market]:
        """Refresh market watchlist."""
        all_markets = await self.client.get_markets(
            limit=50,
            min_volume=self.cfg.min_volume_24h,
            exclude_neg_risk=True,
        )
        # Take top N by volume
        all_markets.sort(key=lambda m: m.volume_24h, reverse=True)
        top = all_markets[: self.cfg.max_markets_watched]
        # Build (market, token) pairs for YES tokens only
        self._watched_tokens = []
        for m in top:
            if m.yes_token:
                self._watched_tokens.append((m, m.yes_token))
        logger.info("Watching %d tokens", len(self._watched_tokens))
        return top

    async def _scan_and_enter(self, balance: float) -> None:
        if len(self.open_positions) >= self.cfg.max_open_positions:
            return

        position_usd = min(
            balance * self.cfg.bankroll_pct_per_trade,
            self.cfg.max_position_usd,
        )

        for market, token in self._watched_tokens:
            if len(self.open_positions) >= self.cfg.max_open_positions:
                break
            # Skip if already holding this token
            if any(p.token_id == token.token_id for p in self.open_positions):
                continue

            try:
                book = await self.client.get_order_book(token.token_id)
            except Exception as exc:
                logger.debug("Book fetch failed for %s: %s", token.token_id[:8], exc)
                continue

            # Update rolling history
            hist = self.book_history.setdefault(
                token.token_id, deque(maxlen=self.BOOK_HISTORY_SIZE)
            )
            hist.append(book)

            if book.best_bid is None or book.best_ask is None:
                continue

            target_size = position_usd / book.mid if book.mid else 0
            signal = compute_signal(book, hist, target_size, self.cfg)
            if signal is None:
                continue

            logger.info(
                "Signal on %s | composite=%.2f momentum=%.2f spread=%.1f¢ → %s @ %.3f",
                market.question[:50],
                signal.composite_score,
                signal.momentum_score,
                book.spread_cents,
                signal.recommended_side,
                signal.entry_price,
            )

            size = round(position_usd / signal.entry_price, 2)
            entry_fee = signal.entry_price * size * self.cfg.fee_rate
            stop = signal.entry_price - self.cfg.stop_loss_cents / 100
            target = signal.entry_price + self.cfg.target_profit_cents / 100

            # Place or simulate order
            resp = await self.client.place_limit_order(
                token.token_id, signal.entry_price, size, signal.recommended_side
            )
            prefix = "[PAPER] " if self.paper else ""
            self.tg.order_placed(
                market.question, signal.recommended_side,
                signal.entry_price, size, resp.order_id, paper=self.paper
            )
            self.db.log_order(
                resp.order_id, token.token_id, market.question,
                signal.recommended_side, signal.entry_price, size, resp.status, self.paper
            )

            # Track as simulated position (used for both paper and live until fill confirmed)
            pos = SimPosition(
                token_id=token.token_id,
                market_question=market.question,
                side=signal.recommended_side,
                entry_price=signal.entry_price,
                size=size,
                entry_time=datetime.utcnow(),
                entry_fee=entry_fee,
                stop_price=stop,
                target_price=target,
            )
            self.open_positions.append(pos)

    async def _check_exits(self) -> None:
        now = datetime.utcnow()
        closed: list[SimPosition] = []

        for pos in list(self.open_positions):
            try:
                book = await self.client.get_order_book(pos.token_id)
            except Exception:
                continue

            mid = book.mid
            if mid is None:
                continue

            hold_secs = (now - pos.entry_time).total_seconds()
            exit_price: Optional[float] = None
            reason: Optional[str] = None

            # Stop loss
            if mid <= pos.stop_price:
                exit_price = mid
                reason = "stop"
                self.tg.stop_loss_triggered(
                    pos.market_question, pos.entry_price, mid,
                    (mid - pos.entry_price) * pos.size, paper=self.paper
                )

            # Target profit
            elif mid >= pos.target_price:
                offset = self.cfg.exit_offset_cents / 100
                exit_price = round(mid - offset, 3)
                reason = "target"

            # Time stop
            elif hold_secs >= self.cfg.max_hold_seconds:
                exit_price = mid
                reason = "time"

            if exit_price is not None and reason is not None:
                exit_fee = exit_price * pos.size * self.cfg.fee_rate
                pos.exit_price = exit_price
                pos.exit_time = now
                pos.exit_reason = reason
                pos.exit_fee = exit_fee
                net = pos.net_pnl
                self.daily_pnl += net

                if net < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0

                self.tg.trade_filled(
                    pos.market_question, pos.side, pos.entry_price,
                    exit_price, pos.size, net, hold_secs, paper=self.paper
                )
                self.db.log_trade(
                    pos.token_id, pos.market_question, pos.side,
                    pos.entry_price, exit_price, pos.size, net,
                    pos.entry_fee, exit_fee, hold_secs, reason, self.paper
                )
                self.db.log_event("INFO", f"Trade closed: {reason}", f"pnl={net:.4f}")
                closed.append(pos)

                # Circuit breaker checks
                if self.daily_pnl <= -self.cfg.daily_loss_limit_usd:
                    self._trigger_circuit("Daily loss limit reached")
                elif self.consecutive_losses >= self.cfg.max_consecutive_losses:
                    self._trigger_circuit(f"{self.consecutive_losses} consecutive losses")

        for pos in closed:
            self.open_positions.remove(pos)

    def _trigger_circuit(self, reason: str) -> None:
        self.circuit_open = True
        resume = "manually (restart bot)"
        logger.warning("CIRCUIT BREAKER: %s", reason)
        self.tg.circuit_breaker(reason, self.daily_pnl, resume)
        self.db.log_event("WARNING", "Circuit breaker triggered", reason)
