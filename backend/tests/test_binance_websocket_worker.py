from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from backend.services import binance_websocket_worker as websocket_worker


class _FakeSocket:
    def __init__(self):
        self.closed = False
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


class BinanceWebSocketWorkerTests(unittest.TestCase):
    def test_market_target_changes_update_subscription_without_new_handshake(self):
        line = websocket_worker._MarketLine("mainnet", "FUTURES")
        socket = _FakeSocket()
        first = ("mainnet", "FUTURES", "BTCUSDT", "15m")
        second = ("mainnet", "FUTURES", "ETHUSDT", "15m")
        line.socket = socket
        line.connected = True
        line.handshake_count = 1
        line.desired = {"btcusdt@ticker": first}
        line.active_streams = {"btcusdt@ticker"}

        line.set_desired({
            "btcusdt@ticker": first,
            "ethusdt@ticker": second,
        })
        line._sync_subscriptions(socket, line.desired)

        self.assertFalse(socket.closed)
        self.assertEqual(line.handshake_count, 1)
        self.assertEqual(socket.sent, [{
            "method": "SUBSCRIBE",
            "params": ["ethusdt@ticker"],
            "id": 100,
        }])

    def test_market_target_removal_uses_unsubscribe_on_existing_socket(self):
        line = websocket_worker._MarketLine("mainnet", "FUTURES")
        socket = _FakeSocket()
        first = ("mainnet", "FUTURES", "BTCUSDT", "15m")
        second = ("mainnet", "FUTURES", "ETHUSDT", "15m")
        line.socket = socket
        line.connected = True
        line.desired = {"btcusdt@ticker": first, "ethusdt@ticker": second}
        line.active_streams = set(line.desired)

        line.set_desired({"btcusdt@ticker": first})
        line._sync_subscriptions(socket, line.desired)

        self.assertFalse(socket.closed)
        self.assertEqual(socket.sent, [{
            "method": "UNSUBSCRIBE",
            "params": ["ethusdt@ticker"],
            "id": 100,
        }])

    def test_desired_market_streams_keep_chart_streams_and_one_position_price_stream(self):
        chart_target = ("mainnet", "FUTURES", "BTCUSDT", "15m")
        with patch(
            "backend.services.binance_snapshot_worker.get_market_stream_targets",
            return_value=[chart_target],
        ), patch(
            "backend.services.binance_snapshot_worker.get_execution_market_price_targets",
            return_value=[("mainnet", "FUTURES", "ETHUSDT")],
        ), patch(
            "backend.services.binance_snapshot_worker.get_account_position_market_price_targets",
            return_value=[("mainnet", "FUTURES", "INTCUSDT")],
        ), patch(
            "backend.services.binance_snapshot_worker.get_execution_monitor_targets",
            return_value=[
                ("mainnet", "FUTURES", "ETHUSDT", "4h"),
                ("mainnet", "FUTURES", "ETHUSDT", "1h"),
                ("mainnet", "FUTURES", "ETHUSDT", "15m"),
                ("mainnet", "FUTURES", "ETHUSDT", "5m"),
            ],
        ):
            streams = websocket_worker._desired_market_streams({7})[("mainnet", "FUTURES")]

        self.assertEqual(
            set(streams),
            {
                "btcusdt@ticker",
                "btcusdt@kline_15m",
                "ethusdt@markPrice@1s",
                "intcusdt@ticker",
                "intcusdt@markPrice@1s",
                "ethusdt@kline_4h",
                "ethusdt@kline_1h",
                "ethusdt@kline_15m",
                "ethusdt@kline_5m",
            },
        )

    def test_desired_market_streams_only_keep_position_price_when_chart_is_empty(self):
        with patch(
            "backend.services.binance_snapshot_worker.get_market_stream_targets",
            return_value=[],
        ), patch(
            "backend.services.binance_snapshot_worker.get_execution_market_price_targets",
            return_value=[("mainnet", "FUTURES", "ETHUSDT")],
        ), patch(
            "backend.services.binance_snapshot_worker.get_account_position_market_price_targets",
            return_value=[("mainnet", "FUTURES", "INTCUSDT")],
        ), patch(
            "backend.services.binance_snapshot_worker.get_execution_monitor_targets",
            return_value=[],
        ):
            streams = websocket_worker._desired_market_streams({7})[("mainnet", "FUTURES")]

        self.assertEqual(set(streams), {"ethusdt@markPrice@1s", "intcusdt@ticker", "intcusdt@markPrice@1s"})

    def test_account_line_is_not_healthy_until_a_successful_response_is_cached(self):
        line = websocket_worker._AccountLine({
            "userId": 991234,
            "network": "mainnet",
            "apiKey": "public-key",
            "apiSecret": "private-secret",
        })
        socket = _FakeSocket()
        line.socket = socket
        line.connected = True
        line.last_message_at = int(time.time() * 1000)
        websocket_worker._account_lines[line.user_id] = line
        try:
            self.assertFalse(websocket_worker.is_account_line_healthy(line.user_id))
            line.last_data_at = int(time.time() * 1000)
            self.assertTrue(websocket_worker.is_account_line_healthy(line.user_id))
        finally:
            websocket_worker._account_lines.pop(line.user_id, None)

    def test_market_error_closes_socket_and_counts_one_reconnect(self):
        line = websocket_worker._MarketLine("mainnet", "FUTURES")
        socket = _FakeSocket()
        line.socket = socket
        line.connected = True
        line.active_streams = {"btcusdt@ticker"}

        line._set_error("订阅失败")

        self.assertTrue(socket.closed)
        self.assertFalse(line.connected)
        self.assertEqual(line.reconnect_count, 1)
        self.assertEqual(line.status, "ERROR")

    def test_market_target_health_requires_data_from_that_stream(self):
        line = websocket_worker._MarketLine("mainnet", "FUTURES")
        socket = _FakeSocket()
        target = ("mainnet", "FUTURES", "BTCUSDT", "15m")
        stream_map = {"btcusdt@kline_15m": target}
        line.socket = socket
        line.connected = True
        line.desired = stream_map
        line.active_streams = set(stream_map)
        websocket_worker._market_lines[("mainnet", "FUTURES")] = line
        try:
            now = int(time.time() * 1000)
            with patch.object(
                websocket_worker,
                "_now_ms",
                return_value=now,
            ), patch(
                "backend.services.binance_snapshot_worker.get_cached_kline_snapshot",
                return_value={"items": [{}] * 80, "updatedAt": now},
            ):
                # A fresh connection and a subscription ACK are not enough:
                # the target stream itself must have delivered a frame.
                line.last_message_at = now
                self.assertFalse(websocket_worker.is_market_target_healthy(target))

                with patch.object(websocket_worker, "_cache_market_event_for_network"):
                    line._handle(
                        {
                            "e": "kline",
                            "E": now,
                            "s": "BTCUSDT",
                            "k": {
                                "s": "BTCUSDT",
                                "i": "15m",
                                "t": now,
                                "o": "100",
                                "h": "101",
                                "l": "99",
                                "c": "100.5",
                                "v": "1",
                                "T": now + 899999,
                                "x": False,
                            },
                        },
                        stream_map,
                    )
                self.assertIn("btcusdt@kline_15m", line.last_data_at)
                self.assertTrue(websocket_worker.is_market_target_healthy(target))
        finally:
            websocket_worker._market_lines.pop(("mainnet", "FUTURES"), None)


if __name__ == "__main__":
    unittest.main()
