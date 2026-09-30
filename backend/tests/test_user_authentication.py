from __future__ import annotations

import os
import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import database as db


class UserAuthenticationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "auth.db")
        db.init_db()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def register(self, username: str) -> tuple[dict, dict]:
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "secure-password"},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        return payload["user"], {"Authorization": f"Bearer {payload['token']}"}

    def test_registration_session_restore_and_logout(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "secure-password"},
        )

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["user"]["username"], "alice")
        self.assertNotIn("password", payload["user"])
        self.assertTrue(payload["token"])
        self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))

        # The persistent cookie is an independent recovery path for a fresh
        # page load or a browser context where localStorage is unavailable.
        cookie_only = self.client.get("/api/auth/me")
        self.assertEqual(cookie_only.status_code, 200)
        self.assertEqual(cookie_only.get_json()["user"]["username"], "alice")

        header = {"Authorization": f"Bearer {payload['token']}"}
        self.assertEqual(self.client.get("/api/auth/me", headers=header).get_json()["user"]["username"], "alice")

        # A stale browser header must not shadow a valid persistent cookie.
        stale_header = {"Authorization": "Bearer stale-session-token"}
        self.assertEqual(self.client.get("/api/auth/me", headers=stale_header).status_code, 200)

        self.assertEqual(self.client.post("/api/auth/logout", headers=header).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=header).status_code, 401)

    def test_registration_and_login_reject_invalid_credentials(self):
        invalid = self.client.post(
            "/api/auth/register",
            json={"username": "a", "password": "short"},
        )
        self.assertEqual(invalid.status_code, 400)

        self.register("alice")
        duplicate = self.client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "another-password"},
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code,
            401,
        )
        login = self.client.post("/api/auth/login", json={"username": "alice", "password": "secure-password"})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.get_json()["token"])

    def test_private_trading_endpoints_reject_anonymous_requests(self):
        for path in (
            "/api/portfolio",
            "/api/portfolio/trades",
            "/api/watchlist",
            "/api/discipline/portfolio",
            "/api/discipline/watchlist",
            "/api/discipline/journal",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertTrue(response.get_json()["authenticationRequired"])

        with patch.object(app_module, "search_stocks", return_value=[]):
            self.assertEqual(self.client.get("/api/stocks/search?keyword=000001").status_code, 200)

        for method in (self.client.get, self.client.put):
            response = method("/api/account/ai-settings", json={} if method == self.client.put else None)
            self.assertEqual(response.status_code, 401)
            self.assertTrue(response.get_json()["authenticationRequired"])

        with patch.object(app_module, "analyze_with_ai") as analyze_with_ai:
            response = self.client.post("/api/ai/analyze", json={"result": {}})
            self.assertEqual(response.status_code, 401)
            analyze_with_ai.assert_not_called()

    def test_ai_settings_are_isolated_and_ai_route_uses_database_settings(self):
        first_user, first_header = self.register("first-ai-user")
        second_user, second_header = self.register("second-ai-user")

        saved = self.client.put(
            "/api/account/ai-settings",
            json={
                "baseUrl": "https://first.example/v1",
                "apiKey": "first-secret",
                "model": "first-model",
            },
            headers=first_header,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.get_json()["settings"]["apiKeyConfigured"])
        self.assertNotIn("apiKey", saved.get_json()["settings"])

        first_settings = self.client.get("/api/account/ai-settings", headers=first_header)
        second_settings = self.client.get("/api/account/ai-settings", headers=second_header)
        self.assertEqual(first_settings.status_code, 200)
        self.assertEqual(first_settings.get_json()["settings"]["baseUrl"], "https://first.example/v1")
        self.assertEqual(first_settings.get_json()["settings"]["model"], "first-model")
        self.assertTrue(first_settings.get_json()["settings"]["apiKeyConfigured"])
        self.assertNotIn("apiKey", first_settings.get_json()["settings"])
        self.assertFalse(second_settings.get_json()["settings"]["apiKeyConfigured"])
        self.assertEqual(db.get_user_ai_settings(first_user["id"], include_secret=True)["apiKey"], "first-secret")
        self.assertEqual(db.get_user_ai_settings(second_user["id"], include_secret=True)["apiKey"], "")

        with patch.object(
            app_module,
            "analyze_with_ai",
            return_value={"targetType": "stock", "analysis": "ok"},
        ) as analyze_with_ai:
            response = self.client.post(
                "/api/ai/analyze",
                json={
                    "result": {"symbol": "000001"},
                    "settings": {
                        "baseUrl": "https://attacker.example/v1",
                        "apiKey": "attacker-secret",
                        "model": "attacker-model",
                    },
                },
                headers=first_header,
            )
        self.assertEqual(response.status_code, 200)
        analyze_with_ai.assert_called_once()
        self.assertEqual(analyze_with_ai.call_args.kwargs["base_url"], "https://first.example/v1")
        self.assertEqual(analyze_with_ai.call_args.kwargs["api_key"], "first-secret")
        self.assertEqual(analyze_with_ai.call_args.kwargs["model"], "first-model")

        with patch.object(app_module, "analyze_with_ai") as analyze_with_ai:
            response = self.client.post(
                "/api/ai/analyze",
                json={"result": {"symbol": "000001"}},
                headers=second_header,
            )
        self.assertEqual(response.status_code, 400)
        analyze_with_ai.assert_not_called()

    def test_users_cannot_read_or_modify_each_others_trading_data(self):
        first_user, first_header = self.register("first-user")
        second_user, second_header = self.register("second-user")
        db.upsert_daily_quotes(
            [{"symbol": "000001", "name": "平安银行", "latestPrice": 10.2, "pctChange": 1.1}],
            "2026-08-21 closed",
        )
        db.upsert_portfolio_position(
            first_user["id"],
            {"symbol": "000001", "name": "第一账户", "costPrice": 9.5, "shares": 100, "note": "first"},
        )
        db.upsert_portfolio_position(
            second_user["id"],
            {"symbol": "000001", "name": "第二账户", "costPrice": 8.5, "shares": 200, "note": "second"},
        )
        db.set_portfolio_cash(first_user["id"], 5_000)
        db.set_portfolio_cash(second_user["id"], 8_000)
        db.execute_portfolio_trade(
            first_user["id"],
            {"action": "BUY", "symbol": "600000", "name": "浦发银行", "price": 10, "shares": 100, "fee": 0},
        )
        db.execute_portfolio_trade(
            second_user["id"],
            {"action": "BUY", "symbol": "600000", "name": "浦发银行", "price": 8, "shares": 100, "fee": 0},
        )

        self.assertEqual(
            self.client.post(
                "/api/watchlist",
                json={"symbol": "000001", "name": "第一自选", "note": "first-note"},
                headers=first_header,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/watchlist",
                json={"symbol": "000001", "name": "第二自选", "note": "second-note"},
                headers=second_header,
            ).status_code,
            200,
        )

        first_portfolio = self.client.get("/api/portfolio", headers=first_header).get_json()
        second_portfolio = self.client.get("/api/portfolio", headers=second_header).get_json()
        first_positions = {item["symbol"]: item for item in first_portfolio["items"]}
        second_positions = {item["symbol"]: item for item in second_portfolio["items"]}
        self.assertEqual(first_positions["000001"]["costPrice"], 9.5)
        self.assertEqual(second_positions["000001"]["costPrice"], 8.5)
        self.assertEqual(first_positions["600000"]["shares"], 100)
        self.assertEqual(second_positions["600000"]["shares"], 100)
        self.assertEqual(first_portfolio["summary"]["cashBalance"], 4_000)
        self.assertEqual(second_portfolio["summary"]["cashBalance"], 7_200)
        self.assertEqual(
            [item["note"] for item in self.client.get("/api/watchlist", headers=first_header).get_json()["items"]],
            ["first-note"],
        )
        self.assertEqual(
            [item["note"] for item in self.client.get("/api/watchlist", headers=second_header).get_json()["items"]],
            ["second-note"],
        )
        self.assertEqual(len(self.client.get("/api/portfolio/trades", headers=first_header).get_json()["items"]), 1)
        self.assertEqual(len(self.client.get("/api/portfolio/trades", headers=second_header).get_json()["items"]), 1)

        self.assertEqual(self.client.delete("/api/watchlist/000001", headers=first_header).status_code, 200)
        self.assertEqual(self.client.get("/api/watchlist", headers=first_header).get_json()["items"], [])
        self.assertEqual(len(self.client.get("/api/watchlist", headers=second_header).get_json()["items"]), 1)


class LegacyPersonalDataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        self.db_file = Path(self.temp_dir.name) / "legacy.db"
        os.environ["CHANLUN_DB_PATH"] = str(self.db_file)
        with sqlite3.connect(self.db_file) as conn:
            conn.executescript(
                """
                CREATE TABLE portfolio_positions (
                    symbol TEXT PRIMARY KEY, name TEXT, cost_price REAL NOT NULL, shares INTEGER NOT NULL,
                    note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE watchlist_items (
                    symbol TEXT PRIMARY KEY, name TEXT, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE portfolio_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1), cash_balance REAL NOT NULL DEFAULT 100000,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE portfolio_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT,
                    price REAL NOT NULL, shares INTEGER NOT NULL, fee REAL NOT NULL DEFAULT 0, amount REAL NOT NULL,
                    cash_before REAL NOT NULL, cash_after REAL NOT NULL, cost_before REAL, cost_after REAL,
                    shares_before INTEGER NOT NULL DEFAULT 0, shares_after INTEGER NOT NULL DEFAULT 0,
                    realized_profit REAL, note TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE discipline_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, action TEXT NOT NULL, trade_date TEXT,
                    acknowledged_json TEXT NOT NULL, note TEXT, plan_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO portfolio_positions VALUES ('000001', '旧持仓', 9.8, 100, 'legacy', '2026-08-01', '2026-08-02')"
            )
            conn.execute(
                "INSERT INTO watchlist_items VALUES ('600000', '旧自选', 'legacy', '2026-08-01', '2026-08-02')"
            )
            conn.execute("INSERT INTO portfolio_account VALUES (1, 43210, '2026-08-02')")
            conn.execute(
                """
                INSERT INTO portfolio_trades (
                    action, symbol, name, price, shares, fee, amount, cash_before, cash_after,
                    shares_before, shares_after, note, created_at
                ) VALUES ('BUY', '000001', '旧持仓', 9.8, 100, 0, 980, 44190, 43210, 0, 100, 'legacy', '2026-08-02')
                """
            )
            conn.execute(
                """
                INSERT INTO discipline_journal (
                    symbol, action, trade_date, acknowledged_json, note, plan_json, created_at
                ) VALUES ('000001', 'HOLD', '2026-08-02', '[\"a\"]', 'legacy', '{}', '2026-08-02')
                """
            )
        db.init_db()

    def tearDown(self):
        # Python 3.13 may retain temporary SQLite cursors until collection;
        # collect them while the temporary database is still selected.
        gc.collect()
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def test_first_registration_claims_legacy_data_once(self):
        first = db.register_user("legacy-owner", "legacy-password")
        second = db.register_user("second-owner", "second-password")

        self.assertEqual(db.get_portfolio_account(first["id"])["cashBalance"], 43210)
        self.assertEqual(db.get_portfolio_position(first["id"], "000001")["name"], "旧持仓")
        self.assertEqual(db.list_watchlist_items(first["id"])[0]["name"], "旧自选")
        self.assertEqual(len(db.list_portfolio_trades(first["id"])), 1)
        self.assertEqual(len(db.list_discipline_journal(first["id"])), 1)

        self.assertEqual(db.get_portfolio_account(second["id"])["cashBalance"], 100000)
        self.assertEqual(db.list_portfolio_positions(second["id"]), [])
        self.assertEqual(db.list_watchlist_items(second["id"]), [])
        self.assertEqual(db.list_portfolio_trades(second["id"]), [])


if __name__ == "__main__":
    unittest.main()
