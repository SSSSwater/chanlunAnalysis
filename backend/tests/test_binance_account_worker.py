import unittest
from unittest.mock import patch

from backend.services import binance_account_worker as worker
from backend.services import binance_futures_client as futures_client


class BinanceAccountWorkerTests(unittest.TestCase):
    def setUp(self):
        with worker._account_snapshot_lock:
            worker._account_snapshots.clear()
            worker._pending_protection_cache_updates.clear()
        with worker._account_refresh_request_lock:
            worker._requested_account_refreshes.clear()
            worker._forced_account_refreshes.clear()
            worker._last_account_refresh_request_at.clear()

    def tearDown(self):
        with worker._account_snapshot_lock:
            worker._account_snapshots.clear()
            worker._pending_protection_cache_updates.clear()
        with worker._account_refresh_request_lock:
            worker._requested_account_refreshes.clear()
            worker._forced_account_refreshes.clear()
            worker._last_account_refresh_request_at.clear()

    def test_healthy_websocket_reconciles_protection_without_full_account_refresh(self):
        worker.cache_binance_account_snapshot(
            7,
            {"futures": {"available": True, "positions": []}},
            {"configured": True},
        )

        with patch.object(
            worker.db,
            "list_user_binance_credentials_for_refresh",
            return_value=[{"userId": 7, "connectionMode": "WEBSOCKET"}],
        ), patch.object(
            worker,
            "_websocket_account_line_is_healthy",
            return_value=True,
        ), patch.object(
            worker,
            "_wake_websocket_account_line",
            return_value=True,
        ) as wake, patch.object(worker, "_refresh_one_account_protection", return_value=True) as protection_refresh, patch.object(
            worker, "_refresh_one_account"
        ) as rest_refresh:
            refreshed = worker.refresh_binance_accounts_once()

        wake.assert_called_once_with(7)
        protection_refresh.assert_called_once()
        rest_refresh.assert_not_called()
        self.assertEqual(refreshed, 0)

    def test_protection_state_forces_rest_when_account_mode_is_websocket(self):
        transports = []

        def read_algo(*_args):
            transports.append(futures_client._use_websocket_api.get())
            return []

        def read_orders(*_args):
            transports.append(futures_client._use_websocket_api.get())
            return []

        with patch.object(futures_client, "_get_open_algo_orders", side_effect=read_algo), patch.object(
            futures_client, "_get_open_futures_orders", side_effect=read_orders
        ):
            with futures_client.websocket_api_requests(True):
                result = futures_client.get_futures_protection_state("mainnet", "key", "secret", [])

        self.assertEqual(result["protectionState"], "CONFIRMED")
        self.assertEqual(transports, [False, False])

    def test_protection_reconciliation_clears_a_deleted_stop_from_cache(self):
        worker.cache_binance_account_snapshot(
            7,
            {
                "futures": {
                    "available": True,
                    "positions": [{
                        "symbol": "BTCUSDT",
                        "positionSide": "BOTH",
                        "side": "LONG",
                        "quantity": 1,
                        "entryPrice": 100,
                        "positionStopLoss": 98,
                        "stopLoss": 98,
                        "protectionState": "UNKNOWN",
                    }],
                }
            },
            {"configured": True},
            transport="WEBSOCKET",
        )
        credentials = {"userId": 7, "network": "mainnet", "apiKey": "key", "apiSecret": "secret"}
        deleted_stop = {
            "positions": [{
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "side": "LONG",
                "quantity": 1,
                "entryPrice": 100,
                "positionStopLoss": None,
                "stopLoss": None,
                "protectionOrders": [],
                "partialTakeProfitLevels": [],
                "protectionState": "CONFIRMED",
            }],
            "openOrderCount": 0,
            "openStandardOrderCount": 0,
            "openAlgoOrderCount": 0,
            "protectionState": "CONFIRMED",
            "protectionCheckedAt": 123,
        }

        with patch.object(worker, "get_futures_protection_state", return_value=deleted_stop), patch.object(
            worker.db,
            "get_user_binance_credentials",
            return_value={"configured": True},
        ) as get_profile, patch.object(worker, "websocket_api_requests") as transport:
            self.assertTrue(worker._refresh_one_account_protection(credentials))

        cached = worker.get_cached_binance_account_snapshot(7)
        position = cached["account"]["futures"]["positions"][0]
        self.assertIsNone(position["positionStopLoss"])
        self.assertIsNone(position["stopLoss"])
        self.assertEqual(position["protectionState"], "CONFIRMED")
        self.assertEqual(cached["account"]["futures"]["protectionState"], "CONFIRMED")
        get_profile.assert_called_once_with(7, include_secrets=False)
        transport.assert_called_once_with(False)

    def test_authoritative_deleted_stop_overrides_pending_success_cache(self):
        initial_account = {
            "futures": {
                "available": True,
                "positions": [{
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "side": "LONG",
                    "quantity": 1,
                    "entryPrice": 100,
                    "positionStopLoss": 97,
                    "stopLoss": 97,
                }],
            },
        }
        worker.cache_binance_account_snapshot(7, initial_account, {"configured": True})
        worker.update_cached_binance_futures_position_protection(7, "BTCUSDT", "BOTH", stop_loss=98)

        deleted_stop = {
            "positions": [{
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "side": "LONG",
                "quantity": 1,
                "entryPrice": 100,
                "positionStopLoss": None,
                "stopLoss": None,
                "protectionOrders": [],
                "partialTakeProfitLevels": [],
                "protectionState": "CONFIRMED",
            }],
            "openOrderCount": 0,
            "openStandardOrderCount": 0,
            "openAlgoOrderCount": 0,
            "protectionState": "CONFIRMED",
            "protectionCheckedAt": 456,
        }
        credentials = {"userId": 7, "network": "mainnet", "apiKey": "key", "apiSecret": "secret"}

        with patch.object(worker, "get_futures_protection_state", return_value=deleted_stop), patch.object(
            worker.db,
            "get_user_binance_credentials",
            return_value={"configured": True},
        ):
            self.assertTrue(worker._refresh_one_account_protection(credentials))

        cached = worker.get_cached_binance_account_snapshot(7)
        position = cached["account"]["futures"]["positions"][0]
        self.assertIsNone(position["positionStopLoss"])
        self.assertIsNone(position["stopLoss"])
        self.assertEqual(worker._pending_protection_cache_updates, {})

    def test_websocket_snapshot_keeps_optimistic_stop_until_rest_reconciliation(self):
        worker.cache_binance_account_snapshot(
            7,
            {
                "futures": {
                    "available": True,
                    "positions": [{
                        "symbol": "BTCUSDT",
                        "positionSide": "BOTH",
                        "side": "LONG",
                        "quantity": 1,
                        "entryPrice": 100,
                        "positionStopLoss": 97,
                        "stopLoss": 97,
                        "protectionOrders": [],
                    }],
                },
            },
            {"configured": True},
        )
        worker.update_cached_binance_futures_position_protection(7, "BTCUSDT", "BOTH", stop_loss=98)

        # account.status omits protection fields and marks them UNKNOWN.  The
        # pending successful mutation must still be visible to the monitor.
        worker.cache_binance_websocket_account_snapshot(
            7,
            {
                "available": True,
                "positions": [{
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "side": "LONG",
                    "positionAmt": "1",
                    "entryPrice": "100",
                }],
            },
            {"configured": True},
        )

        cached = worker.get_cached_binance_account_snapshot(7)
        position = cached["account"]["futures"]["positions"][0]
        self.assertEqual(position["positionStopLoss"], 98.0)
        self.assertEqual(position["stopLoss"], 98.0)
        self.assertEqual(position["protectionState"], "UNKNOWN")
        self.assertEqual(worker._pending_protection_cache_updates, {})

    def test_forced_refresh_uses_rest_even_when_websocket_is_healthy(self):
        worker.request_binance_account_refresh(7, force_rest=True)

        with patch.object(
            worker.db,
            "get_user_binance_connection_settings",
            return_value={"connectionMode": "WEBSOCKET"},
        ), patch.object(worker, "_websocket_account_line_is_healthy", return_value=True), patch.object(
            worker, "_wake_websocket_account_line", return_value=True
        ) as wake, patch.object(
            worker.db,
            "get_user_binance_credentials",
            return_value={"configured": True, "network": "mainnet", "apiKey": "key", "apiSecret": "secret"},
        ), patch.object(worker, "_refresh_one_account", return_value=True) as rest_refresh:
            refreshed = worker.refresh_requested_binance_accounts_once()

        wake.assert_called_once_with(7)
        rest_refresh.assert_called_once_with(
            {"userId": 7, "network": "mainnet", "apiKey": "key", "apiSecret": "secret"}
        )
        self.assertEqual(refreshed, 1)


if __name__ == "__main__":
    unittest.main()
