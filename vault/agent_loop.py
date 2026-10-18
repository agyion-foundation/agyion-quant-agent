#!/usr/bin/env python3
"""
agent_loop.py — QuantVault agent döngüsü.

Her koşuda:
  1. Son snapshot'ı okur (engine/data/snapshot.json)
  2. POLICY.md kanonik politikasını uygular (docs/POLICY.md §2-§5):
     NÖTR etiket -> değişiklik yok; AL/SAT etiketi -> logistic(composite, k=4)
     taban hedefi; LT+/ST- çatışması x0.75; LT-/ST- -> 0.05; rejim < -0.5 -> x0.5
  3. Hedef ile mevcut exposure arasındaki fark |delta| > REBALANCE_THRESHOLD ise
     trade önerisi üretir  (vault/proposals/proposal_<id>.json)
  4. Onay varsa QuantVault.queueTrade + timelock sonrası executeTrade gönderir

Onay modeli:
  --auto            : onaysız doğrudan kuyruğa gönder (testnet/demo)
  varsayılan        : öneriyi yazar, approvals/<id>.json dosyası beklenir
  --wait-approval N : N saniye boyunca onay dosyasını bekle

Çalıştırma:
  python3 vault/agent_loop.py --network fuji
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "engine" / "data" / "snapshot.json"
PROPOSALS = ROOT / "vault" / "proposals"
APPROVALS = ROOT / "vault" / "approvals"
DEPLOYMENT = ROOT / "vault" / "deployment.json"
DECISIONS = ROOT / "vault" / "logs" / "agent_decisions.jsonl"

# POLICY.md kanonik parametreleri (kaynak: docs/POLICY.md §2-§5 — burada yeniden
# tanımlanmaz, oradan türetilmiştir):
# §2  logistic(x, k) = 1/(1+e^(-k·x)), k = 4; NÖTR etiket -> değişiklik yok (hold)
# §3  LT+/ST- çatışması -> hedef x 0.75; LT-/ST- -> hedef 0.05 (işaret bazlı)
# §4  regime_index < -0.5 -> hedef x 0.5
# §5  |hedef - mevcut| > 0.05 VEYA etiket değişimi -> rebalance; aksi HOLD
K_LOGISTIC = 4.0
REBALANCE_THRESHOLD = 0.05
CONFLICT_CUT = 0.75
FLIGHT_TARGET = 0.05
REGIME_CUT = 0.5
REGIME_RISK_OFF = -0.5

TRADE_ASSET = "tUSDC"          # testnet stable
MAX_NOTIONAL_USD = 9_500       # günlük limitin ($10k) altında güvenli tavan


def logistic(x, k=K_LOGISTIC):
    return 1.0 / (1.0 + math.exp(-k * x))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_decision(rec):
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_snapshot():
    if not SNAPSHOT.exists():
        raise SystemExit(f"snapshot yok: {SNAPSHOT} — önce engine koşsun")
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def load_deployment(network):
    if not DEPLOYMENT.exists():
        raise SystemExit(f"deployment.json yok: {DEPLOYMENT} — önce deploy.py")
    dep = json.loads(DEPLOYMENT.read_text(encoding="utf-8"))
    if dep.get("network") != network:
        raise SystemExit(f"ağ uyuşmazlığı: deployment={dep.get('network')} arg={network}")
    return dep


def current_exposure_usd(w3, vault, asset_addr):
    """Vault içindeki riskli varlık payını USD olarak okur.

    Basit model: vault toplam varlığı - tUSDC (stable) bakiyesi = riskli kısım.
    Testnet demo'sunda yalnızca tUSDC tutulduğu için exposure ≈ 0.
    """
    total = vault.functions.totalAssets().call()
    return {"total_assets_usd": total / 1e6, "risk_usd": 0.0}


def decide(snap, exposure, total_assets_usd):
    """POLICY.md kanonik politikasını uygular (§1-§5).

    Etiket snapshot'taki engine hesabıdır (Schmitt hysteresis §1); bu döngü
    boyutlandırma/rebalance kurallarını uygular (§2-§5).
    Dönüş: (hedef_exposure, gerekçe, detay)
    """
    lt = snap.get("long_term_index") or 0.0
    st = snap.get("short_term_index") or 0.0
    rg = snap.get("regime_index")
    comp = snap.get("composite") or 0.0
    label = snap.get("signal") or "NÖTR"
    prev = snap.get("signal_prev")

    # §2: NÖTR etiket -> değişiklik yok (hold)
    if label == "NÖTR":
        return exposure, "HOLD", "NEUTRAL label -> no change (POLICY.md §2)"

    base = logistic(comp)
    steps = [f"logistic(k={K_LOGISTIC}) base={base:.3f}"]

    # §3: çatışma ve türev kuralları (işaret bazlı)
    if lt > 0 and st < 0:
        base *= CONFLICT_CUT
        steps.append(f"conflict LT+/ST- -> x{CONFLICT_CUT}")
    elif lt < 0 and st < 0:
        base = FLIGHT_TARGET
        steps.append(f"double-negative LT-/ST- -> {FLIGHT_TARGET}")

    # §4: rejim filtresi
    if rg is not None and rg < REGIME_RISK_OFF:
        base *= REGIME_CUT
        steps.append(f"regime {rg:.2f} < {REGIME_RISK_OFF} -> x{REGIME_CUT}")

    target = min(0.95, max(0.0, base))
    return target, "REBALANCE_CANDIDATE", "; ".join(steps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", default="fuji", choices=["fuji", "local"])
    ap.add_argument("--auto", action="store_true", help="onaysız gönder (demo)")
    ap.add_argument("--wait-approval", type=int, default=0,
                    help="onay dosyasını N saniye bekle")
    ap.add_argument("--dry-run", action="store_true",
                    help="zincire dokunma; yalnızca öneri üret")
    args = ap.parse_args()

    snap = load_snapshot()
    dep = load_deployment(args.network)
    comp = snap.get("composite") or 0.0
    label = snap.get("signal") or "NÖTR"
    prev = snap.get("signal_prev")

    # mevcut exposure: dry-run'da zincire dokunmadan 0 varsay (demo)
    exposure = 0.0
    total_usd = dep.get("deposit_tx") and 2000.0 or 0.0  # DEPLOY'da bilinen ilk yatırım

    target, action, detail = decide(snap, exposure, total_usd)
    delta = target - exposure
    label_changed = (prev is not None and prev != label)

    # §5: rebalance tetikleyicisi — |delta| > eşik VEYA etiket değişimi
    triggered = abs(delta) > REBALANCE_THRESHOLD or label_changed
    if action == "HOLD" or not triggered:
        rec = {"ts": now_iso(), "type": "HOLD", "composite": comp, "label": label,
               "label_prev": prev, "exposure": exposure, "target": round(target, 4),
               "reason": detail if action == "HOLD"
               else f"|delta|={abs(delta):.3f} <= {REBALANCE_THRESHOLD} and label unchanged"}
        log_decision(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return

    prop_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + \
        hex(abs(hash((comp, target))) & 0xFFFFFF)[2:]
    notional = round(abs(delta) * max(total_usd, 1e9), 2)  # demo: total bilinmiyorsa nominal
    notional = min(notional, MAX_NOTIONAL_USD)
    proposal = {
        "id": prop_id,
        "ts": now_iso(),
        "policy": "POLICY.md v1 (Schmitt + logistic k=4 + conflict/regime rules)",
        "composite": comp, "label": label, "label_prev": prev,
        "lt": snap.get("long_term_index"), "st": snap.get("short_term_index"),
        "regime": snap.get("regime_index"),
        "current_exposure": exposure, "target_exposure": round(target, 4),
        "delta": round(delta, 4), "notional_usd": notional, "asset": TRADE_ASSET,
        "rationale": detail,
        "status": "proposed",
    }
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    APPROVALS.mkdir(parents=True, exist_ok=True)
    (PROPOSALS / f"proposal_{prop_id}.json").write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    log_decision({"ts": now_iso(), "type": "PROPOSE", **{k: proposal[k] for k in
                  ("id", "composite", "label", "target_exposure", "delta", "notional_usd")},
                  "rationale": detail})
    print(json.dumps({"type": "PROPOSE", **proposal}, ensure_ascii=False, indent=2))

    if args.dry_run:
        print("--dry-run: zincire gönderim yapılmadı")
        return

    # onay bekleme
    approval_file = APPROVALS / f"{prop_id}.json"
    approved = args.auto
    if not approved and args.wait_approval > 0:
        deadline = time.time() + args.wait_approval
        while time.time() < deadline:
            if approval_file.exists():
                approved = True
                break
            time.sleep(2)
    if not approved:
        log_decision({"ts": now_iso(), "type": "AWAITING_APPROVAL", "id": prop_id})
        print(f"onay bekleniyor: {approval_file} (--auto ile atla)")
        return

    # zincir: queueTrade
    try:
        from web3 import Web3
    except ImportError:
        raise SystemExit("web3 yok: pip install web3")
    w3 = Web3(Web3.HTTPProvider(dep["rpc"]))
    if not w3.is_connected():
        raise SystemExit(f"RPC bağlanamadı: {dep['rpc']}")
    abi = json.loads((ROOT / "vault" / "contract" / "QuantVault.abi.json").read_text())
    vault = w3.eth.contract(address=Web3.to_checksum_address(dep["address"]), abi=abi)

    import os
    pk = os.environ.get("AGENT_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if not pk:
        raise SystemExit("AGENT_PRIVATE_KEY env yok")
    acct = w3.eth.account.from_key(pk)
    amount6 = int(notional * 1e6)
    data = b""  # demo: calldata yok; gerçekte swap selector + params

    nonce = w3.eth.get_transaction_count(acct.address)
    tx = vault.functions.queueTrade(
        Web3.to_checksum_address(dep["whitelist"][0]),
        amount6, data).build_transaction({
            "from": acct.address, "nonce": nonce,
            "gas": 250_000, "maxFeePerGas": w3.to_wei(30, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(2, "gwei"),
            "chainId": dep["chainId"]})
    signed = w3.eth.account.sign_transaction(tx, pk)
    txh = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    trade_id = None
    for ev in vault.events.TradeQueued().process_receipt(rcpt):
        trade_id = ev["args"]["tradeId"]
    log_decision({"ts": now_iso(), "type": "QUEUED", "id": prop_id,
                  "tradeId": trade_id, "tx": txh.hex(),
                  "status": rcpt.status})
    print(f"queueTrade tx: {txh.hex()} tradeId={trade_id}")
    print("timelock: 15 dk sonra executeTrade çağrılabilir")


if __name__ == "__main__":
    main()
