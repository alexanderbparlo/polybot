# Polymarket Scalper — System Architecture

## Overview

Async Python spread scalping bot targeting Polymarket prediction markets.
Strategy: enter inside the bid-ask spread on high-signal tokens, exit at +2.5¢ target,
stop at -3.5¢, or after 5 minutes. ~2% fee makes 4¢+ gross spreads viable.

---

## Module Map

```
main.py
  └── SpreadScalper (spread_scalper.py)
        ├── PolymarketClient (polymarket/client.py)
        │     ├── GammaAPI     — market discovery
        │     ├── ClobAPI      — order book + order management (py-clob-client)
        │     ├── DataAPI      — positions + balance
        │     └── WebSocket    — streaming order book
        ├── TradeLogger (logger.py) — SQLite persistence
        └── TelegramAlert (logger.py) — mobile alerts

backtest.py
  └── BacktestRunner (backtester/runner.py)
        ├── HistoricalDataFetcher (backtester/data.py) — tick fetcher + SQLite cache
        └── BacktestEngine (backtester/engine.py) — simulation engine

dashboard/
  └── React terminal (Vite + Recharts)
        — currently mock data
        — TODO: connect FastAPI WebSocket bridge for live feed
```

---

## Authentication

| Level | Mechanism | Purpose |
|-------|-----------|---------|
| L1    | ECDSA private key (Polygon wallet) | Derive L2 credentials (run once) |
| L2    | HMAC-SHA256 (apiKey + secret + passphrase) | Sign REST requests + WS auth |

**EIP-712 order signing is handled entirely by `py-clob-client`.**
Never attempt to implement EIP-712 manually.

---

## Three-Signal Scoring System

| Signal | Weight | Description |
|--------|--------|-------------|
| Spread | 0.35 | Normalised spread width (4¢ min, linear to 15¢ = 1.0) |
| Momentum | 0.45 | Rolling bid/ask depth trend + instantaneous imbalance |
| Liquidity | 0.20 | Depth at best bid + ask relative to target position size |

**Trade fires when:** composite > 0.55 AND momentum > 0.60

---

## Order Flow

```
Signal fires
  → place_limit_order (BUY inside spread)
  → SimPosition created (paper) or Order tracked (live)
  → monitor: mid price vs stop / target / time
  → exit: limit sell at target, market sell at stop/time
  → log to SQLite + Telegram alert
```

---

## Paper Mode

When `POLY_PAPER_MODE=true` (default):
- All `place_limit_order` / `cancel_order` calls are NO-OPs
- Fill simulated at current mid price at signal time
- `SimPosition` tracked in memory
- All SQLite logs and Telegram alerts fire with `[PAPER]` prefix
- **Identical code path to live** — safest way to verify strategy behaviour

Double-gate to prevent accidental live trading:
1. `--live` CLI flag must be passed
2. `POLY_PAPER_MODE=false` must be set in `.env`

---

## Database Schema

| Table | Purpose |
|-------|---------|
| `orders` | Every order placed (or paper-simulated) |
| `trades` | Closed positions with entry/exit/PnL |
| `events` | Bot log events (signals, warnings, errors) |
| `daily_summary` | Per-day aggregated stats |

---

## Blockchain

- **Network**: Polygon PoS (chain ID 137)
- **Collateral**: USDC (ERC-20)
- **Gas**: MATIC (~$0.01/tx, warn < 0.5 MATIC, halt < 0.1 MATIC)
- **Settlement**: non-custodial via Polymarket Exchange contract

---

## Key Differences vs Kalshi Bot

| Aspect | Kalshi | Polymarket |
|--------|--------|-----------|
| Fee | ~7% | ~2% |
| Min viable spread | 6¢ | 4¢ |
| Auth | RSA-PSS | ECDSA + HMAC |
| Blockchain | None | Polygon |
| Order sides | yes/no | BUY/SELL |
| Order book | Single (complementary) | Separate YES + NO books |

---

## Live Dashboard (FastAPI Bridge — Not Yet Built)

The React dashboard currently uses static mock data.
To connect live data:

1. Add FastAPI server with WebSocket endpoint:
   ```python
   @app.websocket("/ws")
   async def ws_endpoint(ws: WebSocket):
       # push positions, trades, events as JSON
   ```
2. Uncomment proxy in `dashboard/vite.config.js`
3. Replace mock data in `Dashboard.jsx` with `useEffect` + `WebSocket` hook
