"""
Backtest simulation engine.

Replays historical candle data through the spread scalping strategy.
Fill simulation rules:
  - Limit BUY fills when candle low <= bid price
  - Limit SELL fills when candle high >= ask price
  - Market orders fill at candle mid + slippage (1¢)
  - Stale orders cancelled after order_timeout_candles periods
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

from .data import Candle

logger = logging.getLogger(__name__)

SLIPPAGE = 0.01   # 1¢ market order slippage


@dataclass
class BacktestResult:
    token_id: str
    total_trades: int
    wins: int
    losses: int
    total_pnl: float
    total_fees: float
    net_pnl: float
    win_rate: float
    avg_pnl_per_trade: float
    max_drawdown: float
    sharpe_ratio: float
    candles_processed: int

    def __str__(self) -> str:
        return (
            f"Trades: {self.total_trades} | W/L: {self.wins}/{self.losses} "
            f"| WR: {self.win_rate:.1%} | Net PnL: ${self.net_pnl:.2f} "
            f"| Fees: ${self.total_fees:.2f} | DD: ${self.max_drawdown:.2f}"
        )


@dataclass
class _OpenPos:
    entry_price: float
    size: float
    entry_candle_idx: int
    stop_price: float
    target_price: float
    entry_fee: float
    side: str


@dataclass
class BacktestConfig:
    min_spread_cents: float = 4.0
    target_profit_cents: float = 2.5
    stop_loss_cents: float = 3.5
    entry_offset_cents: float = 0.5
    exit_offset_cents: float = 0.5
    bankroll_pct: float = 0.03
    max_position_usd: float = 100.0
    fee_rate: float = 0.02
    order_timeout_candles: int = 60   # cancel stale limit order after N candles
    initial_bankroll: float = 1_000.0


class BacktestEngine:
    """Single-pass backtester over a list of Candle objects."""

    def __init__(self, cfg: BacktestConfig = None):
        self.cfg = cfg or BacktestConfig()

    def run(self, token_id: str, candles: list[Candle]) -> BacktestResult:
        cfg = self.cfg
        bankroll = cfg.initial_bankroll
        peak_bankroll = bankroll
        max_drawdown = 0.0
        total_pnl = 0.0
        total_fees = 0.0
        wins = 0
        losses = 0
        pnl_history: list[float] = []

        open_pos: Optional[_OpenPos] = None
        pending_entry: Optional[tuple[float, float, int]] = None  # (price, size, placed_at_idx)

        for i, candle in enumerate(candles):
            mid = (candle.high + candle.low) / 2

            # -------------------------------------------------------------------
            # 1. Check pending limit entry fill
            # -------------------------------------------------------------------
            if pending_entry is not None:
                entry_price, size, placed_idx = pending_entry
                # Cancel stale order
                if i - placed_idx >= cfg.order_timeout_candles:
                    pending_entry = None
                    continue
                # Fill: BUY fills when candle low <= bid price
                if candle.low <= entry_price:
                    entry_fee = entry_price * size * cfg.fee_rate
                    stop = entry_price - cfg.stop_loss_cents / 100
                    target = entry_price + cfg.target_profit_cents / 100
                    open_pos = _OpenPos(
                        entry_price=entry_price,
                        size=size,
                        entry_candle_idx=i,
                        stop_price=stop,
                        target_price=target,
                        entry_fee=entry_fee,
                        side="BUY",
                    )
                    pending_entry = None

            # -------------------------------------------------------------------
            # 2. Check open position exit conditions
            # -------------------------------------------------------------------
            if open_pos is not None:
                exit_price: Optional[float] = None
                exit_fee = 0.0

                # Stop loss
                if candle.low <= open_pos.stop_price:
                    exit_price = open_pos.stop_price
                    exit_fee = exit_price * open_pos.size * cfg.fee_rate
                    gross = (exit_price - open_pos.entry_price) * open_pos.size
                    net = gross - open_pos.entry_fee - exit_fee
                    losses += 1

                # Target
                elif candle.high >= open_pos.target_price:
                    offset = cfg.exit_offset_cents / 100
                    exit_price = open_pos.target_price - offset
                    exit_fee = exit_price * open_pos.size * cfg.fee_rate
                    gross = (exit_price - open_pos.entry_price) * open_pos.size
                    net = gross - open_pos.entry_fee - exit_fee
                    wins += 1

                # Time stop (max hold = order_timeout_candles * 2 as proxy)
                elif i - open_pos.entry_candle_idx >= cfg.order_timeout_candles * 2:
                    exit_price = mid + SLIPPAGE
                    exit_fee = exit_price * open_pos.size * cfg.fee_rate
                    gross = (exit_price - open_pos.entry_price) * open_pos.size
                    net = gross - open_pos.entry_fee - exit_fee
                    if net >= 0:
                        wins += 1
                    else:
                        losses += 1

                if exit_price is not None:
                    gross = (exit_price - open_pos.entry_price) * open_pos.size
                    net = gross - open_pos.entry_fee - exit_fee
                    total_pnl += gross
                    total_fees += open_pos.entry_fee + exit_fee
                    bankroll += net
                    pnl_history.append(net)

                    peak_bankroll = max(peak_bankroll, bankroll)
                    dd = peak_bankroll - bankroll
                    max_drawdown = max(max_drawdown, dd)

                    open_pos = None

            # -------------------------------------------------------------------
            # 3. Signal: enter if spread is wide enough and no open position
            # -------------------------------------------------------------------
            if open_pos is None and pending_entry is None:
                # Reconstruct pseudo-spread from candle range as proxy
                pseudo_spread = (candle.high - candle.low) * 100  # in cents
                if pseudo_spread >= cfg.min_spread_cents:
                    position_usd = min(bankroll * cfg.bankroll_pct, cfg.max_position_usd)
                    entry_price = candle.low + cfg.entry_offset_cents / 100
                    if entry_price < candle.high:
                        size = round(position_usd / entry_price, 2) if entry_price > 0 else 0
                        if size > 0:
                            pending_entry = (entry_price, size, i)

        # -------------------------------------------------------------------
        # Summary stats
        # -------------------------------------------------------------------
        total_trades = wins + losses
        net_pnl = total_pnl - total_fees
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        avg_pnl = net_pnl / total_trades if total_trades > 0 else 0.0

        # Sharpe (simplified, using per-trade PnL)
        if len(pnl_history) > 1:
            import statistics
            mu = statistics.mean(pnl_history)
            sigma = statistics.stdev(pnl_history)
            sharpe = mu / sigma if sigma > 0 else 0.0
        else:
            sharpe = 0.0

        return BacktestResult(
            token_id=token_id,
            total_trades=total_trades,
            wins=wins,
            losses=losses,
            total_pnl=total_pnl,
            total_fees=total_fees,
            net_pnl=net_pnl,
            win_rate=win_rate,
            avg_pnl_per_trade=avg_pnl,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            candles_processed=len(candles),
        )
