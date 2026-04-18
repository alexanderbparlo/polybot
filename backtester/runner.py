"""
Backtest runner: single market, batch across markets, or parameter sweep.
"""

from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .data import HistoricalDataFetcher
from .engine import BacktestConfig, BacktestEngine, BacktestResult

logger = logging.getLogger(__name__)


class BacktestRunner:
    """Orchestrates data fetching + engine runs."""

    def __init__(self, cfg: Optional[BacktestConfig] = None):
        self.cfg = cfg or BacktestConfig()
        self.fetcher = HistoricalDataFetcher()
        self.engine = BacktestEngine(self.cfg)

    async def run_single(
        self,
        token_id: str,
        days_back: int = 30,
        candle_secs: int = 60,
    ) -> BacktestResult:
        end_ts = int(time.time())
        start_ts = end_ts - days_back * 86_400
        trades = await self.fetcher.fetch_trades(token_id, start_ts, end_ts)
        candles = self.fetcher.build_candles(trades, interval_secs=candle_secs)
        logger.info("Running backtest: %d candles over %d days", len(candles), days_back)
        return self.engine.run(token_id, candles)

    async def run_batch(
        self,
        token_ids: list[str],
        days_back: int = 30,
        candle_secs: int = 60,
    ) -> list[BacktestResult]:
        tasks = [self.run_single(tid, days_back, candle_secs) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[BacktestResult] = []
        for r in results:
            if isinstance(r, BacktestResult):
                out.append(r)
            else:
                logger.warning("Backtest failed: %s", r)
        return out

    async def parameter_sweep(
        self,
        token_id: str,
        param_grid: dict,
        days_back: int = 30,
    ) -> list[dict]:
        """
        Run a grid search over ScalperConfig parameters.
        param_grid: {"min_spread_cents": [3.5, 4.0, 5.0], "stop_loss_cents": [3.0, 3.5]}
        Returns list of {params, result} dicts sorted by net_pnl descending.
        """
        import itertools
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        end_ts = int(time.time())
        start_ts = end_ts - days_back * 86_400
        trades = await self.fetcher.fetch_trades(token_id, start_ts, end_ts)

        results: list[dict] = []
        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            sweep_cfg = BacktestConfig(**{**self.cfg.__dict__, **params})
            engine = BacktestEngine(sweep_cfg)
            candles = self.fetcher.build_candles(trades)
            result = engine.run(token_id, candles)
            results.append({"params": params, "result": result})

        results.sort(key=lambda x: x["result"].net_pnl, reverse=True)
        return results
