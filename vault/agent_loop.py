#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agyion Quant Agent — Avalanche vault agent döngüsü.

Akış:
  data/snapshot.json oku → sinyal etiketi (engine'in Schmitt ±0.50/±0.40 hysteresis'i,
  signal/signal_prev alanları) → hedef maruziyet (POLICY.md v1 kanonik kuralları:
  NÖTR hold, AL/SAT logistic bantları, çatışma ×0.75, LT−/ST−→0.05, regime<-0.5 → ×0.5)
  → rebalance tetikleyicisi (|Δ| > 0.05 veya etiket değişimi) → trade önerisi →
  onay modu:
    * varsayılan (human-in-the-loop): proposals/<id>.json yazılır,
      approvals/<id>.approved dosyası görülünce kontrata queueTrade gönderilir.
    * --auto: öneri doğrudan kontrat kuyruğuna gönderilir.
  Her karar logs/agent_decisions.jsonl'e append edilir (jüriye kanıt defteri).

Kontrat çağrıları web3.py ile Fuji testnet'e gider. Anahtar SADECE
AGENT_PRIVATE_KEY env değişkeninden okunur — koda gömülmez.

Kullanım:
  python3 agent_loop.py --once                 # tek karar döngüsü (HITL)
  python3 agent_loop.py --once --auto          # tek karar, doğrudan kuyruğa
  python3 agent_loop.py --interval 900         # sürekli döngü (15 dk)
  python3 agent_loop.py --dry-run              # zincir yok, sadece defter+dosya
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

VAULT_DIR = Path(__file__).resolve().parent
ROOT = VAULT_DIR.parent
SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"
LOGS_DIR = VAULT_DIR / "logs"
PROPOSALS_DIR = VAULT_DIR / "proposals"
APPROVALS_DIR = VAULT_DIR / "approvals"
DECISIONS_LOG = LOGS_DIR / "agent_decisions.jsonl"
STATE_PATH = LOGS_DIR / "exposure_state.json"
CONTRACT_PATH = VAULT_DIR / "contract" / "QuantVault.sol"

FUJI_RPC = "https://api.avax-test.network/ext/bc/C/rpc"
FUJI_CHAIN_ID = 43113

