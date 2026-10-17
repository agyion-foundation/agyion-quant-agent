"""
llm_commentary.py — Anthropic (Claude Haiku) tabanlı LLM yorum katmanı.

Public bültenin üst özet paragrafını snapshot'ın license=="clean" alanlarından
üretir (120-160 kelime, jargonsuz İngilizce, retail okuyucu). Tasarım ve kural
seti: research/newsletter_final_design.md §3 (sistem prompt, hallucination
kontrolü, onarım döngüsü, yayın kapısı).

Davranış sözleşmesi (newsletter.py ile):
  generate_commentary(snap) -> str | None
    * None dönerse bülten deterministik şablonla (fallback) çıkar — LLM metni
      ASLA doğrulama kapısını geçmeden yayınlanmaz.
    * Key yok, ağ kapalı veya API hatası -> graceful skip (None), hata loglanır.

Doğrulama kapısı (deterministik, 2 katman):
  1. Sayısal: çıktıdaki her sayı, LLM'e gönderilen temiz payload'daki bir
     değere tolerans içinde eşleşmeli (yuvarlama payı).
  2. Yasaklı kelime listesi: kazanç vaadi / kişisel tavsiye / fiyat kehaneti.

Maliyet: her gerçek API çağrısında tokens + model + tahmini USD ->
logs/llm_cost.jsonl (append-only).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from fetchers import load_key, _safe_err

# ---------------------------------------------------------------- config
MODEL_ID = "claude-haiku-4-5"          # tek yerden; model emekliliğinde burayı değiştir
TEMPERATURE = 0.2
MAX_TOKENS = 400
TIMEOUT = 30
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Haiku 4.5 liste fiyatı ($ / 1M token) — maliyet logundaki USD tahmini için.
PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00

WORD_RANGE = (120, 160)

ROOT = Path(__file__).resolve().parent.parent
COST_LOG = ROOT / "logs" / "llm_cost.jsonl"

# Sistem promptu: research/newsletter_final_design.md §3.2 taslağından
# uyarlandı (bizim snapshot şeması: long_term/short_term/regime/composite +
# clean bileşenler).
SYSTEM_PROMPT = """You are the commentary writer for a daily crypto quant index newsletter read by
retail investors. Every day you receive one JSON snapshot containing the day's
index values and signal-driving components. Your only job: turn those numbers
into a short, clear, honest plain-English read.

HARD RULES (violating any of these fails the issue):
1. Use ONLY numbers that appear in the provided JSON snapshot. Never invent,
   estimate, round differently, or recall any price, percentage, date, or level
   from your training knowledge. If a number is not in the JSON, do not state it.
2. Never predict future prices. Describe what the data shows and what the
   signals indicate — not what "will" happen. Allowed: "the index moved into
   BUY territory". Forbidden: "BTC will rally", "expect a breakout".
3. Never mention news, events, people, companies, or catalysts that are not in
   the JSON. No macro events, no ETF flows. Nothing external.
4. Do not give personalized advice. No "you should buy/sell". Describe the
   system's signals in third person ("the model", "the index").
5. Do not use the words "guarantee", "certain", "sure", "risk-free", or
   "can't lose".

STYLE:
- 120-160 words, 2-3 short paragraphs: (1) what the long-term, short-term and
  macro regime gauges read today and what the composite signal is, (2) which
  components drove the reading and what that means in plain words.
- Plain English for non-professionals. Short sentences. No jargon without a
  gloss ("funding rate — the fee leveraged longs pay shorts — ...").
- Neutral, factual tone. No hype, no fear, no emojis, no exclamation marks.
- Refer to signals exactly as given: BUY / NEUTRAL / SELL.
- Index scores run from -1.0 to +1.0; +0.5 and -0.5 are the BUY/SELL
  thresholds (you may cite these threshold numbers).

