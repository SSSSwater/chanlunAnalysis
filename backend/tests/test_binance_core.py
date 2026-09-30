"""Small, deterministic Binance regression suite.

The application has a much larger surface than this file, but most historical
tests exercised the same branch repeatedly with only a changed direction or a
different literal.  Keep one test per externally important contract here and
leave detailed experiments to the model/backtest research pages.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import time
import unittest
from decimal import Decimal
from unittest.mock import call, patch
from urllib.error import HTTPError

from backend.services import binance_futures_client as futures_client
from backend.services import binance_futures_strategy as futures_strategy
from backend.services import binance_ml as ml
from backend.services import binance_simulated_portfolio as simulated_portfolio
from backend.services import binance_strategy_backtest as backtest


def _bar(open_price: float, high: float, low: float, close: float) -> dict[str, float]:
    return {
        "openTime": 1,
        "closeTime": 2,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def _position(**overrides):
    position = {
        "symbol": "TESTUSDT",
        "side": "LONG",
        "entryPrice": 100.0,
        "quantity": 1.0,
        "initialQuantity": 1.0,
        "margin": 1.0,
        "initialMargin": 1.0,
        "leverage": 1,
        "initialStop": 95.0,
        "positionRiskStop": 90.0,
        "targetOne": 106.0,
        "targetTwo": 110.0,
        "risk": 2.0,
        "peakPrice": 100.0,
        "movingStop": None,
        "protectedStop": 95.0,
        "activeStopSource": "STRUCTURE",
        "stopManagementStage": "INITIAL",
        "firstTargetReached": False,
        "secondTargetReached": False,
        "runner": False,
        "realizedPnl": 0.0,
        "currentPrice": 100.0,
        "openedAt": 0,
    }
    position.update(overrides)
    return position


def _portfolio():
    return {
        "cash": 0.0,
        "tradeCount": 0,
        "stoppedCount": 0,
        "firstTargetCount": 0,
        "secondTargetCount": 0,
    }


class BinanceCoreRegressionTests(unittest.TestCase):
    def test_plan_live_metrics_follow_account_snapshot_values(self):
        plan = {
            "currentPrice": 90.0,
            "latestPrice": 90.0,
            "unrealizedPnl": -10.0,
            "unrealizedPnlPercent": -10.0,
        }
        actual = {
            "markPrice": "101.23456",
            "lastPrice": "101.2",
            "unrealizedProfit": "0.1234",
            "roePercent": "3.4567",
        }

        synchronized = simulated_portfolio._sync_plan_live_metrics_from_account(plan, actual)

        self.assertEqual(synchronized["currentPrice"], 101.2346)
        self.assertEqual(synchronized["markPrice"], 101.2346)
        self.assertEqual(synchronized["latestPrice"], 101.2)
        self.assertEqual(synchronized["unrealizedPnl"], 0.1234)
        self.assertEqual(synchronized["unrealizedPnlPercent"], 3.4567)

    def test_model_scan_requests_the_full_causal_window_for_each_interval(self):
        requested = []
        now = int(time.time() * 1000)
        interval_ms = {"4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}

        def loader(network, symbol, interval, limit, with_meta=True):
            requested.append((interval, limit))
            step = interval_ms[interval]
            items = [
                {
                    "openTime": now - (400 - index + 1) * step,
                    "closeTime": now - (400 - index) * step,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 1.0,
                }
                for index in range(400)
            ]
            return {"items": items, "stale": False}

        with patch.object(futures_strategy, "get_futures_klines", side_effect=loader), patch.object(
            futures_strategy, "predict_binance_futures_frames", return_value={"direction": "WAIT"}
        ):
            futures_strategy._analyze_symbol(
                "FUTURES",
                "mainnet",
                {
                    "symbol": "AAAUSDT",
                    "lastPrice": 100.0,
                    "quoteVolume": 10_000_000,
                    "_includeModelFrames": True,
                    "_modelPredictionEnabled": True,
                    "_modelBranch": "BEST",
                    "_modelRunId": "run-1",
                },
                strategy_settings={"strategyEngine": "MODEL"},
            )

        self.assertEqual(
            dict(requested),
            {"4h": 96, "1h": 144, "15m": 176, "5m": 360},
        )

    def test_replay_retains_the_full_direct_model_5m_window(self):
        bars = [
            {
                "openTime": index * 300_000,
                "closeTime": (index + 1) * 300_000 - 1,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1.0,
            }
            for index in range(400)
        ]
        frame = backtest._frame(bars)

        retained = backtest._bars_until(frame, bars[-1]["closeTime"])

        self.assertEqual(len(retained), ml.WINDOWS["5m"])
        self.assertEqual(retained[0]["openTime"], bars[-ml.WINDOWS["5m"]]["openTime"])

    def test_classic_trigger_zone_bounds_keep_first_target_r_comparable(self):
        for side, stop, target in (("LONG", 98.0, 103.0), ("SHORT", 102.0, 97.0)):
            with self.subTest(side=side):
                low, high, geometry = futures_strategy._bound_trigger_zone_for_risk_reward(
                    80.0,
                    120.0,
                    trigger=100.0,
                    stop=stop,
                    first_target=target,
                    is_long=side == "LONG",
                )
                self.assertLess(high - low, 0.25)
                self.assertLessEqual(
                    geometry["firstTargetRSpread"],
                    geometry["maxFirstTargetRSpread"] + 1e-8,
                )

    def test_replay_path_is_adverse_first_for_both_directions(self):
        bar = _bar(100, 105, 90, 99)
        self.assertEqual(backtest._bar_path(bar, "LONG"), [100.0, 90.0, 105.0, 99.0])
        self.assertEqual(backtest._bar_path(bar, "SHORT"), [100.0, 105.0, 90.0, 99.0])

    def test_entry_path_discards_pre_trigger_prices_and_never_needs_one_minute_data(self):
        cases = (
            ("LONG", [_bar(99, 99, 94, 99), _bar(99, 101, 99, 100)]),
            ("SHORT", [_bar(101, 106, 101, 101), _bar(101, 101, 99, 100)]),
        )
        for side, bars in cases:
            with self.subTest(side=side):
                path = backtest._entry_path({"side": side, "trigger": 100.0}, bars)
                self.assertIsNotNone(path)
                self.assertEqual(path[0]["openTime"], bars[1]["openTime"])
                self.assertEqual(path[0]["close"], 100.0)
                self.assertEqual(path[0]["_replayPath"][0], 100.0)

    def test_position_path_processes_one_r_then_protective_stop_for_both_directions(self):
        for side, points in (("LONG", [100.0, 102.5, 100.0]), ("SHORT", [100.0, 97.5, 100.0])):
            with self.subTest(side=side):
                position = _position(
                    side=side,
                    initialStop=105.0 if side == "SHORT" else 95.0,
                    positionRiskStop=110.0 if side == "SHORT" else 90.0,
                    targetOne=94.0 if side == "SHORT" else 106.0,
                    targetTwo=90.0 if side == "SHORT" else 110.0,
                    protectedStop=105.0 if side == "SHORT" else 95.0,
                )
                actions = []
                backtest._process_position_path(position, points, {"atr": 1.0}, _portfolio(), actions)
                self.assertEqual(actions[-1]["type"], "STOP")
                self.assertTrue(any(item["type"] == "STOP_STAGE" for item in actions))

    def test_pending_entry_uses_one_following_bar_and_allows_missing_extension(self):
        plan = {
            "symbol": "TESTUSDT",
            "direction": "LONG",
            "entry": {"trigger": 100.0},
            "stopLoss": 95.0,
            "takeProfits": [{"price": 106.0}, {"price": None}],
        }
        candidate = backtest._pending_entry(plan, 1_000_000)
        self.assertEqual(candidate["expiresAt"], 1_000_000 + 15 * 60 * 1000)
        self.assertIsNone(candidate["targetTwo"])

    def test_backtest_options_keep_the_leverage_cap_and_model_selection(self):
        options = backtest._normalize_backtest_options({"maxLeverage": 20, "modelRunId": "saved-run"})
        self.assertEqual(options["maxLeverage"], 20)
        self.assertEqual(options["modelRunId"], "saved-run")
        self.assertEqual(options["modelEvaluationMode"], "OFF")

    def test_model_backtest_normalizes_legacy_classic_route_settings(self):
        settings = backtest._strategy_settings({
            "strategyEngine": "MODEL",
            "strategyMode": "SHORT_TERM",
            "levelStrategy": "CONFIRMED_PLATFORM",
            "entryConfirmationMode": "RETEST_REQUIRED",
            "entryConfirmationExpiryBars": 5,
        })
        self.assertEqual(settings["strategyMode"], "MIDLINE")
        self.assertEqual(settings["levelStrategy"], "STRUCTURE_EXTREME")
        self.assertEqual(settings["entryConfirmationMode"], "TRIGGER_ONLY")
        self.assertEqual(settings["entryConfirmationExpiryBars"], 1)

    def test_model_scan_bypasses_classic_analyzer_and_builds_5m_candidate(self):
        settings = {"strategyEngine": "MODEL", "modelBranch": "BEST", "modelRunId": "run-1"}
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.5,
                "entryZoneLowAtr": 0.2,
                "entryZoneHighAtr": 0.8,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 55.0,
                "targetTwoRatio": 35.0,
                "initialActivationR": 0.8,
                "breakevenTriggerR": 0.9,
                "structureLookbackBars": 12,
                "atrPeriod": 21,
                "atrMultiplier": 2.2,
            },
            reference_price=100.0,
            atr=2.0,
            symbol="AAAUSDT",
        )
        plan.update({"status": "ARMED", "modelTask": "DIRECT_PLAN", "modelGenerated": True, "entryTiming": {"pending": True}})
        history = {"symbols": ["AAAUSDT"], "items": {"AAAUSDT": {"quote": {"symbol": "AAAUSDT"}, "frames": {}}}}
        portfolio = {
            "positions": [],
            "_executionSettings": {
                "strategyMode": "MIDLINE",
                "initialBalance": 1000,
                "positionMarginFraction": 0.25,
                "maxManagedPositions": 2,
                "scanLimit": 1,
                "maxLeverage": 20,
                "leverageMode": "RISK_BUDGET",
                "leverage": None,
            },
        }
        frames = {interval: [] for interval in ("4h", "1h", "15m", "5m")}
        with patch.object(backtest, "_slice_top_turnover_symbols", return_value=[("AAAUSDT", 1_000_000)]), patch.object(
            backtest, "_analysis_frames", return_value=frames
        ), patch.object(backtest, "predict_binance_futures_frames", return_value=plan), patch.object(
            backtest, "analyze_futures_history", side_effect=AssertionError("classic analyzer must not run")
        ):
            candidates = backtest._scan_for_entries(
                history,
                {},
                portfolio,
                1_000,
                [],
                strategy_settings=settings,
                execution_interval="15m",
                model_selection=backtest.MODEL_SELECTION_FILTER,
                model_branch="BEST",
                model_run_id="run-1",
            )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["strategySettings"]["strategyMode"], "MIDLINE")
        self.assertEqual(candidates[0]["entryConfirmationRequired"], False)
        self.assertEqual(candidates[0]["atrTrailingPeriod"], 21)

    def test_direct_model_configuration_uses_four_training_intervals(self):
        config = ml._normalize_training_config({"inputProfile": "MACRO"})
        self.assertEqual(ml._input_intervals(config), ("4h", "1h", "15m", "5m"))

    def test_direct_decoder_preserves_short_geometry_and_ratio_budget(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "SHORT",
                "entryTriggerAtr": -0.5,
                "entryZoneLowAtr": -0.8,
                "entryZoneHighAtr": -0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 80,
                "targetTwoRatio": 80,
            },
            reference_price=100,
            atr=2,
        )
        self.assertEqual(plan["direction"], "SHORT")
        self.assertGreater(plan["stopLoss"], plan["entry"]["trigger"])
        self.assertGreater(plan["entry"]["trigger"], plan["takeProfits"][0]["price"])
        self.assertGreater(plan["takeProfits"][0]["price"], plan["takeProfits"][1]["price"])
        self.assertLessEqual(sum(item["ratio"] for item in plan["takeProfits"]), 100.0)

    def test_direct_decoder_returns_wait_for_invalid_model_output(self):
        plan = ml.decode_direct_plan_prediction({"direction": "WAIT"}, reference_price=100, atr=2)
        self.assertEqual(plan["direction"], "WAIT")
        self.assertEqual(plan["modelReason"], "MODEL_UNCERTAIN")
        self.assertIsNone(plan["entry"]["trigger"])
        self.assertEqual(plan["takeProfits"], [])

    def test_direct_model_armed_plan_uses_one_condition_contract(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.2,
                "entryZoneHighAtr": 0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
        )
        plan.update(
            modelTask="DIRECT_PLAN",
            modelGenerated=True,
            conditionMet=1,
            conditionTotal=1,
        )
        self.assertTrue(futures_strategy._plan_is_actionable(plan))

    def test_model_wait_is_not_replaced_by_classic_empty_recommendation(self):
        wait = {
            "symbol": "BTCUSDT",
            "strategyEngine": "MODEL",
            "modelTask": "DIRECT_PLAN",
            "direction": "WAIT",
            "status": "WAIT",
            "modelReason": "MODEL_BELOW_WAIT_BASELINE",
            "modelPrediction": {"selectionScore": -0.2, "waitScore": -0.1},
        }
        self.assertFalse(futures_strategy._plan_is_actionable(wait))
        self.assertEqual(futures_strategy._sort_key(wait)[2], -0.2)

    def test_capture_keeps_account_protective_ratio_when_plan_has_no_protective_target(self):
        account_settings = {
            **simulated_portfolio.db.default_binance_strategy_settings(),
            "protectiveTakeProfitRatio": 25.0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        plan = {
            "strategyEngine": "CLASSIC",
            "takeProfits": [
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
            # Older MODEL/CLASSIC clients could persist this sentinel even
            # though no PROTECTIVE_TARGET price existed.
            "protectiveTakeProfitRatio": 0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        with patch.object(
            simulated_portfolio.db,
            "get_user_binance_strategy_settings",
            return_value=account_settings,
        ):
            captured = simulated_portfolio._capture_execution_strategy_settings(17, plan)
        self.assertEqual(captured["strategySettings"]["protectiveTakeProfitRatio"], 25.0)
        self.assertEqual(captured["protectiveTakeProfitRatio"], 25.0)

    def test_capture_still_uses_plan_protective_ratio_when_target_exists(self):
        account_settings = simulated_portfolio.db.default_binance_strategy_settings()
        plan = {
            "strategyEngine": "CLASSIC",
            "takeProfits": [
                {"role": "PROTECTIVE_TARGET", "price": 103.0, "cumulativeRatio": 20.0},
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
            "protectiveTakeProfitRatio": 20.0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        with patch.object(
            simulated_portfolio.db,
            "get_user_binance_strategy_settings",
            return_value=account_settings,
        ):
            captured = simulated_portfolio._capture_execution_strategy_settings(17, plan)
        self.assertEqual(captured["strategySettings"]["protectiveTakeProfitRatio"], 20.0)

    def test_capture_treats_empty_protective_role_as_no_target(self):
        account_settings = simulated_portfolio.db.default_binance_strategy_settings()
        plan = {
            "strategyEngine": "MODEL",
            "takeProfits": [
                {"role": "PROTECTIVE_TARGET", "price": None, "available": False},
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
            "protectiveTakeProfitRatio": 0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        with patch.object(
            simulated_portfolio.db,
            "get_user_binance_strategy_settings",
            return_value=account_settings,
        ):
            captured = simulated_portfolio._capture_execution_strategy_settings(17, plan)
        self.assertEqual(captured["strategySettings"]["protectiveTakeProfitRatio"], 25.0)

    def test_two_target_plan_does_not_restore_a_phantom_protective_ratio(self):
        settings = {
            **simulated_portfolio.db.default_binance_strategy_settings(),
            "protectiveTakeProfitRatio": 25.0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        plan = {
            "strategyEngine": "MODEL",
            "strategySettings": settings,
            "takeProfits": [
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
        }

        protective, first, extension, protective_ratio, first_ratio, second_ratio = simulated_portfolio._plan_protection_targets(plan)

        self.assertIsNone(protective)
        self.assertEqual((first, extension), (106.0, 110.0))
        self.assertIsNone(protective_ratio)
        self.assertEqual((first_ratio, second_ratio), (50.0, 75.0))
        self.assertNotIn(
            "protective_take_profit_ratio",
            simulated_portfolio._plan_take_profit_ratio_kwargs(
                protective_ratio,
                first_ratio,
                second_ratio,
                has_protective_target=False,
            ),
        )

    def test_low_ratio_two_target_model_plan_can_be_created_without_protective_target(self):
        settings = simulated_portfolio.db.default_binance_strategy_settings()
        plan = {
            "strategyEngine": "MODEL",
            "modelTask": "DIRECT_PLAN",
            "strategySettings": settings,
            "takeProfits": [
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 5.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 10.0},
            ],
            "firstTakeProfitRatio": 5.0,
            "secondTakeProfitRatio": 10.0,
        }

        captured = simulated_portfolio._capture_execution_strategy_settings(17, plan)

        self.assertEqual(captured["firstTakeProfitRatio"], 5.0)
        self.assertEqual(captured["secondTakeProfitRatio"], 10.0)
        self.assertLess(captured["protectiveTakeProfitRatio"], 5.0)
        self.assertIsNone(simulated_portfolio._plan_protection_targets(captured)[0])

    def test_backtest_two_target_plan_has_no_protective_exit_slice(self):
        settings = {
            **backtest.db.default_binance_strategy_settings(),
            "protectiveTakeProfitRatio": 25.0,
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        targets = [
            {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
            {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
        ]

        self.assertEqual(backtest._target_cumulative_ratios(targets, settings), (None, 50.0, 75.0))
        position = _position(
            targetProtection=None,
            protectiveTakeProfitRatio=None,
            firstTakeProfitRatio=50.0,
            secondTakeProfitRatio=75.0,
            strategySettings=settings,
        )
        self.assertEqual(backtest._target_exit_quantity(position, protective=True), 0.0)
        self.assertEqual(backtest._target_exit_quantity(position, first=True), 0.5)

    def test_live_monitor_does_not_rebuild_targets_after_target_was_reached(self):
        """A consumed target ladder must stay immutable during refreshes."""

        position = {
            "id": 41,
            "marketMode": "FUTURES",
            "symbol": "TESTUSDT",
            "side": "LONG",
            "quantity": 1.0,
            "costPrice": 100.0,
            "dynamicPlan": {},
        }
        plan = {
            "activeStop": 95.0,
            "state": {"protectedStop": 95.0},
            "direction": "LONG",
            "firstTargetReached": True,
        }
        actual = {
            "symbol": "TESTUSDT",
            "side": "LONG",
            "positionSide": "BOTH",
            "quantity": 1.0,
            "entryPrice": 100.0,
            "markPrice": 100.0,
            "positionStopLoss": 95.0,
            # Simulate a post-target account snapshot: the filled target is
            # gone, but the live position remains open.
            "partialTakeProfitLevels": [],
            "protectionOrders": [],
            "protectionState": "CONFIRMED",
        }
        credentials = {"network": "mainnet", "apiKey": "key", "apiSecret": "secret"}
        with patch.object(simulated_portfolio, "get_cached_binance_account_snapshot", return_value={
            "stale": False,
            "account": {"futures": {"available": True, "positions": [actual]}},
        }), patch.object(simulated_portfolio, "_cached_real_futures_position", return_value=actual), patch.object(
            simulated_portfolio, "_load_user_credentials", return_value=credentials
        ), patch.object(simulated_portfolio.db, "update_binance_simulated_position_monitoring_metadata"), patch.object(
            simulated_portfolio, "_normalized_stop_values", return_value=(95.0, 95.0)
        ), patch.object(
            simulated_portfolio, "_auto_close_if_stop_crossed", return_value=None
        ), patch.object(simulated_portfolio, "_plan_protection_targets", return_value=(None, 106.0, 110.0, None, 50.0, 75.0)), patch.object(
            simulated_portfolio, "_apply_current_plan_take_profits", side_effect=AssertionError("target ladder must not be rebuilt")
        ):
            result = simulated_portfolio._auto_apply_plan_protection(41, position, plan)

        self.assertEqual(result["status"], "SYNCED")
        self.assertTrue(plan["initialTargetsApplied"])

    def test_target_pnl_snapshot_uses_incremental_creation_quantities(self):
        plan = {
            "takeProfits": [
                {"role": "PROTECTIVE_TARGET", "price": 101.0, "cumulativeRatio": 25.0},
                {"role": "FIRST_TARGET", "price": 106.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 110.0, "cumulativeRatio": 75.0},
            ],
            "strategySettings": {
                **backtest.db.default_binance_strategy_settings(),
                "protectiveTakeProfitRatio": 25.0,
                "firstTakeProfitRatio": 50.0,
                "secondTakeProfitRatio": 75.0,
            },
        }
        snapshot = simulated_portfolio._target_pnl_snapshot(
            plan,
            entry_price=100.0,
            quantity=10.0,
            side="LONG",
        )
        levels = snapshot["levels"]
        self.assertEqual(levels["PROTECTIVE_TARGET"]["quantity"], 2.5)
        self.assertEqual(levels["FIRST_TARGET"]["quantity"], 2.5)
        self.assertEqual(levels["EXTENSION_TARGET"]["quantity"], 2.5)
        self.assertEqual(levels["FIRST_TARGET"]["pnl"], 15.0)
        self.assertEqual(levels["EXTENSION_TARGET"]["pnl"], 25.0)

    def test_target_pnl_snapshot_handles_short_two_target_plan(self):
        plan = {
            "takeProfits": [
                {"role": "FIRST_TARGET", "price": 94.0, "cumulativeRatio": 50.0},
                {"role": "EXTENSION_TARGET", "price": 90.0, "cumulativeRatio": 75.0},
            ],
            "strategySettings": {
                **backtest.db.default_binance_strategy_settings(),
                "firstTakeProfitRatio": 50.0,
                "secondTakeProfitRatio": 75.0,
            },
        }
        snapshot = simulated_portfolio._target_pnl_snapshot(
            plan,
            entry_price=100.0,
            quantity=8.0,
            side="SHORT",
        )
        levels = snapshot["levels"]
        self.assertEqual(levels["FIRST_TARGET"]["quantity"], 4.0)
        self.assertEqual(levels["EXTENSION_TARGET"]["quantity"], 2.0)
        self.assertEqual(levels["FIRST_TARGET"]["pnl"], 24.0)
        self.assertEqual(levels["EXTENSION_TARGET"]["pnl"], 20.0)

    def test_target_pnl_snapshot_promotes_single_fixed_target_to_final_ratio(self):
        settings = {
            **backtest.db.default_binance_strategy_settings(),
            "firstTakeProfitRatio": 50.0,
            "secondTakeProfitRatio": 75.0,
        }
        snapshot = simulated_portfolio._target_pnl_snapshot(
            {"takeProfits": [{"price": 106.0, "cumulativeRatio": 50.0}], "strategySettings": settings},
            entry_price=100.0,
            quantity=10.0,
            side="LONG",
        )
        self.assertEqual(snapshot["levels"]["FIRST_TARGET"]["cumulativeRatio"], 75.0)
        self.assertEqual(snapshot["levels"]["FIRST_TARGET"]["quantity"], 7.5)

    def test_model_scan_keeps_direct_armed_plan_and_wait_recommendation(self):
        settings = {"strategyEngine": "MODEL", "modelBranch": "STABLE", "modelRunId": "run-1"}
        armed = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.2,
                "entryZoneHighAtr": 0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
            symbol="AAAUSDT",
        )
        armed.update(modelTask="DIRECT_PLAN", modelGenerated=True, conditionMet=1, conditionTotal=1)
        wait = {
            "symbol": "BBBUSDT",
            "strategyEngine": "MODEL",
            "modelTask": "DIRECT_PLAN",
            "modelGenerated": False,
            "direction": "WAIT",
            "status": "WAIT",
            "modelReason": "MODEL_BELOW_WAIT_BASELINE",
            "modelPrediction": {"selectionScore": -0.2, "waitScore": -0.1},
        }
        with patch.object(futures_strategy, "get_futures_markets", return_value={"items": [{"symbol": "AAAUSDT", "quoteVolume": 2_000_000}, {"symbol": "BBBUSDT", "quoteVolume": 1_000_000}], "stale": False}), patch.object(
            futures_strategy, "has_binance_ml_model", return_value=True
        ), patch.object(futures_strategy, "_analyze_contract", side_effect=[armed, wait]):
            result = futures_strategy.analyze_market("FUTURES", limit=2, strategy_settings=settings)
        self.assertEqual(result["recommendation"]["symbol"], "AAAUSDT")
        self.assertEqual(len(result["matchedPlans"]), 1)
        self.assertEqual(result["matchedPlans"][0]["conditionTotal"], 1)
        self.assertTrue(result["plans"][0]["executionEligible"])
        self.assertEqual(len(result["plans"]), 2)
        self.assertEqual(result["plans"][0]["symbol"], "AAAUSDT")
        self.assertEqual(result["plans"][1]["symbol"], "BBBUSDT")
        timings = result["marketScan"]["timings"]
        self.assertIn("totalWallMs", timings)
        self.assertIn("klineFetchWall", timings)
        self.assertEqual(timings["contractTotal"]["sampleCount"], 0)

        with patch.object(futures_strategy, "get_futures_markets", return_value={"items": [{"symbol": "BBBUSDT", "quoteVolume": 1_000_000}], "stale": False}), patch.object(
            futures_strategy, "has_binance_ml_model", return_value=True
        ), patch.object(futures_strategy, "_analyze_contract", return_value=wait):
            wait_result = futures_strategy.analyze_market("FUTURES", limit=1, strategy_settings=settings)
        self.assertEqual(wait_result["recommendation"]["symbol"], "BBBUSDT")
        self.assertEqual(wait_result["recommendation"]["modelReason"], "MODEL_BELOW_WAIT_BASELINE")
        self.assertEqual(wait_result["recommendation"]["direction"], "WAIT")

    def test_direct_plan_snapshot_keeps_the_same_execution_gate(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.2,
                "entryZoneHighAtr": 0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
            symbol="BTCUSDT",
        )
        plan.update(modelGenerated=True, status="ARMED")
        snapshot = simulated_portfolio._normalize_plan_snapshot(plan)
        self.assertTrue(futures_strategy._plan_is_actionable(snapshot))
        position = simulated_portfolio._normalize_position(
            {
                "network": "mainnet",
                "marketMode": "FUTURES",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quantity": 1,
                "costPrice": 100,
                "leverage": 1,
                "plan": plan,
            },
            require_plan=True,
        )
        self.assertEqual(position["plan"]["modelTask"], "DIRECT_PLAN")

    def test_direct_model_gate_rejects_trigger_already_crossed(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.2,
                "entryZoneHighAtr": 0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
            symbol="BTCUSDT",
        )
        plan.update(modelGenerated=True, status="ARMED", lastPrice=101.0)
        self.assertFalse(futures_strategy._plan_is_actionable(plan))
        plan["lastPrice"] = 99.0
        self.assertTrue(futures_strategy._plan_is_actionable(plan))

    def test_classic_execution_gate_accepts_numeric_json_scores(self):
        self.assertTrue(
            futures_strategy._plan_is_actionable(
                {"status": "ARMED", "conditionMet": "10.0", "conditionTotal": "10.0"}
            )
        )
        self.assertTrue(
            futures_strategy._plan_is_trial_eligible(
                {
                    "status": "TRIAL",
                    "trialEligible": True,
                    "conditionMet": "9.0",
                    "conditionTotal": "10.0",
                }
            )
        )

    def test_empty_model_scan_reports_model_data_unavailable(self):
        recommendation = futures_strategy._empty_model_recommendation(
            branch="STABLE",
            run_id="run-1",
        )
        self.assertEqual(recommendation["strategyEngine"], "MODEL")
        self.assertEqual(recommendation["direction"], "WAIT")
        self.assertEqual(recommendation["modelReason"], "MODEL_DATA_UNAVAILABLE")
        self.assertEqual(recommendation["modelBranch"], "STABLE")

    def test_model_run_resolution_never_falls_back_to_latest_checkpoint(self):
        selected = {"id": "selected", "network": "mainnet", "status": "COMPLETED"}
        with patch.object(ml.db, "get_binance_ml_training_run", return_value=selected) as get_run:
            self.assertEqual(ml._resolve_model_record("mainnet", "selected"), selected)
            self.assertIsNone(ml._resolve_model_record("testnet", "selected"))
        self.assertEqual(get_run.call_args_list, [call("selected"), call("selected")])

    def test_take_profit_ratios_are_normalized_for_first_only_and_two_targets(self):
        self.assertEqual(futures_client._plan_take_profit_ratios(40, 80), (40.0, 80.0))
        self.assertEqual(futures_client._plan_take_profit_ratios(40, 80, has_extension=False), (80.0, 80.0))

    def test_protection_price_validation_rejects_wrong_side(self):
        with self.assertRaises(ValueError):
            futures_client._validate_protection_price(
                Decimal("101"), Decimal("100"), True, is_stop=True, min_price=None, max_price=None
            )

    def test_futures_market_catalog_filters_to_trading_usdt_perpetuals(self):
        exchange_info = {
            "symbols": [
                {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "BTCUSD", "contractType": "PERPETUAL", "quoteAsset": "USD", "status": "TRADING"},
                {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "BREAK"},
            ]
        }
        tickers = [{"symbol": "BTCUSDT", "lastPrice": "60000", "quoteVolume": "20000000"}]
        with patch.object(futures_client, "_exchange_info", return_value=(exchange_info, False)), patch.object(
            futures_client, "_ticker_24h", return_value=(tickers, False)
        ), patch.object(futures_client, "_premium_index", return_value=([], False)):
            result = futures_client.get_futures_markets("mainnet")
        self.assertEqual([item["symbol"] for item in result["items"]], ["BTCUSDT"])

    def test_futures_market_catalog_is_ranked_by_24h_quote_volume(self):
        exchange_info = {
            "symbols": [
                {"symbol": "ZETAUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
                {"symbol": "ALPHAUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"},
            ]
        }
        tickers = [
            {"symbol": "ZETAUSDT", "lastPrice": "1", "quoteVolume": "10"},
            {"symbol": "ALPHAUSDT", "lastPrice": "1", "quoteVolume": "100"},
        ]
        with patch.object(futures_client, "_exchange_info", return_value=(exchange_info, False)), patch.object(
            futures_client, "_ticker_24h", return_value=(tickers, False)
        ), patch.object(futures_client, "_premium_index", return_value=([], False)):
            result = futures_client.get_futures_markets("mainnet")

        self.assertEqual([item["symbol"] for item in result["items"]], ["ALPHAUSDT", "ZETAUSDT"])

    def test_websocket_order_mode_uses_account_status_without_position_side_rpc(self):
        account_status = {"positions": [{"symbol": "BTCUSDT", "positionSide": "LONG"}]}
        with patch.object(futures_client, "_request", return_value=account_status) as request:
            with futures_client.websocket_api_requests(True):
                resolved = futures_client._resolve_futures_order_position_side(
                    "mainnet", "key", "secret", "LONG", None
                )

        self.assertEqual(resolved, "LONG")
        self.assertEqual(request.call_args.args[1], "/fapi/v2/account")

    def test_realized_pnl_is_aggregated_by_symbol_and_position_side(self):
        def fake_user_trades(_network, _api_key, _api_secret, *, symbol, limit=1000):
            self.assertEqual(limit, 1000)
            return [
                {"symbol": symbol, "positionSide": "LONG", "side": "BUY", "qty": "2", "time": 1, "id": 1, "realizedPnl": "0"},
                {"symbol": symbol, "positionSide": "LONG", "side": "SELL", "qty": "2", "time": 2, "id": 2, "realizedPnl": "9", "commission": "0.1", "commissionAsset": "USDT"},
                {"symbol": symbol, "positionSide": "LONG", "side": "BUY", "qty": "1", "time": 3, "id": 3, "realizedPnl": "0", "commission": "0.1", "commissionAsset": "USDT"},
                {"symbol": symbol, "positionSide": "LONG", "side": "SELL", "qty": "0.5", "time": 4, "id": 4, "realizedPnl": "1.25", "commission": "0.05", "commissionAsset": "USDT"},
                {"symbol": symbol, "positionSide": "SHORT", "side": "SELL", "qty": "2", "time": 5, "id": 5, "realizedPnl": "0"},
                {"symbol": symbol, "positionSide": "SHORT", "side": "BUY", "qty": "2", "time": 6, "id": 6, "realizedPnl": "-2"},
                {"symbol": symbol, "positionSide": "SHORT", "side": "SELL", "qty": "1", "time": 7, "id": 7, "realizedPnl": "0"},
                {"symbol": symbol, "positionSide": "SHORT", "side": "BUY", "qty": "0.5", "time": 8, "id": 8, "realizedPnl": "-0.5", "commission": "0.02", "commissionAsset": "USDT"},
            ]

        positions = [
            {"symbol": "TESTUSDT", "positionSide": "LONG", "positionAmt": "0.5"},
            {"symbol": "TESTUSDT", "positionSide": "SHORT", "positionAmt": "0.5"},
        ]
        with patch.object(futures_client, "get_futures_user_trades", side_effect=fake_user_trades):
            realized, failed = futures_client._realized_pnl_by_position("mainnet", "key", "secret", positions)

        self.assertEqual(failed, set())
        self.assertEqual(realized[("TESTUSDT", "LONG")], 1.1)
        self.assertEqual(realized[("TESTUSDT", "SHORT")], -0.52)

    def test_leverage_change_forces_rest_even_in_websocket_order_mode(self):
        captured = {}

        def fake_request(*args, **kwargs):
            captured["websocket_enabled"] = futures_client._use_websocket_api.get()
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"symbol": "BTCUSDT", "leverage": 10}

        with patch.object(futures_client, "_request", side_effect=fake_request):
            with futures_client.websocket_api_requests(True):
                result = futures_client._set_futures_leverage(
                    "mainnet",
                    "public-key",
                    "private-secret",
                    symbol="BTCUSDT",
                    leverage=10,
                )

        self.assertEqual(result, {"symbol": "BTCUSDT", "leverage": 10})
        self.assertFalse(captured["websocket_enabled"])
        self.assertEqual(captured["args"][1], "/fapi/v1/leverage")
        self.assertEqual(captured["kwargs"]["method"], "POST")

    def test_signed_request_keeps_signature_out_of_the_secret_and_preserves_rate_limit(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch.object(futures_client, "urlopen", side_effect=fake_urlopen), patch.object(
            futures_client, "_sync_server_time_if_stale"
        ):
            payload = futures_client._request(
                "mainnet",
                "/fapi/v1/order",
                {"timestamp": 1_700_000_000_000},
                api_key="public-key",
                api_secret="private-secret",
                signed=True,
            )
        self.assertEqual(payload, {"ok": True})
        query = captured["request"].full_url.split("?", 1)[1]
        signature_base, signature = query.rsplit("&signature=", 1)
        expected = hmac.new(b"private-secret", signature_base.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertNotIn("private-secret", captured["request"].full_url)

    def test_rate_limit_status_is_exposed_as_binance_api_error(self):
        def fake_urlopen(request, timeout):
            raise HTTPError(request.full_url, 429, "rate limited", {}, io.BytesIO(b'{"code":-1003,"msg":"busy"}'))

        with patch.object(futures_client, "urlopen", side_effect=fake_urlopen), self.assertRaises(
            futures_client.BinanceApiError
        ) as raised:
            futures_client._request("testnet", "/fapi/v1/aggTrades")
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.exchange_code, -1003)

    def test_public_market_reads_prefer_curl_proxy_transport(self):
        with patch.object(futures_client, "_curl_request_json", return_value=[[1, 2, 3]]) as curl_request, patch.object(
            futures_client, "urlopen"
        ) as urlopen:
            payload = futures_client._request("mainnet", "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "5m"})
        self.assertEqual(payload, [[1, 2, 3]])
        curl_request.assert_called_once()
        urlopen.assert_not_called()

    def test_public_cache_reuses_a_fresh_successful_read(self):
        key = ("test-fresh-cache",)
        futures_client._cache.pop(key, None)
        try:
            first, first_stale = futures_client._cached_read(key, lambda: {"value": 1})
            second, second_stale = futures_client._cached_read(
                key,
                lambda: self.fail("fresh cache unexpectedly called the network loader"),
            )
        finally:
            futures_client._cache.pop(key, None)
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertFalse(first_stale)
        self.assertFalse(second_stale)


if __name__ == "__main__":
    unittest.main()
