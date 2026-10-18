#!/usr/bin/env python3
"""signal_to_trade.py — engine sinyalini OKX ATK trade planına çevirir.

engine/data/snapshot.json -> POLICY.md §1-§2 eşikleri -> trade plan.
NEUTRAL sinyalde emir yok; BUY/SELL sinyalinde logistic(k=4) boyutlandırma.

Çalıştırma:  python3 atk/signal_to_trade.py
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "engine" / "data" / "snapshot.json"
LOGS = Path(__file__).resolve().parent / "logs"

K_LOGISTIC = 4.0          # POLICY.md §2
ENTER = 0.50              # POLICY.md §1 giriş eşiği
INST = "BTC-USDT"
MAX_PLAN_USD = 9_500.0    # günlük güvenli tavan (vault ile uyumlu)


def logistic(x, k=K_LOGISTIC):
    return 1.0 / (1.0 + math.exp(-k * x))


def log(rec):
    LOGS.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds")
                   .replace("+00:00", "Z"))
    with (LOGS / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def plan_from_snapshot(snap):
    comp = snap.get("composite") or 0.0
    label = snap.get("signal") or "NÖTR"
    if label == "NÖTR":
        return {"action": "NO_ORDER", "reason": "NEUTRAL label (POLICY §2: hold)",
                "composite": comp}
    side = "buy" if comp > ENTER else "sell"
    size_frac = min(0.95, max(0.05, logistic(comp)))
    return {
        "action": "PLACE_ORDER",
        "inst": INST,
        "side": side,
        "size_frac": round(size_frac, 4),
        "notional_usd_cap": round(size_frac * MAX_PLAN_USD, 2),
        "composite": comp,
        "label": label,
        "policy": "POLICY.md v1 (Schmitt ±0.50/±0.40, logistic k=4)",
    }


def main():
    if not SNAPSHOT.exists():
        raise SystemExit(f"snapshot yok: {SNAPSHOT} — önce engine koşsun")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    plan = plan_from_snapshot(snap)
    log({"type": "SIGNAL_TO_TRADE", **plan})
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
