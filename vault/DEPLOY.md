# DEPLOY.md — QuantVault'u 5 Dakikada Fuji Testnet'e Kur

## 0) Ön koşul
```bash
cd /mnt/agents/output/quant-agent/vault
pip install web3 py-solc-x   # bir kere
```

## 1) Test AVAX al (1 dk)
1. https://faucet.avax.network/ aç → **Avalanche Fuji (C-Chain)** ağını seç.
2. Kendi EVM adresini yapıştır → 2 test AVAX gelir (yeterli, deploy ~0.05 AVAX).

## 2) Private key hazırla (1 dk)
- Yeni bir **test** cüzdanı oluştur (Core/MetaMask, ağ: Fuji, chainId 43113).
- Private key'i **sadece env'e** koy — asla koda/dosyaya yazma:
```bash
export PRIVATE_KEY=0x<deployer_key>     # owner + guardian olacak
export AGENT_ADDRESS=0x<agent_adresi>   # opsiyonel; boşsa deployer=agent (demo)
```

## 3) Deploy (2 dk)
```bash
python3 deploy.py
```
Script sırayla: `TestToken` (tUSDC, 6 decimal) → `MockRouter` → `QuantVault` deploy eder,
`MockRouter.swap(uint256)` selector'ünü whitelist'ler, `deployment.json` ve
`contract/QuantVault.abi.json` üretir.

Opsiyonel env'ler: `TIMELOCK_SECONDS` (vars. 900), `MAX_DAILY_NOTIONAL` (vars. 10_000e6),
`ASSET_ADDRESS` (kendi Fuji USDC'n varsa; yoksa TestToken basılır), `RPC_URL`.

## 4) Vault'a para koy (30 sn)
Remix veya cast ile `TestToken.mint(adresin, 1000e6)` → `approve(vault, ...)` →
`vault.deposit(1000e6)`. (Fuji explorer: https://testnet.snowtrace.io/)

## 5) Agent'ı çalıştır (30 sn)
```bash
export VAULT_ADDRESS=<deploy çıktısındaki vault adresi>
export ROUTER_ADDRESS=<deploy çıktısındaki router adresi>
export AGENT_PRIVATE_KEY=0x<agent_key>   # AGENT_ADDRESS'in key'i
python3 agent_loop.py --once             # HITL: öneri + onay dosyası
python3 agent_loop.py --once --auto      # otomatik kuyruk
python3 agent_loop.py --interval 900     # sürekli döngü
```

## CANLI DEPLOYMENT (Fuji, 2026-10-17) ✅
Bu bölüm gerçekleşen deploy'u belgeler — yeniden deploy gerekmez.

| Rol | Adres |
|---|---|
| Guardian (owner) | `0xb422a80C774618F91Dc6540Fa7c914335e01C4aE` |
| Agent | `0x54E92e0b15d082E7023AE22525Be53E1E00674Ed` |
| **QuantVault** | [`0xd4D6fAcC84fC230b605522Bd21b9f2E82A01Fa50`](https://testnet.snowtrace.io/address/0xd4D6fAcC84fC230b605522Bd21b9f2E82A01Fa50) |
| TestToken (tUSDC) | [`0x69D8FbB1433e5f5203dC6a3Fb6F8EfB651E3669d`](https://testnet.snowtrace.io/address/0x69D8FbB1433e5f5203dC6a3Fb6F8EfB651E3669d) |
| MockRouter (whitelist'li) | [`0x2BD375f7d0082433e37bbF7183Be4340cAB62a48`](https://testnet.snowtrace.io/address/0x2BD375f7d0082433e37bbF7183Be4340cAB62a48) |

Parametreler: timelock 900 sn, maxDailyNotional 10.000 tUSDC. Vault'ta 2.000 tUSDC deposit var.
Zincirde kanıt: `SignalRecorded` (tx `0xb98777a7…ad0052`) + kuyrukta `TradeQueued` id=0
(tx `0x389def24…01be7b`, eta'dan sonra agent execute edebilir / guardian veto edebilir).
Tüm tx hash'ler `deployment.json` içinde. Agent'ı canlı çalıştırmak için:
```bash
export VAULT_ADDRESS=0xd4D6fAcC84fC230b605522Bd21b9f2E82A01Fa50
export ROUTER_ADDRESS=0x2BD375f7d0082433e37bbF7183Be4340cAB62a48
export AGENT_PRIVATE_KEY=<agent key — asla dosyaya yazma>
python3 agent_loop.py --interval 900 --auto   # veya --once (HITL)
```

## Güvenlik modeli (jüri özeti)
- **Non-custodial:** `deposit`/`withdraw` izinsiz ve pause edilemez; kullanıcı her zaman çıkabilir.
- **Agent kafesi:** agent sadece whitelist'li `(target, selector)` çağırabilir; asset kontratını
  doğrudan çağıramaz; `withdraw`'u çağırsa bile kendi share'i olmadığından fon çekemez.
- **Timelock + veto:** agent trade'i kuyruğa girer, `timelockDelay` dolmadan execute edilemez;
  guardian bu pencerede `vetoTrade(id)` ile iptal eder.
- **Günlük limit:** `maxDailyNotional` aşımı revert.
- **Pause:** guardian `pause()` → agent fonksiyonları durur; kullanıcı çekimi çalışmaya devam eder.
- **Defter:** her karar `logs/agent_decisions.jsonl` + zincirde `SignalRecorded` event'i.
