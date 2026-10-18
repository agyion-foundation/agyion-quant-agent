"""
fetchers.py — Ücretsiz, key'siz public API'lerden ham metrik çekimi.

Her fetcher izole try/except ile korunur: biri patlarsa diğerleri çalışmaya
devam eder. Hiçbir fetcher exception fırlatmaz; hata durumunda kısmi veri +
errors listesi döner. Timeout: 10 sn (BGeometrics büyük seriler için 15 sn).

Dönüş sözleşmesi: her fetch_* fonksiyonu (data: dict, errors: list[str]) döner.
Erişilemeyen alanlar dict'te hiç yer almaz (None/mock YAZILMAZ).
"""

import csv
import datetime
import io
import os
import time
from pathlib import Path

import requests

TIMEOUT = 10
_UA = {"User-Agent": "quant-agent/0.1 (hackathon)"}

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _safe_err(e):
    """Hata metninde API key sızıntısını engelle: query param'a giden
    key'ler (FRED, Etherscan) hata URL'sinde görünür -> maskeler."""
    s = f"{type(e).__name__}: {e}"
    for name in ("FRED_API_KEY", "ETHERSCAN_API_KEY", "COINGECKO_API_KEY",
                 "BGEOMETRICS_API_KEY", "CQ_API_KEY"):
        k = load_key(name)
        if k:
            s = s.replace(k, "***")
    return s


def load_key(name):
    """API key çözümleme: 1) env var, 2) proje kökü .env (KEY=VALUE satırı).
    Key'ler koda gömülmez; sandbox/cron'da env kalıcı olmadığı için .env
    fallback'i zorunlu. Bulunamazsa None."""
    key = os.environ.get(name)
    if key:
        return key
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _get(url, params=None, timeout=TIMEOUT, headers=None):
    h = dict(_UA)
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- CoinGecko
def _cg_headers():
    """CoinGecko demo key (varsa): x-cg-demo-api-key header'ı."""
    key = load_key("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else None


def fetch_coingecko():
    """CoinGecko demo API (COINGECKO_API_KEY varsa key'li, yoksa key'siz).

    - simple/price: BTC/ETH/AVAX güncel fiyat + 24s değişim
    - market_chart (BTC, 365g): 200DMA hesabı + tarihsel ratio serisi
      Rate limit / hata durumunda seri atlanır, simple/price ile idare edilir.
    """
    data, errors = {}, []
    base = "https://api.coingecko.com/api/v3"
    hdr = _cg_headers()
    try:
        j = _get(f"{base}/simple/price", params={
            "ids": "bitcoin,ethereum,avalanche-2",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }, headers=hdr)
        data["btc_price"] = j["bitcoin"]["usd"]
        data["eth_price"] = j["ethereum"]["usd"]
        data["avax_price"] = j["avalanche-2"]["usd"]
        data["btc_24h_change"] = j["bitcoin"].get("usd_24h_change")
        data["eth_24h_change"] = j["ethereum"].get("usd_24h_change")
        data["avax_24h_change"] = j["avalanche-2"].get("usd_24h_change")
    except Exception as e:
        errors.append(f"coingecko simple/price başarısız: {type(e).__name__}: {e}")
        return data, errors  # fiyat yoksa market_chart'ın da anlamı sınırlı

    try:
        j = _get(f"{base}/coins/bitcoin/market_chart", params={
            "vs_currency": "usd", "days": "365", "interval": "daily",
        }, headers=hdr)
        prices = [p[1] for p in j.get("prices", [])]
        if len(prices) >= 200:
            dma200 = sum(prices[-200:]) / 200.0
            # Tarihsel (fiyat/200DMA - 1) serisi: z-score normalizasyonu için.
            # Her gün için o ana kadarki 200 günlük ortalama kullanılır.
            ratio_series = []
            for i in range(199, len(prices)):
                ma = sum(prices[i - 199:i + 1]) / 200.0
                ratio_series.append(prices[i] / ma - 1.0)
            data["btc_200dma"] = dma200
            data["btc_price_vs_200dma"] = prices[-1] / dma200 - 1.0
            data["btc_vs_200dma_series"] = ratio_series
        else:
            errors.append(f"coingecko market_chart yetersiz veri ({len(prices)} nokta)")
    except Exception as e:
        errors.append(f"coingecko market_chart başarısız (simple/price ile idare edilecek): "
                      f"{type(e).__name__}: {e}")
    return data, errors


def fetch_coingecko_market():
    """CoinGecko /global + /coins/markets: alt_rejim hazırlığı (bileşen değil).

    - btc_dominance: /global → market_cap_percentage.btc
    - top/worst 5 performer: /coins/markets top-100, 24h ve 7d getiriler
      (newsletter performer tablosu verisi; snapshot metrics_raw'a gider).
    Sandbox'ta host kapalı olabilir -> graceful skip."""
    data, errors = {}, []
    base = "https://api.coingecko.com/api/v3"
    hdr = _cg_headers()
    try:
        j = _get(f"{base}/global", headers=hdr)
        data["btc_dominance"] = float(j["data"]["market_cap_percentage"]["btc"])
    except Exception as e:
        errors.append(f"coingecko /global erişilemedi: {type(e).__name__}: {e}")
    try:
        j = _get(f"{base}/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": "100", "page": "1",
            "price_change_percentage": "24h,7d",
        }, headers=hdr, timeout=15)
        if not isinstance(j, list) or not j:
            raise ValueError("boş yanıt")
        rows = [{"symbol": c["symbol"].upper(), "name": c["name"],
                 "chg_24h": c.get("price_change_percentage_24h"),
                 "chg_7d": c.get("price_change_percentage_7d_in_currency")}
                for c in j]
        valid = [r for r in rows if r["chg_24h"] is not None]
        valid.sort(key=lambda r: r["chg_24h"], reverse=True)
        data["cg_top_performers"] = valid[:5]
        data["cg_worst_performers"] = valid[-5:][::-1]
    except Exception as e:
        errors.append(f"coingecko /coins/markets erişilemedi: {type(e).__name__}: {e}")
    return data, errors


