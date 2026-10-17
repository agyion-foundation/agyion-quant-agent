"""
engine.py — Quant Agent v0 veri motoru.

Akış: fetch (izole kaynaklar) -> normalize ([-1,+1]) -> CONTRACT ağırlıklarıyla
katman indeksleri -> composite = 0.5*LT + 0.5*ST -> sinyal (POLICY.md §1
Schmitt hysteresis: SAT/NÖTR/AL) ->
data/snapshot.json + data/history/YYYY-MM-DDTHH.json + stdout özeti.

Erişilemeyen kaynak bileşenleri düşülür, kalan ağırlıklar katman içinde
yeniden 1.0'a normalize edilir ve errors'a yazılır. Mock veri YOK.

Çalıştırma:  python engine.py
"""

import json
from datetime import datetime
from pathlib import Path

import cryptoquant
import fetchers
import normalize as nz

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_DIR = DATA_DIR / "history"

# CONTRACT.md "Bileşen seti v0/v0.2" — katman içi başlangıç ağırlıkları
# (katman başına toplam 1.0). Kaynak erişilemezse bileşen düşer ve kalan
# ağırlıklar yeniden normalize edilir.
# v0.2: CQ capitalization türevleri (mvrv_z/nupl/bci/realized_price_dist/
# growth_rate_diff), hash ribbon (LT); ETH liq asimetrisi, TRX 7g momentum,
# fear&greed, binance taker CVD, OKX spot-perp gap (ST); yeni "rejim"
# katmanı (FRED makro). Eski bileşenler dengeli biçimde yeniden tartıldı.
# WEIGHTS "v1" — WEIGHTING.md uzman ağırlıklarıyla hizalı.
# Küme eşikleri uygulandı: değerleme kümesi (mvrv_z+nupl+realized_dist+bci,
# CQ+BGeometrics) LT'nin tam %40'ı; kaldıraç kümesi (funding+OI+liq) ST'nin
# %50'si; talep kümesi (F&G+CVD+premium) ST'nin %30'u. XRP üçlüsü C sınıfı
# (.25×base) düşük ağırlıkla. btc_sopr v1'de LT'ye taşındı (sopr_family).
# "squeeze" bileşeni henüz veri kaynağı olmadığı için tanımlı değil —
# ağırlığı kaldıraç kümesi içinde funding'e dağıtıldı.
WEIGHTS = {
    "long_term": {
        # --- değerleme kümesi (toplam 0.40) ---
        "btc_mvrv_z": 0.11,               # CQ capitalization (tanh z)
        "btc_nupl_cap": 0.09,             # CQ capitalization (bant)
        "btc_bci_momentum": 0.07,         # CQ capitalization (BCI yaklaşımı)
        "btc_realized_price_dist": 0.06,  # CQ capitalization
        "btc_mvrv": 0.03,                 # BGeometrics
        "btc_nupl": 0.02,                 # BGeometrics
        "eth_mvrv_z": 0.015,              # CQ/CM (ETH realized cap yoksa düşer)
        "eth_nupl_cap": 0.005,            # CQ/CM (ETH realized cap yoksa düşer)
        # --- küme dışı (toplam 0.60) ---
        "btc_sopr": 0.12,                 # BGeometrics (v1: sopr_family, LT)
        "btc_growth_rate_diff": 0.08,     # CQ capitalization
        "btc_hash_ribbon": 0.08,          # mempool.space hashrate MA30/60
        "btc_price_vs_200dma": 0.10,      # CQ ohlcv öncelikli, CoinGecko fallback
        "btc_supply_profit": 0.06,        # BGeometrics profit-loss
        "stablecoin_mcap_momentum": 0.04, # DefiLlama
        "avax_tvl_momentum_30d": 0.04,    # DefiLlama (C sınıfı)
        "eth_btc_active_addr_ratio": 0.03,  # CoinMetrics (C, rotasyon)
        "eth_btc_tx_ratio": 0.03,           # CoinMetrics (C, rotasyon)
        "eth_btc_mvrv_ratio": 0.02,         # ETH realized cap yok -> düşer
    },
    "short_term": {
        # --- kaldıraç kümesi (toplam 0.50) ---
        "btc_funding": 0.12,              # OKX v5 öncelikli, CQ fallback
        "eth_funding": 0.07,              # CryptoQuant funding-rates (ETH)
        "oi_change_24h": 0.09,            # OKX v5 öncelikli, CQ BTC OI fallback
        "eth_oi_change_24h": 0.05,        # CryptoQuant open-interest (ETH)
        "btc_liq_asymmetry": 0.08,        # CryptoQuant liquidations
        "eth_liq_asymmetry": 0.04,        # CryptoQuant liquidations (ETH)
        "xrp_funding": 0.02,              # CQ (C sınıfı)
        "xrp_oi_change_24h": 0.015,       # CQ (C sınıfı)
        "xrp_liq_asymmetry": 0.015,       # CQ (C sınıfı)
        # --- talep kümesi (toplam 0.30) ---
        "binance_taker_cvd": 0.12,        # Binance taker long/short 7g eğim
        "cb_premium": 0.07,               # CQ coinbase-premium-index
        "kr_premium": 0.05,               # CQ korea-premium-index
        "fear_greed": 0.06,               # alternative.me
        # --- küme dışı (toplam 0.20) ---
        "price_momentum_24h": 0.06,       # CQ ohlcv öncelikli, CoinGecko fallback
        "trx_momentum_7d": 0.04,          # CryptoQuant trx ohlcv
        "okx_spot_perp_gap": 0.05,        # OKX spot vs SWAP primi
        "eth_btc_oi_ratio": 0.05,         # CQ günlük OI oranı vs 90g ort.
    },
    # Rejim (makro) katmanı: FRED anahtarsız seriler, haftalık frekans.
    # composite hesabına girmez; ayrıca raporlanır.
    "regime": {
        "fred_dollar_momentum": 0.34,     # DTWEXBGS 30g değişim (%)
        "fred_vix_momentum": 0.33,        # VIXCLS 30g değişim (puan)
        "fred_dgs10_momentum": 0.33,      # DGS10 30g değişim (puan)
    },
}
WEIGHTS_VERSION = "v1"


