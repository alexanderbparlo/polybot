/**
 * Polymarket Scalper Terminal — Bloomberg-style dark trading dashboard
 *
 * Tabs:
 *   1. Live    — real-time PnL curve, open orders, session stats, circuit breaker
 *   2. Trades  — full trade history table
 *   3. Backtest — parameter sweep results, per-market batch results
 *   4. System  — active config, event log, Telegram status
 *
 * Data: currently wired to mock/static data.
 * TODO: connect to FastAPI WebSocket bridge for live data:
 *   const ws = new WebSocket("ws://localhost:8000/ws");
 *   ws.onmessage = (e) => dispatch(JSON.parse(e.data));
 */

import React, { useState, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from "recharts";

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
const C = {
  bg: "#0a0a0a",
  panel: "#111111",
  border: "#222222",
  amber: "#ffb300",
  green: "#00e676",
  red: "#ff1744",
  blue: "#40c4ff",
  dim: "#555555",
  text: "#e0e0e0",
  muted: "#888888",
};

const s = {
  app: {
    background: C.bg, minHeight: "100vh", padding: "0",
    fontFamily: '"IBM Plex Mono", monospace', color: C.text,
  },
  header: {
    borderBottom: `1px solid ${C.border}`,
    padding: "10px 20px",
    display: "flex", alignItems: "center", gap: "20px",
    background: C.panel,
  },
  logo: { color: C.amber, fontSize: "15px", fontWeight: 600, letterSpacing: "2px" },
  modeBadge: (paper) => ({
    fontSize: "10px", fontWeight: 600, padding: "2px 8px",
    border: `1px solid ${paper ? C.amber : C.red}`,
    color: paper ? C.amber : C.red,
    borderRadius: "2px",
  }),
  tabBar: {
    display: "flex", borderBottom: `1px solid ${C.border}`,
    background: C.panel, padding: "0 20px",
  },
  tab: (active) => ({
    padding: "10px 18px", cursor: "pointer", fontSize: "11px",
    color: active ? C.amber : C.muted,
    borderBottom: active ? `2px solid ${C.amber}` : "2px solid transparent",
    letterSpacing: "1px",
    textTransform: "uppercase",
  }),
  content: { padding: "20px" },
  panel: {
    background: C.panel, border: `1px solid ${C.border}`,
    borderRadius: "2px", padding: "14px", marginBottom: "14px",
  },
  panelTitle: {
    fontSize: "10px", color: C.amber, letterSpacing: "2px",
    textTransform: "uppercase", marginBottom: "12px",
    borderBottom: `1px solid ${C.border}`, paddingBottom: "6px",
  },
  statGrid: { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "12px" },
  statBox: {
    background: C.bg, border: `1px solid ${C.border}`,
    padding: "10px 14px", borderRadius: "2px",
  },
  statLabel: { fontSize: "9px", color: C.muted, letterSpacing: "1px", textTransform: "uppercase" },
  statValue: (color) => ({ fontSize: "18px", color: color || C.green, fontWeight: 600, marginTop: "4px" }),
  table: { width: "100%", borderCollapse: "collapse", fontSize: "11px" },
  th: {
    textAlign: "left", color: C.amber, fontSize: "9px", letterSpacing: "1px",
    textTransform: "uppercase", padding: "6px 10px",
    borderBottom: `1px solid ${C.border}`,
  },
  td: {
    padding: "7px 10px", borderBottom: `1px solid ${C.border}`,
    color: C.text, verticalAlign: "middle",
  },
  pnl: (v) => ({ color: v >= 0 ? C.green : C.red, fontWeight: 600 }),
  cancelBtn: {
    fontSize: "10px", padding: "2px 8px", cursor: "pointer",
    background: "transparent", border: `1px solid ${C.red}`, color: C.red,
    borderRadius: "2px",
  },
  circuit: (open) => ({
    display: "inline-block", padding: "3px 10px", fontSize: "10px",
    border: `1px solid ${open ? C.red : C.green}`,
    color: open ? C.red : C.green, borderRadius: "2px",
  }),
  configRow: {
    display: "grid", gridTemplateColumns: "220px 1fr",
    padding: "5px 0", borderBottom: `1px solid ${C.border}`,
  },
  configKey: { color: C.muted, fontSize: "11px" },
  configVal: { color: C.amber, fontSize: "11px" },
  eventRow: (lvl) => ({
    display: "flex", gap: "14px", padding: "4px 0",
    borderBottom: `1px solid ${C.border}`,
    color: lvl === "WARNING" ? C.amber : lvl === "ERROR" ? C.red : C.text,
    fontSize: "11px",
  }),
};

// ---------------------------------------------------------------------------
// Mock data
// ---------------------------------------------------------------------------

const PAPER = true;

const mockPnlCurve = Array.from({ length: 60 }, (_, i) => ({
  t: `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 === 0 ? "00" : "30"}`,
  pnl: parseFloat((Math.sin(i * 0.3) * 3 + i * 0.05 - 0.5).toFixed(3)),
}));

const mockOpenOrders = [
  { id: "PAPER_a1b2c3", market: "Will BTC exceed $100k by June 2025?", side: "BUY", price: 0.312, size: 32.05, status: "LIVE" },
  { id: "PAPER_d4e5f6", market: "Will Fed cut rates in May 2025?", side: "BUY", price: 0.448, size: 22.32, status: "LIVE" },
];

const mockTrades = [
  { id: 1, market: "Will ETH flip BTC in 2025?", side: "BUY", entry: 0.220, exit: 0.245, size: 45.5, net_pnl: 0.892, fees: 0.218, hold: 142, reason: "target" },
  { id: 2, market: "Will BTC exceed $100k by June 2025?", side: "BUY", entry: 0.380, exit: 0.347, size: 26.3, net_pnl: -1.237, fees: 0.194, hold: 301, reason: "stop" },
  { id: 3, market: "Will Fed cut rates in May 2025?", side: "BUY", entry: 0.412, exit: 0.439, size: 24.3, net_pnl: 0.453, fees: 0.203, hold: 88, reason: "target" },
  { id: 4, market: "Will Ukraine ceasefire happen by Q3?", side: "BUY", entry: 0.551, exit: 0.578, size: 18.1, net_pnl: 0.288, fees: 0.190, hold: 214, reason: "target" },
  { id: 5, market: "Will Tesla stock hit $400 in 2025?", side: "BUY", entry: 0.290, exit: 0.260, size: 34.5, net_pnl: -1.451, fees: 0.193, hold: 300, reason: "time" },
];

const mockSweepResults = [
  { spread: 4.0, target: 2.5, stop: 3.5, trades: 184, wr: "61.4%", pnl: "$12.44", dd: "$4.21" },
  { spread: 4.0, target: 3.0, stop: 3.5, trades: 142, wr: "58.5%", pnl: "$9.82",  dd: "$3.88" },
  { spread: 5.0, target: 2.5, stop: 3.0, trades: 97,  wr: "65.0%", pnl: "$8.11",  dd: "$2.14" },
  { spread: 3.5, target: 2.5, stop: 3.5, trades: 221, wr: "57.1%", pnl: "$6.93",  dd: "$6.44" },
  { spread: 4.0, target: 2.0, stop: 3.5, trades: 209, wr: "55.0%", pnl: "$4.22",  dd: "$5.10" },
];

const mockConfig = {
  min_spread_cents: 4.0,
  min_volume_24h: 10000,
  min_book_depth: 50,
  max_markets_watched: 20,
  entry_offset_cents: 0.5,
  target_profit_cents: 2.5,
  stop_loss_cents: 3.5,
  max_hold_seconds: 300,
  bankroll_pct_per_trade: "3%",
  max_position_usd: 100,
  max_open_positions: 3,
  daily_loss_limit_usd: 50,
  max_consecutive_losses: 4,
  weight_spread: 0.35,
  weight_momentum: 0.45,
  weight_liquidity: 0.20,
  fee_rate: "2%",
};

const mockEvents = [
  { ts: "09:42:11", level: "INFO",    msg: "Bot started — paper mode", detail: "watching 18 markets" },
  { ts: "09:42:14", level: "INFO",    msg: "Signal on BTC >$100k market", detail: "composite=0.71 spread=5.2¢" },
  { ts: "09:42:15", level: "INFO",    msg: "[PAPER] Order placed", detail: "BUY 32.05 @ 0.312" },
  { ts: "09:44:03", level: "INFO",    msg: "Trade closed: target", detail: "pnl=+$0.892" },
  { ts: "09:47:18", level: "WARNING", msg: "Low book depth on ETH flip market", detail: "depth=38 (min=50)" },
  { ts: "09:51:02", level: "INFO",    msg: "[PAPER] Order placed", detail: "BUY 22.32 @ 0.448" },
];

// ---------------------------------------------------------------------------
// Tab: Live
// ---------------------------------------------------------------------------
function LiveTab() {
  const sessionPnl = mockTrades.reduce((a, t) => a + t.net_pnl, 0);
  const wins = mockTrades.filter((t) => t.net_pnl > 0).length;
  const [circuitOpen] = useState(false);

  return (
    <div>
      {/* Session stats */}
      <div style={s.panel}>
        <div style={s.panelTitle}>Session Stats</div>
        <div style={s.statGrid}>
          <StatBox label="Session PnL" value={`$${sessionPnl.toFixed(3)}`} color={sessionPnl >= 0 ? C.green : C.red} />
          <StatBox label="Trades" value={mockTrades.length} />
          <StatBox label="Win Rate" value={`${((wins / mockTrades.length) * 100).toFixed(1)}%`} color={C.blue} />
          <StatBox label="Open Positions" value={mockOpenOrders.length} color={C.amber} />
        </div>
      </div>

      {/* Mode + circuit breaker */}
      <div style={{ ...s.panel, display: "flex", alignItems: "center", gap: "16px" }}>
        <span style={s.modeBadge(PAPER)}>{PAPER ? "PAPER MODE" : "LIVE MODE"}</span>
        <span style={s.circuit(circuitOpen)}>
          CIRCUIT {circuitOpen ? "OPEN — HALTED" : "CLOSED — ACTIVE"}
        </span>
        {PAPER && (
          <span style={{ fontSize: "10px", color: C.muted }}>
            No real orders placed. Set POLY_PAPER_MODE=false + --live flag to trade live.
          </span>
        )}
      </div>

      {/* PnL curve */}
      <div style={s.panel}>
        <div style={s.panelTitle}>Cumulative PnL (session)</div>
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={mockPnlCurve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="2 4" stroke={C.border} />
            <XAxis dataKey="t" tick={{ fill: C.dim, fontSize: 9 }} interval={9} />
            <YAxis tick={{ fill: C.dim, fontSize: 9 }} />
            <Tooltip
              contentStyle={{ background: C.panel, border: `1px solid ${C.border}`, fontSize: 11 }}
              labelStyle={{ color: C.amber }}
            />
            <ReferenceLine y={0} stroke={C.dim} strokeDasharray="3 3" />
            <Line type="monotone" dataKey="pnl" stroke={C.green} dot={false} strokeWidth={1.5} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Open orders */}
      <div style={s.panel}>
        <div style={s.panelTitle}>Open Orders</div>
        {mockOpenOrders.length === 0 ? (
          <div style={{ color: C.muted, fontSize: "11px" }}>No open orders</div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>
                {["Order ID", "Market", "Side", "Price", "Size", "Status", ""].map((h) => (
                  <th key={h} style={s.th}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {mockOpenOrders.map((o) => (
                <tr key={o.id}>
                  <td style={s.td}><code style={{ color: C.dim }}>{o.id}</code></td>
                  <td style={{ ...s.td, maxWidth: "260px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.market}</td>
                  <td style={{ ...s.td, color: o.side === "BUY" ? C.green : C.red }}>{o.side}</td>
                  <td style={s.td}>{o.price.toFixed(3)}</td>
                  <td style={s.td}>{o.size.toFixed(2)}</td>
                  <td style={{ ...s.td, color: C.amber }}>{o.status}</td>
                  <td style={s.td}>
                    <button style={s.cancelBtn} onClick={() => alert(`Cancel ${o.id} — wire to API`)}>✕</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Trades
// ---------------------------------------------------------------------------
function TradesTab() {
  return (
    <div style={s.panel}>
      <div style={s.panelTitle}>Trade History</div>
      <table style={s.table}>
        <thead>
          <tr>
            {["Market", "Side", "Entry", "Exit", "Size", "Net PnL", "Fees", "Hold", "Reason"].map((h) => (
              <th key={h} style={s.th}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {mockTrades.map((t) => (
            <tr key={t.id}>
              <td style={{ ...s.td, maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {t.market}
              </td>
              <td style={{ ...s.td, color: t.side === "BUY" ? C.green : C.red }}>{t.side}</td>
              <td style={s.td}>{t.entry.toFixed(3)}</td>
              <td style={s.td}>{t.exit.toFixed(3)}</td>
              <td style={s.td}>{t.size.toFixed(2)}</td>
              <td style={{ ...s.td, ...s.pnl(t.net_pnl) }}>${t.net_pnl.toFixed(3)}</td>
              <td style={{ ...s.td, color: C.muted }}>${t.fees.toFixed(3)}</td>
              <td style={s.td}>{t.hold}s</td>
              <td style={{ ...s.td, color: t.reason === "target" ? C.green : t.reason === "stop" ? C.red : C.amber }}>
                {t.reason}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Backtest
// ---------------------------------------------------------------------------
function BacktestTab() {
  return (
    <div>
      <div style={s.panel}>
        <div style={s.panelTitle}>Parameter Sweep — Top Results</div>
        <table style={s.table}>
          <thead>
            <tr>
              {["Min Spread (¢)", "Target (¢)", "Stop (¢)", "Trades", "Win Rate", "Net PnL", "Max DD"].map((h) => (
                <th key={h} style={s.th}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {mockSweepResults.map((r, i) => (
              <tr key={i} style={{ background: i === 0 ? "#1a1400" : "transparent" }}>
                <td style={s.td}>{r.spread}</td>
                <td style={s.td}>{r.target}</td>
                <td style={s.td}>{r.stop}</td>
                <td style={s.td}>{r.trades}</td>
                <td style={{ ...s.td, color: C.green }}>{r.wr}</td>
                <td style={{ ...s.td, color: C.green }}>{r.pnl}</td>
                <td style={{ ...s.td, color: C.red }}>{r.dd}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ marginTop: "10px", fontSize: "10px", color: C.muted }}>
          ★ Highlighted row = best net PnL. Run: <code>python backtest.py --sweep --token &lt;token_id&gt;</code>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: System
// ---------------------------------------------------------------------------
function SystemTab() {
  const tgStatus = true; // mock

  return (
    <div>
      <div style={s.panel}>
        <div style={s.panelTitle}>Active ScalperConfig</div>
        {Object.entries(mockConfig).map(([k, v]) => (
          <div key={k} style={s.configRow}>
            <span style={s.configKey}>{k}</span>
            <span style={s.configVal}>{String(v)}</span>
          </div>
        ))}
      </div>

      <div style={s.panel}>
        <div style={s.panelTitle}>Telegram Status</div>
        <span style={s.circuit(!tgStatus)}>
          {tgStatus ? "● CONNECTED" : "○ DISCONNECTED"}
        </span>
        <span style={{ fontSize: "10px", color: C.muted, marginLeft: "12px" }}>
          {tgStatus ? "Alerts active" : "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env"}
        </span>
      </div>

      <div style={s.panel}>
        <div style={s.panelTitle}>Event Log</div>
        {mockEvents.map((e, i) => (
          <div key={i} style={s.eventRow(e.level)}>
            <span style={{ color: C.dim, minWidth: "68px" }}>{e.ts}</span>
            <span style={{ minWidth: "72px", color: e.level === "WARNING" ? C.amber : e.level === "ERROR" ? C.red : C.blue }}>
              {e.level}
            </span>
            <span>{e.msg}</span>
            {e.detail && <span style={{ color: C.muted }}>— {e.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reusable stat box
// ---------------------------------------------------------------------------
function StatBox({ label, value, color }) {
  return (
    <div style={s.statBox}>
      <div style={s.statLabel}>{label}</div>
      <div style={s.statValue(color)}>{value}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------
const TABS = ["Live", "Trades", "Backtest", "System"];

export default function Dashboard() {
  const [tab, setTab] = useState("Live");

  return (
    <div style={s.app}>
      {/* Header */}
      <div style={s.header}>
        <span style={s.logo}>POLYMARKET SCALPER</span>
        <span style={s.modeBadge(PAPER)}>{PAPER ? "PAPER" : "LIVE"}</span>
        <span style={{ marginLeft: "auto", fontSize: "10px", color: C.dim }}>
          {/* TODO: wire to FastAPI WebSocket: ws://localhost:8000/ws */}
          MOCK DATA — connect FastAPI bridge for live feed
        </span>
      </div>

      {/* Tab bar */}
      <div style={s.tabBar}>
        {TABS.map((t) => (
          <div key={t} style={s.tab(tab === t)} onClick={() => setTab(t)}>
            {t}
          </div>
        ))}
      </div>

      {/* Content */}
      <div style={s.content}>
        {tab === "Live"     && <LiveTab />}
        {tab === "Trades"   && <TradesTab />}
        {tab === "Backtest" && <BacktestTab />}
        {tab === "System"   && <SystemTab />}
      </div>
    </div>
  );
}
