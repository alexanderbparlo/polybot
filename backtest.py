"""
Backtester CLI

Usage:
    python backtest.py --token <token_id> [--days 30] [--candle 60]
    python backtest.py --sweep --token <token_id>
    python backtest.py --batch --tokens <id1> <id2> ...
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging

from dotenv import load_dotenv

load_dotenv()

from backtester import BacktestRunner
from backtester.engine import BacktestConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


async def run_single(args: argparse.Namespace) -> None:
    runner = BacktestRunner()
    result = await runner.run_single(args.token, days_back=args.days, candle_secs=args.candle)
    print(f"\n{'='*60}")
    print(f"Token:           {args.token}")
    print(f"Days:            {args.days}")
    print(f"Candle (secs):   {args.candle}")
    print(f"{'='*60}")
    print(f"Candles:         {result.candles_processed}")
    print(f"Trades:          {result.total_trades}")
    print(f"Win Rate:        {result.win_rate:.1%}")
    print(f"Net PnL:         ${result.net_pnl:.4f}")
    print(f"Total Fees:      ${result.total_fees:.4f}")
    print(f"Avg PnL/Trade:   ${result.avg_pnl_per_trade:.4f}")
    print(f"Max Drawdown:    ${result.max_drawdown:.4f}")
    print(f"Sharpe Ratio:    {result.sharpe_ratio:.3f}")
    print(f"{'='*60}")


async def run_sweep(args: argparse.Namespace) -> None:
    param_grid = {
        "min_spread_cents": [3.5, 4.0, 5.0],
        "target_profit_cents": [2.0, 2.5, 3.0],
        "stop_loss_cents": [3.0, 3.5, 4.0],
    }
    runner = BacktestRunner()
    results = await runner.parameter_sweep(args.token, param_grid, days_back=args.days)

    print(f"\nParameter Sweep Results (top 10):")
    print(f"{'Params':<50} {'Net PnL':>10} {'WR':>8} {'Trades':>8}")
    print("-" * 78)
    for row in results[:10]:
        p = row["params"]
        r = row["result"]
        param_str = f"spread={p.get('min_spread_cents')} tgt={p.get('target_profit_cents')} sl={p.get('stop_loss_cents')}"
        print(f"{param_str:<50} ${r.net_pnl:>9.3f} {r.win_rate:>7.1%} {r.total_trades:>8}")


async def run_batch(args: argparse.Namespace) -> None:
    runner = BacktestRunner()
    results = await runner.run_batch(args.tokens, days_back=args.days, candle_secs=args.candle)
    print(f"\nBatch Backtest Results ({len(results)} markets):")
    print(f"{'Token':<20} {'Trades':>7} {'WR':>7} {'Net PnL':>10} {'DD':>10}")
    print("-" * 57)
    for r in sorted(results, key=lambda x: x.net_pnl, reverse=True):
        print(f"{r.token_id[:18]:<20} {r.total_trades:>7} {r.win_rate:>6.1%} ${r.net_pnl:>9.3f} ${r.max_drawdown:>9.3f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Backtester")
    parser.add_argument("--token", help="Single token_id to backtest")
    parser.add_argument("--tokens", nargs="+", help="Multiple token_ids for batch backtest")
    parser.add_argument("--days", type=int, default=30, help="Days of history (default: 30)")
    parser.add_argument("--candle", type=int, default=60, help="Candle size in seconds (default: 60)")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--batch", action="store_true", help="Run batch backtest across tokens")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.sweep and args.token:
        asyncio.run(run_sweep(args))
    elif args.batch and args.tokens:
        asyncio.run(run_batch(args))
    elif args.token:
        asyncio.run(run_single(args))
    else:
        print("Usage: python backtest.py --token <token_id> [--days 30] [--candle 60]")
        print("       python backtest.py --sweep --token <token_id>")
        print("       python backtest.py --batch --tokens <id1> <id2>")
