#!/usr/bin/env python3
"""signal_to_trade.py — snapshot.json -> ATK çağrı planı.

engine'in ürettiği composite sinyali OKX Agent Trade Kit (ATK) çağrılarına çevirir:

  composite >  +0.5  ->  AL  : BTC-USDT demo market alım planı (quote USDT)
  composite <  -0.5  ->  SAT : BTC-USDT demo market satış planı (base BTC)
  aksi               ->  NÖTR: emir yok; sadece ATK ile fiyat doğrulaması

Her koşuda:
  1) snapshot okunur (data/snapshot.json; yedek: engine/data/snapshot.json)
  2) ATK üzerinden BTC-USDT fiyatı doğrulanır (market_get_ticker)
  3) Sinyal yönündeyse demo_market_order planı/çağrısı yapılır (spot_place_order)
  4) Karar kaydı atk/logs/decisions.jsonl'e eklenir, plan atk/logs/ altına yazılır

Kullanım:
  python3 signal_to_trade.py              # plan + (anahtar varsa) demo execution
  python3 signal_to_trade.py --dry-run    # sadece plan, ATK çağrısı yok
  python3 signal_to_trade.py --size 25    # alım büyüklüğü (USDT)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ATK_DIR = Path(__file__).resolve().parent
REPO = ATK_DIR.parent
SNAPSHOT_CANDIDATES = [
    REPO / "data" / "snapshot.json",
    REPO / "engine" / "data" / "snapshot.json",
]
LOGS = ATK_DIR / "logs"
DECISIONS_LOG = LOGS / "decisions.jsonl"

BUY_THRESHOLD = 0.5
SELL_THRESHOLD = -0.5
DEFAULT_SIZE_USDT = "10"     # demo alım: quote cinsinden
DEFAULT_SELL_BTC = "0.0001"  # demo satış: base cinsinden
INSTRUMENT = "BTC-USDT"

# snapshot "signal" değeri Türkçe/İngilizce olabilir
SIGNAL_ALIASES = {
    "AL": "BUY", "BUY": "BUY",
    "SAT": "SELL", "SELL": "SELL",
    "NÖTR": "NEUTRAL", "NOTR": "NEUTRAL", "NEUTRAL": "NEUTRAL",
}


def load_snapshot() -> dict:
    for p in SNAPSHOT_CANDIDATES:
        if p.exists():
            snap = json.loads(p.read_text(encoding="utf-8"))
            snap["_path"] = str(p)
            return snap
    raise FileNotFoundError(
        "snapshot.json bulunamadı: " + ", ".join(str(p) for p in SNAPSHOT_CANDIDATES))


def decide(composite: float) -> str:
    if composite > BUY_THRESHOLD:
        return "BUY"
    if composite < SELL_THRESHOLD:
        return "SELL"
    return "NEUTRAL"


def build_plan(snapshot: dict, size_usdt: str = DEFAULT_SIZE_USDT) -> dict:
    composite = float(snapshot.get("composite") or 0.0)
    raw_signal = str(snapshot.get("signal", "")).upper()
    signal = SIGNAL_ALIASES.get(raw_signal, decide(composite))
    # Sinyal ile eşik çakışırsa composite esas alınır (tek doğruluk kaynağı)
    decided = decide(composite)
    conflict = signal != decided

    plan: dict = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "snapshot_path": snapshot.get("_path"),
        "snapshot_time": snapshot.get("generated_at"),
        "composite": composite,
        "snapshot_signal": raw_signal,
        "decided_signal": decided,
        "signal_conflict": conflict,
        "instrument": INSTRUMENT,
        "atk_calls": [
            {"fn": "get_price", "atk_tool": "market_get_ticker",
             "module": "market", "args": {"instId": INSTRUMENT},
             "purpose": "sinyal fiyat doğrulaması (CryptoQuant fiyatı ile çapraz kontrol)"},
        ],
    }
    if decided == "BUY":
        plan["atk_calls"].append({
            "fn": "demo_market_order", "atk_tool": "spot_place_order",
            "module": "spot",
            "args": {"instId": INSTRUMENT, "tdMode": "cash", "side": "buy",
                     "ordType": "market", "sz": size_usdt,
                     "tgtCcy": "quote_ccy"},
            "purpose": f"composite {composite:+.4f} > +0.5 — {size_usdt} USDT demo alım"})
    elif decided == "SELL":
        plan["atk_calls"].append({
            "fn": "demo_market_order", "atk_tool": "spot_place_order",
            "module": "spot",
            "args": {"instId": INSTRUMENT, "tdMode": "cash", "side": "sell",
                     "ordType": "market", "sz": DEFAULT_SELL_BTC,
                     "tgtCcy": "base_ccy"},
            "purpose": f"composite {composite:+.4f} < -0.5 — {DEFAULT_SELL_BTC} BTC demo satış"})
    else:
        plan["note"] = (f"composite {composite:+.4f} ∈ [-0.5, +0.5] — NÖTR, "
                        "emir planlanmadı; yalnızca ATK fiyat doğrulaması yapılır.")
    plan["atk_calls"].append({
        "fn": "get_positions", "atk_tool": "account_get_positions",
        "module": "account", "args": {},
        "purpose": "işlem sonrası pozisyon/bakiye teyidi"})
    return plan


def log_decision(record: dict) -> None:
    LOGS.mkdir(exist_ok=True)
    with DECISIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(dry_run: bool = False, size_usdt: str = DEFAULT_SIZE_USDT) -> dict:
    snapshot = load_snapshot()
    plan = build_plan(snapshot, size_usdt)
    result: dict = {"plan": plan, "execution": []}

    if not dry_run:
        from atk_bridge import AtkBridge
        bridge = AtkBridge()
        try:
            price_res = bridge.get_price(INSTRUMENT)
            result["execution"].append({
                "fn": "get_price", "ok": price_res["entry"]["ok"],
                "transport": price_res["entry"]["transport"],
                "price": price_res["price"]})
            cq_price = (snapshot.get("metrics_raw", {})
                        .get("cryptoquant", {}).get("btc_price"))
            if price_res["price"] and cq_price:
                dev = abs(price_res["price"] - cq_price) / cq_price
                result["price_crosscheck"] = {
                    "atk_price": price_res["price"],
                    "engine_cryptoquant_price": cq_price,
                    "deviation_pct": round(dev * 100, 3),
                    "within_tolerance": dev < 0.02}
            if plan["decided_signal"] in ("BUY", "SELL"):
                side = "buy" if plan["decided_signal"] == "BUY" else "sell"
                size = size_usdt if side == "buy" else DEFAULT_SELL_BTC
                order = bridge.demo_market_order(INSTRUMENT, side, size)
                result["execution"].append({
                    "fn": "demo_market_order", "ok": order["ok"],
                    "transport": order["transport"],
                    "side": side, "size": size,
                    "response": order["response"]})
            pos = bridge.get_positions()
            result["execution"].append({
                "fn": "get_positions", "ok": pos["ok"],
                "transport": pos["transport"]})
        finally:
            bridge.close()
    else:
        result["execution"] = [{"dry_run": True,
                                "note": "ATK çağrıları atlandı"}]

    log_decision({
        "ts": plan["generated_at"], "composite": plan["composite"],
        "signal": plan["decided_signal"], "snapshot_signal": raw_signal_norm(plan),
        "dry_run": dry_run,
        "n_atk_calls_planned": len(plan["atk_calls"]),
        "executed": [e.get("fn") for e in result["execution"] if e.get("fn")],
        "price": next((e.get("price") for e in result["execution"]
                       if e.get("fn") == "get_price"), None),
    })
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    plan_file = LOGS / f"trade_plan_{ts}.json"
    tmp = plan_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(plan_file)  # atomik
    result["plan_file"] = str(plan_file)
    result["decisions_log"] = str(DECISIONS_LOG)
    return result


def raw_signal_norm(plan: dict) -> str:
    return plan.get("snapshot_signal", "")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    size = DEFAULT_SIZE_USDT
    if "--size" in sys.argv:
        size = sys.argv[sys.argv.index("--size") + 1]
    out = run(dry_run=dry, size_usdt=size)
    print(json.dumps(out, ensure_ascii=False, indent=2))
