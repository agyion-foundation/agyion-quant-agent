#!/usr/bin/env python3
"""atk_bridge.py — Quant Agent <-> OKX Agent Trade Kit (ATK) köprüsü.

ATK = @okx_ai/okx-trade-mcp (OKX'in resmi MCP sunucusu, GitHub: okx/agent-trade-kit).
Bu modül üç katmanlı bir köprü sağlar:

  1) MCP stdio client  : node sürecini spawn edip JSON-RPC (MCP) konuşur.
                         Komut: npx -y @okx_ai/okx-trade-mcp --profile demo --modules market,spot,account
  2) REST fallback     : node/npm yoksa aynı işlevleri doğrudan OKX V5 REST ile yapar
                         (public market verisi anahtarsız; emir/pozisyon HMAC imzalı,
                          anahtarlar env'den: OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE).
  3) Mock              : ağ da kapalıysa gerçek çağrı imzasını + beklenen yanıt şemasını
                         kaydedip deterministik simüle sonuç döner (simulated: true işaretli).

Her çağrı atk/demo_log.json'a şu alanlarla kaydedilir:
  tool, arguments, transport (mcp|rest|mock), request_signature,
  expected_response (şema), response (gerçek ya da simüle), ok, ts.

Kullanım:
  python3 atk_bridge.py --demo-run            # tüm akışı koş, demo_log.json üret
  python3 atk_bridge.py --price BTC-USDT      # tek fiyat sorgusu
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

ATK_DIR = Path(__file__).resolve().parent
DEMO_LOG = ATK_DIR / "demo_log.json"
OKX_REST = "https://www.okx.com/api/v5"
MCP_CMD_DEFAULT = ["npx", "-y", "@okx_ai/okx-trade-mcp",
                   "--profile", "demo", "--modules", "market,spot,account"]
PROTOCOL_VERSION = "2024-11-05"
TIMEOUT_S = 60

# ---------------------------------------------------------------------------
# ATK tool eşleme tablosu (modül:tool -> OKX V5 endpoint + bizim işlevimiz)
# tools/list çıktısından doğrulandı (okx-trade-mcp v1.4.8, 167 tool / 11 modül).
# ---------------------------------------------------------------------------
TOOL_MAP = {
    "get_price": {
        "atk_tool": "market_get_ticker", "atk_module": "market",
        "rest": "GET /api/v5/market/ticker?instId={instId}", "auth": False,
    },
    "demo_market_order": {
        "atk_tool": "spot_place_order", "atk_module": "spot",
        "rest": "POST /api/v5/trade/order", "auth": True,
    },
    "get_positions": {
        "atk_tool": "account_get_positions", "atk_module": "account",
        "rest": "GET /api/v5/account/positions", "auth": True,
    },
    "get_balance": {
        "atk_tool": "account_get_balance", "atk_module": "account",
        "rest": "GET /api/v5/account/balance", "auth": True,
    },
}

# Beklenen yanıt şemaları (dokümantasyon + mock için)
EXPECTED = {
    "market_get_ticker": {
        "tool": "market_get_ticker", "ok": True,
        "data": {"endpoint": "GET /api/v5/market/ticker", "data": [{
            "instType": "SPOT", "instId": "BTC-USDT", "last": "81486.4",
            "askPx": "...", "bidPx": "...", "open24h": "...", "high24h": "...",
            "low24h": "...", "vol24h": "...", "ts": "..."}]},
    },
    "spot_place_order": {
        "tool": "spot_place_order", "ok": True,
        "data": {"endpoint": "POST /api/v5/trade/order", "data": [{
            "ordId": "<okx-order-id>", "clOrdId": "<client-order-id>",
            "sCode": "0", "sMsg": ""}]},
        "demo_note": "demo profiliyle OKX simulated-trading'e gider; "
                     "başarıda sCode=0 döner, bakiye demo USDT'den düşer.",
    },
    "account_get_positions": {
        "tool": "account_get_positions", "ok": True,
        "data": {"endpoint": "GET /api/v5/account/positions", "data": [
            {"instId": "BTC-USDT-SWAP", "pos": "...", "avgPx": "...",
             "upl": "...", "lever": "..."}]},
        "note": "Spot için pozisyon kavramı yok; bakiye account_get_balance ile okunur.",
    },
    "account_get_balance": {
        "tool": "account_get_balance", "ok": True,
        "data": {"endpoint": "GET /api/v5/account/balance", "data": [{
            "totalEq": "...", "details": [{"ccy": "USDT", "eq": "..."}]}]},
    },
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _load_dotenv() -> None:
    """Repo kökündeki .env'den OKX_* anahtarlarını oku (varsa). Secret loglanmaz."""
    env_path = ATK_DIR.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("OKX_") and k not in os.environ:
                    os.environ[k] = v.strip()


