#!/usr/bin/env python3
"""atk_bridge.py — OKX Agent Trade Kit (okx-trade-mcp) köprüsü.

Gerçek MCP stdio transport'u üzerinden JSON-RPC konuşur:
  npx -y @okx_ai/okx-trade-mcp  -> subprocess; initialize / tools/list / tools/call

Fallback zinciri (her yanıt kullanılan transport ile etiketlenir):
  1) MCP stdio (birincil)  -> transport="mcp"
  2) OKX V5 REST (HMAC imzalı, demo bayrağı x-simulated-trading:1) -> "rest-sim"
  3) Mock (yalnızca offline geliştirme) -> "mock", simulated=true

Güvenlik: API key'leri yalnızca env'den okunur (OKX_API_KEY/SECRET/PASSPHRASE);
hiçbiri koda gömülmez. Key yoksa auth'lu çağrılar AUTH_MISSING ile kapanır
(fail-closed) — uydurma veri üretilmez.
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = Path(__file__).resolve().parent / "logs"
DECISIONS = LOGS / "decisions.jsonl"

MCP_PACKAGE = "@okx_ai/okx-trade-mcp"
REST_BASE = "https://www.okx.com"


# ---------------------------------------------------------------- loglama
def log(rec):
    LOGS.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds")
                   .replace("+00:00", "Z"))
    with DECISIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ----------------------------------------------------------- MCP stdio istemcisi
class McpClient:
    """okx-trade-mcp'yi subprocess olarak spawn eder ve stdio üzerinden
    JSON-RPC 2.0 konuşur. Her satır bir JSON-RPC mesajıdır."""

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._id = 0
        self.proc = None
        self._lock = threading.Lock()

    def start(self):
        if shutil.which("npx") is None:
            raise RuntimeError("npx yok — Node.js gerekli")
        self.proc = subprocess.Popen(
            ["npx", "-y", MCP_PACKAGE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        # initialize el sıkışması
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "agyion-quant-agent", "version": "0.1"}})
        self._notify("notifications/initialized")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _rpc(self, method, params):
        with self._lock:
            self._id += 1
            rid = self._id
            self.proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params}) + "\n")
            self.proc.stdin.flush()
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(f"MCP error: {msg['error']}")
                    return msg.get("result", {})
            raise TimeoutError(f"MCP yanıtı gelmedi: {method}")

    def _notify(self, method):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def list_tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call_tool(self, name, arguments):
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


# ------------------------------------------------------------- REST (fallback)
def _rest_headers(method, path, body, simulated=True):
    """OKX V5 HMAC imzası. Key yoksa None döner (AUTH_MISSING yolu)."""
    key = os.environ.get("OKX_API_KEY")
    secret = os.environ.get("OKX_API_SECRET")
    passphrase = os.environ.get("OKX_API_PASSPHRASE")
    if not (key and secret and passphrase):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    prehash = ts + method + path + (body or "")
    sign = base64.b64encode(hmac.new(
        secret.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
    h = {"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": passphrase, "Content-Type": "application/json"}
    if simulated:
        h["x-simulated-trading"] = "1"
    return h


def rest_get_ticker(inst="BTC-USDT"):
    """Public ticker — imza gerekmez. Sandbox'ta egress kapalıysa hata fırlatır."""
    import urllib.request
    url = f"{REST_BASE}/api/v5/market/ticker?instId={inst}"
    with urllib.request.urlopen(url, timeout=12) as r:
        j = json.loads(r.read())
    return float(j["data"][0]["last"])


# ------------------------------------------------------------------- köprü
class AtkBridge:
    """Sinyal -> ATK aracı çağrısı. Her çağrı hangi transport'tan geldiğini
    açıkça etiketler; mock yanıtlar simulated=true işaretli döner."""

    def __init__(self):
        self.client = None
        self.transport = None

    def connect(self):
        try:
            self.client = McpClient()
            self.client.start()
            tools = self.client.list_tools()
            self.transport = "mcp"
            log({"type": "ATK_CONNECT", "transport": "mcp",
                 "tools_visible": len(tools),
                 "server": "okx-trade-mcp"})
            return tools
        except Exception as e:  # noqa: BLE001 — fallback zinciri kasıtlı
            log({"type": "ATK_CONNECT", "transport": "none",
                 "error": f"{type(e).__name__}: {e}"})
            self.transport = "rest-sim"
            return []

    def get_ticker(self, inst="BTC-USDT"):
        if self.transport == "mcp":
            res = self.client.call_tool("market_get_ticker", {"instId": inst})
            txt = res.get("content", [{}])[0].get("text", "{}")
            price = json.loads(txt).get("data", [{}])[0].get("last")
            log({"type": "ATK_TICKER", "transport": "mcp", "inst": inst,
                 "last": price})
            return {"price": float(price), "transport": "mcp", "simulated": False}
        try:
            price = rest_get_ticker(inst)
            log({"type": "ATK_TICKER", "transport": "rest-sim", "inst": inst,
                 "last": price})
            return {"price": price, "transport": "rest-sim", "simulated": False}
        except Exception as e:  # noqa: BLE001
            log({"type": "ATK_TICKER", "transport": "mock", "inst": inst,
                 "error": f"{type(e).__name__}: {e}", "simulated": True})
            return {"price": None, "transport": "mock", "simulated": True}

    def account_call(self, tool, arguments=None):
        """Auth gerektiren hesap çağrıları — fail-closed: key yoksa AUTH_MISSING."""
        if self.transport == "mcp":
            try:
                res = self.client.call_tool(tool, arguments or {})
                txt = res.get("content", [{}])[0].get("text", "")
                if "missing" in txt.lower() or "auth" in txt.lower():
                    log({"type": "ATK_AUTH_CALL", "tool": tool,
                         "transport": "mcp", "result": "AUTH_MISSING"})
                    return {"status": "AUTH_MISSING", "transport": "mcp",
                            "simulated": False}
                log({"type": "ATK_AUTH_CALL", "tool": tool,
                     "transport": "mcp", "result": "OK"})
                return {"status": "OK", "raw": txt, "transport": "mcp",
                        "simulated": False}
            except Exception as e:  # noqa: BLE001
                log({"type": "ATK_AUTH_CALL", "tool": tool,
                     "transport": "mcp", "result": "ERROR",
                     "error": f"{type(e).__name__}: {e}"})
                return {"status": "ERROR", "transport": "mcp", "simulated": False}
        log({"type": "ATK_AUTH_CALL", "tool": tool, "transport": self.transport,
             "result": "AUTH_MISSING"})
        return {"status": "AUTH_MISSING", "transport": self.transport,
                "simulated": False}

    def close(self):
        if self.client:
            self.client.stop()


def main():
    b = AtkBridge()
    tools = b.connect()
    print(f"transport: {b.transport} | tools: {len(tools)}")
    if tools:
        mods = sorted({t["name"].split("_")[0] for t in tools})
        print(f"modüller: {', '.join(mods)}")
    tick = b.get_ticker("BTC-USDT")
    print(json.dumps(tick, ensure_ascii=False))
    for t in ("account_get_balance", "account_get_positions"):
        r = b.account_call(t)
        print(f"{t}: {r['status']} (transport={r['transport']})")
    b.close()


if __name__ == "__main__":
    sys.exit(main())
