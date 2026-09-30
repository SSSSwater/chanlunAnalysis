from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.services import binance_browser_auth as browser_auth


class _FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        return json.dumps(next(self.messages))

    def close(self):
        pass


class BinanceBrowserAuthTests(unittest.TestCase):
    def test_header_capture_is_case_insensitive(self):
        result = browser_auth._header_capture(browser_auth._normalized_headers({"COOKIE": "session=abc", "CSRFTOKEN": "csrf-1"}))
        self.assertEqual(result, {"cookie": "session=abc", "csrfToken": "csrf-1"})

    def test_capture_merges_request_and_extra_info_headers(self):
        socket = _FakeSocket([
            {
                "method": "Network.requestWillBeSent",
                "params": {
                    "requestId": "1",
                    "request": {
                        "url": "https://www.binance.com/bapi/asset/v1/private/future/smart-money/profile/query-positions",
                        "headers": {"Cookie": "session=abc"},
                    },
                },
            },
            {
                "method": "Network.requestWillBeSentExtraInfo",
                "params": {"requestId": "1", "headers": {"csrftoken": "csrf-1"}},
            },
        ])
        with patch.object(browser_auth, "_find_or_open_page", return_value={"webSocketDebuggerUrl": "ws://binance"}), patch(
            "websocket.create_connection", return_value=socket
        ):
            result = browser_auth.capture_smart_money_auth(timeout_seconds=1)

        self.assertEqual(result, {"cookie": "session=abc", "csrfToken": "csrf-1"})
        self.assertTrue(any(command["method"] == "Network.enable" for command in socket.sent))


if __name__ == "__main__":
    unittest.main()
