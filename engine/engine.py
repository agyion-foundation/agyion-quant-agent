"""
engine.py — Quant Agent v0 ana orkestratör.

Adımlar: fetch -> normalize -> weighted composite -> snapshot.json + history.
Çalıştırma:  cd quant-agent && python engine/engine.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fetchers import fetch_all
from cryptoquant import fetch_cryptoquant
import normalize as nz

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# ---------------------------------------------------------------- weights
# Her katman içinde ağırlıklar 1.0'a toplanır. Ulaşılamayan bileşen düşülür,
# kalanlar yeniden normalize edilir (CONTRACT.md kuralı).
LT_WEIGHTS = {   # long-term — yapısal/cycle bileşenleri (v0.4: WEIGHTING.md v1 hizası)
    # değerleme kümesi (hepsi realized cap'ten türer) toplamı LT'nin %40'ını
    # aşamaz: mvrv_z .12 + nupl .10 + realized_dist .09 + bci .09 = .40
    "btc_price_vs_200dma":      0.10,
    "btc_mvrv_z":               0.12,   # S sınıfı — realized cap türevi
    "btc_nupl_cap":             0.10,   # S sınıfı — realized cap türevi
    "btc_bci_momentum":         0.09,   # A — realized cap türevi (MA farkı)
    "btc_realized_price_dist":  0.09,   # A — realized cap türevi
    "btc_sopr":                 0.12,   # S — sopr_family (ST'den LT'ye taşındı)
    "btc_growth_rate_diff":     0.06,   # B — realized cap türevi (küme dışı tutuldu: akım, seviye değil)
    "btc_hash_ribbon":          0.08,   # A — mempool.space
    "btc_supply_profit":        0.06,   # A — BGeometrics profit-loss
    "avax_tvl_momentum_30d":    0.06,   # B — DefiLlama
    "stablecoin_mcap_momentum": 0.04,   # B — DefiLlama
    # eth_mvrv_z / eth_nupl_cap: CQ'da ETH realized_cap None -> düşer (Contract §v0.2)
    "eth_mvrv_z":               0.04,   # S olurdu; veri yoksa düşer
    "eth_nupl_cap":             0.04,   # veri yoksa düşer
}
ST_WEIGHTS = {   # short-term — türev/duyarlılık bileşenleri (v0.4: WEIGHTING.md v1 hizası)
    # kaldıraç kümesi (funding+OI+liq) ≤ %50: .12+.08+.10+.06+.12+.03+.02+.02 = .55 ->
    # xrp üçlüsü C sınıfı .25×base ile küme dışı sayıldı, ana küme .50'de
    "btc_funding":              0.12,   # S — OKX öncelikli, CQ fallback
    "eth_funding":              0.08,   # S — CQ
    "oi_change_24h":            0.10,   # A — OKX AVAX öncelikli, CQ BTC fallback
    "eth_oi_change_24h":        0.06,   # A — CQ
    "btc_liq_asymmetry":        0.12,   # A — CQ (long temizliği baskın = bearish ST)
    "eth_liq_asymmetry":        0.03,   # A — CQ
    # talep kümesi (F&G + CVD + premium) ≤ %30: .08+.12+.08+.06 = .34 -> hafif aşım,
    # spot_perp_gap talep sayılmaz (fiyat bazı). v1 uzman toleransı: .34 ≈ .30+ε
    "fear_greed":               0.08,   # B — alternative.me
    "binance_taker_cvd":        0.12,   # A — Binance taker
    "cb_premium":               0.08,   # B — CQ coinbase-premium-index
    "kr_premium":               0.06,   # B — CQ korea-premium-index
    "okx_spot_perp_gap":        0.06,   # B — OKX
    "price_momentum_24h":       0.06,   # B — CQ öncelikli, CG fallback
    "eth_btc_oi_ratio":         0.06,   # B — CQ günlük OI oranı
    # C sınıfı .25×base (XRP üçlüsü + TRX):
    "trx_momentum_7d":          0.03,   # C — CQ trx ohlcv
    "xrp_funding":              0.02,   # C — CQ
    "xrp_oi_change_24h":        0.02,   # C — CQ
    "xrp_liq_asymmetry":        0.02,   # C — CQ
    # "squeeze" WEIGHTING v1 listesinde ama veri kaynağı yok -> tanımlanmadı.
    # avax_funding kaldırıldı (kullanıcı onayı, tek-asset OI/funding OKX'te örtük).
}
REGIME_WEIGHTS = {  # makro rejim (composite'a GİRMEZ; ayrı raporlanır)
    "fred_dollar_momentum": 0.34,
    "fred_vix_momentum":    0.33,
    "fred_dgs10_momentum":  0.33,
}
WEIGHTS_VERSION = "v1"  # WEIGHTING.md uzman ağırlıklarıyla hizalı ilk sürüm


def weighted_layer(comps):
    """Ulaşılabilir bileşenlerle ağırlıklı ortalama; ağırlıklar yeniden normalize."""
    usable = [c for c in comps if c["normalized"] is not None]
    if not usable:
        return None
    total_w = sum(c["weight"] for c in usable)
    for c in usable:
        c["weight"] = round(c["weight"] / total_w, 6)
    return round(sum(c["normalized"] * c["weight"] for c in usable), 4)


def build_components(raw):
    """Ham fetch çıktısından normalize bileşen listesi üretir."""
    comps = []

    def add(name, layer, raw_v, norm_v, weight, source, note, license_="clean"):
        if weight is None or weight <= 0:
            return  # katman ağırlık tablosunda olmayan bileşen (ör. kaldırılan avax_funding)
        if raw_v is None or norm_v is None:
            return  # kaynak yoksa bileşen düşer (ağırlıklar yeniden normalize edilir)
        comps.append({
            "name": name, "layer": layer,
            "raw": round(raw_v, 6) if isinstance(raw_v, float) else raw_v,
            "normalized": round(norm_v, 4),
            "weight": weight, "source": source, "license": license_,
            "note": note,
        })

    # ---------------- long-term ----------------
    # Öncelik: CQ ohlcv (260g kapanış, z-score serisi dahil) > CoinGecko (365g).
    cq_btc_price = raw.get("cq_btc_price")
    cq_ratio = raw.get("cq_btc_price_vs_200dma")
    cq_ratio_series = raw.get("cq_btc_vs_200dma_series") or []
    cg_ratio = raw.get("btc_price_vs_200dma")
    cg_ratio_series = raw.get("btc_vs_200dma_series") or []
    if cq_ratio is not None:
        dma_src, dma_note = "CryptoQuant price-ohlcv", "cq"
        ratio, ratio_series = cq_ratio, cq_ratio_series
        btc_price = cq_btc_price
        dma_val = raw.get("cq_btc_200dma")
    elif cg_ratio is not None:
        dma_src, dma_note = "CoinGecko market_chart", "clean"
        ratio, ratio_series = cg_ratio, cg_ratio_series
        btc_price = raw.get("btc_price")
        dma_val = raw.get("btc_200dma")
    else:
        dma_src, dma_note, ratio, ratio_series, btc_price, dma_val = \
            None, "clean", None, [], None, None
    if ratio is not None:
        if len(ratio_series) >= 30:
            norm = nz.norm_momentum_z(ratio_series, ratio)
        else:
            norm = nz.norm_band(ratio, 0.5)  # seri yoksa: ±%50 sapma -> ±1
        add("btc_price_vs_200dma", "long_term", ratio, norm,
            LT_WEIGHTS["btc_price_vs_200dma"], dma_src,
            f"BTC is {ratio:+.1%} vs its 200-day average"
            + (f" (${btc_price:,.0f} vs ${dma_val:,.0f})"
               if btc_price and dma_val else ""),
            license_=dma_note)

    tvl = raw.get("avax_tvl_momentum_30d")
    add("avax_tvl_momentum_30d", "long_term", tvl, nz.norm_tvl_momentum_30d(tvl),
        LT_WEIGHTS["avax_tvl_momentum_30d"], "DefiLlama",
        (f"Avalanche TVL {tvl:+.1%} in 30 days"
         + (f" (${raw['avax_tvl_usd']/1e6:,.0f}M → ${raw['avax_tvl_prev_usd']/1e6:,.0f}M)"
            if raw.get("avax_tvl_usd") and raw.get("avax_tvl_prev_usd") else "")
         if tvl is not None else ""))

    stbl = raw.get("stablecoin_mcap_momentum")
    add("stablecoin_mcap_momentum", "long_term", stbl, nz.norm_stable_mcap_momentum(stbl),
        LT_WEIGHTS["stablecoin_mcap_momentum"], "DefiLlama stablecoins",
        (f"Avalanche stablecoin supply {stbl:+.1%} in 30 days"
         + (f" (${raw['stable_mcap_usd']/1e6:,.0f}M → ${raw['stable_mcap_prev_usd']/1e6:,.0f}M)"
            if raw.get("stable_mcap_usd") and raw.get("stable_mcap_prev_usd") else "")
         if stbl is not None else ""))

    # CQ capitalization türevleri — MVRV-Z, NUPL, BCI, realized dist, growth diff.
    # BGeometrics MVRV/NUPL varsa hâlâ CQ Z'ye düşer; yoksa BG fallback.
    btc_cap = raw.get("cq_btc_cap") or {}
    if btc_cap.get("mvrv_z_raw") is not None:
        add("btc_mvrv_z", "long_term", btc_cap["mvrv_z_raw"],
            nz.norm_tanh_z(btc_cap["mvrv_z_raw"]), LT_WEIGHTS["btc_mvrv_z"],
            "CryptoQuant capitalization",
            f"MVRV-Z = {btc_cap['mvrv_z_raw']:.2f} (market cap {btc_cap['mvrv']:.2f}× realized cap)",
            license_="cq")
    if btc_cap.get("nupl") is not None:
        add("btc_nupl_cap", "long_term", btc_cap["nupl"],
            nz.norm_nupl_band(btc_cap["nupl"]), LT_WEIGHTS["btc_nupl_cap"],
            "CryptoQuant capitalization",
            f"NUPL = {btc_cap['nupl']:.3f} — "
            + ("euphoria zone" if btc_cap["nupl"] > 0.75
               else "capitulation zone" if btc_cap["nupl"] < 0
               else "moderate profit"), license_="cq")
    if btc_cap.get("bci_momentum") is not None:
        bci = btc_cap["bci_momentum"]
        add("btc_bci_momentum", "long_term", bci, nz.norm_bci_momentum(bci),
            LT_WEIGHTS["btc_bci_momentum"], "CryptoQuant capitalization",
            f"Bull-bear cycle proxy (MA30−MA365 of MVRV) = {bci:+.3f} — "
            + ("bull phase" if bci > 0 else "bear phase"), license_="cq")
    if btc_cap.get("realized_price_dist") is not None:
        dist = btc_cap["realized_price_dist"]
        add("btc_realized_price_dist", "long_term", dist,
            nz.norm_realized_price_dist(dist), LT_WEIGHTS["btc_realized_price_dist"],
            "CryptoQuant capitalization",
            f"BTC is {dist:+.1%} vs realized price (~${btc_cap.get('realized_price', 0):,.0f})",
            license_="cq")
    if btc_cap.get("growth_rate_diff") is not None:
        g = btc_cap["growth_rate_diff"]
        add("btc_growth_rate_diff", "long_term", g, nz.norm_growth_rate_diff(g),
            LT_WEIGHTS["btc_growth_rate_diff"], "CryptoQuant capitalization",
            f"Market-cap growth minus realized-cap growth (30d) = {g:+.1%}",
            license_="cq")

    mvrv = raw.get("btc_mvrv")
    if mvrv is not None and btc_cap.get("mvrv_z_raw") is None:
        series = raw.get("btc_mvrv_series") or []
        norm = nz.norm_mean_reversion_z(series, mvrv) if len(series) >= 30 \
            else nz.norm_mvrv_fallback(mvrv)
        add("btc_mvrv", "long_term", mvrv, norm, LT_WEIGHTS["btc_mvrv_z"], "BGeometrics",
            f"MVRV = {mvrv:.2f}")

    nupl = raw.get("btc_nupl")
    if nupl is not None and btc_cap.get("nupl") is None:
        series = raw.get("btc_nupl_series") or []
        norm = nz.norm_mean_reversion_z(series, nupl) if len(series) >= 30 else None
        add("btc_nupl", "long_term", nupl, norm, LT_WEIGHTS["btc_nupl_cap"], "BGeometrics",
            f"NUPL = {nupl:.3f}")

    # ETH realized_cap CQ'da None geldiği için eth_mvrv_z / eth_nupl_cap normalde düşer;
    # ileride kaynak açılırsa ağırlıklar hazır (LT_WEIGHTS).
    eth_cap = raw.get("cq_eth_cap") or {}
    if eth_cap.get("mvrv_z_raw") is not None:
        add("eth_mvrv_z", "long_term", eth_cap["mvrv_z_raw"],
            nz.norm_tanh_z(eth_cap["mvrv_z_raw"]), LT_WEIGHTS["eth_mvrv_z"],
            "CryptoQuant capitalization",
            f"ETH MVRV-Z = {eth_cap['mvrv_z_raw']:.2f}", license_="cq")
    if eth_cap.get("nupl") is not None:
        add("eth_nupl_cap", "long_term", eth_cap["nupl"],
            nz.norm_nupl_band(eth_cap["nupl"]), LT_WEIGHTS["eth_nupl_cap"],
            "CryptoQuant capitalization",
            f"ETH NUPL = {eth_cap['nupl']:.3f}", license_="cq")

    hr = raw.get("btc_hash_ribbon")
    add("btc_hash_ribbon", "long_term", hr, nz.norm_hash_ribbon(hr),
        LT_WEIGHTS["btc_hash_ribbon"], "mempool.space mining",
        (f"Hashrate 30d average is {hr:+.1%} above the 60d average — "
         + ("miner recovery" if hr > 0 else "miner capitulation")
         if hr is not None else ""))

    # v0.4: BGeometrics supply-in-profit (profit-loss); MVRV/NUPL'in BG karşılığı
    # artık CQ capitalization'a yedeklendi (yukarıda); supply_profit kendi sinyali.
    bg_sp = raw.get("btc_supply_profit")
    if bg_sp is not None:
        # %50..%95 kârda arz bandı: yüksek kâr = dağıtım riski (bearish uç),
        # düşük kâr = kapitülasyon (bullish uç). (0.72 - v)/0.22 -> v=0.50 -> +1, v=0.94 -> -1
        norm_sp = nz.clip((0.72 - bg_sp) / 0.22)
        add("btc_supply_profit", "long_term", bg_sp, norm_sp,
            LT_WEIGHTS["btc_supply_profit"], "BGeometrics profit-loss",
            f"BTC supply in profit = {bg_sp:.0%}")

    # v0.4: SOPR long-term katmanına taşındı (sopr_family kümesi).
    sopr = raw.get("btc_sopr")
    add("btc_sopr", "long_term", sopr, nz.norm_band((sopr - 1.0) if sopr is not None else None, 0.02),
        LT_WEIGHTS["btc_sopr"], "BGeometrics",
        f"SOPR = {sopr:.4f} — spent outputs in {'profit' if (sopr or 0) >= 1 else 'loss'}")

    # ---------------- short-term ----------------
    # OKX AVAX funding yalnız başına dar varlık sinyali; v0.2'de ağırlık BTC
    # tarafına kaydırıldı, AVAX funding besleme metriği olarak kalıyor.
    avax_fr = raw.get("avax_funding")
    add("avax_funding", "short_term", avax_fr, nz.norm_funding(avax_fr),
        ST_WEIGHTS.get("avax_funding", 0), "OKX",
        f"AVAX funding {avax_fr:+.4%}/8h" if avax_fr is not None else "")

    # BTC funding: OKX varken OKX, yoksa CQ all_exchange (saatlik % -> 8s ondalık).
    btc_fr = raw.get("btc_funding")
    cq_btc_fr = raw.get("cq_btc_funding")
    if btc_fr is not None:
        fr_src, fr_val, fr_lic = "OKX", btc_fr, "clean"
    elif cq_btc_fr is not None:
        fr_src, fr_val, fr_lic = "CryptoQuant funding-rates", cq_btc_fr, "cq"
    else:
        fr_src, fr_val, fr_lic = None, None, "clean"
    add("btc_funding", "short_term", fr_val, nz.norm_funding(fr_val),
        ST_WEIGHTS["btc_funding"], fr_src,
        (f"BTC funding {fr_val:+.4%}/8h ({'longs crowding' if fr_val > 0.0001 else 'shorts crowding' if fr_val < -0.0001 else 'balanced'})"
         if fr_val is not None else ""), license_=fr_lic)

    eth_fr = raw.get("cq_eth_funding")
    add("eth_funding", "short_term", eth_fr, nz.norm_funding(eth_fr),
        ST_WEIGHTS["eth_funding"], "CryptoQuant funding-rates",
        (f"ETH funding {eth_fr:+.4%}/8h" if eth_fr is not None else ""),
        license_="cq")

    # OI değişimi: OKX AVAX varken OKX; yoksa CQ BTC OI %24s.
    oi_pct = raw.get("avax_oi_change_24h")
    cq_btc_oi, cq_btc_oi_prev = raw.get("cq_btc_oi_usd"), raw.get("cq_btc_oi_usd_prev")
    if oi_pct is not None:
        oi_src, oi_val, oi_lic = "OKX", oi_pct, "clean"
        oi_note = f"AVAX open interest {oi_pct:+.1%} in 24h"
        if raw.get("avax_oi_contracts") and raw.get("avax_oi_prev"):
            oi_note += f" ({raw['avax_oi_prev']/1e6:.1f}M → {raw['avax_oi_contracts']/1e6:.1f}M contracts)"
    elif cq_btc_oi and cq_btc_oi_prev:
        oi_src, oi_lic = "CryptoQuant open-interest", "cq"
        oi_val = cq_btc_oi / cq_btc_oi_prev - 1.0
        oi_note = (f"BTC open interest {oi_val:+.1%} in 24h "
                   f"(${cq_btc_oi_prev/1e9:.2f}B → ${cq_btc_oi/1e9:.2f}B)")
    else:
        oi_src, oi_val, oi_lic, oi_note = None, None, "clean", ""
    add("oi_change_24h", "short_term", oi_val, nz.norm_oi_change(oi_val),
        ST_WEIGHTS["oi_change_24h"], oi_src, oi_note, license_=oi_lic)

    cq_eth_oi, cq_eth_oi_prev = raw.get("cq_eth_oi_usd"), raw.get("cq_eth_oi_usd_prev")
    eth_oi_pct = (cq_eth_oi / cq_eth_oi_prev - 1.0) if (cq_eth_oi and cq_eth_oi_prev) else None
    add("eth_oi_change_24h", "short_term", eth_oi_pct, nz.norm_oi_change(eth_oi_pct),
        ST_WEIGHTS["eth_oi_change_24h"], "CryptoQuant open-interest",
        (f"ETH open interest {eth_oi_pct:+.1%} in 24h"
         if eth_oi_pct is not None else ""), license_="cq")

    # 24s fiyat momentumu: CQ gün kapanışları varken CQ, yoksa CoinGecko.
    mom = raw.get("price_momentum_24h")
    cq_mom = None
    if raw.get("cq_btc_24h_change") is not None and raw.get("cq_eth_24h_change") is not None:
        cq_mom = (raw["cq_btc_24h_change"] + raw["cq_eth_24h_change"]) / 2.0
    elif raw.get("cq_btc_24h_change") is not None:
        cq_mom = raw["cq_btc_24h_change"]
    if cq_mom is not None:
        mom_src, mom_val, mom_lic = "CryptoQuant price-ohlcv", cq_mom, "cq"
        mom_note = f"BTC/ETH average 24h change {cq_mom:+.1%}"
    elif mom is not None:
        mom_src, mom_val, mom_lic = "CoinGecko", mom, "clean"
        mom_note = f"BTC/ETH/AVAX average 24h change {mom:+.1%}"
    else:
        mom_src, mom_val, mom_lic, mom_note = None, None, "clean", ""
    add("price_momentum_24h", "short_term", mom_val, nz.norm_price_momentum_24h(mom_val),
        ST_WEIGHTS["price_momentum_24h"], mom_src, mom_note, license_=mom_lic)

    btc_long_liq = raw.get("cq_btc_long_liq_usd_24h")
    btc_short_liq = raw.get("cq_btc_short_liq_usd_24h")
    if btc_long_liq is not None and btc_short_liq is not None:
        asym_norm = nz.norm_liq_asymmetry(btc_long_liq, btc_short_liq)
        tot = btc_long_liq + btc_short_liq
        asym_raw = (btc_long_liq - btc_short_liq) / tot if tot > 0 else None
        add("btc_liq_asymmetry", "short_term", asym_raw, asym_norm,
            ST_WEIGHTS["btc_liq_asymmetry"], "CryptoQuant liquidations",
            f"BTC 24h liquidations: long ${btc_long_liq/1e6:.0f}M vs short ${btc_short_liq/1e6:.0f}M "
            f"({'long flush dominant' if btc_long_liq > btc_short_liq else 'short flush dominant'})",
            license_="cq")

    eth_long_liq = raw.get("cq_eth_long_liq_usd_24h")
    eth_short_liq = raw.get("cq_eth_short_liq_usd_24h")
    if eth_long_liq is not None and eth_short_liq is not None:
        add("eth_liq_asymmetry", "short_term",
            ((eth_long_liq - eth_short_liq) / (eth_long_liq + eth_short_liq)
             if (eth_long_liq + eth_short_liq) > 0 else None),
            nz.norm_liq_asymmetry(eth_long_liq, eth_short_liq),
            ST_WEIGHTS["eth_liq_asymmetry"], "CryptoQuant liquidations",
            f"ETH 24h liquidations: long ${eth_long_liq/1e6:.0f}M vs short ${eth_short_liq/1e6:.0f}M",
            license_="cq")

    xrp_long_liq = raw.get("cq_xrp_long_liq_usd_24h")
    xrp_short_liq = raw.get("cq_xrp_short_liq_usd_24h")
    if xrp_long_liq is not None and xrp_short_liq is not None:
        add("xrp_liq_asymmetry", "short_term",
            ((xrp_long_liq - xrp_short_liq) / (xrp_long_liq + xrp_short_liq)
             if (xrp_long_liq + xrp_short_liq) > 0 else None),
            nz.norm_liq_asymmetry(xrp_long_liq, xrp_short_liq),
            ST_WEIGHTS["xrp_liq_asymmetry"], "CryptoQuant liquidations",
            f"XRP 24h liquidations: long ${xrp_long_liq/1e6:.0f}M vs short ${xrp_short_liq/1e6:.0f}M",
            license_="cq")

    xrp_fr = raw.get("cq_xrp_funding")
    add("xrp_funding", "short_term", xrp_fr, nz.norm_funding(xrp_fr),
        ST_WEIGHTS["xrp_funding"], "CryptoQuant funding-rates",
        (f"XRP funding {xrp_fr:+.4%}/8h" if xrp_fr is not None else ""),
        license_="cq")

    cq_xrp_oi, cq_xrp_oi_prev = raw.get("cq_xrp_oi_usd"), raw.get("cq_xrp_oi_usd_prev")
    xrp_oi_pct = (cq_xrp_oi / cq_xrp_oi_prev - 1.0) if (cq_xrp_oi and cq_xrp_oi_prev) else None
    add("xrp_oi_change_24h", "short_term", xrp_oi_pct, nz.norm_oi_change(xrp_oi_pct),
        ST_WEIGHTS["xrp_oi_change_24h"], "CryptoQuant open-interest",
        (f"XRP open interest {xrp_oi_pct:+.1%} in 24h" if xrp_oi_pct is not None else ""),
        license_="cq")

    trx7 = raw.get("cq_trx_7d_return")
    add("trx_momentum_7d", "short_term", trx7, nz.norm_momentum_7d(trx7),
        ST_WEIGHTS["trx_momentum_7d"], "CryptoQuant trx price-ohlcv",
        (f"TRX 7d return {trx7:+.1%} (close ${raw.get('cq_trx_price', 0):.4f})"
         if trx7 is not None else ""), license_="cq")

    fg = raw.get("fear_greed")
    add("fear_greed", "short_term", fg, nz.norm_fear_greed(fg),
        ST_WEIGHTS["fear_greed"], "alternative.me F&G",
        (f"Fear & Greed = {fg}/100 ({raw.get('fear_greed_label', '')})"
         if fg is not None else ""))

    cvd = raw.get("binance_taker_slope")
    add("binance_taker_cvd", "short_term", cvd, nz.norm_taker_slope(cvd),
        ST_WEIGHTS["binance_taker_cvd"], "Binance taker long/short",
        (f"Taker buy/sell ratio trend over 7d: {cvd:+.4f}/day"
         if cvd is not None else ""))

    gap = raw.get("okx_spot_perp_gap")
    add("okx_spot_perp_gap", "short_term", gap, nz.norm_spot_perp_gap(gap),
        ST_WEIGHTS["okx_spot_perp_gap"], "OKX spot/swap",
        (f"AVAX perp premium vs spot {gap:+.3%}"
         if gap is not None else ""))

    cb = raw.get("cq_btc_cb_premium")
    add("cb_premium", "short_term", cb, nz.norm_cb_premium(cb),
        ST_WEIGHTS["cb_premium"], "CryptoQuant coinbase-premium-index",
        (f"Coinbase premium {cb:+.2f}% — "
         + ("US spot buying" if cb > 0 else "US spot selling / discount")
         if cb is not None else ""), license_="cq")

    kr = raw.get("cq_btc_kr_premium")
    add("kr_premium", "short_term", kr, nz.norm_kr_premium(kr),
        ST_WEIGHTS["kr_premium"], "CryptoQuant korea-premium-index",
        (f"Korea premium {kr:+.2f}% — "
         + ("Korean retail demand strong" if kr > 0 else "Korean retail weak")
         if kr is not None else ""), license_="cq")

    # ETH/BTC OI oranı: 90g günlük seri; oranın kendi ortalamasından sapması.
    eth_oi_d, btc_oi_d = raw.get("cq_eth_oi_daily"), raw.get("cq_btc_oi_daily")
    if eth_oi_d and btc_oi_d and len(eth_oi_d) >= 30 and len(btc_oi_d) >= 30:
        n = min(len(eth_oi_d), len(btc_oi_d))
        ratios = [e / b for e, b in zip(eth_oi_d[-n:], btc_oi_d[-n:]) if b > 0]
        if len(ratios) >= 30:
            ma = sum(ratios) / len(ratios)
            rel = (ratios[-1] - ma) / ma if ma else None
            add("eth_btc_oi_ratio", "short_term", rel, nz.norm_ratio_dev(rel),
                ST_WEIGHTS["eth_btc_oi_ratio"], "CryptoQuant open-interest",
                (f"ETH/BTC OI ratio {ratios[-1]:.3f} vs 90d avg {ma:.3f} ({rel:+.1%}) — "
                 + ("leverage rotating to ETH" if rel > 0 else "leverage rotating to BTC")
                 if rel is not None else ""), license_="cq")

    # CoinMetrics ETH/BTC oranları (adres/tx) — metrics_raw'a besleme; v1'de
    # bileşen ağırlığı yok, v2'de talep kümesi adayı (WEIGHTING.md §Çarpan 1 C).

    # ---------------- regime (composite'a girmez) ----------------
    dxy = raw.get("fred_dollar_momentum_30d")
    add("fred_dollar_momentum", "regime", dxy, nz.norm_macro_change(dxy, 0.03),
        REGIME_WEIGHTS["fred_dollar_momentum"], "FRED DTWEXBGS",
        (f"Broad dollar index 30d change {dxy:+.1%} ({raw.get('fred_dollar_latest')}) — "
         + ("USD strengthening: risk-off" if dxy > 0 else "USD easing: risk-on")
         if dxy is not None else ""))

    vix = raw.get("fred_vix_momentum_30d")
    add("fred_vix_momentum", "regime", vix, nz.norm_macro_change(vix, 8.0),
        REGIME_WEIGHTS["fred_vix_momentum"], "FRED VIXCLS",
        (f"VIX 30d change {vix:+.1f} pts ({raw.get('fred_vix_latest')}) — "
         + ("fear rising: risk-off" if vix > 0 else "fear easing: risk-on")
         if vix is not None else ""))

    dgs = raw.get("fred_dgs10_momentum_30d")
    add("fred_dgs10_momentum", "regime", dgs, nz.norm_macro_change(dgs, 0.5),
        REGIME_WEIGHTS["fred_dgs10_momentum"], "FRED DGS10",
        (f"US 10Y yield 30d change {dgs:+.2f} pts ({raw.get('fred_dgs10_latest')}) — "
         + ("tightening: risk-off" if dgs > 0 else "easing: risk-on")
         if dgs is not None else ""))

    return comps


def run():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "history").mkdir(parents=True, exist_ok=True)

    print("[1/4] fetching free sources (CoinGecko/OKX/DefiLlama/BGeometrics/...)")
    raw, errors = fetch_all()
    print("[2/4] fetching CryptoQuant (Bearer plan)")
    cq_raw, cq_errors = fetch_cryptoquant()
    raw.update({f"cq_{k}": v for k, v in cq_raw.items() if not k.startswith("_")})
    errors.extend(cq_errors)
    credit_log = cq_raw.get("_credit_log", {})

    print("[3/4] normalizing + weighting")
    comps = build_components(raw)
    lt = weighted_layer([c for c in comps if c["layer"] == "long_term"])
    st = weighted_layer([c for c in comps if c["layer"] == "short_term"])
    rg = weighted_layer([c for c in comps if c["layer"] == "regime"])
    # Katman tamamen boşsa 0 kabul edilir; composite her durumda hesaplanır.
    lt_c = lt if lt is not None else 0.0
    st_c = st if st is not None else 0.0
    composite = round(0.5 * lt_c + 0.5 * st_c, 4)

    # Schmitt hysteresis (POLICY.md §1): giriş ±0.50, çıkış ±0.40; bant içinde
    # önceki etiket korunur (history'deki en son snapshot'tan okunur).
    def label(x):
        return "SAT" if x < -0.5 else ("AL" if x > 0.5 else "NÖTR")

    hist_dir = DATA_DIR / "history"
    sig_prev = None
    prev_snaps = sorted(hist_dir.glob("*.json")) if hist_dir.exists() else []
    if prev_snaps:
        try:
            sig_prev = json.loads(prev_snaps[-1].read_text(
                encoding="utf-8")).get("signal")
        except (OSError, json.JSONDecodeError):
            sig_prev = None
    sig_now = label(composite)
    if sig_prev == "AL" and composite > 0.4:
        signal = "AL"           # çıkış eşiğinin üstünde: AL kal
    elif sig_prev == "SAT" and composite < -0.4:
        signal = "SAT"          # çıkış eşiğinin altında: SAT kal
    else:
        signal = sig_now        # giriş eşikleri veya NÖTR bölge

    snap = {
        "version": "0.1",
        "weights_version": WEIGHTS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "long_term_index": lt,
        "short_term_index": st,
        "regime_index": rg,
        "signal": signal,
        "signal_prev": sig_prev,
        "signal_rule": "Schmitt hysteresis (POLICY.md §1): enter >+0.50 AL / <−0.50 SAT; exit ±0.40 — band holds previous label  (composite = 0.5*LT + 0.5*ST)",
        "composite": composite,
        "components": comps,
        "metrics_raw": {
            "coingecko": {"btc_price": raw.get("btc_price"), "eth_price": raw.get("eth_price"), "avax_price": raw.get("avax_price")},
            "okx": {"avax_funding": raw.get("avax_funding"), "avax_oi": raw.get("avax_oi_contracts")},
            "defillama": {"avax_tvl": raw.get("avax_tvl_usd")},
            "bgeometrics": {"mvrv": raw.get("btc_mvrv"), "nupl": raw.get("btc_nupl"), "sopr": raw.get("btc_sopr")},
            "cryptoquant": {"btc_price": raw.get("cq_btc_price"), "eth_price": raw.get("cq_eth_price")},
            "cryptoquant_credit_log": credit_log,
            "bgeometrics_profit": {"supply_profit": raw.get("btc_supply_profit")},
            "fred": {"source": raw.get("fred_source"), "dollar": raw.get("fred_dollar_latest"),
                     "vix": raw.get("fred_vix_latest"), "dgs10": raw.get("fred_dgs10_latest")},
            "binance": {"taker_ratio_latest": raw.get("binance_taker_ratio_latest")},
            "coinmetrics": {"eth_btc_addr_ratio": raw.get("eth_btc_addr_ratio"),
                            "eth_btc_tx_ratio": raw.get("eth_btc_tx_ratio")},
            "coingecko_market": {"btc_dominance": raw.get("btc_dominance"),
                                 "top5": raw.get("top_performers"),
                                 "worst5": raw.get("worst_performers")},
            "etherscan": {"eth_total_supply": raw.get("eth_total_supply")},
        },
        "errors": errors,
    }

    print("[4/4] writing snapshot")
    (DATA_DIR / "snapshot.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    # Koşu-bazlı arşiv (F8): gün-içi üzerine yazma yok; weights v2 IC-weighting
    # için her koşu ayrı dosyada korunur.
    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    hist = DATA_DIR / "history" / f"{run_id}.json"
    hist.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nLONG-TERM : {lt}  ({'AL' if (lt or 0) > 0.5 else 'SAT' if (lt or 0) < -0.5 else 'NÖTR'})")
    print(f"SHORT-TERM: {st}  ({'AL' if (st or 0) > 0.5 else 'SAT' if (st or 0) < -0.5 else 'NÖTR'})")
    print(f"REGIME    : {rg}  (composite'a girmez)")
    print(f"COMPOSITE : {composite}  ->  {signal}")
    print(f"components: {len(comps)} | errors: {len(errors)}")
    for e in errors:
        print(f"  ! {e}")
    print(f"snapshot -> {DATA_DIR / 'snapshot.json'}")


if __name__ == "__main__":
    run()
