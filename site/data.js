/* ============================================================
   AGYION QUANT — embedded data bundle
   Source: quant-agent/data/snapshot.json (2026-10-17T14:55:27Z)
           quant-agent/vault/logs/agent_decisions.jsonl
           quant-agent/atk/logs/decisions.jsonl
           quant-agent/vault/deployment.json (Fuji, deployed)
   Embedded statically so the site works over file:// with no fetch.
   ============================================================ */

window.AGYION_DATA = {
  version: "0.1",
  generated_at: "2026-10-17T14:55:27+00:00",
  weights_version: "v1",

  composite: 0.0399,
  long_term_index: 0.0767,
  short_term_index: 0.0032,
  regime_index: -0.6035,
  signal: "NEUTRAL",
  signal_rule: "Schmitt hysteresis (POLICY §1): enter > +0.50 BUY / < -0.50 SELL; exit at ±0.40 — inside the band the previous label holds  (composite = 0.5*LT + 0.5*ST)",

  btc_price: 81506.0,
  atk_price: 81479.2,
  crosscheck_deviation_pct: 0.033,

  layers: {
    long_term: {
      index: 0.0767,
      band: "NEUTRAL",
      components: [
        { name: "BTC vs 200-Day Average",      raw: "0.1264",  score: -0.0941, weight: 0.1493, impact: -0.014,  note: "BTC +12.6% above 200DMA ($81,506 vs $72,363)" },
        { name: "Avalanche TVL Momentum (30d)",raw: "0.2323",  score: 1.0,     weight: 0.0597, impact: 0.0597, note: "AVAX TVL +23.2% in 30d ($480M -> $591M)" },
        { name: "Stablecoin Supply Mom. (30d)",raw: "-0.0317", score: -0.3169, weight: 0.0597, impact: -0.0189, note: "AVAX stablecoin supply -3.2% in 30d" },
        { name: "BTC MVRV-Z",                  raw: "2.1902",  score: -0.7987, weight: 0.1642, impact: -0.1311, note: "MVRV-Z 2.19 — overvaluation zone" },
        { name: "BTC NUPL",                    raw: "0.3391",  score: 0.0957,  weight: 0.1343, impact: 0.0129,  note: "NUPL 0.339 — moderate profit" },
        { name: "BTC Bull-Bear Cycle (proxy)", raw: "0.1198",  score: 0.2396,  weight: 0.1045, impact: 0.025,   note: "BCI proxy +0.120 — bull-phase trend" },
        { name: "BTC vs Realized Price",       raw: "0.5131",  score: 0.2669,  weight: 0.0896, impact: 0.0239,  note: "+51.3% above realized price (~$53,865)" },
        { name: "BTC Growth Rate Gap (30d)",   raw: "0.0596",  score: 0.3975,  weight: 0.1194, impact: 0.0475,  note: "Mcap minus realized-cap growth +6.0% — speculative inflow" },
        { name: "BTC Hash Ribbon",             raw: "0.0301",  score: 0.6019,  weight: 0.1194, impact: 0.0719,  note: "Hashrate 30d avg +3.0% above 60d — miner recovery" }
      ]
    },
    short_term: {
      index: 0.0032,
      band: "NEUTRAL",
      components: [
        { name: "BTC Funding Rate",            raw: "0.00035", score: 0.6925,  weight: 0.1446, impact: 0.100,  note: "+0.0346%/8h — longs crowding" },
        { name: "ETH Funding Rate",            raw: "0.00031", score: 0.628,   weight: 0.0843, impact: 0.053,  note: "+0.0314%/8h — longs crowding" },
        { name: "BTC Open Interest Chg (24h)", raw: "-0.0079", score: -0.0393, weight: 0.1084, impact: -0.004, note: "OI -0.8% ($25.20B -> $25.00B)" },
        { name: "ETH Open Interest Chg (24h)", raw: "-0.0205", score: -0.1024, weight: 0.0602, impact: -0.006, note: "OI -2.0% ($12.35B -> $12.10B)" },
        { name: "Price Momentum (24h)",        raw: "-0.0077", score: -0.0768, weight: 0.0723, impact: -0.006, note: "BTC/ETH avg 24h change -0.8%" },
        { name: "BTC Liquidation Skew (24h)",  raw: "0.4811",  score: -0.4811, weight: 0.0964, impact: -0.046, note: "long $23M vs short $8M — long flush dominant" },
        { name: "ETH Liquidation Skew (24h)",  raw: "0.5250",  score: -0.525,  weight: 0.0482, impact: -0.025, note: "long $32M vs short $10M — long flush dominant" },
        { name: "TRX Momentum (7d)",           raw: "0.0105",  score: 0.042,   weight: 0.0482, impact: 0.002,  note: "TRX 7d +1.0% (close $0.3351)" },
        { name: "XRP Funding Rate",            raw: "0.00020", score: 0.4075,  weight: 0.0241, impact: 0.010,  note: "+0.0204%/8h — longs crowding" },
        { name: "XRP Open Interest Chg (24h)", raw: "-0.0080", score: -0.04,   weight: 0.0181, impact: -0.001, note: "OI -0.8% ($885M -> $878M)" },
        { name: "XRP Liquidation Skew (24h)",  raw: "0.8982",  score: -0.8982, weight: 0.0181, impact: -0.016, note: "long $4M vs short $0M — long flush dominant" },
        { name: "Fear & Greed Index",          raw: "55",      score: 0.1,     weight: 0.0723, impact: 0.007,  note: "F&G 55/100 (Greed)" },
        { name: "Coinbase Premium Index",      raw: "-0.1450", score: -0.4835, weight: 0.0843, impact: -0.041, note: "-0.15% — US spot selling / discount" },
        { name: "Korea Premium (Kimchi)",      raw: "0.97",    score: 0.194,   weight: 0.0602, impact: 0.012,  note: "+0.97% — Korean retail demand strong" },
        { name: "ETH/BTC OI Ratio",            raw: "-0.1171", score: -0.5854, weight: 0.0602, impact: -0.035, note: "0.478 vs 90d avg 0.541 — leverage rotating to BTC" }
      ]
    },
    regime: {
      index: -0.6035,
      band: "RISK-OFF",
      components: [
        { name: "US Dollar Index Mom. (30d)",  raw: "0.0274",  score: -0.9136, weight: 0.34,   impact: -0.311, note: "Broad USD +2.7% (121.6) — risk-off" },
        { name: "VIX Momentum (30d)",          raw: "2.14",    score: -0.2675, weight: 0.33,   impact: -0.088, note: "VIX +2.1 pts (16.5) — fear rising" },
        { name: "US 10Y Yield Mom. (30d)",     raw: "0.31",    score: -0.62,   weight: 0.33,   impact: -0.205, note: "10Y +0.31 pts (5.10) — tightening" }
      ]
    }
  },

  /* ---- What the agent did — real decision log records ---- */
  decisions: [
    {
      ts: "2026-10-17T14:18:21Z",
      source: "atk",
      line: "ATK mcp tools/call: market_get_ticker + account_get_positions executed. BTC=81,479.2 USDT (live, transport=mcp). Cross-check vs engine 81,506.0 -> dev 0.033% OK"
    },
    {
      ts: "2026-10-17T14:27:04Z",
      source: "vault",
      line: "PROPOSE id=20261017T142704Z-be3a2a  target_exposure=0.5043 current=0.0 delta=+0.5043  logistic(k=4.0) base=0.504  notional=$504,299,893 (dry-run)"
    },
    {
      ts: "2026-10-17T14:42:37Z",
      source: "vault",
      line: "PROPOSE id=20261017T144237Z-dca29c  target_exposure=0.5043 delta=+0.5043  awaiting approval (no approval file; run --wait-approval)"
    },
    {
      ts: "2026-10-17T14:55:27Z",
      source: "engine",
      line: "RUN weights=v1 -> composite=+0.0399  LT=+0.0767 ST=+0.0032 REGIME=-0.6035 => NEUTRAL  (27 components live incl. hash ribbon)"
    },
    {
      ts: "2026-10-17T15:07:38Z",
      source: "vault",
      line: "HOLD composite=+0.0399 NEUTRAL (prev NEUTRAL, label unchanged) — NEUTRAL label -> no change, exposure kept at 0.000 [POLICY §2]"
    }
  ],

  /* ---- Vault / contract — DEPLOYED on Avalanche Fuji ---- */
  vault: {
    contract_name: "QuantVault.sol",
    network: "Avalanche Fuji (chainId 43113)",
    address: "0xd4D6fAcC84fC230b605522Bd21b9f2E82A01Fa50",
    explorer_url: "https://testnet.snowtrace.io/address/0xd4D6fAcC84fC230b605522Bd21b9f2E82A01Fa50",
    e2e_trade_tx: "https://testnet.snowtrace.io/tx/0x7a98e1ba648dde950afa53d44c62f2f91e043a28874af1b0a9e3af6e46e2760c",
    timelock: "900 s (15 min)",
    deposit: "2,000 tUSDC",
    tests_passed: 19,
    tests_total: 19
  },

  /* ---- ATK proof ---- */
  atk: {
    server: "okx-trade-mcp 1.4.8",
    tools_visible: 50,
    tools_used: ["market_get_ticker", "spot_place_order", "account_get_positions", "account_get_balance"],
    modules: "market, spot, account",
    transport: "MCP stdio (JSON-RPC) — real spawn, not a REST mock",
    crosscheck: "ATK 81,479.2 vs engine/CryptoQuant 81,506.0 -> deviation 0.033% (tolerance 2%)"
  },

  /* ---- Newsletter sample (excerpt of data/newsletter-2026-10-17.md) ---- */
  newsletter: {
    title: "Quant Agent Daily — 2026-10-17",
    signal: "NEUTRAL",
    lines: [
      "Long Term:   0.0767  ▰▰▰▰▰▱▱▱▱▱  NEUTRAL",
      "Short Term:  0.0032  ▰▰▰▰▰▱▱▱▱▱  NEUTRAL",
      "Macro Regime: -0.6035 ▰▰▱▱▱▱▱▱▱▱  risk-off",
      "Composite (0.5xLT + 0.5xST): 0.0399 -> NEUTRAL",
      "(Schmitt hysteresis: enter ±0.50, exit ±0.40)"
    ]
  }
};
