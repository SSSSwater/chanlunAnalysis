from __future__ import annotations

import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import database as db


class PortfolioTradeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "portfolio-api.db")
        db.init_db()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        user = db.register_user("portfolio-api", "portfolio-password")
        session = db.create_auth_session(user["id"])
        self.auth_header = {"Authorization": f"Bearer {session['token']}"}

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_unconfirmed_discipline_exception_returns_confirmation_details(self):
        violations = [{"code": "AVERAGING_DOWN", "message": "向下摊平"}]
        with patch.object(
            app_module,
            "execute_portfolio_trade",
            side_effect=db.DisciplineConfirmationRequired(violations),
        ):
            response = self.client.post(
                "/api/portfolio/trades",
                json={"action": "BUY", "symbol": "000001"},
                headers=self.auth_header,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["requiresDisciplineConfirmation"], True)
        self.assertEqual(response.get_json()["disciplineViolations"], violations)

    def test_api_forwards_explicit_discipline_override(self):
        with patch.object(app_module, "execute_portfolio_trade", return_value={"id": 2}) as execute_trade:
            response = self.client.post(
                "/api/portfolio/trades",
                json={"action": "BUY", "symbol": "000001", "disciplineOverride": True},
                headers=self.auth_header,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIs(execute_trade.call_args.kwargs["discipline_override"], True)
        self.assertIsNone(execute_trade.call_args.kwargs["fee"])


if __name__ == "__main__":
    unittest.main()
