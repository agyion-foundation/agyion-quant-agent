"""
cryptoquant.py — CryptoQuant REST API fetcher (Advanced plan, Bearer key).

Base: https://api.cryptoquant.com — Auth: `Authorization: Bearer {CQ_API_KEY}`
Key env var `CQ_API_KEY`'den okunur; yoksa açık hata ile tüm metrikler atlanır
(key koda GÖMÜLMEZ).

Empirik doğrulanmış endpoint'ler (bu key ile 200 OK):
  /v1/{asset}/market-data/price-ohlcv   ?window=day&limit=N     (btc, eth, trx)
  /v1/{asset}/market-data/funding-rates ?exchange=all_exchange&window=hour&limit=N  (btc, eth, xrp)
  /v1/{asset}/market-data/open-interest ?exchange=all_exchange&window=hour&limit=N  (btc, eth, xrp)
  /v1/{asset}/market-data/liquidations  ?exchange=all_exchange&window=hour&limit=N  (btc, eth, xrp)
  /v1/{asset}/market-data/capitalization?window=day&limit=N     (btc tam: market_cap /
    realized_cap / average_cap / delta_cap; eth 200 OK ama sadece market_cap dolu —
    realized_cap None geldiği için ETH MVRV/NUPL türetilemez)
403 (KULLANILMAZ): tüm on-chain endpoint'ler, avax/sol ohlcv, whale-ratio.

Dönüş sözleşmesi fetchers.py ile aynı: (data: dict, errors: list[str]).
Her çağrı izole try/except; hata durumunda ilgili alan dict'te hiç yer almaz.
Yanıtlar newest-first gelir; seri çıktıları eski->yeni sırasına çevrilir.

Kredi takibi: her başarılı/başarısız endpoint stdout'a loglanır ve dönen
data["_credit_log"] içinde {endpoint: satır_sayısı} toplanır.
"""

import os
from pathlib import Path

import requests

BASE = "https://api.cryptoquant.com"
TIMEOUT = 12

OHLCV_LIMIT = 260    # 200DMA + z-score serisi (ratio serisi >= 30 nokta) için
DERIV_LIMIT = 48     # funding / OI: 24s delta için 48 saat
LIQ_LIMIT = 24       # liquidations: son 24 saat
CAP_LIMIT = 400      # capitalization: 365g pencere + pay için tarihsel seri
TRX_LIMIT = 30       # trx ohlcv: 7g momentum (kredi dostu üst sınır)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

try:  # canonical loader fetchers.py'de (tüm key'ler için ortak)
    from fetchers import load_key as _load_key
except ImportError:  # tek başına import edilirse yerel kopya
    def _load_key(name):
        """Key çözümleme: 1) env var, 2) proje kökü .env (KEY=VALUE).
        Key hiçbir zaman koda gömülmez."""
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


def _load_api_key():
    return _load_key("CQ_API_KEY")


