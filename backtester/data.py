"""
Historical data fetcher for Polymarket backtesting.

Fetches raw trade ticks from the CLOB API and reconstructs OHLCV candles
by bucketing trades into time intervals (Polymarket has no native candle endpoint).
"""

from __future__ import annotations
import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CACHE_DB = Path("backtest_cache.db")
CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class Candle:
    ts: int         # Unix timestamp of candle open
    open: float
    high: float
    low: float
    close: float
    volume: float   # total size traded in interval


@dataclass
class TickTrade:
    trade_id: str
    price: float
    size: float
    side: str
    ts: int         # Unix timestamp (seconds)


class HistoricalDataFetcher:
    """
    Downloads trade tick data from the CLOB API and caches it in SQLite.
    Reconstructs OHLCV candles from ticks.
    """

    def __init__(self, cache_db: Path = CACHE_DB):
        self.cache_db = cache_db
        self._init_cache()

    def _init_cache(self) -> None:
        with sqlite3.connect(self.cache_db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticks (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id  TEXT NOT NULL,
                    trade_id  TEXT UNIQUE,
                    price     REAL,
                    size      REAL,
                    side      TEXT,
                    ts        INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_token_ts ON ticks (token_id, ts)")
            conn.commit()

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def fetch_trades(
        self,
        token_id: str,
        start_ts: int,
        end_ts: Optional[int] = None,
        max_trades: int = 10_000,
    ) -> list[TickTrade]:
        """Fetch paginated trade ticks from CLOB API and cache them."""
        end_ts = end_ts or int(time.time())
        cached = self._load_cached(token_id, start_ts, end_ts)
        if cached:
            logger.info("Loaded %d cached ticks for token %s", len(cached), token_id[:8])
            return cached

        logger.info("Fetching trades for token %s (%d → %d)…", token_id[:8], start_ts, end_ts)
        trades: list[TickTrade] = []
        before = end_ts
        page_size = 500

        async with aiohttp.ClientSession() as session:
            while len(trades) < max_trades:
                url = f"{CLOB_BASE}/trades"
                params = {
                    "market": token_id,
                    "before": before,
                    "after": start_ts,
                    "limit": page_size,
                }
                try:
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                except Exception as exc:
                    logger.error("Failed to fetch trades page: %s", exc)
                    break

                raw_list = data if isinstance(data, list) else data.get("data", [])
                if not raw_list:
                    break

                for raw in raw_list:
                    try:
                        t = TickTrade(
                            trade_id=raw.get("id") or raw.get("tradeId", ""),
                            price=float(raw.get("price", 0)),
                            size=float(raw.get("size", 0)),
                            side=raw.get("side", "BUY").upper(),
                            ts=int(float(raw.get("timestamp", raw.get("ts", 0)))),
                        )
                        trades.append(t)
                    except Exception:
                        continue

                # Paginate backwards
                if len(raw_list) < page_size:
                    break
                before = min(t.ts for t in trades[-page_size:]) - 1
                await asyncio.sleep(0.1)  # ~10 req/s rate limit

        self._cache_trades(token_id, trades)
        logger.info("Fetched and cached %d ticks", len(trades))
        return trades

    def _cache_trades(self, token_id: str, trades: list[TickTrade]) -> None:
        with sqlite3.connect(self.cache_db) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO ticks (token_id,trade_id,price,size,side,ts) VALUES (?,?,?,?,?,?)",
                [(token_id, t.trade_id, t.price, t.size, t.side, t.ts) for t in trades],
            )
            conn.commit()

    def _load_cached(self, token_id: str, start_ts: int, end_ts: int) -> list[TickTrade]:
        with sqlite3.connect(self.cache_db) as conn:
            rows = conn.execute(
                "SELECT trade_id,price,size,side,ts FROM ticks "
                "WHERE token_id=? AND ts>=? AND ts<=? ORDER BY ts ASC",
                (token_id, start_ts, end_ts),
            ).fetchall()
            return [TickTrade(*r) for r in rows]

    # ------------------------------------------------------------------
    # Candle reconstruction
    # ------------------------------------------------------------------

    def build_candles(self, trades: list[TickTrade], interval_secs: int = 60) -> list[Candle]:
        """Bucket ticks into OHLCV candles of `interval_secs` width."""
        if not trades:
            return []

        # Sort by time
        trades = sorted(trades, key=lambda t: t.ts)
        first_ts = trades[0].ts
        candles: dict[int, dict] = {}

        for t in trades:
            bucket = ((t.ts - first_ts) // interval_secs) * interval_secs + first_ts
            if bucket not in candles:
                candles[bucket] = {"open": t.price, "high": t.price, "low": t.price,
                                   "close": t.price, "volume": 0.0}
            c = candles[bucket]
            c["high"] = max(c["high"], t.price)
            c["low"] = min(c["low"], t.price)
            c["close"] = t.price
            c["volume"] += t.size

        result = [
            Candle(ts=ts, open=c["open"], high=c["high"], low=c["low"],
                   close=c["close"], volume=c["volume"])
            for ts, c in sorted(candles.items())
        ]
        return result
