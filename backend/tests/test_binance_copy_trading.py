from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import binance_copy_trading as copy_trading
from backend.services import database as db


class BinanceCopyTradingTests(unittest.TestCase):
    def setUp(self):
        copy_trading._cache_by_user.clear()
        copy_trading._pending_notifications_by_user.clear()

    def tearDown(self):
        copy_trading._cache_by_user.clear()
        copy_trading._pending_notifications_by_user.clear()

    def test_copy_multiplier_only_accepts_integers_from_one_to_ten(self):
        self.assertEqual(db.normalize_binance_copy_trading_settings({"copyMultiplier": 1})["copyMultiplier"], 1)
        self.assertEqual(db.normalize_binance_copy_trading_settings({"copyMultiplier": "10"})["copyMultiplier"], 10)
        for invalid_value in (0, 11, 1.5, "not-a-number", True):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(ValueError, "1 到 10 的整数"):
                    db.normalize_binance_copy_trading_settings({"copyMultiplier": invalid_value})

    def test_refreshes_every_subscription_and_fetches_positions_after_history_failure(self):
        subscriptions = {
            "success": True,
            "data": [
                {"topTraderId": "selected", "traderName": "Selected"},
                {"topTraderId": "other", "traderName": "Other"},
            ],
        }

        def history(payload, **_kwargs):
            if payload["topTraderId"] == "other":
                raise RuntimeError("operation is private")
            return {"success": True, "data": [{"symbol": "BTCUSDT", "time": 1}]}

        def positions(trader_id, **_kwargs):
            return {"success": True, "data": [{"symbol": f"{trader_id}USDT"}]}

        def profile(trader_id, **_kwargs):
            return {"success": True, "data": {"umMarginBalance": "100" if trader_id == "selected" else "200"}}

        follows = [{"topTraderId": "other", "symbol": "BTCUSDT", "positionSide": "LONG"}]
        with patch.object(copy_trading.db, "get_user_binance_copy_trading_settings", return_value={"topTraderId": "selected"}), patch.object(
            copy_trading.db,
            "get_user_binance_smart_money_auth",
            return_value={"configured": True, "cookie": "session"},
        ), patch.object(copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=follows), patch.object(
            copy_trading, "fetch_smart_money_subscriptions", return_value=subscriptions
        ), patch.object(copy_trading, "fetch_history", side_effect=history) as history_fetch, patch.object(
            copy_trading, "fetch_positions", side_effect=positions
        ) as positions_fetch, patch.object(copy_trading, "fetch_profile", side_effect=profile):
            result = copy_trading.refresh_binance_copy_trading_history_once(7)

        self.assertEqual([call.args[0]["topTraderId"] for call in history_fetch.call_args_list], ["selected", "other"])
        self.assertEqual([call.args[0] for call in positions_fetch.call_args_list], ["selected", "other"])
        snapshots = {item["topTraderId"]: item for item in result["traderSnapshots"]}
        self.assertEqual(snapshots["selected"]["positions"], [{"symbol": "selectedUSDT"}])
        self.assertIn("操作记录：operation is private", snapshots["other"]["error"])
        self.assertEqual(result["followedPositionSnapshots"][0]["positions"], [{"symbol": "otherUSDT"}])

    def test_follow_projection_reuses_trader_snapshots_without_fetching(self):
        cached = {
            "traderSnapshots": [
                {"topTraderId": "other", "positions": [{"symbol": "BTCUSDT"}], "sourceTotalMargin": 200, "error": None},
            ],
        }
        follows = [{"topTraderId": "other", "symbol": "BTCUSDT", "positionSide": "LONG"}]
        result = copy_trading.project_binance_copy_trading_follows(cached, follows)
        self.assertEqual(result["follows"], follows)
        self.assertEqual(result["followedPositionSnapshots"][0]["sourceTotalMargin"], 200)

    def test_reliable_account_snapshot_removes_only_missing_real_position_follows(self):
        follows = [
            {"topTraderId": "trader-a", "symbol": "BTCUSDT", "positionSide": "LONG"},
            {"topTraderId": "trader-b", "symbol": "ETHUSDT", "positionSide": "SHORT"},
            {"topTraderId": "trader-c", "symbol": "SOLUSDT", "positionSide": "LONG"},
        ]
        snapshot = {
            "stale": False,
            "account": {"futures": {"available": True, "positions": [
                {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.001"},
                {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "-2"},
                {"symbol": "SOLUSDT", "positionSide": "LONG", "positionAmt": "0"},
            ]}},
        }
        with patch.object(copy_trading, "get_cached_binance_account_snapshot", return_value=snapshot), patch.object(
            copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=follows
        ), patch.object(copy_trading.db, "set_user_binance_smart_money_position_follow") as set_follow:
            removed = copy_trading.cleanup_binance_smart_money_follows_without_real_positions(7)

        self.assertEqual(removed, [follows[2]])
        set_follow.assert_called_once_with(
            7, top_trader_id="trader-c", symbol="SOLUSDT", position_side="LONG", enabled=False,
        )

    def test_stale_account_snapshot_never_removes_follows(self):
        follows = [{"topTraderId": "trader", "symbol": "BTCUSDT", "positionSide": "LONG"}]
        snapshot = {"stale": True, "account": {"futures": {"available": True, "positions": []}}}
        with patch.object(copy_trading, "get_cached_binance_account_snapshot", return_value=snapshot), patch.object(
            copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=follows
        ) as list_follows, patch.object(copy_trading.db, "set_user_binance_smart_money_position_follow") as set_follow:
            removed = copy_trading.cleanup_binance_smart_money_follows_without_real_positions(7)

        self.assertEqual(removed, [])
        list_follows.assert_not_called()
        set_follow.assert_not_called()

    def test_selecting_trader_settings_reads_cache_without_remote_refresh(self):
        app_module.app.config.update(TESTING=True)
        with patch.object(app_module, "_current_user_from_request", return_value=({"id": 7}, "token")), patch.object(
            app_module.db,
            "save_user_binance_copy_trading_settings",
            return_value={"topTraderId": "other", "copyMultiplier": 1},
        ), patch.object(
            app_module.db,
            "list_user_binance_smart_money_position_follows",
            return_value=[],
        ), patch.object(
            app_module,
            "get_cached_binance_copy_trading_history",
            return_value={"subscriptions": [], "traderSnapshots": []},
        ):
            response = app_module.app.test_client().put(
                "/api/binance/copy-trading-settings",
                json={"topTraderId": "other", "copyMultiplier": 1},
                headers={"Authorization": "Bearer token"},
            )

        self.assertEqual(response.status_code, 200)

    def test_change_notification_ignores_operation_only_changes(self):
        previous = {"updatedAt": None, "traderSnapshots": []}
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{"topTraderId": "trader", "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 2}], "positions": []}],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        send_alert.assert_not_called()

        previous["updatedAt"] = 1
        previous["traderSnapshots"] = [{"topTraderId": "trader", "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 1}], "positions": []}]
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        send_alert.assert_not_called()

    def test_change_notification_does_not_realert_old_history_after_an_empty_response(self):
        previous = {
            "updatedAt": 1_000,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "ETHUSDT", "side": "BUY", "time": 1_000}],
                "positions": [],
                "error": None,
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 999}],
                "positions": [],
                "error": None,
            }],
        }

        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        send_alert.assert_not_called()

    def test_change_notification_ignores_aggregate_operation_changes(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 1_000, "qty": "1"}],
                "positions": [],
                "error": None,
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 1_000, "qty": "2"}],
                "positions": [],
                "error": None,
            }],
        }

        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        send_alert.assert_not_called()

    def test_change_notification_skips_operations_when_history_request_failed(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [],
                "error": "操作记录：timeout",
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 2}],
                "positions": [],
                "error": None,
            }],
        }

        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        send_alert.assert_not_called()

    def test_refresh_keeps_previous_operation_baseline_when_history_is_empty(self):
        copy_trading._cache_by_user[7] = {
            "updatedAt": 1,
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 1}],
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
                "error": None,
            }],
        }

        with patch.object(copy_trading.db, "get_user_binance_copy_trading_settings", return_value={"topTraderId": "trader"}), patch.object(
            copy_trading.db,
            "get_user_binance_smart_money_auth",
            return_value={"configured": True, "cookie": "session"},
        ), patch.object(copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=[]), patch.object(
            copy_trading.db,
            "get_user_notification_settings",
            return_value={},
        ), patch.object(
            copy_trading,
            "fetch_smart_money_subscriptions",
            return_value={"success": True, "data": [{"topTraderId": "trader", "traderName": "Trader"}]},
        ), patch.object(copy_trading, "fetch_history", return_value={"success": True, "data": []}), patch.object(
            copy_trading,
            "fetch_positions",
            return_value={"success": True, "data": [{"symbol": "BTCUSDT", "positionAmt": "1"}]},
        ), patch.object(
            copy_trading,
            "fetch_profile",
            return_value={"success": True, "data": {"umMarginBalance": "100"}},
        ):
            result = copy_trading.refresh_binance_copy_trading_history_once(7)

        self.assertEqual(result["traderSnapshots"][0]["items"][0]["time"], 1)

    def test_hidden_operation_notification_uses_position_quantity_change(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1"}]}],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2"}],
                "error": "操作记录：operation is private",
            }],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        send_alert.assert_called_once()
        message = send_alert.call_args.args[1][0]["message"]
        self.assertIn("Trader仓位：BTCUSDT:LONG 1 -> 2（+100.00%）", message)

    def test_position_reduction_closes_the_followed_real_position_before_email(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2"}],
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1"}],
            }],
        }
        with patch.object(copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=[{
            "topTraderId": "trader", "symbol": "BTCUSDT", "positionSide": "LONG",
        }]), patch.object(copy_trading.db, "get_user_binance_credentials", return_value={
            "configured": True, "network": "mainnet", "apiKey": "key", "apiSecret": "secret",
        }), patch.object(copy_trading, "close_futures_position_market", return_value={"quantity": 0.5, "positionQuantity": 1}) as close_position, patch.object(
            copy_trading, "send_resend_smart_money_alert"
        ) as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        close_position.assert_called_once_with(
            "mainnet", "key", "secret", symbol="BTCUSDT", position_side="LONG", quantity_ratio=50.0,
        )
        message = send_alert.call_args.args[1][0]["message"]
        self.assertIn("Trader仓位：BTCUSDT:LONG 2 -> 1（-50.00%），已按 50.00% 平掉真实仓位 0.5/1（原先真实仓位数）", message)
        self.assertIn("Trader：BTCUSDT[LONG] -50.00%", send_alert.call_args.args[1][0]["subject"])
        self.assertTrue(send_alert.call_args.args[1][0]["autoClosed"])

    def test_position_increase_does_not_close_the_followed_real_position(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1"}]}],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2"}]}],
        }
        with patch.object(copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=[{
            "topTraderId": "trader", "symbol": "BTCUSDT", "positionSide": "LONG",
        }]), patch.object(copy_trading.db, "get_user_binance_credentials", return_value={"configured": True}), patch.object(
            copy_trading, "close_futures_position_market"
        ) as close_position, patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        close_position.assert_not_called()
        message = send_alert.call_args.args[1][0]["message"]
        self.assertNotIn("平掉真实仓位", message)

    def test_failed_position_refresh_does_not_close_the_real_position(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1"}]}],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader", "items": [], "positions": [], "error": "仓位：timeout",
            }],
        }
        with patch.object(copy_trading.db, "list_user_binance_smart_money_position_follows", return_value=[{
            "topTraderId": "trader", "symbol": "BTCUSDT", "positionSide": "LONG",
        }]), patch.object(copy_trading.db, "get_user_binance_credentials", return_value={"configured": True}), patch.object(
            copy_trading, "close_futures_position_market"
        ) as close_position, patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        close_position.assert_not_called()
        send_alert.assert_not_called()

    def test_empty_public_history_falls_back_to_position_change(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
                "error": None,
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1.5"}],
                "error": None,
            }],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            delivered = copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        self.assertTrue(delivered)
        self.assertIn("1.5（+50.00%）", send_alert.call_args.args[1][0]["message"])

    def test_notification_includes_recommended_position_value_change(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "sourceTotalMargin": "100",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1", "entryPrice": "10"}],
                "error": None,
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "sourceTotalMargin": "100",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2", "entryPrice": "10"}],
                "error": None,
            }],
        }
        with patch.object(copy_trading, "get_cached_binance_account_snapshot", return_value={"account": {"futures": {"totalMarginBalance": "1000"}}}), patch.object(
            copy_trading.db,
            "get_user_binance_copy_trading_settings",
            return_value={"topTraderId": "trader", "copyMultiplier": 1, "copyMultipliers": {"trader": 1}},
        ), patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        message = send_alert.call_args.args[1][0]["message"]
        self.assertIn("推荐仓位价值 100.00 -> 200.00 USDT", message)

    def test_notification_marks_recommended_value_deviation_for_reliable_real_position(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "sourceTotalMargin": "100",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "1", "entryPrice": "10"}],
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "sourceTotalMargin": "100",
                "items": [],
                "positions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2", "entryPrice": "10"}],
            }],
        }
        with patch.object(copy_trading, "get_cached_binance_account_snapshot", return_value={
            "stale": False,
            "account": {"futures": {"available": True, "totalMarginBalance": "1000", "positions": [
                {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "10", "notional": "1000"},
            ]}},
        }), patch.object(
            copy_trading.db,
            "get_user_binance_copy_trading_settings",
            return_value={"topTraderId": "trader", "copyMultiplier": 1, "copyMultipliers": {"trader": 1}},
        ), patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)

        change = send_alert.call_args.args[1][0]
        self.assertTrue(change["valueDeviation"])

    def test_new_operation_also_reports_a_full_position_close(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "BUY", "time": 1}],
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
                "error": None,
            }],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{
                "topTraderId": "trader",
                "items": [{"symbol": "BTCUSDT", "side": "SELL", "time": 2}],
                "positions": [],
                "error": None,
            }],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        message = send_alert.call_args.args[1][0]["message"]
        self.assertIn("0（-100.00%）", message)

    def test_position_fallback_ignores_failed_position_response(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}], "error": None}],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [], "error": "仓位：timeout"}],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert") as send_alert:
            delivered = copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        self.assertTrue(delivered)
        send_alert.assert_not_called()

    def test_notification_failure_keeps_position_change_retryable(self):
        previous = {
            "updatedAt": 1,
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}]}],
        }
        current = {
            "subscriptions": [{"topTraderId": "trader", "traderName": "Trader"}],
            "traderSnapshots": [{"topTraderId": "trader", "items": [], "positions": [{"symbol": "BTCUSDT", "positionAmt": "2"}]}],
        }
        with patch.object(copy_trading, "send_resend_smart_money_alert", side_effect=RuntimeError("delivery failed")):
            delivered = copy_trading._notify_smart_money_changes(7, "user@example.com", previous, current)
        self.assertTrue(delivered)
        self.assertEqual(len(copy_trading._pending_notifications_by_user[7]), 1)

    def test_hidden_operation_notification_rounds_relative_position_change(self):
        self.assertEqual(copy_trading._position_quantity_change_percent("3", "2"), "（-33.33%）")
        self.assertEqual(copy_trading._position_quantity_change_percent("-1", "-2"), "（+100.00%）")
        self.assertEqual(copy_trading._position_quantity_change_percent("0", "1"), "（+100.00%）")

    def test_notification_email_is_per_user_and_independent_of_copy_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_path = os.environ.get("CHANLUN_DB_PATH")
            os.environ["CHANLUN_DB_PATH"] = str(Path(directory) / "settings.db")
            try:
                db.init_db()
                user = db.register_user("copy-alert-user", "secure-password")
                settings = db.save_user_notification_settings(user["id"], {"email": "user@example.com"})
                self.assertEqual(settings["email"], "user@example.com")
                db.save_user_binance_copy_trading_settings(
                    user["id"],
                    {"topTraderId": "456", "copyMultiplier": 2},
                )
                self.assertEqual(db.get_user_notification_settings(user["id"])["email"], "user@example.com")
            finally:
                if previous_path is None:
                    os.environ.pop("CHANLUN_DB_PATH", None)
                else:
                    os.environ["CHANLUN_DB_PATH"] = previous_path

    def test_copy_multiplier_is_independent_for_each_smart_money_user(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_path = os.environ.get("CHANLUN_DB_PATH")
            os.environ["CHANLUN_DB_PATH"] = str(Path(directory) / "multipliers.db")
            try:
                db.init_db()
                user = db.register_user("copy-multiplier-user", "secure-password")
                first = db.save_user_binance_copy_trading_settings(
                    user["id"],
                    {"topTraderId": "111", "copyMultiplier": 2},
                )
                self.assertEqual(first["copyMultiplier"], 2)
                self.assertEqual(first["copyMultipliers"], {"111": 2})

                second = db.save_user_binance_copy_trading_settings(user["id"], {"topTraderId": "222"})
                self.assertEqual(second["copyMultiplier"], 1)
                self.assertEqual(second["copyMultipliers"], {"111": 2, "222": 1})

                db.save_user_binance_copy_trading_settings(
                    user["id"],
                    {"topTraderId": "222", "copyMultiplier": 3},
                )
                restored = db.save_user_binance_copy_trading_settings(user["id"], {"topTraderId": "111"})
                self.assertEqual(restored["copyMultiplier"], 2)

                db.ensure_user_binance_smart_money_trader_settings(user["id"], ["333"])
                multipliers = db.get_user_binance_copy_trading_settings(user["id"])["copyMultipliers"]
                self.assertEqual(multipliers, {"111": 2, "222": 3, "333": 1})
            finally:
                if previous_path is None:
                    os.environ.pop("CHANLUN_DB_PATH", None)
                else:
                    os.environ["CHANLUN_DB_PATH"] = previous_path


if __name__ == "__main__":
    unittest.main()