# ---------------------------------------------------------------------- OKX
def _okx_funding(inst):
    j = _get("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst})
    return float(j["data"][0]["fundingRate"])


def _okx_funding_prev(inst):
    """Funding 8 saatte bir settle olur -> 3 kayıt öncesi ~24 saat öncesi."""
    j = _get("https://www.okx.com/api/v5/public/funding-rate-history",
             params={"instId": inst, "limit": "10"})
    rows = j.get("data", [])
    if len(rows) >= 4:
        return float(rows[3]["fundingRate"])
    if rows:
        return float(rows[-1]["fundingRate"])
    raise ValueError("funding-rate-history boş")


def fetch_okx():
    """OKX v5 public: AVAX/BTC perpetual funding (şimdi + 24s önce) ve AVAX OI."""
    data, errors = {}, []
    for key, inst in (("avax", "AVAX-USDT-SWAP"), ("btc", "BTC-USDT-SWAP")):
        try:
            data[f"{key}_funding"] = _okx_funding(inst)
        except Exception as e:
            errors.append(f"okx funding {inst} başarısız: {type(e).__name__}: {e}")
        try:
            data[f"{key}_funding_24h_ago"] = _okx_funding_prev(inst)
        except Exception as e:
            errors.append(f"okx funding-history {inst} başarısız: {type(e).__name__}: {e}")
    try:
        j = _get("https://www.okx.com/api/v5/public/open-interest",
                 params={"instType": "SWAP", "uly": "AVAX-USDT"})
        rows = j.get("data", [])
        # Tüm AVAX-USDT swap'ların USD cinsinden OI toplamı
        data["avax_oi_usd"] = sum(float(r.get("oiUsd") or 0) for r in rows) or None
        if data["avax_oi_usd"] is None:
            raise ValueError("open-interest yanıtı boş")
    except Exception as e:
        errors.append(f"okx open-interest başarısız: {type(e).__name__}: {e}")
    # 24s önceki OI: OKX rubik günlük istatistik endpoint'i (public)
    try:
        j = _get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                 params={"ccy": "AVAX", "period": "1D"})
        rows = j.get("data", [])
        if len(rows) >= 2:
            data["avax_oi_usd_prev"] = float(rows[-2][1])  # [ts, oi, vol]
    except Exception as e:
        errors.append(f"okx OI geçmişi (rubik) başarısız: {type(e).__name__}: {e}")
    return data, errors