# --- Strateji sabitleri (POLICY.md v1 — kanonik, tek otorite) ---------------
LOGISTIC_K = 4.0          # logistic(x,k) = 1/(1+e^{-k·x})              [POLICY §2]
BUY_BAND = (0.50, 0.95)   # AL etiketinde hedef bandı                    [POLICY §2]
SELL_BAND = (0.05, 0.50)  # SAT etiketinde hedef bandı                   [POLICY §2]
CONFLICT_HAIRCUT = 0.75   # LT+/ST− "Correction" → hedef × 0.75          [POLICY §3]
FULL_EXIT = 0.05          # LT−/ST− çift negatif → hedef = 0.05          [POLICY §3]
REGIME_CUTOFF = -0.5      # regime_index < -0.5 → güçlü risk-off         [POLICY §4]
REGIME_SCALE = 0.5        # risk-off'ta hedef × 0.5                      [POLICY §4]
REBALANCE_BAND = 0.05     # |hedef - mevcut| > 0.05 VEYA etiket değişimi [POLICY §5]


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_decision(record: dict) -> None:
    """Kanıt defterine atomik append (JSONL)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": now_iso(), **record}
    with open(DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_snapshot(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_current_exposure() -> float:
    if STATE_PATH.exists():
        try:
            return float(json.loads(STATE_PATH.read_text())["exposure"])
        except Exception:
            pass
    return 0.0


def save_current_exposure(exposure: float) -> None:
    STATE_PATH.write_text(json.dumps({"exposure": exposure, "ts": now_iso()}))


def target_exposure(label_tr: str, composite: float, lt: float | None, st: float | None,
                    regime: float | None, current: float) -> tuple[float, str]:
    """POLICY.md v1 kanonik boyutlandırma → hedef maruziyet [0,1].

      §2  Etiket bazlı: NÖTR → hold (hedef = mevcut, değişiklik yok).
          AL  → logistic(composite, k=4) clamp [0.50, 0.95]
          SAT → logistic(composite, k=4) clamp [0.05, 0.50]
      §3  Çatışma (işaret bazlı): LT+/ST− → ×0.75 ; LT−/ST− → hedef = 0.05
      §4  Rejim: regime_index < -0.5 → ×0.5 (composite'a girmez, sadece ölçekler)
    Sinyal etiketinin kendisi (Schmitt ±0.50/±0.40 hysteresis) engine'de üretilir;
    agent snapshot'taki signal/signal_prev alanlarını okur, yeniden hesaplamaz.
    """
    if label_tr == "NÖTR":
        return current, f"NÖTR hold [POLICY §2] — mevcut {current:.3f} korunur"

    base = 1.0 / (1.0 + math.exp(-LOGISTIC_K * composite))
    if label_tr == "AL":
        lo, hi = BUY_BAND
    else:  # SAT
        lo, hi = SELL_BAND
    tgt = min(max(base, lo), hi)
    reason = f"{label_tr}: logistic(k={LOGISTIC_K})={base:.3f} clamp[{lo},{hi}] → {tgt:.3f} [POLICY §2]"

    if lt is not None and st is not None:
        if lt < 0 and st < 0:
            tgt = FULL_EXIT
            reason += f" | LT−/ST− çift negatif → tam çıkış {FULL_EXIT} [POLICY §3]"
        elif lt > 0 and st < 0:
            tgt *= CONFLICT_HAIRCUT
            reason += f" | LT+/ST− çatışma → ×{CONFLICT_HAIRCUT} = {tgt:.3f} [POLICY §3]"

    if regime is not None and regime < REGIME_CUTOFF:
        tgt *= REGIME_SCALE
        reason += f" | regime={regime:.3f} < {REGIME_CUTOFF} risk-off → ×{REGIME_SCALE} = {tgt:.3f} [POLICY §4]"

    return tgt, reason


def map_signal(sig_tr: str) -> str:
    """Snapshot'ın Türkçe sinyalini kontrat İngilizce'sine çevir."""
    return {"AL": "BUY", "SAT": "SELL"}.get(sig_tr, "NEUTRAL")


# ---------------------------------------------------------------------------
# Zincir katmanı (web3)
# ---------------------------------------------------------------------------

def get_web3(rpc: str):
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        raise ConnectionError(f"RPC'ye bağlanılamadı: {rpc}")
    return w3


def load_vault(w3, vault_address: str):
    """QuantVault ABI'sini solcx çıktısından veya önbellek ABI dosyasından yükler."""
    abi_path = VAULT_DIR / "contract" / "QuantVault.abi.json"
    if abi_path.exists():
        abi = json.loads(abi_path.read_text())
    else:
        abi = _compile_abi()
    return w3.eth.contract(address=w3.to_checksum_address(vault_address), abi=abi)


def _compile_abi() -> list:
    import solcx
    compiled = solcx.compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi"],
        solc_version="0.8.24",
    )
    key = next(k for k in compiled if k.endswith(":QuantVault"))
    return compiled[key]["abi"]


# keccak256("swap(uint256)")[:4] — eth_utils yoksa diye sabit (doğrulandı).
SWAP_SELECTOR = bytes.fromhex("94b918de")


def build_trade_calldata(delta_exposure: float, total_assets: int) -> tuple[str, bytes, int]:
    """Hedef maruziyet farkını trade calldata'sına çevirir.

    Varsayılan: MockRouter.swap(uint256 amountIn) — Fuji demo hedefi.
    Notional = |Δexposure| × totalAssets (asset'in en küçük birimi).
    """
    notional = int(abs(delta_exposure) * total_assets)
    data = SWAP_SELECTOR + notional.to_bytes(32, "big")
    return "swap(uint256)", data, notional


