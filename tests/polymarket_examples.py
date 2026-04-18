"""
Polymarket API usage examples.

Run individual examples by uncommenting the desired test at the bottom.
Requires no auth for market/order book examples (paper mode safe).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from polymarket import PolymarketClient


async def example_get_markets():
    """Fetch top markets by volume — no auth required."""
    async with PolymarketClient() as pm:
        markets = await pm.get_markets(limit=5, min_volume=10_000)
        print(f"\nTop {len(markets)} markets by volume:")
        for m in markets:
            yes = m.yes_token
            price_str = f"{yes.price:.3f}" if yes else "?"
            print(f"  [{price_str}] {m.question[:70]}")


async def example_get_order_books():
    """Fetch order books for top markets — no auth required."""
    async with PolymarketClient() as pm:
        markets = await pm.get_markets(limit=3, min_volume=50_000)
        for m in markets:
            if m.yes_token:
                book = await pm.get_order_book(m.yes_token.token_id)
                print(f"\n{m.question[:60]}")
                print(f"  {book}")
                print(f"  Spread: {book.spread_cents:.1f}¢  BidDepth: {book.bid_depth:.2f}  AskDepth: {book.ask_depth:.2f}")


async def example_get_balance():
    """Fetch USDC balance — requires POLY_FUNDER_ADDRESS in .env."""
    async with PolymarketClient() as pm:
        balance = await pm.get_balance()
        print(f"\nUSDC Balance: ${balance:.4f}")


async def example_get_positions():
    """Fetch open positions — requires POLY_FUNDER_ADDRESS in .env."""
    async with PolymarketClient() as pm:
        positions = await pm.get_positions()
        print(f"\nOpen positions: {len(positions)}")
        for p in positions:
            print(f"  {p.outcome} {p.size:.2f} @ {p.avg_price:.3f} | unrealised: ${p.unrealized_pnl:.3f}")


async def example_setup_credentials():
    """
    Derive L2 API credentials from private key — run once and store in .env.
    Requires POLY_PRIVATE_KEY in .env.
    """
    async with PolymarketClient() as pm:
        creds = await pm.setup_credentials()
        print("\nL2 Credentials (add these to .env):")
        print(f"  POLY_API_KEY={creds.get('apiKey', '')}")
        print(f"  POLY_API_SECRET={creds.get('secret', '')}")
        print(f"  POLY_API_PASSPHRASE={creds.get('passphrase', '')}")


if __name__ == "__main__":
    # Uncomment to run individual examples:
    asyncio.run(example_get_markets())
    # asyncio.run(example_get_order_books())
    # asyncio.run(example_get_balance())
    # asyncio.run(example_get_positions())
    # asyncio.run(example_setup_credentials())
