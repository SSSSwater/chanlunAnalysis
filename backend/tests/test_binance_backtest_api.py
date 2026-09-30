import unittest
from contextlib import contextmanager
from unittest.mock import patch

from backend import app as app_module
from backend.services import binance_simulated_portfolio as simulated_portfolio


class BinanceBacktestApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_history_status_reads_the_strict_cache_summary(self):
        expected = {
            "requiredSeries": 10,
            "completeSeries": 4,
            "percent": 40,
            "intervals": [],
        }
        with patch.object(app_module, "get_fixed_history_completeness", return_value=expected) as status:
            response = self.client.get("/api/binance/futures/backtest/history-status?network=mainnet")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": expected})
        status.assert_called_once_with("mainnet")

    def test_copy_trading_market_order_is_confirmed_before_monitor_creation(self):
        calls = []
        credentials = {
            "configured": True,
            "network": "mainnet",
            "apiKey": "key",
            "apiSecret": "secret",
        }

        def place_order(*args, **kwargs):
            calls.append("order")
            return {
                "entryAccepted": True,
                "symbol": kwargs["symbol"],
                "quantity": kwargs["quantity"],
                "positionSide": "BOTH",
                "entryPrice": 100,
            }

        def save_monitor(*args, **kwargs):
            calls.append("monitor")
            return {"id": 44}

        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db, "get_user_binance_credentials", return_value=credentials
        ), patch.object(app_module, "place_futures_market_entry_order", side_effect=place_order), patch.object(
            app_module, "save_binance_simulated_position", side_effect=save_monitor
        ), patch.object(app_module, "_request_current_user_account_reconciliation"), patch.object(
            app_module, "notify_snapshot_update"
        ):
            response = self.client.post(
                "/api/binance/futures/copy-trading/order",
                json={
                    "orderType": "MARKET",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "quantity": 1,
                    "costPrice": 100,
                    "leverage": 1,
                    "stopLoss": 95,
                    "firstTakeProfit": 106,
                },
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(calls, ["order"])
        self.assertIsNone(response.get_json()["monitor"])

    def test_copy_trading_limit_order_is_confirmed_before_monitor_creation(self):
        calls = []
        credentials = {
            "configured": True,
            "network": "mainnet",
            "apiKey": "key",
            "apiSecret": "secret",
        }

        def place_order(*args, **kwargs):
            calls.append("order")
            return {
                "entryAccepted": True,
                "symbol": kwargs["symbol"],
                "quantity": kwargs["quantity"],
                "entryOrder": {"orderId": "entry-1"},
            }

        def save_monitor(*args, **kwargs):
            calls.append("monitor")
            return {"id": 45}

        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db, "get_user_binance_credentials", return_value=credentials
        ), patch.object(app_module, "place_futures_limit_plan_order", side_effect=place_order), patch.object(
            app_module, "save_binance_simulated_position", side_effect=save_monitor
        ), patch.object(app_module, "_request_current_user_account_reconciliation"), patch.object(
            app_module, "notify_snapshot_update"
        ):
            response = self.client.post(
                "/api/binance/futures/copy-trading/order",
                json={
                    "orderType": "LIMIT",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "quantity": 1,
                    "costPrice": 100,
                    "leverage": 1,
                    "stopLoss": 95,
                    "firstTakeProfit": 106,
                },
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(calls, ["order"])
        self.assertIsNone(response.get_json()["monitor"])

    def test_existing_position_monitor_only_requires_protection_levels(self):
        normalized = simulated_portfolio._normalize_position(
            {
                "network": "mainnet",
                "marketMode": "FUTURES",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quantity": 1,
                "costPrice": 100,
                "leverage": 3,
                "existingPositionMonitor": True,
                "plan": {
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "conditionMet": 0,
                    "conditionTotal": 10,
                    "status": "WAIT",
                    "stopLoss": 95,
                    "takeProfits": [{"role": "FIRST_TARGET", "price": 106}],
                },
            },
            require_plan=True,
        )

        self.assertEqual(normalized["executionStatus"], "EXECUTING")
        self.assertEqual(normalized["plan"]["stopLoss"], 95)

    def test_history_fill_starts_without_starting_a_backtest(self):
        expected = {"id": "fill-job", "kind": "HISTORY_FILL", "status": "QUEUED"}
        with patch.object(app_module, "start_fixed_history_fill", return_value=expected) as start:
            response = self.client.post("/api/binance/futures/backtest/history-fill", json={"network": "mainnet"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job": expected})
        start.assert_called_once_with("mainnet")

    def test_history_fill_status_reads_only_history_fill_jobs(self):
        expected = {"id": "fill-job", "kind": "HISTORY_FILL", "status": "RUNNING"}
        with patch.object(app_module, "get_fixed_history_fill", return_value=expected) as status:
            response = self.client.get("/api/binance/futures/backtest/history-fill/fill-job")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"job": expected})
        status.assert_called_once_with("fill-job")

    def test_backtest_uses_the_authenticated_users_strategy_settings_snapshot(self):
        expected = {"id": "backtest-job", "status": "QUEUED"}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module,
            "start_current_strategy_backtest",
            return_value=expected,
        ) as start:
            response = self.client.post(
                "/api/binance/futures/backtest",
                json={"network": "mainnet"},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job": expected})
        start.assert_called_once_with("mainnet", user_id=17)

    def test_backtest_passes_the_requested_window_to_the_job(self):
        expected = {"id": "backtest-job", "status": "QUEUED"}
        options = {"mode": "RANGE", "startTime": "2026-08-15T00:00:00Z", "endTime": "2026-08-16T00:00:00Z"}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module,
            "start_current_strategy_backtest",
            return_value=expected,
        ) as start:
            response = self.client.post(
                "/api/binance/futures/backtest",
                json={"network": "mainnet", "backtestOptions": options},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job": expected})
        start.assert_called_once_with("mainnet", user_id=17, backtest_options=options)

    def test_backtest_passes_explicit_strategy_settings_to_the_job(self):
        expected = {"id": "backtest-job", "status": "QUEUED"}
        options = {"mode": "RANGE", "startTime": "2026-08-14T08:00:00Z", "endTime": "2026-09-01T00:00:00Z"}
        strategy = {
            "levelStrategy": "CONFIRMED_PLATFORM",
            "maxAccountLossRatio": 8,
            "trailingAtrMultiplier": 3,
            "structureStopAtrMultiplier": 0.4,
            "breakevenBufferAtrMultiplier": 0.1,
            "movingStopActivationR": 1.5,
            "protectiveTakeProfitRatio": 20,
            "firstTakeProfitRatio": 45,
            "secondTakeProfitRatio": 80,
        }
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module,
            "start_current_strategy_backtest",
            return_value=expected,
        ) as start:
            response = self.client.post(
                "/api/binance/futures/backtest",
                json={"network": "mainnet", "backtestOptions": options, "strategySettings": strategy},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job": expected})
        start.assert_called_once_with("mainnet", user_id=17, backtest_options=options, strategy_settings=strategy)

    def test_limit_plan_ignores_stale_protective_ratio_without_target(self):
        credentials = {
            "configured": True,
            "network": "mainnet",
            "apiKey": "key",
            "apiSecret": "secret",
        }
        plan = {
            "symbol": "TESTUSDT",
            "strategyEngine": "CLASSIC",
            "status": "ARMED",
            "conditionMet": 10,
            "conditionTotal": 10,
            "direction": "LONG",
            "entry": {"trigger": 100.0, "zoneLow": 99.0, "zoneHigh": 101.0},
            "stopLoss": 95.0,
            "takeProfits": [
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
            "strategySettings": {"firstTakeProfitRatio": 50.0, "secondTakeProfitRatio": 75.0},
        }
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_credentials",
            return_value=credentials,
        ), patch.object(
            app_module,
            "create_pending_binance_entry_monitor",
            return_value={"id": 44},
        ), patch.object(
            app_module,
            "place_futures_limit_plan_order",
            return_value={"entryAccepted": True, "entryOrder": {"orderId": "entry-1"}},
        ) as place, patch.object(app_module, "_request_current_user_account_reconciliation"), patch.object(
            app_module, "notify_snapshot_update"
        ):
            response = self.client.post(
                "/api/binance/futures/orders/limit-plan",
                json={
                    "marketMode": "FUTURES",
                    "symbol": "TESTUSDT",
                    "direction": "LONG",
                    "quantity": 1,
                    "costPrice": 100,
                    "leverage": 1,
                    "stopLoss": 95,
                    "firstTakeProfit": 106,
                    "extensionTakeProfit": 110,
                    "firstTakeProfitRatio": 50,
                    "secondTakeProfitRatio": 75,
                    "protectiveTakeProfitRatio": 0,
                    "plan": plan,
                },
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("protective_take_profit_ratio", place.call_args.kwargs)

    def test_real_limit_plan_forces_rest_when_account_mode_is_websocket(self):
        credentials = {
            "configured": True,
            "network": "mainnet",
            "apiKey": "key",
            "apiSecret": "secret",
        }
        plan = {
            "symbol": "币安人生USDT",
            "strategyEngine": "CLASSIC",
            "status": "ARMED",
            "conditionMet": 10,
            "conditionTotal": 10,
            "direction": "LONG",
            "entry": {"trigger": 100.0, "zoneLow": 99.0, "zoneHigh": 101.0},
            "stopLoss": 95.0,
            "takeProfits": [{"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 100.0}],
            "strategySettings": {"firstTakeProfitRatio": 100.0},
        }
        transport_modes = []

        @contextmanager
        def record_transport(enabled):
            transport_modes.append(enabled)
            yield

        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_credentials",
            return_value=credentials,
        ), patch.object(
            app_module,
            "create_pending_binance_entry_monitor",
            return_value={"id": 44},
        ), patch.object(
            app_module,
            "place_futures_limit_plan_order",
            return_value={"entryAccepted": True, "entryOrder": {"orderId": "entry-1"}},
        ), patch.object(
            app_module,
            "websocket_api_requests",
            side_effect=record_transport,
        ), patch.object(app_module, "_request_current_user_account_reconciliation"), patch.object(
            app_module, "notify_snapshot_update"
        ):
            response = self.client.post(
                "/api/binance/futures/orders/limit-plan",
                json={
                    "marketMode": "FUTURES",
                    "symbol": "币安人生USDT",
                    "direction": "LONG",
                    "quantity": 1,
                    "costPrice": 100,
                    "leverage": 1,
                    "stopLoss": 95,
                    "firstTakeProfit": 106,
                    "firstTakeProfitRatio": 100,
                    "plan": plan,
                },
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(transport_modes, [False])

    def test_chinese_futures_symbol_passes_execution_monitor_validation(self):
        normalized = simulated_portfolio._normalize_position({
            "network": "mainnet",
            "marketMode": "FUTURES",
            "symbol": "币安人生USDT",
            "side": "LONG",
            "quantity": 1,
            "costPrice": 100,
            "leverage": 2,
        })

        self.assertEqual(normalized["symbol"], "币安人生USDT")

    def test_real_protection_routes_force_rest_transport(self):
        credentials = {
            "configured": True,
            "network": "mainnet",
            "apiKey": "key",
            "apiSecret": "secret",
        }
        transport_modes = []

        @contextmanager
        def record_transport(enabled):
            transport_modes.append(enabled)
            yield

        headers = {"Authorization": "Bearer authenticated-token"}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_credentials",
            return_value=credentials,
        ), patch.object(
            app_module,
            "websocket_api_requests",
            side_effect=record_transport,
        ), patch.object(
            app_module,
            "update_futures_position_protection",
            return_value={"ok": True},
        ), patch.object(
            app_module,
            "update_futures_partial_protection",
            return_value={"ok": True},
        ), patch.object(
            app_module,
            "apply_futures_plan_protection",
            return_value={"ok": True},
        ), patch.object(app_module, "_request_current_user_account_reconciliation"), patch.object(
            app_module, "notify_snapshot_update"
        ):
            responses = [
                self.client.put(
                    "/api/binance/futures/positions/position-protection",
                    json={"symbol": "币安人生USDT", "stopLoss": 95, "takeProfit": 106},
                    headers=headers,
                ),
                self.client.put(
                    "/api/binance/futures/positions/partial-protection",
                    json={"symbol": "币安人生USDT", "stopLoss": 95, "takeProfit": 106, "quantityRatio": 50},
                    headers=headers,
                ),
                self.client.post(
                    "/api/binance/futures/positions/apply-plan",
                    json={"symbol": "币安人生USDT", "stopLoss": 95, "firstTakeProfit": 106},
                    headers=headers,
                ),
            ]

        self.assertEqual([response.status_code for response in responses], [200, 200, 200])
        self.assertEqual(transport_modes, [False, False, False])

    def test_strategy_settings_endpoints_are_authenticated_and_return_saved_values(self):
        defaults = {
            "levelStrategy": "STRUCTURE_EXTREME",
            "maxAccountLossRatio": 2.0,
            "maxPortfolioRiskRatio": 4.0,
            "maxSameSidePositions": 2,
            "dailyLossLimitRatio": 4.0,
            "rangeEdgeFraction": 0.25,
            "rangeMinimumTargetR": 1.0,
            "trendMinimumTargetR": 1.5,
            "entryConfirmationMode": "TRIGGER_ONLY",
            "entryConfirmationExpiryBars": 3,
            "entryFailureExitBars": 3,
            "entryFailureBodyAtrMultiplier": 0.5,
            "trailingAtrMultiplier": 2.5,
            "structureStopAtrMultiplier": 0.28,
            "breakevenBufferAtrMultiplier": 0.05,
            "movingStopActivationR": 1.0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
            "updatedAt": None,
        }
        saved = {
            **defaults,
            "levelStrategy": "CONFIRMED_PLATFORM",
            "maxAccountLossRatio": 7.5,
            "firstTakeProfitRatio": 40.0,
            "secondTakeProfitRatio": 80.0,
        }
        headers = {"Authorization": "Bearer authenticated-token"}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_strategy_settings",
            return_value=defaults,
        ) as get_settings, patch.object(
            app_module.db,
            "save_user_binance_strategy_settings",
            return_value=saved,
        ) as save_settings:
            get_response = self.client.get("/api/binance/strategy-settings", headers=headers)
            put_response = self.client.put(
                "/api/binance/strategy-settings",
                json={
                    "levelStrategy": "CONFIRMED_PLATFORM",
                    "maxAccountLossRatio": 7.5,
                    "maxPortfolioRiskRatio": 8,
                    "maxSameSidePositions": 3,
                    "dailyLossLimitRatio": 6,
                    "rangeEdgeFraction": 0.2,
                    "rangeMinimumTargetR": 2.5,
                    "trendMinimumTargetR": 2.0,
                    "entryConfirmationMode": "TRIGGER_ONLY",
                    "entryConfirmationExpiryBars": 2,
                    "entryFailureExitBars": 4,
                    "entryFailureBodyAtrMultiplier": 0.8,
                    "firstTakeProfitRatio": 40,
                    "secondTakeProfitRatio": 80,
                },
                headers=headers,
            )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json(), {"settings": defaults})
        get_settings.assert_called_once_with(17)
        self.assertEqual(put_response.status_code, 200)
        self.assertEqual(put_response.get_json(), {"settings": saved})
        save_settings.assert_called_once_with(17, {
            "levelStrategy": "CONFIRMED_PLATFORM",
            "maxAccountLossRatio": 7.5,
            "maxPortfolioRiskRatio": 8,
            "maxSameSidePositions": 3,
            "dailyLossLimitRatio": 6,
            "rangeEdgeFraction": 0.2,
            "rangeMinimumTargetR": 2.5,
            "trendMinimumTargetR": 2.0,
            "entryConfirmationMode": "TRIGGER_ONLY",
            "entryConfirmationExpiryBars": 2,
            "entryFailureExitBars": 4,
            "entryFailureBodyAtrMultiplier": 0.8,
            "firstTakeProfitRatio": 40,
            "secondTakeProfitRatio": 80,
        })
