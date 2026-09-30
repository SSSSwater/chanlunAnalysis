from __future__ import annotations

import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import URLError

from backend import fetch_binance_copy_trading_history as transport


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class _Opener:
    def __init__(self, result):
        self.result = result

    def open(self, _request, timeout):
        if isinstance(self.result, BaseException):
            raise self.result
        return _Response(self.result)


class BinanceCopyTransportTests(unittest.TestCase):
    def test_tls_eof_retries_with_fresh_proxy_openers(self):
        eof = URLError(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))
        openers = iter([_Opener(eof), _Opener(eof), _Opener({"success": True})])
        with patch.object(transport, "build_opener", side_effect=lambda *_args: next(openers)), patch.object(
            transport.time, "sleep"
        ) as sleep:
            result = transport._fetch_json(
                "https://example.test/api",
                {"page": 1},
                cookie="",
                proxy="http://127.0.0.1:10808",
                referer="https://example.test",
                timeout=1,
            )

        self.assertEqual(result, {"success": True})
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