# ---------------------------------------------------------------- DefiLlama
def fetch_defillama():
    """DefiLlama (key'siz): Avalanche TVL serisi + stablecoin mcap serisi (30g momentum)."""
    data, errors = {}, []
    try:
        j = _get("https://api.llama.fi/v2/historicalChainTvl/Avalanche")
        if len(j) >= 31:
            data["avax_tvl_now"] = j[-1]["tvl"]
            data["avax_tvl_30d_ago"] = j[-31]["tvl"]
            data["avax_tvl_date"] = j[-1]["date"]
        else:
            raise ValueError(f"yetersiz TVL serisi ({len(j)} nokta)")
    except Exception as e:
        errors.append(f"defillama Avalanche TVL başarısız: {type(e).__name__}: {e}")
    try:
        j = _get("https://stablecoins.llama.fi/stablecoincharts/Avalanche")
        # totalCirculatingUSD.peggedUSD = zincirdeki USD stablecoin toplamı
        def mcap(row):
            return (row.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if len(j) >= 31 and mcap(j[-1]) and mcap(j[-31]):
            data["stable_mcap_now"] = mcap(j[-1])
            data["stable_mcap_30d_ago"] = mcap(j[-31])
        else:
            raise ValueError(f"yetersiz stablecoin serisi ({len(j)} nokta)")
    except Exception as e:
        errors.append(f"defillama stablecoin mcap başarısız: {type(e).__name__}: {e}")
    return data, errors


# --------------------------------------------------------------- BGeometrics
def fetch_bgeometrics():
    """BGeometrics REST API (api.bgeometrics.com/v1, günlük).

    BGEOMETRICS_API_KEY varsa Authorization: Bearer ile gönderilir (doküman:
    ?token= veya Bearer) — kota rahatlar; yoksa ücretsiz katman (8 istek/saat,
    veri ~7 gün gecikmeli; 'date' alanı bunu gösterir).
    Erişilemezse ilgili metrik null kalır ve errors'a yazılır (mock YOK).
    """
    data, errors = {}, []
    bg_key = load_key("BGEOMETRICS_API_KEY")
    hdr = {"Authorization": f"Bearer {bg_key}"} if bg_key else None
    # profit-loss = "% of BTC supply currently in profit" (WEIGHTING.md v1
    # supply_profit bileşeni). Ücretsiz kota: 8 istek/saat — 4. metrik ekli.
    for metric in ("mvrv", "nupl", "sopr", "profit-loss"):
        try:
            if not bg_key:
                time.sleep(1.5)  # key'siz katman: istekleri seyrelt
            j = _get(f"https://api.bgeometrics.com/v1/{metric}", timeout=15,
                     headers=hdr)
            # 429 alınırsa retry YAPILMAZ: limit saatliktir, retry kotayı boşa yakar.
            if not isinstance(j, list) or not j:
                raise ValueError("boş yanıt")
            # Alan adı endpoint adıyla birebir aynı olmayabilir (ör.
            # profit-loss -> "profit_loss"): "d" (tarih) dışındaki ilk alan.
            def val(row):
                if row.get(metric) is not None:
                    return row[metric]
                for k, v in row.items():
                    if k != "d" and v is not None:
                        return v
                return None
            series = [v for v in (val(row) for row in j) if v is not None]
            if not series:
                raise ValueError("metrik alanı boş")
            data[metric] = {
                "value": series[-1],
                "date": j[-1]["d"],
                "series": series[-365:],  # son 1 yıl z-score için yeterli
            }
        except Exception as e:
            errors.append(f"bgeometrics {metric} erişilemedi: {type(e).__name__}: {e}")
    if not data:
        errors.append("bgeometrics erişilemedi — tüm on-chain metrikler null")
    return data, errors


# ------------------------------------------------------------- Fear & Greed
def fetch_fear_greed():
    """Alternative.me Fear & Greed Index (key'siz): 0-100 güncel değer +
    30g seri (z-score yedeği). Sandbox ağında host kapalı olabilir -> skip."""
    data, errors = {}, []
    try:
        j = _get("https://api.alternative.me/fng/", params={"limit": "30"})
        rows = j.get("data", [])
        if not rows:
            raise ValueError("boş yanıt")
        # newest-first: [0] güncel değer
        data["fear_greed"] = int(rows[0]["value"])
        data["fear_greed_series"] = [int(r["value"]) for r in reversed(rows)]
        data["fear_greed_label"] = rows[0].get("value_classification")
    except Exception as e:
        errors.append(f"alternative.me fear&greed erişilemedi: {type(e).__name__}: {e}")
    return data, errors


# ------------------------------------------------------------- mempool.space
def fetch_hash_ribbon():
    """mempool.space public API: hash ribbon basit versiyonu.

    Hashrate 30g MA vs 60g MA kesişimi: 30g MA 60g MA'nın üstüne çıkıyorsa
    madenci kapitülasyonu bitiyor/toparlanma = bullish uzun-vade.
    (1w penceresi 60g MA için yetersiz -> 3m günlük seri çekilir.)
    Ayrıca difficulty serisi teyit olarak denenir. Host kapalıysa skip."""
    data, errors = {}, []
    try:
        j = _get("https://mempool.space/api/v1/mining/hashrate/3m")
        # API yanıt anahtarı sürüme göre "hashrates" (güncel) veya "hashes"
        hashes = j.get("hashrates") or j.get("hashes") or []
        series = [float(h["avgHashrate"]) for h in hashes
                  if h.get("avgHashrate") is not None]
        if len(series) >= 60:
            ma30 = sum(series[-30:]) / 30.0
            ma60 = sum(series[-60:]) / 60.0
            data["hashrate_ma30"] = ma30
            data["hashrate_ma60"] = ma60
            data["hash_ribbon_rel"] = ma30 / ma60 - 1.0
        else:
            raise ValueError(f"yetersiz hashrate serisi ({len(series)} nokta)")
    except Exception as e:
        errors.append(f"mempool.space hashrate erişilemedi: {type(e).__name__}: {e}")
    try:
        j = _get("https://mempool.space/api/v1/mining/difficulty-adjustments/1m")
        if isinstance(j, list) and len(j) >= 2:
            # [timestamp, height, difficulty, change] — son ayar değişimi %
            data["difficulty_last_change_pct"] = float(j[-1][3])
    except Exception as e:
        errors.append(f"mempool.space difficulty erişilemedi (teyit atlandı): "
                      f"{type(e).__name__}: {e}")
    return data, errors


# ------------------------------------------------------------------ Binance
def _slope(series):
    """Son N noktanın en küçük kareler eğimi (nokta başına birim)."""
    n = len(series)
    if n < 2:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(series) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, series)) / den


