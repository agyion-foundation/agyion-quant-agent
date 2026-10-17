# ATK — OKX Agent Trade Kit Entegrasyonu

Quant Agent sinyal motorunun (`data/snapshot.json`) OKX tarafına bağlanan
resmi köprüsü. **ATK**, OKX'in açık kaynak MCP (Model Context Protocol)
sunucusudur: npm paketi `@okx_ai/okx-trade-mcp`, GitHub `okx/agent-trade-kit`.
167 tool / 11 modül (market, account, spot, swap, futures, earn, dca, dcd,
event, grid, system) sunar; CEX V5 API üzerine kuruludur ve **demo trading**
(simulated trading) profili içerir.

Bu klasör hackathon'un "ATK kullanımı" gereksinimini kanıtlar: köprü gerçek
MCP stdio protokolüyle ATK sürecini spawn eder, `tools/call` ile konuşur ve
tüm çağrıları `demo_log.json`'a kaydeder.

## Kurulum

```bash
# Seçenek A — global
npm install -g @okx_ai/okx-trade-mcp

# Seçenek B — npx ile (kurulumsuz; köprümüz bunu kullanır)
npx -y @okx_ai/okx-trade-mcp --profile demo --modules market,spot,account

# Seçenek C — kaynaktan
git clone https://github.com/okx/agent-trade-kit && cd agent-trade-kit && npm i
```

Kimlik bilgileri (`~/.okx/config.toml`):

```toml
default_profile = "demo"
[profiles.demo]
api_key = "your-demo-api-key"        # OKX demo trading anahtarı
secret_key = "your-demo-secret-key"
passphrase = "your-demo-passphrase"
demo = true                          # x-simulated-trading: 1
```

Alternatif olarak env: `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`,
`OKX_DEMO=1`. **Bu repo'da hiçbir anahtar saklanmaz** — sandbox koşusunda
anahtar yoktu; market modülü anahtarsız çalışır, emir/pozisyon çağrıları ATK'dan
gerçek `AuthenticationError` döndürür (bkz. `demo_log.json`).

## Kullandığımız modüller

| Modül    | ATK tool                | Bizim işlev            | Amaç |
|----------|-------------------------|------------------------|------|
| market   | `market_get_ticker`     | `get_price(instId)`    | Sinyal fiyatını OKX ile çapraz doğrulama (anahtar gerekmez) |
| spot     | `spot_place_order`      | `demo_market_order()`  | Demo (simulated) market emir — composite sinyalinin execution'ı |
| account  | `account_get_positions` | `get_positions()`      | İşlem sonrası pozisyon teyidi |
| account  | `account_get_balance`   | `get_balance()`        | Demo bakiye kontrolü |

## MCP client olarak bağlanma

### Claude Code / Claude Desktop (stdio config)

```json
{
  "mcpServers": {
    "okx-demo": {
      "command": "npx",
      "args": ["-y", "@okx_ai/okx-trade-mcp", "--profile", "demo",
               "--modules", "market,spot,account"]
    },
    "okx-live": {
      "command": "npx",
      "args": ["-y", "@okx_ai/okx-trade-mcp", "--profile", "live",
               "--modules", "all", "--read-only"]
    }
  }
}
```

### Kendi agent'ımız (Python → MCP stdio JSON-RPC)

`atk_bridge.py` içindeki `McpClient` node sürecini spawn eder ve satır-temelli
JSON-RPC konuşur (`initialize` → `notifications/initialized` → `tools/call`):

```python
from atk_bridge import AtkBridge
b = AtkBridge()
print(b.get_price("BTC-USDT")["price"])      # market_get_ticker (gerçek)
b.demo_market_order("BTC-USDT", "buy", "10") # spot_place_order (demo profili)
b.get_positions()                            # account_get_positions
b.close()
```

Transport önceliği: **MCP (node/npx) → OKX V5 REST (HMAC imzalı) → mock**
(ağ kapalıysa; sonuçlar `simulated: true` işaretli). Hangi transport'un
kullanıldığı her log kaydında `transport` alanında.

## Dosyalar

| Dosya | Rol |
|---|---|
| `atk_bridge.py` | MCP stdio client + REST fallback + mock; `demo_log.json` üretir |
| `signal_to_trade.py` | `snapshot.json` composite → ATK çağrı planı + execution |
| `demo_log.json` | Gerçek koşu kaydı: çağrı imzaları, beklenen yanıtlar, gerçek ATK yanıtları |
| `logs/decisions.jsonl` | Sinyal → karar kayıtları (append-only) |
| `logs/trade_plan_*.json` | Koşu başına plan + execution sonucu |
| `INTEGRATION_PROOF.md` | Jüri kanıt sayfası |

## Çalıştırma

```bash
cd atk
python3 atk_bridge.py --demo-run        # uçtan uca ATK demo akışı + log
python3 signal_to_trade.py              # snapshot → plan → ATK execution
python3 signal_to_trade.py --dry-run    # sadece plan
```
