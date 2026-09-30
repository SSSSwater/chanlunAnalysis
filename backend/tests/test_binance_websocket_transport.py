from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.services import binance_websocket_transport as transport


class _RawWebSocket:
    def __init__(self):
        self.connected = False
        self.sock = object()
        self.sent = []
        self.messages = ['{"result":null,"id":1}']
        self.timeouts = []
        self.closed = False

    def connect(self, endpoint, **options):
        self.endpoint = endpoint
        self.options = options
        self.connected = True

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        return self.messages.pop(0)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True
        self.connected = False
        self.sock = None


class BinanceWebSocketTransportTests(unittest.TestCase):
    def test_websocket_client_adapter_keeps_json_worker_contract(self):
        raw = _RawWebSocket()
        adapter = transport._WebSocketClientAdapter(raw)
        adapter.connect("wss://example.test/ws", timeout=3)
        adapter.send_json({"method": "SUBSCRIBE", "id": 1})

        self.assertEqual(raw.sent, ['{"method":"SUBSCRIBE","id":1}'])
        self.assertEqual(adapter.recv_json(), {"result": None, "id": 1})
        self.assertFalse(adapter.closed)
        adapter.close()
        self.assertTrue(adapter.closed)

    def test_open_websocket_uses_configured_proxy(self):
        raw = _RawWebSocket()
        websocket_factory = type(
            "WebSocketModule",
            (),
            {"WebSocket": staticmethod(lambda: raw)},
        )
        with patch.object(transport, "websocket_client", websocket_factory):
            socket = transport.open_websocket(
                "wss://example.test/ws",
                timeout=5,
            )
        self.assertIsInstance(socket, transport._WebSocketClientAdapter)
        self.assertEqual(
            raw.options,
            {
                "timeout": 5,
                "http_proxy_host": "127.0.0.1",
                "http_proxy_port": 10808,
                "proxy_type": "http",
                "http_no_proxy": [],
            },
        )

        fallback = _RawWebSocket()
        curl_module = type(
            "CurlModule",
            (),
            {"WebSocket": staticmethod(lambda: fallback)},
        )
        with patch.object(transport, "websocket_client", None), patch.object(
            transport, "curl_requests", curl_module
        ):
            socket = transport.open_websocket(
                "wss://example.test/ws",
                timeout=5,
            )
        self.assertIs(socket, fallback)
        self.assertEqual(fallback.options["impersonate"], "edge")
        self.assertEqual(
            fallback.options["proxies"],
            {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"},
        )


if __name__ == "__main__":
    unittest.main()
