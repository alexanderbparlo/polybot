from __future__ import annotations
import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from .config import PolymarketConfig
from .http import PolymarketHTTP
from .models import Order, OrderBook, OrderResponse, PriceLevel, Trade

logger = logging.getLogger(__name__)


class ClobAPI:
    """
    CLOB API client — order book reads and authenticated order management.

    All order signing uses py-clob-client (EIP-712). Synchronous py-clob-client
    calls are run in an executor to avoid blocking the event loop.
    """

    def __init__(self, config: PolymarketConfig, http: PolymarketHTTP):
        self.config = config
        self.http = http
        self._clob_client: Optional[Any] = None  # py_clob_client.ClobClient

    def _get_clob_client(self) -> Any:
        """Lazily initialise the synchronous py-clob-client."""
        if self._clob_client is None:
            from py_clob_client.client import ClobClient  # type: ignore
            self._clob_client = ClobClient(
                host=self.config.clob_host,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                funder=self.config.funder_address,
                signature_type=self.config.signature_type,
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            )
        return self._clob_client

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    async def create_or_derive_api_key(self) -> dict:
        """
        Derive L2 API credentials from the L1 private key. Run once and
        store the result in .env.
        Returns: {"apiKey": str, "secret": str, "passphrase": str}
        """
        client = self._get_clob_client()

        def _derive():
            try:
                creds = client.derive_api_key()
            except Exception:
                creds = client.create_api_key()
            return {
                "apiKey": creds.api_key,
                "secret": creds.api_secret,
                "passphrase": creds.api_passphrase,
            }

        return await self._run_sync(_derive)

    # ------------------------------------------------------------------
    # Public endpoints — no auth required
    # ------------------------------------------------------------------

    async def get_order_book(self, token_id: str) -> OrderBook:
        url = f"{self.config.clob_host}/book"
        data = await self.http.get(url, params={"token_id": token_id})
        return self._parse_order_book(token_id, data)

    async def get_midpoint(self, token_id: str) -> float:
        url = f"{self.config.clob_host}/midpoint"
        data = await self.http.get(url, params={"token_id": token_id})
        return float(data.get("mid", 0.5))

    async def get_price(self, token_id: str, side: str) -> float:
        url = f"{self.config.clob_host}/price"
        data = await self.http.get(url, params={"token_id": token_id, "side": side})
        return float(data.get("price", 0.5))

    async def get_trades(
        self,
        market: Optional[str] = None,
        before: Optional[int] = None,
        after: Optional[int] = None,
        limit: int = 500,
    ) -> list[Trade]:
        url = f"{self.config.clob_host}/trades"
        params: dict[str, Any] = {"limit": limit}
        if market:
            params["market"] = market
        if before:
            params["before"] = before
        if after:
            params["after"] = after

        data = await self.http.get(url, params=params)
        raw_list = data if isinstance(data, list) else data.get("data", [])
        trades: list[Trade] = []
        for raw in raw_list:
            try:
                trades.append(self._parse_trade(raw))
            except Exception as exc:
                logger.debug("Failed to parse trade: %s", exc)
        return trades

    # ------------------------------------------------------------------
    # Authenticated endpoints — L2 API key required
    # ------------------------------------------------------------------

    async def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
    ) -> OrderResponse:
        """
        Place a limit order. Uses py-clob-client for EIP-712 signing.
        side: "BUY" or "SELL"
        order_type: "GTC", "GTD", "FOK"
        """
        from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
        from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

        clob_side = BUY if side.upper() == "BUY" else SELL
        ot_map = {"GTC": OrderType.GTC, "GTD": OrderType.GTD, "FOK": OrderType.FOK}
        clob_order_type = ot_map.get(order_type.upper(), OrderType.GTC)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )

        client = self._get_clob_client()

        def _place():
            signed = client.create_order(order_args)
            resp = client.post_order(signed, clob_order_type)
            return resp

        try:
            result = await self._run_sync(_place)
            order_id = result.get("orderID") or result.get("order_id") or result.get("id", "unknown")
            status = result.get("status", "LIVE")
            return OrderResponse(order_id=order_id, status=status)
        except Exception as exc:
            logger.error("Failed to place order: %s", exc)
            return OrderResponse(order_id="", status="ERROR", error_msg=str(exc))

    async def cancel_order(self, order_id: str) -> bool:
        client = self._get_clob_client()

        def _cancel():
            return client.cancel(order_id)

        try:
            await self._run_sync(_cancel)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    async def cancel_all_orders(self) -> int:
        client = self._get_clob_client()

        def _cancel_all():
            return client.cancel_all()

        try:
            result = await self._run_sync(_cancel_all)
            count = len(result) if isinstance(result, list) else 0
            logger.info("Cancelled %d open orders", count)
            return count
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return 0

    async def get_open_orders(self) -> list[Order]:
        client = self._get_clob_client()

        def _get():
            return client.get_orders()

        try:
            raw_list = await self._run_sync(_get)
            if not isinstance(raw_list, list):
                raw_list = raw_list.get("data", [])
            return [self._parse_order(r) for r in raw_list]
        except Exception as exc:
            logger.error("Failed to fetch open orders: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_order_book(self, token_id: str, data: dict) -> OrderBook:
        def parse_levels(raw_levels: list) -> list[PriceLevel]:
            levels: list[PriceLevel] = []
            for lvl in raw_levels:
                if isinstance(lvl, dict):
                    p = float(lvl.get("price", 0))
                    s = float(lvl.get("size", 0))
                else:
                    p, s = float(lvl[0]), float(lvl[1])
                levels.append(PriceLevel(price=p, size=s))
            return levels

        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])
        bids = sorted(parse_levels(raw_bids), key=lambda x: x.price, reverse=True)
        asks = sorted(parse_levels(raw_asks), key=lambda x: x.price)

        return OrderBook(
            token_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )

    def _parse_order(self, raw: dict) -> Order:
        return Order(
            order_id=raw.get("id") or raw.get("orderID", ""),
            token_id=raw.get("asset_id") or raw.get("tokenId", ""),
            side=raw.get("side", "BUY").upper(),
            price=float(raw.get("price", 0)),
            size=float(raw.get("original_size") or raw.get("size", 0)),
            size_matched=float(raw.get("size_matched", 0)),
            status=raw.get("status", "LIVE").upper(),
            created_at=datetime.utcnow(),
            order_type=raw.get("type", "GTC"),
        )

    def _parse_trade(self, raw: dict) -> Trade:
        return Trade(
            trade_id=raw.get("id") or raw.get("tradeId", ""),
            token_id=raw.get("asset_id") or raw.get("tokenId", ""),
            side=raw.get("side", "BUY").upper(),
            price=float(raw.get("price", 0)),
            size=float(raw.get("size", 0)),
            fee=float(raw.get("fee", 0)),
            timestamp=datetime.utcnow(),
        )
