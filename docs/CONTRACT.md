# Quant Agent v0 — Veri Sözleşmesi (snapshot.json)

Tüm bileşenler bu dosya üzerinden konuşur. Üreten: engine (Python). Tüketen: dashboard (HTML) + newsletter.

## Konum
`/mnt/agents/output/quant-agent/data/snapshot.json` (engine her koşuda üzerine yazar; arşiv: `data/history/YYYY-MM-DD.json`)

## Şema
```json
{
  "version": "0.1",
  "generated_at": "2026-10-17T09:30:00+03:00",
  "long_term_index": 0.42,
  "short_term_index": -0.18,
  "signal": "NÖTR",
  "signal_rule": "<-0.5 SAT | -0.5..+0.5 NÖTR | >+0.5 AL (composite = 0.5*LT + 0.5*ST)",
  "composite": 0.12,
  "components": [
    {
      "name": "btc_price_vs_200dma",
      "layer": "long_term",
      "raw": 0.087,
      "normalized": 0.44,
      "weight": 0.25,
      "source": "CoinGecko market_chart",
      "license": "clean",
      "note": "BTC fiyatı 200DMA'nın %8.7 üstünde"
    }
  ],
  "metrics_raw": {
    "coingecko": {"btc_price": 0, "eth_price": 0, "avax_price": 0},
    "okx": {"avax_funding": 0.0, "avax_oi": 0},
    "defillama": {"avax_tvl": 0},
    "bgeometrics": {"mvrv": null, "nupl": null, "sopr": null}
  },
  "errors": ["bgeometrics timeout — atlandı"]
}
```

## Kurallar
- `normalized` her zaman [-1, +1]. Pozitif = bullish, negatif = bearish.
- Katman indeksi = bileşenlerin ağırlıklı ortalaması; ağırlıklar katman içinde 1'e toplanır; erişilemeyen kaynak düşülür ve ağırlıklar yeniden normalize edilir (errors'a yaz).
- `signal` composite'a göre, **Schmitt hysteresis** (POLICY.md §1): giriş >+0.50 → "AL" / <−0.50 → "SAT"; çıkış ±0.40 — bant içinde önceki etiket korunur (`signal_prev` alanı önceki etiketi taşır).
- Her bileşende `license: "clean"|"cq"` alanı vardır: `"cq"` = CryptoQuant provenance'lı (public çıktıda ham değer olarak yayınlanamaz; yalnız internal taslak + demo). `"clean"` = lisans-temiz 🆓/🔧 kaynaklar (CoinGecko, OKX, DefiLlama, BGeometrics, alternative.me, mempool.space, Binance, FRED, CoinMetrics). Endeks/sinyal değerleri (LT/ST/rejim/composite) bizim türev IP'mizdir — public'te serbest.
- Bileşen dili Türkçe not içerir (newsletter doğrudan kullanır).

## Bileşen seti v0
Long-term: btc_price_vs_200dma, btc_mvrv (bgeometrics, yoksa düş), btc_nupl (yoksa düş), avax_tvl_momentum_30d (DefiLlama), stablecoin_mcap_momentum (DefiLlama)
Short-term: avax_funding (OKX), btc_funding (OKX), oi_change_24h (OKX), price_momentum_24h (CoinGecko), btc_sopr (bgeometrics, yoksa düş), btc_liq_asymmetry (CryptoQuant)

