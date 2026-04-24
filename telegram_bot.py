"""
Hybrid-strategy Telegram alerts.

Extends ``logger.TelegramAlert`` with one template per strategy so we don't
duplicate the fire-and-forget send machinery. Import ``HybridTelegram``
everywhere the three strategies need alerts; it still provides all the
base ``TelegramAlert`` methods (``trade_filled``, ``circuit_breaker``,
``daily_summary``, etc.) via inheritance.
"""
from __future__ import annotations

import logging
from typing import Optional

from logger import TelegramAlert

logger = logging.getLogger(__name__)


def _shorten_addr(addr: str) -> str:
    if not addr:
        return "?"
    if len(addr) <= 10:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def _tag(paper: bool) -> str:
    return "[PAPER] " if paper else ""


class HybridTelegram(TelegramAlert):
    """Adds strategy-specific alert templates on top of the base alerts."""

    # ---- Strategy 1: arbitrage ------------------------------------------
    def arb_opportunity(
        self,
        market: str,
        yes_price: float,
        no_price: float,
        spread_pct: float,
        capital: float,
        expected_profit: float,
        paper: bool = True,
    ) -> None:
        self._fire(
            f"💰 {_tag(paper)}<b>Arb Executed</b>\n"
            f"Market: {market[:80]}\n"
            f"YES {yes_price:.3f} + NO {no_price:.3f} "
            f"= {(yes_price + no_price):.3f}\n"
            f"Spread: {spread_pct * 100:.2f}% | Capital: ${capital:.2f}\n"
            f"Expected net: ${expected_profit:+.3f}"
        )

    def arb_skipped(self, market: str, reason: str) -> None:
        self._fire(
            f"⚠️ <b>Arb Skipped</b>\n"
            f"Market: {market[:80]}\n"
            f"Reason: {reason}"
        )

    # ---- Strategy 2: latency --------------------------------------------
    def latency_signal(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        poly_yes_price: float,
        size: float,
        expected_edge: float,
        paper: bool = True,
    ) -> None:
        arrow = "🟢↑" if direction.upper() == "UP" else "🔴↓"
        self._fire(
            f"⚡ {_tag(paper)}<b>Latency Signal</b> {arrow}\n"
            f"{asset} momentum {momentum_pct * 100:+.2f}%\n"
            f"Polymarket YES: {poly_yes_price:.3f}\n"
            f"Size: ${size:.2f} | Edge: {expected_edge * 100:+.2f}%"
        )

    def latency_settled(
        self, asset: str, direction: str, pnl: float, outcome: str,
        paper: bool = True,
    ) -> None:
        emoji = "✅" if pnl >= 0 else "🔴"
        self._fire(
            f"{emoji} {_tag(paper)}<b>Latency Settled</b>\n"
            f"{asset} {direction}: {outcome}\n"
            f"PnL: ${pnl:+.2f}"
        )

    # ---- Strategy 3: wallet copy ----------------------------------------
    def copy_trade(
        self,
        wallet: str,
        market: str,
        their_size: float,
        our_size: float,
        price: float,
        paper: bool = True,
    ) -> None:
        self._fire(
            f"🔁 {_tag(paper)}<b>Copy Trade</b>\n"
            f"Wallet: <code>{_shorten_addr(wallet)}</code>\n"
            f"Market: {market[:80]}\n"
            f"Their size: ${their_size:.0f} → Our size: ${our_size:.2f}\n"
            f"Entry: {price:.3f}"
        )

    def copy_skipped(self, wallet: str, market: str, reason: str) -> None:
        self._fire(
            f"➖ <b>Copy Skipped</b>\n"
            f"Wallet: <code>{_shorten_addr(wallet)}</code>\n"
            f"Market: {market[:80]}\n"
            f"Reason: {reason}"
        )

    def wallet_ranked(self, top: list[dict]) -> None:
        if not top:
            return
        lines = ["🏆 <b>Top Wallets (refreshed)</b>"]
        for i, w in enumerate(top[:5], start=1):
            lines.append(
                f"{i}. <code>{_shorten_addr(w.get('address', ''))}</code> "
                f"score={w.get('score', 0):.2f} "
                f"ROI={w.get('roi_30d', 0) * 100:.1f}% "
                f"WR={w.get('win_rate', 0) * 100:.0f}% "
                f"n={w.get('n_trades', 0)}"
            )
        self._fire("\n".join(lines))

    # ---- Global risk ----------------------------------------------------
    def risk_halt(
        self, reason: str, daily_pnl: float, strategy: Optional[str] = None,
    ) -> None:
        s = f" [{strategy}]" if strategy else ""
        self._fire(
            f"🛑 <b>Trading Halted</b>{s}\n"
            f"Reason: {reason}\n"
            f"Daily PnL: ${daily_pnl:+.2f}"
        )

    def hybrid_started(self, strategies: list[str], paper: bool) -> None:
        mode = "PAPER" if paper else "LIVE"
        self._fire(
            f"🚀 <b>Hybrid Bot Started</b> ({mode})\n"
            f"Active: {', '.join(strategies)}"
        )