def send_tx(w3, contract_call, agent_addr: str, privkey: str) -> str:
    tx = contract_call.build_transaction({
        "from": agent_addr,
        "nonce": w3.eth.get_transaction_count(agent_addr),
        "chainId": FUJI_CHAIN_ID,
        "gasPrice": w3.eth.gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, privkey)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"İşlem revert oldu: {tx_hash.hex()}")
    return tx_hash.hex()


# ---------------------------------------------------------------------------
# Ana karar döngüsü
# ---------------------------------------------------------------------------

def run_once(args) -> dict:
    snap = load_snapshot(Path(args.snapshot))
    composite = float(snap["composite"])
    lt = snap.get("long_term_index")
    st = snap.get("short_term_index")
    regime = snap.get("regime_index")
    label_tr = snap.get("signal", "NÖTR")
    label_prev = snap.get("signal_prev")
    signal_en = map_signal(label_tr)
    label_changed = label_prev is not None and label_prev != label_tr

    cur = load_current_exposure()
    tgt, reason = target_exposure(label_tr, composite, lt, st, regime, cur)
    delta = tgt - cur

    decision = {
        "composite": composite,
        "signal": signal_en,
        "signal_prev": map_signal(label_prev) if label_prev else None,
        "label_changed": label_changed,
        "regime_index": regime,
        "weights_version": snap.get("weights_version"),
        "long_term_index": lt,
        "short_term_index": st,
        "target_exposure": round(tgt, 4),
        "current_exposure": round(cur, 4),
        "delta": round(delta, 4),
        "mapping_rule": reason,
        "policy": "POLICY.md v1",
    }

    # POLICY §5 — rebalance tetikleyicisi: |Δ| > 0.05 VEYA etiket değişimi
    if label_tr == "NÖTR":
        decision["action"] = "HOLD"
        decision["why"] = "NÖTR etiket → değişiklik yok (hold) [POLICY §2]"
        append_decision(decision)
        print(f"[HOLD] {decision['why']} (composite={composite:+.4f})")
        return decision
    if abs(delta) <= REBALANCE_BAND and not label_changed:
        decision["action"] = "HOLD"
        decision["why"] = f"|Δ|={abs(delta):.4f} <= {REBALANCE_BAND} ve etiket değişmedi [POLICY §5]"
        append_decision(decision)
        print(f"[HOLD] {decision['why']} (composite={composite:+.4f}, sinyal={signal_en})")
        return decision

    # --- Trade önerisi oluştur ---
    proposal_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    total_assets = args.default_total_assets
    fn_sig, calldata, notional = build_trade_calldata(delta, total_assets)
    proposal = {
        "id": proposal_id,
        "ts": now_iso(),
        "direction": "INCREASE" if delta > 0 else "DECREASE",
        "target_exposure": round(tgt, 4),
        "delta_exposure": round(delta, 4),
        "notional": notional,
        "target": args.router_address,
        "calldata": "0x" + calldata.hex(),
        "function": fn_sig,
        "composite": composite,
        "signal": signal_en,
        "mode": "auto" if args.auto else "human-in-the-loop",
        "status": "proposed",
    }
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    prop_path = PROPOSALS_DIR / f"proposal_{proposal_id}.json"
    prop_path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False))
    decision["action"] = "PROPOSE"
    decision["proposal_id"] = proposal_id
    decision["notional"] = notional

    if args.dry_run:
        proposal["status"] = "dry-run"
        prop_path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False))
        decision["why"] = "dry-run: zincir çağrısı yapılmadı"
        append_decision(decision)
        print(f"[DRY-RUN] öneri yazıldı: {prop_path}")
        return decision

    # --- Onay: HITL dosya kontrolü veya --auto ---
    if not args.auto:
        approval = APPROVALS_DIR / f"{proposal_id}.approved"
        print(f"[HITL] Öneri: {prop_path}")
        print(f"[HITL] Onay için:  touch {approval}")
        if not args.wait_approval:
            decision["why"] = "onay bekleniyor (approval dosyası yok; --wait-approval ile bekle)"
            append_decision(decision)
            return decision
        deadline = time.time() + args.approval_timeout
        while time.time() < deadline and not approval.exists():
            time.sleep(5)
        if not approval.exists():
            proposal["status"] = "expired"
            prop_path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False))
            decision["action"] = "EXPIRED"
            decision["why"] = f"{args.approval_timeout}s içinde onay gelmedi"
            append_decision(decision)
            return decision

    # --- Zincire gönder: recordSignal + queueTrade ---
    privkey = os.environ.get("AGENT_PRIVATE_KEY", "")
    if not privkey or not args.vault_address:
        decision["action"] = "SIMULATED"
        decision["why"] = "AGENT_PRIVATE_KEY/--vault-address yok — zincir yerine simülasyon"
        append_decision(decision)
        print(f"[SIM] {decision['why']}")
        return decision

    w3 = get_web3(args.rpc)
    vault = load_vault(w3, args.vault_address)
    agent_addr = w3.eth.account.from_key(privkey).address

    total_assets_chain = vault.functions.totalAssets().call()
    if total_assets_chain > 0:
        fn_sig, calldata, notional = build_trade_calldata(delta, total_assets_chain)
        proposal["notional"] = notional
        proposal["calldata"] = "0x" + calldata.hex()

    sig_tx = send_tx(
        w3,
        vault.functions.recordSignal(int(composite * 10**18), signal_en, int(tgt * 10000)),
        agent_addr, privkey,
    )
    q_tx = send_tx(
        w3,
        vault.functions.queueTrade(
            w3.to_checksum_address(args.router_address), calldata, notional
        ),
        agent_addr, privkey,
    )
    proposal["status"] = "queued"
    proposal["recordSignal_tx"] = sig_tx
    proposal["queueTrade_tx"] = q_tx
    prop_path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False))

    save_current_exposure(tgt)
    decision["action"] = "QUEUED"
    decision["recordSignal_tx"] = sig_tx
    decision["queueTrade_tx"] = q_tx
    decision["why"] = "recordSignal + queueTrade zincire yazıldı (timelock kuyruğunda)"
    append_decision(decision)
    print(f"[QUEUED] sinyal={sig_tx} trade={q_tx}")
    return decision


