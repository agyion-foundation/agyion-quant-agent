"""
normalize.py — Ham metrikleri [-1, +1] aralığına normalize eder.

Kural: pozitif = bullish, negatif = bearish. Tüm çıktılar [-1, +1] içine
clip edilir. Tarihsel serisi olan metriklerde z-score kullanılır
(z / Z_FULL ölçeklenir, yani z = ±Z_FULL -> ±1). Seri yoksa aşağıda
her eşiği yorumla dokümante edilmiş sezgisel bantlar kullanılır.
"""

import math

Z_FULL = 2.0  # z-score = ±2 -> ±1 kabulü (istatistiksel olarak ~%95 kuyruk)


def clip(x):
    """Her normalize çıktısının geçtiği son filtre: [-1, +1]."""
    if x is None:
        return None
    return max(-1.0, min(1.0, float(x)))


def _zscore(series, latest):
    if not series or len(series) < 30:
        return None
    n = len(series)
    mean = sum(series) / n
    var = sum((v - mean) ** 2 for v in series) / n
    std = var ** 0.5
    if std == 0:
        return 0.0
    return (latest - mean) / std


# ------------------------------------------------- momentum (seri varsa z)
def norm_momentum_z(series, latest):
    """Momentum metriği: tarihsel ortalamanın üstü = bullish.
    Ör: BTC fiyatı / 200DMA oranının son 1 yıllık dağılımına göre z-score."""
    z = _zscore(series, latest)
    return None if z is None else clip(z / Z_FULL)


def norm_mean_reversion_z(series, latest):
    """Ortalamaya dönüş metriği: tarihsel ortalamanın ÇOK üstü = aşırı
    değerlenme = bearish (işaret terslenir). Ör: MVRV, NUPL, SOPR.
    Not: bu metrikler cycle zirvelerinde doygunluğa işaret eder; uzun vade
    endeksinde 'pahalı = risk' olarak yorumlanır."""
    z = _zscore(series, latest)
    return None if z is None else clip(-z / Z_FULL)


# ----------------------------------------- sezgisel bantlar (seri yoksa)
def norm_band(value, band):
    """Lineer bant: |value| >= band -> ±1. Ör: norm_band(0.10, 0.20) = +0.5."""
    if value is None or band <= 0:
        return None
    return clip(value / band)


def norm_funding(rate):
    """Funding rate (8s'lik, ondalık). OKX/Binance'ta |funding| > %0.05
    aşırı kalabalık pozisyonlanma sayılır -> ±1. %0.01 (tipik nötr) ~ 0.2.
    Pozitif funding = long'lar ödüyor = bullish kalabalık -> pozitif."""
    return norm_band(rate, 0.0005)


def norm_oi_change(pct):
    """OI 24s değişimi (ondalık). Günde ±%20 OI değişimi ekstrem kabul
    edilir -> ±1. OI artışı = yeni para/kaldıraç girişi = bullish."""
    return norm_band(pct, 0.20)


def norm_price_momentum_24h(pct):
    """24s fiyat değişimi (ondalık). BTC/ETH/AVAX ortalaması ±%10'luk
    günlük hareket ekstrem -> ±1."""
    return norm_band(pct, 0.10)


def norm_tvl_momentum_30d(pct):
    """30g TVL momentumu (ondalık). Ayda ±%20 TVL değişimi güçlü trend
    sayılır -> ±1."""
    return norm_band(pct, 0.20)


def norm_stable_mcap_momentum(pct):
    """30g stablecoin mcap momentumu (ondalık). Stablecoin arzı yavaş
    hareket eder; ayda ±%10 değişim güçlü sermaye girişi/çıkışı -> ±1."""
    return norm_band(pct, 0.10)


def norm_liq_asymmetry(long_usd, short_usd):
    """Likidasyon asimetrisi (24s USD toplamları).

    asym = (long - short) / (long + short) zaten [-1, +1] aralığında.
    Kısa vade mantığı: long likidasyonu baskınsa (asym > 0) long'lar
    kaldıraçla sıkışmış ve satışla temizleniyor demektir -> fiyat baskı
    altında -> BEARISH short-term sinyal (işaret terslenir: -asym).
    Karşıt yorum (bilgi notu): AŞIRI long temizliği (asym -> +1) çoğu
    kez kapitülasyon/dip yakınlığına işaret eder; bu uzun vadeli bir
    sinyaldir ve bu bileşenin short-term kapsamı dışındadır — burada
    sadece anlık baskı yönü kodlanır. Her iki taraf da sıfırsa None.
    """
    total = (long_usd or 0) + (short_usd or 0)
    if total <= 0:
        return None
    asym = (long_usd - short_usd) / total
    return clip(-asym)


def norm_mvrv_fallback(value):
    """MVRV için seri yoksa cycle bandı: MVRV <= 1.0 tarihsel alım bölgesi
    (+1), >= 3.5 tarihsel balon bölgesi (-1), arası lineer."""
    if value is None:
        return None
    return clip((1.9 - value) / 0.9)  # 1.0 -> +1, 2.8 -> -1, ~1.9 -> 0


