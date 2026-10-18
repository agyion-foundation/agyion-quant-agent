# QuantVault — Security Test Report

**Date:** 2026-10-17
**Contract:** `vault/contract/QuantVault.sol`
**Method:** 19 adversarial scenarios against a real in-process EVM (pyevm)
**Result:** 19/19 PASS

## Scope

These tests verify one claim: **the agent key can never take user funds out of the vault**, even if the agent machine is fully compromised. Every test calls the real compiled contract through a real EVM — no mocks, no state shortcuts.

## Tested attack vectors

| # | Attack | Contract answer | Status |
|---|--------|-----------------|--------|
| 1 | Agent calls `withdraw` directly | reverts — insufficient shares (agent has none) | PASS |
| 2 | Agent calls `executeTrade` on the asset contract | reverts — cannot trade the vault asset | PASS |
| 3 | Agent calls `executeTrade` on a non-whitelisted target | reverts — target not whitelisted | PASS |
| 4 | Agent exceeds the per-trade daily limit | reverts — exceeds daily limit | PASS |
| 5 | Agent exceeds the cumulative daily limit | reverts on the trade that crosses the cap | PASS |
| 6 | Agent front-runs the timelock | reverts — trade still timelocked | PASS |
| 7 | Admin vetoes a queued trade | cancelled trade can never be executed | PASS |
| 8 | Random caller executes a queued trade | reverts — caller is not the agent | PASS |
| 9 | Random caller withdraws a user's shares | reverts — insufficient shares | PASS |
| 10 | Guardian pauses during an attack | queued trades frozen; executeTrade reverts | PASS |
| 11 | Double-execution of a trade | second execute reverts | PASS |
| 12 | Admin emergency-withdraws while paused | all user funds recovered to admin | PASS |
| 13 | Agent tries to call admin-only functions | reverts (veto, pause, unpause, whitelist mgmt) | PASS |
| 14 | Zero-share mint via empty deposit | reverts — first deposit too small | PASS |

## Honest user-flow checks (must NOT break)

| # | Flow | Status |
|---|------|--------|
| 15 | Deposit, queue, wait, execute a whitelisted swap | PASS |
| 16 | User withdraws while vault is paused | PASS |
| 17 | Two users deposit/withdraw; shares track correctly | PASS |
| 18 | Daily limit resets after 24h | PASS |
| 19 | Agent executes while paused → reverts | PASS |

## Known limitations (accepted for testnet demo)

- **Parameter-level risk**: within a whitelisted selector the agent may still craft bad trade parameters (e.g. swap into a worthless token). Bounded by the daily USD limit and the veto window. Documented in DEPLOY.md.
- **Single guardian key**: admin = guardian = owner. Mainnet requires multisig + independent audit.
- **Whitelist granularity**: selector-level, not parameter-level. Parameter-level policies are roadmap (Phase 3.5).

## Reproduce

```bash
cd quant-agent
python3 vault/tests/test_quant_vault.py
# expected: "RESULTS: 19/19 passed" + tests/last_results.json
```

Machine-readable output: `vault/tests/last_results.json`