OUTPUT FORMAT: plain text only, no markdown headers, no bullet lists,
no preamble. Just the commentary body."""

# Katman-2 yasaklı kelime/kalıp listesi (küçük harfe indirgenmiş metinde aranır).
BANNED_PATTERNS = [
    r"\bguarantee(?:d|s)?\b", r"\brisk[\s-]?free\b", r"\bcan'?t lose\b",
    r"\bsure thing\b", r"\bcertain(?:ly)? (?:to|that) (?:rise|fall|pump|dump)\b",
    r"\bwill (?:rally|pump|moon|crash|surge|soar|break ?out)\b",
    r"\bexpect (?:a )?(?:breakout|rally|pump|crash|surge)\b",
    r"\byou should (?:buy|sell|hold|invest)\b",
    r"\bfinancial advice\b", r"\ball[ -]?in\b",
]

# Doğrulamada serbest bırakılan yapısal sabitler (eşikler, skala, gün sayıları).
ALLOWED_CONSTANTS = {0.5, -0.5, 0.4, -0.4, 1.0, -1.0, 0.0}

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")


# ---------------------------------------------------------------- payload
def _round(v, nd=4):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def build_public_payload(snap):
    """LLM'e giden temiz görünüm: yalnız endeksler/sinyal + license=='clean'
    bileşenler (isim, skor, ağırlık, açıklama notu). cq ham serileri, kaynak
    adları ve hata detayları BURAYA GİRMEZ (F1 lisans kuralı)."""
    from newsletter import NAME_TR, signal_en
    comps = [c for c in snap.get("components", []) if c.get("license") == "clean"]
    return {
        "date": snap.get("generated_at", "")[:10],
        "indices": {
            "long_term": _round(snap.get("long_term_index")),
            "short_term": _round(snap.get("short_term_index")),
            "macro_regime": _round(snap.get("regime_index")),
            "composite": _round(snap.get("composite")),
        },
        "signal": signal_en(snap.get("signal", "")),
        "signal_rule": "BUY above +0.5, SELL below -0.5, hysteresis band at ±0.4",
        "components": [
            {
                "name": NAME_TR.get(c["name"], c["name"]),
                "layer": c.get("layer"),
                "raw": _round(c.get("raw"), 6),
                "score": _round(c.get("normalized")),
                "weight": _round(c.get("weight")),
                "note": c.get("note", ""),
            }
            for c in comps
        ],
    }


def _allowed_numbers(payload):
    """Payload'daki tüm numerik değerler + note metinlerindeki sayılar +
    yapısal sabitler = doğrulamanın izin kümesi."""
    vals = set(ALLOWED_CONSTANTS)

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.add(float(x))
        elif isinstance(x, str):
            for m in _NUM_RE.findall(x):
                t = m.rstrip("%").replace(",", "")
                try:
                    vals.add(float(t))
                except ValueError:
                    pass
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(payload)
    return vals


# ------------------------------------------------------------- validation
def validate_output(text, payload):
    """Doğrulama kapısı. (ok, problems) döner; problems insan-okunur liste
    (onarım denemesinde LLM'e feedback olarak verilir)."""
    problems = []
    if not text or not text.strip():
        return False, ["empty output"]
    words = len(text.split())
    if not (WORD_RANGE[0] - 30) <= words <= (WORD_RANGE[1] + 40):
        # kelime sayısı uyarısı sert fail değil: onarım denemesine taşınır
        problems.append(f"word count {words} outside target {WORD_RANGE[0]}-{WORD_RANGE[1]}")
    low = text.lower()
    for pat in BANNED_PATTERNS:
        if re.search(pat, low):
            problems.append(f"banned phrase matched: {pat}")
    if "!" in text:
        problems.append("exclamation mark (hype tone)")
    allowed = _allowed_numbers(payload)

    def ok(num):
        return any(abs(num - a) <= max(0.015, 0.02 * abs(a)) for a in allowed)

    for m in _NUM_RE.findall(text):
        t = m.rstrip("%").replace(",", "")
        try:
            num = float(t)
        except ValueError:
            continue
        if not ok(num):
            problems.append(f"number {m} not found in snapshot data")
    return not problems, problems


# ------------------------------------------------------------------ API
def _log_cost(model, usage, attempt, status):
    """Her gerçek API çağrısı: tokens + model + tahmini USD -> jsonl append."""
    try:
        COST_LOG.parent.mkdir(parents=True, exist_ok=True)
        inp = int(usage.get("input_tokens", 0))
        out = int(usage.get("output_tokens", 0))
        usd = (inp * PRICE_INPUT_PER_MTOK + out * PRICE_OUTPUT_PER_MTOK) / 1e6
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": model, "attempt": attempt, "status": status,
            "input_tokens": inp, "output_tokens": out,
            "est_cost_usd": round(usd, 6),
        }
        with COST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # log yazılamazsa yayın akışını bozma


def _call_api(key, system, user):
    """Tek Anthropic Messages çağrısı. (text, usage) döner; hata fırlatır."""
    r = requests.post(
        API_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": MODEL_ID,
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    text = "".join(b.get("text", "") for b in j.get("content", [])
                   if b.get("type") == "text")
    return text.strip(), j.get("usage", {})


# ------------------------------------------------------------------ main
def generate_commentary(snap, key=None):
    """Public bülten özeti üret. Doğrulanamazsa/hata olursa None (fallback).

    Akış: payload -> 1. çağrı -> doğrulama -> fail ise 1 onarım denemesi
    (feedback'li) -> yine fail ise None. Ağ/API hatası: graceful skip (None).
    """
    key = key if key is not None else load_key("ANTHROPIC_API_KEY")
    if not key:
        print("llm_commentary: ANTHROPIC_API_KEY yok — deterministik fallback kullanılacak")
        return None
    payload = build_public_payload(snap)
    if not payload["components"] or payload["indices"]["composite"] is None:
        print("llm_commentary: clean bileşen/endeks yok — fallback")
        return None
    user = ("Here is today's snapshot. Write today's commentary following the rules.\n\n"
            "<snapshot>\n" + json.dumps(payload, indent=2, ensure_ascii=False)
            + "\n</snapshot>")
    feedback = ""
    for attempt in (1, 2):
        try:
            text, usage = _call_api(key, SYSTEM_PROMPT, user + feedback)
            _log_cost(MODEL_ID, usage, attempt, "ok")
        except Exception as e:
            # Sandbox'ta api.anthropic.com kapalı olabilir -> graceful skip.
            # Key hata metnine sızmasın diye _safe_err pattern'i.
            _log_cost(MODEL_ID, {}, attempt, f"error: {_safe_err(e)}")
            print(f"llm_commentary: API çağrısı başarısız (attempt {attempt}): {_safe_err(e)}")
            return None
        ok, problems = validate_output(text, payload)
        if ok:
            if attempt == 2:
                print("llm_commentary: onarım denemesiyle doğrulandı")
            return text
        if attempt == 1:
            feedback = ("\n\nYour previous draft FAILED validation. Problems: "
                        + "; ".join(problems[:6])
                        + ". Rewrite strictly following the rules; use only "
                        "numbers from the snapshot.")
        else:
            print(f"llm_commentary: doğrulama 2 kez başarısız ({problems[:3]}) "
                  "— yayınlanmıyor, fallback kullanılacak")
    return None