# ---------------------------------------------------------------------------
# 1) MCP stdio client
# ---------------------------------------------------------------------------
class McpClient:
    """Minimal MCP stdio client: node süreci spawn + satır-temelli JSON-RPC."""

    def __init__(self, cmd: list[str] | None = None):
        self.cmd = cmd or self._resolve_cmd()
        self.proc: subprocess.Popen | None = None
        self._id = 0

    @staticmethod
    def _resolve_cmd() -> list[str]:
        local = ATK_DIR / "node_modules" / "@okx_ai" / "okx-trade-mcp" / "dist" / "index.js"
        node = shutil.which("node")
        if local.exists() and node:
            return [node, str(local), "--profile", "demo",
                    "--modules", "market,spot,account"]
        if shutil.which("npx"):
            return list(MCP_CMD_DEFAULT)
        raise RuntimeError("node/npx bulunamadı — MCP transport kullanılamaz")

    def start(self) -> None:
        env = dict(os.environ)
        _load_dotenv()
        # ATK config önceliği: ~/.okx/config.toml > OKX_* env. Placeholder yok;
        # anahtar yoksa sunucu hasAuth:false ile açılır, market modülü yine çalışır.
        if os.environ.get("OKX_API_KEY"):
            env["OKX_API_KEY"] = os.environ["OKX_API_KEY"]
            env["OKX_SECRET_KEY"] = os.environ.get("OKX_SECRET_KEY", "")
            env["OKX_PASSPHRASE"] = os.environ.get("OKX_PASSPHRASE", "")
            env.setdefault("OKX_DEMO", "1")
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "quant-agent-atk-bridge", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def _rpc(self, method: str, params: dict) -> dict:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP sunucusu kapandı (method={method})")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"JSON-RPC error: {msg['error']}")
                return msg.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name: str, arguments: dict) -> dict:
        """tools/call -> ATK'nın döndürdüğü iç JSON'u parse eder."""
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = res.get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return {"raw_text": content[0]["text"]}
        return res

    def stop(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc = None


# ---------------------------------------------------------------------------
# 2) REST fallback (OKX V5)
# ---------------------------------------------------------------------------
class RestClient:
    def __init__(self):
        _load_dotenv()
        self.key = os.environ.get("OKX_API_KEY", "")
        self.secret = os.environ.get("OKX_SECRET_KEY", "")
        self.passphrase = os.environ.get("OKX_PASSPHRASE", "")
        self.demo = os.environ.get("OKX_DEMO", "1") == "1"

    def _request(self, method: str, path: str, body: dict | None = None,
                 auth: bool = False) -> dict:
        payload = json.dumps(body) if body else ""
        ts = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        headers = {"Content-Type": "application/json"}
        if self.demo:
            headers["x-simulated-trading"] = "1"
        if auth:
            if not (self.key and self.secret and self.passphrase):
                raise PermissionError("REST auth için OKX_API_KEY/OKX_SECRET_KEY/"
                                      "OKX_PASSPHRASE env gerekli")
            prehash = ts + method + path + payload
            sign = base64.b64encode(hmac.new(
                self.secret.encode(), prehash.encode(),
                hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign,
                            "OK-ACCESS-TIMESTAMP": ts,
                            "OK-ACCESS-PASSPHRASE": self.passphrase})
        req = urllib.request.Request(
            OKX_REST + path, data=payload.encode() or None,
            headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode())

    def get_ticker(self, inst_id: str) -> dict:
        return self._request("GET", f"/market/ticker?instId={inst_id}")

    def market_order(self, inst_id: str, side: str, size: str,
                     tgt_ccy: str = "quote_ccy") -> dict:
        body = {"instId": inst_id, "tdMode": "cash", "side": side,
                "ordType": "market", "sz": size, "tgtCcy": tgt_ccy}
        return self._request("POST", "/trade/order", body, auth=True)

    def positions(self) -> dict:
        return self._request("GET", "/account/positions", auth=True)


# ---------------------------------------------------------------------------
# 3) Mock (ağ kapalı senaryo) — deterministik, açıkça işaretli
# ---------------------------------------------------------------------------
def mock_response(tool: str, args: dict) -> dict:
    base = {"tool": tool, "ok": True, "simulated": True,
            "mock_reason": "sandbox ağı/MCP kapalı — gerçek çağrı imzası ve "
                           "beklenen yanıt kaydedildi, sonuç simüle edildi",
            "timestamp": _now()}
    if tool == "market_get_ticker":
        base["data"] = {"endpoint": "GET /api/v5/market/ticker", "data": [{
            "instType": "SPOT", "instId": args.get("instId", "BTC-USDT"),
            "last": "81500.0", "askPx": "81500.1", "bidPx": "81499.9",
            "ts": "0"}]}
    elif tool == "spot_place_order":
        base["data"] = {"endpoint": "POST /api/v5/trade/order", "data": [{
            "ordId": "MOCK-DEMO-0001", "clOrdId": args.get("clOrdId", ""),
            "sCode": "0", "sMsg": ""}]}
    elif tool == "account_get_positions":
        base["data"] = {"endpoint": "GET /api/v5/account/positions", "data": []}
    elif tool == "account_get_balance":
        base["data"] = {"endpoint": "GET /api/v5/account/balance", "data": [{
            "totalEq": "10000", "details": [
                {"ccy": "USDT", "eq": "10000"}]}]}
    return base


# ---------------------------------------------------------------------------
# Köprü cephesi: MCP -> REST -> Mock sırasıyla dener, her şeyi loglar
# ---------------------------------------------------------------------------
class AtkBridge:
    def __init__(self, log_path: Path = DEMO_LOG):
        self.log_path = log_path
        self._mcp: McpClient | None = None
        self._mcp_failed = False
        self._rest: RestClient | None = None
        self.entries: list[dict] = []
        if log_path.exists():
            try:
                self.entries = json.loads(log_path.read_text(encoding="utf-8"))
            except Exception:
                self.entries = []

    # -- transport seçimi --
    def _get_mcp(self) -> McpClient | None:
        if self._mcp_failed:
            return None
        if self._mcp is None:
            try:
                self._mcp = McpClient()
                self._mcp.start()
            except Exception:
                self._mcp = None
                self._mcp_failed = True
        return self._mcp

    def _get_rest(self) -> RestClient:
        if self._rest is None:
            self._rest = RestClient()
        return self._rest

    # -- loglama --
    def _record(self, fn: str, args: dict, transport: str, ok: bool,
                response: dict) -> dict:
        spec = TOOL_MAP[fn]
        entry = {
            "ts": _now(), "function": fn,
            "atk_tool": spec["atk_tool"], "atk_module": spec["atk_module"],
            "transport": transport, "arguments": args,
            "request_signature": {
                "mcp": {"method": "tools/call",
                        "params": {"name": spec["atk_tool"], "arguments": args}},
                "rest_equivalent": spec["rest"],
            },
            "expected_response": EXPECTED[spec["atk_tool"]],
            "ok": ok, "response": response,
        }
        self.entries.append(entry)
        tmp = self.log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.log_path)  # atomik
        return entry

    # -- public API --
    def _run(self, fn: str, args: dict, mcp_tool: str,
             rest_fn) -> dict:
        mcp = self._get_mcp()
        if mcp:
            try:
                resp = mcp.call_tool(mcp_tool, args)
                ok = bool(resp.get("ok")) and not resp.get("error")
                return self._record(fn, args, "mcp", ok, resp)
            except Exception as e:
                self._mcp_failed = True
                self._record(fn, args, "mcp", False,
                             {"transport_error": str(e)})
        try:
            resp = rest_fn(self._get_rest())
            ok = str(resp.get("code", "0")) == "0"
            return self._record(fn, args, "rest", ok, resp)
        except Exception as e:
            resp = mock_response(mcp_tool, args)
            resp["upstream_error"] = str(e)
            return self._record(fn, args, "mock", True, resp)

    def get_price(self, inst_id: str = "BTC-USDT") -> dict:
        """Son fiyat. Dönüş: {'price': float|None, 'entry': log kaydı}."""
        args = {"instId": inst_id}
        entry = self._run("get_price", args, "market_get_ticker",
                          lambda r: r.get_ticker(inst_id))
        price = None
        resp = entry["response"]
        try:
            if entry["transport"] == "rest":
                price = float(resp["data"][0]["last"])
            else:
                price = float(resp["data"]["data"][0]["last"])
        except Exception:
            pass
        return {"price": price, "entry": entry}

    def demo_market_order(self, inst_id: str, side: str, size: str,
                          tgt_ccy: str = "quote_ccy") -> dict:
        """Demo (simulated-trading) market emri. side: buy|sell.
        Spot market alımda size quote (USDT), satışta base (BTC) cinsinden."""
        args = {"instId": inst_id, "tdMode": "cash", "side": side,
                "ordType": "market", "sz": str(size),
                "tgtCcy": "quote_ccy" if side == "buy" else "base_ccy",
                "clOrdId": "quantagent" + dt.datetime.now(
                    dt.timezone.utc).strftime("%H%M%S")}
        return self._run("demo_market_order", args, "spot_place_order",
                         lambda r: r.market_order(inst_id, side, str(size),
                                                  args["tgtCcy"]))

    def get_positions(self) -> dict:
        args: dict = {}
        return self._run("get_positions", args, "account_get_positions",
                         lambda r: r.positions())

    def get_balance(self) -> dict:
        args: dict = {}
        return self._run("get_balance", args, "account_get_balance",
                         lambda r: r._request("GET", "/account/balance",
                                              auth=True))

    def close(self) -> None:
        if self._mcp:
            self._mcp.stop()


