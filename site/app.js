/* ============================================================
   AGYION QUANT — app.js
   Gauge physics, ticker tape, family cards, decision log,
   subscribe form (Supabase REST, anon insert-only).
   All data comes from data.js (embedded) — works over file://.
   ============================================================ */
(function () {
  "use strict";
  var D = window.AGYION_DATA;

  var COLORS = { sell: "#b5453f", neut: "#8a8a8a", buy: "#4a7c59", amber: "#c9a86a", faint: "#565b58", text: "#d8dade" };

  function $(id) { return document.getElementById(id); }
  function fmt(v, d) { return (v >= 0 ? "+" : "") + Number(v).toFixed(d == null ? 4 : d); }
  function bandOf(v) { return v < -0.5 ? "SELL" : v > 0.5 ? "BUY" : "NEUTRAL"; }
  function clsOf(v) { return v < -0.5 ? "sell" : v > 0.5 ? "buy" : "neutral"; }

  /* ---------------- masthead: clock + snapshot ts ---------------- */
  function tickClock() {
    var el = $("clock");
    if (el) el.textContent = new Date().toUTCString().slice(17, 25) + " UTC";
  }
  tickClock(); setInterval(tickClock, 1000);
  $("snapTs").textContent = D.generated_at.replace("T", " ").replace("+00:00", "Z");
  $("signalRule").textContent = "rule: " + D.signal_rule;

  /* ---------------- ticker tape ---------------- */
  function buildTicker(el) {
    if (!el) return;
    var items = [];
    function push(name, val, dir) {
      var cls = dir > 0 ? "up" : dir < 0 ? "down" : "";
      var arrow = dir > 0 ? "▲" : dir < 0 ? "▼" : "■";
      items.push('<span class="tk"><b>' + name + '</b> <span class="' + cls + '">' + val + " " + arrow + "</span></span>");
    }
    push("COMPOSITE", fmt(D.composite), 0);
    push("BTC-LT", fmt(D.long_term_index), D.long_term_index);
    push("BTC-ST", fmt(D.short_term_index), D.short_term_index);
    push("REGIME", fmt(D.regime_index), D.regime_index);
    push("BTC/USD", D.btc_price.toLocaleString("en-US"), 1);
    push("AVAX TVL 30D", "+23.6%", 1);
    push("MVRV-Z", "2.19", -1);
    push("F&G", "55 GREED", 1);
    push("VIX 30D", "+2.1", -1);
    push("US10Y 30D", "+0.31", -1);
    push("KIMCHI", "+0.97%", 1);
    push("CB PREMIUM", "-0.15%", -1);
    push("SIGNAL", D.signal, 0);
    var html = items.join('<span class="sep">///</span>');
    el.innerHTML = html + '<span class="sep">///</span>' + html + '<span class="sep">///</span>';
  }
  buildTicker($("ticker"));
  buildTicker($("ticker2"));

  /* ---------------- P100 gauge ---------------- */
  // Semicircular gauge: value -1 (left) .. +1 (right), 0 = top
  var CX = 200, CY = 210, R = 158, R_BAND = 150;

  function polar(v, r) {
    var a = (v / 1) * Math.PI / 2; // -PI/2 .. +PI/2, 0 = top
    return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
  }
  function arcPath(v0, v1, r) {
    var p0 = polar(v0, r), p1 = polar(v1, r);
    var large = Math.abs(v1 - v0) > 1 ? 1 : 0;
    return "M " + p0[0].toFixed(1) + " " + p0[1].toFixed(1) +
           " A " + r + " " + r + " 0 " + large + " 1 " +
           p1[0].toFixed(1) + " " + p1[1].toFixed(1);
  }
  function svgEl(tag, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  var bands = $("bands");
  var bandDefs = [
    { v0: -1,   v1: -0.5, c: COLORS.sell },
    { v0: -0.5, v1: 0.5,  c: COLORS.neut },
    { v0: 0.5,  v1: 1,    c: COLORS.buy }
  ];
  bandDefs.forEach(function (b) {
    bands.appendChild(svgEl("path", {
      d: arcPath(b.v0 + 0.015, b.v1 - 0.015, R_BAND),
      fill: "none", stroke: b.c, "stroke-width": 16, opacity: 0.55
    }));
  });
  bands.appendChild(svgEl("path", {
    d: arcPath(-1, 1, R + 14), fill: "none", stroke: "#272b29", "stroke-width": 1.5
  }));

  var ticks = $("ticks");
  for (var t = -1; t <= 1.001; t += 0.25) {
    var tv = Math.round(t * 100) / 100;
    var major = Math.abs(((tv * 2) % 1)) < 0.01;
    var p0 = polar(tv, R + 20), p1 = polar(tv, R + (major ? 32 : 27));
    ticks.appendChild(svgEl("line", {
      x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1],
      stroke: COLORS.faint, "stroke-width": 1.4
    }));
    if (major) {
      var lp = polar(tv, R + 44);
      var lab = svgEl("text", { x: lp[0], y: lp[1] + 3, "text-anchor": "middle", "class": "tick-lab" });
      lab.textContent = (tv > 0 ? "+" : "") + tv.toFixed(1);
      ticks.appendChild(lab);
    }
  }

  // Needle physics: spring toward live value + tiny live jitter.
  var needle = $("needle");
  var gaugeVal = $("gaugeVal");
  var target = D.composite, angle = -1, vel = 0;
  var jitter = 0, jitterT = 0;

  function renderNeedle() {
    var eff = angle + jitter;
    var p = polar(eff, R - 14);
    needle.setAttribute("x2", p[0].toFixed(2));
    needle.setAttribute("y2", p[1].toFixed(2));
    gaugeVal.textContent = fmt(eff);
  }
  function step() {
    var k = 0.045, damp = 0.86;
    vel += (target - angle) * k;
    vel *= damp;
    angle += vel;
    jitterT += 0.05;
    jitter = Math.sin(jitterT * 1.3) * 0.004 + Math.sin(jitterT * 0.31) * 0.003;
    renderNeedle();
    requestAnimationFrame(step);
  }
  renderNeedle();
  requestAnimationFrame(step);

  $("heroValue").textContent = fmt(D.composite);
  var hs = $("heroSignal");
  hs.textContent = D.signal;
  hs.className = "ro-signal " + clsOf(D.composite);

  /* ---------------- P200 family cards ---------------- */
  function asciiBar(v, width) {
    var n = Math.round(Math.abs(v) * width);
    return "▰".repeat(n) + "▱".repeat(width - n);
  }
  var famData = [
    { key: "long_term", title: "BTC · LONG-TERM INDEX", note: "On-chain valuation & cycle structure. 8 components: MVRV-Z, NUPL, realized-price distance, 200DMA, AVAX TVL…" },
    { key: "short_term", title: "BTC · SHORT-TERM INDEX", note: "Derivatives positioning & sentiment. 15 components: funding, OI, liquidation skew, premia, F&G…" },
    { key: "regime", title: "MACRO REGIME INDEX", note: "TradFi weather. 3 components: broad dollar, VIX, 10Y yield momentum." }
  ];
  var famWrap = $("familyCards");
  famData.forEach(function (f) {
    var L = D.layers[f.key];
    var band = f.key === "regime" ? (L.index < -0.5 ? "RISK-OFF" : L.index > 0.5 ? "RISK-ON" : "MIXED") : bandOf(L.index);
    var cls = f.key === "regime" ? (L.index < -0.5 ? "risk-off" : L.index > 0.5 ? "buy" : "neut") : clsOf(L.index);
    var comps = L.components.slice().sort(function (a, b) { return Math.abs(b.impact) - Math.abs(a.impact); });
    var top = comps.slice(0, 3).map(function (c) {
      var ic = c.impact >= 0 ? "imp-pos" : "imp-neg";
      return '<div>· ' + c.name + ' <span class="' + ic + '">' + fmt(c.impact, 3) + "</span></div>";
    }).join("");
    var barColor = (cls === "risk-off" || cls === "sell") ? "sell" : (cls === "buy" ? "buy" : "neut");
    var card = document.createElement("div");
    card.className = "fam-card";
    card.innerHTML =
      '<div class="fam-name">' + f.title + "</div>" +
      '<div class="fam-val">' + fmt(L.index) + "</div>" +
      '<div class="fam-band ' + cls + '">' + band + "</div>" +
      '<div class="fam-ascii" style="color:var(--' + barColor + ')">' + asciiBar(L.index, 10) + "</div>" +
      '<div class="fam-note">' + f.note + "</div>" +
      '<div class="fam-top">' + top + "</div>";
    famWrap.appendChild(card);
  });

  /* ---------------- P200 drill-down tables ---------------- */
  var drill = $("drillBody");
  ["long_term", "short_term", "regime"].forEach(function (key) {
    var L = D.layers[key];
    var cap = key === "long_term" ? "LONG TERM — INDEX " + fmt(L.index)
            : key === "short_term" ? "SHORT TERM — INDEX " + fmt(L.index)
            : "MACRO REGIME — INDEX " + fmt(L.index);
    var rows = L.components.map(function (c) {
      var sc = c.score >= 0 ? "pos" : "neg";
      var ic = c.impact >= 0 ? "pos" : "neg";
      return "<tr><td>" + c.name + '</td><td class="num">' + c.raw +
             '</td><td class="num ' + sc + '">' + fmt(c.score, 2) +
             '</td><td class="num">' + c.weight.toFixed(2) +
             '</td><td class="num ' + ic + '">' + fmt(c.impact, 3) +
             '</td><td class="note">' + c.note + "</td></tr>";
    }).join("");
    var tbl = document.createElement("table");
    tbl.innerHTML = "<caption>" + cap + "</caption>" +
      "<thead><tr><th>COMPONENT</th><th>RAW</th><th>SCORE</th><th>W</th><th>IMPACT</th><th>NOTE</th></tr></thead>" +
      "<tbody>" + rows + "</tbody>";
    drill.appendChild(tbl);
  });

  /* ---------------- P300 decision log ---------------- */
  var log = $("decisionLog");
  D.decisions.forEach(function (r) {
    var div = document.createElement("div");
    div.className = "dl-line";
    div.innerHTML = '<span class="dl-ts">[' + r.ts + ']</span> ' +
                    '<span class="dl-src">' + r.source.toUpperCase().padEnd(6) + "</span> " +
                    '<span class="dl-txt">' + r.line + "</span>";
    log.appendChild(div);
  });
  var cur = document.createElement("div");
  cur.className = "dl-line cursor";
  cur.innerHTML = '<span class="dl-ts">[–––––––––––––––––––]</span> <span class="dl-src">DAEMON</span> <span class="dl-txt dim">next scheduled run pending… </span>';
  log.appendChild(cur);

  /* ---------------- P400 vault ---------------- */
  $("vaultName").textContent = D.vault.contract_name;
  $("vaultNet").textContent = D.vault.network;
  if (D.vault.address) {
    var short = D.vault.address.slice(0, 6) + "…" + D.vault.address.slice(-4);
    $("vaultAddr").innerHTML = '<a class="evi-link" href="' + D.vault.explorer_url + '">' + short + " ▸</a>";
    $("snowtraceNote").innerHTML = 'contract: <a class="evi-link" href="' + D.vault.explorer_url + '">snowtrace ▸</a>';
    $("e2eNote").innerHTML = 'e2e trade: <a class="evi-link" href="' + D.vault.e2e_trade_tx + '">executeTrade tx ▸</a>';
  } else {
    $("vaultAddr").innerHTML = '<span class="deploying">deploying…</span>';
  }
  $("vaultTimelock").textContent = D.vault.timelock;
  $("vaultDeposit").textContent = D.vault.deposit;
  $("testBadge").querySelector(".vs-big").textContent = D.vault.tests_passed + "/" + D.vault.tests_total;

  /* ---------------- P500 ATK ---------------- */
  $("atkTools").textContent = D.atk.tools_visible;

  /* ---------------- P600 newsletter ---------------- */
  $("newsTitle").textContent = "▚ " + D.newsletter.title + " | Signal: " + D.newsletter.signal;
  $("newsBody").textContent = D.newsletter.lines.join("\n");

  /* ---------------- subscribe form (Supabase REST, anon insert-only) ---------------- */
  var SUB_URL = "https://qpsthoardxlwvqbippoo.supabase.co/rest/v1/subscribers";
  var SUB_KEY = "sb_publishable_YboIulLJgcSAjuPRInotmQ_XDZNMgyp";
  var subForm = $("subForm");
  if (subForm) {
    subForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var input = $("subEmail"), btn = $("subBtn"), msg = $("subMsg");
      var email = (input.value || "").trim();
      msg.className = "sub-msg mono";
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        msg.classList.add("err");
        msg.textContent = "✗ invalid address — check the format.";
        return;
      }
      btn.disabled = true;
      msg.textContent = "transmitting…";
      fetch(SUB_URL, {
        method: "POST",
        headers: {
          "apikey": SUB_KEY,
          "Authorization": "Bearer " + SUB_KEY,
          "Content-Type": "application/json",
          "Prefer": "return=minimal"
        },
        body: JSON.stringify({ email: email })
      }).then(function (res) {
        btn.disabled = false;
        if (res.status === 201 || res.ok) {
          msg.classList.add("ok");
          msg.textContent = "✔ You're in — next issue lands 23:15 TRT";
          input.value = "";
        } else if (res.status === 409) {
          msg.classList.add("dup");
          msg.textContent = "■ Already subscribed — see you at 23:15 TRT.";
        } else {
          throw new Error("HTTP " + res.status);
        }
      }).catch(function () {
        btn.disabled = false;
        msg.classList.add("err");
        msg.innerHTML = "✗ line down — <a href=\"mailto:brief@agyionlabs.dev?subject=Daily%20Brief%20Subscription&body=subscribe%20me\">subscribe via email instead ▸</a>";
      });
    });
  }

})();
