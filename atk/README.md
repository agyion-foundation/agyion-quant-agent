# atk/ — OKX Agent Trade Kit integration

## What this is
A thin, honest bridge between the quant engine's signal and OKX's Agent Trade Kit
(`@okx_ai/okx-trade-mcp`). The engine produces a signal; this package turns it into
a trade plan and executes it through ATK — over real MCP stdio transport when
available, with clearly-marked fallbacks when not.

## Files
- `atk_bridge.py` — MCP stdio client (JSON-RPC): spawn, initialize, tools/list, tools/call.
  Fallback chain: **MCP → OKX V5 REST (HMAC, `x-simulated-trading`) → mock**.
  Every response is tagged with the transport actually used.
- `signal_to_trade.py` — maps snapshot signal → trade plan (composite thresholds,
  logistic sizing, same policy as the vault — see `docs/POLICY.md`).
- `demo_log.json` — the recorded demo run (2026-10-17).
- `INTEGRATION_PROOF.md` — what was proven, with numbers and boundaries.

## Run
```bash
python3 atk/signal_to_trade.py            # reads engine/data/snapshot.json, prints plan
python3 atk/atk_bridge.py                 # spawns MCP server, lists tools, gets ticker
```

## Security posture
No API keys in the repo. Authenticated calls fail closed (`AUTH_MISSING`) without
credentials. Simulated responses are always marked `simulated: true` and never
presented as live fills.
