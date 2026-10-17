# POLICY.md — Kanonik Karar Politikası (v1)
**Tarih:** 2026-10-17 · **Statü:** OTORİTER — sinyal etiketi ve pozisyon boyutlandırma
kararlarının TEK kanonik tanımı bu dosyadadır. WEIGHTING.md yalnızca ağırlık
spec'idir; multitimeframe_signals.md'deki 3×3 matris bu politikanın
discretizasyonudur, ayrı bir kural değildir.
**Tüketiciler:** engine/engine.py (sinyal etiketi), vault/agent_loop.py (icra
boyutlandırması), atk/ (demo — aynı kararı okur). Başka hiçbir dosya kendi
boyutlandırma/eşik kuralını tanımlayamaz.

## 1. Sinyal etiketi — Schmitt hysteresis (çift eşik)
composite = 0.5×LT + 0.5×ST üzerinden, önceki etiket state'ten okunur:

| Önceki etiket | Yeni etiket kuralı |
|---|---|
| herhangi | composite > **+0.50** → **AL** (giriş eşiği) |
| herhangi | composite < **−0.50** → **SAT** (giriş eşiği) |
| AL | composite ≤ **+0.40** → NÖTR'e düş (çıkış eşiği); > +0.40 → AL kal |
| SAT | composite ≥ **−0.40** → NÖTR'e çık; < −0.40 → SAT kal |
| NÖTR | ±0.50 bandı içinde → NÖTR kal |

Yani giriş ±0.50, çıkış ±0.40 — aradaki bant (±0.40…±0.50) önceki etiketi
korur (whipsaw koruması). engine.py bunu uygular; önceki etiket
`data/history/` arşivindeki en son snapshot'tan okunur.

## 2. Maruziyet (hedef exposure ∈ [0, 1])
- Etiket **NÖTR** iken **değişiklik YOK** (hold) — mevcut pozisyon korunur.
- Etiket **BUY (AL)** iken hedef = logistic(composite, k=4) ∈ **[0.5, 0.95]**.
- Etiket **SELL (SAT)** iken hedef = logistic(composite, k=4) ∈ **[0.05, 0.5]**.

logistic(x, k) = 1 / (1 + e^(−k·x)). Etiket bandı ile sürekli fonksiyon böyle
hizalanır: NÖTR bölgede agent işlem önermez; AL/SAT etiketi ancak ±0.50
üstünde aktifleşir ve logistic o bölgede zaten ~%50'nin doğru tarafındadır.

## 3. Çatışma ve türev kuralları (LT×ST işaret ayrışması — işaret bazlı)
- **LT+ / ST−** ("Correction"): hedef × **0.75** (kıs, ama hiç girmeme YOK —
  Gârleanu-Pedersen kısmi uyum).
- **LT− / ST−** (çift negatif): tek tam "kaç" hücresi → **tam çıkış, hedef = 0.05**.
- Çatışma koşulu bilinçli olarak **işaret (sign) bazlıdır** (lt>0 & st<0);
  ±0.5 bandı değil.

## 4. Rejim (makro) filtresi
- **regime_index < −0.5** (güçlü risk-off) → hedef × **0.5**.
- Rejim composite hesabına GİRMEZ (CONTRACT kuralı korunur); yalnızca
  maruziyet hedefini ölçekler.

## 5. Rebalance tetikleyicisi
Yeni hedef şu durumda icraya gider:
- **|hedef − mevcut| > 0.05**, VEYA
- **etiket değişimi** (AL↔NÖTR↔SAT geçişi).

Aksi HOLD. (Pozisyon-hysteresis'i budur; §1'deki sinyal-hysteresis'inden
ayrıdır — ikisi de tanımlı ve tek yerdedir.)

## 6. Hiyerarşi
ATK = demo/CEX vitrini; vault = ürün icrası; ikisi de aynı POLICY.md kararını
okur (ayrıntı: CONTRACT.md §Kanonik icra).
