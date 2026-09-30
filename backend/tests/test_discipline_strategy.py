from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.services.discipline_strategy import (
    DEFAULT_RULES,
    _initial_position_stop,
    _latest_future_plan,
    _moving_stop_state,
    _nearest_support,
    _position_levels,
    _reduction_shares,
    _target_touch_evidence,
    evaluate_discipline_strategy,
)


def bar(day: int, open_price: float, high: float, low: float, close: float, volume: float = 1000) -> dict:
    return {
        "date": (date(2026, 1, 1) + timedelta(days=day)).isoformat(),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def range_bars(count: int = 38) -> list[dict]:
    return [bar(index, 9.94 if index % 2 else 10.06, 10.32, 9.68, 10.14 if index % 2 else 9.86) for index in range(count)]


def confirmed_buy_bars() -> list[dict]:
    bars = range_bars()
    bars.extend(
        [
            bar(38, 10.24, 10.82, 10.40, 10.68, 1800),
            bar(39, 10.39, 10.88, 10.45, 10.74, 1600),
        ]
    )
    return bars


def market_bull_bars(count: int = 42) -> list[dict]:
    return [bar(index, 3000 + index * 2 - 1, 3000 + index * 2 + 2, 3000 + index * 2 - 2, 3000 + index * 2) for index in range(count)]


def confirmed_defensive_sell_bars() -> list[dict]:
    bars = range_bars(38)
    bars.extend(
        [
            bar(38, 10.28, 10.86, 10.24, 10.70, 1800),
            bar(39, 10.56, 10.64, 9.82, 9.92, 2200),
        ]
    )
    return bars


def confirmed_defensive_followthrough_bars() -> list[dict]:
    bars = range_bars(38)
    bars.extend(
        [
            bar(38, 10.28, 10.86, 10.24, 10.70, 1800),
            bar(39, 10.70, 11.20, 10.00, 10.10, 2200),
            bar(40, 10.30, 10.40, 9.60, 9.80, 2400),
        ]
    )
    return bars


class DisciplineStrategyTests(unittest.TestCase):
    def test_returns_wait_when_history_is_insufficient(self):
        plan = evaluate_discipline_strategy(range_bars(20), 100000)

        self.assertEqual(plan["action"], "WAIT")
        self.assertIn("日线数据不足", plan["blockedReasons"])

    def test_insufficient_history_still_preserves_actual_position_entry(self):
        plan = evaluate_discipline_strategy(
            range_bars(20),
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 100},
        )

        self.assertEqual(plan["levels"]["entryPrice"], 10.0)
        self.assertEqual(plan["levels"]["costPrice"], 10.0)
        self.assertIsNone(plan["levels"]["plannedEntryPrice"])

    def test_returns_buy_when_complete_price_action_and_account_gates_pass(self):
        plan = evaluate_discipline_strategy(confirmed_buy_bars(), 100000, index_history=market_bull_bars())

        self.assertEqual(plan["action"], "BUY")
        self.assertEqual(plan["positionContext"]["mode"], "ENTRY_WATCH")
        self.assertFalse(plan["positionContext"]["hasPosition"])
        self.assertEqual(plan["positionContext"]["shares"], 0)
        self.assertGreater(plan["order"]["shares"], 0)
        self.assertGreater(plan["levels"]["plannedEntryPrice"], 0)
        self.assertLess(plan["levels"]["protectiveStop"], plan["levels"]["plannedEntryPrice"])
        self.assertGreater(plan["levels"]["firstTarget"], plan["levels"]["plannedEntryPrice"])
        self.assertGreaterEqual(len(plan["levels"]["cancellationConditions"]), 2)
        next_session = plan["nextSessionPlan"]
        self.assertEqual(next_session["status"], "ARMED")
        self.assertFalse(next_session["hasPosition"])
        self.assertEqual(next_session["branches"][0]["action"], "BUY")
        self.assertGreater(next_session["branches"][0]["shares"], 0)
        self.assertEqual(plan["priceAction"]["activeSetup"]["type"], "BREAKOUT_RETEST_UP")
        self.assertTrue(plan["priceAction"]["futurePlans"])
        self.assertGreater(plan["levels"]["futureAnchorPrice"], 0)
        self.assertIn("anchorCondition", plan["priceAction"]["futurePlans"][0])
        decision = plan["decisionHierarchy"]
        self.assertEqual(decision["context"], "WATCHLIST_ENTRY")
        self.assertIn(decision["marketMode"], {"TREND", "RANGE", "REBOUND"})
        self.assertTrue(decision["pointDerivation"])
        self.assertTrue(any(item["id"] == "first_target" for item in decision["pointDerivation"]))

    def test_long_position_keeps_upside_take_profit_separate_from_downside_targets(self):
        plan = evaluate_discipline_strategy(
            confirmed_defensive_sell_bars(),
            100000,
            position={"symbol": "000523", "costPrice": 10.0, "shares": 1300},
            index_history=market_bull_bars(),
        )

        levels = plan["levels"]
        self.assertEqual(levels["levelContext"], "LONG_POSITION_DEFENSE")
        self.assertEqual(levels["targetDirection"], "UP")
        self.assertGreater(levels["firstTarget"], levels["costPrice"])
        self.assertEqual(levels["takeProfitStatus"], "STRUCTURE")
        self.assertIn("上方磁铁", levels["firstTargetSource"])
        self.assertNotIn("20周期EMA", levels["firstTargetSource"])
        self.assertLess(levels["downsideTarget"], levels["activeDefense"])
        self.assertLess(levels["downsideExtensionTarget"], levels["downsideTarget"])
        self.assertEqual(levels["defenseTriggerPrice"], levels["triggerPrice"])
        self.assertEqual(plan["action"], "HOLD")
        self.assertEqual(plan["actionLabel"], "防守观察")
        self.assertEqual(plan["order"]["shares"], 0)
        self.assertIn("下一交易日条件", plan["reasons"][0])
        next_session = plan["nextSessionPlan"]
        self.assertEqual(next_session["status"], "ARMED")
        self.assertTrue(next_session["hasPosition"])
        self.assertEqual(next_session["positionShares"], 1300)
        self.assertEqual(next_session["branches"][0]["action"], "SELL")
        self.assertEqual(next_session["branches"][0]["shares"], 1300)
        first_target_branch = next(item for item in next_session["branches"] if item["id"] == "FIRST_TARGET")
        self.assertEqual(first_target_branch["action"], "REDUCE")
        self.assertEqual(first_target_branch["shares"] % 100, 0)
        self.assertLess(first_target_branch["shares"], 1300)
        decision = plan["decisionHierarchy"]
        self.assertEqual(decision["context"], "HOLDING")
        self.assertIn("nextCondition", decision)
        first_target = next(item for item in decision["pointDerivation"] if item["id"] == "first_target")
        self.assertTrue(first_target["available"])
        self.assertTrue(first_target["meaning"])

    def test_position_context_tracks_existing_share_count(self):
        plan = evaluate_discipline_strategy(
            range_bars(),
            100000,
            position={"symbol": "000523", "costPrice": 10.0, "shares": 1300},
            index_history=market_bull_bars(),
        )

        self.assertEqual(plan["positionContext"]["mode"], "POSITION_MANAGEMENT")
        self.assertTrue(plan["positionContext"]["hasPosition"])
        self.assertEqual(plan["positionContext"]["shares"], 1300)
        self.assertIn("SELL", plan["positionContext"]["allowedActions"])

    def test_confirmed_counter_followthrough_allows_partial_reduction_above_stop(self):
        plan = evaluate_discipline_strategy(
            confirmed_defensive_followthrough_bars(),
            100000,
            position={"symbol": "000523", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        self.assertEqual(plan["action"], "REDUCE")
        self.assertEqual(plan["actionLabel"], "部分减仓")
        self.assertGreater(plan["order"]["shares"], 0)
        self.assertLess(plan["order"]["shares"], 1000)
        self.assertGreater(plan["metrics"]["latestClose"], plan["levels"]["activeDefense"])
        self.assertEqual(plan["levels"]["reductionStatus"], "CONFIRMED")

    def test_first_magnet_touch_is_a_partial_profit_event(self):
        evidence = _target_touch_evidence(
            close=12.0,
            upside_plan={"status": "CONFIRMED", "reviewRequired": False, "firstTarget": 11.8},
            long_signal=None,
        )

        self.assertEqual(evidence["type"], "TARGET_TOUCH")
        self.assertIn("第一磁铁", evidence["reason"])

    def test_plain_holding_warning_does_not_create_reduction_order(self):
        plan = evaluate_discipline_strategy(
            range_bars(),
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        self.assertEqual(plan["action"], "HOLD")
        self.assertEqual(plan["order"]["shares"], 0)
        self.assertNotIn("reductionShares", plan["levels"])

    def test_partial_reduction_uses_a_share_lots_and_one_lot_routes_to_full_exit(self):
        rules = {**DEFAULT_RULES, "accountValue": 100000}
        partial = _reduction_shares(
            position={"costPrice": 10.0, "shares": 1300},
            active_defense=9.53,
            rules=rules,
        )
        one_lot = _reduction_shares(
            position={"costPrice": 10.0, "shares": 100},
            active_defense=9.53,
            rules=rules,
        )

        self.assertEqual(partial % 100, 0)
        self.assertGreaterEqual(partial, 100)
        self.assertLessEqual(partial, 1200)
        self.assertEqual(one_lot, 100)

    def test_rejects_complete_setup_when_cash_cannot_buy_one_lot(self):
        plan = evaluate_discipline_strategy(
            confirmed_buy_bars(),
            100000,
            available_cash=50,
            index_history=market_bull_bars(),
        )

        self.assertEqual(plan["action"], "WAIT")
        self.assertIn("账户风险预算或可用现金不足以买入一手", plan["blockedReasons"])
        self.assertEqual(plan["riskRules"]["availableCash"], 50)

    def test_returns_sell_when_holding_position_hits_hard_stop(self):
        bars = market_bull_bars(40)
        bars[-1] = bar(39, 9.5, 9.6, 8.9, 9.1, 2000)

        plan = evaluate_discipline_strategy(
            bars,
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        self.assertEqual(plan["action"], "SELL")
        self.assertEqual(plan["order"]["shares"], 1000)
        self.assertEqual(plan["levels"]["hardStop"], 9.2)
        self.assertTrue(plan["levels"]["cancellationConditions"])

    def test_moving_stop_triggers_before_cost_stop_after_a_recent_high(self):
        bars = range_bars(38)
        bars.extend(
            [
                bar(38, 10.4, 15.0, 10.1, 14.5, 3000),
                bar(39, 14.6, 14.8, 13.2, 14.0, 2500),
                bar(40, 13.8, 14.0, 11.2, 11.5, 2800),
            ]
        )

        plan = evaluate_discipline_strategy(
            bars,
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        levels = plan["levels"]
        self.assertEqual(plan["action"], "SELL")
        self.assertEqual(levels["recentHigh20"], 15.0)
        self.assertEqual(levels["entryPrice"], 10.0)
        self.assertEqual(plan["position"]["entryPrice"], 10.0)
        self.assertTrue(levels["movingStopActive"])
        self.assertGreater(levels["movingStop"], levels["costStop"])
        self.assertEqual(levels["activeDefense"], levels["movingStop"])
        self.assertLess(11.5, levels["movingStop"])

    def test_does_not_use_pre_entry_high_for_an_unprofitable_position(self):
        bars = range_bars(38)
        bars.extend(
            [
                bar(38, 10.0, 20.0, 9.8, 10.2, 3000),
                bar(39, 10.2, 15.0, 9.9, 10.0, 2500),
            ]
        )

        plan = evaluate_discipline_strategy(
            bars,
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        levels = plan["levels"]
        self.assertFalse(levels["movingStopActive"])
        self.assertIsNone(levels["movingStop"])
        self.assertLessEqual(levels["activeDefense"], levels["hardStop"])
        moving_check = next(item for item in plan["checks"] if item["name"] == "移动止损")
        self.assertTrue(moving_check["passed"])
        self.assertIn("不以建仓前历史高点推断", moving_check["detail"])

    def test_moving_stop_uses_only_completed_bars_after_position_date(self):
        bars = range_bars(38)
        bars.extend(
            [
                bar(38, 10.0, 20.0, 9.8, 10.2, 3000),
                bar(39, 10.8, 14.0, 10.5, 11.0, 2500),
                bar(40, 11.0, 13.0, 10.8, 11.2, 2400),
            ]
        )

        plan = evaluate_discipline_strategy(
            bars,
            100000,
            position={
                "symbol": "000001",
                "costPrice": 10.0,
                "shares": 1000,
                "createdAt": f"{bars[38]['date']}T10:00:00",
            },
            index_history=market_bull_bars(),
        )

        levels = plan["levels"]
        self.assertTrue(levels["movingStopActive"])
        self.assertEqual(levels["movingStopActivationDate"], bars[39]["date"])
        self.assertEqual(levels["recentHigh20"], 14.0)
        self.assertNotEqual(levels["recentHigh20"], 20.0)
        self.assertGreaterEqual(levels["movingStop"], levels["entryPrice"])

    def test_position_opened_on_latest_bar_keeps_trailing_stop_inactive(self):
        bars = range_bars(38)
        bars.extend(
            [
                bar(38, 10.0, 20.0, 9.8, 11.0, 3000),
                bar(39, 11.0, 18.0, 10.8, 11.2, 2500),
            ]
        )

        plan = evaluate_discipline_strategy(
            bars,
            100000,
            position={
                "symbol": "000001",
                "costPrice": 10.0,
                "shares": 1000,
                "createdAt": f"{bars[-1]['date']}T14:30:00",
            },
            index_history=market_bull_bars(),
        )

        self.assertFalse(plan["levels"]["movingStopActive"])
        self.assertIsNone(plan["levels"]["movingStop"])
        self.assertEqual(plan["levels"]["activeDefense"], plan["levels"]["hardStop"])

    def test_position_targets_and_add_price_respect_cost_and_direction(self):
        levels = _position_levels(
            defense_plan=None,
            upside_plan={"entryLimit": 10.8, "firstTarget": 11.2, "extensionTarget": 11.8},
            close=10.5,
            cost_price=10.6,
            structural_invalidation=0,
            structural_reference=9.8,
            active_defense=9.5,
            hard_stop=9.2,
            cost_stop=9.2,
            moving_stop=9.4,
            recent_high=12.0,
            rules=DEFAULT_RULES,
        )

        self.assertEqual(levels["plannedEntryPrice"], 10.8)
        self.assertGreater(levels["plannedEntryPrice"], levels["costPrice"])
        self.assertGreater(levels["firstTarget"], levels["costPrice"])
        self.assertGreater(levels["extensionTarget"], levels["firstTarget"])

        below_cost = _position_levels(
            defense_plan=None,
            upside_plan={"entryLimit": 10.5, "firstTarget": 10.55, "extensionTarget": 10.7},
            close=10.2,
            cost_price=10.6,
            structural_invalidation=0,
            structural_reference=9.8,
            active_defense=9.5,
            hard_stop=9.2,
            cost_stop=9.2,
            moving_stop=9.4,
            recent_high=12.0,
            rules=DEFAULT_RULES,
        )

        self.assertIsNone(below_cost["plannedEntryPrice"])
        self.assertGreater(below_cost["firstTarget"], below_cost["costPrice"])
        self.assertEqual(below_cost["takeProfitStatus"], "RISK_FALLBACK")
        self.assertIsNone(below_cost["extensionTarget"])

    def test_existing_position_always_gets_a_take_profit_from_an_upper_magnet(self):
        plan = evaluate_discipline_strategy(
            range_bars(),
            100000,
            position={"symbol": "000001", "costPrice": 10.0, "shares": 1000},
            index_history=market_bull_bars(),
        )

        levels = plan["levels"]
        self.assertGreater(levels["firstTarget"], levels["costPrice"])
        self.assertGreater(levels["firstTarget"], plan["metrics"]["latestClose"])
        self.assertEqual(levels["takeProfitStatus"], "STRUCTURE")
        self.assertIn("上方磁铁", levels["firstTargetSource"])

    def test_take_profit_uses_auditable_atr_risk_fallback_without_upper_levels(self):
        levels = _position_levels(
            defense_plan=None,
            upside_plan=None,
            close=10.0,
            cost_price=10.0,
            structural_invalidation=0,
            structural_reference=None,
            active_defense=9.0,
            hard_stop=9.0,
            cost_stop=9.2,
            moving_stop=None,
            recent_high=None,
            rules=DEFAULT_RULES,
            atr=0.5,
        )

        self.assertGreater(levels["firstTarget"], levels["costPrice"])
        self.assertGreater(levels["firstTarget"], 10.0)
        self.assertEqual(levels["takeProfitStatus"], "RISK_FALLBACK")
        self.assertIn("ATR/风险回报", levels["firstTargetSource"])

    def test_take_profit_does_not_use_a_resistance_below_cost(self):
        levels = _position_levels(
            defense_plan=None,
            upside_plan=None,
            close=9.8,
            cost_price=10.0,
            structural_invalidation=0,
            structural_reference=None,
            active_defense=9.0,
            hard_stop=9.0,
            cost_stop=9.2,
            moving_stop=None,
            recent_high=None,
            rules=DEFAULT_RULES,
            price_action_levels=[
                {"kind": "RANGE_HIGH", "role": "RESISTANCE_MAGNET", "price": 9.95, "source": "区间上沿"},
            ],
            atr=0.4,
        )

        self.assertGreater(levels["firstTarget"], levels["costPrice"])
        self.assertEqual(levels["takeProfitStatus"], "RISK_FALLBACK")

    def test_rejects_structure_stop_beyond_hard_risk_boundary(self):
        bars = range_bars()
        bars.extend([bar(38, 10.24, 10.82, 10.20, 10.68, 1800), bar(39, 10.39, 10.88, 10.25, 10.74, 1600)])

        plan = evaluate_discipline_strategy(bars, 100000, index_history=market_bull_bars())

        self.assertEqual(plan["action"], "WAIT")
        self.assertIn("结构止损超过硬风险边界，不能人为收紧止损", plan["blockedReasons"])

    def test_initial_stop_follows_structure_instead_of_cost_cap(self):
        stop, source, reason = _initial_position_stop(
            bars=range_bars(),
            close=31.55,
            cost_price=31.72,
            cost_stop=29.1824,
            atr=1.2,
            structural_invalidation=27.39,
            structural_plan=None,
            structural_reference=28.0,
            rules=DEFAULT_RULES,
        )

        self.assertEqual(stop, 27.39)
        self.assertEqual(source, "已确认结构失效位")
        self.assertIn("另一侧", reason)

    def test_atr_fallback_does_not_move_technical_stop_to_cost_risk_line(self):
        stop, source, reason = _initial_position_stop(
            bars=[],
            close=10.0,
            cost_price=10.0,
            cost_stop=9.2,
            atr=1.0,
            structural_invalidation=0,
            structural_plan=None,
            structural_reference=None,
            rules=DEFAULT_RULES,
        )

        self.assertEqual(stop, 8.0)
        self.assertEqual(source, "ATR波动后备")
        self.assertIn("账户风险退出线 9.20 另行检查", reason)

    def test_current_structure_stop_can_remain_above_cost(self):
        stop, source, _ = _initial_position_stop(
            bars=range_bars(),
            close=12.0,
            cost_price=10.0,
            cost_stop=9.2,
            atr=0.5,
            structural_invalidation=11.2,
            structural_plan=None,
            structural_reference=10.8,
            rules=DEFAULT_RULES,
        )

        self.assertEqual(stop, 11.2)
        self.assertEqual(source, "已确认结构失效位")
        self.assertGreater(stop, 10.0)

    def test_atr_fallback_uses_latest_completed_close_not_cost(self):
        stop, source, reason = _initial_position_stop(
            bars=[],
            close=15.0,
            cost_price=10.0,
            cost_stop=9.2,
            atr=1.0,
            structural_invalidation=0,
            structural_plan=None,
            structural_reference=None,
            rules=DEFAULT_RULES,
        )

        self.assertEqual(stop, 13.0)
        self.assertEqual(source, "ATR波动后备")
        self.assertIn("最近完整收盘价下方", reason)

    def test_moving_stop_uses_binance_style_breakeven_fallback_after_activation(self):
        state = _moving_stop_state(
            bars=[bar(0, 11.5, 12.0, 11.2, 12.0)],
            position={"symbol": "000001", "costPrice": 10.0, "shares": 100},
            cost_price=10.0,
            initial_stop=8.0,
            atr=1.0,
            close=12.0,
            rules=DEFAULT_RULES,
        )

        self.assertTrue(state["active"])
        self.assertEqual(state["stage"], "BREAKEVEN")
        self.assertEqual(state["movingStop"], 10.05)
        self.assertGreater(state["movingStop"], 10.0)

    def test_persisted_moving_stop_never_relaxes_after_peak_leaves_lookback(self):
        state = _moving_stop_state(
            bars=[bar(50, 11.0, 11.3, 10.8, 11.1)],
            position={
                "symbol": "000001",
                "costPrice": 10.0,
                "shares": 100,
                "createdAt": "2026-01-01T10:00:00",
                "trailingStop": 13.0,
                "trailingPeak": 15.0,
                "trailingPeakAt": "2026-01-30",
                "trailingActive": True,
                "trailingStage": "ATR_TRAILING",
                "trailingActivationPrice": 11.0,
                "trailingActivationAt": "2026-01-10",
            },
            cost_price=10.0,
            initial_stop=9.0,
            atr=1.0,
            close=11.1,
            rules=DEFAULT_RULES,
        )

        self.assertTrue(state["active"])
        self.assertEqual(state["stage"], "ATR_TRAILING")
        self.assertEqual(state["peakPrice"], 15.0)
        self.assertEqual(state["movingStop"], 13.0)

    def test_legacy_active_trailing_stop_without_peak_does_not_break_holding_analysis(self):
        plan = evaluate_discipline_strategy(
            range_bars(),
            100000,
            position={
                "symbol": "000001",
                "costPrice": 10.0,
                "shares": 1000,
                "createdAt": "2099-01-01T10:00:00",
                "trailingActive": True,
                "trailingStage": "BREAKEVEN",
                "trailingStop": 10.05,
            },
            index_history=market_bull_bars(),
        )

        self.assertTrue(plan["levels"]["movingStopActive"])
        self.assertIn("峰值数据暂缺", plan["levels"]["movingStopReason"])
        self.assertGreaterEqual(plan["levels"]["movingStop"], 10.05)

    def test_ema_is_not_used_as_a_structural_stop_boundary(self):
        support = _nearest_support(
            [
                {"kind": "EMA20", "role": "SUPPORT", "price": 9.8},
                {"kind": "SWING_LOW", "role": "SUPPORT_MAGNET", "price": 8.6},
                {"kind": "RANGE_LOW", "role": "SUPPORT_MAGNET", "price": 8.3},
            ],
            10.0,
        )

        self.assertEqual(support, 8.6)

    def test_position_levels_exposes_distinct_entry_technical_and_account_lines(self):
        levels = _position_levels(
            defense_plan=None,
            upside_plan={"status": "PENDING", "entryLimit": 12.5, "firstTarget": 14.0},
            close=12.0,
            cost_price=10.0,
            structural_invalidation=0,
            structural_reference=8.7,
            active_defense=8.5,
            hard_stop=8.5,
            cost_stop=9.2,
            moving_stop=None,
            recent_high=None,
            rules=DEFAULT_RULES,
            structure_boundary=8.7,
        )

        self.assertEqual(levels["entryPrice"], 10.0)
        self.assertEqual(levels["plannedEntryCandidate"], 12.5)
        self.assertEqual(levels["technicalStop"], 8.5)
        self.assertEqual(levels["accountRiskExit"], 9.2)
        self.assertEqual(levels["structureBoundary"], 8.7)
        self.assertNotEqual(levels["technicalStop"], levels["accountRiskExit"])

    def test_pending_bearish_anchor_does_not_become_active_defense(self):
        price_action = {
            "futurePlans": [
                {"direction": "SELL", "status": "PENDING", "anchorPrice": 9.8},
                {"direction": "BUY", "status": "CONFIRMED", "anchorPrice": 11.0},
            ]
        }

        self.assertIsNone(_latest_future_plan(price_action, "SELL", confirmed_only=True))
        self.assertEqual(_latest_future_plan(price_action, "SELL")["anchorPrice"], 9.8)
        self.assertEqual(_latest_future_plan(price_action, "BUY", confirmed_only=True)["anchorPrice"], 11.0)

    def test_pending_entry_is_visible_as_observation_but_not_executable(self):
        levels = _position_levels(
            defense_plan=None,
            upside_plan={"status": "PENDING", "entryLimit": 12.5, "firstTarget": 14.0},
            close=12.0,
            cost_price=10.0,
            structural_invalidation=0,
            structural_reference=9.4,
            active_defense=8.8,
            hard_stop=8.8,
            cost_stop=9.2,
            moving_stop=None,
            recent_high=None,
            rules=DEFAULT_RULES,
        )

        self.assertIsNone(levels["plannedEntryPrice"])
        self.assertEqual(levels["plannedEntryCandidate"], 12.5)
        self.assertEqual(levels["entryPlanStatus"], "PENDING")


if __name__ == "__main__":
    unittest.main()
