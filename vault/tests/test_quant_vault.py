#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QuantVault güvenlik testleri — gerçek EVM (eth-tester / py-evm) üzerinde.

Senaryolar (görev tanımındaki a-g):
  a) agent withdraw denemesi → revert (agent'ın share'i yok; fon çekemez)
  b) whitelist dışı target → queueTrade revert
  c) günlük notional limit aşımı → revert
  d) timelock dolmadan executeTrade → revert
  e) guardian veto akışı → veto sonrası execute revert
  f) pause sırasında agent işlemi → revert (kullanıcı çekimi çalışır)
  g) normal deposit/withdraw her zaman çalışır (pause dahil)
Ek:
  h) whitelist'li target + selector ile timelock sonrası execute → başarılı
  i) whitelist'li target'ta whitelist'siz selector → revert
  j) agent asset kontratını doğrudan çağıramaz → revert
  k) agent owner fonksiyonlarını çağıramaz → revert
  l) SWAP_SELECTOR sabiti (agent_loop.py) keccak ile uyumlu

Çalıştırma:  python3 tests/test_quant_vault.py
Çıkış: PASS/FAIL tablosu (SECURITY_TEST_REPORT.md bu çıktıdan üretilir).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VAULT_DIR = Path(__file__).resolve().parent.parent
CONTRACT_DIR = VAULT_DIR / "contract"
SOLC_VERSION = "0.8.24"
TIMELOCK = 900            # 15 dk
MAX_DAILY = 10_000 * 10**6

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))


def expect_revert(name: str, fn, substr: str = "") -> None:
    try:
        fn()
        record(name, False, "revert BEKLENİYORDU ama başarılı oldu")
    except Exception as e:
        msg = str(e)
        if substr and substr not in msg:
            record(name, False, f"revert oldu ama mesaj farklı: {msg[:120]}")
        else:
            record(name, True, f"revert: {substr or msg[:80]}")


def expect_ok(name: str, fn, note: str = "") -> None:
    try:
        fn()
        record(name, True, note)
    except Exception as e:
        record(name, False, f"beklenmeyen hata: {e!r}"[:200])


