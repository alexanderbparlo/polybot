from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

import aiohttp

from .config import PolymarketConfig
from .models import OrderBook, PriceLevel

logger = logging.getLogger(__name__)

# Callback type: receives (channel, message_type, data)
MessageCallback = Callable[[str, str, dict], None]


class PolymarketWebSocket:
    """
    WebSocket streaming client.

    Channels:
    - "Market" (public): real-time order book updates for token_id list
    - "User" (authenticated): order fills and status changes

    Usage:
        ws = PolymarketWebSocket(config)
        await ws.connect()
        await ws.subscribe_market(["token_id_1", "token_id_2"], on_message)
        await ws.listen()
    """

    def __init__(self, config: PolymarketConfig):
        self.config = config
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._callbacks: list[MessageCallback] = []
        self._subscriptions: list[dict] = []
        self._running = False

    def add_callback(self, cb: MessageCallback) -> None:
        self._callbacks.append(cb)

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.config.ws_host)
        logger.info("WebSocket connected to %s", self.config.ws_host)

    async def subscribe_market(self, token_ids: list[str]) -> None:
        """Subscribe to public market channel for given token IDs."""
        msg = {
            "auth": {},
            "type": "Market",
            "assets_ids": token_ids,
        }
        self._subscriptions.append(msg)
        if self._ws:
            await self._ws.send_str(json.dumps(msg))
            logger.info("Subscribed to Market channel for %d tokens", len(token_ids))

    def _build_user_auth_msg(self) -> dict:
        """
        Build a fresh User-channel subscription message with a current timestamp.
        Called both on initial subscribe and on reconnect so the timestamp is never stale.

        SECURITY: the raw api_secret is NOT included in the payload — only the
        derived HMAC signature is transmitted. The secret stays in process memory only.
        """
        timestamp = str(int(time.time()))
        message = timestamp + "GET" + "/ws-auth"
        raw_secret = base64.b64decode(self.config.api_secret)
        signature = hmac.new(raw_secret, message.encode(), hashlib.sha256).digest()
        sig_b64 = base64.b64encode(signature).decode()
        return {
            "auth": {
                "apiKey": self.config.api_key,
                # "secret" intentionally omitted — server only needs the signature
                "passphrase": self.config.api_passphrase,
                "timestamp": timestamp,
                "signature": sig_b64,
            },
            "type": "User",
            "markets": [],
        }

    async def subscribe_user(self) -> None:
        """Subscribe to authenticated user channel for order/trade updates."""
        if not self.config.is_authenticated:
            logger.warning("Cannot subscribe to User channel — not authenticated")
            return

        msg = self._build_user_auth_msg()
        # Store a sentinel (not the actual creds) so _reconnect knows to re-subscribe.
        # The sentinel triggers a fresh auth message (new timestamp) on each reconnect.
        if "__user_channel__" not in [s.get("_sentinel") for s in self._subscriptions]:
            self._subscriptions.append({"_sentinel": "__user_channel__"})
        if self._ws:
            await self._ws.send_str(json.dumps(msg))
            logger.info("Subscribed to User channel")

    async def listen(self) -> None:
        """Receive and dispatch messages until stopped."""
        self._running = True
        if not self._ws:
            raise RuntimeError("Not connected — call connect() first")

        while self._running:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keepalive ping
                if self._ws and not self._ws.closed:
                    await self._ws.ping()
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._dispatch(data)
                except json.JSONDecodeError as exc:
                    logger.debug("WS JSON decode error: %s", exc)

            elif msg.type == aiohttp.WSMsgType.CLOSED:
                logger.warning("WebSocket closed — will reconnect")
                await self._reconnect()

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("WebSocket error: %s", msg.data)
                await self._reconnect()

    async def _dispatch(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        channel = data.get("channel", "")
        msg_type = data.get("type", "")
        for cb in self._callbacks:
            try:
                cb(channel, msg_type, data)
            except Exception as exc:
                logger.error("Callback error: %s", exc)

    async def _reconnect(self) -> None:
        logger.info("Reconnecting WebSocket in 3s…")
        await asyncio.sleep(3)
        try:
            await self.connect()
            for sub in self._subscriptions:
                if self._ws:
                    if sub.get("_sentinel") == "__user_channel__":
                        # Rebuild with a fresh timestamp — never replay stale auth
                        await self._ws.send_str(json.dumps(self._build_user_auth_msg()))
                    else:
                        await self._ws.send_str(json.dumps(sub))
            logger.info("WebSocket reconnected and re-subscribed")
        except Exception as exc:
            logger.error("Reconnect failed: %s", exc)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("WebSocket stopped")

    def parse_book_snapshot(self, data: dict) -> Optional[OrderBook]:
        """Parse a 'book' type WS message into an OrderBook."""
        token_id = data.get("asset_id") or data.get("market", "")
        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])

        def parse_levels(raw):
            levels = []
            for lvl in raw:
                if isinstance(lvl, dict):
                    levels.append(PriceLevel(float(lvl["price"]), float(lvl["size"])))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    levels.append(PriceLevel(float(lvl[0]), float(lvl[1])))
            return levels

        bids = sorted(parse_levels(raw_bids), key=lambda x: x.price, reverse=True)
        asks = sorted(parse_levels(raw_asks), key=lambda x: x.price)
        return OrderBook(token_id=token_id, timestamp=datetime.utcnow(), bids=bids, asks=asks)