# ---------------------------------------------------------------------------
# CLI / demo akışı
# ---------------------------------------------------------------------------
def demo_run() -> dict:
    """Jüri demosu: fiyat doğrulama + demo emir + pozisyon okuma, hepsi loglu."""
    bridge = AtkBridge()
    summary: dict = {"run_at": _now(), "steps": []}
    try:
        p = bridge.get_price("BTC-USDT")
        summary["steps"].append({
            "step": "get_price", "transport": p["entry"]["transport"],
            "ok": p["entry"]["ok"], "price": p["price"]})
        px = p["price"] or 81500.0
        size_usdt = "10"  # demo alım: 10 USDT'lik BTC
        o = bridge.demo_market_order("BTC-USDT", "buy", size_usdt)
        summary["steps"].append({
            "step": "demo_market_order", "transport": o["transport"],
            "ok": o["ok"], "side": "buy", "size_usdt": size_usdt,
            "ref_price": px, "note": "anahtarsız sandbox'ta ATK "
                "AuthenticationError döner — imza ve beklenen yanıt logda"})
        pos = bridge.get_positions()
        summary["steps"].append({
            "step": "get_positions", "transport": pos["transport"],
            "ok": pos["ok"]})
        bal = bridge.get_balance()
        summary["steps"].append({
            "step": "get_balance", "transport": bal["transport"],
            "ok": bal["ok"]})
    finally:
        bridge.close()
    summary["log_file"] = str(DEMO_LOG)
    summary["transports_used"] = sorted({e["transport"] for e in bridge.entries})
    return summary


if __name__ == "__main__":
    if "--demo-run" in sys.argv:
        print(json.dumps(demo_run(), ensure_ascii=False, indent=2))
    elif "--price" in sys.argv:
        inst = sys.argv[sys.argv.index("--price") + 1]
        b = AtkBridge()
        try:
            print(json.dumps(b.get_price(inst), ensure_ascii=False, indent=2))
        finally:
            b.close()
    else:
        print(__doc__)
