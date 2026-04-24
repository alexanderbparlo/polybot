# Polybot — Polymarket Hybrid Trading Bot

Async Python bot that runs **three Polymarket strategies concurrently** with
shared risk management. Originally scaffolded from a Kalshi spread scalper;
the legacy scalper is still runnable via `--scalper`.

The three hybrid strategies:

| # | Strategy                              | Module                   |
|---|---------------------------------------|--------------------------|
| 1 | Sub-$1 rebalancing arbitrage          | `strategy1_arbitrage.py` |
| 2 | Binance → Polymarket latency arb      | `strategy2_latency.py`   |
| 3 | Wallet copy trading                   | `strategy3_copy.py`      |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in .env
cp .env.example .env
# edit .env: POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS, ...

# 3. Derive L2 API credentials (only needed for live trading)
python main.py setup-credentials
# paste the printed POLYMARKET_API_* values into .env

# 4. Run all three strategies in paper mode (safe, default)
python main.py

# 5. Run only one strategy
python main.py --only arbitrage
python main.py --only latency
python main.py --only copy

# 6. Skip one strategy
python main.py --skip copy

# 7. Legacy spread scalper (unchanged behaviour)
python main.py --scalper --conservative
```

## Live Trading

**Both** gates must be satisfied:

```bash
# 1. In .env:
LIVE_TRADING=true

# 2. CLI flag:
python main.py --live
```

If either is missing the bot silently falls back to paper mode. `setup-credentials`
must be run once, and USDC / conditional-token allowances must be approved on
your wallet for first-time live use.

## Strategy 1 — Sub-$1 Rebalancing Arbitrage

On every Polymarket binary market, `YES + NO ≡ $1` at settlement. When both
legs' best-ask prices sum to less than `1 - ARB_MIN_EDGE`, we buy both
simultaneously to lock a risk-free payoff.

Key env vars (defaults shown):

```
ARB_MIN_EDGE=0.03          # YES+NO must be < 0.97 (3% buffer above 2% fee)
ARB_MIN_LIQUIDITY=2000     # require $2k depth on each side
ARB_SCAN_INTERVAL=3        # seconds between Gamma scans
ARB_FEE_RATE=0.02          # winner-side fee
ARB_MARKET_LIMIT=200       # top-N markets crawled per scan
```

Run standalone:

```bash
python strategy1_arbitrage.py
```

Every executed arb is logged to `arb_trades` in `polymarket_trades.db` and a
Telegram alert fires with spread %, capital deployed, and net profit.

## Strategy 2 — Latency Arb on 15-min Crypto Contracts

Subscribes to Binance trade streams for BTC, ETH, SOL. When a 30-second
rolling momentum exceeds `LATENCY_MOMENTUM_PCT` with volume confirmation, the
strategy buys YES on the matching Polymarket 15-minute up/down market —
provided the order book hasn't yet repriced past the configured thresholds.
Contracts auto-settle after 15 minutes; PnL is booked when the settlement
query returns.

Key env vars:

```
BINANCE_WS_URL=wss://stream.binance.com:9443/ws
LATENCY_SYMBOLS=btcusdt,ethusdt,solusdt
LATENCY_MOMENTUM_PCT=0.0015     # 0.15% move in 30s
LATENCY_WINDOW_SECS=30
LATENCY_UP_MAX_PRICE=0.60       # skip UP signal if YES already above 0.60
LATENCY_DOWN_MIN_PRICE=0.40     # skip DOWN signal if YES already below 0.40
LATENCY_TRADE_SIZE=50           # flat $ per trade
```

Run standalone:

```bash
python strategy2_latency.py
```

Signals (acted AND skipped) are logged to `latency_signals`. Telegram alerts
fire on entry and settlement.

## Strategy 3 — Wallet Copy Trading

Watches a configurable list of Polymarket wallets and mirrors their buys
subject to a pre-trade filter: liquidity ≥ $1,000 each side, current price
within 5% of their entry, wallet not on a 3-trade losing streak, and our
exposure to the market < 10% of total capital.

Key env vars:

```
COPY_WALLETS=0xabc,0xdef,...    # lowercase wallet addresses (comma-separated)
COPY_MIN_TRADES=50              # min trades for a wallet to be rankable
COPY_LOOKBACK_DAYS=30           # scoring window
COPY_SIZE_FRACTION=0.25         # mirror 25% of their size
COPY_SIZE_CAP=100               # paper-mode cap per copy
COPY_POLL_INTERVAL=20           # seconds between activity polls
COPY_MAX_PRICE_SLIP=0.05
COPY_MARKET_MIN_LIQ=1000
COPY_MARKET_MAX_EXPOSURE_PCT=0.10
```

Run standalone:

```bash
python strategy3_copy.py
```

Each decision (executed OR skipped with reason) is written to
`copy_decisions`. Wallets are rescored hourly into `wallet_scores`.

## Global Risk Management

`position_manager.PositionManager` is shared by all strategies:

| Cap                       | Env var                 | Default |
|---------------------------|-------------------------|---------|
| Total capital assumed     | `TOTAL_CAPITAL`         | `1000`  |
| Max exposure (% capital)  | `MAX_EXPOSURE_PCT`      | `0.30`  |
| Max single trade (USDC)   | `MAX_TRADE_SIZE`        | `100`   |
| Default position size     | `MAX_POSITION`          | `50`    |
| Daily loss halt           | `DAILY_LOSS_LIMIT`      | `-50`   |
| Max correlated positions  | `MAX_CORRELATED_OPEN`   | `3`     |

Crypto-themed markets (BTC/ETH/SOL) are bucketed as "correlated" so the three
latency legs can't stack past `MAX_CORRELATED_OPEN`.

If today's combined PnL drops below `DAILY_LOSS_LIMIT`, the watchdog halts
all strategies and fires a Telegram alert.

## Reporting

```bash
python report.py         # human-readable status
python report.py --json  # machine-readable
```

Shows today's PnL by strategy, open positions, arbitrage / latency / copy
stats, risk caps, and the top-ranked copy wallets.

## Backtester (legacy scalper)

```bash
python backtest.py --token <token_id> --days 30
python backtest.py --sweep --token <token_id>
python backtest.py --batch --tokens <id1> <id2>
```

## Testing

```bash
python tests/test_logger.py
python tests/polymarket_examples.py
```

## Architecture

```
main.py                       hybrid orchestrator (asyncio.gather)
config.py                     HybridConfig (env loading + validation)
database.py                   HybridDatabase (extra SQLite tables)
polymarket_client.py          thin wrapper + helpers on polymarket/
position_manager.py           global risk caps + halt logic
telegram_bot.py               HybridTelegram (extends logger.TelegramAlert)

strategy1_arbitrage.py
strategy2_latency.py
strategy3_copy.py

polymarket/                   (unchanged) CLOB / Gamma / Data / WebSocket
logger.py                     (unchanged) TradeLogger + TelegramAlert
spread_scalper.py             (unchanged) legacy scalper
backtester/                   (unchanged) historical simulation
```

See `docs/architecture.md` for the underlying Polymarket client design.

## Geographic Note

Polymarket obtained CFTC approval in 2024. Confirm current U.S. availability
before going live. Paper mode works from any location.
