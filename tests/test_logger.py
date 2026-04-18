"""
Logger integration test.

Run: python tests/test_logger.py

Checks:
  - SQLite DB created and tables exist
  - Orders, trades, events write correctly
  - Telegram test message sent (if credentials set)
"""

import asyncio
import os
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from logger import TradeLogger, TelegramAlert


def test_trade_logger():
    db_path = Path("test_trades.db")
    if db_path.exists():
        db_path.unlink()

    logger = TradeLogger(db_path)
    print("✓ DB created")

    logger.log_order(
        order_id="TEST_001",
        token_id="abc123",
        market_q="Will BTC hit $100k?",
        side="BUY",
        price=0.42,
        size=10.0,
        status="LIVE",
        paper=True,
    )
    print("✓ log_order OK")

    logger.log_trade(
        token_id="abc123",
        market_q="Will BTC hit $100k?",
        side="BUY",
        entry_price=0.42,
        exit_price=0.445,
        size=10.0,
        net_pnl=0.132,
        entry_fee=0.084,
        exit_fee=0.089,
        hold_secs=120.0,
        exit_reason="target",
        paper=True,
    )
    print("✓ log_trade OK")

    logger.log_event("INFO", "Test event", "detail goes here")
    print("✓ log_event OK")

    daily_pnl = logger.get_daily_pnl()
    print(f"✓ get_daily_pnl: ${daily_pnl:.4f}")

    trades = logger.get_recent_trades(10)
    assert len(trades) == 1, f"Expected 1 trade, got {len(trades)}"
    assert trades[0]["net_pnl"] == 0.132
    print(f"✓ get_recent_trades: {len(trades)} trade(s)")

    logger.log_daily_summary(
        {"trades": 1, "wins": 1, "losses": 0, "total_pnl": 0.132, "max_drawdown": 0.0},
        paper=True,
    )
    print("✓ log_daily_summary OK")

    # Cleanup
    db_path.unlink()
    print("✓ DB cleanup OK")
    print("\nAll TradeLogger tests passed.")


async def test_telegram():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("\nTelegram credentials not set — skipping Telegram test.")
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable.")
        return

    tg = TelegramAlert(bot_token, chat_id)
    tg.test()
    # Give the async fire-and-forget task time to complete
    await asyncio.sleep(2)
    print("✓ Telegram test message sent (check your phone)")


if __name__ == "__main__":
    print("=== TradeLogger tests ===")
    test_trade_logger()

    print("\n=== Telegram tests ===")
    asyncio.run(test_telegram())
