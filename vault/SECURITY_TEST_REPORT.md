# SECURITY_TEST_REPORT.md — QuantVault Güvenlik Test Raporu

**Tarih:** 2026-10-17 · **Kontrat:** `contract/QuantVault.sol` (Solidity 0.8.24)
**Yöntem:** `solc 0.8.24` ile derleme + **gerçek EVM** üzerinde yürütme (eth-tester / py-evm,
web3.py). Mock-EVM mantık simülasyonu DEĞİL — bytecode gerçek EVM'de çalıştırıldı.
**Test dosyası:** `tests/test_quant_vault.py` · **Sonuç:** **19/19 PASS**

## Pass/Fail Tablosu

| # | Senaryo | Beklenen | Sonuç |
|---|---------|----------|-------|
| a | Agent `withdraw(1)` dener (share'i yok) | revert `insufficient shares` — fon çekemez | ✅ PASS |
| b | Agent whitelist dışı target'a `queueTrade` | revert `target not whitelisted` | ✅ PASS |
| c1 | Tek işlemde `maxDailyNotional` aşımı | revert `daily limit exceeded` | ✅ PASS |
| c2 | Kümülatif günlük limit aşımı (kuyrukta bekleyen + yeni) | revert `daily limit exceeded` | ✅ PASS |
| d | Timelock dolmadan `executeTrade` | revert `timelock not elapsed` | ✅ PASS |
| e | Guardian `vetoTrade` → sonra execute | revert `vetoed`; veto limiti serbest bırakır | ✅ PASS |
| f1 | Pause'da `queueTrade` | revert `paused` | ✅ PASS |
| f2 | Pause'da `recordSignal` | revert `paused` | ✅ PASS |
| f3 | Pause'da `executeTrade` | revert `paused` | ✅ PASS |
| f4 | **Pause'da kullanıcı deposit/withdraw** | **çalışır (pause edilemez)** | ✅ PASS |
| g | Normal deposit → share mint → withdraw → tam iade | çalışır | ✅ PASS |
| h | Whitelist'li trade: kuyruk → timelock → execute | başarılı, `executed=true` | ✅ PASS |
| i | Whitelist'li target'ta whitelist'siz selector | revert `selector not whitelisted` | ✅ PASS |
| j | Agent asset (token) kontratını doğrudan çağırır | revert `cannot call asset` | ✅ PASS |
| k1 | Agent `pause()` çağırır | revert `not the owner` | ✅ PASS |
| k2 | Agent whitelist değiştirir | revert `not the owner` | ✅ PASS |
| k3 | Agent `setAgent`'i değiştirir | revert `not the owner` | ✅ PASS |
| l | `agent_loop.py` SWAP_SELECTOR sabiti = keccak("swap(uint256)")[:4] | eşit (`0x94b918de`) | ✅ PASS |
| m | `recordSignal` başarılı + `SignalRecorded` event'i (agent_loop akışıyla aynı) | event doğru alanlarla yayınlanır | ✅ PASS |

## Güvenlik modeli özeti

1. **Non-custodial çekirdek:** `deposit`/`withdraw` izinsizdir ve `pause`'dan etkilenmez.
   Kullanıcı (share sahibi) her durumda fonunu çekebilir (f4, g).
2. **Agent kafesi (least-privilege):**
   - Agent sadece `recordSignal`, `queueTrade`, `executeTrade` çağırabilir; owner
     fonksiyonlarına erişemez (k1–k3).
   - Trade hedefi `(target, selector)` çifti whitelist'te olmalı (b, i); asset kontratı
     hedef olamaz (j) → agent token'ın `transfer`/`approve` fonksiyonunu vault üzerinden
     çağıramaz.
   - Agent `withdraw` çağırabilir ama sadece kendi share'i kadar çeker; agent'a hiç
     share mint'lenmez → fonlara erişim yok (a).
3. **Timelock + veto:** her trade `timelockDelay` kadar kuyrukta bekler (d); guardian
   bu pencerede veto edebilir (e). Veto edilen notional günlük limite karşı serbest kalır.
4. **Günlük limit:** `spent + pending + yeni <= maxDailyNotional` kuyrukta kontrol edilir;
   execute anında kesin sayaç işlenir → çifte sayım yok, veto'lu trade limit yemez (c1, c2, h).
5. **ReentrancyGuard:** `deposit`, `withdraw`, `executeTrade` `nonReentrant`.
6. **Defter/kanıt:** zincirde `Deposited/Withdrawn/TradeQueued/TradeExecuted/TradeVetoed/
   SignalRecorded` event'leri; zincir dışında `logs/agent_decisions.jsonl`.

## Bilinen sınırlar (dürüstlük notu)
- `executeTrade` içindeki dış çağrı whitelist'li target'a gider; target'ın kendisi
  güvenilir sayılır (guardian whitelist'i yönetir). Kötü niyetli whitelist'li target
  vault bakiyesini riske atabilir → whitelist değişiklikleri guardian'ın sorumluluğundadır.
- Share hesabı `balanceOf` tabanlıdır; vault'a doğrudan token gönderimi (donation) share
  fiyatını şişirir. Testnet kapsamı için kabul edilebilir.
- `setAllowedCall(target, sel, false)` son selector kaldırıldığında target flag'i kalabilir;
  tam kaldırma için `removeTarget` kullanılır (dokümante edildi).

## Tekrar üretme
```bash
pip install web3 py-solc-x eth-tester py-evm
cd vault && python3 tests/test_quant_vault.py
```
