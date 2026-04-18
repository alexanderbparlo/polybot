from __future__ import annotations
import asyncio
import logging
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

DB_PATH = Path("polymarket_trades.db")


class TradeLogger:
    """SQLite trade logger. Persists orders, trades, events, and daily summaries."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS orders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id    TEXT,
                    token_id    TEXT,
                    market_q    TEXT,
                    side        TEXT,
                    price       REAL,
                    size        REAL,
                    status      TEXT,
                    paper       INTEGER DEFAULT 0,
                    created_at  TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id    TEXT,
                    market_q    TEXT,
                    side        TEXT,
                    entry_price REAL,
                    exit_price  REAL,
                    size        REAL,
                    net_pnl     REAL,
                    entry_fee   REAL,
                    exit_fee    REAL,
                    hold_secs   REAL,
                    exit_reason TEXT,
                    paper       INTEGER DEFAULT 0,
                    closed_at   TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    level       TEXT,
                    message     TEXT,
                    detail      TEXT,
                    ts          TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_summary (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date  TEXT UNIQUE,
                    trades      INTEGER,
                    wins        INTEGER,
                    losses      INTEGER,
                    total_pnl   REAL,
                    max_drawdown REAL,
                    paper       INTEGER DEFAULT 0,
                    created_at  TEXT
                );
            """)
            conn.commit()

    def log_order(
        self,
        order_id: str,
        token_id: str,
        market_q: str,
        side: str,
        price: float,
        size: float,
        status: str,
        paper: bool = True,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO orders (order_id,token_id,market_q,side,price,size,status,paper,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (order_id, token_id, market_q, side, price, size, status, int(paper),
                 datetime.utcnow().isoformat()),
            )
            conn.commit()

    def log_trade(
        self,
        token_id: str,
        market_q: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        net_pnl: float,
        entry_fee: float,
        exit_fee: float,
        hold_secs: float,
        exit_reason: str,
        paper: bool = True,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO trades "
                "(token_id,market_q,side,entry_price,exit_price,size,net_pnl,"
                "entry_fee,exit_fee,hold_secs,exit_reason,paper,closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (token_id, market_q, side, entry_price, exit_price, size, net_pnl,
                 entry_fee, exit_fee, hold_secs, exit_reason, int(paper),
                 datetime.utcnow().isoformat()),
            )
            conn.commit()

    def log_event(self, level: str, message: str, detail: str = "") -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO events (level,message,detail,ts) VALUES (?,?,?,?)",
                (level, message, detail, datetime.utcnow().isoformat()),
            )
            conn.commit()

    def log_daily_summary(self, summary: dict, paper: bool = True) -> None:
        today = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO daily_summary "
                "(trade_date,trades,wins,losses,total_pnl,max_drawdown,paper,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (today, summary.get("trades", 0), summary.get("wins", 0),
                 summary.get("losses", 0), summary.get("total_pnl", 0.0),
                 summary.get("max_drawdown", 0.0), int(paper),
                 datetime.utcnow().isoformat()),
            )
            conn.commit()

    def get_daily_pnl(self, trade_date: Optional[str] = None) -> float:
        today = trade_date or date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(net_pnl) FROM trades WHERE date(closed_at)=?", (today,)
            ).fetchone()
            return row[0] or 0.0

    def get_recent_trades(self, n: int = 50) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]


class TelegramAlert:
    """Non-blocking Telegram alerts. Never raises — all sends are fire-and-forget."""

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    async def _send(self, text: str) -> None:
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        logger.warning("Telegram send failed: %s", await resp.text())
        except Exception as exc:
            logger.warning("Telegram error (non-fatal): %s", exc)

    def _fire(self, text: str) -> None:
        """Schedule send without blocking the caller."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._send(text))
            else:
                asyncio.run(self._send(text))
        except Exception as exc:
            logger.debug("Telegram fire error: %s", exc)

    def bot_started(self, bankroll: float, markets_watching: int) -> None:
        self._fire(
            f"🟢 <b>Polymarket Bot Started</b>\n"
            f"Bankroll: ${bankroll:.2f}\n"
            f"Watching: {markets_watching} markets"
        )

    def order_placed(
        self, market: str, side: str, price: float, size: float, order_id: str, paper: bool = True
    ) -> None:
        prefix = "[PAPER] " if paper else ""
        self._fire(
            f"📋 {prefix}<b>Order Placed</b>\n"
            f"Market: {market[:60]}\n"
            f"Side: {side} @ {price:.3f} x {size:.2f}\n"
            f"ID: {order_id}"
        )

    def trade_filled(
        self,
        market: str,
        side: str,
        entry: float,
        exit_price: float,
        size: float,
        net_pnl: float,
        hold_secs: float,
        paper: bool = True,
    ) -> None:
        prefix = "[PAPER] " if paper else ""
        emoji = "✅" if net_pnl >= 0 else "🔴"
        self._fire(
            f"{emoji} {prefix}<b>Trade Closed</b>\n"
            f"Market: {market[:60]}\n"
            f"Side: {side} | Entry: {entry:.3f} → Exit: {exit_price:.3f}\n"
            f"Size: {size:.2f} | Net PnL: ${net_pnl:.3f}\n"
            f"Hold: {hold_secs:.0f}s"
        )

    def stop_loss_triggered(
        self, market: str, entry: float, stop_price: float, loss: float, paper: bool = True
    ) -> None:
        prefix = "[PAPER] " if paper else ""
        self._fire(
            f"🛑 {prefix}<b>Stop Loss Hit</b>\n"
            f"Market: {market[:60]}\n"
            f"Entry: {entry:.3f} → Stop: {stop_price:.3f}\n"
            f"Loss: ${loss:.3f}"
        )

    def circuit_breaker(self, reason: str, daily_pnl: float, resume_at: str) -> None:
        self._fire(
            f"⚡ <b>Circuit Breaker Triggered</b>\n"
            f"Reason: {reason}\n"
            f"Daily PnL: ${daily_pnl:.2f}\n"
            f"Resumes at: {resume_at}"
        )

    def daily_summary(self, summary: dict) -> None:
        self._fire(
            f"📊 <b>Daily Summary</b>\n"
            f"Trades: {summary.get('trades', 0)} "
            f"(W: {summary.get('wins', 0)} / L: {summary.get('losses', 0)})\n"
            f"PnL: ${summary.get('total_pnl', 0):.2f}\n"
            f"Max Drawdown: ${summary.get('max_drawdown', 0):.2f}"
        )

    def error(self, message: str, detail: str = "") -> None:
        self._fire(f"❌ <b>Error</b>\n{message}\n<code>{detail[:200]}</code>")

    def test(self) -> None:
        self._fire("🔔 <b>Polymarket Bot — Telegram test OK</b>")
