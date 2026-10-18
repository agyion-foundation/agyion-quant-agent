"""
fetchers.py — Ücretsiz veri kaynaklarından ham metrikleri çeker.

Her fetcher izole try/except ile çalışır: biri patlarsa kalanlar devam eder.
Dönüş sözleşmesi:
  fetch_all() -> (data: dict, errors: list[str])
  data:  alan adı -> ham değer (float / list[float])
  errors: insan-okunur Türkçe hata notları (snapshot.errors'a gider)

Seri dönenler (z-score için): `btc_vs_200dma_series` gibi geçmiş->bugün sıralı
listeler. Tekil değerler düz float.
"""

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TIMEOUT = 12
HEADERS = {"User-Agent": "quant-agent/0.1 (+local research)"}
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_key(name):
    """Ortak key çözümleme (v0.5): 1) env var, 2) proje kökü .env (KEY=VALUE).
    Key hiçbir zaman koda gömülmez; dosyalar/endpoint'ler key'siz fallback'e sahip."""
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


def _safe_err(e):
    """Hata metninden olası secret sızıntısını maskeler (key'ler asla
    snapshot/errors içinde görünmez)."""
    msg = str(e)
    for k in ("CQ_API_KEY", "FRED_API_KEY", "BGEOMETRICS_API_KEY",
              "COINGECKO_API_KEY", "ETHERSCAN_API_KEY", "ANTHROPIC_API_KEY"):
        v = load_key(k)
        if v and v in msg:
            msg = msg.replace(v, "***")
    return msg


# ------------------------------------------------------------- CoinGecko
def fetch_coingecko(days=365):
    """BTC/ETH/AVAX fiyatları + BTC 200g ortalama sapması ve serisi.

    market_chart?days=365 günlük nokta döner; seriden 200DMA ve
    (fiyat/200DMA - 1) oran serisi türetilir.
    """
    out = {}
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "bitcoin,ethereum,avalanche-2", "vs_currencies": "usd",
                "include_24hr_change": "true"},
        headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    out["btc_price"] = float(j["bitcoin"]["usd"])
    out["eth_price"] = float(j["ethereum"]["usd"])
    out["avax_price"] = float(j["avalanche-2"]["usd"])
    out["btc_24h_change"] = float(j["bitcoin"].get("usd_24h_change", 0.0)) / 100.0
    out["eth_24h_change"] = float(j["ethereum"].get("usd_24h_change", 0.0)) / 100.0
    out["avax_24h_change"] = float(j["avalanche-2"].get("usd_24h_change", 0.0)) / 100.0

    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
        params={"vs_currency": "usd", "days": str(days), "interval": "daily"},
        headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    prices = [p[1] for p in r.json()["prices"]]
    if len(prices) < 200:
        raise ValueError(f"market_chart serisi kısa: {len(prices)} nokta")
    dma200 = sum(prices[-200:]) / 200.0
    ratio_series = []
    for i in range(199, len(prices)):
        ma = sum(prices[i - 199:i + 1]) / 200.0
        ratio_series.append(prices[i] / ma - 1.0)
    out["btc_200dma"] = dma200
    out["btc_price_vs_200dma"] = out["btc_price"] / dma200 - 1.0
    out["btc_vs_200dma_series"] = ratio_series
    return out


