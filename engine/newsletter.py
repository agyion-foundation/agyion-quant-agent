"""
newsletter.py — snapshot.json'dan İngilizce günlük bülten üretir
(global aboneler; sade, jargonsuz retail tonu; sinyal etiketleri SELL/NEUTRAL/BUY).

İKİ çıktı üretir (F1 lisans kuralı):
  * data/newsletter-public-YYYY-MM-DD.md   — ÜCRETLİ/PUBLIC ürün. Sadece
    license=="clean" bileşenlerin ham değerleri + bizim türev IP'miz olan
    endeks/sinyal özetleri (LT/ST/rejim/composite). CryptoQuant provenance'lı
    ("cq") ham seriler (funding %, OI $, liq $, premium index, CQ OHLCV fiyatı)
    ve kaynak adları bu dosyada ASLA görünmez.
  * data/newsletter-internal-YYYY-MM-DD.md — iç araştırma/hackathon demosu;
    tüm bileşenleri (cq dahil) ve tam hata listesini içerir.

Çalıştırma:  python newsletter.py   (engine.py'nin ardından)
"""

import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT = DATA_DIR / "snapshot.json"

LAYER_TR = {"long_term": "Long Term", "short_term": "Short Term",
            "regime": "Macro Regime"}
LAYER_ORDER = ("long_term", "short_term", "regime")
NAME_TR = {
    "btc_price_vs_200dma": "BTC vs 200-Day Average",
    "btc_mvrv": "BTC MVRV",
    "btc_nupl": "BTC NUPL",
    "avax_tvl_momentum_30d": "Avalanche TVL Momentum (30d)",
    "stablecoin_mcap_momentum": "Stablecoin Supply Momentum (30d)",
    "avax_funding": "AVAX Funding Rate",
    "btc_funding": "BTC Funding Rate",
    "oi_change_24h": "Open Interest Change (24h)",
    "price_momentum_24h": "Price Momentum (24h)",
    "btc_sopr": "BTC SOPR",
    "btc_liq_asymmetry": "BTC Liquidation Skew (24h)",
    "eth_liq_asymmetry": "ETH Liquidation Skew (24h)",
    "trx_momentum_7d": "TRX Momentum (7d)",
    "xrp_funding": "XRP Funding Rate",
    "xrp_oi_change_24h": "XRP Open Interest Change (24h)",
    "xrp_liq_asymmetry": "XRP Liquidation Skew (24h)",
    "btc_mvrv_z": "BTC MVRV-Z (CryptoQuant)",
    "btc_nupl_cap": "BTC NUPL (CryptoQuant)",
    "btc_bci_momentum": "BTC Bull-Bear Cycle (proxy)",
    "btc_realized_price_dist": "BTC Price vs Realized Price",
    "btc_growth_rate_diff": "BTC Growth Rate Gap (30d)",
    "eth_mvrv_z": "ETH MVRV-Z (CryptoQuant)",
    "eth_nupl_cap": "ETH NUPL (CryptoQuant)",
    "btc_hash_ribbon": "BTC Hash Ribbon (30d/60d avg)",
    "fear_greed": "Fear & Greed Index",
    "binance_taker_cvd": "Binance Taker Buy/Sell Trend (7d)",
    "okx_spot_perp_gap": "OKX Spot-Futures Premium",
    "fred_dollar_momentum": "US Dollar Index Momentum (30d)",
    "fred_vix_momentum": "VIX Momentum (30d)",
    "fred_dgs10_momentum": "US 10Y Yield Momentum (30d)",
    "eth_funding": "ETH Funding Rate",
    "eth_oi_change_24h": "ETH Open Interest Change (24h)",
    "cb_premium": "Coinbase Premium Index",
    "kr_premium": "Korea Premium (Kimchi)",
    "eth_btc_oi_ratio": "ETH/BTC Open Interest Ratio",
    "eth_btc_active_addr_ratio": "ETH/BTC Active Addresses Ratio",
    "eth_btc_tx_ratio": "ETH/BTC Transaction Ratio",
    "eth_btc_mvrv_ratio": "ETH/BTC MVRV Ratio",
    "btc_supply_profit": "BTC Supply in Profit",
}

# Snapshot'taki Türkçe sinyal etiketleri bültende İngilizce gösterilir.
SIGNAL_EN = {"SAT": "SELL", "NÖTR": "NEUTRAL", "AL": "BUY",
             "SELL": "SELL", "NEUTRAL": "NEUTRAL", "BUY": "BUY"}


def signal_en(s):
    return SIGNAL_EN.get(s, s)


def gauge(value, width=10):
    """Text gauge: -1..+1 -> ▰/▱ bar. Middle is neutral, right is bullish."""
    if value is None:
        return "▱" * width + " (no data)"
    filled = round((value + 1) / 2 * width)
    return "▰" * filled + "▱" * (width - filled)


