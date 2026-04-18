from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from .config import PolymarketConfig

logger = logging.getLogger(__name__)


class PolymarketHTTP:
    """Base async HTTP client with HMAC-SHA256 auth and exponential backoff retry."""

    def __init__(self, config: PolymarketConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "PolymarketHTTP":
        await self._ensure_session()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # HMAC-SHA256 auth (L2 API key)
    # ------------------------------------------------------------------

    def _build_auth_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """
        Generate Polymarket L2 HMAC-SHA256 auth headers.
        `body` must be canonical JSON (no spaces, sorted keys) or empty string.
        """
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        # api_secret is validated as base64 at config load time (config.py from_env)
        raw_secret = base64.b64decode(self.config.api_secret)
        signature = hmac.new(raw_secret, message.encode(), hashlib.sha256).digest()
        sig_b64 = base64.b64encode(signature).decode()
        return {
            "POLY_ADDRESS": self.config.funder_address or "",
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_API_KEY": self.config.api_key or "",
            "POLY_PASSPHRASE": self.config.api_passphrase or "",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        auth: bool = False,
        path_for_auth: str = "",
    ) -> Any:
        await self._ensure_session()
        headers: dict[str, str] = {}

        if auth:
            # SECURITY: use canonical JSON (no spaces, sorted keys) so the HMAC
            # message is deterministic regardless of dict insertion order.
            body_str = json.dumps(json_body, separators=(",", ":"), sort_keys=True) if json_body else ""
            headers = self._build_auth_headers(method, path_for_auth, body_str)

        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(self.config.max_retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                ) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning("Rate limited — waiting %ss", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as exc:
                last_exc = exc
                if exc.status < 500:
                    raise
                wait = 2 ** attempt
                logger.warning("HTTP %s on attempt %d — retrying in %ss", exc.status, attempt + 1, wait)
                await asyncio.sleep(wait)
            except aiohttp.ClientError as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("Network error on attempt %d — retrying in %ss: %s", attempt + 1, wait, exc)
                await asyncio.sleep(wait)

        raise last_exc

    async def get(self, url: str, params: Optional[dict] = None, auth: bool = False, path: str = "") -> Any:
        return await self._request("GET", url, params=params, auth=auth, path_for_auth=path)

    async def post(self, url: str, body: Optional[dict] = None, auth: bool = True, path: str = "") -> Any:
        return await self._request("POST", url, json_body=body, auth=auth, path_for_auth=path)

    async def delete(self, url: str, body: Optional[dict] = None, auth: bool = True, path: str = "") -> Any:
        return await self._request("DELETE", url, json_body=body, auth=auth, path_for_auth=path)
