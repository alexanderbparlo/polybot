"""
Polymarket Spread Scalping Bot — Entry Point

Usage:
    python main.py                    # paper mode, default config
    python main.py --live             # real orders (requires POLY_PAPER_MODE=false in .env)
    python main.py --conservative     # tighter risk params
    python main.py --aggressive       # wider params
    python main.py setup-credentials  # one-time L2 key derivation
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before any polymarket imports that read env vars
load_dotenv()

from polymarket import PolymarketClient
from polymarket.config import PolymarketConfig
from logger import TradeLogger, TelegramAlert
from spread_scalper import ScalperConfig, SpreadScalper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

async def preflight_checks(client: PolymarketClient, live: bool) -> float:
    """Run startup checks. Returns USDC balance. Raises on hard failures."""
    logger.info("Running preflight checks…")

    balance = await client.get_balance()
    logger.info("USDC balance: $%.2f", balance)

    if live:
        # Check MATIC gas balance via Web3
        try:
            from web3 import Web3  # type: ignore
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            addr = client.config.funder_address
            if addr:
                matic_wei = w3.eth.get_balance(Web3.to_checksum_address(addr))
                matic = matic_wei / 1e18
                if matic < 0.1:
                    raise SystemExit(
                        f"CRITICAL: MATIC balance {matic:.4f} < 0.1 — "
                        "bot cannot sign transactions. Please top up."
                    )
                if matic < 0.5:
                    logger.warning("Low MATIC balance: %.4f (recommend >= 0.5)", matic)
                else:
                    logger.info("MATIC balance: %.4f", matic)
        except ImportError:
            logger.warning("web3 not installed — skipping MATIC balance check")
        except Exception as exc:
            logger.warning("MATIC check failed (non-fatal): %s", exc)

        # Remind about token allowances for EOA wallets
        if client.config.signature_type == 0:
            logger.warning(
                "EOA wallet detected. Before your first trade, ensure USDC and "
                "conditional token allowances are approved. See: "
                "https://docs.polymarket.com/developers/CLOB/quickstart"
            )

    return balance


# ---------------------------------------------------------------------------
# Credential setup subcommand
# ---------------------------------------------------------------------------

async def setup_credentials() -> None:
    """Derive L2 API credentials from L1 private key and print them."""
    config = PolymarketConfig.from_env()
    if not config.private_key:
        print("ERROR: POLY_PRIVATE_KEY not set in .env")
        sys.exit(1)

    print("Deriving L2 API credentials from private key…")
    async with PolymarketClient(config) as client:
        creds = await client.setup_credentials()

    print("\nAdd these to your .env file:")
    print(f"POLY_API_KEY={creds.get('apiKey', '')}")
    print(f"POLY_API_SECRET={creds.get('secret', '')}")
    print(f"POLY_API_PASSPHRASE={creds.get('passphrase', '')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    # -----------------------------------------------------------------------
    # Safety gate: double-lock on live trading
    # -----------------------------------------------------------------------
    live_mode = False
    if args.live:
        env_paper = os.getenv("POLY_PAPER_MODE", "true").lower()
        if env_paper != "false":
            logger.error(
                "--live flag passed but POLY_PAPER_MODE is not 'false' in .env. "
                "Set POLY_PAPER_MODE=false to enable real orders."
            )
            sys.exit(1)
        live_mode = True
        logger.warning("LIVE MODE ENABLED — real orders will be placed")
    else:
        logger.info("Paper mode (default) — no real orders")

    # Override paper_mode in env so config picks it up correctly
    if not live_mode:
        os.environ["POLY_PAPER_MODE"] = "true"

    # -----------------------------------------------------------------------
    # Config profile
    # -----------------------------------------------------------------------
    if args.conservative:
        cfg = ScalperConfig.conservative()
        logger.info("Using CONSERVATIVE profile")
    elif args.aggressive:
        cfg = ScalperConfig.aggressive()
        logger.info("Using AGGRESSIVE profile")
    else:
        cfg = ScalperConfig()
        logger.info("Using DEFAULT profile")

    # -----------------------------------------------------------------------
    # Build client, logger, alerts
    # -----------------------------------------------------------------------
    poly_config = PolymarketConfig.from_env()
    trade_logger = TradeLogger()
    telegram = TelegramAlert(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )

    async with PolymarketClient(poly_config) as client:
        balance = await preflight_checks(client, live=live_mode)

        scalper = SpreadScalper(
            client=client,
            cfg=cfg,
            trade_logger=trade_logger,
            telegram=telegram,
        )

        try:
            await scalper.run()
        except asyncio.CancelledError:
            logger.info("Scalper loop cancelled — shutting down gracefully")
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — shutting down")
        finally:
            # Cancel any open paper positions and log a daily summary
            if scalper.open_positions:
                await client.cancel_all_orders()
            pnl = trade_logger.get_daily_pnl()
            summary = {
                "trades": len(trade_logger.get_recent_trades(1000)),
                "total_pnl": pnl,
                "wins": 0,   # Would be counted from DB in production
                "losses": 0,
                "max_drawdown": 0.0,
            }
            trade_logger.log_daily_summary(summary, paper=not live_mode)
            telegram.daily_summary(summary)
            logger.info("Shutdown complete. Daily PnL: $%.2f", pnl)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket Spread Scalping Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("subcommand", nargs="?", help="setup-credentials")
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires POLY_PAPER_MODE=false)")
    parser.add_argument("--conservative", action="store_true", help="Conservative risk profile")
    parser.add_argument("--aggressive", action="store_true", help="Aggressive risk profile")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.subcommand == "setup-credentials":
        asyncio.run(setup_credentials())
    else:
        try:
            asyncio.run(main(args))
        except KeyboardInterrupt:
            pass