def pct(x):
    return f"{x * 100:+.1f}%"


def musd(x):
    return f"${x / 1e6:,.0f}M"


def build_components(cg, okx, dl, bg, cq, fg, hb, bn, sp, fred, cm):
    """Mevcut ham veriden bileşen listesi üretir. Verisi olmayan bileşen
    hiç eklenmez (düşülür). Kaynak önceliği: bileşen adları CONTRACT'taki
    gibi kalır; CryptoQuant (cq) canlıysa CoinGecko/OKX'in üstüne geçer
    (veya fallback'i olur)."""
    comps = []

    def add(layer, name, raw, normalized, source, note):
        if normalized is None:
            return
        comps.append({
            "name": name, "layer": layer,
            "raw": round(raw, 6) if isinstance(raw, float) else raw,
            "normalized": round(nz.clip(normalized), 4),
            "weight": WEIGHTS[layer][name],
            "source": source, "note": note,
            # F1 lisans alanı: "cq" = CryptoQuant provenance'lı (public üründe
            # HAM değer olarak yayınlanamaz — ToS §6.4/CC BY-NC; sadece internal
            # taslağa ve demo'ya gider). "clean" = projenin lisans-temiz 🆓/🔧
            # kaynakları (CoinGecko, OKX, DefiLlama, BGeometrics, alternative.me,
            # mempool.space, Binance, FRED, CoinMetrics). Endeks/sinyal değerleri
            # (LT/ST/rejim/composite) bizim türev IP'mizdir, her iki durumda da
            # public'te yayınlanabilir.
            "license": "cq" if "CryptoQuant" in source else "clean",
        })

    # ---- Long-term ------------------------------------------------------
    # btc_price_vs_200dma: CQ ohlcv (close serisi, 200g SMA) öncelikli;
    # CQ fail -> CoinGecko market_chart fallback.
    dma_src = None
    if cq.get("btc_price_vs_200dma") is not None:
        dma_src = (cq, "CryptoQuant price-ohlcv")
    elif cg.get("btc_price_vs_200dma") is not None:
        dma_src = (cg, "CoinGecko market_chart")
    if dma_src:
        src, sname = dma_src
        raw = src["btc_price_vs_200dma"]
        n = None
        if src.get("btc_vs_200dma_series"):
            n = nz.norm_momentum_z(src["btc_vs_200dma_series"], raw)
        if n is None:  # seri yok/yetersizse sezgisel bant: ±%20 sapma -> ±1
            n = nz.norm_band(raw, 0.20)
        yon = "above" if raw >= 0 else "below"
        add("long_term", "btc_price_vs_200dma", raw, n, sname,
            f"BTC price is {pct(abs(raw))} {yon} its 200-day average (${src['btc_price']:,.0f} vs 200DMA ${src['btc_200dma']:,.0f})")

    for name, label, key in (("btc_mvrv", "MVRV", "mvrv"),
                             ("btc_nupl", "NUPL", "nupl")):
        m = bg.get(key)
        if m:
            n = nz.norm_mean_reversion_z(m["series"], m["value"])
            add("long_term", name, m["value"], n, "BGeometrics",
                f"BTC {label} = {m['value']:.4f} (data date {m['date']}, "
                f"{'above historical average — overheating risk' if n < 0 else 'below historical average — cool/cheap'})")

    if dl.get("avax_tvl_now") and dl.get("avax_tvl_30d_ago"):
        raw = dl["avax_tvl_now"] / dl["avax_tvl_30d_ago"] - 1.0
        add("long_term", "avax_tvl_momentum_30d", raw,
            nz.norm_tvl_momentum_30d(raw), "DefiLlama",
            f"Avalanche TVL {pct(raw)} in 30 days ({musd(dl['avax_tvl_30d_ago'])} → {musd(dl['avax_tvl_now'])})")

    if dl.get("stable_mcap_now") and dl.get("stable_mcap_30d_ago"):
        raw = dl["stable_mcap_now"] / dl["stable_mcap_30d_ago"] - 1.0
        add("long_term", "stablecoin_mcap_momentum", raw,
            nz.norm_stable_mcap_momentum(raw), "DefiLlama stablecoins",
            f"Avalanche stablecoin supply {pct(raw)} in 30 days ({musd(dl['stable_mcap_30d_ago'])} → {musd(dl['stable_mcap_now'])})")

    # CQ capitalization türevleri (BTC; ETH endpoint'i canlıysa ETH de).
    for asset, label in (("btc", "BTC"), ("eth", "ETH")):
        cap = cq.get(f"{asset}_cap")
        if not cap:
            continue
        pfx = f"{asset}_"
        if cap.get("mvrv_z_raw") is not None:
            add("long_term", f"{pfx}mvrv_z", cap["mvrv_z_raw"],
                nz.norm_tanh_z(cap["mvrv_z_raw"]), "CryptoQuant capitalization",
                f"{label} MVRV-Z = {cap['mvrv_z_raw']:.2f} "
                f"({'overvaluation zone' if cap['mvrv_z_raw'] > 2 else 'accumulation zone' if cap['mvrv_z_raw'] < 0 else 'no overheating'})")
        if cap.get("nupl") is not None:
            add("long_term", f"{pfx}nupl_cap", cap["nupl"],
                nz.norm_nupl_band(cap["nupl"]), "CryptoQuant capitalization",
                f"{label} NUPL = {cap['nupl']:.3f} "
                f"({'high profit zone — distribution risk' if cap['nupl'] > 0.5 else 'moderate profit' if cap['nupl'] > 0 else 'loss/capitulation zone'})")
        if asset == "btc" and cap.get("bci_momentum") is not None:
            add("long_term", "btc_bci_momentum", cap["bci_momentum"],
                nz.norm_bci_momentum(cap["bci_momentum"]), "CryptoQuant capitalization",
                f"BTC BCI proxy (MVRV MA30−MA365) = {cap['bci_momentum']:+.3f} — "
                f"{'bull-phase trend' if cap['bci_momentum'] > 0 else 'bear-phase trend'}")
        if asset == "btc" and cap.get("realized_price_dist") is not None:
            d = cap["realized_price_dist"]
            add("long_term", "btc_realized_price_dist", d,
                nz.norm_realized_price_dist(d), "CryptoQuant capitalization",
                f"BTC price is {pct(d)} "
                f"{'above' if d >= 0 else 'below'} realized price (realized ≈ ${cap.get('realized_price', 0):,.0f})")
        if asset == "btc" and cap.get("growth_rate_diff") is not None:
            g = cap["growth_rate_diff"]
            add("long_term", "btc_growth_rate_diff", g,
                nz.norm_growth_rate_diff(g), "CryptoQuant capitalization",
                f"BTC 30d market cap growth minus realized cap growth = {pct(g)} "
                f"({'speculative inflow dominant' if g > 0 else 'speculative outflow / realized cap growing'})")

    # Hash ribbon (mempool.space): hashrate MA30 vs MA60 kesişimi.
    if hb.get("hash_ribbon_rel") is not None:
        raw = hb["hash_ribbon_rel"]
        add("long_term", "btc_hash_ribbon", raw, nz.norm_hash_ribbon(raw),
            "mempool.space mining",
            f"Hashrate 30d average is {pct(raw)} "
            f"{'above the 60d average — miner recovery' if raw >= 0 else 'below the 60d average — miner capitulation pressure'}")

    # ---- Short-term ------------------------------------------------------
    # Funding: BTC için OKX öncelikli, CQ fallback; ETH sadece CQ
    # (v1: avax_funding kaldırıldı, eth_funding eklendi).
    for name, key, label, cq_only in (("btc_funding", "btc", "BTC", False),
                                      ("eth_funding", "eth", "ETH", True)):
        raw, prev, sname = None, None, None
        if not cq_only and okx.get(f"{key}_funding") is not None:
            raw, prev, sname = okx[f"{key}_funding"], okx.get(f"{key}_funding_24h_ago"), "OKX v5 funding-rate"
        elif cq.get(f"{key}_funding") is not None:
            raw, prev, sname = cq[f"{key}_funding"], cq.get(f"{key}_funding_24h_ago"), "CryptoQuant funding-rates"
        if raw is not None:
            delta = f", 24h ago {prev * 100:+.3f}%" if prev is not None else ""
            add("short_term", name, raw, nz.norm_funding(raw), sname,
                f"{label} funding {raw * 100:+.4f}% (8h{delta}) — "
                f"{'longs crowding' if raw > 0.0001 else 'shorts crowding' if raw < -0.0001 else 'neutral'}")

    # OI 24s değişimi: OKX (AVAX) öncelikli, CQ BTC OI fallback.
    if okx.get("avax_oi_usd") and okx.get("avax_oi_usd_prev"):
        raw = okx["avax_oi_usd"] / okx["avax_oi_usd_prev"] - 1.0
        add("short_term", "oi_change_24h", raw, nz.norm_oi_change(raw), "OKX v5 open-interest",
            f"AVAX open interest (OI) {pct(raw)} in 24h")
    elif cq.get("btc_oi_usd") and cq.get("btc_oi_usd_prev"):
        raw = cq["btc_oi_usd"] / cq["btc_oi_usd_prev"] - 1.0
        add("short_term", "oi_change_24h", raw, nz.norm_oi_change(raw),
            "CryptoQuant open-interest",
            f"BTC open interest (OI, all_exchange) {pct(raw)} in 24h "
            f"(${cq['btc_oi_usd_prev'] / 1e9:,.2f}B → ${cq['btc_oi_usd'] / 1e9:,.2f}B)")

    # ETH OI 24s değişimi (v1: oi_eth, CQ).
    if cq.get("eth_oi_usd") and cq.get("eth_oi_usd_prev"):
        raw = cq["eth_oi_usd"] / cq["eth_oi_usd_prev"] - 1.0
        add("short_term", "eth_oi_change_24h", raw, nz.norm_oi_change(raw),
            "CryptoQuant open-interest (ETH)",
            f"ETH open interest (OI, all_exchange) {pct(raw)} in 24h "
            f"(${cq['eth_oi_usd_prev'] / 1e9:,.2f}B → ${cq['eth_oi_usd'] / 1e9:,.2f}B)")

    # 24s fiyat momentumu: CQ ohlcv (BTC/ETH close) öncelikli, CoinGecko fallback.
    cq_changes = [cq.get(k) for k in ("btc_24h_change", "eth_24h_change")]
    cq_changes = [c for c in cq_changes if c is not None]
    if cq_changes:
        raw = sum(cq_changes) / len(cq_changes)
        add("short_term", "price_momentum_24h", raw, nz.norm_price_momentum_24h(raw),
            "CryptoQuant price-ohlcv",
            f"BTC/ETH average 24h price change {pct(raw)} (daily closes)")
    else:
        changes = [cg.get(k) for k in ("btc_24h_change", "eth_24h_change", "avax_24h_change")]
        changes = [c / 100.0 for c in changes if c is not None]
        if changes:
            raw = sum(changes) / len(changes)
            add("short_term", "price_momentum_24h", raw, nz.norm_price_momentum_24h(raw),
                "CoinGecko simple/price",
                f"BTC/ETH/AVAX average 24h price change {pct(raw)}")

    # YENİ: BTC likidasyon asimetrisi (son 24s, CQ). Long liq baskın -> bearish ST.
    if (cq.get("btc_long_liq_usd_24h") is not None
            and cq.get("btc_short_liq_usd_24h") is not None):
        lo, sh = cq["btc_long_liq_usd_24h"], cq["btc_short_liq_usd_24h"]
        n = nz.norm_liq_asymmetry(lo, sh)
        if n is not None:
            raw = (lo - sh) / (lo + sh)  # asimetri oranı: +1 = tamamen long temizliği
            add("short_term", "btc_liq_asymmetry", raw, n,
                "CryptoQuant liquidations",
                f"BTC 24h liquidations: long {musd(lo)} vs short {musd(sh)} — "
                f"{'long flush dominant (selling pressure; if extreme, a bottom may be near)' if raw > 0.1 else 'short flush dominant (short squeeze potential)' if raw < -0.1 else 'both sides balanced'}")

    # ETH likidasyon asimetrisi (btc_liq_asymmetry'nin ETH kopyası, CQ).
    if (cq.get("eth_long_liq_usd_24h") is not None
            and cq.get("eth_short_liq_usd_24h") is not None):
        lo, sh = cq["eth_long_liq_usd_24h"], cq["eth_short_liq_usd_24h"]
        n = nz.norm_liq_asymmetry(lo, sh)
        if n is not None:
            raw = (lo - sh) / (lo + sh)
            add("short_term", "eth_liq_asymmetry", raw, n,
                "CryptoQuant liquidations",
                f"ETH 24h liquidations: long {musd(lo)} vs short {musd(sh)} — "
                f"{'long flush dominant (selling pressure)' if raw > 0.1 else 'short flush dominant (short squeeze potential)' if raw < -0.1 else 'both sides balanced'}")

    # TRX 7g momentum (CQ trx ohlcv).
    if cq.get("trx_7d_return") is not None:
        raw = cq["trx_7d_return"]
        add("short_term", "trx_momentum_7d", raw, nz.norm_momentum_7d(raw),
            "CryptoQuant price-ohlcv (TRX)",
            f"TRX 7-day return {pct(raw)} (close ${cq.get('trx_price', 0):.4f})")

    # XRP türev seti (CQ): funding, OI 24s değişimi, likidasyon asimetrisi —
    # btc/eth bileşenlerinin XRP kopyaları (aynı parametreler, /v1/xrp/...).
    if cq.get("xrp_funding") is not None:
        raw = cq["xrp_funding"]
        prev = cq.get("xrp_funding_24h_ago")
        delta = f", 24h ago {prev * 100:+.3f}%" if prev is not None else ""
        add("short_term", "xrp_funding", raw, nz.norm_funding(raw),
            "CryptoQuant funding-rates (XRP)",
            f"XRP funding {raw * 100:+.4f}% (8h{delta}) — "
            f"{'longs crowding' if raw > 0.0001 else 'shorts crowding' if raw < -0.0001 else 'neutral'}")
    if cq.get("xrp_oi_usd") and cq.get("xrp_oi_usd_prev"):
        raw = cq["xrp_oi_usd"] / cq["xrp_oi_usd_prev"] - 1.0
        add("short_term", "xrp_oi_change_24h", raw, nz.norm_oi_change(raw),
            "CryptoQuant open-interest (XRP)",
            f"XRP open interest (OI, all_exchange) {pct(raw)} in 24h "
            f"({musd(cq['xrp_oi_usd_prev'])} → {musd(cq['xrp_oi_usd'])})")
    if (cq.get("xrp_long_liq_usd_24h") is not None
            and cq.get("xrp_short_liq_usd_24h") is not None):
        lo, sh = cq["xrp_long_liq_usd_24h"], cq["xrp_short_liq_usd_24h"]
        n = nz.norm_liq_asymmetry(lo, sh)
        if n is not None:
            raw = (lo - sh) / (lo + sh)
            add("short_term", "xrp_liq_asymmetry", raw, n,
                "CryptoQuant liquidations (XRP)",
                f"XRP 24h liquidations: long {musd(lo)} vs short {musd(sh)} — "
                f"{'long flush dominant (selling pressure)' if raw > 0.1 else 'short flush dominant (short squeeze potential)' if raw < -0.1 else 'both sides balanced'}")

    # Fear & Greed (alternative.me).
    if fg.get("fear_greed") is not None:
        raw = fg["fear_greed"]
        add("short_term", "fear_greed", raw, nz.norm_fear_greed(raw),
            "alternative.me F&G",
            f"Fear & Greed = {raw}/100 ({fg.get('fear_greed_label', '?')})")

    # Binance taker long/short 7g eğimi (CVD yaklaşımı).
    if bn.get("btc_taker_slope_7d") is not None:
        raw = bn["btc_taker_slope_7d"]
        add("short_term", "binance_taker_cvd", raw, nz.norm_taker_slope(raw),
            "Binance futures taker long/short",
            f"BTC taker long/short ratio {bn.get('btc_taker_ls_ratio', 0):.2f}, "
            f"7d slope {raw:+.4f}/day — {'aggressive buying on the rise' if raw > 0 else 'aggressive selling on the rise'}")

    # OKX spot-perp gap.
    if sp.get("spot_perp_gap") is not None:
        raw = sp["spot_perp_gap"]
        add("short_term", "okx_spot_perp_gap", raw, nz.norm_spot_perp_gap(raw),
            "OKX v5 ticker (spot vs SWAP)",
            f"OKX BTC perp premium {pct(raw)} (spot ${sp.get('btc_spot_okx', 0):,.0f} vs swap ${sp.get('btc_swap_okx', 0):,.0f})")

    # Premium index'ler (CQ, BTC): Coinbase (ABD kurumsal talebi) + Kore
    # (kimchi premium, perakende talebi). Pozitif = alım baskısı.
    if cq.get("btc_cb_premium") is not None:
        raw = cq["btc_cb_premium"]
        add("short_term", "cb_premium", raw, nz.norm_cb_premium(raw),
            "CryptoQuant coinbase-premium-index",
            f"Coinbase premium {raw:+.2f}% — "
            f"{'US spot demand stronger (institutional buying)' if raw > 0.02 else 'US spot selling / discount' if raw < -0.02 else 'no premium'}")
    if cq.get("btc_kr_premium") is not None:
        raw = cq["btc_kr_premium"]
        add("short_term", "kr_premium", raw, nz.norm_kr_premium(raw),
            "CryptoQuant korea-premium-index",
            f"Korea premium (kimchi) {raw:+.2f}% — "
            f"{'Korean retail demand strong' if raw > 0.5 else 'Korean discount — weak retail' if raw < -0.5 else 'neutral'}")

    # ETH/BTC OI oranı (CQ günlük OI): 90g ortalamasından göreli sapma.
    # Oran yükseliyorsa kaldıraç ETH tarafına kayıyor = rotasyon.
    btc_oi_d, eth_oi_d = cq.get("btc_oi_daily"), cq.get("eth_oi_daily")
    if btc_oi_d and eth_oi_d:
        n_days = min(len(btc_oi_d), len(eth_oi_d))
        ratio_series = [e / b for e, b in zip(eth_oi_d[-n_days:], btc_oi_d[-n_days:]) if b > 0]
        if len(ratio_series) >= 30:
            mean = sum(ratio_series) / len(ratio_series)
            raw = ratio_series[-1] / mean - 1.0
            add("short_term", "eth_btc_oi_ratio", raw, nz.norm_ratio_dev(raw),
                "CryptoQuant open-interest (günlük)",
                f"ETH/BTC OI ratio {ratio_series[-1]:.3f} vs 90d avg {mean:.3f} ({pct(raw)}) — "
                f"{'leverage rotating toward ETH' if raw > 0 else 'leverage rotating toward BTC'}")

    # ---- Rejim (makro, FRED) --------------------------------------------
    # Haftalık frekanslı makro arka plan; composite'a girmez, ayrı izlenir.
    if fred.get("fred_DTWEXBGS_chg30_pct") is not None:
        raw = fred["fred_DTWEXBGS_chg30_pct"]
        add("regime", "fred_dollar_momentum", raw,
            nz.norm_macro_change(raw, 0.03), "FRED DTWEXBGS",
            f"Broad dollar index 30d change {pct(raw)} ({fred.get('fred_DTWEXBGS', 0):.1f}) — "
            f"{'USD strengthening: risk-off' if raw > 0 else 'USD weakening: risk-on'}")
    if fred.get("fred_VIXCLS_chg30") is not None:
        raw = fred["fred_VIXCLS_chg30"]
        add("regime", "fred_vix_momentum", raw,
            nz.norm_macro_change(raw, 8.0), "FRED VIXCLS",
            f"VIX 30d change {raw:+.1f} pts ({fred.get('fred_VIXCLS', 0):.1f}) — "
            f"{'fear rising: risk-off' if raw > 0 else 'fear easing: risk-on'}")
    if fred.get("fred_DGS10_chg30") is not None:
        raw = fred["fred_DGS10_chg30"]
        add("regime", "fred_dgs10_momentum", raw,
            nz.norm_macro_change(raw, 0.5), "FRED DGS10",
            f"US 10Y yield 30d change {raw:+.2f} pts ({fred.get('fred_DGS10', 0):.2f}) — "
            f"{'tightening: risk-off' if raw > 0 else 'easing: risk-on'}")

    return comps


