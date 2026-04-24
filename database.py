"""
Strategy-layer SQLite helpers.

Reuses the same ``polymarket_trades.db`` created by ``logger.TradeLogger``
and adds four extra tables the hybrid strategies need:

- ``arb_trades``        — Strategy 1 executed arbitrages (both legs + net PnL)
- ``latency_signals``   — Strategy 2 signals + outcomes keyed by contract
- ``wallet_scores``     — Strategy 3 wallet rankings (upserted per refresh)
- ``copy_decisions``    — Strategy 3 copy evaluations (executed OR skipped + reason)
- ``strategy_pnl``      — per-strategy rolling PnL aggregation

We DO NOT touch the existing ``orders``, ``trades``, ``events``, or
``daily_summary`` tables — ``TradeLogger`` owns those and the scalper still
writes to them. Both writers share the DB file safely because sqlite3
serializes via the per-connection lock.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB = Path("polymarket_trades.db")


class HybridDatabase:
    """SQLite wrapper for the three hybrid strategies."""

    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self._init_schema()

    # ---- connection helper ------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- schema -----------------------------------------------------------
    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS arb_trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    condition_id    TEXT,
                    market_q        TEXT,
                    yes_price       REAL,
                    no_price        REAL,
                    combined_price  REAL,
                    spread          REAL,        -- 1 - combined
                    capital         REAL,        -- total USDC deployed
                    shares          REAL,        -- size of each leg
                    gross_profit    REAL,
                    fee_cost        REAL,
                    net_profit      REAL,
                    paper           INTEGER DEFAULT 1,
                    executed_at     TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_arb_exec ON arb_trades(executed_at);

                CREATE TABLE IF NOT EXISTS latency_signals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset           TEXT,        -- BTC/ETH/SOL
                    direction       TEXT,        -- UP / DOWN
                    momentum_pct    REAL,
                    spot_price      REAL,
                    poly_yes_price  REAL,
                    poly_token_id   TEXT,
                    market_q        TEXT,
                    entry_size      REAL,
                    acted           INTEGER,     -- 1 if trade fired
                    skip_reason     TEXT,
                    pnl             REAL,
                    outcome         TEXT,        -- WIN/LOSS/OPEN
                    created_at      TEXT,
                    settled_at      TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_latency_asset ON latency_signals(asset, created_at);

                CREATE TABLE IF NOT EXISTS wallet_scores (
                    address         TEXT PRIMARY KEY,
                    score           REAL,
                    roi_30d         REAL,
                    win_rate        REAL,
                    n_trades        INTEGER,
                    last_active     TEXT,
                    diversity       REAL,
                    losing_streak   INTEGER DEFAULT 0,
                    metadata_json   TEXT,
                    updated_at      TEXT
                );

                CREATE TABLE IF NOT EXISTS copy_decisions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet          TEXT,
                    condition_id    TEXT,
                    market_q        TEXT,
                    their_price     REAL,
                    their_size      REAL,
                    our_price       REAL,
                    our_size        REAL,
                    executed        INTEGER,    -- 1 if copied, 0 if skipped
                    skip_reason     TEXT,
                    paper           INTEGER DEFAULT 1,
                    created_at      TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_copy_wallet ON copy_decisions(wallet, created_at);

                CREATE TABLE IF NOT EXISTS strategy_pnl (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date      TEXT,
                    strategy        TEXT,
                    trades          INTEGER,
                    wins            INTEGER,
                    losses          INTEGER,
                    net_pnl         REAL,
                    paper           INTEGER DEFAULT 1,
                    UNIQUE(trade_date, strategy, paper)
                );
            """)

    # ---- Strategy 1: arbitrage -------------------------------------------
    def log_arb_trade(
        self,
        condition_id: str,
        market_q: str,
        yes_price: float,
        no_price: float,
        capital: float,
        shares: float,
        fee_cost: float,
        paper: bool = True,
    ) -> int:
        combined = yes_price + no_price
        spread = 1.0 - combined
        gross = spread * shares          # $1 payout per share minus cost
        net = gross - fee_cost
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO arb_trades "
                "(condition_id,market_q,yes_price,no_price,combined_price,spread,"
                "capital,shares,gross_profit,fee_cost,net_profit,paper,executed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (condition_id, market_q, yes_price, no_price, combined, spread,
                 capital, shares, gross, fee_cost, net, int(paper),
                 datetime.utcnow().isoformat()),
            )
            return int(cur.lastrowid or 0)

    def arb_pnl(self, since: Optional[str] = None) -> float:
        sql = "SELECT COALESCE(SUM(net_profit), 0) FROM arb_trades"
        args: tuple = ()
        if since:
            sql += " WHERE executed_at >= ?"
            args = (since,)
        with self._conn() as conn:
            return float(conn.execute(sql, args).fetchone()[0] or 0.0)

    # ---- Strategy 2: latency ----------------------------------------------
    def log_latency_signal(
        self,
        asset: str,
        direction: str,
        momentum_pct: float,
        spot_price: float,
        poly_yes_price: float,
        poly_token_id: Optional[str],
        market_q: Optional[str],
        entry_size: float,
        acted: bool,
        skip_reason: Optional[str] = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO latency_signals "
                "(asset,direction,momentum_pct,spot_price,poly_yes_price,poly_token_id,"
                "market_q,entry_size,acted,skip_reason,outcome,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset, direction, momentum_pct, spot_price, poly_yes_price,
                 poly_token_id, market_q, entry_size, int(acted), skip_reason,
                 "OPEN" if acted else "SKIPPED",
                 datetime.utcnow().isoformat()),
            )
            return int(cur.lastrowid or 0)

    def settle_latency_signal(self, signal_id: int, pnl: float, outcome: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE latency_signals SET pnl=?, outcome=?, settled_at=? WHERE id=?",
                (pnl, outcome, datetime.utcnow().isoformat(), signal_id),
            )

    def latency_stats(self, since: Optional[str] = None) -> dict[str, Any]:
        where = ""
        args: tuple = ()
        if since:
            where = "WHERE created_at >= ?"
            args = (since,)
        with self._conn() as conn:
            r = conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN acted=1 THEN 1 ELSE 0 END) AS acted, "
                f"SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins, "
                f"SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses, "
                f"COALESCE(SUM(pnl), 0) AS pnl "
                f"FROM latency_signals {where}",
                args,
            ).fetchone()
            total = r["total"] or 0
            acted = r["acted"] or 0
            wins = r["wins"] or 0
            losses = r["losses"] or 0
            return {
                "total": total,
                "acted": acted,
                "wins": wins,
                "losses": losses,
                "pnl": float(r["pnl"] or 0.0),
                "win_rate": (wins / (wins + losses)) if (wins + losses) else 0.0,
            }

    # ---- Strategy 3: wallet copy -----------------------------------------
    def upsert_wallet_score(
        self,
        address: str,
        score: float,
        roi_30d: float,
        win_rate: float,
        n_trades: int,
        last_active: str,
        diversity: float,
        losing_streak: int = 0,
        metadata: Optional[dict] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO wallet_scores "
                "(address,score,roi_30d,win_rate,n_trades,last_active,diversity,losing_streak,"
                "metadata_json,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET "
                "score=excluded.score, roi_30d=excluded.roi_30d, "
                "win_rate=excluded.win_rate, n_trades=excluded.n_trades, "
                "last_active=excluded.last_active, diversity=excluded.diversity, "
                "losing_streak=excluded.losing_streak, "
                "metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
                (address.lower(), score, roi_30d, win_rate, n_trades, last_active,
                 diversity, losing_streak,
                 json.dumps(metadata or {}),
                 datetime.utcnow().isoformat()),
            )

    def get_wallet_score(self, address: str) -> Optional[dict]:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM wallet_scores WHERE address=?",
                (address.lower(),),
            ).fetchone()
            return dict(r) if r else None

    def top_wallets(self, n: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wallet_scores ORDER BY score DESC LIMIT ?",
                (n,),
            ).fetchall()
            return [dict(r) for r in rows]

    def log_copy_decision(
        self,
        wallet: str,
        condition_id: Optional[str],
        market_q: str,
        their_price: float,
        their_size: float,
        our_price: Optional[float],
        our_size: Optional[float],
        executed: bool,
        skip_reason: Optional[str] = None,
        paper: bool = True,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO copy_decisions "
                "(wallet,condition_id,market_q,their_price,their_size,our_price,our_size,"
                "executed,skip_reason,paper,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (wallet.lower(), condition_id, market_q, their_price, their_size,
                 our_price, our_size, int(executed), skip_reason, int(paper),
                 datetime.utcnow().isoformat()),
            )
            return int(cur.lastrowid or 0)

    def copy_stats(self, wallet: Optional[str] = None) -> dict[str, Any]:
        where = ""
        args: tuple = ()
        if wallet:
            where = "WHERE wallet=?"
            args = (wallet.lower(),)
        with self._conn() as conn:
            r = conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN executed=1 THEN 1 ELSE 0 END) AS executed "
                f"FROM copy_decisions {where}",
                args,
            ).fetchone()
            return {
                "total": r["total"] or 0,
                "executed": r["executed"] or 0,
                "skipped": (r["total"] or 0) - (r["executed"] or 0),
            }

    # ---- Per-strategy PnL rollup -----------------------------------------
    def record_strategy_pnl(
        self,
        strategy: str,
        net_pnl: float,
        wins: int = 0,
        losses: int = 0,
        trades: int = 0,
        paper: bool = True,
        trade_date: Optional[str] = None,
    ) -> None:
        d = trade_date or date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO strategy_pnl (trade_date,strategy,trades,wins,losses,net_pnl,paper) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(trade_date,strategy,paper) DO UPDATE SET "
                "trades=excluded.trades, wins=excluded.wins, losses=excluded.losses, "
                "net_pnl=excluded.net_pnl",
                (d, strategy, trades, wins, losses, net_pnl, int(paper)),
            )

    def strategy_pnl_today(self) -> dict[str, float]:
        today = date.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT strategy, COALESCE(SUM(net_pnl), 0) AS pnl "
                "FROM strategy_pnl WHERE trade_date=? GROUP BY strategy",
                (today,),
            ).fetchall()
            return {r["strategy"]: float(r["pnl"] or 0.0) for r in rows}

    # ---- Shared PnL helpers ---------------------------------------------
    def combined_pnl_today(self) -> float:
        """Sum of scalper trades + arb trades + latency trades for today.

        The ``trades`` table is owned by ``TradeLogger`` (scalper); if it
        hasn't been created yet we quietly treat it as zero.
        """
        today = date.today().isoformat()
        with self._conn() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            scalper = 0.0
            if "trades" in tables:
                scalper = conn.execute(
                    "SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE date(closed_at)=?",
                    (today,),
                ).fetchone()[0] or 0.0
            arb = conn.execute(
                "SELECT COALESCE(SUM(net_profit), 0) FROM arb_trades WHERE date(executed_at)=?",
                (today,),
            ).fetchone()[0] or 0.0
            lat = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM latency_signals "
                "WHERE date(settled_at)=? AND outcome IN ('WIN','LOSS')",
                (today,),
            ).fetchone()[0] or 0.0
            return float(scalper + arb + lat)

    # ---- Maintenance ------------------------------------------------------
    def prune_old(self, days: int = 90) -> None:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM arb_trades WHERE executed_at < ?", (cutoff,))
            conn.execute("DELETE FROM latency_signals WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM copy_decisions WHERE created_at < ?", (cutoff,))
