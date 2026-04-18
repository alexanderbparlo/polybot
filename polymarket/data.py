from __future__ import annotations
import logging
from typing import Any, Optional

from .config import PolymarketConfig
from .http import PolymarketHTTP
from .models import Position, Trade

logger = logging.getLogger(__name__)


class DataAPI:
    """Data API — positions, trades, portfolio balance (address-based, public)."""

    def __init__(self, config: PolymarketConfig, http: PolymarketHTTP):
        self.config = config
        self.http = http

    async def get_positions(self, address: Optional[str] = None) -> list[Position]:
        addr = address or self.config.funder_address
        if not addr:
            logger.warning("No address provided for get_positions")
            return []

        url = f"{self.config.data_host}/positions"
        try:
            data = await self.http.get(url, params={"user": addr})
            raw_list = data if isinstance(data, list) else data.get("data", [])
            return [self._parse_position(r) for r in raw_list]
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            return []

    async def get_balance(self, address: Optional[str] = None) -> float:
        """Returns USDC balance in dollars."""
        addr = address or self.config.funder_address
        if not addr:
            return 0.0

        url = f"{self.config.data_host}/balance"
        try:
            data = await self.http.get(url, params={"user": addr})
            return float(data.get("balance", 0))
        except Exception as exc:
            logger.error("Failed to fetch balance: %s", exc)
            return 0.0

    async def get_activity(
        self,
        address: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        addr = address or self.config.funder_address
        if not addr:
            return []

        url = f"{self.config.data_host}/activity"
        try:
            data = await self.http.get(url, params={"user": addr, "limit": limit})
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            logger.error("Failed to fetch activity: %s", exc)
            return []

    def _parse_position(self, raw: dict) -> Position:
        size = float(raw.get("size", 0))
        avg_price = float(raw.get("avgPrice") or raw.get("averagePrice", 0))
        current_price = float(raw.get("currentPrice", avg_price))
        unrealized = (current_price - avg_price) * size
        realized = float(raw.get("realizedPnl", 0))

        return Position(
            token_id=raw.get("asset") or raw.get("tokenId", ""),
            outcome=raw.get("outcome", ""),
            size=size,
            avg_price=avg_price,
            current_price=current_price,
            unrealized_pnl=unrealized,
            realized_pnl=realized,
        )
