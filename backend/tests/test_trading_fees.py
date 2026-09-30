from __future__ import annotations

import unittest

from backend.services.trading_fees import estimate_a_share_trade_fee, resolve_a_share_exchange


class TradingFeeTests(unittest.TestCase):
    def test_shanghai_buy_uses_minimum_commission_without_other_fees(self):
        estimate = estimate_a_share_trade_fee("BUY", "600000", 1_000)

        self.assertEqual(estimate["exchange"], "SH")
        self.assertEqual(estimate["commission"], 0.30)
        self.assertEqual(estimate["stampDuty"], 0)
        self.assertEqual(estimate["otherFees"], 0)
        self.assertEqual(estimate["total"], 0.30)

    def test_shenzhen_sale_includes_stamp_duty_and_other_fees(self):
        estimate = estimate_a_share_trade_fee("SELL", "000001", 1_000)

        self.assertEqual(estimate["exchange"], "SZ")
        self.assertEqual(estimate["commission"], 0.30)
        self.assertEqual(estimate["stampDuty"], 0.50)
        self.assertEqual(estimate["otherFees"], 0.01)
        self.assertEqual(estimate["total"], 0.81)

    def test_fee_components_round_half_up_to_cents(self):
        estimate = estimate_a_share_trade_fee("SELL", "000001", 6_666)

        self.assertEqual(estimate["commission"], 0.67)
        self.assertEqual(estimate["stampDuty"], 3.33)
        self.assertEqual(estimate["otherFees"], 0.07)
        self.assertEqual(estimate["total"], 4.07)

    def test_exchange_uses_the_first_digit_of_a_six_digit_code(self):
        self.assertEqual(resolve_a_share_exchange("sh600000"), "SH")
        self.assertEqual(resolve_a_share_exchange("sz000001"), "SZ")
        self.assertEqual(estimate_a_share_trade_fee("BUY", "sh600000", 1_000)["otherFees"], 0)
        self.assertEqual(estimate_a_share_trade_fee("BUY", "sz000001", 1_000)["otherFees"], 0.01)


if __name__ == "__main__":
    unittest.main()