def fetch_binance_taker():
    """Binance USDT-M futures taker long/short ratio (key'siz, günlük).

    longShortRatio > 1: taker alıcıları baskın. 7g eğim pozitifse agresif
    alım eğilimi artıyor = bullish kısa-vade. Host kapalıysa skip."""
    data, errors = {}, []
    try:
        j = _get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                 params={"symbol": "BTCUSDT", "period": "1d", "limit": "30"})
        if not isinstance(j, list) or len(j) < 8:
            raise ValueError(f"yetersiz seri ({len(j) if isinstance(j, list) else '?'} nokta)")
        series = [float(r["longShortRatio"]) for r in j]  # eski->yeni
        data["btc_taker_ls_ratio"] = series[-1]
        data["btc_taker_ls_series"] = series
        s = _slope(series[-7:])
        if s is not None:
            data["btc_taker_slope_7d"] = s
    except Exception as e:
        errors.append(f"binance taker long/short erişilemedi: {type(e).__name__}: {e}")
    return data, errors


# ------------------------------------------------------ OKX spot-perp gap
def fetch_okx_spot_perp():
    """OKX public ticker: BTC-USDT spot vs BTC-USDT-SWAP perp fiyat farkı.

    gap = (swap - spot) / spot. Pozitif gap = perp primi = kalabalık long
    talebi (aşırısı ısınma). Host kapalıysa skip."""
    data, errors = {}, []
    try:
        js = _get("https://www.okx.com/api/v5/market/ticker",
                  params={"instId": "BTC-USDT"})
        jp = _get("https://www.okx.com/api/v5/market/ticker",
                  params={"instId": "BTC-USDT-SWAP"})
        spot = float(js["data"][0]["last"])
        swap = float(jp["data"][0]["last"])
        if spot <= 0:
            raise ValueError("spot fiyat 0")
        data["btc_spot_okx"] = spot
        data["btc_swap_okx"] = swap
        data["spot_perp_gap"] = (swap - spot) / spot
    except Exception as e:
        errors.append(f"okx spot-perp gap erişilemedi: {type(e).__name__}: {e}")
    return data, errors