# ------------------------------------------------------------------- OKX
def _okx_get(path, params):
    r = requests.get(f"https://www.okx.com{path}", params=params,
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if str(j.get("code")) != "0":
        raise ValueError(f"OKX code={j.get('code')} msg={j.get('msg')}")
    return j["data"]


def _okx_funding(inst):
    """Güncel funding yoksa (sınırlar içi) bir sonraki tahmini funding'i kullan."""
    d = _okx_get("/api/v5/public/funding-rate", {"instId": inst})[0]
    cur = d.get("fundingRate")
    if cur not in (None, ""):
        return float(cur)
    nxt = d.get("nextFundingRate")
    if nxt not in (None, ""):
        return float(nxt)
    raise ValueError(f"{inst}: fundingRate/nextFundingRate boş")


def fetch_okx():
    """AVAX-USDT-SWAP funding + OI değişimi, BTC-USDT-SWAP funding."""
    out = {}
    out["avax_funding"] = _okx_funding("AVAX-USDT-SWAP")
    out["btc_funding"] = _okx_funding("BTC-USDT-SWAP")

    d = _okx_get("/api/v5/public/open-interest",
                 {"instType": "SWAP", "instId": "AVAX-USDT-SWAP"})[0]
    out["avax_oi_contracts"] = float(d["oi"])

    # 24s önceki OI: funding-rate-history yok; OI history endpoint'i yok.
    # Yaklaşık: SWap candles'dan OI türetilemez -> oi_change için
    # 24s önceki snapshot'ı local cache'den oku (data/oi_cache.json).
    out["avax_oi_prev"] = None  # cache yoksa None kalır (bileşen düşer)
    return out


def fetch_okx_oi_change(cache_path):
    """OI 24s değişimini yerel cache ile hesaplar: her koşuda güncel OI'yi
    yazar; 20-28 saat arası eski kayıt varsa onunla karşılaştırır."""
    import json

    now = datetime.now(timezone.utc)
    cache = {"history": []}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {"history": []}
    return cache, now  # hesap fetch_all içinde (güncel OI bilinince)


# -------------------------------------------------------------- DefiLlama
def fetch_defillama():
    """Avalanche TVL 30g momentumu + AVAX stablecoin mcap 30g momentumu."""
    out = {}
    r = requests.get("https://api.llama.fi/v2/historicalChainTvl/Avalanche",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    series = [(int(p["date"]), float(p["tvl"])) for p in r.json()]
    if len(series) < 31:
        raise ValueError(f"DefiLlama TVL serisi kısa: {len(series)}")
    out["avax_tvl_usd"] = series[-1][1]
    out["avax_tvl_prev_usd"] = series[-31][1]
    out["avax_tvl_momentum_30d"] = series[-1][1] / series[-31][1] - 1.0

    r = requests.get("https://stablecoins.llama.fi/stablecoincharts/Avalanche",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    pts = [(int(p["date"]), float(p["totalCirculatingUSD"]["peggedUSD"]))
           for p in r.json()]
    if len(pts) < 31:
        raise ValueError(f"DefiLlama stable serisi kısa: {len(pts)}")
    out["stable_mcap_usd"] = pts[-1][1]
    out["stable_mcap_prev_usd"] = pts[-31][1]
    out["stablecoin_mcap_momentum"] = pts[-1][1] / pts[-31][1] - 1.0
    return out


# ------------------------------------------------------------ BGeometrics
def _bg_get(path, params=None, key=None):
    """BGeometrics free API. BGEOMETRICS_API_KEY varsa Authorization: Bearer
    header'ı eklenir (yoksa key'siz public endpoint davranışı)."""
    headers = dict(HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = requests.get(f"https://bitcoin-data.com{path}", params=params or {},
                     headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _bg_series(payload):
    """BGeometrics yanıtını (tarih, değer) listesine çevirir; hem list-of-lists
    hem list-of-dicts formatını tolere eder."""
    series = []
    for row in payload:
        if isinstance(row, dict):
            d, v = row.get("d") or row.get("date"), row.get("v") or row.get("value")
        else:
            d, v = row[0], row[1]
        if v is None:
            continue
        try:
            series.append((str(d), float(v)))
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda t: t[0])
    return series


def fetch_bgeometrics():
    """BTC MVRV, NUPL, SOPR (tekil + 365g seri — z-score için)."""
    out = {}
    bg_key = load_key("BGEOMETRICS_API_KEY")
    for name, path in (("mvrv", "/v1/mvrv"), ("nupl", "/v1/nupl"),
                       ("sopr", "/v1/sopr")):
        series = _bg_series(_bg_get(path, key=bg_key))
        if not series:
            raise ValueError(f"bgeometrics {path}: boş seri")
        out[f"btc_{name}"] = series[-1][1]
        out[f"btc_{name}_series"] = [v for _, v in series[-365:]]
    # supply in profit: /v1/profit-loss (amount + relative); relative = oran.
    pl = _bg_get("/v1/profit-loss", key=bg_key)
    if isinstance(pl, dict):
        # format tahmini: {"supply_profit": ..., "supply_loss": ...} veya seri
        for k in ("relative", "supply_profit_rel", "in_profit_rel"):
            if pl.get(k) is not None:
                out["btc_supply_profit"] = float(pl[k])
                break
        else:
            total = (pl.get("supply_profit") or 0) + (pl.get("supply_loss") or 0)
            if total:
                out["btc_supply_profit"] = float(pl["supply_profit"]) / float(total)
    elif isinstance(pl, list):
        series = _bg_series(pl)
        if series:
            out["btc_supply_profit"] = series[-1][1]
    return out


# --------------------------------------------------------- alternative.me
def fetch_fear_greed():
    """alternative.me Fear & Greed Index (0-100)."""
    r = requests.get("https://api.alternative.me/fng/", params={"limit": "1"},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()["data"][0]
    return {"fear_greed": float(d["value"]),
            "fear_greed_label": d.get("value_classification", "")}


# ----------------------------------------------------------- mempool.space
def fetch_mempool_hashrate():
    """Hashrate MA30/MA60 (hash ribbon yaklaşımı).

    /api/v1/mining/hashrate/3m haftalık noktalar döner; MA30 ~ son 4-5 nokta,
    MA60 ~ son 8-9 nokta ortalamasıyla yakınsanır.
    """
    r = requests.get("https://mempool.space/api/v1/mining/hashrate/3m",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    rates = [float(p["avgHashrate"]) for p in j.get("hashrates", [])
             if p.get("avgHashrate") is not None]
    if len(rates) < 9:
        raise ValueError(f"mempool hashrate serisi kısa: {len(rates)}")
    ma30 = sum(rates[-4:]) / 4.0
    ma60 = sum(rates[-9:]) / 9.0
    return {"btc_hash_ma30": ma30, "btc_hash_ma60": ma60,
            "btc_hash_ribbon": ma30 / ma60 - 1.0 if ma60 else None}


# --------------------------------------------------------------- Binance
def fetch_binance_taker():
    """BTCUSDT taker long/short ratio 7g eğimi (günlük noktalar, en küçük
    kareler slope). /futures/data/takerlongshortRatio (public)."""
    r = requests.get(
        "https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": "BTCUSDT", "period": "1d", "limit": "7"},
        headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    vals = [float(x["buySellRatio"]) for x in rows if x.get("buySellRatio")]
    if len(vals) < 5:
        raise ValueError(f"binance taker serisi kısa: {len(vals)}")
    n = len(vals)
    xs = list(range(n))
    xm, ym = sum(xs) / n, sum(vals) / n
    denom = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, vals)) / denom
    return {"binance_taker_slope": slope,
            "binance_taker_ratio_latest": vals[-1]}


# ------------------------------------------------------------------- FRED
def _fred_series(sid, days=45):
    """FRED günlük serisi (son N gün). FRED_API_KEY varsa resmi API
    (api.stlouisfed.org/fred/series/observations), yoksa key'siz
    fredgraph.csv fallback. (tarih, değer) listesi döner; '.' = eksik atlanır."""
    key = load_key("FRED_API_KEY")
    if key:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": sid, "api_key": key,
                    "file_type": "json",
                    "observation_start": (datetime.now(timezone.utc)
                                          - timedelta(days=days)).strftime("%Y-%m-%d")},
            headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        obs = [(o["date"], o["value"]) for o in r.json().get("observations", [])]
    else:
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": sid}, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        lines = [l for l in r.text.strip().splitlines() if "," in l]
        obs = [tuple(l.split(",", 1)) for l in lines[1:]][-days:]
    series = []
    for d, v in obs:
        try:
            series.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    if len(series) < 22:
        raise ValueError(f"FRED {sid} serisi kısa: {len(series)}")
    return series


def fetch_fred():
    """Makro rejim serileri: dolar endeksi (DTWEXBGS), VIX (VIXCLS), 10Y (DGS10).
    30 takvim günü önceki en yakın gözlemle karşılaştırma (~21 iş günü)."""
    out = {}
    out["fred_source"] = "api" if load_key("FRED_API_KEY") else "fredgraph.csv"
    for sid, name in (("DTWEXBGS", "dollar"), ("VIXCLS", "vix"), ("DGS10", "dgs10")):
        s = _fred_series(sid)
        out[f"fred_{name}_latest"] = s[-1][1]
        out[f"fred_{name}_momentum_30d"] = (s[-1][1] - s[-22][1]) / abs(s[-22][1]) \
            if sid == "DTWEXBGS" else s[-1][1] - s[-22][1]
        # dolar için göreli değişim, VIX/DGS10 için mutlak puan değişimi
    return out


# ------------------------------------------------------------ CoinMetrics
def fetch_coinmetrics():
    """Community API: BTC/ETH aktif adres + tx sayısı (30g) -> ETH/BTC oranı."""
    out = {}
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    vals = {}
    for asset in ("btc", "eth"):
        r = requests.get(
            f"https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            params={"assets": asset, "metrics": "AdrActCnt,TxCnt",
                    "start_time": start, "end_time": end,
                    "frequency": "1d", "page_size": "100"},
            headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("data", [])
        addr = [float(x["AdrActCnt"]) for x in rows if x.get("AdrActCnt")]
        txs = [float(x["TxCnt"]) for x in rows if x.get("TxCnt")]
        if not addr or not txs:
            raise ValueError(f"coinmetrics {asset}: boş seri")
        vals[asset] = (sum(addr) / len(addr), sum(txs) / len(txs))
    out["eth_btc_addr_ratio"] = vals["eth"][0] / vals["btc"][0]
    out["eth_btc_tx_ratio"] = vals["eth"][1] / vals["btc"][1]
    return out


# --------------------------------------------------------------- Etherscan
def fetch_etherscan():
    """v2 API: ETH toplam arzı (stats/tokensupply). Düşük öncelikli besleme
    metriği — bileşen bağlı değil, metrics_raw'a gider. Key yoksa atlanır."""
    key = load_key("ETHERSCAN_API_KEY")
    if not key:
        raise ValueError("ETHERSCAN_API_KEY yok")
    r = requests.get("https://api.etherscan.io/v2/api",
                     params={"chainid": "1", "module": "stats",
                             "action": "tokensupply", "apikey": key},
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if str(j.get("status")) != "1":
        raise ValueError(f"etherscan: {j.get('message')} {j.get('result')}")
    return {"eth_total_supply": float(j["result"]) / 1e18}


# ------------------------------------------------------- CoinGecko (geniş)
def fetch_coingecko_market():
    """/global (BTC dominansı) + /coins/markets top100'den en iyi/en kötü 5
    24s performans. Alt rejim hazırlığı — metrics_raw'a gider."""
    out = {}
    headers = dict(HEADERS)
    cg_key = load_key("COINGECKO_API_KEY")
    if cg_key:
        headers["x-cg-demo-api-key"] = cg_key
    r = requests.get("https://api.coingecko.com/api/v3/global",
                     headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    out["btc_dominance"] = float(
        r.json()["data"]["market_cap_percentage"].get("btc", 0.0))
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "order": "market_cap_desc",
                "per_page": "100", "page": "1", "price_change_percentage": "24h"},
        headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    rows = [(c["symbol"].upper(),
             float(c.get("price_change_percentage_24h_in_currency") or 0.0))
            for c in r.json()]
    rows.sort(key=lambda t: t[1], reverse=True)
    out["top_performers"] = rows[:5]
    out["worst_performers"] = rows[-5:][::-1]
    return out


# ----------------------------------------------------------- OKX spot/perp
def fetch_okx_spot_perp():
    """AVAX spot (AVAX-USDT) ile perp (AVAX-USDT-SWAP) fiyat farkı (primi)."""
    spot = float(_okx_get("/api/v5/market/ticker", {"instId": "AVAX-USDT"})[0]["last"])
    swap = float(_okx_get("/api/v5/market/ticker", {"instId": "AVAX-USDT-SWAP"})[0]["last"])
    return {"avax_spot_price": spot, "avax_swap_price": swap,
            "okx_spot_perp_gap": (swap - spot) / spot if spot else None}


# ----------------------------------------------------------------- orchestrator
def fetch_all():
    """Tüm ücretsiz kaynakları sırayla çağırır; her biri izole try/except."""
    data, errors = {}, {}

    sources = (
        ("coingecko", fetch_coingecko),
        ("okx", fetch_okx),
        ("defillama", fetch_defillama),
        ("bgeometrics", fetch_bgeometrics),
        ("fear_greed", fetch_fear_greed),
        ("mempool_hashrate", fetch_mempool_hashrate),
        ("binance_taker", fetch_binance_taker),
        ("fred", fetch_fred),
        ("coinmetrics", fetch_coinmetrics),
        ("okx_spot_perp", fetch_okx_spot_perp),
        ("coingecko_market", fetch_coingecko_market),
        ("etherscan", fetch_etherscan),
    )
    for name, fn in sources:
        try:
            data.update(fn())
            print(f"  [fetch] {name}: OK")
        except Exception as e:  # noqa: BLE001 — kaynak izolasyonu kasıtlı
            errors[name] = f"{name} başarısız: {type(e).__name__}: {_safe_err(e)}"
            print(f"  [fetch] {name}: HATA ({type(e).__name__})")

    # 24s fiyat momentumu (BTC/ETH/AVAX ortalaması)
    changes = [data.get(k) for k in ("btc_24h_change", "eth_24h_change", "avax_24h_change")]
    changes = [c for c in changes if c is not None]
    if changes:
        data["price_momentum_24h"] = sum(changes) / len(changes)

    # OI 24s değişimi: cache üzerinden
    if data.get("avax_oi_contracts") is not None:
        cache_path = Path(__file__).resolve().parent.parent / "data" / "oi_cache.json"
        try:
            data["avax_oi_change_24h"] = _oi_change_with_cache(
                cache_path, data["avax_oi_contracts"])
        except Exception as e:  # noqa: BLE001
            errors["oi_cache"] = f"oi_cache başarısız: {type(e).__name__}: {_safe_err(e)}"

    return data, list(errors.values())


def _oi_change_with_cache(cache_path, current_oi):
    """Cache'e (timestamp, oi) ekler; 20-28s arası eski kayda göre değişim
    döndürür. Uygun eski kayıt yoksa None (bileşen düşer)."""
    import json

    now = datetime.now(timezone.utc)
    hist = []
    if cache_path.exists():
        try:
            hist = json.loads(cache_path.read_text(encoding="utf-8")).get("history", [])
        except (OSError, json.JSONDecodeError):
            hist = []
    prev = None
    for rec in hist:
        try:
            ts = datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if 20.0 <= age_h <= 28.0:
            prev = float(rec["oi"])
            break
    hist.append({"ts": now.isoformat(timespec="seconds"), "oi": current_oi})
    hist = hist[-200:]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"history": hist}, ensure_ascii=False),
                          encoding="utf-8")
    if prev and prev > 0:
        return current_oi / prev - 1.0
    return None
