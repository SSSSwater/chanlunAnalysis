from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from backend.services import tushare_client
from backend.services.tushare_client import _quote_frame_to_items, get_tushare_kline, list_tushare_securities


class TushareNameNormalizationTests(unittest.TestCase):
    def test_quote_names_fall_back_to_symbol_for_missing_values(self):
        frame = pd.DataFrame(
            [
                {"ts_code": "920790.BJ", "trade_date": "20260826", "close": 10, "name": float("nan")},
                {"ts_code": "920274.BJ", "trade_date": "20260826", "close": 11, "name": ""},
                {"ts_code": "920808.BJ", "trade_date": "20260826", "close": 12, "name": "nan"},
                {"ts_code": "920161.BJ", "trade_date": "20260826", "close": 13, "name": "正常名称"},
            ]
        )

        items = _quote_frame_to_items(frame, is_index=False)

        self.assertEqual([item["name"] for item in items], ["920790", "920274", "920808", "正常名称"])

    def test_security_catalog_names_fall_back_to_symbol_for_missing_values(self):
        frame = pd.DataFrame(
            [
                {"ts_code": "600001.SH", "symbol": "600001", "name": float("nan")},
                {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
            ]
        )

        with patch("backend.services.tushare_client.get_pro") as get_pro:
            get_pro.return_value.stock_basic.return_value = frame
            items = list_tushare_securities(limit=2)

        self.assertEqual([item["name"] for item in items], ["600001", "平安银行"])

    @unittest.skipUnless(tushare_client.ts is not None and hasattr(tushare_client.ts, "pro_bar"), "tushare is not installed")
    def test_daily_kline_fills_historical_turnover_rates(self):
        bars = pd.DataFrame(
            [
                {"ts_code": "000001.SZ", "trade_date": "20260825", "open": 10, "high": 11, "low": 9, "close": 10.5, "vol": 100, "amount": 1000},
                {"ts_code": "000001.SZ", "trade_date": "20260826", "open": 10.5, "high": 12, "low": 10, "close": 11.5, "vol": 120, "amount": 1200},
            ]
        )
        daily_basic = pd.DataFrame(
            [
                {"trade_date": "20260825", "turnover_rate": 1.25},
                {"trade_date": "20260826", "turnover_rate": 1.5},
            ]
        )

        class FakePro:
            def daily_basic(self, **kwargs):
                return daily_basic

        with patch.object(tushare_client, "get_pro", return_value=FakePro()), patch.object(
            tushare_client.ts, "pro_bar", return_value=bars
        ):
            items = get_tushare_kline("000001", start_date="20260825", end_date="20260826", limit=2)

        self.assertEqual([item["turnoverRate"] for item in items], [1.25, 1.5])


if __name__ == "__main__":
    unittest.main()