# POLICY.md §1 — Schmitt hysteresis: giriş ±0.50, çıkış ±0.40.
SIGNAL_ENTER = 0.50
SIGNAL_EXIT = 0.40


def schmitt_signal(composite, prev_signal):
    """POLICY.md §1 sinyal etiketi. ±0.40..±0.50 bandında önceki etiket
    korunur (whipsaw koruması). prev_signal None ise NÖTR varsayılır."""
    if composite is None:
        return None
    if composite > SIGNAL_ENTER:
        return "AL"
    if composite < -SIGNAL_ENTER:
        return "SAT"
    if prev_signal == "AL" and composite > SIGNAL_EXIT:
        return "AL"
    if prev_signal == "SAT" and composite < -SIGNAL_EXIT:
        return "SAT"
    return "NÖTR"


def previous_signal():
    """Önceki etiketi data/history arşivindeki en son snapshot'tan okur.
    Yoksa None (NÖTR varsayımı)."""
    if not HISTORY_DIR.exists():
        return None
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8")).get("signal")
    except Exception:
        return None


def layer_index(comps, layer, errors):
    """Katman indeksi: mevcut bileşenlerin ağırlıklı ortalaması; ağırlıklar
    katman içinde yeniden 1.0'a normalize edilir (bileşen 'weight' alanına
    final ağırlık yazılır)."""
    items = [c for c in comps if c["layer"] == layer]
    if not items:
        errors.append(f"{layer} katmanında hiç bileşen yok — indeks null")
        return None
    total_w = sum(c["weight"] for c in items)
    dropped = set(WEIGHTS[layer]) - {c["name"] for c in items}
    for c in items:
        c["weight"] = round(c["weight"] / total_w, 4)
    if dropped:
        errors.append(f"{layer}: erişilemeyen bileşen(ler) düşüldü: {', '.join(sorted(dropped))} "
                      f"— ağırlıklar yeniden normalize edildi")
    return round(sum(c["normalized"] * c["weight"] for c in items), 4)


