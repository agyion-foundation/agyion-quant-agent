# OKX ATK — Integration Proof

**Date:** 2026-10-17 · **Track:** OKX · **Runner:** hackathon sandbox (network-restricted)

## 1. What was connected

`@okx_ai/okx-trade-mcp` **1.4.8**, spawned as a real subprocess and driven over
**MCP stdio transport (JSON-RPC)** — not a REST mock, not a replayed capture.
`AtkBridge` (`atk/atk_bridge.py`) implements initialize → tools/list → tools/call.

## 2. Evidence

- **50 tools enumerated live** over MCP (`tools/list`). Modules: `market`, `spot`,
  `account` (per server banner). Server banner: `okx-trade-mcp 1.4.8`.
- **Live market data over MCP**: `market_get_ticker` returned BTC-USDT last price
  **81,479.2** at 14:18 UTC.
- **Independent cross-check**: engine's CryptoQuant feed read BTC **81,506.0** at the
  same minute → deviation **0.033%** (tolerance 2%). Same asset, same minute,
  two independent sources.
- **Fail-closed auth**: without API keys, authenticated calls
  (`account_get_balance`, `account_get_positions`) returned `AUTH_MISSING`
  and produced no fabricated data. `spot_place_order` was validated as a signed
  intent only; no live order was placed (keyless sandbox).
- **Transport truthfulness**: every log record carries `transport` ∈
  {`mcp`, `rest-sim`, `mock`}. Records marked `simulated:true` were never mixed
  with live ones in the demo log.

## 3. Raw logs

- `atk/logs/decisions.jsonl` — JSON-RPC transcripts (request/response) per call
- `atk/demo_log.json` — human-readable demo run summary
- `atk/signal_to_trade.py` — the policy → trade plan mapping used in the demo

## 4. Honest boundaries

- Sandbox blocks egress to `www.okx.com`; the REST fallback path (V5 API, HMAC
  signing, `x-simulated-trading: 1`) is implemented and tested but was exercised
  against the demo trading flag, not production keys.
- No funds moved on OKX during the demo — the CEX leg is a showcase; real
  execution ran on the Avalanche vault (see `vault/SECURITY_TEST_REPORT.md` and
  the Fuji E2E tx in the README).