# -------------------------------------------------------------- CoinMetrics
def fetch_coinmetrics():
    """CoinMetrics community API (anahtarsız): btc+eth 400g günlük seri.

    Community katmanında CapRealUSD ve FeeTotUSD 403 (test edildi) — realized
    cap türevleri buradan ÜRETİLEMEZ (CQ capitalization kullanılır). Açık
    metrikler: CapMrktCurUSD, AdrActCnt, TxCnt. Tek istek, pagination yok
    (page_size=10000). ETH/BTC rotasyon oranları (aktif adres, tx) için
    tarih-hizalı oran serileri üretir. Host kapalıysa skip."""
    data, errors = {}, []
    try:
        start = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
        j = _get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                 params={"assets": "btc,eth",
                         "metrics": "CapMrktCurUSD,AdrActCnt,TxCnt",
                         "start_time": start, "frequency": "1d",
                         "page_size": "10000"},
                 timeout=20)
        rows = j.get("data", [])
        if not rows:
            raise ValueError("boş yanıt")
        per = {}
        for asset in ("btc", "eth"):
            rs = [r for r in rows if r.get("asset") == asset]
            rs.sort(key=lambda r: r["time"])
            per[asset] = rs
        nb, ne = len(per["btc"]), len(per["eth"])
        if nb < 31 or ne < 31:
            raise ValueError(f"yetersiz seri (btc={nb}, eth={ne})")
        # Tarih hizalama: iki varlıkta da ortak olan günler
        def field(rows_):  # zaman -> ölçümler
            return {r["time"]: r for r in rows_}
        mb, me = field(per["btc"]), field(per["eth"])
        common = sorted(set(mb) & set(me))
        def col(m, k, days):
            return [float(m[t][k]) for t in days
                    if m[t].get(k) is not None]
        addr_ratio, tx_ratio = [], []
        for t in common:
            b, e = mb[t], me[t]
            if b.get("AdrActCnt") and e.get("AdrActCnt") and float(b["AdrActCnt"]) > 0:
                addr_ratio.append(float(e["AdrActCnt"]) / float(b["AdrActCnt"]))
            if b.get("TxCnt") and e.get("TxCnt") and float(b["TxCnt"]) > 0:
                tx_ratio.append(float(e["TxCnt"]) / float(b["TxCnt"]))
        if len(addr_ratio) >= 30:
            data["cm_eth_btc_addr_ratio_series"] = addr_ratio
        if len(tx_ratio) >= 30:
            data["cm_eth_btc_tx_ratio_series"] = tx_ratio
        if not data:
            raise ValueError("hizalanmış oran serisi üretilemedi")
    except Exception as e:
        errors.append(f"coinmetrics community erişilemedi: {type(e).__name__}: {e}")
    return data, errors


