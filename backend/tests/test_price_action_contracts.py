from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import app as app_module
from backend.services.ai_analysis import _build_payload, _build_prompt
from backend.services.price_action import analyze_price_action
from backend.tests.test_discipline_strategy import confirmed_buy_bars, market_bull_bars


class PriceActionContractTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_daily_stock_route_returns_price_action_without_chan_fields(self):
        with patch.object(app_module, "get_stock_history", return_value=confirmed_buy_bars()), patch.object(
            app_module, "get_stock_fundamentals_quick", return_value={}
        ):
            response = self.client.get("/api/analyze?symbol=000001")

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertIn("priceAction", result)
        self.assertIn("environment", result["priceAction"])
        self.assertIn("signals", result["priceAction"])
        for field in ("mergedKlines", "fractals", "strokes", "centers", "futureSignals"):
            self.assertNotIn(field, result)

    def test_index_route_marks_assessment_as_exposure_guidance(self):
        with patch.object(app_module, "get_index_history", return_value=market_bull_bars()):
            response = self.client.get("/api/index/analyze?symbol=000001&name=%E4%B8%8A%E8%AF%81%E6%8C%87%E6%95%B0")

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["priceAction"]["assessment"]["scope"], "INDEX_EXPOSURE")

    def test_intraday_route_uses_the_same_price_action_contract(self):
        with patch.object(app_module, "get_stock_intraday_history", return_value=confirmed_buy_bars()):
            response = self.client.get("/api/intraday/analyze?symbol=000001&period=15")

        self.assertEqual(response.status_code, 200)
        period = response.get_json()["periods"]["15"]
        self.assertEqual(period["priceAction"]["timeframe"], "15m")
        self.assertNotIn("signals", period)
        self.assertIn("actionableSetupCount", response.get_json()["summary"])

    def test_intraday_route_reports_a_readable_missing_symbol_error(self):
        response = self.client.get("/api/intraday/analyze?period=15")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "缺少股票代码")

    def test_screener_match_requires_complete_confirmed_buy_signal(self):
        result = analyze_price_action(confirmed_buy_bars())
        match = app_module._match_price_action_buy(result, 0.2, 10.74)

        self.assertIsNotNone(match)
        self.assertEqual(match["direction"], "BUY")
        self.assertGreater(match["firstTarget"], match["entryLimit"])
        self.assertGreaterEqual(match["riskReward"], 1.5)

    def test_ai_snapshot_contains_price_action_and_excludes_chan_shape(self):
        result = analyze_price_action(confirmed_buy_bars())
        result.update({"symbol": "000001", "name": "测试股", "dateRange": {"start": "2026-01-01", "end": "2026-02-09"}})

        payload = _build_payload(result, None, "stock")

        self.assertIn("priceAction", payload)
        self.assertIn("environment", payload["priceAction"])
        for field in ("recentMergedKlines", "recentStrokes", "recentCenters", "futureSignals"):
            self.assertNotIn(field, payload)

    def test_ai_prompt_requests_conditional_levels_not_anchors(self):
        result = analyze_price_action(confirmed_buy_bars())
        result.update({"symbol": "000001", "name": "测试股", "dateRange": {"start": "2026-01-01", "end": "2026-02-09"}})

        prompt = _build_prompt(_build_payload(result, None, "stock"))

        self.assertIn('"levels"', prompt)
        self.assertNotIn('"anchors"', prompt)


if __name__ == "__main__":
    unittest.main()
