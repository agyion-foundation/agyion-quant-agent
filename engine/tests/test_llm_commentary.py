"""llm_commentary birim testleri — mock snapshot + sahte _call_api ile
gerçek ağa çıkmadan doğrulama kapısı ve fallback yolu test edilir.
Çalıştırma:  python3 tests/test_llm_commentary.py   (repo kökünden)
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

import llm_commentary as lc          # noqa: E402
import newsletter as nl              # noqa: E402

MOCK_SNAP = {
    "version": "0.1",
    "generated_at": "2026-10-17T14:55:27+00:00",
    "long_term_index": 0.0767,
    "short_term_index": 0.0032,
    "regime_index": -0.6035,
    "composite": 0.0399,
    "signal": "NÖTR",
    "signal_prev": "NÖTR",
    "components": [
        {"name": "btc_mvrv", "layer": "long_term", "raw": 1.82,
         "normalized": 0.21, "weight": 0.10, "source": "BGeometrics",
         "note": "MVRV at 1.82, neutral zone", "license": "clean"},
        {"name": "avax_funding", "layer": "short_term", "raw": -0.0004,
         "normalized": -0.12, "weight": 0.15, "source": "OKX",
         "note": "funding slightly negative", "license": "clean"},
        {"name": "fred_vix_momentum", "layer": "regime", "raw": -2.1,
         "normalized": -0.55, "weight": 0.34, "source": "FRED",
         "note": "VIX easing over 30d", "license": "clean"},
        # cq bileşeni public payload'a GİRMEMELİ:
        {"name": "btc_price_vs_200dma", "layer": "long_term", "raw": 0.126,
         "normalized": -0.094, "weight": 0.149, "source": "CryptoQuant price-ohlcv",
         "note": "BTC +12.6% above 200-day average", "license": "cq"},
    ],
    "errors": ["okx funding-history başarısız"],
}

GOOD_TEXT = (
    "The long-term gauge reads 0.08 today and the short-term gauge sits at 0.0, "
    "so the composite score of 0.04 leaves the signal at NEUTRAL. The macro "
    "regime gauge is much lower at -0.6, which keeps the overall picture "
    "defensive even though the trading gauges are near flat. "
    "BTC MVRV at 1.82 stays in the neutral zone and added a small positive push, "
    "while AVAX funding — the fee leveraged traders pay each other — was slightly "
    "negative and pulled the short-term reading down a little. In the macro "
    "layer, easing VIX momentum over 30 days was the main driver of the weak "
    "regime score. Because the composite remains well below the +0.5 BUY "
    "threshold and above the -0.5 SELL threshold, the model stays on the "
    "sidelines and simply reports what the data shows today, nothing more."
)


def fake_call_ok(key, system, user):
    return GOOD_TEXT, {"input_tokens": 1200, "output_tokens": 210}


def fake_call_bad(key, system, user):
    # Uydurma sayı + yasaklı kelime içeren bozuk çıktı (her denemede aynı).
    return ("BTC will rally — guaranteed gains ahead! Dominance hit 58.4% "
            "and you should buy now."), {"input_tokens": 1200, "output_tokens": 60}


class TestValidationGate(unittest.TestCase):
    def setUp(self):
        self.payload = lc.build_public_payload(MOCK_SNAP)

    def test_payload_excludes_cq(self):
        names = [c["name"] for c in self.payload["components"]]
        self.assertEqual(len(self.payload["components"]), 3)
        self.assertNotIn("btc_price_vs_200dma", names)  # lisans kuralı
        self.assertEqual(self.payload["signal"], "NEUTRAL")

    def test_good_output_passes(self):
        ok, problems = lc.validate_output(GOOD_TEXT, self.payload)
        self.assertTrue(ok, problems)

    def test_invented_number_fails(self):
        ok, problems = lc.validate_output(
            GOOD_TEXT + " BTC dominance is 58.4%.", self.payload)
        self.assertFalse(ok)
        self.assertTrue(any("58.4" in p for p in problems))

    def test_banned_phrase_fails(self):
        ok, _ = lc.validate_output(GOOD_TEXT + " Guaranteed profit.", self.payload)
        self.assertFalse(ok)
        ok, _ = lc.validate_output(GOOD_TEXT + " BTC will rally.", self.payload)
        self.assertFalse(ok)
        ok, _ = lc.validate_output(GOOD_TEXT + " You should buy now.", self.payload)
        self.assertFalse(ok)

    def test_rounding_tolerance(self):
        # 1.82 -> 1.8 yuvarlaması tolerans içinde kalmalı
        ok, problems = lc.validate_output(
            "MVRV reads 1.8 today and the composite is 0.04 with a neutral tone. " * 10,
            self.payload)
        self.assertTrue(ok, problems)

    def test_threshold_constants_allowed(self):
        ok, problems = lc.validate_output(
            GOOD_TEXT + " BUY starts above +0.5 and SELL below -0.5, with a ±0.4 band.",
            self.payload)
        self.assertTrue(ok, problems)


class TestGenerateCommentary(unittest.TestCase):
    def test_keyless_path_explicit(self):
        # key='' -> graceful skip, API çağrısı yapılmaz
        self.assertIsNone(lc.generate_commentary(MOCK_SNAP, key=""))

    def test_valid_output_returned(self):
        orig = lc._call_api
        lc._call_api = fake_call_ok
        try:
            text = lc.generate_commentary(MOCK_SNAP, key="test-key")
        finally:
            lc._call_api = orig
        self.assertEqual(text, GOOD_TEXT)

    def test_bad_output_never_publishes(self):
        calls = {"n": 0}

        def counting_bad(key, system, user):
            calls["n"] += 1
            return fake_call_bad(key, system, user)
        orig = lc._call_api
        lc._call_api = counting_bad
        try:
            text = lc.generate_commentary(MOCK_SNAP, key="test-key")
        finally:
            lc._call_api = orig
        self.assertIsNone(text)                 # doğrulanamayan metin yayınlanmaz
        self.assertEqual(calls["n"], 2)         # tam 1 onarım denemesi

    def test_repair_loop_succeeds_on_second_try(self):
        calls = {"n": 0}

        def flaky(key, system, user):
            calls["n"] += 1
            return (fake_call_bad if calls["n"] == 1 else fake_call_ok)(key, system, user)
        orig = lc._call_api
        lc._call_api = flaky
        try:
            text = lc.generate_commentary(MOCK_SNAP, key="test-key")
        finally:
            lc._call_api = orig
        self.assertEqual(text, GOOD_TEXT)
        self.assertEqual(calls["n"], 2)

    def test_api_error_graceful_skip(self):
        def boom(key, system, user):
            raise ConnectionError("api.anthropic.com unreachable")
        orig = lc._call_api
        lc._call_api = boom
        try:
            self.assertIsNone(lc.generate_commentary(MOCK_SNAP, key="test-key"))
        finally:
            lc._call_api = orig


class TestNewsletterIntegration(unittest.TestCase):
    def test_fallback_template_preserved(self):
        lines = nl.build_newsletter(MOCK_SNAP, "2026-10-17", public=True, llm_text=None)
        body = "\n".join(lines)
        self.assertIn("## Today's takeaway", body)
        self.assertIn("biggest market drivers", body)  # deterministik şablon
        self.assertNotIn("_AI-generated summary", body)

    def test_llm_text_used_when_present(self):
        lines = nl.build_newsletter(MOCK_SNAP, "2026-10-17", public=True,
                                    llm_text=GOOD_TEXT)
        body = "\n".join(lines)
        self.assertIn(GOOD_TEXT, body)
        self.assertIn("_AI-generated summary verified", body)
        self.assertNotIn("biggest market drivers", body)

    def test_disclaimer_public_only(self):
        pub = "\n".join(nl.build_newsletter(MOCK_SNAP, "2026-10-17", public=True))
        internal = "\n".join(nl.build_newsletter(MOCK_SNAP, "2026-10-17", public=False))
        self.assertIn("informational and educational purposes only", pub)
        self.assertIn("not indicative of future results", pub)
        self.assertNotIn("informational and educational purposes only", internal)

    def test_cq_hidden_in_public_llm_text(self):
        # public bültende cq ham değeri/kaynak adı sızmasın (F1)
        pub = "\n".join(nl.build_newsletter(MOCK_SNAP, "2026-10-17", public=True,
                                            llm_text=GOOD_TEXT))
        self.assertNotIn("CryptoQuant", pub)
        self.assertNotIn("12.6", pub)


if __name__ == "__main__":
    unittest.main(verbosity=2)
