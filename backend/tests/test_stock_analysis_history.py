from __future__ import annotations

import os
import tempfile
import unittest

from backend.services import database as db


class StockAnalysisHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(prefix="chanlun-stock-history-", suffix=".db")
        self.previous_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = self.db_path
        db.init_db()
        self.user_one = db.register_user("history-user-1", "password-123")
        self.user_two = db.register_user("history-user-2", "password-123")

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

    def test_history_is_scoped_per_user_and_limited_to_ten_items(self):
        user_one_id = self.user_one["id"]
        user_two_id = self.user_two["id"]

        for index in range(12):
            db.upsert_stock_analysis_history(
                user_one_id,
                {"symbol": f"600{index:03d}", "name": f"股票{index}"},
            )
        db.upsert_stock_analysis_history(user_one_id, {"symbol": "600000", "name": "股票零"})
        db.upsert_stock_analysis_history(user_two_id, {"symbol": "600000", "name": "另一用户的股票"})

        user_one_history = db.list_stock_analysis_history(user_one_id)
        user_two_history = db.list_stock_analysis_history(user_two_id)

        self.assertEqual(len(user_one_history), 10)
        self.assertEqual(len({item["symbol"] for item in user_one_history}), 10)
        self.assertEqual(user_two_history[0]["name"], "另一用户的股票")
        self.assertNotEqual(
            {item["name"] for item in user_one_history},
            {item["name"] for item in user_two_history},
        )

    def test_history_can_delete_one_item_without_affecting_another_user(self):
        user_one_id = self.user_one["id"]
        user_two_id = self.user_two["id"]
        db.upsert_stock_analysis_history(user_one_id, {"symbol": "600000", "name": "浦发银行"})
        db.upsert_stock_analysis_history(user_one_id, {"symbol": "600036", "name": "招商银行"})
        db.upsert_stock_analysis_history(user_two_id, {"symbol": "600000", "name": "另一用户的股票"})

        self.assertTrue(db.delete_stock_analysis_history(user_one_id, "600000"))
        self.assertFalse(db.delete_stock_analysis_history(user_one_id, "600000"))
        self.assertEqual([item["symbol"] for item in db.list_stock_analysis_history(user_one_id)], ["600036"])
        self.assertEqual([item["symbol"] for item in db.list_stock_analysis_history(user_two_id)], ["600000"])


if __name__ == "__main__":
    unittest.main()
