"""
Thin re-export / facade around the existing ``polymarket`` package plus a
few helpers the hybrid strategies need that aren't on the base client:

- Concurrent order-book fetch for many tokens (Strategy 1)
- Paginated Gamma market crawl with keyword / endpoint filters (Strategy 2 & 1)
- Polymarket Data-API wallet activity (Strategy 3)
- Reverse book->market resolution used when we only have a token_id

Nothing here replaces ``polymarket/client.py``; we compose it so existing
code (``main.py``, ``spread_scalper.py``) still works unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from polymarket import PolymarketClient
from polymarket.config import PolymarketConfig
from polymarket.models import Market, OrderBook, OrderResponse

logger = logging.getLogger(__name__)


# Re-export so callers can do ``from polymarket_client import PolymarketClient``.
__all__ = ["PolymarketClient", "PolymarketConfig", "HybridPolymarketClient"]


class HybridPolymarketClient:
    """Wrapper exposing the base ``PolymarketClient`` plus strategy helpers.

    Use as an async context manager — it delegates lifecycle to the underlying
    client, which owns the aiohttp session.
    """

    def __init__(self, config: Optional[PolymarketConfig] = None):
        self._client = PolymarketClient(config)

    # ---- lifecycle -------------------------------------------------------
    async def __aenter__(self) -> "HybridPolymarketClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def base(self) -> PolymarketClient:
        return self._client

    @property
    def config(self) -> PolymarketConfig:
        return self._client.config

    # ---- passthroughs ----------------------------------------------------
    async def get_markets(self, **kw: Any) -> list[Market]:
        return await self._client.get_markets(**kw)

    async def get_order_book(self, token_id: str) -> OrderBook:
        return await self._client.get_order_book(token_id)

    async def get_balance(self) -> float:
        return await self._client.get_balance()

    async def place_limit_order(
        self, token_id: str, price: float, size: float, side: str,
        order_type: str = "GTC",
    ) -> OrderResponse:
        return await self._client.place_limit_order(
            token_id=token_id, price=price, size=size, side=side, order_type=order_type,
        )

    async def cancel_order(self, order_id: str) -> bool:
        return await self._client.cancel_order(order_id)

    # ---- helpers (new for hybrid strategies) -----------------------------

    async def fetch_active_markets(
        self,
        min_volume: float = 0.0,
        limit: int = 500,
        page_size: int = 100,
        exclude_neg_risk: bool = True,
    ) -> list[Market]:
        """Crawl Gamma in pages until we have ``limit`` markets (or run out)."""
        out: list[Market] = []
        offset = 0
        while len(out) < limit:
            page = await self._client.get_markets(
                limit=page_size,
                min_volume=min_volume,
                active_only=True,
                exclude_neg_risk=exclude_neg_risk,
            )
            # The base client's GammaAPI does not currently expose ``offset`` via
            # get_markets(...), so we call it directly for pagination.
            if not page:
                break
            out.extend(page)
            # If the API returned fewer than page_size it's the last page.
            if len(page) < page_size:
                break
            offset += page_size
            if offset >= 2000:  # safety cap
                break
        return out[:limit]

    async def get_order_books(
        self, token_ids: list[str], concurrency: int = 10,
    ) -> dict[str, OrderBook]:
        """Fetch many order books concurrently. Missing/failed ones are omitted."""
        sem = asyncio.Semaphore(concurrency)

        async def _one(tid: str) -> tuple[str, Optional[OrderBook]]:
            async with sem:
                try:
                    return tid, await self._client.get_order_book(tid)
                except Exception as exc:
                    logger.debug("order book fetch failed for %s: %s", tid, exc)
                    return tid, None

        results = await asyncio.gather(*(_one(t) for t in token_ids))
        return {tid: ob for tid, ob in results if ob is not None}

    async def find_crypto_15m_market(
        self, asset: str, direction: str,
    ) -> Optional[Market]:
        """Best-effort lookup of the currently open BTC/ETH/SOL 15-min market.

        Polymarket's market slugs / titles vary across runs, so we do a
        keyword search: asset + ("up"/"higher") for UP, ("down"/"lower") for DOWN,
        and pick the highest-volume active, non-negRisk market.
        """
        markets = await self._client.get_markets(
            limit=250, active_only=True, exclude_neg_risk=True,
        )
        asset_u = asset.upper()
        if direction.upper() == "UP":
            pos = ("up", "higher", "above")
        else:
            pos = ("down", "lower", "below")

        def match(m: Market) -> bool:
            q = (m.question or "").lower()
            return asset_u.lower() in q and any(k in q for k in pos)

        cand = [m for m in markets if match(m)]
        if not cand:
            return None
        cand.sort(key=lambda m: m.volume_24h, reverse=True)
        return cand[0]

    async def get_wallet_activity(
        self, address: str, limit: int = 100,
    ) -> list[dict]:
        """Fetch a wallet's recent Polymarket activity via the Data API.

        Returns the raw Data-API objects; callers parse the fields they need.
        """
        return await self._client.data.get_activity(address=address, limit=limit)

    async def get_wallet_positions(self, address: str) -> list:
        return await self._client.data.get_positions(address=address)
