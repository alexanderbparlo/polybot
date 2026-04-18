from __future__ import annotations
import logging
from typing import Optional

from .clob import ClobAPI
from .config import PolymarketConfig
from .data import DataAPI
from .gamma import GammaAPI
from .http import PolymarketHTTP
from .models import Market, OrderBook, OrderResponse, Position, Trade
from .websocket import PolymarketWebSocket

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    Main entry point composing all Polymarket sub-clients.

    Usage:
        async with PolymarketClient() as pm:
            markets = await pm.get_markets(limit=10, min_volume=10000)
    """

    def __init__(self, config: Optional[PolymarketConfig] = None):
        self.config = config or PolymarketConfig.from_env()
        self._http = PolymarketHTTP(self.config)
        self.gamma = GammaAPI(self.config, self._http)
        self.clob = ClobAPI(self.config, self._http)
        self.data = DataAPI(self.config, self._http)
        self.ws = PolymarketWebSocket(self.config)

    async def __aenter__(self) -> "PolymarketClient":
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        await self._http.__aexit__(*args)

    async def close(self) -> None:
        await self._http.close()
        await self.ws.stop()

    # ------------------------------------------------------------------
    # Convenience passthrough methods
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        limit: int = 100,
        min_volume: float = 0.0,
        active_only: bool = True,
        exclude_neg_risk: bool = True,
    ) -> list[Market]:
        return await self.gamma.get_markets(
            limit=limit,
            min_volume=min_volume,
            active_only=active_only,
            exclude_neg_risk=exclude_neg_risk,
        )

    async def get_order_book(self, token_id: str) -> OrderBook:
        return await self.clob.get_order_book(token_id)

    async def get_balance(self) -> float:
        return await self.data.get_balance()

    async def get_positions(self) -> list[Position]:
        return await self.data.get_positions()

    async def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
    ) -> OrderResponse:
        if self.config.paper_mode:
            logger.info("[PAPER] Skipping real order: %s %s@%.3f x%.2f", side, token_id[:8], price, size)
            return OrderResponse(order_id="PAPER_" + token_id[:8], status="PAPER")
        return await self.clob.place_limit_order(token_id, price, size, side, order_type)

    async def cancel_order(self, order_id: str) -> bool:
        if self.config.paper_mode:
            logger.info("[PAPER] Skipping cancel: %s", order_id)
            return True
        return await self.clob.cancel_order(order_id)

    async def cancel_all_orders(self) -> int:
        if self.config.paper_mode:
            logger.info("[PAPER] Skipping cancel_all")
            return 0
        return await self.clob.cancel_all_orders()

    async def setup_credentials(self) -> dict:
        """Derive L2 API credentials from the L1 private key. Run once."""
        return await self.clob.create_or_derive_api_key()
