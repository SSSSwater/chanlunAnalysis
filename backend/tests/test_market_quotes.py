from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from backend.services import database as db
from backend.services import stock_data
from backend.services.market_data_util import MarketDataResult


def quote(symbol: str, name: str) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "market": "0",
        "secid": f"0.{symbol}",
        "secucode": f"{symbol}.SZ",
        "isSt": False,
        "latestPrice": 10.0,
        "pctChange": 1.0,
        "priceChange": 0.1,
        "volume": 1000.0,
        "amount": 10000.0,
        "turnoverRate": 1.0,
        "peDynamic": 10.0,
        "pb": 1.0,
        "totalMarketCap": 1000000.0,
    }


class MarketQuoteFallbackTests(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(prefix="chanlun-market-", suffix=".db")
        self.previous_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = self.db_path
        db.init_db()

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

    def test_partial_eastmoney_snapshot_falls_back_to_tencent(self):
        universe = [quote(f"{index:06d}", f"Stock {index}") for index in range(1000)]
        db.upsert_securities(universe)
        partial = universe[:196]
        tencent = universe[:900]

        with patch.object(stock_data.market_data, "tushare_is_configured", return_value=False), patch.object(
            stock_data.market_data, "_eastmoney_stocks", return_value=partial
        ), patch.object(stock_data.market_data, "_tencent_stocks", return_value=tencent):
            meta = stock_data.ensure_daily_quotes(force=True)

        self.assertEqual(meta["source"], "tencent")
        self.assertEqual(meta["count"], 900)
        self.assertEqual(len(db.list_daily_quotes()), 900)

    def test_market_rankings_can_exclude_growth_and_star_boards(self):
        rows = [
            quote("300001", "创业板样本"),
            quote("688001", "科创板样本"),
            quote("920001", "京板样本"),
            quote("600001", "主板样本"),
        ]
        snapshot = {
            "quoteMinute": "2026-08-20 closed",
            "updatedAt": "2026-08-20T16:00:00",
            "requiredQuoteMinute": "2026-08-20 closed",
            "complete": True,
            "stale": False,
        }
        with patch.object(stock_data.db, "list_daily_quotes", return_value=rows), patch.object(
            stock_data, "_market_snapshot_metadata", return_value=snapshot
        ), patch.object(stock_data, "get_home_index_quotes", return_value=[]):
            included = stock_data.get_market_today(include_growth_boards=True)
            excluded = stock_data.get_market_today()

        self.assertEqual(included["count"], 4)
        self.assertEqual(len(included["topGainers"]), 3)
        self.assertFalse(excluded["rankingIncludesGrowthBoards"])
        self.assertEqual([item["symbol"] for item in excluded["topGainers"]], ["600001"])
        self.assertEqual([item["symbol"] for item in excluded["topTurnover"]], ["600001"])

    def test_market_today_can_force_index_quote_refresh(self):
        snapshot = {
            "quoteMinute": "2026-08-20 closed",
            "updatedAt": "2026-08-20T16:00:00",
            "requiredQuoteMinute": "2026-08-20 closed",
            "complete": True,
            "stale": False,
        }
        with patch.object(stock_data.db, "list_daily_quotes", return_value=[]), patch.object(
            stock_data, "_market_snapshot_metadata", return_value=snapshot
        ), patch.object(stock_data, "get_home_index_quotes", return_value=[]) as refresh_indices:
            result = stock_data.get_market_today(refresh_indices=True)

        refresh_indices.assert_called_once_with(force=True, cache_only=False)
        self.assertEqual(result["indices"], [])

    def test_market_today_uses_cached_indices_without_refreshing(self):
        snapshot = {
            "quoteMinute": "2026-08-20 closed",
            "updatedAt": "2026-08-20T16:00:00",
            "requiredQuoteMinute": "2026-08-20 closed",
            "complete": True,
            "stale": False,
        }
        with patch.object(stock_data.db, "list_daily_quotes", return_value=[]), patch.object(
            stock_data, "_market_snapshot_metadata", return_value=snapshot
        ), patch.object(stock_data, "get_home_index_quotes", return_value=[]) as cached_indices:
            result = stock_data.get_market_today()

        cached_indices.assert_called_once_with(force=False, cache_only=True)
        self.assertEqual(result["indices"], [])

    def test_search_catalog_is_kept_in_memory_between_queries(self):
        catalog = [quote("000001", "平安银行"), quote("000002", "万科A")]
        with patch.object(
            stock_data.market_data,
            "list_non_tushare_securities",
            return_value=MarketDataResult(items=catalog, source="eastmoney"),
        ) as load_catalog:
            first = stock_data.search_stocks("平安")
            second = stock_data.search_stocks("000002")

        self.assertEqual(first[0]["name"], "平安银行")
        self.assertEqual(second[0]["name"], "万科A")
        load_catalog.assert_called_once()

    def test_rankings_restore_catalog_name_when_quote_contains_a_code(self):
        db.upsert_securities([quote("000001", "平安银行")])
        rows = [quote("000001", "000001")]
        snapshot = {
            "quoteMinute": "2026-08-20 closed",
            "updatedAt": "2026-08-20T16:00:00",
            "requiredQuoteMinute": "2026-08-20 closed",
            "complete": True,
            "stale": False,
        }
        with patch.object(stock_data.db, "list_daily_quotes", return_value=rows), patch.object(
            stock_data, "_market_snapshot_metadata", return_value=snapshot
        ), patch.object(stock_data, "get_home_index_quotes", return_value=[]):
            result = stock_data.get_market_today()

        self.assertEqual(result["topGainers"][0]["name"], "平安银行")


if __name__ == "__main__":
    unittest.main()