# ------------------------------- CryptoQuant capitalization türevleri (v0.2)
def norm_tanh_z(z, scale=2.0):
    """tanh yumuşatmalı z benzeri skor: tanh(z/scale). z = ±scale civarında
    ±0.76'ya doyurulur; uç değerlerde sert clip yerine yumuşak doygunluk.
    Ör: MVRV-Z = (market_cap - realized_cap) / std(market_cap, 365g).
    Yüksek MVRV-Z = aşırı değerlenme -> işaret terslenir (pahalı = risk)."""
    if z is None:
        return None
    return clip(-math.tanh(z / scale))


def norm_nupl_band(value):
    """NUPL = (market_cap - realized_cap) / market_cap.
    <= 0 (kapitülasyon bölgesi) -> +1, >= 0.75 (öfori bölgesi) -> -1,
    arası lineer. Uzun vade: aşırı kâr = dağıtım riski = bearish."""
    if value is None:
        return None
    return clip((0.375 - value) / 0.375)


def norm_bci_momentum(diff):
    """Bull-Bear Cycle Indicator yaklaşımı: MA30(MVRV) - MA365(MVRV).
    Pozitif = kısa vadeli değerlenme trendi uzun vadenin üstünde = boğa
    fazı. ±0.5 fark ekstrem rejim sayılır -> ±1."""
    return norm_band(diff, 0.5)


def norm_realized_price_dist(dist):
    """(price - realized_price)/realized_price. Fiyat realized price'a
    yaklaştıkça birikim bölgesi (bullish), çok üstüne çıktıkça ısınma
    (bearish). Tarihsel medyan ~ +0.7; orası nötr kabul edilir:
    dist = 0 -> +1, dist = 1.4 -> -1, arası lineer."""
    if dist is None:
        return None
    return clip((0.7 - dist) / 0.7)


def norm_growth_rate_diff(diff):
    """30g market_cap değişimi - 30g realized_cap değişimi (ondalık fark).
    Market cap realized cap'ten hızlı büyüyorsa spekülatif para girişi
    baskın = bullish momentum. ±%15 fark ekstrem -> ±1."""
    return norm_band(diff, 0.15)


# --------------------------------------------------- ücretsiz kaynaklar (v0.2)
def norm_fear_greed(value):
    """Fear & Greed Index (0-100) -> [-1,+1]: (v - 50) / 50.
    50 nötr; 100 aşırı hırs = +1, 0 aşırı korku = -1 (kontra yorum
    yapılmaz; bu bileşen anlık sentiment yönünü kodlar)."""
    if value is None:
        return None
    return clip((value - 50.0) / 50.0)


def norm_momentum_7d(pct):
    """7g fiyat getirisi (ondalık). Altcoinlerde haftada ±%25 ekstrem
    hareket sayılır -> ±1."""
    return norm_band(pct, 0.25)


def norm_hash_ribbon(rel):
    """Hash ribbon: hashrate MA30 / MA60 - 1 (ondalık). Pozitif = kısa
    ortalama uzun ortalamanın üstünde = madenci kapitülasyonu sonrası
    toparlanma = bullish. ±%5 sapma ekstrem -> ±1."""
    return norm_band(rel, 0.05)


def norm_taker_slope(slope):
    """Binance taker long/short ratio 7g eğimi (gün başına oran değişimi).
    Pozitif eğim = agresif alıcı eğilimi artıyor = bullish kısa-vade.
    Günde ±0.05 eğim ekstrem -> ±1."""
    return norm_band(slope, 0.05)


def norm_spot_perp_gap(gap):
    """(perp - spot) / spot (ondalık). Pozitif gap = perp primi = long
    talebi; aşırı pozitif prim ısınma işareti olsa da kısa vadede yön
    talebi gösterir -> pozitif kodlanır. ±%0.2 gap ekstrem -> ±1."""
    return norm_band(gap, 0.002)


def norm_macro_change(change, band):
    """FRED rejim serisi 30g değişimi. Hepsi risk-off yönünde bearish
    kodlanır (işaret tersli): VIX +8 puan, DGS10 +0.5 puan, dolar
    endeksi +%3 -> -1."""
    n = norm_band(change, band)
    return None if n is None else clip(-n)


def norm_cb_premium(pct_val):
    """Coinbase Premium Index (% birimi, ör. -0.137 = %-0.137). Pozitif =
    ABD kurumsal/spot talebi baskın = bullish. ±%0.3 ekstrem -> ±1."""
    return norm_band(pct_val, 0.3)


def norm_kr_premium(pct_val):
    """Korea Premium Index (kimchi premium, % birimi, ör. 0.97 = %0.97).
    Pozitif = Kore perakende talebi = bullish; aşırısı (>%5) tarihsel
    ısınma işareti ama kısa vadede talep yönü kodlanır. ±%5 -> ±1."""
    return norm_band(pct_val, 5.0)


def norm_ratio_dev(rel):
    """Oranın kendi ortalamasından göreli sapması (ondalık, ör. ETH/BTC OI
    oranı vs 90g ortalama). Pozitif = oran ortalamanın üstünde = ETH lehine
    rotasyon. ±%20 sapma ekstrem -> ±1."""
    return norm_band(rel, 0.20)
