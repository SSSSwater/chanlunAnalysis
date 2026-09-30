from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend import app as app_module
from backend.services.binance_client import BinanceApiError
from backend.services import binance_account_worker as account_worker
from backend.services import binance_snapshot_worker as snapshot_worker


class BinanceSnapshotWorkerTests(unittest.TestCase):
    def setUp(self):
        snapshot_worker._market_snapshots.clear()
        snapshot_worker._public_market_next_due.clear()
        snapshot_worker._kline_snapshots.clear()
        snapshot_worker._live_ticker_snapshots.clear()
        snapshot_worker._kline_bootstrap_attempts.clear()
        snapshot_worker._monitor_targets.clear()
        snapshot_worker._poll_monitor_targets.clear()
        snapshot_worker._poll_monitor_last_seen.clear()
        account_worker._account_snapshots.clear()
        account_worker._pending_protection_cache_updates.clear()
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        snapshot_worker._market_snapshots.clear()
        snapshot_worker._public_market_next_due.clear()
        snapshot_worker._kline_snapshots.clear()
        snapshot_worker._live_ticker_snapshots.clear()
        snapshot_worker._kline_bootstrap_attempts.clear()
        snapshot_worker._monitor_targets.clear()
        snapshot_worker._poll_monitor_targets.clear()
        snapshot_worker._poll_monitor_last_seen.clear()
        account_worker._account_snapshots.clear()
        account_worker._pending_protection_cache_updates.clear()

    def test_public_markets_are_refreshed_by_backend_and_limited_to_100(self):
        spot = {"items": [{"symbol": "BTCUSDT"}], "limit": 100, "stale": False, "updatedAt": 123}
        futures = {"items": [{"symbol": "ETHUSDT"}], "limit": 100, "stale": False, "updatedAt": 124}
        # Spot remains available for legacy callers, but is refreshed only
        # when an explicit spot monitor target exists.
        snapshot_worker.upsert_poll_monitor_target("test-public", "mainnet", "SPOT", "BTCUSDT", "1h")
        with patch.object(snapshot_worker, "get_spot_markets", return_value=spot) as spot_loader, patch.object(
            snapshot_worker, "get_futures_markets", return_value=futures
        ) as futures_loader:
            snapshot_worker.refresh_public_markets_once()

        spot_loader.assert_called_once_with("mainnet", 100)
        futures_loader.assert_called_once_with("mainnet", 100)
        subscription = snapshot_worker.upsert_poll_monitor_target("test-public", "mainnet", "SPOT", "BTCUSDT", "1h")
        result = snapshot_worker.get_snapshot(subscription)
        self.assertEqual(result["markets"]["spot"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(result["markets"]["futures"]["items"][0]["symbol"], "ETHUSDT")

    def test_public_market_refresh_defaults_to_futures_only(self):
        futures = {"items": [{"symbol": "ETHUSDT"}], "limit": 100, "stale": False}
        with patch.object(snapshot_worker, "get_spot_markets") as spot_loader, patch.object(
            snapshot_worker, "get_futures_markets", return_value=futures
        ) as futures_loader:
            snapshot_worker.refresh_public_markets_once()

        spot_loader.assert_not_called()
        futures_loader.assert_called_once_with("mainnet", 100)

    def test_real_positions_receive_exact_ticker_and_mark_price_streams(self):
        account_worker.cache_binance_account_snapshot(
            7,
            {"futures": {"positions": [{"symbol": "INTCUSDT", "positionSide": "BOTH"}]}},
            {"configured": True, "network": "mainnet"},
        )
        self.assertEqual(
            snapshot_worker.get_account_position_market_price_targets({7}),
            [("mainnet", "FUTURES", "INTCUSDT")],
        )
        snapshot_worker.cache_live_ticker(
            "mainnet",
            "FUTURES",
            "INTCUSDT",
            {"lastPrice": "124.01", "markPrice": "123.99"},
        )

        result = snapshot_worker.get_snapshot_without_target("mainnet", "FUTURES", "1h", 7)
        position = result["account"]["account"]["futures"]["positions"][0]
        self.assertEqual(position["lastPrice"], "124.01")
        self.assertEqual(position["markPrice"], "123.99")

    def test_failed_public_market_refresh_is_scheduled_for_a_short_retry(self):
        snapshot_worker.upsert_poll_monitor_target("test-retry", "mainnet", "SPOT", "BTCUSDT", "1h")
        with patch.object(snapshot_worker, "get_spot_markets", side_effect=BinanceApiError("temporary")) as spot_loader, patch.object(
            snapshot_worker, "get_futures_markets", return_value={"items": [], "limit": 100, "stale": False}
        ):
            snapshot_worker.refresh_public_markets_once(force=False)
            snapshot_worker.refresh_public_markets_once(force=False)

        spot_loader.assert_called_once_with("mainnet", 100)
        self.assertGreater(snapshot_worker._public_market_next_due["SPOT"], 0)

    def test_monitored_klines_are_cached_and_snapshot_read_does_not_call_exchange(self):
        spot_klines = {"items": [{"open": "1", "high": "2", "low": "1", "close": "2"}], "stale": False}
        subscription = snapshot_worker.upsert_poll_monitor_target("test-kline", "mainnet", "SPOT", "BTCUSDT", "15m")
        with patch.object(snapshot_worker, "get_spot_klines", return_value=spot_klines) as loader:
            snapshot_worker.refresh_monitored_klines_once()
        loader.assert_called_once_with("mainnet", "BTCUSDT", "15m", 180, with_meta=True)

        with patch.object(snapshot_worker, "get_spot_klines", side_effect=AssertionError("snapshot read fetched exchange")):
            result = snapshot_worker.get_snapshot(subscription)
        self.assertEqual(result["selected"]["klines"], spot_klines["items"])

    def test_monitored_five_minute_klines_are_available_for_spot_and_futures(self):
        spot_klines = {"items": [{"close": "100"}], "stale": False}
        futures_klines = {"items": [{"close": "101"}], "stale": False}
        spot_subscription = snapshot_worker.upsert_poll_monitor_target("test-spot-5m", "mainnet", "SPOT", "BTCUSDT", "5m")
        futures_subscription = snapshot_worker.upsert_poll_monitor_target("test-futures-5m", "mainnet", "FUTURES", "ETHUSDT", "5m")

        with patch.object(snapshot_worker, "get_spot_klines", return_value=spot_klines) as spot_loader, patch.object(
            snapshot_worker, "get_futures_klines", return_value=futures_klines
        ) as futures_loader:
            snapshot_worker.refresh_monitored_klines_once()

        spot_loader.assert_called_once_with("mainnet", "BTCUSDT", "5m", 180, with_meta=True)
        futures_loader.assert_called_once_with("mainnet", "ETHUSDT", "5m", 180, with_meta=True)
        self.assertEqual(snapshot_worker.get_snapshot(spot_subscription)["selected"]["klines"], spot_klines["items"])
        self.assertEqual(snapshot_worker.get_snapshot(futures_subscription)["selected"]["klines"], futures_klines["items"])

    def test_websocket_mode_still_bootstraps_chart_history_over_rest(self):
        """Live transport selection must not reuse the old local backtest corpus."""

        subscription = snapshot_worker.upsert_poll_monitor_target(
            "test-websocket-history", "mainnet", "FUTURES", "BTCUSDT", "15m", owner_user_id=7
        )
        rest_klines = {
            "items": [{"openTime": 1789675200000, "close": "101"}],
            "stale": False,
        }
        with patch.object(
            snapshot_worker.db,
            "get_user_binance_connection_settings",
            return_value={"connectionMode": "WEBSOCKET"},
        ), patch.object(
            snapshot_worker.db,
            "list_latest_binance_futures_history_klines",
            side_effect=AssertionError("live chart must not use local history"),
        ), patch.object(snapshot_worker, "get_futures_klines", return_value=rest_klines) as loader:
            snapshot_worker.refresh_monitored_klines_once(force=False)

        loader.assert_called_once_with("mainnet", "BTCUSDT", "15m", 180, with_meta=True)
        self.assertEqual(snapshot_worker.get_snapshot(subscription)["selected"]["klines"], rest_klines["items"])

    def test_unicode_futures_symbol_can_create_a_chart_subscription(self):
        subscription = snapshot_worker.upsert_poll_monitor_target(
            "test-unicode-futures", "mainnet", "FUTURES", "测试USDT", "5m"
        )
        klines = {"items": [{"close": "101"}], "stale": False}

        with patch.object(snapshot_worker, "get_futures_klines", return_value=klines) as loader:
            snapshot_worker.refresh_monitored_klines_once()

        loader.assert_called_once_with("mainnet", "测试USDT", "5m", 180, with_meta=True)
        self.assertEqual(snapshot_worker.get_snapshot(subscription)["selected"]["klines"], klines["items"])

    def test_websocket_market_targets_do_not_expand_execution_plans(self):
        snapshot_worker.upsert_poll_monitor_target("test-chart", "mainnet", "FUTURES", "BTCUSDT", "15m", owner_user_id=7)
        execution = {
            "network": "mainnet",
            "marketMode": "FUTURES",
            "symbol": "ETHUSDT",
            "executionStatus": "EXECUTING",
        }
        with patch.object(
            snapshot_worker.db,
            "list_binance_simulated_positions_for_refresh",
            return_value=[(7, execution)],
        ):
            self.assertEqual(
                snapshot_worker.get_market_stream_targets({7}),
                [("mainnet", "FUTURES", "BTCUSDT", "15m")],
            )
            self.assertEqual(
                snapshot_worker.get_execution_market_price_targets({7}),
                [("mainnet", "FUTURES", "ETHUSDT")],
            )

    def test_kline_bootstrap_does_not_repeat_every_second_after_a_failed_short_seed(self):
        snapshot_worker.upsert_poll_monitor_target("test-bootstrap", "mainnet", "SPOT", "BTCUSDT", "15m")
        short_seed = {"items": [{"openTime": 1, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}], "stale": False}
        with patch.object(snapshot_worker, "get_spot_klines", return_value=short_seed) as loader:
            snapshot_worker.refresh_monitored_klines_once(force=False)
            snapshot_worker.refresh_monitored_klines_once(force=False)
        loader.assert_called_once_with("mainnet", "BTCUSDT", "15m", 180, with_meta=True)

    def test_account_refresh_is_user_scoped_and_cache_excludes_secrets(self):
        account = {"futures": {"positions": [{"symbol": "BTCUSDT"}]}}
        profile = {
            "configured": True,
            "network": "mainnet",
            "apiKeyMasked": "publ...-key",
            "updatedAt": "now",
            "requiresReconfiguration": False,
        }
        credentials = [{"userId": 7, "network": "mainnet", "apiKey": "public", "apiSecret": "private"}]
        with patch.object(account_worker.db, "list_user_binance_credentials_for_refresh", return_value=credentials), patch.object(
            account_worker.db, "get_user_binance_credentials", return_value=profile
        ), patch.object(account_worker, "get_binance_account_snapshot", return_value=account):
            self.assertEqual(account_worker.refresh_binance_accounts_once(), 1)

        cached = account_worker.get_cached_binance_account_snapshot(7)
        self.assertEqual(cached["account"], account)
        self.assertNotIn("apiKey", cached["credentials"])
        self.assertNotIn("apiSecret", cached["credentials"])
        self.assertNotIn("private", json.dumps(cached))

    def test_spot_account_failure_does_not_hide_futures_response(self):
        futures = {
            "available": True,
            "assets": [{"asset": "USDT", "walletBalance": 5}],
            "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
        }
        with patch.object(
            account_worker,
            "get_binance_account",
            side_effect=BinanceApiError("spot request failed"),
        ), patch.object(account_worker, "get_futures_account", return_value=futures):
            result = account_worker.get_binance_account_snapshot("mainnet", "public", "private")

        self.assertFalse(result["spotAvailable"])
        self.assertTrue(result["futures"]["available"])
        self.assertEqual(result["futures"]["positions"], futures["positions"])

    def test_stale_account_snapshot_keeps_last_success_time_and_records_check_time(self):
        account_worker.cache_binance_account_snapshot(
            7,
            {"futures": {"availableBalance": 1}},
            {"configured": True, "apiKeyMasked": "publ...-key"},
        )
        first = account_worker.get_cached_binance_account_snapshot(7)

        account_worker.cache_binance_account_snapshot(
            7,
            None,
            {"configured": True, "apiKeyMasked": "publ...-key"},
            stale=True,
            error="暂时无法连接",
        )
        stale = account_worker.get_cached_binance_account_snapshot(7)

        self.assertEqual(stale["updatedAt"], first["updatedAt"])
        self.assertGreaterEqual(stale["checkedAt"], first["checkedAt"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["account"], first["account"])

    def test_old_account_read_does_not_rollback_a_successful_stop_update(self):
        initial_account = {
            "futures": {
                "available": True,
                "positions": [{
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "side": "LONG",
                    "positionStopLoss": 97.0,
                }],
            },
        }
        account_worker.cache_binance_account_snapshot(7, initial_account, {"configured": True})
        account_worker.update_cached_binance_futures_position_protection(
            7,
            "BTCUSDT",
            "BOTH",
            stop_loss=98.0,
        )

        # Simulate the response of a REST request that started before the
        # successful protection mutation was acknowledged.
        account_worker.cache_binance_account_snapshot(
            7,
            initial_account,
            {"configured": True},
        )

        cached = account_worker.get_cached_binance_account_snapshot(7)
        position = cached["account"]["futures"]["positions"][0]
        self.assertEqual(position["positionStopLoss"], 98.0)
        self.assertEqual(position["stopLoss"], 98.0)

    def test_partial_futures_account_failure_keeps_last_data_but_marks_snapshot_stale(self):
        previous_account = {"futures": {"available": True, "positions": [{"symbol": "BTCUSDT"}]}}
        current_account = {
            "futures": {"available": False, "positions": [], "error": "合约账户暂时不可用"},
            "balances": [{"asset": "USDT", "free": 2}],
        }
        profile = {"configured": True, "apiKeyMasked": "publ...-key"}
        credentials = [{"userId": 7, "network": "mainnet", "apiKey": "public", "apiSecret": "private"}]
        account_worker.cache_binance_account_snapshot(7, previous_account, profile)
        with patch.object(account_worker.db, "list_user_binance_credentials_for_refresh", return_value=credentials), patch.object(
            account_worker.db, "get_user_binance_credentials", return_value=profile
        ), patch.object(account_worker, "get_binance_account_snapshot", return_value=current_account):
            self.assertEqual(account_worker.refresh_binance_accounts_once(), 0)

        cached = account_worker.get_cached_binance_account_snapshot(7)
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["account"]["futures"]["positions"], [{"symbol": "BTCUSDT"}])
        self.assertEqual(cached["account"]["balances"], current_account["balances"])
        self.assertIn("合约账户", cached["error"])

    def test_snapshot_poll_disables_http_caching(self):
        response = self.client.get(
            "/api/binance/snapshot?market=SPOT&symbol=BTCUSDT&interval=1h&clientId=cache-check"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Pragma"], "no-cache")

    def test_snapshot_poll_reads_cache_and_reuses_one_monitor_target(self):
        snapshot_worker._set_market_snapshot(
            "mainnet", "SPOT", {"items": [{"symbol": "BTCUSDT", "lastPrice": "100"}], "limit": 100, "stale": False}
        )
        with patch.object(snapshot_worker, "get_spot_klines", side_effect=AssertionError("poll read fetched exchange")):
            first = self.client.get(
                "/api/binance/snapshot?market=SPOT&symbol=BTCUSDT&interval=1h&clientId=browser-a"
            )
            second = self.client.get(
                "/api/binance/snapshot?market=SPOT&symbol=BTCUSDT&interval=1h&clientId=browser-a"
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()["markets"]["spot"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(len(snapshot_worker._poll_monitor_targets), 1)

    def test_snapshot_projects_account_position_values_into_monitor(self):
        account = {
            "account": {
                "futures": {
                    "positions": [{
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "positionSide": "BOTH",
                        "quantity": 0.25,
                        "entryPrice": 100.0,
                        "lastPrice": 101.2,
                        "markPrice": 101.1,
                        "unrealizedProfit": 0.275,
                        "roePercent": 4.4,
                    }],
                },
            },
        }
        simulated = {
            "items": [{
                "id": 7,
                "symbol": "BTCUSDT",
                "side": "LONG",
                "marketMode": "FUTURES",
                "executionStatus": "EXECUTING",
                "currentPrice": 99.0,
                "unrealizedPnl": -0.25,
                "plan": {"currentPrice": 99.0, "latestPrice": 99.0},
            }],
            "updatedAt": 1,
        }

        projected = snapshot_worker._project_account_positions_into_monitors(account, simulated)
        item = projected["items"][0]
        self.assertEqual(item["currentPrice"], 101.1)
        self.assertEqual(item["unrealizedPnl"], 0.275)
        self.assertEqual(item["quantity"], 0.25)
        self.assertEqual(item["plan"]["latestPrice"], 101.2)

    def test_kline_rest_fallback_only_refreshes_stale_targets(self):
        snapshot_worker.upsert_poll_monitor_target("test-fresh", "mainnet", "SPOT", "BTCUSDT", "1h")
        snapshot_worker.upsert_poll_monitor_target("test-stale", "mainnet", "SPOT", "ETHUSDT", "1h")
        snapshot_worker._set_kline_snapshot(
            ("mainnet", "SPOT", "BTCUSDT", "1h"),
            {"items": [{"openTime": 1, "close": "100"}], "stale": False},
        )
        snapshot_worker._set_kline_snapshot(
            ("mainnet", "SPOT", "ETHUSDT", "1h"),
            error="行情暂时不可用",
        )

        with patch.object(
            snapshot_worker,
            "get_spot_klines",
            return_value={"items": [{"openTime": 2, "close": "200"}], "stale": False},
        ) as loader:
            snapshot_worker.refresh_monitored_klines_once(only_stale=True)

        loader.assert_called_once_with("mainnet", "ETHUSDT", "1h", 180, with_meta=True)


if __name__ == "__main__":
    unittest.main()