def signal_word(v):
    if v is None:
        return "NO DATA"
    return "SELL" if v < -0.5 else ("BUY" if v > 0.5 else "NEUTRAL")


def driver_comment(snap, comps):
    """Plain-English takeaway: which components moved the composite most?

    F-minor (audit F9-v): rejim katmanı composite'a GİRMEZ — "en büyük
    sürücü" sıralaması rejim-dışı bileşenlerle sınırlıdır. `comps` çağıran
    tarafından kapsam-filtrelenmiş gelir (public'te yalnız clean bileşenler).
    """
    comps = [c for c in comps if c.get("layer") != "regime"]
    if not comps:
        return "Not enough data was collected today to read the market."
    contrib = [(c["normalized"] * c["weight"], c) for c in comps]
    contrib.sort(key=lambda t: abs(t[0]), reverse=True)
    top = contrib[:3]
    parts = []
    for k, c in top:
        direction = "pushing up" if k > 0 else "pushing down"
        parts.append(f"**{NAME_TR.get(c['name'], c['name'])}** ({k:+.2f}, {direction})")
    s = (f"Today's biggest market drivers: {', '.join(parts)}. ")
    lt, st = snap.get("long_term_index"), snap.get("short_term_index")
    if lt is not None and st is not None:
        if lt > 0.2 and st > 0.2:
            s += ("Both long-term and short-term gauges are positive — "
                  "the overall picture leans toward risk appetite. ")
        elif lt < -0.2 and st < -0.2:
            s += ("Both long-term and short-term gauges are negative — "
                  "the market looks defensive right now. ")
        elif abs(lt - st) > 0.4:
            which = "Long-term" if lt > st else "Short-term"
            s += (f"The two layers disagree: {which.lower()} signals look stronger, "
                  f"so expect choppy price action in the near term. ")
        else:
            s += ("Signals are clustered around neutral — "
                  "no clear direction today. ")
    if snap.get("errors"):
        s += (f"Note: {len(snap['errors'])} data source(s) were unavailable today, "
              f"so the index was computed with partial data.")
    return s


DISCLAIMER_EN = (
    "**Disclaimer.** This newsletter is provided for informational and educational purposes only and "
    "does not constitute investment, financial, legal, or tax advice, or a "
    "recommendation or solicitation to buy, sell, or hold any digital asset. Index "
    "readings and signal labels (BUY / NEUTRAL / SELL) describe the output of a "
    "mechanical quantitative model, not a personal recommendation. Portions of this "
    "issue, including \"The Read\", are generated by an AI language model from the "
    "day's data snapshot and are verified against that data before publication; even "
    "so, errors and omissions may occur.\n\n"
    "Cryptoassets are highly volatile and speculative; you may lose some or all of "
    "your investment. Past performance is not indicative of future results. Always "
    "do your own research and, where appropriate, consult a licensed financial "
    "advisor before making investment decisions. By reading this newsletter you "
    "acknowledge and accept these terms."
)