def main() -> int:
    ap = argparse.ArgumentParser(description="Agyion Quant Agent — vault agent döngüsü")
    ap.add_argument("--snapshot", default=str(SNAPSHOT_PATH), help="snapshot.json yolu")
    ap.add_argument("--rpc", default=FUJI_RPC, help="Avalanche Fuji RPC")
    ap.add_argument("--vault-address", default=os.environ.get("VAULT_ADDRESS", ""),
                    help="QuantVault kontrat adresi (veya VAULT_ADDRESS env)")
    ap.add_argument("--router-address", default=os.environ.get(
        "ROUTER_ADDRESS", "0x0000000000000000000000000000000000000000"),
        help="Whitelist'li router kontrat adresi (veya ROUTER_ADDRESS env)")
    ap.add_argument("--default-total-assets", type=int, default=1_000_000_000,
                    help="Zincir okunamıyorsa notional hesabı için varsayılan toplam varlık (en küçük birim)")
    ap.add_argument("--once", action="store_true", help="Tek döngü çalıştır ve çık")
    ap.add_argument("--auto", action="store_true", help="Onay beklemeden doğrudan kuyruğa gönder")
    ap.add_argument("--dry-run", action="store_true", help="Zincire dokunma, sadece defter+dosya")
    ap.add_argument("--wait-approval", action="store_true", help="HITL modunda onay dosyasını bekle")
    ap.add_argument("--approval-timeout", type=int, default=600, help="HITL onay bekleme süresi (sn)")
    ap.add_argument("--interval", type=int, default=0, help="Sürekli döngü aralığı (sn); 0 = tek sefer")
    args = ap.parse_args()

    if args.interval > 0 and not args.once:
        while True:
            try:
                run_once(args)
            except Exception as e:  # döngüyü öldürme, deftere yaz
                append_decision({"action": "ERROR", "why": repr(e)})
                print(f"[ERROR] {e!r}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        run_once(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
