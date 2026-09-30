from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from backend.services import database as db
from backend.services import eastmoney_client
from backend.services import stock_data


class FundamentalValuationTests(unittest.TestCase):
    def setUp(self):
        self.db_path = tempfile.mktemp(prefix="chanlun-valuation-", suffix=".db")
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

    def test_partial_summary_does_not_hide_snapshot_valuation(self):
        snapshot = {
            "latestPrice": 19.96,
            "peDynamic": 11.66,
            "peTtm": 13.39,
            "pb": 1.49,
            "totalMarketCap": 54_888_862_579.4,
        }
        cached = {"latestPrice": None, "peDynamic": None, "pb": None}
        quote = {"latestPrice": 19.96, "pctChange": -0.55, "turnoverRate": 1.53}

        with patch.object(stock_data, "_get_snapshot_with_cache", return_value=snapshot) as fetch_snapshot, patch.object(
            db, "list_daily_quotes", return_value=[quote]
        ), patch.object(db, "search_securities", return_value=[]):
            result = stock_data._resolve_valuation("600426", cached)

        fetch_snapshot.assert_called_once_with("600426")
        self.assertEqual(result["peDynamic"], 11.66)
        self.assertEqual(result["peTtm"], 13.39)
        self.assertEqual(result["pb"], 1.49)
        self.assertEqual(result["pctChange"], -0.55)

    def test_snapshot_pb_and_nav_are_not_scaled_again(self):
        payload = {
            "data": {
                "f57": "600426",
                "f58": "华鲁恒升",
                "f43": 1996,
                "f59": 2,
                "f162": 1166,
                "f164": 1339,
                "f167": 157,
                "f108": 1.490792882,
                "f92": 16.5139831,
            }
        }

        with patch.object(eastmoney_client, "_get_json", return_value=payload):
            result = eastmoney_client.get_stock_snapshot("600426")

        self.assertAlmostEqual(result["peDynamic"], 11.66)
        self.assertAlmostEqual(result["peTtm"], 13.39)
        self.assertAlmostEqual(result["pb"], 1.490792882)
        self.assertAlmostEqual(result["navPerShare"], 16.5139831)


if __name__ == "__main__":
    unittest.main()
