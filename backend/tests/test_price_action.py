from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.services.price_action import MIN_ANALYSIS_BARS, analyze_price_action


def make_bar(day: int, open_price: float, high: float, low: float, close: float, volume: float = 1000) -> dict:
    return {
        "date": (date(2026, 1, 1) + timedelta(days=day)).isoformat(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def trend_bars(count: int = 42) -> list[dict]:
    return [make_bar(index, 10 + index * 0.2 - 0.08, 10 + index * 0.2 + 0.12, 10 + index * 0.2 - 0.18, 10 + index * 0.2) for index in range(count)]


def range_bars(count: int = 42) -> list[dict]:
    bars = []
    for index in range(count):
        close = 10.14 if index % 2 else 9.86
        open_price = 9.94 if index % 2 else 10.06
        bars.append(make_bar(index, open_price, 10.32, 9.68, close))
    return bars


class PriceActionTests(unittest.TestCase):
    def test_marks_insufficient_history_as_wait(self):
        result = analyze_price_action(trend_bars(MIN_ANALYSIS_BARS - 1))

        self.assertEqual(result["priceAction"]["environment"]["state"], "DATA_INSUFFICIENT")
        self.assertEqual(result["priceAction"]["assessment"]["action"], "WAIT")
        self.assertEqual(result["priceAction"]["signals"], [])

    def test_classifies_a_measurable_bull_trend_and_enriches_bars(self):
        bars = trend_bars()
        bars[-1]["pctChange"] = 9.98
        bars[-1]["priceChange"] = 1.1
        result = analyze_price_action(bars)
        price_action = result["priceAction"]

        self.assertEqual(price_action["environment"]["state"], "BULL_TREND")
        self.assertGreater(price_action["metrics"]["emaSlope"], 0)
        self.assertEqual(price_action["structure"]["lookbackBars"], 20)
        self.assertIn("最近20根日完整K线", price_action["structure"]["label"])
        self.assertIn("ema20", result["rawKlines"][-1])
        self.assertIn("atr14", result["rawKlines"][-1])
        self.assertEqual(result["rawKlines"][-1]["pctChange"], 9.98)
        self.assertEqual(result["rawKlines"][-1]["priceChange"], 1.1)
        self.assertTrue(any(level["kind"] == "EMA20" for level in price_action["levels"]))
        self.assertIn("volume", price_action)
        self.assertIn("futurePlans", price_action)
        self.assertTrue(all("anchorPrice" in plan and "volume" in plan for plan in price_action["futurePlans"]))

    def test_classifies_overlapping_bars_as_range_and_blocks_middle_trade(self):
        bars = range_bars()
        bars[-1] = make_bar(len(bars) - 1, 10.0, 10.05, 9.92, 10.0)
        result = analyze_price_action(bars)
        price_action = result["priceAction"]

        self.assertEqual(price_action["environment"]["state"], "RANGE")
        self.assertTrue(any(level["kind"] == "RANGE_LOW" for level in price_action["levels"]))
        self.assertTrue(any(setup["type"] == "RANGE_MIDDLE" for setup in price_action["setups"]))
        middle_plan = next(plan for plan in price_action["futurePlans"] if plan["type"] == "RANGE_MIDDLE")
        self.assertEqual(middle_plan["anchorType"], "OBSERVATION")
        self.assertIsNotNone(middle_plan["anchorPrice"])
        self.assertEqual(price_action["assessment"]["action"], "WAIT")

    def test_breakout_retest_emits_complete_buy_plan(self):
        bars = range_bars(38)
        bars.extend(
            [
                make_bar(38, 10.24, 10.82, 10.20, 10.68, 1800),
                make_bar(39, 10.39, 10.88, 10.25, 10.74, 1600),
            ]
        )

        result = analyze_price_action(bars)
        signal = next(item for item in result["priceAction"]["signals"] if item["type"] == "BREAKOUT_RETEST_UP")

        self.assertEqual(signal["direction"], "BUY")
        self.assertEqual(signal["status"], "CONFIRMED")
        self.assertGreater(signal["entryLimit"], signal["triggerPrice"])
        self.assertLess(signal["invalidationPrice"], signal["entryLimit"])
        self.assertGreater(signal["firstTarget"], signal["entryLimit"])
        self.assertGreaterEqual(len(signal["reasons"]), 2)
        self.assertTrue(signal["cancellationConditions"])
        cancellation_text = "；".join(signal["cancellationConditions"])
        self.assertIn("突破回踩再启动", cancellation_text)
        self.assertNotIn("BREAKOUT_RETEST_UP", cancellation_text)

    def test_low_volume_breakout_remains_a_future_plan_without_confirmed_signal(self):
        bars = range_bars(38)
        bars.extend(
            [
                make_bar(38, 10.24, 10.82, 10.20, 10.68, 1000),
                make_bar(39, 10.39, 10.88, 10.25, 10.74, 1000),
            ]
        )

        result = analyze_price_action(bars)
        price_action = result["priceAction"]
        plan = next(item for item in price_action["futurePlans"] if item["type"] == "BREAKOUT_RETEST_UP")

        self.assertEqual(plan["status"], "PENDING")
        self.assertTrue(plan["reviewRequired"])
        self.assertFalse(plan["volume"]["qualified"])
        self.assertTrue(any("量" in condition for condition in plan["missingConditions"]))
        self.assertEqual(price_action["signals"], [])

    def test_double_top_uses_the_neckline_as_a_future_exit_anchor(self):
        bars = [make_bar(index, 10.0, 10.3, 9.7, 10.0) for index in range(42)]
        bars[25] = make_bar(25, 11.6, 12.0, 11.4, 11.8)
        bars[30] = make_bar(30, 10.7, 11.0, 10.3, 10.5)
        bars[35] = make_bar(35, 11.5, 11.9, 11.2, 11.6)
        for index in range(36, 40):
            bars[index] = make_bar(index, 10.95, 11.1, 10.8, 10.95)
        bars[39] = make_bar(39, 11.4, 11.8, 10.8, 11.1)
        bars[40] = make_bar(40, 11.0, 11.2, 10.8, 11.0)
        bars[41] = make_bar(41, 11.1, 11.4, 10.9, 11.0)

        result = analyze_price_action(bars)
        plan = next(item for item in result["priceAction"]["futurePlans"] if item["type"] == "DOUBLE_TOP")

        self.assertEqual(plan["anchorLabel"], "双顶颈线确认")
        self.assertAlmostEqual(plan["anchorPrice"], 10.8, places=2)
        self.assertEqual(plan["status"], "PENDING")
        self.assertIn("颈线", plan["anchorCondition"])

    def test_bear_flag_uses_the_flag_floor_as_a_future_exit_anchor(self):
        bars = [make_bar(index, 20.0, 20.3, 19.7, 20.0) for index in range(34)]
        bars.extend(
            [
                make_bar(34, 20.0, 20.2, 19.4, 19.5, 1800),
                make_bar(35, 19.5, 19.7, 18.6, 18.7, 1900),
                make_bar(36, 18.7, 18.9, 17.8, 17.9, 2000),
                make_bar(37, 17.9, 18.1, 17.1, 17.2, 2100),
                make_bar(38, 17.2, 17.4, 16.2, 16.4, 2200),
                make_bar(39, 16.5, 17.1, 16.35, 16.8, 1200),
                make_bar(40, 16.8, 17.2, 16.55, 17.0, 1100),
                make_bar(41, 17.0, 17.05, 16.1, 16.15, 2400),
            ]
        )

        result = analyze_price_action(bars)
        plan = next(item for item in result["priceAction"]["futurePlans"] if item["type"] == "BEAR_FLAG")

        self.assertEqual(plan["direction"], "SELL")
        self.assertEqual(plan["status"], "CONFIRMED")
        self.assertEqual(plan["anchorLabel"], "熊旗下沿确认")
        self.assertAlmostEqual(plan["anchorPrice"], 16.35, places=2)
        self.assertTrue(plan["volume"]["qualified"])

    def test_failed_bullish_breakout_emits_defensive_sell_signal(self):
        bars = range_bars(38)
        bars.extend(
            [
                make_bar(38, 10.28, 10.86, 10.24, 10.70, 1800),
                make_bar(39, 10.56, 10.64, 9.82, 9.92, 2200),
            ]
        )

        result = analyze_price_action(bars)
        signal = next(item for item in result["priceAction"]["signals"] if item["type"] == "FAILED_BULLISH_BREAKOUT")

        self.assertEqual(signal["direction"], "SELL")
        self.assertEqual(signal["executionAction"], "SELL_EXISTING_LONG")
        self.assertEqual(signal["executionConstraint"], "EXISTING_LONG_ONLY")
        self.assertTrue(signal["positionRequired"])
        self.assertFalse(signal["shortSellingAllowed"])
        self.assertIsNone(signal["entryLimit"])
        self.assertIsNone(signal["firstTarget"])

    def test_single_countertrend_break_is_review_required_not_a_reversal_order(self):
        bars = range_bars(34)
        bars.extend(
            [
                make_bar(34, 9.96, 10.08, 9.92, 10.03),
                make_bar(35, 10.00, 10.13, 9.98, 10.08),
                make_bar(36, 10.05, 10.18, 10.03, 10.13),
                make_bar(37, 10.10, 10.23, 10.08, 10.18),
                make_bar(38, 10.15, 10.28, 10.13, 10.23),
                make_bar(39, 10.20, 10.31, 10.18, 10.26),
                make_bar(40, 10.25, 10.29, 10.14, 10.20),
                make_bar(41, 10.16, 10.18, 9.78, 9.82, 1800),
            ]
        )

        result = analyze_price_action(bars)
        reversal = next(item for item in result["priceAction"]["setups"] if item["type"] == "MAJOR_BEARISH_REVERSAL")

        self.assertEqual(reversal["status"], "NEEDS_REVIEW")
        self.assertTrue(reversal["reviewRequired"])
        self.assertFalse(any(item["type"] == "MAJOR_BEARISH_REVERSAL" for item in result["priceAction"]["signals"]))


if __name__ == "__main__":
    unittest.main()
