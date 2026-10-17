#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QuantVault — Avalanche Fuji deploy scripti.

Gerekli env:
  PRIVATE_KEY        : deploy eden (owner/guardian) hesabın private key'i (0x...)
                       ASLA koda gömülmez; sadece env'den okunur.
İsteğe bağlı env:
  RPC_URL            : varsayılan Fuji C-Chain RPC
  AGENT_ADDRESS      : agent adresi (yoksa deploy edenle aynı)
  ASSET_ADDRESS      : mevcut ERC-20 (ör. Fuji USDC). Yoksa TestToken deploy edilir.
  TIMELOCK_SECONDS   : agent trade gecikmesi (varsayılan 900 = 15 dk)
  MAX_DAILY_NOTIONAL : günlük üst sınır, asset en küçük birimi (varsayılan 10_000e6)

Çalıştırma:
  PRIVATE_KEY=0x... python3 deploy.py
Çıktı: kontrat adresleri + QuantVault.abi.json (agent_loop.py bunu kullanır).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VAULT_DIR = Path(__file__).resolve().parent
CONTRACT_DIR = VAULT_DIR / "contract"

FUJI_RPC = "https://api.avax-test.network/ext/bc/C/rpc"
FUJI_CHAIN_ID = 43113
SOLC_VERSION = "0.8.24"


def compile_contracts() -> dict:
    import solcx
    try:
        solcx.get_installed_solc_versions()
        if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
            print(f"[..] solc {SOLC_VERSION} indiriliyor...")
            solcx.install_solc(SOLC_VERSION)
    except Exception:
        solcx.install_solc(SOLC_VERSION)
    return solcx.compile_files(
        [str(CONTRACT_DIR / "QuantVault.sol"), str(CONTRACT_DIR / "TestToken.sol")],
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
    )


def deploy(w3, compiled, name: str, args: list, deployer: str, privkey: str) -> tuple[str, object]:
    key = next(k for k in compiled if k.endswith(f":{name}"))
    contract = w3.eth.contract(abi=compiled[key]["abi"], bytecode=compiled[key]["bin"])
    tx = contract.constructor(*args).build_transaction({
        "from": deployer,
        "nonce": w3.eth.get_transaction_count(deployer),
        "chainId": FUJI_CHAIN_ID,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, privkey)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"[..] {name} gönderildi: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"{name} deploy revert: {tx_hash.hex()}")
    addr = receipt.contractAddress
    print(f"[OK] {name} @ {addr} (gas: {receipt.gasUsed})")
    return addr, w3.eth.contract(address=addr, abi=compiled[key]["abi"])


def main() -> int:
    privkey = os.environ.get("PRIVATE_KEY", "").strip()
    if not privkey:
        print("HATA: PRIVATE_KEY env değişkeni gerekli (Fuji testnet key'i).", file=sys.stderr)
        print("      Faucet: https://faucet.avax.network/ (Avalanche Fuji C-Chain)", file=sys.stderr)
        return 1

    from web3 import Web3
    rpc = os.environ.get("RPC_URL", FUJI_RPC)
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print(f"HATA: RPC'ye bağlanılamadı: {rpc}", file=sys.stderr)
        return 1

    deployer = w3.eth.account.from_key(privkey).address
    balance = w3.eth.get_balance(deployer)
    print(f"[..] Deployer: {deployer} | bakiye: {w3.from_wei(balance, 'ether')} AVAX")
    if balance == 0:
        print("HATA: Bakiye 0. Önce faucet'tan test AVAX al: https://faucet.avax.network/", file=sys.stderr)
        return 1

    agent = os.environ.get("AGENT_ADDRESS", deployer)
    timelock = int(os.environ.get("TIMELOCK_SECONDS", "900"))
    max_daily = int(os.environ.get("MAX_DAILY_NOTIONAL", str(10_000 * 10**6)))

    print("[..] Derleniyor...")
    compiled = compile_contracts()

    # 1) Asset: varsa kullan, yoksa TestToken bas
    asset_addr = os.environ.get("ASSET_ADDRESS", "").strip()
    if not asset_addr:
        asset_addr, token = deploy(w3, compiled, "TestToken", ["Test USDC", "tUSDC"], deployer, privkey)
    else:
        asset_addr = w3.to_checksum_address(asset_addr)
        print(f"[..] Mevcut asset kullanılıyor: {asset_addr}")

    # 2) MockRouter (demo trade hedefi — whitelist'lenecek)
    router_addr, _ = deploy(w3, compiled, "MockRouter", [], deployer, privkey)

    # 3) QuantVault
    vault_addr, vault = deploy(
        w3, compiled, "QuantVault",
        [asset_addr, w3.to_checksum_address(agent), timelock, max_daily],
        deployer, privkey,
    )

    # 4) Whitelist: MockRouter.swap(uint256)
    from eth_utils import keccak
    selector = keccak(text="swap(uint256)")[:4]
    tx = vault.functions.setAllowedCall(
        w3.to_checksum_address(router_addr), selector, True
    ).build_transaction({
        "from": deployer,
        "nonce": w3.eth.get_transaction_count(deployer),
        "chainId": FUJI_CHAIN_ID,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, privkey)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    assert r.status == 1, "setAllowedCall revert"
    print(f"[OK] Whitelist: MockRouter.swap selector=0x{selector.hex()}")

    # 5) ABI'yi agent_loop'un okuyacağı yere yaz
    key = next(k for k in compiled if k.endswith(":QuantVault"))
    (CONTRACT_DIR / "QuantVault.abi.json").write_text(json.dumps(compiled[key]["abi"]))

    summary = {
        "network": "avalanche-fuji",
        "chainId": FUJI_CHAIN_ID,
        "deployer_guardian": deployer,
        "agent": agent,
        "asset": asset_addr,
        "router_whitelisted": router_addr,
        "vault": vault_addr,
        "timelock_seconds": timelock,
        "max_daily_notional": max_daily,
    }
    (VAULT_DIR / "deployment.json").write_text(json.dumps(summary, indent=2))
    print("\n=== DEPLOY TAMAM ===")
    print(json.dumps(summary, indent=2))
    print("\nAgent'ı çalıştırmak için:")
    print(f"  export VAULT_ADDRESS={vault_addr}")
    print(f"  export ROUTER_ADDRESS={router_addr}")
    print(f"  export AGENT_PRIVATE_KEY=<agent hesabının key'i>")
    print("  python3 agent_loop.py --once")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
