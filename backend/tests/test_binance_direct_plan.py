import unittest
from unittest.mock import patch

import numpy as np

from backend.services import binance_ml as ml


class DirectPlanTests(unittest.TestCase):
    @staticmethod
    def _bars(interval_ms, count, slope=0.08):
        start = 1_700_000_000_000
        return [
            {
                "openTime": start + index * interval_ms,
                "closeTime": start + (index + 1) * interval_ms - 1,
                "open": 100 + index * slope,
                "high": 100.2 + index * slope,
                "low": 99.8 + index * slope,
                "close": 100.1 + index * slope,
                "volume": 1000 + index,
            }
            for index in range(count)
        ]

    def test_direct_plan_emits_complete_long_contract_without_classic_plan(self):
        frames = {
            "4h": self._bars(4 * 60 * 60 * 1000, 40),
            "1h": self._bars(60 * 60 * 1000, 160),
            "15m": self._bars(15 * 60 * 1000, 640),
        }
        plan = ml._direct_plan_from_frames(frames)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["strategyEngine"], "MODEL")
        self.assertEqual(plan["modelTask"], ml.MODEL_TASK_TYPE)
        self.assertIn(plan["direction"], {"LONG", "SHORT", "WAIT"})
        if plan["direction"] != "WAIT":
            self.assertEqual(len(plan["takeProfits"]), 2)
            self.assertEqual(set(plan["trailingStop"]), {"INITIAL", "BREAKEVEN", "STRUCTURE_TRAILING", "ATR_TRAILING"})
            valid, reason = ml._validate_direct_plan(plan)
            self.assertTrue(valid, reason)

    def test_risk_coverage_rejects_when_validation_has_no_safe_point(self):
        config = ml._normalize_training_config({"selectionRule": "RISK_COVERAGE"})
        config.update({
            "selectionCalibrationStatus": "NO_SAFE_OPERATING_POINT",
            "calibratedSelectionFloor": -0.12,
            "calibratedMinimumFillProbability": 0.0,
            "calibratedMinimumTargetProbability": 0.0,
        })
        self.assertFalse(ml._selection_accepts_action(0.80, 0.20, 0.90, 0.90, config))
        self.assertFalse(ml._selection_accepts_action(-0.20, 0.20, 0.25, 0.10, config))

    def test_calibration_keeps_one_best_trade_per_market_state(self):
        rows = [
            {"group": 7, "direction": ml.DIRECTION_LONG, "score": 0.1},
            {"group": 7, "direction": ml.DIRECTION_SHORT, "score": 0.4},
            {"group": 8, "direction": ml.DIRECTION_WAIT, "score": 9.0},
            {"group": 8, "direction": ml.DIRECTION_LONG, "score": 0.2},
        ]
        selected = ml._one_action_per_state(rows)
        self.assertEqual([(row["group"], row["direction"]) for row in selected], [
            (7, ml.DIRECTION_SHORT),
            (8, ml.DIRECTION_LONG),
        ])

    def test_profit_win_truth_does_not_require_full_target_outcome(self):
        self.assertTrue(ml._profitable_realized_r(0.05))
        self.assertFalse(ml._profitable_realized_r(0.0))
        self.assertFalse(ml._profitable_realized_r(-0.01))

    def test_risk_coverage_calibration_keeps_negative_wait_margins(self):
        rows = [
            {
                "group": index,
                "direction": ml.DIRECTION_LONG,
                "score": -0.30,
                "waitScore": 0.0,
                "margin": -0.30,
                "fill": 0.8,
                "target": 0.8,
                "utility": 0.4,
                "drawdown": 0.2,
                "win": True,
            }
            for index in range(20)
        ]
        calibration = ml._calibrate_risk_coverage(rows, ml._normalize_training_config({}))
        self.assertLess(calibration["calibratedSelectionMargin"], 0.0)

    def test_stable_epoch_never_uses_an_invalid_neighbor(self):
        config = ml._normalize_training_config({})
        history = [
            {"epoch": 1, "validation": {"selectedCoverage": 0.0, "selectedWinRate": 0.0}},
            {"epoch": 2, "validation": {
                "selectedCoverage": 0.08,
                "selectedWinRate": 0.56,
                "selectedRealizedUtility": 0.5,
                "selectedMeanDrawdown": 0.2,
                "utilityCalibrationMae": 0.2,
                "selectionCalibration": {
                    "selectionCalibrationStatus": "LIMITED_SAFE_OPERATING_POINT",
                    "selectionCalibrationLowerBound": 0.48,
                },
            }},
            {"epoch": 3, "validation": {"selectedCoverage": 0.20, "selectedWinRate": 0.40}},
        ]
        self.assertEqual(ml._stable_checkpoint_epoch(history, 3, config), 2)

    def test_best_checkpoint_prioritizes_expected_realized_r_after_safety_gate(self):
        config = ml._normalize_training_config({
            "coverageTarget": 0.60,
            "coverageWeight": 0.85,
        })

        def metrics(*, coverage, realized_r, utility, drawdown, calibration_mae):
            return {
                "selectedCoverage": coverage,
                "selectedWinRate": 0.60,
                "selectedRealizedR": realized_r,
                "selectedRealizedUtility": utility,
                "selectedMeanDrawdown": drawdown,
                "utilityCalibrationMae": calibration_mae,
                "selectionCalibration": {
                    "selectionCalibrationStatus": "LIMITED_SAFE_OPERATING_POINT",
                    "selectionCalibrationLowerBound": 0.48,
                },
            }

        higher_return = metrics(
            coverage=0.10,
            realized_r=0.60,
            utility=0.20,
            drawdown=0.30,
            calibration_mae=0.20,
        )
        higher_coverage = metrics(
            coverage=0.60,
            realized_r=0.28,
            utility=0.75,
            drawdown=0.05,
            calibration_mae=0.03,
        )
        self.assertGreater(
            ml._direct_policy_expected_return_score(higher_return, config),
            ml._direct_policy_expected_return_score(higher_coverage, config),
        )
        self.assertGreater(
            ml._direct_policy_selection_score(higher_coverage, config),
            ml._direct_policy_selection_score(higher_return, config),
        )

    def test_best_checkpoint_rejects_an_unsafe_operating_point(self):
        metrics = {
            "selectedCoverage": 0.60,
            "selectedWinRate": 0.95,
            "selectedRealizedR": 2.0,
            "selectionCalibration": {
                "selectionCalibrationStatus": "NO_SAFE_OPERATING_POINT",
                "selectionCalibrationLowerBound": 0.95,
            },
        }
        self.assertEqual(ml._direct_policy_expected_return_score(metrics), float("-inf"))

    def test_checkpoint_rejects_validation_coverage_below_production_floor(self):
        config = ml._normalize_training_config({})
        metrics = {
            "selectedCoverage": 0.03,
            "selectedWinRate": 0.80,
            "selectedRealizedR": 1.0,
            "selectionCalibration": {
                "selectionCalibrationStatus": "LIMITED_SAFE_OPERATING_POINT",
                "selectionCalibrationLowerBound": 0.55,
            },
        }
        self.assertIsNone(ml._direct_policy_checkpoint_safety(metrics, config))

    def test_coverage_fallback_requires_side_support_and_rejects_wait_dominance(self):
        config = ml._normalize_training_config({"coverageFallbackEnabled": True})
        q = np.asarray([-0.20, 0.10, 0.30], dtype=np.float32)
        dd = np.asarray([0.10, 0.20], dtype=np.float32)
        self.assertTrue(ml._coverage_fallback_accepts_action(
            -0.05, 0.30, q, dd, 0.80, 0.20, 0.60, config,
            forecast_support=0.20, forecast_wait_probability=0.20,
        ))
        self.assertFalse(ml._coverage_fallback_accepts_action(
            -0.05, 0.30, q, dd, 0.80, 0.20, 0.60, config,
            forecast_support=0.01, forecast_wait_probability=0.90,
        ))

    def test_direct_plan_wait_is_safe_and_has_no_prices(self):
        frames = {
            "4h": self._bars(4 * 60 * 60 * 1000, 40, slope=0.0),
            "1h": self._bars(60 * 60 * 1000, 160, slope=0.0),
            "15m": self._bars(15 * 60 * 1000, 640, slope=0.0),
        }
        plan = ml._direct_plan_from_frames(frames)
        self.assertIsNotNone(plan)
        if plan["direction"] == "WAIT":
            self.assertEqual(plan["takeProfits"], [])
            self.assertIsNone(plan["stopLoss"])
            self.assertIsNone(plan["entry"]["trigger"])

    def test_holding_profile_owns_swing_and_position_label_semantics(self):
        swing = ml._normalize_training_config({"holdingProfile": "SWING", "labelHorizonBars": 32})
        position = ml._normalize_training_config({"holdingProfile": "POSITION", "labelHorizonBars": 32})
        self.assertEqual(swing["executionInterval"], "15m")
        self.assertEqual(swing["labelHorizonBars"], 480)
        self.assertEqual(position["executionInterval"], "1h")
        self.assertEqual(position["labelHorizonBars"], 336)
        self.assertLess(position["rewardTimePenalty"], swing["rewardTimePenalty"])

    def test_profileless_checkpoint_is_incompatible(self):
        metadata = {
            "modelVersion": ml.MODEL_VERSION,
            "datasetVersion": ml.DATASET_VERSION,
            "taskType": ml.MODEL_TASK_TYPE,
            "outputSchemaVersion": ml.DIRECT_PLAN_SCHEMA_VERSION,
            "trainingObjective": ml.TRAINING_OBJECTIVE,
            "entrySemantics": ml.ENTRY_SEMANTICS_VERSION,
            "referenceInterval": "15m",
            "executionInterval": "15m",
            "forecastOutputNames": list(ml.FORECAST_OUTPUT_NAMES),
            "forecastHorizonsBars": list(ml.FORECAST_HORIZONS),
            "forecastQuantiles": list(ml.FORECAST_QUANTILES),
            "planQualityOutputNames": list(ml.PLAN_QUALITY_OUTPUT_NAMES),
        }
        self.assertFalse(ml._is_compatible_model_metadata(metadata))

    def test_direct_teacher_uses_causal_15m_atr_for_fallback_plan(self):
        frames = {
            "4h": self._bars(4 * 60 * 60 * 1000, 40),
            "1h": self._bars(60 * 60 * 1000, 160),
            "15m": self._bars(15 * 60 * 1000, 640),
        }
        atr = ml._direct_15m_atr_from_frames(frames)
        self.assertIsNotNone(atr)
        plan = ml._direct_plan_from_frames(frames)
        self.assertIsNotNone(plan)
        self.assertNotEqual(plan["direction"], "WAIT")
        target, _ = ml._direct_teacher_target(plan, {"durationBars": 1}, atr)
        # The fallback plan has no classic ``timeframes`` payload.  Its
        # teacher distances must still be expressed in real 15m ATR units,
        # not the 0.06% price-fraction fallback.
        self.assertLess(float(target[3]), 10.0)
        self.assertLess(float(target[5]), 15.0)

    def test_normalized_decoder_keeps_short_geometry_and_ratio_budget(self):
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

    def test_decoder_bounds_trigger_zone_risk_reward_spread(self):
        for direction in ("LONG", "SHORT"):
            with self.subTest(direction=direction):
                plan = ml.decode_direct_plan_prediction(
                    {
                        "direction": direction,
                        "entryTriggerAtr": 0.0,
                        "entryZoneLowAtr": -6.0,
                        "entryZoneHighAtr": 6.0,
                        "stopDistanceAtr": 1.0,
                        "targetOneDistanceAtr": 1.5,
                        "targetTwoDistanceAtr": 2.5,
                        "targetOneRatio": 50.0,
                        "targetTwoRatio": 40.0,
                    },
                    reference_price=100.0,
                    atr=2.0,
                )
                geometry = plan["entry"]["zoneGeometry"]
                self.assertLessEqual(geometry["spanAtr"], 0.12 + 1e-8)
                self.assertLessEqual(
                    geometry["firstTargetRSpread"],
                    geometry["maxFirstTargetRSpread"] + 1e-8,
                )

    def test_policy_action_clip_uses_same_bounded_entry_zone(self):
        names = {name: index for index, name in enumerate(ml.DIRECT_PLAN_OUTPUT_NAMES[1:])}
        action = np.zeros(len(names), dtype=np.float32)
        action[names["entryTriggerAtr"]] = 0.5
        action[names["entryZoneLowAtr"]] = -6.0
        action[names["entryZoneHighAtr"]] = 6.0
        action[names["stopDistanceAtr"]] = 1.0
        action[names["targetOneDistanceAtr"]] = 1.5
        action[names["targetTwoDistanceAtr"]] = 2.5
        clipped = ml._clip_policy_action(action)
        span = clipped[names["entryZoneHighAtr"]] - clipped[names["entryZoneLowAtr"]]
        self.assertLessEqual(float(span), 0.12 + 1e-8)

    def test_decoder_preserves_all_four_trigger_geometries(self):
        base = {
            "entryZoneLowAtr": -0.8,
            "entryZoneHighAtr": 0.8,
            "stopDistanceAtr": 1.0,
            "targetOneDistanceAtr": 1.5,
            "targetTwoDistanceAtr": 2.5,
            "targetOneRatio": 50.0,
            "targetTwoRatio": 40.0,
        }
        cases = (
            ("LONG", 0.5, "STOP"),
            ("LONG", -0.5, "LIMIT"),
            ("SHORT", -0.5, "STOP"),
            ("SHORT", 0.5, "LIMIT"),
        )
        for direction, trigger, order_type in cases:
            with self.subTest(direction=direction, trigger=trigger):
                plan = ml.decode_direct_plan_prediction(
                    {**base, "direction": direction, "entryTriggerAtr": trigger},
                    reference_price=100,
                    atr=2,
                )
                self.assertEqual(plan["entry"]["orderType"], order_type)

    def test_retrieval_candidates_are_taken_from_nearest_training_state(self):
        features = np.zeros((2, 4, ml.FEATURE_DIM), dtype=np.float32)
        features[0] += 0.1
        features[1] += 2.0
        actions = np.zeros((2, len(ml.DIRECT_PLAN_OUTPUT_NAMES) - 1), dtype=np.float32)
        trigger_index = list(ml.DIRECT_PLAN_OUTPUT_NAMES[1:]).index("entryTriggerAtr")
        actions[0, trigger_index] = -0.5
        actions[1, trigger_index] = 0.5
        loaded = {
            "retrievalBank": {
                "features": features,
                "actions": actions,
                "directions": np.asarray([ml.DIRECTION_LONG, ml.DIRECTION_SHORT]),
            }
        }
        result = ml._retrieval_guided_actions(features[0:1], loaded, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], ml.DIRECTION_LONG)
        self.assertAlmostEqual(float(result[0][1][trigger_index]), -0.5)

    def test_direct_entry_state_allows_approach_from_safe_side_but_blocks_crossed_trigger(self):
        long_plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.5,
                "entryZoneLowAtr": 0.2,
                "entryZoneHighAtr": 0.8,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
        )
        below = {**long_plan, "lastPrice": 99.0}
        crossed = {**long_plan, "lastPrice": 102.0}
        self.assertTrue(ml._direct_model_entry_state(below)["pending"])
        self.assertEqual(ml._direct_model_entry_state(below)["state"], "WAITING_BELOW_ZONE")
        self.assertFalse(ml._direct_model_entry_state(crossed)["pending"])
        self.assertEqual(ml._direct_model_entry_state(crossed)["reason"], "MODEL_TRIGGER_ALREADY_CROSSED")

        short_plan = ml.decode_direct_plan_prediction(
            {
                "direction": "SHORT",
                "entryTriggerAtr": -0.5,
                "entryZoneLowAtr": -0.8,
                "entryZoneHighAtr": -0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
        )
        self.assertTrue(ml._direct_model_entry_state({**short_plan, "lastPrice": 101.0})["pending"])
        self.assertFalse(ml._direct_model_entry_state({**short_plan, "lastPrice": 98.0})["pending"])

    def test_direct_entry_state_rejects_a_completed_bar_that_already_crossed(self):
        short_plan = ml.decode_direct_plan_prediction(
            {
                "direction": "SHORT",
                "entryTriggerAtr": -0.5,
                "entryZoneLowAtr": -0.8,
                "entryZoneHighAtr": -0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
        )
        # The quote has rebounded above the trigger, but the last closed bar
        # traded through it.  A stop-trigger plan cannot be re-armed after
        # that historical crossing.
        state = ml._direct_model_entry_state(
            {**short_plan, "lastPrice": 101.0},
            observed_bars=[{"high": 102.0, "low": 98.5}],
        )
        self.assertFalse(state["pending"])
        self.assertTrue(state["latestBarCrossed"])

    def test_action_entry_point_must_be_outside_current_candle(self):
        names = {name: index for index, name in enumerate(ml.DIRECT_PLAN_OUTPUT_NAMES[1:])}
        inside = np.zeros(len(names), dtype=np.float32)
        inside[names["entryTriggerAtr"]] = 0.25
        outside = inside.copy()
        outside[names["entryTriggerAtr"]] = 1.0
        self.assertFalse(ml._entry_point_is_pending(
            inside, ml.DIRECTION_LONG, reference_price=100.0,
            current_high=101.0, current_low=99.5, atr=2.0,
        ))
        self.assertTrue(ml._entry_point_is_pending(
            outside, ml.DIRECTION_LONG, reference_price=100.0,
            current_high=101.0, current_low=99.5, atr=2.0,
        ))
        outside[names["entryTriggerAtr"]] = -1.0
        self.assertTrue(ml._entry_point_is_pending(
            outside, ml.DIRECTION_SHORT, reference_price=100.0,
            current_high=101.0, current_low=99.5, atr=2.0,
        ))

    def test_coverage_fallback_rearms_crossed_trigger_without_changing_geometry(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "SHORT",
                "entryTriggerAtr": -0.3,
                "entryZoneLowAtr": -0.6,
                "entryZoneHighAtr": 0.0,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 40.0,
            },
            reference_price=100,
            atr=2,
        )
        rearmed = ml._rearm_direct_model_trigger(
            plan,
            current_price=100,
            latest_bar={"high": 101.0, "low": 98.0},
            atr=2,
        )
        self.assertTrue(rearmed.get("entryRearmed"))
        self.assertLess(rearmed["entry"]["trigger"], 98.0)
        self.assertAlmostEqual(
            rearmed["entry"]["trigger"] - plan["entry"]["trigger"],
            rearmed["stopLoss"] - plan["stopLoss"],
        )

    def test_normalized_decoder_accepts_wait_without_exposing_prices(self):
        plan = ml.decode_direct_plan_prediction(
            {"direction": "WAIT"},
            reference_price=100,
            atr=2,
        )
        self.assertEqual(plan["direction"], "WAIT")
        self.assertEqual(plan["modelReason"], "MODEL_UNCERTAIN")
        self.assertIsNone(plan["entry"]["trigger"])
        self.assertEqual(plan["takeProfits"], [])

    def test_normalized_decoder_exposes_dynamic_plan_risk_reward(self):
        common = {
            "direction": "LONG",
            "entryTriggerAtr": 0.0,
            "entryZoneLowAtr": -0.4,
            "entryZoneHighAtr": 0.4,
            "stopDistanceAtr": 1.0,
            "targetOneRatio": 50.0,
            "targetTwoRatio": 40.0,
        }
        first = ml.decode_direct_plan_prediction(
            {**common, "targetOneDistanceAtr": 1.5, "targetTwoDistanceAtr": 2.5},
            reference_price=100,
            atr=2,
        )
        second = ml.decode_direct_plan_prediction(
            {**common, "targetOneDistanceAtr": 3.0, "targetTwoDistanceAtr": 5.0},
            reference_price=100,
            atr=2,
        )
        self.assertEqual(first["riskReward"], first["takeProfits"][0]["rMultiple"])
        self.assertEqual(second["riskReward"], second["takeProfits"][0]["rMultiple"])
        self.assertNotEqual(first["riskReward"], second["riskReward"])

    def test_model_target_ratio_preserves_model_allocation(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.2,
                "entryZoneHighAtr": 0.2,
                "stopDistanceAtr": 1.0,
                "targetOneDistanceAtr": 1.5,
                "targetTwoDistanceAtr": 2.5,
                "targetOneRatio": 16.0,
                "targetTwoRatio": 16.0,
            },
            reference_price=100,
            atr=2,
        )
        targets = plan["takeProfits"]
        self.assertEqual(targets[0]["cumulativeRatio"], 16.0)
        self.assertEqual(targets[1]["cumulativeRatio"], 32.0)
        self.assertEqual(targets[1]["ratio"], 16.0)

    def test_v16_policy_action_ratio_bounds_are_not_legacy_sixteen_percent_cap(self):
        names = ml.DIRECT_PLAN_OUTPUT_NAMES[1:]
        action = [0.0] * len(names)
        action[names.index("targetOneRatio")] = 38.0
        action[names.index("targetTwoRatio")] = 47.0
        clipped = ml._clip_policy_action(action)
        self.assertGreater(float(clipped[names.index("targetOneRatio")]), 16.0)
        self.assertGreater(float(clipped[names.index("targetTwoRatio")]), 16.0)
        self.assertLessEqual(
            float(clipped[names.index("targetOneRatio")] + clipped[names.index("targetTwoRatio")]),
            ml.MAX_MODEL_FIXED_TARGET_RATIO + 1e-5,
        )

    def test_v16_proposal_search_varies_target_allocation(self):
        names = ml.DIRECT_PLAN_OUTPUT_NAMES[1:]
        base = ml._clip_policy_action([0.0] * len(names))
        proposals = ml._proposal_actions(base, 8)
        allocations = {
            (
                round(float(item[names.index("targetOneRatio")]), 3),
                round(float(item[names.index("targetTwoRatio")]), 3),
            )
            for item in proposals
        }
        self.assertGreaterEqual(len(allocations), 4)

    def test_normalized_decoder_uses_positive_distance_aware_time_cost(self):
        plan = ml.decode_direct_plan_prediction(
            {
                "direction": "LONG",
                "entryTriggerAtr": 0.0,
                "entryZoneLowAtr": -0.4,
                "entryZoneHighAtr": 0.4,
                "stopDistanceAtr": 2.0,
                "targetOneDistanceAtr": 4.0,
                "targetTwoDistanceAtr": 7.0,
                "targetOneRatio": 50.0,
                "targetTwoRatio": 50.0,
                "tp1Bars": 0.0,
                "tp2Bars": 0.0,
                "exitBars": 0.0,
                "timeEfficiency": 0.0,
                "holdingCost": 0.0,
            },
            reference_price=100,
            atr=2,
        )
        time_cost = plan["timeCost"]
        self.assertGreaterEqual(time_cost["tp1Bars"], 2.0)
        self.assertGreater(time_cost["tp2Bars"], time_cost["tp1Bars"])
        self.assertGreater(time_cost["exitBars"], time_cost["tp2Bars"])
        self.assertLess(time_cost["holdingCost"], 0.0)
        self.assertGreater(time_cost["efficiency"], 0.0)

    def test_model_output_shape_error_is_wait_instead_of_geometry_fallback(self):
        frames = {
            "4h": self._bars(4 * 60 * 60 * 1000, 40),
            "1h": self._bars(60 * 60 * 1000, 160),
            "15m": self._bars(15 * 60 * 1000, 640),
        }
        loaded = {"config": dict(ml.DEFAULT_TRAINING_CONFIG), "metadata": {"modelVersion": ml.MODEL_VERSION}}
        with patch.object(ml, "_predict_candidate_batch", return_value=[{"directDirection": [1.0], "directPlan": [0.0]}]):
            plan = ml._direct_plan_with_model(frames, loaded)
        self.assertEqual(plan["direction"], "WAIT")
        self.assertEqual(plan["modelReason"], "MODEL_INVALID")
        self.assertFalse(plan["modelGenerated"])

    def test_action_replay_keeps_wait_and_adverse_stop_outcomes_without_one_minute(self):
        self.assertEqual(ml.MACRO_INPUT_INTERVALS, ("4h", "1h", "15m", "5m"))
        self.assertNotIn("1m", ml.MACRO_INPUT_INTERVALS)
        names = ml.DIRECT_PLAN_OUTPUT_NAMES[1:]
        indexes = {name: index for index, name in enumerate(names)}
        action = np.zeros(len(names), dtype="float32")
        for name, value in {
            "entryTriggerAtr": 0.0,
            "entryZoneLowAtr": -0.1,
            "entryZoneHighAtr": 0.1,
            "stopDistanceAtr": 0.5,
            "targetOneDistanceAtr": 1.0,
            "targetTwoDistanceAtr": 2.0,
            "targetOneRatio": 50.0,
            "targetTwoRatio": 40.0,
            "initialActivationR": 1.0,
            "breakevenTriggerR": 1.0,
            "structureLookbackBars": 5.0,
            "atrPeriod": 14.0,
            "atrMultiplier": 2.0,
            "tp1Bars": 1.0,
            "tp2Bars": 2.0,
            "exitBars": 4.0,
        }.items():
            action[indexes[name]] = value
        frame = {
            "open": np.array([100.0, 100.0]),
            "high": np.array([100.1, 102.0]),
            "low": np.array([99.9, 99.0]),
            "close": np.array([100.0, 101.0]),
            "atr14": np.array([1.0, 1.0]),
        }
        outcome = ml._legacy._direct_policy_fast_replay(
            frame,
            0,
            "LONG",
            action,
            {"labelHorizonBars": 1, "entryExpiryBars": 1},
        )
        self.assertTrue(outcome["entered"])
        self.assertEqual(outcome["outcome"], "STOP")
        self.assertLess(outcome["realizedR"], 0.0)

    def test_training_sampler_keeps_both_trigger_sides_for_each_side(self):
        rng = np.random.default_rng(1234)
        trigger_index = list(ml.DIRECT_PLAN_OUTPUT_NAMES[1:]).index("entryTriggerAtr")
        long_actions = [ml._sample_direct_policy_action(rng, "LONG") for _ in range(24)]
        short_actions = [ml._sample_direct_policy_action(rng, "SHORT") for _ in range(24)]
        # Training keeps both geometries; direction/sign consistency is
        # evaluated by replay rather than used to discard half the actions.
        self.assertTrue(any(float(action[trigger_index]) > 0.0 for action in long_actions))
        self.assertTrue(any(float(action[trigger_index]) < 0.0 for action in long_actions))
        self.assertTrue(any(float(action[trigger_index]) > 0.0 for action in short_actions))
        self.assertTrue(any(float(action[trigger_index]) < 0.0 for action in short_actions))

    def test_training_pending_filter_uses_anchor_close_side_without_extra_wick_filter(self):
        names = list(ml.DIRECT_PLAN_OUTPUT_NAMES[1:])
        trigger_index = names.index("entryTriggerAtr")
        long_action = np.zeros(len(names), dtype=np.float32)
        long_action[trigger_index] = 0.5
        short_action = np.zeros(len(names), dtype=np.float32)
        short_action[trigger_index] = -0.5
        base = {
            "open": np.asarray([100.0]),
            "close": np.asarray([100.0]),
            "atr14": np.asarray([2.0]),
        }
        long_safe = {**base, "high": np.asarray([100.5]), "low": np.asarray([99.5])}
        long_crossed = {**base, "high": np.asarray([101.1]), "low": np.asarray([99.5])}
        short_safe = {**base, "high": np.asarray([100.5]), "low": np.asarray([99.5])}
        short_crossed = {**base, "high": np.asarray([100.5]), "low": np.asarray([98.9])}
        self.assertTrue(ml._pending_action_at_anchor(long_safe, 0, "LONG", long_action))
        # A wick through the level is intentionally not an additional
        # training-side rejection; the pending contract is defined relative
        # to the completed anchor close and the future replay starts after it.
        self.assertTrue(ml._pending_action_at_anchor(long_crossed, 0, "LONG", long_action))
        self.assertTrue(ml._pending_action_at_anchor(short_safe, 0, "SHORT", short_action))
        self.assertTrue(ml._pending_action_at_anchor(short_crossed, 0, "SHORT", short_action))

    def test_old_entry_semantics_checkpoint_is_incompatible(self):
        metadata = {
            "modelVersion": ml.MODEL_VERSION,
            "datasetVersion": ml.DATASET_VERSION,
            "taskType": ml.MODEL_TASK_TYPE,
            "outputSchemaVersion": ml.DIRECT_PLAN_SCHEMA_VERSION,
            "trainingObjective": ml.TRAINING_OBJECTIVE,
            "referenceInterval": ml.MODEL_REFERENCE_INTERVAL,
            "executionInterval": ml.MODEL_EXECUTION_INTERVAL,
        }
        self.assertFalse(ml._is_compatible_model_metadata(metadata))
        metadata["entrySemantics"] = ml.ENTRY_SEMANTICS_VERSION
        metadata["holdingProfile"] = "SWING"
        metadata["holdingProfileVersion"] = ml.HOLDING_PROFILE_VERSION
        metadata["forecastOutputNames"] = list(ml.FORECAST_OUTPUT_NAMES)
        metadata["forecastHorizonsBars"] = list(ml.FORECAST_HORIZONS)
        metadata["forecastQuantiles"] = list(ml.FORECAST_QUANTILES)
        metadata["planQualityOutputNames"] = list(ml.PLAN_QUALITY_OUTPUT_NAMES)
        metadata["profitCalibrationTarget"] = "REALIZED_R_GT_ZERO"
        metadata["productionReady"] = True
        self.assertTrue(ml._is_compatible_model_metadata(metadata))

    def test_multihorizon_forecast_targets_are_causal_and_directional(self):
        frame = {
            "close": np.asarray([100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
            "high": np.asarray([100.2, 101.5, 102.5, 103.5, 104.5, 105.5]),
            "low": np.asarray([99.8, 100.5, 101.5, 102.5, 103.5, 104.5]),
        }
        values, direction = ml._forecast_targets_at_anchor(frame, 0)
        self.assertEqual(values.shape, (len(ml.FORECAST_OUTPUT_NAMES),))
        self.assertEqual(direction, ml.DIRECTION_LONG)
        q10 = values[ml.FORECAST_OUTPUT_NAMES.index("return_16xexecution_q10")]
        median = values[ml.FORECAST_OUTPUT_NAMES.index("return_16xexecution_q50")]
        q90 = values[ml.FORECAST_OUTPUT_NAMES.index("return_16xexecution_q90")]
        self.assertLessEqual(float(q10), float(median))
        self.assertLessEqual(float(median), float(q90))


if __name__ == "__main__":
    unittest.main()
