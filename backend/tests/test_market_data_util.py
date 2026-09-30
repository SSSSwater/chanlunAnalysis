from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.services import market_data_util


def _bars(count: int = 2) -> list[dict]:
    return [
        {
            "date": f"2026-08-{25 + index:02d}",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
        }
        for index in range(count)
    ]


class MarketDataUtilTests(unittest.TestCase):
    def test_daily_kline_prefers_tushare(self):
        bars = _bars()
        with patch.object(market_data_util, "tushare_is_configured", return_value=True), patch.object(
            market_data_util, "get_tushare_kline", return_value=bars
        ) as tushare, patch.object(market_data_util, "_tencent_kline") as tencent, patch.object(
            market_data_util, "_eastmoney_kline"
        ) as eastmoney:
            result = market_data_util.get_daily_kline(
                "000001",
                "20260825",
                "20260826",
                limit=2,
            )

        self.assertEqual(result.source, "tushare")
        self.assertEqual(result.selected_items, bars)
        tushare.assert_called_once()
        tencent.assert_not_called()
        eastmoney.assert_not_called()

    def test_daily_kline_falls_back_after_tushare_failure(self):
        bars = _bars()
        with patch.object(market_data_util, "tushare_is_configured", return_value=True), patch.object(
            market_data_util, "get_tushare_kline", side_effect=RuntimeError("Tushare不可用")
        ), patch.object(market_data_util, "_tencent_kline", side_effect=RuntimeError("腾讯不可用")), patch.object(
            market_data_util, "_eastmoney_kline", return_value=bars
        ):
            result = market_data_util.get_daily_kline(
                "000001",
                "20260825",
                "20260826",
                limit=2,
            )

        self.assertEqual(result.source, "eastmoney")
        self.assertEqual(result.selected_items, bars)
        self.assertEqual(len(result.errors), 2)
        self.assertIn("Tushare", result.errors[0])

    def test_analysis_daily_kline_rejects_provider_without_historical_turnover(self):
        price_only = _bars()
        complete = [{**bar, "turnoverRate": 1.2} for bar in price_only]
        with patch.object(market_data_util, "tushare_is_configured", return_value=True), patch.object(
            market_data_util, "get_tushare_kline", return_value=price_only
        ), patch.object(market_data_util, "_tencent_kline", return_value=price_only), patch.object(
            market_data_util, "_eastmoney_kline", return_value=complete
        ) as eastmoney:
            result = market_data_util.get_daily_kline(
                "000001",
                "20260825",
                "20260826",
                limit=2,
                require_turnover=True,
            )

        self.assertEqual(result.source, "eastmoney")
        self.assertEqual(result.selected_items, complete)
        eastmoney.assert_called_once()
        self.assertIn("Tushare", result.errors[0])
        self.assertIn("历史换手率", result.errors[0])

    def test_market_quotes_reject_incomplete_provider_before_fallback(self):
        partial = [{"symbol": "000001", "latestPrice": 10.0}]
        complete = [
            {"symbol": "000001", "latestPrice": 10.0},
            {"symbol": "000002", "latestPrice": 11.0},
        ]
        with patch.object(market_data_util, "tushare_is_configured", return_value=True), patch.object(
            market_data_util, "list_tushare_daily_quotes", return_value=partial
        ), patch.object(market_data_util, "_eastmoney_stocks", return_value=complete) as eastmoney:
            result = market_data_util.list_market_quotes(minimum_count=2)

        self.assertEqual(result.source, "eastmoney")
        self.assertEqual(result.items, complete)
        eastmoney.assert_called_once()
        self.assertIn("数据量不足", result.errors[0])

    def test_market_quotes_keep_beijing_board_for_market_statistics(self):
        tushare_quotes = [
            {"symbol": "000001", "latestPrice": 10.0},
            {"symbol": "920001", "latestPrice": 11.0},
            {"symbol": "000002", "latestPrice": 12.0},
        ]
        with patch.object(market_data_util, "tushare_is_configured", return_value=True), patch.object(
            market_data_util, "list_tushare_daily_quotes", return_value=tushare_quotes
        ):
            result = market_data_util.list_market_quotes(minimum_count=3)

        self.assertEqual(result.source, "tushare")
        self.assertEqual([item["symbol"] for item in result.items], ["000001", "920001", "000002"])

    def test_searchable_catalog_skips_tushare_and_prefers_eastmoney(self):
        catalog = [{"symbol": "000001", "name": "平安银行"}]
        with patch.object(
            market_data_util, "list_tushare_securities", side_effect=AssertionError("不应调用 Tushare")
        ), patch.object(market_data_util, "_eastmoney_stocks", return_value=catalog) as eastmoney:
            result = market_data_util.list_non_tushare_securities()

        self.assertEqual(result.source, "eastmoney")
        self.assertEqual(result.items, catalog)
        eastmoney.assert_called_once()

    def test_home_market_quotes_skip_tushare_and_fall_back_to_tencent(self):
        partial = [{"symbol": "000001", "latestPrice": 10.0}]
        complete = [
            {"symbol": "000001", "latestPrice": 10.0},
            {"symbol": "000002", "latestPrice": 11.0},
        ]
        with patch.object(
            market_data_util, "list_tushare_daily_quotes", side_effect=AssertionError("不应调用 Tushare")
        ), patch.object(market_data_util, "_eastmoney_stocks", return_value=partial), patch.object(
            market_data_util, "_tencent_stocks", return_value=complete
        ) as tencent:
            result = market_data_util.list_non_tushare_market_quotes(
                minimum_count=2,
                tencent_securities=[{"symbol": "000001", "name": "平安银行"}],
            )

        self.assertEqual(result.source, "tencent")
        self.assertEqual(result.items, complete)
        tencent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
