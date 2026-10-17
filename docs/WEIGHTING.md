# WEIGHTING.md — Endeks Ağırlıklandırma Spec'i (v1)
**Tarih:** 2026-10-17 · **Bağlam:** 170+ ekran CQ taraması + literatür taraması (multitimeframe_signals.md) + dashboard scan'leri A-E

## Prensipler
1. Eşit ağırlık YOK — sinyal gücü eşit değil (kullanıcı kuralı).
2. Ama v1'de kanıt da yok (backtest henüz yok) → **uzman ağırlığı başlangıç + kanıtla evrim**.
3. Ağırlık üç çarpandan oluşur: **güven (kanıt sınıfı) × özgüllük (korelasyon dedup) × tazelik (veri hızı/uygunluğu)**.
4. Her ağırlık değişikliği versiyonlanır (weights_v1 → v2...), snapshot'a yazılır. Jüri sorusu: "ağırlıklar şeffaf ve sürümlü."

## Çarpan 1 — Kanıt sınıfı (base weight)
Literatür + CQ community kullanım yoğunluğu + döngü kanıtına göre:

| Sınıf | Base | Örnekler |
|---|---|---|
| S — döngü-kanıtlı çekirdek | 1.5-2.0 | MVRV-Z, NUPL, aSOPR, funding |
| A — güçlü tamamlayıcı | 1.0-1.5 | OI değişimi, liq asimetrisi, realized price dist, hash ribbon, CVD |
| B — bağlam/teyit | 0.5-1.0 | F&G, premium index'ler, TRX/XRP momentum, spot-perp gap |
| C — deneysel | 0.25-0.5 | yeni eklenen her şey varsayılan C'den başlar, kanıtla yükselir |

## Çarpan 2 — Korelasyon dedup (özgüllük)
- Aynı bilgiyi ölçen metrikler kümelenir, küme toplam ağırlığı sabit tutulur:
  - **Değerleme kümesi:** MVRV-Z + NUPL + realized_price_dist + BCI (hepsi realized cap'ten) → küme toplamı ≤ LT'nin %40'ı
  - **Kaldıraç kümesi:** funding + OI + liq + ELR → ST'nin ≤ %50'si
  - **Talep kümesi:** F&G + CVD + premium → ST'nin ≤ %30'u
- Kural: yeni metrik eklerken küme eşiğini aşarsa, küme içi yeniden dağıt (toplam sabit).

## Çarpan 3 — Tazelik/veri kalitesi
- Saatlik veri ×1.0, günlük ×0.9, haftalık ×0.7 (ST katmanında); LT'de tersine ceza yok.
- Kaynak kesintisi >48s → bileşen o koşuda düşer, ağırlığı hayatta kalanlara **yeniden normalize** edilir (engine zaten yapıyor; CONTRACT kuralı).

## Hesap
```
final_weight_i = base_i × dedup_i × freshness_i
normalize: Σw = 1.0 katman başına
index = Σ (norm_i × w_i)
```

## Evrim yol haritası (kayıtlı taahhüt)
- **v1 (bugün):** uzman ağırlığı — yukarıdaki sınıflar, elle.
- **v2 (30-60 gün veri sonrası):** IC-weighting — her bileşenin günlük değeri × ertesi 7g BTC getirisi korelasyonu (Information Coefficient); w_i ∝ max(0, IC_i). Kümeler korunur.
- **v3:** regime-conditional weights (Breaking Bad Trends 2024: çatışma sıklığı +1σ → global risk azalt).

## LT×ST birleşimi (literatür-onaylı, multitimeframe_signals.md)
- composite = 0.5×LT + 0.5×ST (Hurst 2017 eşit ağırlık baz; Goulding 2023 barbell yeterli)
- Çatışma kuralı (LT+ / ST− "Correction", **işaret-bazlı**): maruziyet ×0.75 + DCA ile ST düzelmesini yakala; hiç girmeme YOK (Gârleanu-Pedersen kısmi uyum) — icra parametreleri POLICY.md §3
- LT−/ST− = tek tam "kaç" hücresi (Martone 2026 AND-gate: çift-negatif −%1.36/ay) → tam çıkış 0.05 — POLICY.md §3
- Rejim katmanı composite'a girmez; maruziyet etkisi (regime_index < −0.5 → hedef ×0.5) POLICY.md §4'te tanımlıdır

## v1 uzman ağırlıkları (engine ile hizalı)
LT (Σ=1.0): mvrv_z .16, nupl .13, bci .12, realized_dist .11, sopr_family .12, growth_diff .08, hash_ribbon .08, dma200 .10, supply_profit(BGeometrics) .06, stable_momentum .04
ST (Σ=1.0): funding_btc .12, funding_eth .08, oi_btc .10, oi_eth .06, liq_asym .12, cvd .12, premium_cb .08, premium_kr .06, spot_perp_gap .06, fear_greed .08, momentum .06, squeeze .06
(XRP üçlüsü C sınıfı .25×base ile girer; rejim katmanı composite'a girmez, ayrı raporlanır.)
