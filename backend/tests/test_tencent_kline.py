from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from backend.services import eastmoney_client


class TencentKlineTests(unittest.TestCase):
    def test_parses_qfq_daily_bars_into_shared_contract(self):
        response = Mock()
        response.json.return_value = {
            "code": 0,
            "data": {
                "sh600426": {
                    "qfqday": [
                        ["2026-08-20", "20.0", "20.5", "20.8", "19.8", "100"],
                        ["2026-08-21", "20.5", "21.0", "21.2", "20.2", "120"],
                    ]
                }
            },
        }
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response

        with patch.object(eastmoney_client.requests, "Session", return_value=session):
            items = eastmoney_client.get_tencent_kline("600426", limit=2)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["provider"], "tencent")
        self.assertEqual(items[1]["close"], 21.0)
        self.assertAlmostEqual(items[1]["pctChange"], 2.439, places=2)
        session.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
