# Polymarket Spread Scalping Bot

Async Python bot that scalps bid-ask spreads on Polymarket prediction markets.
Targets 4¢+ gross spreads, nets ~2.5¢ after Polymarket's ~2% fee.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in .env
cp .env.example .env
# Edit .env: add POLY_PRIVATE_KEY, POLY_FUNDER_ADDRESS

# 3. Derive L2 API credentials (run once)
python main.py setup-credentials
# Copy printed credentials into .env

# 4. Run in paper mode (default — safe, no real orders)
python main.py

# 5. Run with conservative risk profile
python main.py --conservative
```

## Live Trading

Two safety gates must both be satisfied:

```bash
# In .env:
POLY_PAPER_MODE=false

# CLI flag:
python main.py --live
```

## Modes

| Command | Profile | Notes |
|---------|---------|-------|
| `python main.py` | Default | Paper mode |
| `python main.py --conservative` | Conservative | Tighter spreads, smaller positions |
| `python main.py --aggressive` | Aggressive | Wider parameters, larger positions |
| `python main.py --live` | Live | Real orders (requires POLY_PAPER_MODE=false) |

## Backtester

```bash
# Single market
python backtest.py --token <token_id> --days 30

# Parameter sweep
python backtest.py --sweep --token <token_id>

# Batch across markets
python backtest.py --batch --tokens <id1> <id2> <id3>
```

## Dashboard

```bash
cd dashboard
npm install
npm run dev
# Open http://localhost:5173
```

Currently wired to mock/static data. See `docs/architecture.md` for how to
connect a FastAPI WebSocket bridge for live data.

## Testing

```bash
# Logger + Telegram integration test
python tests/test_logger.py

# API usage examples (no auth needed for market/book examples)
python tests/polymarket_examples.py
```

## Blockchain Setup

- Network: Polygon PoS (chain ID 137)
- Collateral: USDC on Polygon
- Gas: MATIC (~$1 worth is plenty; bot warns if < 0.5 MATIC)
- For EOA wallets: approve USDC and conditional token allowances before first trade
  See: https://docs.polymarket.com/developers/CLOB/quickstart

## Strategy

Three-signal composite scoring system:

| Signal | Weight | Threshold |
|--------|--------|-----------|
| Spread | 0.35 | >= 4¢ gross |
| Momentum | 0.45 | >= 0.60 score |
| Liquidity | 0.20 | >= 50 USDC depth |

Trade fires when: composite > 0.55 AND momentum > 0.60

Exit rules:
- Target: +2.5¢ → post limit sell
- Stop loss: -3.5¢ → market sell
- Time stop: 300s → market sell

## Architecture

See `docs/architecture.md` for full system design.

## Geographic Note

Polymarket obtained CFTC approval in 2024. Confirm current U.S. availability
status before going live. Paper mode works from any location.
