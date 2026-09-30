from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import database as db
from backend.services import stock_data


class WatchlistStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "watchlist.db")
        db.init_db()
        self.user_id = db.register_user("watchlist-test", "watchlist-password")["id"]

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_watchlist_item_persists_and_includes_cached_quote(self):
        db.upsert_watchlist_item(self.user_id, {"symbol": "000001", "name": "平安银行", "note": "等待突破确认"})
        db.upsert_daily_quotes(
            [{"symbol": "000001", "name": "平安银行", "latestPrice": 10.25, "pctChange": 1.8}],
            "2026-08-21 closed",
        )

        result = stock_data.get_watchlist(self.user_id)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["name"], "平安银行")
        self.assertEqual(result["items"][0]["note"], "等待突破确认")
        self.assertEqual(result["items"][0]["latestPrice"], 10.25)

    def test_catalog_name_replaces_placeholder_names_in_positions_watchlist_and_new_trades(self):
        db.upsert_securities(
            [{
                "symbol": "000001",
                "name": "平安银行",
                "market": "0",
                "secid": "0.000001",
                "secucode": "000001.SZ",
                "isSt": False,
            }]
        )
        db.upsert_daily_quotes(
            [{"symbol": "000001", "name": "代码直查", "latestPrice": 10.25, "pctChange": 1.8}],
            "2026-08-21 closed",
        )
        db.upsert_portfolio_position(
            self.user_id,
            {"symbol": "000001", "name": "000001", "costPrice": 10.0, "shares": 100, "note": ""},
        )
        db.upsert_watchlist_item(self.user_id, {"symbol": "000001", "name": "代码直查", "note": "等待确认"})

        self.assertEqual(stock_data.get_portfolio(self.user_id)["items"][0]["name"], "平安银行")
        self.assertEqual(stock_data.get_watchlist(self.user_id)["items"][0]["name"], "平安银行")

        db.delete_portfolio_position(self.user_id, "000001")
        db.set_portfolio_cash(self.user_id, 5000)
        stock_data.execute_portfolio_trade(
            self.user_id,
            "BUY",
            "000001",
            10.0,
            100,
            fee=0,
            name="代码直查",
        )

        self.assertEqual(db.get_portfolio_position(self.user_id, "000001")["name"], "平安银行")

    def test_delete_watchlist_item_removes_only_requested_symbol(self):
        db.upsert_watchlist_item(self.user_id, {"symbol": "000001", "name": "平安银行"})
        db.upsert_watchlist_item(self.user_id, {"symbol": "600000", "name": "浦发银行"})

        self.assertTrue(db.delete_watchlist_item(self.user_id, "000001"))
        self.assertEqual([item["symbol"] for item in db.list_watchlist_items(self.user_id)], ["600000"])


class WatchlistApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "watchlist-api.db")
        db.init_db()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        user = db.register_user("watchlist-api", "watchlist-password")
        self.user_id = user["id"]
        session = db.create_auth_session(user["id"])
        self.auth_header = {"Authorization": f"Bearer {session['token']}"}

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_watchlist_rejects_invalid_symbol(self):
        response = self.client.post("/api/watchlist", json={"symbol": "not-a-stock"}, headers=self.auth_header)

        self.assertEqual(response.status_code, 400)
        self.assertIn("有效股票代码", response.get_json()["message"])

    def test_watchlist_batch_analysis_returns_each_item_and_summary(self):
        watchlist = {
            "quoteMinute": "2026-08-21 closed",
            "updatedAt": "2026-08-21T15:00:00",
            "stale": False,
            "marketData": {},
            "items": [{"symbol": "000001", "name": "平安银行", "latestPrice": 10.25}],
        }
        analyzed = {
            "symbol": "000001",
            "name": "平安银行",
            "latestPrice": 10.25,
            "plan": {"action": "BUY"},
            "error": None,
        }
        with patch.object(app_module, "get_watchlist", return_value=watchlist), patch.object(
            app_module,
            "get_portfolio",
            return_value={"account": {"totalAccountValue": 100000, "cashBalance": 50000}},
        ), patch.object(app_module, "get_cached_index_history", return_value=[]), patch.object(
            app_module,
            "_build_watchlist_discipline_item",
            return_value=analyzed,
        ):
            response = self.client.get("/api/discipline/watchlist", headers=self.auth_header)

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["items"], [analyzed])
        self.assertEqual(payload["summary"], {"watchCount": 1, "actionCounts": {"BUY": 1}, "errorCount": 0})

    def test_watchlist_without_position_exposes_entry_candidates_only(self):
        bearish = {"direction": "SELL", "planId": "sell-1"}
        bullish = {"direction": "BUY", "planId": "buy-1"}
        plan = {
            "tradePlans": [bearish, bullish],
            "positionContext": {
                "hasPosition": False,
                "shares": 0,
                "mode": "ENTRY_WATCH",
            },
        }
        with patch.object(app_module, "_build_discipline_plan", return_value=plan):
            result = app_module._build_watchlist_discipline_item(
                {"symbol": "000001", "name": "平安银行"},
                ("20260101", "20260821", 100000, 50000, [], self.user_id),
            )

        self.assertIsNone(result["error"])
        self.assertEqual(result["plan"]["tradePlans"], [bullish])
        self.assertEqual(result["plan"]["candidateTradePlans"], [bearish, bullish])
        self.assertEqual(result["plan"]["positionContext"]["excludedManagementPlans"], 1)


if __name__ == "__main__":
    unittest.main()
