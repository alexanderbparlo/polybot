"""
CLI status report.

Prints today's PnL by strategy, the arbitrage / latency / copy stats, any
open positions tracked by the PositionManager (best-effort: we only know
positions for the current process; DB reflects closed trades), top-scored
wallets, and current exposure caps.

Usage:
    python report.py
    python report.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from config import HybridConfig
from database import HybridDatabase


def _fmt_money(x: float) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}${x:,.2f}"


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def collect(cfg: HybridConfig) -> dict:
    db = HybridDatabase(cfg.db_path)
    today_iso = date.today().isoformat()

    # Arb stats (today)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        r_arb = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(net_profit), 0) AS pnl, "
            "COALESCE(SUM(capital), 0) AS capital, "
            "COALESCE(AVG(spread), 0) AS avg_spread "
            "FROM arb_trades WHERE date(executed_at)=?",
            (today_iso,),
        ).fetchone()

        r_lat = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN acted=1 THEN 1 ELSE 0 END) AS acted, "
            "SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses, "
            "COALESCE(SUM(pnl), 0) AS pnl "
            "FROM latency_signals WHERE date(created_at)=?",
            (today_iso,),
        ).fetchone()

        r_copy = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN executed=1 THEN 1 ELSE 0 END) AS executed "
            "FROM copy_decisions WHERE date(created_at)=?",
            (today_iso,),
        ).fetchone()

        # Scalper trades (legacy)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "trades" in tables:
            r_scalp = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl), 0) AS pnl, "
                "SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN net_pnl <= 0 THEN 1 ELSE 0 END) AS losses "
                "FROM trades WHERE date(closed_at)=?",
                (today_iso,),
            ).fetchone()
            scalper = {
                "trades": r_scalp["n"] or 0,
                "wins": r_scalp["wins"] or 0,
                "losses": r_scalp["losses"] or 0,
                "pnl": float(r_scalp["pnl"] or 0.0),
            }
        else:
            scalper = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}

        top_wallets = [
            dict(r) for r in conn.execute(
                "SELECT address, score, roi_30d, win_rate, n_trades, last_active, "
                "losing_streak FROM wallet_scores ORDER BY score DESC LIMIT 10"
            ).fetchall()
        ]

    arb_pnl = float(r_arb["pnl"] or 0.0)
    lat_pnl = float(r_lat["pnl"] or 0.0)
    combined = arb_pnl + lat_pnl + scalper["pnl"]

    return {
        "date": today_iso,
        "paper_mode": cfg.paper_mode,
        "combined_pnl": combined,
        "daily_loss_limit": cfg.daily_loss_limit,
        "strategies": {
            "arbitrage": {
                "trades": r_arb["n"] or 0,
                "capital_deployed": float(r_arb["capital"] or 0.0),
                "avg_spread": float(r_arb["avg_spread"] or 0.0),
                "pnl": arb_pnl,
            },
            "latency": {
                "signals": r_lat["n"] or 0,
                "acted": r_lat["acted"] or 0,
                "wins": r_lat["wins"] or 0,
                "losses": r_lat["losses"] or 0,
                "pnl": lat_pnl,
            },
            "copy": {
                "decisions": r_copy["n"] or 0,
                "executed": r_copy["executed"] or 0,
                "skipped": (r_copy["n"] or 0) - (r_copy["executed"] or 0),
            },
            "scalper": scalper,
        },
        "risk_caps": {
            "total_capital": cfg.total_capital,
            "max_exposure_pct": cfg.max_exposure_pct,
            "max_trade_size": cfg.max_trade_size,
            "max_exposure_usd": cfg.total_capital * cfg.max_exposure_pct,
        },
        "top_wallets": top_wallets,
    }


def render(rep: dict) -> str:
    out: list[str] = []
    mode = "PAPER" if rep["paper_mode"] else "LIVE"
    out.append("=" * 72)
    out.append(f" Polybot Hybrid Report · {rep['date']} · {mode}")
    out.append("=" * 72)
    out.append(
        f" Combined PnL today:  {_fmt_money(rep['combined_pnl'])}   "
        f"(loss-limit {_fmt_money(rep['daily_loss_limit'])})"
    )
    out.append("")
    out.append("-- By strategy ---------------------------------------------------------")
    a = rep["strategies"]["arbitrage"]
    out.append(
        f" [arbitrage]  trades={a['trades']:<4d} capital={_fmt_money(a['capital_deployed']):>12}  "
        f"avg_spread={_fmt_pct(a['avg_spread'])}  pnl={_fmt_money(a['pnl'])}"
    )
    l = rep["strategies"]["latency"]
    wr = (l["wins"] / (l["wins"] + l["losses"])) if (l["wins"] + l["losses"]) else 0.0
    out.append(
        f" [latency]    signals={l['signals']:<4d} acted={l['acted']:<3d} "
        f"W/L={l['wins']}/{l['losses']}  win_rate={wr*100:5.1f}%  pnl={_fmt_money(l['pnl'])}"
    )
    c = rep["strategies"]["copy"]
    out.append(
        f" [copy]       decisions={c['decisions']:<4d} executed={c['executed']:<3d} "
        f"skipped={c['skipped']}"
    )
    s = rep["strategies"]["scalper"]
    out.append(
        f" [scalper]    trades={s['trades']:<4d} W/L={s['wins']}/{s['losses']}  "
        f"pnl={_fmt_money(s['pnl'])}"
    )
    out.append("")
    out.append("-- Risk caps -----------------------------------------------------------")
    rc = rep["risk_caps"]
    out.append(
        f" capital=${rc['total_capital']:,.2f}  "
        f"max_exposure={_fmt_pct(rc['max_exposure_pct'])} "
        f"(${rc['max_exposure_usd']:,.2f})  max_trade=${rc['max_trade_size']:,.2f}"
    )
    out.append("")
    out.append("-- Top copy wallets ----------------------------------------------------")
    if not rep["top_wallets"]:
        out.append(" (none scored yet — run strategy 3)")
    else:
        for w in rep["top_wallets"]:
            addr = w["address"]
            short = f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr
            out.append(
                f" {short}  score={w['score']:.2f}  "
                f"ROI={_fmt_pct(w['roi_30d'])}  "
                f"WR={(w['win_rate'] or 0)*100:5.1f}%  "
                f"n={w['n_trades']}  streak={w.get('losing_streak', 0)}"
            )
    out.append("=" * 72)
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Polybot hybrid report")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    cfg = HybridConfig.from_env()

    db_path = Path(cfg.db_path)
    if not db_path.exists():
        print(f"DB not found at {db_path} — run a strategy first.", file=sys.stderr)
        sys.exit(1)

    # Touch schema (in case report runs before any strategy has)
    HybridDatabase(cfg.db_path)

    rep = collect(cfg)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(render(rep))


if __name__ == "__main__":
    main()