def _get(api_key, path, params):
    r = requests.get(f"{BASE}{path}", params=params,
                     headers={"Authorization": f"Bearer {api_key}"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("result", {}).get("data", [])


def _std(series):
    n = len(series)
    if n < 2:
        return 0.0
    mean = sum(series) / n
    return (sum((v - mean) ** 2 for v in series) / n) ** 0.5


def _derive_capitalization(asset, rows, price):
    """capitalization yanıtından (newest-first) uzun-vade türevleri üretir.

    Dönen dict'teki ham değerler:
      market_cap, realized_cap, mvrv, nupl,
      mvrv_z_raw       = (mc - rc) / std(market_cap, 365g)   [z benzeri]
      bci_momentum     = MA30(MVRV) - MA365(MVRV)            [Bull-Bear Cycle yaklaşımı]
      realized_price_dist = (price - realized_price)/realized_price
      growth_rate_diff = 30g market_cap değişimi - 30g realized_cap değişimi
      mvrv_series      = günlük MVRV serisi (eski->yeni, z-score yedeği için)
    """
    mc = [float(r["market_cap"]) for r in reversed(rows)
          if r.get("market_cap") is not None]
    rc = [float(r["realized_cap"]) for r in reversed(rows)
          if r.get("realized_cap") is not None]
    if len(mc) < 31 or len(rc) != len(mc):
        return None
    out = {"market_cap": mc[-1], "realized_cap": rc[-1]}
    if rc[-1] <= 0:
        return out
    out["mvrv"] = mc[-1] / rc[-1]
    out["nupl"] = (mc[-1] - rc[-1]) / mc[-1]
    out["mvrv_series"] = [m / r for m, r in zip(mc, rc) if r > 0]

    win = mc[-365:] if len(mc) >= 365 else mc
    sd = _std(win)
    if sd > 0:
        out["mvrv_z_raw"] = (mc[-1] - rc[-1]) / sd

    ms = out["mvrv_series"]
    if len(ms) >= 60:
        ma30 = sum(ms[-30:]) / 30.0
        long_win = ms[-365:] if len(ms) >= 365 else ms
        ma_long = sum(long_win) / len(long_win)
        out["bci_momentum"] = ma30 - ma_long

    if price and mc[-1] > 0:
        # supply ≈ market_cap / price; realized_price = realized_cap / supply
        supply = mc[-1] / price
        realized_price = rc[-1] / supply
        out["realized_price"] = realized_price
        out["realized_price_dist"] = (price - realized_price) / realized_price

    out["growth_rate_diff"] = ((mc[-1] / mc[-31] - 1.0)
                               - (rc[-1] / rc[-31] - 1.0))
    return out


def fetch_cryptoquant():
    """4 endpoint × {btc, eth} çeker. CQ_API_KEY yoksa açık hata + boş data."""
    data, errors = {}, []
    credit_log = {}

    api_key = _load_api_key()
    if not api_key:
        errors.append("cryptoquant: CQ_API_KEY ne env var'da ne .env dosyasında — "
                      "tüm CryptoQuant metrikleri atlandı")
        return data, errors

    def call(asset, kind, params, tag=None):
        """İzole endpoint çağrısı + kredi logu. Hata -> None + errors'a not."""
        path = f"/v1/{asset}/market-data/{kind}"
        label = f"{asset}/{kind}" + (f"({tag})" if tag else "")
        try:
            rows = _get(api_key, path, params)
            if not isinstance(rows, list) or not rows:
                raise ValueError("boş yanıt")
            credit_log[label] = len(rows)
            print(f"  [cryptoquant] {label}: OK, {len(rows)} satır")
            return rows
        except Exception as e:
            credit_log[label] = 0
            errors.append(f"cryptoquant {label} başarısız: {type(e).__name__}: {e}")
            print(f"  [cryptoquant] {label}: HATA ({type(e).__name__})")
            return None

    # ---- OHLCV (günlük): 200DMA + 24s momentum --------------------------
    for asset in ("btc", "eth"):
        rows = call(asset, "price-ohlcv",
                    {"window": "day", "limit": str(OHLCV_LIMIT)})
        if rows:
            # newest-first -> eski->yeni close serisi
            closes = [float(r["close"]) for r in reversed(rows)
                      if r.get("close") is not None]
            if len(closes) >= 200:
                dma200 = sum(closes[-200:]) / 200.0
                ratio_series = []
                for i in range(199, len(closes)):
                    ma = sum(closes[i - 199:i + 1]) / 200.0
                    ratio_series.append(closes[i] / ma - 1.0)
                data[f"{asset}_price"] = closes[-1]
                data[f"{asset}_200dma"] = dma200
                data[f"{asset}_price_vs_200dma"] = closes[-1] / dma200 - 1.0
                data[f"{asset}_vs_200dma_series"] = ratio_series
            if len(closes) >= 2:
                # gün kapanışları: son close vs bir önceki close ~ 24s momentum
                data[f"{asset}_24h_change"] = closes[-1] / closes[-2] - 1.0

    # ---- Funding rates (saatlik, all_exchange ortalaması, YÜZDE birimi) --
    # CQ funding_rates saatlik ve yüzde biriminde (ör. 0.005 = %0.005/saat).
    # OKX ile karşılaştırılabilir 8 saatlik ondalık orana çevrilir:
    # rate_8h = cq_degeri / 100 * 8
    # XRP: Advanced planda full market-data kapsamalı (test edildi: 200 OK).
    for asset in ("btc", "eth", "xrp"):
        rows = call(asset, "funding-rates",
                    {"exchange": "all_exchange", "window": "hour",
                     "limit": str(DERIV_LIMIT)})
        if rows:
            data[f"{asset}_funding"] = float(rows[0]["funding_rates"]) / 100.0 * 8.0
            if len(rows) >= 25:
                data[f"{asset}_funding_24h_ago"] = \
                    float(rows[24]["funding_rates"]) / 100.0 * 8.0

    # ---- Open interest (saatlik, USD, all_exchange toplamı) ---------------
    for asset in ("btc", "eth", "xrp"):
        rows = call(asset, "open-interest",
                    {"exchange": "all_exchange", "window": "hour",
                     "limit": str(DERIV_LIMIT)})
        if rows:
            data[f"{asset}_oi_usd"] = float(rows[0]["open_interest"])
            if len(rows) >= 25:
                data[f"{asset}_oi_usd_prev"] = float(rows[24]["open_interest"])

    # ---- Liquidations (saatlik, USD) -> son 24 saatin toplamı -------------
    for asset in ("btc", "eth", "xrp"):
        rows = call(asset, "liquidations",
                    {"exchange": "all_exchange", "window": "hour",
                     "limit": str(LIQ_LIMIT)})
        if rows:
            data[f"{asset}_long_liq_usd_24h"] = sum(
                float(r.get("long_liquidations_usd") or 0) for r in rows)
            data[f"{asset}_short_liq_usd_24h"] = sum(
                float(r.get("short_liquidations_usd") or 0) for r in rows)

    # ---- Capitalization (günlük): MVRV / NUPL / BCI türevleri -----------
    # Tek istek 400 günlük seri çeker (kredi dostu). Türetilen ham değerler
    # data[f"{asset}_cap"] altına konur; [-1,+1] normalize engine tarafında.
    for asset in ("btc", "eth"):
        rows = call(asset, "capitalization",
                    {"window": "day", "limit": str(CAP_LIMIT)})
        if rows:
            cap = _derive_capitalization(asset, rows, data.get(f"{asset}_price"))
            if cap:
                data[f"{asset}_cap"] = cap

    # ---- Premium index'ler (BTC): Coinbase (saatlik, %) + Kore (günlük, %)
    # Test edildi: 200 OK. Pozitif premium = o borsada/bölgede alım baskısı.
    rows = call("btc", "coinbase-premium-index",
                {"window": "hour", "limit": "24"})
    if rows:
        data["btc_cb_premium"] = float(rows[0]["coinbase_premium_index"])
    rows = call("btc", "korea-premium-index",
                {"window": "day", "limit": "30"})
    if rows:
        data["btc_kr_premium"] = float(rows[0]["korea_premium_index"])

    # ---- Günlük OI (90g): ETH/BTC OI oranı + 90g ortalama sapması -------
    for asset in ("btc", "eth"):
        rows = call(asset, "open-interest",
                    {"exchange": "all_exchange", "window": "day",
                     "limit": "90"}, tag="day")
        if rows:
            series = [float(r["open_interest"]) for r in reversed(rows)
                      if r.get("open_interest") is not None]
            if len(series) >= 30:
                data[f"{asset}_oi_daily"] = series

    # ---- TRX OHLCV (günlük): 7g getiri ----------------------------------
    rows = call("trx", "price-ohlcv", {"window": "day", "limit": str(TRX_LIMIT)})
    if rows:
        closes = [float(r["close"]) for r in reversed(rows)
                  if r.get("close") is not None]
        if len(closes) >= 8:
            data["trx_price"] = closes[-1]
            data["trx_7d_return"] = closes[-1] / closes[-8] - 1.0

    total_rows = sum(credit_log.values())
    ok_calls = sum(1 for v in credit_log.values() if v > 0)
    print(f"  [cryptoquant] kredi özeti: {ok_calls}/{len(credit_log)} endpoint OK, "
          f"~{total_rows} satır çekildi")
    data["_credit_log"] = credit_log
    return data, errors