## CryptoQuant kaynağı (v0.1 eki)
`engine/cryptoquant.py` — REST `https://api.cryptoquant.com`, auth `Authorization: Bearer $CQ_API_KEY`
(env var; koda gömülmez, yoksa CQ bileşenleri atlanır). Timeout 12 sn. Kullanılan endpoint'ler
(Advanced plan ile 200 OK): `{btc,eth}/market-data/{price-ohlcv,funding-rates,open-interest,liquidations}`.
On-chain endpoint'ler ve avax/sol ohlcv 403 — kullanılmaz.
Kaynak önceliği (bileşen adları sabit, kaynak değişebilir):
- btc_price_vs_200dma: CQ ohlcv (260g close → 200DMA + z-score serisi) öncelikli → CoinGecko fallback
- btc_funding: OKX öncelikli → CQ all_exchange funding fallback (CQ saatlik % birimi; ×8/100 ile 8s ondalığa çevrilir)
- avax_funding: sadece OKX (CQ'da AVAX türev verisi yok)
- oi_change_24h: OKX (AVAX) öncelikli → CQ BTC OI %24s fallback
- price_momentum_24h: CQ ohlcv (BTC/ETH günlük kapanış) öncelikli → CoinGecko fallback
- btc_liq_asymmetry (YENİ, short_term, başlangıç ağırlığı 0.10): son 24s long vs short
  likidasyon USD asimetrisi; long temizliği baskın = bearish ST (normalize.py'da işaret tersli).
  Eklenince short-term başlangıç ağırlıkları 1/1.1 ile yeniden dengelendi (toplam yine 1.0).
Kredi takibi: her CQ çağrısı stdout'a loglanır; `metrics_raw.cryptoquant_credit_log` endpoint→satır sayısı tutar.

## Bileşen seti v0.2 eki
CQ key çözümleme: `CQ_API_KEY` env var → yoksa proje kökü `.env` (KEY=VALUE). Key koda gömülmez.
Yeni CQ endpoint'leri: `{btc,eth}/market-data/capitalization?window=day&limit=400`
(market_cap, realized_cap, average_cap, delta_cap; ETH'de realized_cap None gelir →
ETH MVRV/NUPL türetilemez), `trx/market-data/price-ohlcv` ve XRP full market-data
(`xrp/{funding-rates,open-interest,liquidations}` — diğer altlar 403).

Long-term (v0.2): btc_mvrv_z, btc_nupl_cap, btc_bci_momentum, btc_realized_price_dist,
btc_growth_rate_diff, eth_mvrv_z, eth_nupl_cap (CQ capitalization; ETH yoksa düş),
btc_hash_ribbon (mempool.space hashrate MA30/MA60; kapalıysa düş).
Short-term (v0.2): eth_liq_asymmetry (CQ), trx_momentum_7d (CQ trx ohlcv),
fear_greed (alternative.me), binance_taker_cvd (Binance taker long/short 7g eğim),
okx_spot_perp_gap (OKX spot vs SWAP primi), xrp_funding / xrp_oi_change_24h /
xrp_liq_asymmetry (CQ xrp). Ücretsiz kaynaklar kapalıysa düşer + errors.
Yeni katman `regime` (composite'a GİRMEZ, `regime_index` olarak ayrı raporlanır):
fred_dollar_momentum (DTWEXBGS), fred_vix_momentum (VIXCLS), fred_dgs10_momentum (DGS10)
— 30g değişim, risk-off yönü bearish kodlanır. Katman başına ağırlık toplamı yine 1.0.
Ağırlıklar v0.2'de tüm katmanlarda dengeli yeniden tartıldı (engine.py WEIGHTS).

## v0.3
Bileşen `note` alanları İngilizce'ye çevrildi (global aboneler). Kod yorumları ve
hata/log mesajları Türkçe kalır (operasyonel iç metinler). `errors` dizisi
tanı amaçlıdır; newsletter "Data warnings" bölümünde olduğu gibi gösterilir.

## v0.4 — weights v1 (WEIGHTING.md hizalaması)
Snapshot'a `weights_version: "v1"` eklendi. Küme eşikleri: değerleme kümesi
(MVRV-Z+NUPL+realized_dist+BCI, tüm kaynaklar) = LT %40; kaldıraç
(funding+OI+liq, XRP dahil) = ST %50; talep (F&G+CVD+premium) = ST %30.
btc_sopr LT'ye taşındı (sopr_family); avax_funding kaldırıldı; yeni bileşenler:
eth_funding, eth_oi_change_24h, cb_premium, kr_premium (CQ), eth_btc_oi_ratio
(CQ günlük OI), eth_btc_active_addr_ratio, eth_btc_tx_ratio (CoinMetrics
community), btc_supply_profit (BGeometrics profit-loss), eth_btc_mvrv_ratio
(tanımlı; ETH realized cap hiçbir ücretsiz kaynakta yok → şimdilik düşer).
"squeeze" v1 listesinde ama veri kaynağı yok — tanımlanmadı, ağırlığı
kaldıraç kümesi içinde funding'e dağıtıldı.

## v0.5 — API key'ler
`.env`'deki key'ler (asla koda gömülmez; çözümleme: env var → .env, ortak
`fetchers.load_key`): CQ_API_KEY, FRED_API_KEY (resmi FRED API
api.stlouisfed.org/fred/series/observations; yoksa fredgraph.csv fallback),
BGEOMETRICS_API_KEY (Bearer header), COINGECKO_API_KEY (x-cg-demo-api-key;
ayrıca fetch_coingecko_market: /global btc_dominance + /coins/markets
top/worst-5 performer — alt_rejim hazırlığı, metrics_raw'a gider),
ETHERSCAN_API_KEY (v2 stats/tokensupply → eth_total_supply, düşük öncelik,
bileşen bağlı değil). Hata mesajları `_safe_err` ile key'leri maskeler
(***); snapshot/errors içinde key ASLA görünmez.

## v0.6 — lisans, POLICY ve icra hiyerarşisi (audit F1/F2/F5)
**Lisans kuralı (F1):** Hackathon demosu ve iç araştırma için CQ verisi
serbesttir (proje kuralı, whale_dash_api_matrix.md:106). Ticari katman
(ücretli newsletter / `newsletter-public-*.md`) yalnızca `license=="clean"`
bileşenlerin ham değerlerini + bizim türev IP'miz olan endeks/sinyal
özetlerini (LT/ST/rejim/composite) yayınlar; CQ-kaynaklı ham seriler
(funding %, OI $, liq $, premium index, CQ OHLCV) yalnızca
`newsletter-internal-*.md` taslağında kalır. CQ ticari lisansı alınırsa
ilgili bileşenlerin `license` alanı yükseltilir.

**Kanonik icra hiyerarşisi (F5):** ATK = demo/CEX vitrini; vault = ürün
icrası; ikisi de aynı POLICY.md kararını okur. Sinyal etiketi ve
boyutlandırma kurallarının tek otoriter tanımı POLICY.md'dedir (Schmitt
±0.50/±0.40, logistic k=4 maruziyet, çatışma ×0.75, LT−/ST− → 0.05,
rejim <−0.5 → ×0.5, rebalance |Δ|>0.05 veya etiket değişimi).

**Koşu arşivi (F8):** history dosyaları koşu-bazlıdır
(`data/history/YYYY-MM-DDTHH.json`); gün-içi üzerine yazma yok —
weights v2 IC-weighting'in ihtiyaç duyduğu veri birikimi korunur.
