"""
Cross-strategy position + risk accountant.

Shared singleton-ish object used by all three strategies to:

1. Track open positions (strategy, market, side, size, entry price, entry time)
2. Enforce caps:
   - total exposure ≤ ``max_exposure_pct`` × total capital
   - per-trade size ≤ ``max_trade_size``
   - no more than ``max_correlated_open`` positions in correlated markets
     (BTC/ETH/SOL crypto 15-min contracts)
   - daily PnL floor → halt bit flips, strategies must check ``is_halted``
3. Compute per-strategy exposure so the UI / report can show it.

Not async-safe across processes, but uses ``asyncio.Lock`` for in-process
concurrency between strategies. PnL deltas are booked live via
``book_pnl()``; daily losses are fetched from the DB at init and refreshed
whenever a strategy calls ``reload_daily_pnl()``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# Markets whose questions contain any of these tokens are bucketed as
# "crypto" for correlation purposes.
_CRYPTO_CORR = re.compile(r"\b(btc|bitcoin|eth|ethereum|sol|solana)\b", re.I)


def _correlation_bucket(market_question: str) -> Optional[str]:
    """Return a correlation bucket name or None.

    Today we only bucket crypto-15min style markets; anything else is
    treated as uncorrelated (``None``) for the cap check.
    """
    if market_question and _CRYPTO_CORR.search(market_question):
        return "crypto"
    return None


@dataclass
class ManagedPosition:
    strategy: str              # "arbitrage" | "latency" | "copy"
    market_id: str             # condition_id or token_id (strategy-specific)
    market_q: str
    side: str                  # "BUY"/"SELL" or "YES"/"NO"
    size: float                # USDC deployed
    entry_price: float
    token_id: Optional[str] = None
    condition_id: Optional[str] = None
    opened_at: datetime = field(default_factory=datetime.utcnow)
    meta: dict = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return (datetime.utcnow() - self.opened_at).total_seconds()


class PositionManager:
    """Global risk + position gatekeeper for all hybrid strategies."""

    def __init__(
        self,
        total_capital: float,
        max_exposure_pct: float = 0.30,
        max_trade_size: float = 100.0,
        daily_loss_limit: float = -50.0,
        max_correlated_open: int = 3,
        initial_daily_pnl: float = 0.0,
    ):
        self.total_capital = total_capital
        self.max_exposure_pct = max_exposure_pct
        self.max_trade_size = max_trade_size
        self.daily_loss_limit = daily_loss_limit
        self.max_correlated_open = max_correlated_open

        self._positions: list[ManagedPosition] = []
        self._daily_pnl: float = initial_daily_pnl
        self._halted: bool = False
        self._halt_reason: Optional[str] = None
        self._lock = asyncio.Lock()

    # ---- read-only views -------------------------------------------------
    @property
    def positions(self) -> list[ManagedPosition]:
        return list(self._positions)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    @property
    def max_exposure_usd(self) -> float:
        return self.total_capital * self.max_exposure_pct

    def total_exposure(self) -> float:
        return sum(p.size for p in self._positions)

    def exposure_by_strategy(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self._positions:
            out[p.strategy] = out.get(p.strategy, 0.0) + p.size
        return out

    def exposure_in_market(self, market_id: str) -> float:
        return sum(p.size for p in self._positions if p.market_id == market_id)

    # ---- gatekeeping -----------------------------------------------------
    def can_trade(
        self,
        strategy: str,
        market_id: str,
        market_q: str,
        size: float,
    ) -> tuple[bool, Optional[str]]:
        """Return (allowed, reason_if_not)."""
        if self._halted:
            return False, f"halted: {self._halt_reason}"
        if size <= 0:
            return False, "size must be positive"
        if size > self.max_trade_size:
            return False, f"size ${size:.2f} exceeds MAX_TRADE_SIZE ${self.max_trade_size:.2f}"
        projected = self.total_exposure() + size
        if projected > self.max_exposure_usd:
            return False, (
                f"exposure ${projected:.2f} would exceed cap "
                f"${self.max_exposure_usd:.2f}"
            )
        bucket = _correlation_bucket(market_q)
        if bucket:
            n_corr = sum(
                1 for p in self._positions
                if _correlation_bucket(p.market_q) == bucket
            )
            if n_corr >= self.max_correlated_open:
                return False, (
                    f"{n_corr} correlated ({bucket}) positions already open "
                    f"(max {self.max_correlated_open})"
                )
        return True, None

    async def try_open(
        self,
        strategy: str,
        market_id: str,
        market_q: str,
        side: str,
        size: float,
        entry_price: float,
        token_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> tuple[Optional[ManagedPosition], Optional[str]]:
        async with self._lock:
            ok, why = self.can_trade(strategy, market_id, market_q, size)
            if not ok:
                return None, why
            pos = ManagedPosition(
                strategy=strategy,
                market_id=market_id,
                market_q=market_q,
                side=side,
                size=size,
                entry_price=entry_price,
                token_id=token_id,
                condition_id=condition_id,
                meta=meta or {},
            )
            self._positions.append(pos)
            logger.info(
                "[pm] opened %s pos on %s size=$%.2f @ %.3f (total exposure=$%.2f)",
                strategy, market_q[:40], size, entry_price, self.total_exposure(),
            )
            return pos, None

    async def close(self, pos: ManagedPosition, pnl: float) -> None:
        async with self._lock:
            if pos in self._positions:
                self._positions.remove(pos)
            self._daily_pnl += pnl
            logger.info(
                "[pm] closed %s pos pnl=$%+.3f daily=$%+.2f",
                pos.strategy, pnl, self._daily_pnl,
            )
            self._check_daily_loss()

    async def book_pnl(self, delta: float, strategy: Optional[str] = None) -> None:
        """Record PnL that isn't tied to a managed position (e.g. arb)."""
        async with self._lock:
            self._daily_pnl += delta
            if strategy:
                logger.info("[pm] booked %s pnl $%+.3f daily=$%+.2f",
                            strategy, delta, self._daily_pnl)
            self._check_daily_loss()

    def reload_daily_pnl(self, pnl: float) -> None:
        """Sync our in-memory PnL to the DB's authoritative value."""
        self._daily_pnl = pnl
        self._check_daily_loss()

    def _check_daily_loss(self) -> None:
        if not self._halted and self._daily_pnl <= self.daily_loss_limit:
            self._halted = True
            self._halt_reason = (
                f"daily PnL ${self._daily_pnl:+.2f} breached "
                f"limit ${self.daily_loss_limit:+.2f}"
            )
            logger.warning("[pm] HALT: %s", self._halt_reason)

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        logger.warning("[pm] HALT: %s", reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = None
        logger.info("[pm] resumed (daily pnl=$%+.2f)", self._daily_pnl)

    # ---- snapshot for /report -------------------------------------------
    def snapshot(self) -> dict:
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "total_capital": self.total_capital,
            "total_exposure": self.total_exposure(),
            "max_exposure": self.max_exposure_usd,
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": self.daily_loss_limit,
            "by_strategy": self.exposure_by_strategy(),
            "open_positions": [
                {
                    "strategy": p.strategy,
                    "market": p.market_q,
                    "side": p.side,
                    "size": p.size,
                    "entry": p.entry_price,
                    "age_s": p.age_seconds,
                }
                for p in self._positions
            ],
        }
