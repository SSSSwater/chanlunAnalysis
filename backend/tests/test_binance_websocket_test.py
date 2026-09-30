from __future__ import annotations

import json
import hashlib
import hmac
import itertools
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from backend import app as app_module
from backend.services import binance_websocket_test as websocket_test
from backend.services import binance_futures_websocket_api as websocket_api
from backend.services import binance_websocket_transport as websocket_transport
from backend.services.binance_client import BinanceApiError


class FakeWebSocket:
    def __init__(self, message):
        self.messages = list(message) if isinstance(message, list) else [message]
        self.sent = []
        self.connected = []
        self.closed = False
        self.timeout = None

    def connect(self, endpoint, **options):
        self.connected.append((endpoint, options))

    def send_json(self, message):
        self.sent.append(message)

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv_json(self):
        message = self.messages.pop(0) if len(self.messages) > 1 else self.messages[0]
        return json.loads(message) if isinstance(message, str) else message

    def recv(self):
        message = self.messages.pop(0) if len(self.messages) > 1 else self.messages[0]
        return json.dumps(message) if isinstance(message, dict) else message

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        self.closed = True


class BinanceWebSocketProbeTests(unittest.TestCase):
    def test_leverage_change_is_explicitly_rest_only(self):
        with self.assertRaisesRegex(BinanceApiError, "WebSocket 模式不支持合约接口 /fapi/v1/leverage"):
            websocket_api.request(
                "mainnet",
                "/fapi/v1/leverage",
                {"symbol": "BTCUSDT", "leverage": 10},
                method="POST",
                api_key="public-key",
                api_secret="private-secret",
                signed=True,
            )

    def test_position_risk_request_uses_account_position_method_name(self):
        self.assertEqual(
            websocket_api.METHODS[("GET", "/fapi/v2/positionRisk")],
            "account.position",
        )

    @unittest.skipUnless(websocket_transport.websocket_client is not None, "websocket-client is not installed")
    def test_probe_subscribes_to_public_stream_and_closes_immediately(self):
        socket = FakeWebSocket('{"result":null,"id":1}')
        with patch.object(websocket_transport.websocket_client, "WebSocket", return_value=socket) as websocket_class:
            result = websocket_test.test_binance_futures_websocket("mainnet")

        websocket_class.assert_called_once_with()
        self.assertEqual(socket.connected, [
            (
                "wss://fstream.binance.com:443/ws",
                {
                    "timeout": websocket_test.WEBSOCKET_TEST_TIMEOUT_SECONDS,
                    "http_proxy_host": "127.0.0.1",
                    "http_proxy_port": 10808,
                    "proxy_type": "http",
                    "http_no_proxy": [],
                },
            ),
        ])
        self.assertEqual(socket.sent, [{"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade"], "id": 1}])
        self.assertTrue(socket.closed)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["subscriptionAck"], True)
        self.assertIn("p", result["dataFields"])
        self.assertEqual(result["testOnly"], True)

    def test_probe_rejects_invalid_network(self):
        with self.assertRaisesRegex(ValueError, "网络必须选择主网或测试网"):
            websocket_test.test_binance_futures_websocket("invalid")

    @unittest.skipUnless(websocket_transport.websocket_client is not None, "websocket-client is not installed")
    def test_probe_rejects_subscription_error_and_still_closes(self):
        socket = FakeWebSocket('{"code":2,"msg":"Invalid request","id":1}')
        with patch.object(websocket_transport.websocket_client, "WebSocket", return_value=socket):
            with self.assertRaises(BinanceApiError) as raised:
                websocket_test.test_binance_futures_websocket("mainnet")

        self.assertEqual(raised.exception.exchange_code, 2)
        self.assertTrue(socket.closed)

    @unittest.skipUnless(websocket_transport.websocket_client is not None, "websocket-client is not installed")
    def test_account_probe_sends_signed_status_request_and_returns_safe_positions(self):
        socket = FakeWebSocket(
            {
                "id": 1,
                "status": 200,
                "result": {
                    "feeTier": 0,
                    "canTrade": True,
                    "canDeposit": True,
                    "canWithdraw": False,
                    "updateTime": 123,
                    "multiAssetsMargin": False,
                    "totalWalletBalance": "100.1234",
                    "totalUnrealizedProfit": "12.5",
                    "availableBalance": "80.0000",
                    "assets": [
                        {
                            "asset": "USDT",
                            "walletBalance": "100.1234",
                            "unrealizedProfit": "12.5",
                            "availableBalance": "80.0000",
                            "marginAvailable": True,
                            "updateTime": 123,
                        }
                    ],
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0.01",
                            "positionSide": "BOTH",
                            "entryPrice": "60000",
                            "breakEvenPrice": "60010",
                            "initialMargin": "60",
                            "maintMargin": "1.2",
                            "positionInitialMargin": "60",
                            "openOrderInitialMargin": "0",
                            "unRealizedProfit": "12.5",
                            "markPrice": "61250",
                            "liquidationPrice": "50000",
                            "notional": "612.5",
                            "maxNotional": "1000000",
                            "leverage": "10",
                            "marginType": "isolated",
                            "isolated": True,
                            "isolatedMargin": "100",
                            "isolatedWallet": "100",
                            "maxNotionalValue": "1000000",
                            "bidNotional": "0",
                            "askNotional": "0",
                            "adl": 2,
                            "updateTime": 123,
                        },
                        {"symbol": "ETHUSDT", "positionAmt": "0", "positionSide": "BOTH"},
                    ],
                },
            }
        )
        with patch.object(websocket_transport.websocket_client, "WebSocket", return_value=socket) as websocket_class, patch.object(
            websocket_test, "_best_effort_sync_account_clock"
        ):
            result = websocket_test.test_binance_futures_account_websocket("mainnet", "public-key", "private-secret")

        websocket_class.assert_called_once_with()
        self.assertEqual(socket.connected, [
            (
                "wss://ws-fapi.binance.com/ws-fapi/v1",
                {
                    "timeout": websocket_test.WEBSOCKET_TEST_TIMEOUT_SECONDS,
                    "http_proxy_host": "127.0.0.1",
                    "http_proxy_port": 10808,
                    "proxy_type": "http",
                    "http_no_proxy": [],
                },
            ),
        ])
        request = socket.sent[0]
        self.assertEqual(request["id"], 1)
        self.assertEqual(request["method"], "account.status")
        self.assertEqual(request["params"]["apiKey"], "public-key")
        self.assertNotIn("apiSecret", request["params"])
        signature_payload = urlencode(sorted((key, value) for key, value in request["params"].items() if key != "signature"))
        expected_signature = hmac.new(b"private-secret", signature_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(request["params"]["signature"], expected_signature)
        self.assertTrue(socket.closed)
        self.assertEqual(result["positionCount"], 1)
        self.assertEqual(result["rawPositionCount"], 2)
        self.assertEqual(result["positions"][0]["side"], "LONG")
        self.assertEqual(result["positions"][0]["quantity"], 0.01)
        self.assertEqual(result["positions"][0]["initialMargin"], 60)
        self.assertEqual(result["positions"][0]["maintMargin"], 1.2)
        self.assertAlmostEqual(result["positions"][0]["roePercent"], 12.5 / 60 * 100)
        self.assertEqual(result["positions"][0]["maxNotional"], 1000000)
        self.assertEqual(result["account"]["totalWalletBalance"], 100.1234)
        self.assertIn("unrealizedProfit", result["dataFields"]["positions"])
        self.assertIn("initialMargin", result["dataFields"]["positions"])
        self.assertTrue(result["testOnly"])
        self.assertNotIn("private-secret", json.dumps(result))

    @unittest.skipUnless(websocket_transport.websocket_client is not None, "websocket-client is not installed")
    def test_account_probe_retries_with_a_fresh_signature_after_timestamp_error(self):
        socket = FakeWebSocket(
            [
                {"id": 1, "status": 400, "error": {"code": -1021, "msg": "timestamp"}},
                {"id": 2, "status": 200, "result": {"positions": [], "assets": []}},
            ]
        )
        with patch.object(websocket_transport.websocket_client, "WebSocket", return_value=socket), patch.object(
            websocket_test, "_best_effort_sync_account_clock"
        ) as sync_clock, patch.object(
            websocket_test, "_futures_signed_timestamp", side_effect=[1700000000000, 1700000004000]
        ):
            result = websocket_test.test_binance_futures_account_websocket("mainnet", "public-key", "private-secret")

        self.assertTrue(result["ok"])
        self.assertEqual([item["id"] for item in socket.sent], [1, 2])
        sync_clock.assert_any_call("mainnet", force=True)
        self.assertNotEqual(socket.sent[0]["params"]["signature"], socket.sent[1]["params"]["signature"])
        self.assertTrue(socket.closed)


class BinanceWebSocketRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_probe_route_requires_login(self):
        with patch.object(app_module.db, "get_user_by_session_token", return_value=None):
            response = self.client.post("/api/binance/websocket-test", json={"network": "mainnet"})

        self.assertEqual(response.status_code, 401)

    def test_probe_route_does_not_use_account_credentials(self):
        expected = {"ok": True, "testOnly": True, "subscriptionAck": True}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module, "test_binance_futures_websocket", return_value=expected
        ) as probe:
            response = self.client.post(
                "/api/binance/websocket-test",
                json={"network": "mainnet"},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"result": expected})
        probe.assert_called_once_with("mainnet")

    def test_account_probe_route_uses_saved_credentials_without_returning_them(self):
        expected = {"ok": True, "testOnly": True, "positionCount": 1}
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_credentials",
            return_value={
                "configured": True,
                "network": "mainnet",
                "apiKey": "saved-key",
                "apiSecret": "saved-secret",
            },
        ), patch.object(app_module, "test_binance_futures_account_websocket", return_value=expected) as probe:
            response = self.client.post(
                "/api/binance/websocket-test",
                json={"scope": "account"},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"result": expected})
        probe.assert_called_once_with("mainnet", "saved-key", "saved-secret")

    def test_account_probe_route_rejects_missing_saved_credentials(self):
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}), patch.object(
            app_module.db,
            "get_user_binance_credentials",
            return_value={"configured": False, "network": "mainnet"},
        ), patch.object(app_module, "test_binance_futures_account_websocket") as probe:
            response = self.client.post(
                "/api/binance/websocket-test",
                json={"scope": "account"},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("保存 Binance API Key", response.get_json()["message"])
        probe.assert_not_called()

    def test_probe_route_rejects_unknown_scope(self):
        with patch.object(app_module.db, "get_user_by_session_token", return_value={"id": 17}):
            response = self.client.post(
                "/api/binance/websocket-test",
                json={"scope": "unknown"},
                headers={"Authorization": "Bearer authenticated-token"},
            )

        self.assertEqual(response.status_code, 400)
