"""
Polybot Hybrid Entrypoint.

Runs the three hybrid Polymarket strategies concurrently via asyncio:

    1. Sub-$1 rebalancing arbitrage   (strategy1_arbitrage.ArbitrageStrategy)
    2. Binance → Polymarket latency arb (strategy2_latency.LatencyStrategy)
    3. Wallet copy trading            (strategy3_copy.CopyStrategy)

They share a single HybridPolymarketClient (one aiohttp session), a single
HybridDatabase (one sqlite file), a single TelegramAlert, and one
PositionManager enforcing global exposure / daily-loss caps.

Usage
-----
    python main.py                       # run all three in paper mode
    python main.py --live                # go live (requires LIVE_TRADING=true)
    python main.py --only arbitrage      # run one strategy only
    python main.py --skip copy           # run all except copy
    python main.py --scalper             # run the legacy spread scalper
    python main.py setup-credentials     # one-time L2 key derivation
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Awaitable, Callable

from dotenv import load_dotenv

load_dotenv()

from config import HybridConfig
from database import HybridDatabase
from logger import TradeLogger
from polymarket_client import HybridPolymarketClient
from polymarket.config import PolymarketConfig
from polymarket import PolymarketClient  # for setup-credentials
from position_manager import PositionManager
from strategy1_arbitrage import ArbitrageStrategy
from strategy2_latency import LatencyStrategy
from strategy3_copy import CopyStrategy
from telegram_bot import HybridTelegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polybot.main")

STRATEGIES = ("arbitrage", "latency", "copy")


# ---------------------------------------------------------------------------
# Preflight (live-mode only)
# ---------------------------------------------------------------------------

async def preflight(client: HybridPolymarketClient, live: bool) -> None:
    logger.info("Preflight: fetching USDC balance…")
    try:
        bal = await client.get_balance()
        logger.info("USDC balance: $%.2f", bal)
    except Exception as exc:
        logger.warning("Balance check failed (non-fatal): %s", exc)

    if not live:
        return

    try:
        from web3 import Web3  # type: ignore
        w3 = Web3(Web3.HTTPProvider(os.getenv("ALCHEMY_RPC_URL", "https://polygon-rpc.com")))
        addr = client.config.funder_address
        if addr:
            matic_wei = w3.eth.get_balance(Web3.to_checksum_address(addr))
            matic = matic_wei / 1e18
            if matic < 0.1:
                raise SystemExit(
                    f"CRITICAL: MATIC balance {matic:.4f} < 0.1 — bot cannot sign."
                )
            logger.info("MATIC balance: %.4f", matic)
    except ImportError:
        logger.warning("web3 not installed — skipping MATIC check")
    except Exception as exc:
        logger.warning("MATIC check failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# setup-credentials (unchanged from legacy main)
# ---------------------------------------------------------------------------

async def setup_credentials() -> None:
    cfg = PolymarketConfig.from_env()
    if not cfg.private_key:
        print("ERROR: POLY_PRIVATE_KEY / POLYMARKET_PRIVATE_KEY not set.", file=sys.stderr)
        sys.exit(1)
    print("Deriving L2 API credentials from private key…")
    async with PolymarketClient(cfg) as client:
        creds = await client.setup_credentials()
    print("\nAdd these to your .env:")
    print(f"POLYMARKET_API_KEY={creds.get('apiKey', '')}")
    print(f"POLYMARKET_API_SECRET={creds.get('secret', '')}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.get('passphrase', '')}")


# ---------------------------------------------------------------------------
# Scalper delegation (legacy)
# ---------------------------------------------------------------------------

async def run_scalper(args: argparse.Namespace) -> None:
    """Delegate to the pre-existing spread scalper."""
    from spread_scalper import ScalperConfig, SpreadScalper

    cfg = HybridConfig.from_env()
    if args.conservative:
        sc = ScalperConfig.conservative()
    elif args.aggressive:
        sc = ScalperConfig.aggressive()
    else:
        sc = ScalperConfig()

    trade_logger = TradeLogger()
    telegram = HybridTelegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    async with HybridPolymarketClient(cfg.polymarket) as client:
        scalper = SpreadScalper(
            client=client.base, cfg=sc, trade_logger=trade_logger, telegram=telegram,
        )
        try:
            await scalper.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("scalper shutdown")


# ---------------------------------------------------------------------------
# Hybrid orchestrator
# ---------------------------------------------------------------------------

async def run_hybrid(args: argparse.Namespace) -> None:
    cfg = HybridConfig.from_env()
    warnings = cfg.sanity_check()
    for w in warnings:
        logger.warning("config: %s", w)

    # Live-mode gate — must pass the --live flag AND have LIVE_TRADING=true
    # (or legacy POLY_PAPER_MODE=false) in env, as already resolved by
    # HybridConfig.from_env().
    if args.live:
        if cfg.paper_mode:
            logger.error(
                "--live passed but LIVE_TRADING is not 'true' in env. "
                "Set LIVE_TRADING=true (or POLY_PAPER_MODE=false) to enable real orders."
            )
            sys.exit(1)
        logger.warning("LIVE MODE — real orders will be placed on Polymarket.")
    else:
        # Force paper regardless of env if --live not passed.
        cfg.live_trading = False
        cfg.polymarket.paper_mode = True
        logger.info("Paper mode (default) — no real orders.")

    # Filter strategies
    enabled = set(STRATEGIES)
    if args.only:
        enabled = set(args.only.split(","))
    if args.skip:
        enabled -= set(args.skip.split(","))
    bad = enabled - set(STRATEGIES)
    if bad:
        logger.error("unknown strategies: %s", bad)
        sys.exit(2)
    if not enabled:
        logger.error("no strategies to run")
        sys.exit(2)
    logger.info("enabled strategies: %s", sorted(enabled))

    db = HybridDatabase(cfg.db_path)
    trade_logger = TradeLogger()
    telegram = HybridTelegram(cfg.telegram_bot_token, cfg.telegram_chat_id)
    pm = PositionManager(
        total_capital=cfg.total_capital,
        max_exposure_pct=cfg.max_exposure_pct,
        max_trade_size=cfg.max_trade_size,
        daily_loss_limit=cfg.daily_loss_limit,
        max_correlated_open=cfg.max_correlated_open,
        initial_daily_pnl=db.combined_pnl_today(),
    )

    async with HybridPolymarketClient(cfg.polymarket) as client:
        await preflight(client, live=not cfg.paper_mode)

        instances: list = []
        tasks: list[asyncio.Task] = []

        if "arbitrage" in enabled:
            s = ArbitrageStrategy(cfg, client, db, trade_logger, telegram, pm)
            instances.append(s)
            tasks.append(asyncio.create_task(s.run(), name="arbitrage"))
        if "latency" in enabled:
            s = LatencyStrategy(cfg, client, db, trade_logger, telegram, pm)
            instances.append(s)
            tasks.append(asyncio.create_task(s.run(), name="latency"))
        if "copy" in enabled:
            s = CopyStrategy(cfg, client, db, trade_logger, telegram, pm)
            instances.append(s)
            tasks.append(asyncio.create_task(s.run(), name="copy"))

        telegram.hybrid_started(sorted(enabled), cfg.paper_mode)

        # Global shutdown handler
        stop_evt = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _stop_all() -> None:
            logger.info("shutdown signal received — stopping all strategies")
            stop_evt.set()
            for inst in instances:
                try:
                    inst.request_stop()
                except Exception:
                    pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop_all)
            except NotImplementedError:
                pass  # Windows

        # Watchdog: if PositionManager halts, stop everything.
        async def _watchdog() -> None:
            while not stop_evt.is_set():
                if pm.is_halted:
                    logger.warning("risk halt tripped: %s", pm.halt_reason)
                    telegram.risk_halt(pm.halt_reason or "halt", pm.daily_pnl)
                    _stop_all()
                    return
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

        wd = asyncio.create_task(_watchdog(), name="watchdog")

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            wd.cancel()
            try:
                await wd
            except (asyncio.CancelledError, Exception):
                pass

            # Final daily summary
            pnl = db.combined_pnl_today()
            summary = {
                "trades": len(trade_logger.get_recent_trades(1000)),
                "total_pnl": pnl,
                "wins": 0,
                "losses": 0,
                "max_drawdown": 0.0,
            }
            trade_logger.log_daily_summary(summary, paper=cfg.paper_mode)
            telegram.daily_summary(summary)
            logger.info("shutdown complete. daily PnL: $%+.2f", pnl)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Polybot — Polymarket Hybrid Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("subcommand", nargs="?", help="optional subcommand (setup-credentials)")
    p.add_argument("--live", action="store_true",
                   help="Enable live trading (requires LIVE_TRADING=true in .env)")
    p.add_argument("--only", help="Comma-separated subset of strategies to run "
                                 "(choices: arbitrage,latency,copy)")
    p.add_argument("--skip", help="Comma-separated strategies to exclude")
    p.add_argument("--scalper", action="store_true",
                   help="Run the legacy SpreadScalper instead of the hybrid bundle")
    p.add_argument("--conservative", action="store_true",
                   help="(scalper mode only) conservative profile")
    p.add_argument("--aggressive", action="store_true",
                   help="(scalper mode only) aggressive profile")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.subcommand == "setup-credentials":
        asyncio.run(setup_credentials())
        return
    if args.scalper:
        asyncio.run(run_scalper(args))
        return
    try:
        asyncio.run(run_hybrid(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
