"""
Central configuration for the hybrid Polymarket bot.

Wraps the existing ``polymarket.config.PolymarketConfig`` (wallet / L2 auth)
and adds the knobs the three strategies and the shared risk layer need.

Env var precedence
------------------
New-style (spec):   POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE,
                    POLYMARKET_PRIVATE_KEY, LIVE_TRADING
Legacy (scalper):   POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
                    POLY_PRIVATE_KEY, POLY_PAPER_MODE

If a new-style var is set it wins; otherwise we fall back to the legacy name
so the existing SpreadScalper keeps working.

Paper mode
----------
LIVE_TRADING defaults to ``false`` (paper mode). The ONLY value that enables
real orders is the literal string ``"true"`` (case-insensitive). Everything
else — unset, blank, "1", "yes" — stays in paper mode. This matches the
existing double-gate convention.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from polymarket.config import PolymarketConfig

logger = logging.getLogger(__name__)


def _env(*keys: str, default: Optional[str] = None) -> Optional[str]:
    """Return first non-empty env var among keys."""
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a float — using default %s", key, raw, default)
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int — using default %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    """True only if value is the literal 'true' (case-insensitive)."""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def _env_list(key: str, default: Optional[list[str]] = None) -> list[str]:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return list(default or [])
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class HybridConfig:
    """Top-level config consumed by every strategy and the risk layer."""

    # --- Polymarket auth / network (delegated) --------------------------------
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)

    # --- Trading mode ---------------------------------------------------------
    live_trading: bool = False  # paper mode is the default

    # --- Risk caps (global, enforced by PositionManager) ----------------------
    total_capital: float = 1000.0      # assumed bankroll for exposure calculations
    max_exposure_pct: float = 0.30     # 30% of total_capital at risk at once
    max_trade_size: float = 100.0      # single-trade cap (USDC)
    daily_loss_limit: float = -50.0    # halt if today's PnL drops below this
    max_position: float = 50.0         # default position size for strategies
    max_correlated_open: int = 3       # BTC/ETH/SOL count as correlated

    # --- Strategy 1: arbitrage ------------------------------------------------
    arb_min_edge: float = 0.03         # YES + NO must be < 1 - arb_min_edge
    arb_min_liquidity: float = 2000.0  # $ each side
    arb_scan_interval: float = 3.0     # seconds between Gamma scans
    arb_fee_rate: float = 0.02         # winner-side fee on Polymarket
    arb_market_limit: int = 200        # top-N markets to pull per scan

    # --- Strategy 2: latency arb ---------------------------------------------
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"
    latency_symbols: list[str] = field(default_factory=lambda: ["btcusdt", "ethusdt", "solusdt"])
    latency_momentum_pct: float = 0.0015    # 0.15% move in 30s triggers signal
    latency_window_secs: int = 30
    latency_up_max_price: float = 0.60      # skip if YES already above this
    latency_down_min_price: float = 0.40    # skip if YES already below this
    latency_trade_size: float = 50.0        # flat size per trade

    # --- Strategy 3: wallet copy ---------------------------------------------
    alchemy_rpc_url: str = "https://polygon-rpc.com"
    copy_wallets: list[str] = field(default_factory=list)    # tracked wallet addresses
    copy_scorer_min_trades: int = 50
    copy_scorer_lookback_days: int = 30
    copy_size_fraction: float = 0.25     # mirror 25% of target's size
    copy_size_cap: float = 100.0         # paper-mode hard cap
    copy_poll_interval: float = 20.0     # seconds between activity polls
    copy_max_price_slip: float = 0.05    # skip if market drifted > 5% since wallet's trade
    copy_market_min_liq: float = 1000.0  # $ each side
    copy_market_max_exposure_pct: float = 0.10  # 10% of capital per market

    # --- Telegram -------------------------------------------------------------
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # --- Storage --------------------------------------------------------------
    db_path: str = "polymarket_trades.db"

    @classmethod
    def from_env(cls) -> "HybridConfig":
        # Polymarket auth resolution: prefer POLYMARKET_* (spec), fall back to
        # POLY_* (existing scalper). We temporarily overlay env so the existing
        # PolymarketConfig.from_env() picks the chosen values and runs its
        # strict validators (hex key format, base64 secret).
        mapping = {
            "POLY_PRIVATE_KEY":    _env("POLYMARKET_PRIVATE_KEY", "POLY_PRIVATE_KEY"),
            "POLY_API_KEY":        _env("POLYMARKET_API_KEY", "POLY_API_KEY"),
            "POLY_API_SECRET":     _env("POLYMARKET_API_SECRET", "POLY_API_SECRET"),
            "POLY_API_PASSPHRASE": _env("POLYMARKET_API_PASSPHRASE", "POLY_API_PASSPHRASE"),
            "POLY_FUNDER_ADDRESS": _env("POLYMARKET_FUNDER_ADDRESS", "POLY_FUNDER_ADDRESS"),
        }
        saved = {k: os.environ.get(k) for k in mapping}
        try:
            for k, v in mapping.items():
                if v is not None:
                    os.environ[k] = v

            # Trading mode: LIVE_TRADING wins if set, else legacy POLY_PAPER_MODE.
            live = _env_bool("LIVE_TRADING", default=False)
            live_raw = os.getenv("LIVE_TRADING")
            if live_raw is None:
                # Fall back to legacy POLY_PAPER_MODE (only "false" disables paper).
                legacy = (os.getenv("POLY_PAPER_MODE", "true").strip().lower() == "false")
                live = legacy
            os.environ["POLY_PAPER_MODE"] = "false" if live else "true"

            poly = PolymarketConfig.from_env()
        finally:
            # Restore caller's env so we don't leak overlays.
            for k, prev in saved.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev

        return cls(
            polymarket=poly,
            live_trading=not poly.paper_mode,

            total_capital=_env_float("TOTAL_CAPITAL", 1000.0),
            max_exposure_pct=_env_float("MAX_EXPOSURE_PCT", 0.30),
            max_trade_size=_env_float("MAX_TRADE_SIZE", 100.0),
            daily_loss_limit=_env_float("DAILY_LOSS_LIMIT", -50.0),
            max_position=_env_float("MAX_POSITION", 50.0),
            max_correlated_open=_env_int("MAX_CORRELATED_OPEN", 3),

            arb_min_edge=_env_float("ARB_MIN_EDGE", 0.03),
            arb_min_liquidity=_env_float("ARB_MIN_LIQUIDITY", 2000.0),
            arb_scan_interval=_env_float("ARB_SCAN_INTERVAL", 3.0),
            arb_fee_rate=_env_float("ARB_FEE_RATE", 0.02),
            arb_market_limit=_env_int("ARB_MARKET_LIMIT", 200),

            binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
            latency_symbols=_env_list("LATENCY_SYMBOLS", ["btcusdt", "ethusdt", "solusdt"]),
            latency_momentum_pct=_env_float("LATENCY_MOMENTUM_PCT", 0.0015),
            latency_window_secs=_env_int("LATENCY_WINDOW_SECS", 30),
            latency_up_max_price=_env_float("LATENCY_UP_MAX_PRICE", 0.60),
            latency_down_min_price=_env_float("LATENCY_DOWN_MIN_PRICE", 0.40),
            latency_trade_size=_env_float("LATENCY_TRADE_SIZE", 50.0),

            alchemy_rpc_url=os.getenv("ALCHEMY_RPC_URL", "https://polygon-rpc.com"),
            copy_wallets=[w.lower() for w in _env_list("COPY_WALLETS", [])],
            copy_scorer_min_trades=_env_int("COPY_MIN_TRADES", 50),
            copy_scorer_lookback_days=_env_int("COPY_LOOKBACK_DAYS", 30),
            copy_size_fraction=_env_float("COPY_SIZE_FRACTION", 0.25),
            copy_size_cap=_env_float("COPY_SIZE_CAP", 100.0),
            copy_poll_interval=_env_float("COPY_POLL_INTERVAL", 20.0),
            copy_max_price_slip=_env_float("COPY_MAX_PRICE_SLIP", 0.05),
            copy_market_min_liq=_env_float("COPY_MARKET_MIN_LIQ", 1000.0),
            copy_market_max_exposure_pct=_env_float("COPY_MARKET_MAX_EXPOSURE_PCT", 0.10),

            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            db_path=os.getenv("DB_PATH", "polymarket_trades.db"),
        )

    @property
    def paper_mode(self) -> bool:
        return not self.live_trading

    def sanity_check(self) -> list[str]:
        """Return a list of human-readable warnings; empty list = all good."""
        warnings: list[str] = []
        if self.max_exposure_pct <= 0 or self.max_exposure_pct > 1:
            warnings.append(f"MAX_EXPOSURE_PCT={self.max_exposure_pct} outside (0, 1]")
        if self.max_trade_size <= 0:
            warnings.append("MAX_TRADE_SIZE must be positive")
        if self.daily_loss_limit >= 0:
            warnings.append("DAILY_LOSS_LIMIT should be negative (e.g. -50)")
        if self.live_trading and not self.polymarket.is_authenticated:
            warnings.append("LIVE_TRADING=true but Polymarket L2 credentials are missing")
        if self.arb_min_edge <= 0:
            warnings.append("ARB_MIN_EDGE must be > 0")
        return warnings