def main() -> int:
    import solcx
    from web3 import Web3
    from web3.providers.eth_tester import EthereumTesterProvider
    from eth_utils import keccak

    if SOLC_VERSION not in [str(v) for str_v in solcx.get_installed_solc_versions() for v in [str_v]]:
        print(f"[..] solc {SOLC_VERSION} kuruluyor...")
        solcx.install_solc(SOLC_VERSION)
    compiled = solcx.compile_files(
        [str(CONTRACT_DIR / "QuantVault.sol"), str(CONTRACT_DIR / "TestToken.sol")],
        output_values=["abi", "bin"], solc_version=SOLC_VERSION,
    )

    w3 = Web3(EthereumTesterProvider())
    guardian, agent, user, attacker = w3.eth.accounts[:4]

    # SWAP_SELECTOR sabitini doğrula (agent_loop.py ile aynı olmalı)
    real_selector = keccak(text="swap(uint256)")[:4]
    sys.path.insert(0, str(VAULT_DIR))
    from agent_loop import SWAP_SELECTOR
    record("l) SWAP_SELECTOR sabiti keccak ile uyumlu", SWAP_SELECTOR == real_selector,
           f"sabit=0x{SWAP_SELECTOR.hex()} keccak=0x{real_selector.hex()}")

    def deploy(name: str, *args):
        key = next(k for k in compiled if k.endswith(f":{name}"))
        c = w3.eth.contract(abi=compiled[key]["abi"], bytecode=compiled[key]["bin"])
        txh = c.constructor(*args).transact({"from": guardian})
        addr = w3.eth.get_transaction_receipt(txh).contractAddress
        return w3.eth.contract(address=addr, abi=compiled[key]["abi"])

    token = deploy("TestToken", "Test USDC", "tUSDC")
    router = deploy("MockRouter")
    vault = deploy("QuantVault", token.address, agent, TIMELOCK, MAX_DAILY)
    G = {"from": guardian}
    A = {"from": agent}
    U = {"from": user}

    # kullanıcıya token + vault'a deposit hazırlığı
    token.functions.mint(user, 5_000 * 10**6).transact(G)
    token.functions.mint(guardian, 5_000 * 10**6).transact(G)
    token.functions.approve(vault.address, 5_000 * 10**6).transact(U)
    token.functions.approve(vault.address, 5_000 * 10**6).transact(G)

    # whitelist: router.swap(uint256)
    vault.functions.setAllowedCall(router.address, real_selector, True).transact(G)

    def swap_calldata(amount: int) -> bytes:
        return real_selector + amount.to_bytes(32, "big")

    # ------------------------------------------------------------------
    # g) normal deposit/withdraw her zaman çalışır
    # ------------------------------------------------------------------
    def g_flow():
        vault.functions.deposit(1_000 * 10**6).transact(U)
        sh = vault.functions.sharesOf(user).call()
        assert sh == 1_000 * 10**6, sh
        before = token.functions.balanceOf(user).call()
        vault.functions.withdraw(sh).transact(U)
        after = token.functions.balanceOf(user).call()
        assert after - before == 1_000 * 10**6
        # kalıcı pozisyon: guardian deposit (sonraki testler vault'ta fon olsun)
        vault.functions.deposit(2_000 * 10**6).transact(G)
    expect_ok("g) deposit/withdraw izinsiz ve her zaman çalışır", g_flow)

    # ------------------------------------------------------------------
    # a) agent withdraw denemesi → revert
    # ------------------------------------------------------------------
    expect_revert("a) agent withdraw denemesi revert", lambda:
                  vault.functions.withdraw(1).transact(A), "insufficient shares")

    # ------------------------------------------------------------------
    # b) whitelist dışı target → revert
    # ------------------------------------------------------------------
    expect_revert("b) non-whitelist target revert", lambda:
                  vault.functions.queueTrade(attacker, swap_calldata(100), 100).transact(A),
                  "target not whitelisted")

    # ------------------------------------------------------------------
    # i) whitelist'li target, whitelist'siz selector → revert
    # ------------------------------------------------------------------
    bad_sel = keccak(text="swapReverting(uint256)")[:4]
    expect_revert("i) whitelist'siz selector revert", lambda:
                  vault.functions.queueTrade(router.address, bad_sel + (100).to_bytes(32, "big"), 100).transact(A),
                  "selector not whitelisted")

    # ------------------------------------------------------------------
    # j) agent asset kontratını doğrudan çağıramaz
    # ------------------------------------------------------------------
    expect_revert("j) agent asset'i doğrudan çağıramaz", lambda:
                  vault.functions.queueTrade(token.address, swap_calldata(1), 1).transact(A),
                  "cannot call asset")

    # ------------------------------------------------------------------
    # c) günlük limit aşımı → revert
    # ------------------------------------------------------------------
    expect_revert("c1) tek seferde limit aşımı revert", lambda:
                  vault.functions.queueTrade(router.address, swap_calldata(MAX_DAILY + 1),
                                             MAX_DAILY + 1).transact(A),
                  "daily limit exceeded")
    # kümülatif: 2 × %60 limit
    vault.functions.queueTrade(router.address, swap_calldata(int(MAX_DAILY * 0.6)),
                               int(MAX_DAILY * 0.6)).transact(A)
    expect_revert("c2) kümülatif günlük limit aşımı revert", lambda:
                  vault.functions.queueTrade(router.address, swap_calldata(int(MAX_DAILY * 0.6)),
                                             int(MAX_DAILY * 0.6)).transact(A),
                  "daily limit exceeded")

    # ------------------------------------------------------------------
    # d) timelock dolmadan execute → revert
    # ------------------------------------------------------------------
    expect_revert("d) timelock öncesi execute revert", lambda:
                  vault.functions.executeTrade(0).transact(A), "timelock not elapsed")

    # ------------------------------------------------------------------
    # e) guardian veto akışı
    # ------------------------------------------------------------------
    vault.functions.vetoTrade(0).transact(G)
    w3.testing.timeTravel(w3.eth.get_block("latest").timestamp + TIMELOCK + 10)
    expect_revert("e) veto edilen trade execute revert", lambda:
                  vault.functions.executeTrade(0).transact(A), "vetoed")

    # ------------------------------------------------------------------
    # h) normal akış: kuyruk → timelock → execute başarılı
    #    (kalan günlük limit: MAX_DAILY - 0.6*MAX_DAILY = 0.4*MAX_DAILY)
    # ------------------------------------------------------------------
    ok_notional = int(MAX_DAILY * 0.3)
    vault.functions.queueTrade(router.address, swap_calldata(ok_notional), ok_notional).transact(A)
    qid = vault.functions.queueLength().call() - 1
    w3.testing.timeTravel(w3.eth.get_block("latest").timestamp + TIMELOCK + 10)
    def exec_ok():
        vault.functions.executeTrade(qid).transact(A)
        t = vault.functions.tradeQueue(qid).call()
        assert t[4] is True  # executed
    expect_ok("h) whitelist'li trade timelock sonrası execute", exec_ok)

    # ------------------------------------------------------------------
    # f) pause sırasında agent işlemleri revert; kullanıcı çekimi çalışır
    # ------------------------------------------------------------------
    vault.functions.pause().transact(G)
    expect_revert("f1) pause'da queueTrade revert", lambda:
                  vault.functions.queueTrade(router.address, swap_calldata(1), 1).transact(A), "paused")
    expect_revert("f2) pause'da recordSignal revert", lambda:
                  vault.functions.recordSignal(0, "NEUTRAL", 5000).transact(A), "paused")
    expect_revert("f3) pause'da executeTrade revert", lambda:
                  vault.functions.executeTrade(qid).transact(A), "paused")
    def withdraw_during_pause():
        token.functions.mint(user, 500 * 10**6).transact(G)
        token.functions.approve(vault.address, 500 * 10**6).transact(U)
        vault.functions.deposit(500 * 10**6).transact(U)
        sh = vault.functions.sharesOf(user).call()
        vault.functions.withdraw(sh).transact(U)
    expect_ok("f4) pause'da kullanıcı deposit/withdraw çalışır", withdraw_during_pause)
    vault.functions.unpause().transact(G)

    # ------------------------------------------------------------------
    # k) agent owner fonksiyonlarını çağıramaz
    # ------------------------------------------------------------------
    expect_revert("k1) agent pause yapamaz", lambda:
                  vault.functions.pause().transact(A), "not the owner")
    expect_revert("k2) agent whitelist değiştiremez", lambda:
                  vault.functions.setAllowedCall(attacker, real_selector, True).transact(A), "not the owner")
    expect_revert("k3) agent agent'ı değiştiremez", lambda:
                  vault.functions.setAgent(attacker).transact(A), "not the owner")

    # ------------------------------------------------------------------
    # m) recordSignal başarılı + SignalRecorded event'i (agent_loop akışı)
    # ------------------------------------------------------------------
    def signal_flow():
        txh = vault.functions.recordSignal(39900000000000000, "NEUTRAL", 5100).transact(A)
        rcpt = w3.eth.get_transaction_receipt(txh)
        evts = vault.events.SignalRecorded().process_receipt(rcpt)
        assert len(evts) == 1
        assert evts[0].args.composite == 39900000000000000
        assert evts[0].args.signal == "NEUTRAL"
        assert evts[0].args.targetExposureBps == 5100
    expect_ok("m) recordSignal başarılı + SignalRecorded event", signal_flow)

    # ------------------------------------------------------------------
    # Özet
    # ------------------------------------------------------------------
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== {passed}/{total} PASS ===")
    # rapor için JSON bırak
    (VAULT_DIR / "tests" / "last_results.json").write_text(json.dumps([
        {"test": n, "pass": ok, "note": note} for n, ok, note in RESULTS
    ], indent=2, ensure_ascii=False))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
