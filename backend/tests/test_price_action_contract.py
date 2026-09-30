from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.services.price_action import analyze_price_action
from backend.services.price_action_contract import (
    EnvironmentState,
    SOURCE_POLICY_ID,
    SOURCE_POLICY_VERSION,
    _range_patterns,
    _reversal_chain,
    build_bar_facts,
    build_session_context,
    build_trade_plan,
    build_volume_turnover_context,
    detect_pattern_families,
    get_price_action_profile,
    is_minute_timeframe,
    market_mode_for_setup,
    session_for_timestamp,
    update_dynamic_stop,
    validate_session_continuity,
)


def make_bar(index: int, close: float, *, volume: float = 1000, amount: float | None = None, **extra) -> dict:
    day = date(2026, 1, 1) + timedelta(days=index)
    return {
        "date": day.isoformat(),
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": volume,
        "amount": amount,
        **extra,
    }


class PriceActionContractTests(unittest.TestCase):
    def test_range_score_reflects_balance_evidence_instead_of_fixed_value(self):
        bars = []
        for index in range(42):
            close = 10.14 if index % 2 else 9.86
            bars.append({
                "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                "open": 9.94 if index % 2 else 10.06,
                "high": 10.32,
                "low": 9.68,
                "close": close,
                "volume": 1000,
            })

        environment = analyze_price_action(bars)["priceAction"]["environment"]

        self.assertEqual(environment["state"], "RANGE")
        self.assertNotEqual(environment["score"], 0.75)
        self.assertGreater(environment["score"], 0.4)
        self.assertLess(environment["score"], 0.9)
        self.assertTrue(any("区间评分依据" in item for item in environment["evidence"]))

    def test_contract_ids_and_confirmations_are_serializable(self):
        bars = [make_bar(index, 10 + index * 0.1, amount=1000) for index in range(50)]
        result = analyze_price_action(bars)
        price_action = result["priceAction"]
        self.assertEqual(result["contractVersion"], "price-action-contract-v1")
        self.assertEqual(price_action["sourcePolicy"]["policyId"], SOURCE_POLICY_ID)
        self.assertEqual(price_action["sourcePolicy"]["policyVersion"], SOURCE_POLICY_VERSION)
        self.assertEqual(len(price_action["sourcePolicy"]["books"]), 4)
        self.assertTrue(price_action["profileVersion"])
        self.assertTrue(price_action["barFacts"][-1]["barId"])
        for structure in price_action["structures"]:
            self.assertTrue(structure["structureId"])
            self.assertLessEqual(structure["confirmedAt"], bars[-1]["date"])
        for plan in price_action["tradePlans"]:
            self.assertTrue(plan["planId"])
            self.assertIn("blockedReasons", plan)

    def test_swing_confirmation_never_uses_unseen_right_bars(self):
        bars = [make_bar(index, 10) for index in range(40)]
        bars[3].update(high=12, low=9.8, close=11.5)
        facts = build_bar_facts(bars, timeframe="1d")
        result = analyze_price_action(bars)
        swing = next(item for item in result["priceAction"]["swings"] if item["index"] == 3 and item["kind"] == "SWING_HIGH")
        self.assertEqual(swing["confirmedAt"], facts[5].timestamp)

    def test_turnover_is_unknown_without_valid_denominator(self):
        facts = build_bar_facts([make_bar(index, 10, amount=1000) for index in range(8)])
        volume = build_volume_turnover_context(facts)
        self.assertIsNone(volume["turnoverRate"])
        self.assertEqual(volume["turnoverSource"], "UNKNOWN")
        self.assertIn("FLOAT_SHARES_MISSING", volume["qualityFlags"])

    def test_computed_turnover_preserves_units(self):
        facts = build_bar_facts([make_bar(index, 10, amount=1000, floatShares=10000) for index in range(8)])
        volume = build_volume_turnover_context(facts)
        self.assertAlmostEqual(volume["turnoverRate"], 0.1)
        self.assertEqual(volume["turnoverSource"], "COMPUTED")
        self.assertEqual(volume["turnoverQuality"], "VALID")

    def test_intraday_session_and_missing_opening_bars_are_explicit(self):
        self.assertEqual(session_for_timestamp("2026-01-01 09:45", timeframe="15m"), "regular")
        self.assertEqual(session_for_timestamp("2026-01-01 09:15", timeframe="15m"), "premarket")
        facts = build_bar_facts([make_bar(0, 10) | {"date": "2026-01-01 10:00"}], timeframe="15m")
        quality = validate_session_continuity(facts, "15m")
        self.assertIn(quality["state"], {"VALID", "DATA_INSUFFICIENT"})

    def test_dynamic_stop_is_monotonic_and_auditable(self):
        facts = build_bar_facts([make_bar(index, 10 + index * 0.5) for index in range(10)])
        plan = {"planId": "p1", "direction": "BUY", "entryLimit": 10, "initialStop": 9, "activeStop": 9, "stopState": "initial", "stopEvents": []}
        managed = update_dynamic_stop(plan, facts)
        self.assertGreaterEqual(managed["activeStop"], 9)
        self.assertTrue(all(event["newPrice"] >= event["oldPrice"] for event in managed["stopEvents"]))

    def test_sell_contract_is_existing_long_only_without_short_risk_math(self):
        facts = build_bar_facts([make_bar(index, 10 + index * 0.05) for index in range(40)])
        plan = build_trade_plan(
            {
                "setupId": "sell-setup",
                "type": "DOUBLE_TOP",
                "direction": "SELL",
                "status": "CONFIRMED",
                "triggerPrice": 11.0,
                "invalidationPrice": 11.4,
                "evidence": ["向下确认"],
            },
            [],
            facts[-1],
            {"evidence": ["下行环境"]},
            {"state": "NORMAL"},
        )
        self.assertEqual(plan["executionAction"], "SELL_EXISTING_LONG")
        self.assertEqual(plan["executionConstraint"], "EXISTING_LONG_ONLY")
        self.assertTrue(plan["positionRequired"])
        self.assertFalse(plan["shortSellingAllowed"])
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["entryOrderType"], "SELL_CONFIRMATION")
        self.assertIsNone(plan["entryLimit"])
        self.assertIsNone(plan["initialStop"])
        self.assertIsNone(plan["firstTarget"])
        self.assertIsNone(plan["roomR"])
        self.assertIn("SHORT_SELLING_DISABLED", plan["blockedReasons"])

    def test_market_modes_are_explicit_and_extension_is_trend_only(self):
        self.assertEqual(market_mode_for_setup("TREND_PULLBACK_H1"), ("TREND", "趋势"))
        self.assertEqual(market_mode_for_setup("RANGE_LOWER_REVERSAL"), ("RANGE", "区间"))
        self.assertEqual(market_mode_for_setup("DOUBLE_BOTTOM"), ("REBOUND", "反弹"))

        facts = build_bar_facts([make_bar(index, 10 + index * 0.05) for index in range(40)])
        levels = [
            {"price": 12.0, "role": "RESISTANCE", "source": "已确认阻力一"},
            {"price": 14.0, "role": "RESISTANCE", "source": "已确认阻力二"},
        ]
        common = {
            "status": "CONFIRMED",
            "direction": "BUY",
            "triggerPrice": 11.0,
            "invalidationPrice": 10.0,
            "evidence": ["结构确认"],
        }
        trend = build_trade_plan({**common, "setupId": "trend", "type": "TREND_PULLBACK_H1"}, levels, facts[-1], {"evidence": []}, {"state": "NORMAL"})
        range_plan = build_trade_plan({**common, "setupId": "range", "type": "RANGE_LOWER_REVERSAL"}, levels, facts[-1], {"evidence": []}, {"state": "NORMAL"})
        rebound = build_trade_plan({**common, "setupId": "rebound", "type": "DOUBLE_BOTTOM"}, levels, facts[-1], {"evidence": []}, {"state": "NORMAL"})
        self.assertEqual(trend["marketMode"], "TREND")
        self.assertEqual(range_plan["marketMode"], "RANGE")
        self.assertEqual(rebound["marketMode"], "REBOUND")
        self.assertIsNotNone(trend["extensionTarget"])
        self.assertIsNone(range_plan["extensionTarget"])
        self.assertIsNone(rebound["extensionTarget"])

    def test_completed_bar_boundary_blocks_latest_unfinished_confirmation(self):
        bars = [make_bar(index, 10 + index * 0.12) for index in range(45)]
        bars[-1].update(open=15.1, high=16.2, low=15.0, close=16.1, completed=False)

        result = analyze_price_action(bars)
        price_action = result["priceAction"]

        self.assertTrue(price_action["uncertainty"]["incompleteLatestBar"])
        self.assertFalse(price_action["barFacts"][-1]["completed"])
        self.assertTrue(all(plan["observedAt"] != bars[-1]["date"] for plan in price_action["tradePlans"]))

    def test_every_mode_pattern_has_lifecycle_and_source_audit(self):
        bars = [make_bar(index, 10.0 + (index % 2) * 0.18, volume=1200) for index in range(42)]
        result = analyze_price_action(bars)
        patterns = result["priceAction"]["patternFamilies"]

        self.assertTrue(patterns)
        for pattern in patterns:
            self.assertIn(pattern["marketMode"], {"TREND", "RANGE", "REBOUND"})
            self.assertIn(pattern["lifecycle"], {"WATCH", "PENDING", "CONFIRMED", "FAILED", "EXPIRED", "NEEDS_REVIEW"})
            self.assertTrue(pattern["sourcePages"])
            self.assertIn("nextCondition", pattern)

    def test_daily_timeframe_never_emits_session_patterns(self):
        result = analyze_price_action([make_bar(index, 10 + index * 0.08) for index in range(42)], timeframe="1d")
        session = result["priceAction"]["sessionContext"]

        self.assertFalse(session["applicable"])
        self.assertEqual(session["openingContext"]["status"], "NOT_APPLICABLE")
        self.assertEqual(session["sessionPatterns"], [])
        self.assertFalse(is_minute_timeframe("1d"))

    def test_minute_session_requires_opening_bars_and_exposes_expiry(self):
        raw = [
            {
                **make_bar(index, 10 + index * 0.03),
                "date": f"2026-02-03 09:{30 + index * 5:02d}" if index < 6 else f"2026-02-03 10:{(index - 6) * 5:02d}",
            }
            for index in range(12)
        ]
        facts = build_bar_facts(raw, timeframe="5m")
        session = build_session_context(facts, "5m")

        self.assertTrue(session["applicable"])
        self.assertEqual(session["openingContext"]["status"], "CONFIRMED")
        self.assertTrue(session["sessionPatterns"])
        self.assertTrue(all(item.get("sessionExpiry") == "SESSION_CLOSE" for item in session["sessionPatterns"]))

    def test_pattern_family_helper_does_not_use_incomplete_bars(self):
        raw = [make_bar(index, 10 + index * 0.2) for index in range(40)]
        raw[-1]["completed"] = False
        facts = build_bar_facts(raw)
        complete = [fact for fact in facts if fact.completed]
        from backend.services.price_action_contract import _environment_from_facts, get_price_action_profile

        profile = get_price_action_profile()
        environment = _environment_from_facts(complete, build_volume_turnover_context(complete), profile)
        patterns = detect_pattern_families(complete, environment, profile)

        self.assertTrue(all(pattern.get("observedAt") != facts[-1].timestamp for pattern in patterns))

    def test_range_breakout_lifecycle_distinguishes_confirmation_from_failure(self):
        profile = get_price_action_profile()
        environment = EnvironmentState(
            "RANGE", "交易区间", 0.0, 0.8, ["区间边界明确"], [], "2026-01-14", range_zone={"low": 9.6, "high": 10.4, "mid": 10.0, "width": 0.8},
        )
        confirmed_bars = [make_bar(index, 10.0 + (index % 2) * 0.04) for index in range(14)]
        confirmed_bars[-1].update(open=10.12, high=11.45, low=10.08, close=11.35)
        confirmed = _range_patterns(build_bar_facts(confirmed_bars), environment, profile)
        confirmed_break = next(item for item in confirmed if item["type"] == "RANGE_BREAKOUT_CONFIRMED")
        self.assertEqual(confirmed_break["status"], "CONFIRMED")
        self.assertEqual(confirmed_break["marketMode"], "RANGE")

        failed_bars = [make_bar(index, 10.0 + (index % 2) * 0.04) for index in range(14)]
        failed_bars[-2].update(open=10.08, high=11.35, low=10.04, close=11.20)
        failed_bars[-1].update(open=11.20, high=11.28, low=9.35, close=9.48)
        failed = _range_patterns(build_bar_facts(failed_bars), environment, profile)
        failure = next(item for item in failed if item["type"] == "FAILED_BULLISH_BREAKOUT")
        self.assertEqual(failure["status"], "FAILED")
        self.assertEqual(failure["direction"], "SELL")
        self.assertIn("前一根完整K线", failure["evidence"][0])

    def test_double_and_wedge_remain_observational_until_the_reversal_chain_confirms(self):
        profile = get_price_action_profile()
        bars = [make_bar(index, 11.0 - index * 0.08) for index in range(14)]
        facts = build_bar_facts(bars)
        environment = EnvironmentState("BEAR_TREND", "空头趋势", -4.0, 0.8, ["趋势仍有效"], [], facts[-1].timestamp)
        swings = [
            {"kind": "SWING_LOW", "index": 4, "price": 9.50},
            {"kind": "SWING_LOW", "index": 7, "price": 9.40},
            {"kind": "SWING_LOW", "index": 10, "price": 9.30},
        ]

        patterns = _reversal_chain(facts, environment, profile, swings)
        double_bottom = next(item for item in patterns if item["type"] == "DOUBLE_BOTTOM")
        wedge = next(item for item in patterns if item["type"] == "WEDGE_THIRD_PUSH")
        self.assertEqual(double_bottom["marketMode"], "REBOUND")
        self.assertEqual(wedge["marketMode"], "REBOUND")
        self.assertNotEqual(double_bottom["status"], "CONFIRMED")
        self.assertNotEqual(wedge["status"], "CONFIRMED")
        self.assertTrue(double_bottom["reviewRequired"])
        self.assertTrue(wedge["reviewRequired"])


if __name__ == "__main__":
    unittest.main()
