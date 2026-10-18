#!/usr/bin/env python3
"""deploy.py — QuantVault'u Avalanche Fuji'ye dağıtır.

Kullanım:
    python vault/deploy.py

Gereken env (.env): PRIVATE_KEY (deployer), AGENT_ADDRESS (agent EOA)
Yazar: vault/deployment.json + vault/contract/QuantVault.abi.json
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
from fetchers import load_key  # noqa: E402 — ortak key çözümleme

from web3 import Web3  # noqa: E402

FUJI_RPC = "https://api.avax-test.network/ext/bc/C/rpc"
CHAIN_ID = 43113


def compile_contract():
    import solcx  # py-solc-x
    solcx.install_solc("0.8.24")
    solcx.set_solc_version("0.8.24")
    src = (ROOT / "vault" / "contract" / "QuantVault.sol").read_text(encoding="utf-8")
    std = solcx.compile_standard({
        "language": "Solidity",
        "sources": {"QuantVault.sol": {"content": src}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode"]}},
        },
    }, allow_paths=[ROOT])
    out = std["contracts"]["QuantVault.sol"]["QuantVault"]
    return out["abi"], out["evm"]["bytecode"]["object"]


def main():
    pk = load_key("PRIVATE_KEY")
    agent_addr = load_key("AGENT_ADDRESS")
    if not pk:
        raise SystemExit("PRIVATE_KEY env/.env yok")
    if not agent_addr:
        raise SystemExit("AGENT_ADDRESS env/.env yok")

    w3 = Web3(Web3.HTTPProvider(FUJI_RPC))
    if not w3.is_connected():
        raise SystemExit("Fuji RPC bağlanamadı")
    acct = w3.eth.account.from_key(pk)
    print(f"deployer: {acct.address}")
    print(f"balance : {w3.from_wei(w3.eth.get_balance(acct.address), 'ether')} AVAX")

    print("derleniyor...")
    abi, bytecode = compile_contract()
    (ROOT / "vault" / "contract" / "QuantVault.abi.json").write_text(
        json.dumps(abi), encoding="utf-8")

    Vault = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = Vault.constructor(acct.address, Web3.to_checksum_address(agent_addr)
                           ).build_transaction({
        "from": acct.address, "nonce": nonce,
        "gas": 3_500_000, "maxFeePerGas": w3.to_wei(30, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(2, "gwei"), "chainId": CHAIN_ID})
    signed = w3.eth.account.sign_transaction(tx, pk)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"deploy tx: {txh.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    if rcpt.status != 1:
        raise SystemExit("deploy revert etti")
    addr = rcpt.contractAddress
    print(f"QuantVault: {addr}")

    dep = {
        "network": "fuji", "chainId": CHAIN_ID, "rpc": FUJI_RPC,
        "address": addr, "deployer": acct.address, "agent": agent_addr,
        "deploy_tx": txh.hex(),
        "explorer": f"https://testnet.snowtrace.io/address/{addr}",
        "whitelist": [],  # deploy sonrası setWhitelisted ile doldurulur
    }
    (ROOT / "vault" / "deployment.json").write_text(
        json.dumps(dep, indent=2), encoding="utf-8")
    print("deployment.json yazıldı")


if __name__ == "__main__":
    main()
