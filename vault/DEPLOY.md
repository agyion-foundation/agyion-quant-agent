# QuantVault — Deployment Guide

## Kontrat
`contract/QuantVault.sol` — 19/19 güvenlik testi geçti (bkz. SECURITY_TEST_REPORT.md).

## Kurallar (on-chain)
- `withdraw` / `redeem`: agent çağıramaz (yalnızca hak sahibi kullanıcı)
- `executeTrade`: yalnızca agent, yalnızca whitelist `(target, selector)` çiftleri,
  hedef kontrat asset kontratı olamaz, günlük USD limiti (başlangıç: $10,000/gün)
- Timelock: trade önce kuyruğa girer, `TIMELOCK` (15 dk) sonra execute edilebilir;
  admin kuyruktaki işlemi veto edebilir (cancel)
- Deposit/withdraw **pause'da da açık** kalır; yalnızca executeTrade durur
- Günlük limit admin tarafından değiştirilebilir (event loglanır)

## Fuji Testnet Deploy
```bash
cd quant-agent
pip install -r vault/requirements.txt   # web3, py-solc-x, eth-account, requests

# .env (quant-agent/.env — .gitignore'da, asla commit etme)
PRIVATE_KEY=0x...        # deployer; AVAX için https://faucet.avax.network
AGENT_ADDRESS=0x...      # agent ayrı EOA

python vault/deploy.py
# → vault/deployment.json + contract/QuantVault.abi.json
```

## Sonrası
1. Agent EOA'sını farklı bir private key ile fonla (0.1 AVAX yeter — gaz için)
2. `vault/agent_loop.py --network fuji` ile agent çalıştır
3. Snowtrace'te doğrula: https://testnet.snowtrace.io
4. Ek whitelist hedefi gerekirse: `setWhitelisted(target, selector, true)` (admin)

## Güvenlik Notu
Bu kontrat testnet demo'sudur. Mainnet öncesi bağımsız denetim (audit) şart.
Bilinen sınır: agent whitelist içindeki swap fonksiyonlarını kötü parametrelerle
çağırabilir (ör. değersiz token'a swap) — günlük limit bu hasarı sınırlar; veto
penceresi (15 dk timelock) admin'in müdahalesine izin verir.