def build_newsletter(snap, day, public, llm_text=None):
    """Bülten gövdesi. public=True iken yalnız license=='clean' bileşenler
    kullanılır; endeks/sinyal özetleri (bizim türev IP'miz) her iki sürümde
    de görünür. llm_text (yalnız public): doğrulanmış LLM özeti; None ise
    deterministik driver_comment şablonu kullanılır (fallback)."""
    lt, st, comp = snap.get("long_term_index"), snap.get("short_term_index"), snap.get("composite")
    rg = snap.get("regime_index")
    sig = signal_en(snap["signal"])
    all_comps = snap.get("components", [])
    if public:
        comps = [c for c in all_comps if c.get("license") == "clean"]
    else:
        comps = all_comps

    lines = []
    title = f"# Quant Agent Daily — {day} | Signal: {sig}"
    if not public:
        title += "  (INTERNAL DRAFT — includes restricted-source research data; do NOT distribute)"
    lines.append(title)
    lines.append("")
    lines.append(f"**Long Term:** {lt if lt is not None else 'null'}  {gauge(lt)}  {signal_word(lt)}")
    lines.append("")
    lines.append(f"**Short Term:** {st if st is not None else 'null'}  {gauge(st)}  {signal_word(st)}")
    lines.append("")
    if rg is not None:
        lines.append(f"**Macro Regime:** {rg}  {gauge(rg)}  "
                     f"{'risk-on' if rg > 0.2 else 'risk-off' if rg < -0.2 else 'neutral'}")
        lines.append("")
    lines.append(f"**Composite (0.5×LT + 0.5×ST):** {comp} → **{sig}** "
                 f"_(Schmitt hysteresis, POLICY.md §1: enter >+0.50 BUY / <-0.50 SELL; "
                 f"exit ±0.40 — band holds previous label)_")
    lines.append("")
    # Layer-based grouping: each layer listed under its own heading.
    lines.append("## Components")
    if public:
        n_hidden = len(all_comps) - len(comps)
        if n_hidden:
            lines.append("")
            lines.append(f"_This public edition shows license-cleared data sources only; "
                         f"{n_hidden} research component(s) feed the index but their raw "
                         f"series are not redistributed (licensing)._")
    for layer in LAYER_ORDER:
        items = [c for c in comps if c["layer"] == layer]
        if not items:
            continue
        idx_key = {"long_term": lt, "short_term": st, "regime": rg}[layer]
        idx_txt = str(idx_key) if idx_key is not None else "null"
        lines.append("")
        lines.append(f"### {LAYER_TR.get(layer, layer)} — index: {idx_txt}")
        lines.append("")
        lines.append("| Component | Raw | Score | Weight | Impact | Note |")
        lines.append("|---|---|---|---|---|---|")
        for c in items:
            katki = c["normalized"] * c["weight"]
            lines.append(f"| {NAME_TR.get(c['name'], c['name'])} "
                         f"| {c['raw']} | {c['normalized']:+.2f} | {c['weight']:.2f} | {katki:+.3f} | {c['note']} |")
    # Unknown layers (forward-compatible) go last.
    extras = [c for c in comps if c["layer"] not in LAYER_ORDER]
    if extras:
        lines.append("")
        lines.append("### Other")
        lines.append("")
        lines.append("| Component | Layer | Raw | Score | Weight | Impact | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for c in extras:
            katki = c["normalized"] * c["weight"]
            lines.append(f"| {NAME_TR.get(c['name'], c['name'])} | {c['layer']} "
                         f"| {c['raw']} | {c['normalized']:+.2f} | {c['weight']:.2f} | {katki:+.3f} | {c['note']} |")
    lines.append("")
    lines.append("## Today's takeaway")
    lines.append("")
    if public and llm_text:
        # Doğrulama kapısını geçmiş LLM özeti (llm_commentary); aksi halde
        # deterministik şablon (driver_comment) korunur.
        lines.append(llm_text)
        lines.append("")
        lines.append("_AI-generated summary verified against today's data snapshot._")
    else:
        lines.append(driver_comment(snap, comps))
    lines.append("")
    if snap.get("errors"):
        lines.append("## Data warnings")
        lines.append("")
        if public:
            # Kaynak/bileşen adları public'te sızmaz — sadece sayı.
            lines.append(f"- {len(snap['errors'])} data source(s) were unavailable today; "
                         f"affected inputs were dropped and layer weights re-normalized.")
        else:
            for e in snap["errors"]:
                lines.append(f"- {e}")
        lines.append("")
    lines.append("---")
    if public:
        used = sorted({c["source"] for c in comps})
        src_txt = ", ".join(used) if used else "public market data"
        lines.append(f"*Sources (license-cleared): {src_txt} — not investment advice.*")
    else:
        lines.append("*Sources: CoinGecko, OKX, DefiLlama, BGeometrics, CryptoQuant, "
                     "alternative.me, mempool.space, Binance, FRED, CoinMetrics — "
                     "internal research draft, not for distribution, not investment advice.*")
    lines.append(f"*Generated: {snap.get('generated_at', '')} — quant-agent v{snap.get('version', '0.1')}*")
    if public:
        # Compliance bloğu: research/newsletter_final_design.md §3.6 standart metni.
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(DISCLAIMER_EN)
    return lines


def main():
    if not SNAPSHOT.exists():
        raise SystemExit(f"snapshot not found: {SNAPSHOT} — run engine.py first.")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    day = datetime.now().strftime("%Y-%m-%d")
    lt, st, comp = snap.get("long_term_index"), snap.get("short_term_index"), snap.get("composite")
    rg = snap.get("regime_index")
    sig = signal_en(snap["signal"])

    # LLM yorum katmanı (yalnız public): key/ağ yoksa veya doğrulama kapısı
    # geçilemezse None -> deterministik şablon (fallback) korunur.
    from llm_commentary import generate_commentary
    llm_text = generate_commentary(snap)

    pub = DATA_DIR / f"newsletter-public-{day}.md"
    pub.write_text("\n".join(build_newsletter(snap, day, public=True,
                                              llm_text=llm_text)), encoding="utf-8")
    internal = DATA_DIR / f"newsletter-internal-{day}.md"
    internal.write_text("\n".join(build_newsletter(snap, day, public=False)), encoding="utf-8")
    print(f"Newsletter written: {pub}")
    print(f"Internal draft:     {internal}")
    print(f"Signal: {sig} | LT={lt} ST={st} regime={rg} composite={comp}")


if __name__ == "__main__":
    main()