# --------------------------------------------------------------------- FRED
FRED_SERIES = ("DTWEXBGS", "VIXCLS", "DGS10")  # dolar endeksi, VIX, 10Y


def _fred_series_api(sid, key):
    """Resmi FRED API (key'li): /fred/series/observations. eski->yeni."""
    j = _get("https://api.stlouisfed.org/fred/series/observations",
             params={"series_id": sid, "api_key": key, "file_type": "json",
                     "sort_order": "asc"},
             timeout=TIMEOUT)
    return [float(o["value"]) for o in j.get("observations", [])
            if o.get("value") not in (None, ".")]


def _fred_series_csv(sid):
    """fredgraph.csv (anahtarsız fallback): tarih,değer; '.' eksik. eski->yeni."""
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": sid}, headers=_UA, timeout=TIMEOUT)
    r.raise_for_status()
    vals = []
    for row in csv.DictReader(io.StringIO(r.text)):
        v = row.get(sid)
        if v and v != ".":
            vals.append(float(v))
    return vals


def fetch_fred():
    """FRED: haftalık makro rejim serileri. FRED_API_KEY varsa resmi API
    (api.stlouisfed.org), yoksa anahtarsız fredgraph.csv fallback.

    30 gözlemlik değişim hesaplanır (günlük seriler ~ takvim 6 hafta):
      DTWEXBGS: geniş dolar endeksi — artış = USD güçleniyor = risk-off
      VIXCLS:   VIX — artış = risk-off
      DGS10:    10Y faiz — artış = sıkılaşma = risk-off
    Host kapalıysa skip."""
    data, errors = {}, []
    fred_key = load_key("FRED_API_KEY")
    for sid in FRED_SERIES:
        try:
            vals = (_fred_series_api(sid, fred_key) if fred_key
                    else _fred_series_csv(sid))
            if len(vals) < 31:
                raise ValueError(f"yetersiz seri ({len(vals)} nokta)")
            data[f"fred_{sid}"] = vals[-1]
            data[f"fred_{sid}_chg30"] = vals[-1] - vals[-31]
            if sid == "DTWEXBGS" and vals[-31] != 0:
                data[f"fred_{sid}_chg30_pct"] = vals[-1] / vals[-31] - 1.0
        except Exception as e:
            errors.append(f"fred {sid} erişilemedi: {_safe_err(e)}")
    return data, errors


# ---------------------------------------------------------------- Etherscan
def fetch_etherscan():
    """Etherscan v2 (ETHERSCAN_API_KEY): şimdilik sadece ETH total supply
    (stats/tokensupply, chainid=1). LT supply tarafı hazırlığı — düşük
    öncelik, bileşen bağlı değil; metrics_raw'a gider. Key yoksa/host
    kapalıysa skip."""
    data, errors = {}, []
    key = load_key("ETHERSCAN_API_KEY")
    if not key:
        errors.append("etherscan: ETHERSCAN_API_KEY yok — atlandı")
        return data, errors
    try:
        j = _get("https://api.etherscan.io/v2/api", params={
            "chainid": "1", "module": "stats", "action": "tokensupply",
            "apikey": key}, timeout=TIMEOUT)
        if j.get("status") != "1":
            raise ValueError(f"API status={j.get('status')}: {j.get('message')}")
        # Wei cinsinden string -> ETH
        data["eth_total_supply"] = float(j["result"]) / 1e18
    except Exception as e:
        errors.append(f"etherscan tokensupply erişilemedi: {_safe_err(e)}")
    return data, errors
