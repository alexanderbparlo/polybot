from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class MarketToken:
    token_id: str      # Used for all CLOB calls
    outcome: str       # "Yes" or "No"
    price: float       # Current price 0.01–0.99


@dataclass
class Market:
    condition_id: str
    question: str
    end_date: Optional[datetime]
    tokens: list[MarketToken]       # [YES token, NO token]
    volume_24h: float
    liquidity: float
    active: bool
    tick_size: float = 0.01
    neg_risk: bool = False          # Special market type — filter out initially

    @property
    def yes_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome.lower() in ("yes", "1"):
                return t
        return None

    @property
    def no_token(self) -> Optional[MarketToken]:
        for t in self.tokens:
            if t.outcome.lower() in ("no", "0"):
                return t
        return None


@dataclass
class OrderBook:
    token_id: str
    timestamp: datetime
    bids: list[PriceLevel] = field(default_factory=list)  # sorted desc (best bid first)
    asks: list[PriceLevel] = field(default_factory=list)  # sorted asc (best ask first)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def spread_cents(self) -> float:
        return self.spread * 100

    @property
    def bid_depth(self) -> float:
        """USDC value at best bid (price * size)."""
        if not self.bids:
            return 0.0
        b = self.bids[0]
        return b.price * b.size

    @property
    def ask_depth(self) -> float:
        """USDC value at best ask (price * size)."""
        if not self.asks:
            return 0.0
        a = self.asks[0]
        return a.price * a.size

    def __repr__(self) -> str:
        return (
            f"OrderBook(token={self.token_id[:8]}… "
            f"bid={self.best_bid} ask={self.best_ask} "
            f"spread={self.spread_cents:.1f}¢)"
        )


@dataclass
class Order:
    order_id: str
    token_id: str
    side: str            # "BUY" or "SELL"
    price: float
    size: float
    size_matched: float
    status: str          # "LIVE", "MATCHED", "CANCELLED", "DELAYED"
    created_at: datetime
    order_type: str = "GTC"


@dataclass
class OrderResponse:
    order_id: str
    status: str
    error_msg: Optional[str] = None


@dataclass
class Position:
    token_id: str
    outcome: str
    size: float
    avg_price: float
    current_price: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class Trade:
    trade_id: str
    token_id: str
    side: str
    price: float
    size: float
    fee: float
    timestamp: datetime


@dataclass
class SimPosition:
    """Tracks a paper-mode simulated position."""
    token_id: str
    market_question: str
    side: str                # "BUY" / "SELL"
    entry_price: float
    size: float
    entry_time: datetime
    entry_fee: float
    stop_price: float
    target_price: float
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None  # "target", "stop", "time", "manual"
    exit_fee: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def net_pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        gross = (self.exit_price - self.entry_price) * self.size
        return gross - self.entry_fee - self.exit_fee

    @property
    def hold_seconds(self) -> float:
        ref = self.exit_time or datetime.utcnow()
        return (ref - self.entry_time).total_seconds()