def main():
    errors = []
    cg, e1 = fetchers.fetch_coingecko()
    okx, e2 = fetchers.fetch_okx()
    dl, e3 = fetchers.fetch_defillama()
    bg, e4 = fetchers.fetch_bgeometrics()
    cq, e5 = cryptoquant.fetch_cryptoquant()
    fg, e6 = fetchers.fetch_fear_greed()
    hb, e7 = fetchers.fetch_hash_ribbon()
    bn, e8 = fetchers.fetch_binance_taker()
    sp, e9 = fetchers.fetch_okx_spot_perp()
    fred, e10 = fetchers.fetch_fred()
    cm, e11 = fetchers.fetch_coinmetrics()
    cgm, e12 = fetchers.fetch_coingecko_market()
    es, e13 = fetchers.fetch_etherscan()
    errors += e1 + e2 + e3 + e4 + e5 + e6 + e7 + e8 + e9 + e10 + e11 + e12 + e13

    comps = build_components(cg, okx, dl, bg, cq, fg, hb, bn, sp, fred, cm)
    lt = layer_index(comps, "long_term", errors)
    st = layer_index(comps, "short_term", errors)
    rg = layer_index(comps, "regime", errors)

    if lt is None and st is None:
        raise SystemExit("FATAL: hiçbir katman üretilemedi, snapshot yazılmadı. Hatalar: " + "; ".join(errors))
    lt_eff = lt if lt is not None else 0.0
    st_eff = st if st is not None else 0.0
    # Bir katman yoksa composite diğer katmandan hesaplanır
    composite = round(0.5 * lt_eff + 0.5 * st_eff, 4) if (lt is not None and st is not None) else (lt or st)
    # POLICY.md §1 — Schmitt hysteresis (giriş ±0.50 / çıkış ±0.40).
    sig_prev = previous_signal()
    signal = schmitt_signal(composite, sig_prev)

    snapshot = {
        "version": "0.1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "long_term_index": lt,
        "short_term_index": st,
        "regime_index": rg,
        "weights_version": WEIGHTS_VERSION,
        "policy": "POLICY.md v1",
        "signal": signal,
        "signal_prev": sig_prev,
        "signal_rule": ("Schmitt hysteresis (POLICY.md §1): giriş >+0.50 AL / <-0.50 SAT; "
                        "çıkış ±0.40 — bant içinde önceki etiket korunur "
                        "(composite = 0.5*LT + 0.5*ST)"),
        "composite": composite,
        "components": comps,
        "metrics_raw": {
            "coingecko": {k: cg.get(k) for k in
                          ("btc_price", "eth_price", "avax_price",
                           "btc_24h_change", "eth_24h_change", "avax_24h_change",
                           "btc_200dma", "btc_price_vs_200dma")},
            "okx": {k: okx.get(k) for k in
                    ("avax_funding", "btc_funding", "avax_funding_24h_ago",
                     "btc_funding_24h_ago", "avax_oi_usd", "avax_oi_usd_prev")},
            "defillama": {k: dl.get(k) for k in
                          ("avax_tvl_now", "avax_tvl_30d_ago",
                           "stable_mcap_now", "stable_mcap_30d_ago")},
            "bgeometrics": {k: (bg.get(k) or {}).get("value") for k in ("mvrv", "nupl", "sopr", "profit-loss")},
            "cryptoquant": {k: cq.get(k) for k in
                            ("btc_price", "eth_price", "btc_200dma", "btc_price_vs_200dma",
                             "btc_24h_change", "eth_24h_change",
                             "btc_funding", "eth_funding",
                             "btc_oi_usd", "btc_oi_usd_prev",
                             "btc_long_liq_usd_24h", "btc_short_liq_usd_24h",
                             "eth_long_liq_usd_24h", "eth_short_liq_usd_24h",
                             "trx_price", "trx_7d_return",
                             "xrp_funding", "xrp_oi_usd", "xrp_oi_usd_prev",
                             "xrp_long_liq_usd_24h", "xrp_short_liq_usd_24h",
                             "eth_funding", "eth_oi_usd", "eth_oi_usd_prev",
                             "btc_cb_premium", "btc_kr_premium")},
            "coinmetrics": {
                "eth_btc_addr_ratio": (cm.get("cm_eth_btc_addr_ratio_series") or [None])[-1],
                "eth_btc_tx_ratio": (cm.get("cm_eth_btc_tx_ratio_series") or [None])[-1],
            },
            "coingecko_market": {
                "btc_dominance": cgm.get("btc_dominance"),
                "top_performers": cgm.get("cg_top_performers"),
                "worst_performers": cgm.get("cg_worst_performers"),
            },
            "etherscan": {"eth_total_supply": es.get("eth_total_supply")},
            "cryptoquant_capitalization": {
                a: ({k: v for k, v in (cq.get(f"{a}_cap") or {}).items()
                     if k != "mvrv_series"} or None)
                for a in ("btc", "eth")},
            "cryptoquant_credit_log": cq.get("_credit_log"),
            "fear_greed": {k: fg.get(k) for k in ("fear_greed", "fear_greed_label")},
            "mempool": {k: hb.get(k) for k in
                        ("hashrate_ma30", "hashrate_ma60", "hash_ribbon_rel",
                         "difficulty_last_change_pct")},
            "binance": {k: bn.get(k) for k in
                        ("btc_taker_ls_ratio", "btc_taker_slope_7d")},
            "okx_spot_perp": {k: sp.get(k) for k in
                              ("btc_spot_okx", "btc_swap_okx", "spot_perp_gap")},
            "fred": {k: fred.get(k) for k in
                     ("fred_DTWEXBGS", "fred_DTWEXBGS_chg30_pct",
                      "fred_VIXCLS", "fred_VIXCLS_chg30",
                      "fred_DGS10", "fred_DGS10_chg30")},
        },
        "errors": errors,
    }

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    # F-minor/F8: koşu-bazlı arşiv dosyası (YYYY-MM-DDTHH) — gün içi çoklu
    # koşuda üzerine yazma yok; IC-weighting (weights v2) verisi korunur.
    stamp = datetime.now().strftime("%Y-%m-%dT%H")
    snap_path = DATA_DIR / "snapshot.json"
    hist_path = HISTORY_DIR / f"{stamp}.json"
    for p in (snap_path, hist_path):
        p.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- stdout özeti -----------------------------------------------------
    print("=" * 64)
    print(f"QUANT AGENT v0 — {snapshot['generated_at']}")
    print(f"  Uzun vade  (LT): {lt if lt is not None else 'null':>7}   "
          f"Kısa vade (ST): {st if st is not None else 'null':>7}   "
          f"Rejim: {rg if rg is not None else 'null':>7}")
    print(f"  Composite: {composite:>7}   SİNYAL: {signal}")
    print(f"  Bileşenler ({len(comps)} adet):")
    for c in comps:
        print(f"    [{c['layer']:<10}] {c['name']:<26} w={c['weight']:.2f} "
              f"norm={c['normalized']:+.2f}  {c['note']}")
    if errors:
        print(f"  Hatalar/Atlananlar ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")
    print(f"  Yazıldı: {snap_path}")
    print(f"  Arşiv:   {hist_path}")
    print("=" * 64)


if __name__ == "__main__":
    main()
