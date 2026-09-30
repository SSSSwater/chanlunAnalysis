from __future__ import annotations

import unittest

from backend.services.a_share_strategy import attach_a_share_strategy_plan
from backend.services.discipline_strategy import evaluate_discipline_strategy
from backend.services.price_action import analyze_price_action
from backend.tests.test_discipline_strategy import confirmed_buy_bars, confirmed_defensive_sell_bars, market_bull_bars, range_bars


class AShareStrategyPlanTests(unittest.TestCase):
    def test_stock_plan_has_binance_style_plan_contract_without_short_execution(self):
        bars = confirmed_buy_bars()
        result = attach_a_share_strategy_plan(analyze_price_action(bars), bars, scope="STOCK")
        plan = result["strategyPlan"]

        for field in (
            "strategyMode",
            "status",
            "conditionMet",
            "conditionTotal",
            "conditionChecks",
            "entry",
            "entryTiming",
            "stopLoss",
            "takeProfits",
            "timeframes",
            "cancellationConditions",
            "levelBasis",
        ):
            self.assertIn(field, plan)
        self.assertEqual(plan["marketType"], "A_SHARE")
        self.assertEqual(plan["conditionTotal"], 10)
        self.assertFalse(plan["shortSellingAllowed"])
        self.assertIn(plan["executionAction"], {"BUY_LONG", "OBSERVE"})
        self.assertEqual(result["priceAction"]["strategyPlan"], plan)

    def test_discipline_plan_reuses_strategy_contract_and_keeps_cash_long_only(self):
        plan = evaluate_discipline_strategy(
            confirmed_buy_bars(),
            100000,
            index_history=market_bull_bars(),
        )
        strategy = plan["strategyPlan"]

        self.assertEqual(strategy["status"], "ARMED")
        self.assertEqual(strategy["conditionMet"], strategy["conditionTotal"])
        self.assertEqual(strategy["executionAction"], "BUY_LONG")
        self.assertEqual(strategy["executionConstraint"], "CASH_LONG_ONLY")
        self.assertFalse(strategy["shortSellingAllowed"])
        self.assertTrue(strategy["entry"]["trigger"])
        self.assertTrue(strategy["stopLoss"])
        self.assertTrue(strategy["takeProfits"])

    def test_index_plan_is_exposure_guidance_not_a_synthetic_order(self):
        bars = market_bull_bars()
        result = attach_a_share_strategy_plan(analyze_price_action(bars), bars, scope="INDEX")
        plan = result["strategyPlan"]

        self.assertEqual(plan["scope"], "INDEX")
        self.assertEqual(plan["direction"], "EXPOSURE")
        self.assertEqual(plan["executionAction"], "EXPOSURE_GUIDANCE")
        self.assertEqual(plan["executionConstraint"], "INDEX_EXPOSURE_ONLY")
        self.assertFalse(plan["shortSellingAllowed"])
        self.assertFalse(plan["executable"])
        self.assertIn("1w", plan["timeframes"])
        self.assertIn("1d", plan["timeframes"])

    def test_bearish_observation_never_leaks_sell_points_into_a_buy_plan(self):
        bars = confirmed_defensive_sell_bars()
        result = attach_a_share_strategy_plan(analyze_price_action(bars), bars, scope="STOCK")
        plan = result["strategyPlan"]

        self.assertEqual(plan["direction"], "OBSERVE")
        self.assertIsNone(plan["entry"]["trigger"])
        self.assertIsNone(plan["stopLoss"])
        self.assertEqual(plan["takeProfits"], [])

    def test_holding_strategy_exposes_the_monotonic_trailing_stop_contract(self):
        plan = evaluate_discipline_strategy(
            range_bars(),
            100000,
            position={
                "symbol": "000001",
                "costPrice": 10.0,
                "shares": 1000,
                "trailingStop": 10.8,
                "trailingPeak": 13.0,
                "trailingPeakAt": "2026-02-05",
                "trailingActive": True,
                "trailingStage": "ATR_TRAILING",
                "trailingActivationPrice": 11.0,
                "trailingActivationAt": "2026-01-20",
            },
            index_history=market_bull_bars(),
        )
        trailing = plan["strategyPlan"]["trailingStop"]

        self.assertTrue(trailing["applicable"])
        self.assertTrue(trailing["active"])
        self.assertTrue(trailing["monotonic"])
        self.assertEqual(trailing["state"], "ATR_TRAILING")
        self.assertGreaterEqual(trailing["stopPrice"], 10.8)
        self.assertEqual(trailing["peakPrice"], 13.0)
        self.assertEqual(trailing["execution"], "NEXT_SESSION_SELL_ONLY")


if __name__ == "__main__":
    unittest.main()
