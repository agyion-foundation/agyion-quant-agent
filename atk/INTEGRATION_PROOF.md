# ATK Entegrasyon Kanıtı (Jüri Sayfası)

**ATK**: OKX Agent Trade Kit — `@okx_ai/okx-trade-mcp` v1.4.8
(npm, MIT) / GitHub `okx/agent-trade-kit`. MCP (stdio) üzerinden OKX CEX V5
API'sini açar: 167 tool, 11 modül. Bu projede **gerçek MCP protokolüyle**
kullanıldı — REST mock'u değil, ATK'nın kendisi spawn edilip `tools/call`
ile konuşuldu.

## 1) Kullandığımız ATK tool'ları (modül:tool)

`tools/list` çıktısından doğrulandı (server: `okx-trade-mcp 1.4.8`,
`--modules market,spot,account` → 50 tool görünür; biz 4'ünü kullanıyoruz):

| Modül   | Tool                    | OKX V5 endpoint                | Kullanım |
|---------|-------------------------|--------------------------------|----------|
| market  | `market_get_ticker`     | `GET /api/v5/market/ticker`    | BTC-USDT son fiyat — engine sinyalinin fiyat doğrulaması |
| spot    | `spot_place_order`      | `POST /api/v5/trade/order`     | Demo market emir (composite > +0.5 → buy, < -0.5 → sell) |
| account | `account_get_positions` | `GET /api/v5/account/positions`| Pozisyon teyidi |
| account | `account_get_balance`   | `GET /api/v5/account/balance`  | Demo bakiye kontrolü |

Input şemaları ATK'dan canlı okundu, ör. `spot_place_order`:
`{instId, tdMode: cash|cross|isolated, side: buy|sell, ordType: market|limit|...,
sz, tgtCcy: base_ccy|quote_ccy, clOrdId, px?}`.

## 2) Akış diyagramı

```
engine (Python)                         atk/ (bu modül)                     OKX
─────────────────                       ──────────────────                  ─────────
fetchers + normalize
      │
      ▼
data/snapshot.json ──okur──► signal_to_trade.py
 {composite, signal,             │ composite > +0.5 → BUY planı
  components[24]}                │ composite < -0.5 → SELL planı
                                 │ aksi → NÖTR (emir yok)
                                 ▼
                          atk_bridge.AtkBridge
                                 │ spawn: npx @okx_ai/okx-trade-mcp
                                 │        --profile demo --modules market,spot,account
                                 ▼  JSON-RPC (MCP stdio)
                          tools/call market_get_ticker ──────────► GET /api/v5/market/ticker (GERÇEK ✅)
                          tools/call spot_place_order ───────────► POST /api/v5/trade/order (demo profili)
                          tools/call account_get_positions ──────► GET /api/v5/account/positions
                                 │
                                 ▼
                    demo_log.json + logs/decisions.jsonl
                    (imza, beklenen yanıt, gerçek yanıt)
```

Fallback zinciri: MCP → OKX V5 REST (HMAC, `x-simulated-trading: 1`) →
mock (ağ kapalıysa, `simulated: true`). Sandbox'ta OKX REST doğrudan kapalıydı
fakat ATK'nın kendi HTTP katmanı (undici) üzerinden market verisi **gerçek**
döndü — transport her kayıtta işaretli.

## 3) Demo log özeti (`demo_log.json`, gerçek koşu 2026-10-17)

| Çağrı | Transport | Sonuç |
|---|---|---|
| `market_get_ticker BTC-USDT` | mcp | ✅ gerçek fiyat: **81,474.9 / 81,479.2 USDT** (2 ayrı koşu) |
| `spot_place_order buy 10 USDT` | mcp | ⚠️ ATK `AuthenticationError` (anahtarsız sandbox) — beklenen davranış; imza + beklenen başarı yanıtı (`sCode:"0"`, `ordId`) logda |
| `account_get_positions` | mcp | ⚠️ ATK `AuthenticationError` (AUTH_MISSING) |
| `account_get_balance` | mcp | ⚠️ ATK `AuthenticationError` (AUTH_MISSING) |

Fiyat çapraz kontrolü (`signal_to_trade.py` çıktısı):
ATK 81,479.2 vs engine/CryptoQuant 81,506.0 → sapma **%0.033** (tolerans %2) ✅

Sinyal kararı: composite **-0.0052** → NÖTR → emir planlanmadı (kurala uygun);
BUY/SELL plan üretimi birim testiyle doğrulandı (composite ±0.6/∓0.7 senaryosu).

## 4) Production'a geçiş

```bash
# OKX demo-trading (paper) anahtarları — https://www.okx.com → Demo Trading → API
export OKX_API_KEY=... OKX_SECRET_KEY=... OKX_PASSPHRASE=... OKX_DEMO=1
python3 signal_to_trade.py     # demo emirler gerçek simulated-trading'e düşer

# Canlı: ~/.okx/config.toml [profiles.live] + ATK --profile live
# (öneri: canlıda önce --read-only ile doğrulama)
```

Anahtarlar yalnızca env/config'den okunur; repoda secret yok.
