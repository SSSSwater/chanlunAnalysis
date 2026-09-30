from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.services import database as db


class PortfolioAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "portfolio.db")
        db.init_db()
        self.user_id = db.register_user("portfolio-test", "portfolio-password") ["id"]

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_buy_updates_cash_and_weighted_average_cost(self):
        db.set_portfolio_cash(self.user_id, 2_000)

        first = db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "name": "测试股", "price": 5, "shares": 100, "fee": 10}
        )
        second = db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "name": "测试股", "price": 6, "shares": 100, "fee": 0}
        )

        position = db.get_portfolio_position(self.user_id, "000001")
        self.assertEqual(first["cashAfter"], 1490)
        self.assertEqual(second["cashAfter"], 890)
        self.assertEqual(position["shares"], 200)
        self.assertEqual(position["costPrice"], 5.55)

    def test_sell_releases_cash_and_keeps_remaining_cost(self):
        db.set_portfolio_cash(self.user_id, 1_000)
        db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "name": "测试股", "price": 5, "shares": 100, "fee": 0}
        )

        trade = db.execute_portfolio_trade(self.user_id,
            {"action": "SELL", "symbol": "000001", "price": 6, "shares": 50, "fee": 5}
        )
        position = db.get_portfolio_position(self.user_id, "000001")

        self.assertEqual(trade["cashAfter"], 795)
        self.assertEqual(trade["realizedProfit"], 45)
        self.assertEqual(position["shares"], 50)
        self.assertEqual(position["costPrice"], 5)

    def test_buy_rejects_amount_above_available_cash(self):
        db.set_portfolio_cash(self.user_id, 100)
        with self.assertRaisesRegex(ValueError, "可用现金不足"):
            db.execute_portfolio_trade(self.user_id,
                {"action": "BUY", "symbol": "000001", "price": 2, "shares": 100, "fee": 0}
            )

    def test_buy_uses_default_fee_when_fee_is_omitted(self):
        db.set_portfolio_cash(self.user_id, 2_000)

        trade = db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "600000", "price": 10, "shares": 100}
        )
        position = db.get_portfolio_position(self.user_id, "600000")

        self.assertEqual(trade["fee"], 0.30)
        self.assertEqual(trade["cashAfter"], 999.70)
        self.assertEqual(position["costPrice"], 10.003)

    def test_sell_uses_default_fee_when_fee_is_omitted(self):
        db.set_portfolio_cash(self.user_id, 2_000)
        db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "price": 10, "shares": 100, "fee": 0}
        )

        trade = db.execute_portfolio_trade(self.user_id,
            {"action": "SELL", "symbol": "000001", "price": 10, "shares": 100}
        )

        self.assertEqual(trade["fee"], 0.81)
        self.assertEqual(trade["cashAfter"], 1_999.19)

    def test_buy_requires_confirmation_before_averaging_down_existing_position(self):
        db.set_portfolio_cash(self.user_id, 2_000)
        db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "price": 5, "shares": 100, "fee": 0}
        )

        with self.assertRaises(db.DisciplineConfirmationRequired) as captured:
            db.execute_portfolio_trade(self.user_id,
                {"action": "BUY", "symbol": "000001", "price": 4, "shares": 100, "fee": 0}
            )

        self.assertEqual(captured.exception.violations[0]["code"], "AVERAGING_DOWN")
        self.assertEqual(db.get_portfolio_position(self.user_id, "000001")["shares"], 100)
        self.assertEqual(db.get_portfolio_account(self.user_id)["cashBalance"], 1_500)
        self.assertEqual(len(db.list_portfolio_trades(self.user_id)), 1)

    def test_confirmed_averaging_down_updates_cost_and_marks_ledger_exception(self):
        db.set_portfolio_cash(self.user_id, 2_000)
        db.execute_portfolio_trade(self.user_id,
            {"action": "BUY", "symbol": "000001", "price": 5, "shares": 100, "fee": 0}
        )

        trade = db.execute_portfolio_trade(self.user_id,
            {
                "action": "BUY",
                "symbol": "000001",
                "price": 4,
                "shares": 100,
                "fee": 0,
                "disciplineOverride": True,
            }
        )

        position = db.get_portfolio_position(self.user_id, "000001")
        self.assertEqual(position["shares"], 200)
        self.assertEqual(position["costPrice"], 4.5)
        self.assertEqual(trade["disciplineOverride"], True)
        self.assertEqual(trade["disciplineOverrideReasons"][0]["code"], "AVERAGING_DOWN")
        self.assertEqual(db.get_portfolio_account(self.user_id)["cashBalance"], 1_100)


if __name__ == "__main__":
    unittest.main()
