from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import database as db, market_data_util
from backend.services import stock_data
from backend.services.price_action import MIN_ANALYSIS_BARS


def _daily_bars(count: int) -> list[dict]:
    return [
        {
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
        }
        for index in range(count)
    ]


class CachedDisciplineDataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "cache.db")
        db.init_db()

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_market_overview_reads_local_quotes_without_refreshing_full_market(self):
        db.upsert_daily_quotes(
            [{"symbol": "000001", "name": "测试", "latestPrice": 10, "pctChange": 1.2, "amount": 100}],
            "2026-08-20 closed",
        )
        with patch("backend.services.stock_data.ensure_daily_quotes") as refresh, patch(
            "backend.services.stock_data.get_home_index_quotes", return_value=[]
        ):
            result = stock_data.get_market_today()

        refresh.assert_not_called()
        self.assertEqual(result["source"], "cache")
        self.assertTrue(result["stale"])
        self.assertEqual(result["count"], 1)

    def test_cached_history_uses_latest_continuous_segment(self):
        bars = []
        for offset in range(70):
            day = 1 + offset
            month = 1 + (day - 1) // 28
            date = f"2026-{month:02d}-{((day - 1) % 28) + 1:02d}"
            bars.append({"date": date, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100})
        db.upsert_klines("000001", "101", "1", bars)
        db.upsert_klines(
            "000001",
            "101",
            "1",
            [{"date": "2024-01-02", "open": 5, "high": 6, "low": 4, "close": 5, "volume": 100}],
        )

        history = stock_data.get_cached_stock_history("000001", "20250101")

        self.assertEqual(len(history), 70)
        self.assertEqual(history[0]["date"], "2026-01-01")
        self.assertEqual(history[-1]["date"], "2026-03-14")

    def test_short_fresh_history_is_refreshed_when_minimum_bars_are_requested(self):
        cached = _daily_bars(MIN_ANALYSIS_BARS - 1)
        refreshed = _daily_bars(MIN_ANALYSIS_BARS + 5)
        provider_result = market_data_util.MarketDataResult(
            items=refreshed,
            source="eastmoney",
            filtered_items=refreshed,
        )

        with patch.object(stock_data.db, "query_klines", return_value=cached), patch.object(
            stock_data.market_data, "get_daily_kline", return_value=provider_result
        ) as get_daily_kline, patch.object(stock_data.db, "upsert_klines"):
            history = stock_data.get_stock_history(
                "000001",
                "20260101",
                "20260821",
                minimum_bars=MIN_ANALYSIS_BARS,
            )

        get_daily_kline.assert_called_once()
        self.assertEqual(
            history,
            [{**bar, "source": "eastmoney", "provider": "eastmoney"} for bar in refreshed],
        )

    def test_legacy_tushare_history_is_refreshed_when_turnover_is_missing(self):
        cached = [{**bar, "source": "tushare", "turnoverRate": None} for bar in _daily_bars(MIN_ANALYSIS_BARS)]
        refreshed = [{**bar, "source": "tushare", "turnoverRate": 1.2} for bar in _daily_bars(MIN_ANALYSIS_BARS)]
        provider_result = market_data_util.MarketDataResult(
            items=refreshed,
            source="tushare",
            filtered_items=refreshed,
        )

        with patch.object(stock_data.db, "query_klines", return_value=cached), patch.object(
            stock_data.market_data, "get_daily_kline", return_value=provider_result
        ) as get_daily_kline, patch.object(stock_data.db, "upsert_klines"):
            history = stock_data.get_stock_history("000001", "20260101", "20260821")

        get_daily_kline.assert_called_once()
        self.assertEqual(
            history,
            [{**bar, "provider": "tushare"} for bar in refreshed],
        )

    def test_discipline_plan_keeps_sufficient_cache_local(self):
        history = _daily_bars(MIN_ANALYSIS_BARS)
        with patch.object(app_module, "get_cached_stock_history", return_value=history), patch.object(
            app_module, "get_stock_history"
        ) as refresh_history, patch.object(app_module, "evaluate_discipline_strategy", return_value={"action": "WAIT"}):
            plan = app_module._build_discipline_plan(
                "000001",
                100_000,
                start_date="20260101",
                end_date="20260821",
                cache_only=True,
            )

        refresh_history.assert_not_called()
        self.assertEqual(plan["symbol"], "000001")

    def test_discipline_plan_refreshes_an_insufficient_cache(self):
        cached = _daily_bars(MIN_ANALYSIS_BARS - 1)
        refreshed = _daily_bars(MIN_ANALYSIS_BARS + 5)
        with patch.object(app_module, "get_cached_stock_history", return_value=cached), patch.object(
            app_module, "get_stock_history", return_value=refreshed
        ) as refresh_history, patch.object(app_module, "evaluate_discipline_strategy", return_value={"action": "WAIT"}):
            plan = app_module._build_discipline_plan(
                "000001",
                100_000,
                start_date="20260101",
                end_date="20260821",
                cache_only=True,
            )

        refresh_history.assert_called_once_with(
            "000001",
            "20260101",
            "20260821",
            minimum_bars=MIN_ANALYSIS_BARS,
        )
        self.assertEqual(plan["symbol"], "000001")


if __name__ == "__main__":
    unittest.main()
