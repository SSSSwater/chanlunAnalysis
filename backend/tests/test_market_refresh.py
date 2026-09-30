from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from backend.services import database as db
from backend.services import stock_data


def quote(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "name": f"测试股{symbol}",
        "market": "0",
        "secid": f"0.{symbol}",
        "secucode": f"{symbol}.SZ",
        "isSt": False,
        "latestPrice": 10.0,
        "pctChange": 1.0,
        "priceChange": 0.1,
        "volume": 1_000.0,
        "amount": 10_000.0,
        "turnoverRate": 1.0,
        "peDynamic": 10.0,
        "pb": 1.0,
        "totalMarketCap": 1_000_000.0,
    }


class MarketRefreshTests(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(prefix="chanlun-market-refresh-", suffix=".db")
        self.previous_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = self.db_path
        db.init_db()
        self.universe = [quote(f"{index:06d}") for index in range(1_000)]
        db.upsert_securities(self.universe)

    def replace_quotes(self, quote_minute: str, updated_at: str) -> None:
        with patch.object(db, "now_iso", return_value=updated_at):
            db.replace_daily_quotes(self.universe, quote_minute)

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.previous_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self.db_path}{suffix}")
            except FileNotFoundError:
                pass

    def test_current_complete_snapshot_is_reused_without_provider_request(self):
        now = datetime(2026, 8, 20, 10, 0)
        self.replace_quotes("2026-08-20 10:00", "2026-08-20T10:00:10")

        with patch.object(stock_data.market_data, "list_market_quotes") as refresh:
            result = stock_data.synchronize_market_data(now=now)

        refresh.assert_not_called()
        self.assertFalse(result["stale"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["source"], "cache")

    def test_stale_snapshot_refreshes_to_current_session(self):
        now = datetime(2026, 8, 20, 10, 0)
        self.replace_quotes("2026-08-20 09:57", "2026-08-20T09:57:05")

        with patch.object(db, "now_iso", return_value="2026-08-20T10:00:10"), patch.object(
            stock_data.market_data, "tushare_is_configured", return_value=False
        ), patch.object(stock_data.market_data, "_eastmoney_stocks", return_value=self.universe) as refresh:
            result = stock_data.synchronize_market_data(now=now)

        refresh.assert_called_once()
        self.assertFalse(result["stale"])
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["source"], "eastmoney")
        self.assertEqual(result["quoteMinute"], "2026-08-20 10:00")

    def test_forced_sync_refreshes_even_when_the_coarse_session_key_matches(self):
        now = datetime(2026, 8, 20, 16, 0)
        self.replace_quotes("2026-08-20 closed", "2026-08-20T15:06:00")

        with patch.object(db, "now_iso", return_value="2026-08-20T16:00:10"), patch.object(
            stock_data.market_data, "tushare_is_configured", return_value=False
        ), patch.object(stock_data.market_data, "_eastmoney_stocks", return_value=self.universe) as refresh:
            result = stock_data.synchronize_market_data(force=True, now=now)

        refresh.assert_called_once()
        self.assertFalse(result["stale"])
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["source"], "eastmoney")

    def test_forced_sync_marks_cache_stale_when_all_providers_fail(self):
        now = datetime(2026, 8, 20, 16, 0)
        self.replace_quotes("2026-08-20 closed", "2026-08-20T15:06:00")

        with patch.object(stock_data.market_data, "tushare_is_configured", return_value=False), patch.object(
            stock_data.market_data, "_eastmoney_stocks", side_effect=RuntimeError("东方财富不可用")
        ), patch.object(stock_data.market_data, "_tencent_stocks", side_effect=RuntimeError("腾讯不可用")), patch.object(
            stock_data.market_data, "_akshare_market_items", side_effect=RuntimeError("AkShare不可用")
        ):
            result = stock_data.synchronize_market_data(force=True, now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["source"], "cache")
        self.assertIn("东方财富", result["error"])

    def test_failed_refresh_preserves_complete_stale_snapshot(self):
        now = datetime(2026, 8, 20, 10, 0)
        self.replace_quotes("2026-08-20 09:57", "2026-08-20T09:57:05")

        with patch.object(stock_data.market_data, "tushare_is_configured", return_value=False), patch.object(
            stock_data.market_data, "_eastmoney_stocks", side_effect=RuntimeError("东方财富不可用")
        ), patch.object(stock_data.market_data, "_tencent_stocks", side_effect=RuntimeError("腾讯不可用")), patch.object(
            stock_data.market_data, "_akshare_market_items", side_effect=RuntimeError("AkShare不可用")
        ):
            result = stock_data.synchronize_market_data(now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["source"], "cache")
        self.assertEqual(result["quoteMinute"], "2026-08-20 09:57")
        self.assertIn("东方财富", result["error"])

    def test_lunch_break_reuses_a_same_day_morning_snapshot(self):
        result = stock_data._market_snapshot_metadata(
            status={"count": 1_000, "quoteMinute": "2026-08-20 11:29", "updatedAt": "2026-08-20T11:29:10"},
            now=datetime(2026, 8, 20, 12, 20),
        )

        self.assertEqual(result["session"], "lunch_break")
        self.assertFalse(result["stale"])

    def test_after_close_requires_a_post_close_snapshot_write(self):
        now = datetime(2026, 8, 20, 16, 0)
        early = stock_data._market_snapshot_metadata(
            status={"count": 1_000, "quoteMinute": "2026-08-20 closed", "updatedAt": "2026-08-20T15:04:59"},
            now=now,
        )
        closing = stock_data._market_snapshot_metadata(
            status={"count": 1_000, "quoteMinute": "2026-08-20 closed", "updatedAt": "2026-08-20T15:06:00"},
            now=now,
        )

        self.assertTrue(early["stale"])
        self.assertFalse(closing["stale"])

    def test_pre_open_reuses_the_most_recent_closed_snapshot(self):
        result = stock_data._market_snapshot_metadata(
            status={"count": 1_000, "quoteMinute": "2026-08-21 closed", "updatedAt": "2026-08-21T15:06:00"},
            now=datetime(2026, 8, 24, 9, 0),
        )

        self.assertEqual(result["session"], "pre_open")
        self.assertFalse(result["stale"])

    def test_closed_session_uses_most_recent_weekday(self):
        weekend = datetime(2026, 8, 22, 10, 0)
        pre_open = datetime(2026, 8, 24, 9, 0)

        self.assertEqual(stock_data._current_quote_session_key(weekend), "2026-08-21 closed")
        self.assertEqual(stock_data._current_quote_session_key(pre_open), "2026-08-21 closed")


if __name__ == "__main__":
    unittest.main()
