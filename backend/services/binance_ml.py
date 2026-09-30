"""v30 forecast-conditioned, decision-focused market-to-plan policy service.

The previous implementation is loaded from the local bytecode snapshot only
as a compatibility surface while this source module replaces its training and
    serving entry points.  v30 never creates or consumes classic-plan teachers
    and trains on the full replay action space at the completed 5m anchor close.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
from threading import Lock, Thread
import time
from typing import Any
from uuid import uuid4

import numpy as np


def _load_legacy_module():
    """Load the pre-v19 implementation as a compatibility/helper module."""

    cache_dir = Path(__file__).with_name("__pycache__")
    candidates = sorted(cache_dir.glob("binance_ml.cpython-*.pyc"), key=lambda item: item.stat().st_size, reverse=True)
    import sys
    current_tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    # Never load this source file's own cache recursively.  A cache from an
    # older compatible implementation is optional; the shim below is complete
    # enough to run when none exists.
    candidates = [item for item in candidates if current_tag not in item.name]
    for candidate in candidates:
        name = "backend.services._binance_ml_v14_compat" if __package__ else "_binance_ml_v14_compat"
        try:
            spec = importlib.util.spec_from_file_location(name, candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            import sys
            sys.modules[name] = module
            spec.loader.exec_module(module)
            if hasattr(module, "_direct_policy_fast_replay"):
                return module
        except (ImportError, ValueError, RuntimeError):
            continue
    raise ImportError("缺少可兼容的 Binance ML 实现")


try:
    _legacy = _load_legacy_module()
except ImportError:
    # The source file was historically generated outside Git.  A small
    # compatibility shim keeps the service recoverable when bytecode from a
    # different Python minor version is present or the cache is cleaned.
    _legacy = None

_using_shim = _legacy is None
if _legacy is None:
    import types
    try:
        from . import database as _shim_db
    except ImportError:  # pragma: no cover
        import database as _shim_db
    _legacy = types.SimpleNamespace()
    _legacy.db = _shim_db
    _legacy.INTERVAL_MS = {"1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}
    _legacy.FEATURE_NAMES = ("log_return", "body_return", "range_return", "upper_wick_ratio", "lower_wick_ratio", "volume_log_deviation", "ema20_distance_atr", "atr_ratio", "momentum_4", "close_position", "ema20_ema50_gap_atr", "ema20_slope_atr", "range_location_20", "overlap_ratio_5", "directional_efficiency_8", "volume_zscore_20", "momentum_12", "momentum_24", "momentum_acceleration_4", "atr_short_long_ratio", "signed_directional_efficiency_8", "distance_to_high_20_atr", "distance_to_low_20_atr", "signed_high_volume_pressure")
    _legacy.FEATURE_DIM = len(_legacy.FEATURE_NAMES)
    _legacy.DIRECT_PLAN_OUTPUT_NAMES = ("direction", "entryTriggerAtr", "entryZoneLowAtr", "entryZoneHighAtr", "stopDistanceAtr", "targetOneDistanceAtr", "targetTwoDistanceAtr", "targetOneRatio", "targetTwoRatio", "initialActivationR", "breakevenTriggerR", "breakevenBufferAtr", "structureLookbackBars", "structureBufferAtr", "atrPeriod", "atrMultiplier", "tp1Bars", "tp2Bars", "exitBars", "timeEfficiency", "holdingCost")
    _legacy.DIRECTION_WAIT, _legacy.DIRECTION_LONG, _legacy.DIRECTION_SHORT = 0, 1, 2
    _legacy.DIRECTION_LABELS = ("WAIT", "LONG", "SHORT")
    _legacy.OUTCOME_STOP, _legacy.OUTCOME_EXPIRE, _legacy.OUTCOME_TARGET = 0, 1, 2
    _legacy.OUTCOME_LABELS = ("STOP", "EXPIRE", "TARGET")
    _legacy.MIN_RISK_FRACTION = 0.0006
    _legacy.FIXED_HISTORY_START_TIME = int(datetime(2026, 5, 31, 16, tzinfo=timezone.utc).timestamp() * 1000)
    _legacy.FIXED_HISTORY_END_TIME = int(datetime(2026, 9, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _legacy.DEFAULT_TRAINING_CONFIG = {"epochs": 25, "batchSize": 128, "learningRate": 0.0003, "weightDecay": 0.0001, "dModel": 64, "numHeads": 4, "numLayers": 2, "dropout": 0.1, "inputProfile": "MULTI_TIMEFRAME", "seed": 20260916, "device": "AUTO", "scanLimit": 40, "rolloutsPerState": 8, "stateSamplingMultiplier": 1, "maxTrainSamples": 48_000, "maxValidationSamples": 9_000, "maxTestSamples": 15_000, "sampleStride": 4, "labelHorizonBars": 192, "entryExpiryBars": 32, "rewardRiskPenalty": 0.24, "rewardTimePenalty": 0.0005, "rewardWaitPenalty": 0.05, "minimumTradeUtility": 0.05, "planGeometryEnabled": False, "criticQuantiles": [0.1, 0.5, 0.9], "proposalCount": 32, "riskAversion": 0.30, "timeCostWeight": 0.0008, "selectionMargin": 0.01, "uncertaintyPenalty": 0.03, "actorAdvantageTemperature": 0.35, "selectionFloor": -0.12, "minimumFillProbability": 0.08, "minimumTargetProbability": 0.08, "targetProbabilityWeight": 0.08, "fillPositiveWeight": 1.25, "targetPositiveWeight": 1.75, "coverageTarget": 0.08, "coverageWeight": 0.85, "minimumWinRate": 0.55, "minimumCalibrationTrades": 16, "riskCoverageConfidence": 0.80, "minimumSafetyLowerBound": 0.45, "fallbackWinRateTolerance": 0.03, "relaxedProbabilityFloor": 0.05, "selectionRule": "RISK_COVERAGE", "calibratedSelectionMargin": 0.01, "calibratedSelectionFloor": -0.12}
# Re-export the stable public and low-level helpers from the recovered module.
# New functions below intentionally shadow only the direct-policy path.
for _name, _value in _legacy.__dict__.items():
    if not _name.startswith("__"):
        globals()[_name] = _value

MODEL_VERSION = "binance-market-to-plan-forecast-plan-transformer-v36-coverage-gated"
MODEL_TASK_TYPE = "DIRECT_PLAN"
DIRECT_PLAN_SCHEMA_VERSION = "direct-plan-policy-v7-trailing-protection-diverse-geometry"
DATASET_VERSION = "binance-action-outcome-jun-aug-2026-v16-pending-entry-point-replay"
TRAINING_OBJECTIVE = "PROFILED_WIDE_HORIZON_PENDING_ENTRY_POINT_TRAILING_PROTECTION"
ENTRY_SEMANTICS_VERSION = "PENDING_ENTRY_POINT_CURRENT_BAR_V1"
CHECKPOINT_SELECTION_VERSION = "v2-best-validation-realized-r-stable-risk-coverage"
# Horizons are measured in a checkpoint's declared execution interval. The
# tensor has a stable shape while metadata provides the concrete candle unit.
FORECAST_HORIZONS = (16, 64, 192, 480)
FORECAST_QUANTILES = (0.10, 0.50, 0.90)
FORECAST_OUTPUT_NAMES = tuple(f"return_{h}xexecution_q{int(q * 100):02d}" for h in FORECAST_HORIZONS for q in FORECAST_QUANTILES)
PLAN_QUALITY_OUTPUT_NAMES = (
    "expectedR",
    "profitProbability",
    "drawdownRisk",
    "timeEfficiency",
    "confidence",
)
CANDIDATE_GENERATOR_VERSION = "FORECAST_CONDITIONED_ACTION_LATTICE_V1"
RETRIEVAL_BANK_VERSION = "RAFT_STYLE_TRAINING_STATE_ACTION_BANK_V2_DIVERSE_GEOMETRY"
MACRO_INPUT_INTERVALS = ("4h", "1h", "15m", "5m")
PRIMARY_INTERVALS = MACRO_INPUT_INTERVALS
# The model still consumes all four resolutions.  The lowest resolution is
# also the causal anchor, price/ATR reference, label/replay unit and serving
# cadence.  Keep these explicit so a future feature cannot silently fall back
# to the 15m macro stream.
HOLDING_PROFILE_VERSION = "holding-profile-v3-trailing-protection"
# A profile owns the label/replay time scale.  5m remains an input for every
# profile, but it is not allowed to turn a SWING/POSITION trade into a 5m
# stop-management problem.  The action ranges describe the corpus explored
# by the policy; they are not serving-time profit/stop hard rules.
HOLDING_PROFILES: dict[str, dict[str, Any]] = {
    "SHORT": {
        "label": "短线", "referenceInterval": "5m", "executionInterval": "5m",
        "labelHorizonBars": 144, "entryExpiryBars": 18,
        "rewardRiskPenalty": 0.26, "rewardTimePenalty": 0.00045, "timeCostWeight": 0.00070,
        "stopDistanceAtr": (1.0, 5.5), "targetDistanceAtr": (1.2, 16.0),
    },
    "SWING": {
        "label": "摆动（数天）", "referenceInterval": "15m", "executionInterval": "15m",
        "labelHorizonBars": 480, "entryExpiryBars": 48,
        "rewardRiskPenalty": 0.22, "rewardTimePenalty": 0.00010, "timeCostWeight": 0.00022,
        "stopDistanceAtr": (1.75, 9.0), "targetDistanceAtr": (2.0, 28.0),
    },
    "POSITION": {
        "label": "趋势持仓（数周）", "referenceInterval": "1h", "executionInterval": "1h",
        "labelHorizonBars": 336, "entryExpiryBars": 36,
        "rewardRiskPenalty": 0.18, "rewardTimePenalty": 0.000035, "timeCostWeight": 0.00008,
        "stopDistanceAtr": (2.0, 12.0), "targetDistanceAtr": (2.5, 30.0),
    },
}
MODEL_HOLDING_PROFILE = "SWING"
MODEL_REFERENCE_INTERVAL = str(HOLDING_PROFILES[MODEL_HOLDING_PROFILE]["referenceInterval"])
MODEL_EXECUTION_INTERVAL = str(HOLDING_PROFILES[MODEL_HOLDING_PROFILE]["executionInterval"])
MODEL_ENTRY_EXPIRY_BARS = int(HOLDING_PROFILES[MODEL_HOLDING_PROFILE]["entryExpiryBars"])
INPUT_PROFILE_MULTI_TIMEFRAME = "MULTI_TIMEFRAME"
INPUT_PROFILE_MACRO_ONLY = "MACRO_ONLY"
INPUT_PROFILES = frozenset({INPUT_PROFILE_MULTI_TIMEFRAME, INPUT_PROFILE_MACRO_ONLY})
MODEL_BRANCH_BEST = "BEST"
MODEL_BRANCH_STABLE = "STABLE"
MODEL_BRANCHES = (MODEL_BRANCH_BEST, MODEL_BRANCH_STABLE)
DIRECT_POLICY_CACHE_DIRECTORY = Path(_legacy.db.ROOT_DIR) / "data" / "binance_ml_policy_corpus"
MODEL_DIRECTORY = Path(_legacy.db.ROOT_DIR) / "data" / "binance_ml_models"

# Keep the existing four stream shape.  The allocation head and critic search,
# rather than a fixed target-ratio rule, are retained from the direct-plan ABI.
WINDOWS = {"4h": 48, "1h": 96, "15m": 128, "5m": 288}
INTERVAL_MS = _legacy.INTERVAL_MS
FEATURE_NAMES = _legacy.FEATURE_NAMES
FEATURE_DIM = _legacy.FEATURE_DIM
DIRECT_PLAN_OUTPUT_NAMES = _legacy.DIRECT_PLAN_OUTPUT_NAMES
DIRECTION_WAIT = _legacy.DIRECTION_WAIT
DIRECTION_LONG = _legacy.DIRECTION_LONG
DIRECTION_SHORT = _legacy.DIRECTION_SHORT
DIRECTION_LABELS = _legacy.DIRECTION_LABELS
OUTCOME_STOP = _legacy.OUTCOME_STOP
OUTCOME_EXPIRE = _legacy.OUTCOME_EXPIRE
OUTCOME_TARGET = _legacy.OUTCOME_TARGET
OUTCOME_LABELS = _legacy.OUTCOME_LABELS
MIN_RISK_FRACTION = _legacy.MIN_RISK_FRACTION
# Direct-plan ratios are model-selected leg allocations.  The only fixed
# constraint here is the executable budget: a small remainder stays available
# to the moving-stop state machine.  There is deliberately no business rule
# such as "the second target must be at least N%"; the allocation head learns
# that trade-off from replay utility, drawdown, and holding cost.
MAX_MODEL_FIXED_TARGET_RATIO = 95.0
# A trigger zone is an execution tolerance around one planned entry, not a
# second, much worse/better entry strategy.  Limit its full span by both the
# initial risk and the first-target R so that the displayed zone bounds retain
# comparable stop/target economics.  The R-spread bound is derived from the
# one-sided worst case (the full zone lies on the favourable side of trigger),
# which also covers asymmetric model outputs.
MAX_ENTRY_ZONE_R_MULTIPLE_SPREAD = 0.30
MAX_ENTRY_ZONE_RISK_FRACTION = 0.12
ENTRY_ZONE_GEOMETRY_VERSION = "risk-reward-bounded-v1"

FIXED_HISTORY_START_TIME = _legacy.FIXED_HISTORY_START_TIME
FIXED_HISTORY_END_TIME = _legacy.FIXED_HISTORY_END_TIME

DEFAULT_TRAINING_CONFIG: dict[str, Any] = dict(_legacy.DEFAULT_TRAINING_CONFIG)
DEFAULT_TRAINING_CONFIG.update({
    "modelVersion": MODEL_VERSION,
    "taskType": MODEL_TASK_TYPE,
    "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION,
    "trainingObjective": TRAINING_OBJECTIVE,
    "rolloutsPerState": 8,
    "stateSamplingMultiplier": 1,
    "proposalCount": 32,
    "criticQuantiles": [0.10, 0.50, 0.90],
    "riskAversion": 0.30,
    "timeCostWeight": 0.0008,
    "selectionMargin": 0.01,
    "selectionFloor": -0.12,
    "minimumFillProbability": 0.08,
    "minimumTargetProbability": 0.08,
    "targetProbabilityWeight": 0.08,
    "fillPositiveWeight": 1.25,
    "targetPositiveWeight": 1.75,
    "coverageTarget": 0.08,
    # Validation coverage is a first-class production constraint.  A
    # checkpoint that only trades a few exceptional states must remain a
    # research result instead of becoming an all-WAIT production model.
    "minimumSelectionCoverage": 0.08,
    "coverageWeight": 0.85,
    # Keep a positive empirical win-rate floor, but do not let a noisy Wilson
    # bound turn the selector into an all-WAIT classifier on a small holdout.
    "minimumWinRate": 0.52,
    "minimumCalibrationTrades": 16,
    "riskCoverageConfidence": 0.80,
    "minimumSafetyLowerBound": 0.40,
    "fallbackWinRateTolerance": 0.05,
    # When a confidence-safe point does not exist, retain a bounded frontier
    # point with an empirical edge instead of collapsing to WAIT.  This is a
    # calibration control, not a forced trade rule.
    "frontierMinimumWinRate": 0.50,
    "frontierSafetyRelaxation": 0.08,
    "relaxedProbabilityFloor": 0.05,
    # Retained only for old research reports.  v32 serving never bypasses the
    # validation-calibrated operating point with a live-only fallback.
    "coverageFallbackEnabled": True,
    "coverageFallbackMaxScoreGap": 1.50,
    # Require a clear side preference before using the fallback; this keeps
    # coverage from being purchased with ambiguous LONG/SHORT states.
    "coverageFallbackMinDirectionProbability": 0.45,
    # The target head is deliberately only a weak support signal at serving
    # time (its validation calibration error is materially larger than the
    # critic tail error).  A small positive floor avoids accepting a dead
    # action without turning that auxiliary head into another all-WAIT gate.
    "coverageFallbackMinTargetProbability": 0.01,
    "coverageFallbackMaxUncertainty": 4.0,
    "coverageFallbackMaxDrawdown": 3.0,
    "coverageFallbackMinLowerReturn": -1.50,
    "selectionRule": "RISK_COVERAGE",
    "calibratedSelectionMargin": 0.01,
    "calibratedSelectionFloor": -0.12,
    "uncertaintyPenalty": 0.03,
    "actorAdvantageTemperature": 0.35,
    "entryExpiryBars": MODEL_ENTRY_EXPIRY_BARS,
    "labelHorizonBars": HOLDING_PROFILES[MODEL_HOLDING_PROFILE]["labelHorizonBars"],
    "holdingProfile": MODEL_HOLDING_PROFILE,
    "holdingProfileVersion": HOLDING_PROFILE_VERSION,
    "executionInterval": MODEL_EXECUTION_INTERVAL,
    "forecastLossWeight": 0.18,
    "forecastDirectionLossWeight": 0.06,
    # The previous value (3) often retained only WAIT and one conservative
    # action, so the calibrated frontier could not see the viable plans that
    # the actor had proposed.  Keep a wider, still bounded action frontier.
    "calibrationCandidatesPerState": 8,
    # The quality head is an auxiliary decision-focused target.  It is a
    # soft ranking signal on top of the distributional critic, never a new
    # directional hard gate.
    "qualityLossWeight": 0.10,
    "qualityScoreWeight": 0.18,
    "qualityRiskWeight": 0.08,
    "forecastSupportWeight": 0.05,
    "rankingLossWeight": 0.10,
    "forecastQuantileLossWeight": 0.18,
    "retrievalBankSize": 256,
    "retrievalNeighbors": 8,
    "retrievalJitterCount": 8,
    "retrievalTemperature": 0.20,
    "crossSectionalRankWeight": 0.12,
    "forecastConflictPenalty": 0.18,
    "forecastWaitDominanceThreshold": 0.75,
    "forecastConflictMinSideSupport": 0.05,
})

# Keep helper functions from the compatibility module aligned with the new
# ABI whenever they resolve constants through their own globals dictionary.
_legacy.MODEL_VERSION = MODEL_VERSION
_legacy.MODEL_TASK_TYPE = MODEL_TASK_TYPE
_legacy.DIRECT_PLAN_SCHEMA_VERSION = DIRECT_PLAN_SCHEMA_VERSION
_legacy.DATASET_VERSION = DATASET_VERSION
_legacy.TRAINING_OBJECTIVE = TRAINING_OBJECTIVE
_legacy.ENTRY_SEMANTICS_VERSION = ENTRY_SEMANTICS_VERSION
_legacy.PLAN_QUALITY_OUTPUT_NAMES = PLAN_QUALITY_OUTPUT_NAMES
_legacy.MACRO_INPUT_INTERVALS = MACRO_INPUT_INTERVALS
_legacy.PRIMARY_INTERVALS = PRIMARY_INTERVALS
_legacy.MODEL_REFERENCE_INTERVAL = MODEL_REFERENCE_INTERVAL
_legacy.MODEL_EXECUTION_INTERVAL = MODEL_EXECUTION_INTERVAL
_legacy.INPUT_PROFILE_MULTI_TIMEFRAME = INPUT_PROFILE_MULTI_TIMEFRAME
_legacy.INPUT_PROFILE_MACRO_ONLY = INPUT_PROFILE_MACRO_ONLY
_legacy.INPUT_PROFILES = INPUT_PROFILES
_legacy.WINDOWS = WINDOWS
_legacy.DEFAULT_TRAINING_CONFIG = DEFAULT_TRAINING_CONFIG

_policy_cache_lock = Lock()
_policy_model_cache: dict[str, Any] = {}


if _using_shim:
    _shim_jobs = {}
    _shim_jobs_lock = Lock()

    def _shim_safe_number(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) else 0.0

    def _shim_iso_ms(value):
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def _shim_json_default(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.integer, np.floating)):
            return value.item()
        return str(value)

    def _shim_normalize_network(value):
        value = str(value or "mainnet").strip().lower()
        return value if value in {"mainnet", "testnet"} else "mainnet"

    def _shim_require_torch():
        import torch
        return torch

    def _shim_resolve_device(torch, value):
        requested = str(value or "AUTO").upper()
        if requested == "CUDA" or (requested == "AUTO" and torch.cuda.is_available()):
            if torch.cuda.is_available():
                return torch.device("cuda")
        return torch.device("cpu")

    def _shim_seed_everything(torch, seed):
        import random
        random.seed(int(seed))
        np.random.seed(int(seed))
        torch.manual_seed(int(seed))
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(int(seed))

    def _shim_split_boundaries(config):
        warmup = max(WINDOWS[interval] * _legacy.INTERVAL_MS[interval] for interval in MACRO_INPUT_INTERVALS)
        horizon = int(config.get("labelHorizonBars", 32)) * _legacy.INTERVAL_MS[MODEL_REFERENCE_INTERVAL]
        start = _legacy.FIXED_HISTORY_START_TIME + warmup
        end = _legacy.FIXED_HISTORY_END_TIME - horizon
        span = end - start
        train_end = start + int(span * 0.62)
        validation_end = start + int(span * 0.82)
        return {"train": {"start": start, "end": train_end, "usableEnd": train_end - horizon}, "validation": {"start": train_end + horizon, "end": validation_end, "usableEnd": validation_end - horizon}, "test": {"start": validation_end + horizon, "end": end, "usableEnd": end}}

    def _shim_sample_split(value, boundaries):
        for name, item in boundaries.items():
            if int(item["start"]) <= int(value) < int(item["end"]):
                return name
        return None

    def _shim_frame_close_times(frame):
        return frame.get("closeTimes") if isinstance(frame, dict) and frame.get("compact") else frame.get("closeTime") if isinstance(frame, dict) else []

    def _shim_numpy_frame_from_compact(frame):
        if not isinstance(frame, dict):
            return None
        if frame.get("compact"):
            return {"openTime": np.asarray(frame.get("openTimes") or [], dtype=np.int64), "closeTime": np.asarray(frame.get("closeTimes") or [], dtype=np.int64), "open": np.asarray(frame.get("opens") or [], dtype=np.float64), "high": np.asarray(frame.get("highs") or [], dtype=np.float64), "low": np.asarray(frame.get("lows") or [], dtype=np.float64), "close": np.asarray(frame.get("closes") or [], dtype=np.float64), "volume": np.asarray(frame.get("volumes") or [], dtype=np.float64)}
        bars = frame.get("bars") if isinstance(frame.get("bars"), list) else []
        return _shim_frame_from_live_bars(bars)

    def _shim_frame_from_live_bars(items):
        if not isinstance(items, list) or not items:
            return None
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                rows.append({"openTime": int(item.get("openTime", item.get("open_time"))), "closeTime": int(item.get("closeTime", item.get("close_time"))), "open": float(item.get("open")), "high": float(item.get("high")), "low": float(item.get("low")), "close": float(item.get("close")), "volume": float(item.get("volume", 0.0))})
            except (TypeError, ValueError):
                continue
        if not rows:
            return None
        return {key: np.asarray([row[key] for row in rows], dtype=np.float64 if key not in {"openTime", "closeTime"} else np.int64) for key in ("openTime", "closeTime", "open", "high", "low", "close", "volume")}

    def _rolling_mean(values, window):
        result = np.empty(len(values), dtype=np.float64)
        for index in range(len(values)):
            result[index] = float(np.mean(values[max(0, index - window + 1):index + 1]))
        return result

    def _rolling_std(values, window):
        result = np.empty(len(values), dtype=np.float64)
        for index in range(len(values)):
            result[index] = float(np.std(values[max(0, index - window + 1):index + 1]))
        return result

    def _shim_with_features(frame):
        close = np.asarray(frame["close"], dtype=np.float64)
        open_ = np.asarray(frame["open"], dtype=np.float64)
        high = np.asarray(frame["high"], dtype=np.float64)
        low = np.asarray(frame["low"], dtype=np.float64)
        volume = np.maximum(np.asarray(frame["volume"], dtype=np.float64), 1e-12)
        n = len(close)
        safe = np.maximum(np.abs(close), 1e-12)
        log_return = np.zeros(n); log_return[1:] = np.log(safe[1:] / safe[:-1])
        body = (close - open_) / safe
        range_return = (high - low) / safe
        upper = (high - np.maximum(open_, close)) / np.maximum(high - low, 1e-12)
        lower = (np.minimum(open_, close) - low) / np.maximum(high - low, 1e-12)
        log_volume = np.log(volume)
        volume_mean = _rolling_mean(log_volume, 20)
        volume_std = np.maximum(_rolling_std(log_volume, 20), 1e-6)
        atr = _rolling_mean(np.maximum(high - low, np.maximum(np.abs(high - np.r_[close[:1], close[:-1]]), np.abs(low - np.r_[close[:1], close[:-1]]))), 14)
        ema20 = np.empty(n); ema50 = np.empty(n); ema20[0] = close[0]; ema50[0] = close[0]
        for i in range(1, n):
            ema20[i] = ema20[i - 1] + (close[i] - ema20[i - 1]) * (2 / 21)
            ema50[i] = ema50[i - 1] + (close[i] - ema50[i - 1]) * (2 / 51)
        atr_safe = np.maximum(atr, safe * MIN_RISK_FRACTION)
        def momentum(period):
            result = np.zeros(n); result[period:] = (close[period:] - close[:-period]) / np.maximum(np.abs(close[:-period]), 1e-12); return result
        mom4, mom12, mom24 = momentum(4), momentum(12), momentum(24)
        directional = np.zeros(n)
        for i in range(n):
            start = max(0, i - 7); movement = abs(close[i] - close[start]); path = np.sum(np.abs(np.diff(close[start:i + 1]))) if i > start else 0.0; directional[i] = movement / max(path, 1e-12)
        high20 = np.maximum.accumulate(np.asarray([np.max(high[max(0, i - 19):i + 1]) for i in range(n)]))
        low20 = np.minimum.accumulate(np.asarray([np.min(low[max(0, i - 19):i + 1]) for i in range(n)]))
        location = (close - low20) / np.maximum(high20 - low20, 1e-12)
        atr_short = _rolling_mean(high - low, 7); atr_long = _rolling_mean(high - low, 28)
        features = np.column_stack((log_return, body, range_return, upper, lower, log_volume - volume_mean, (close - ema20) / atr_safe, atr / np.maximum(_rolling_mean(atr, 40), 1e-12), mom4, (close - low) / np.maximum(high - low, 1e-12), (ema20 - ema50) / atr_safe, np.r_[0.0, np.diff(ema20)] / atr_safe, location, np.ones(n), directional, (log_volume - volume_mean) / volume_std, mom12, mom24, mom4 - np.r_[np.zeros(4), mom4[:-4]], atr_short / np.maximum(atr_long, 1e-12), np.sign(mom4) * directional, (high20 - close) / atr_safe, (close - low20) / atr_safe, np.sign(body) * (log_volume - volume_mean)))
        return {**frame, "features": np.nan_to_num(features.astype(np.float32)), "atr14": np.maximum(atr_safe, 1e-12)}

    def _shim_assemble_tokens(prepared, _anchor_index, anchor_time, intervals):
        chunks = []
        for interval in intervals:
            frame = prepared.get(interval)
            if frame is None:
                return None
            times = np.asarray(frame["closeTime"], dtype=np.int64)
            idx = int(np.searchsorted(times, int(anchor_time), side="right") - 1)
            width = WINDOWS[interval]
            if idx < width - 1:
                return None
            chunks.append(frame["features"][idx - width + 1:idx + 1])
        return np.concatenate(chunks, axis=0).astype(np.float32)

    def _shim_model_inputs_from_live_frames(frames, intervals, plan_config=None):
        prepared = {}
        for interval in intervals:
            frame = _shim_frame_from_live_bars((frames or {}).get(interval))
            if frame is None:
                return None
            prepared[interval] = _shim_with_features(frame)
        reference = prepared.get(MODEL_REFERENCE_INTERVAL)
        if reference is None or not reference.get("closeTime"):
            return None
        anchor = int(reference["closeTime"][-1])
        tokens = _shim_assemble_tokens(prepared, len(reference["close"]) - 1, anchor, intervals)
        # Live inference consumes one state; keep the batch dimension explicit
        # so it follows the same [batch, tokens, features] ABI as training.
        return (tokens[None, ...], np.zeros((len(CANDIDATE_PLAN_FEATURE_NAMES) if "CANDIDATE_PLAN_FEATURE_NAMES" in globals() else 1,), dtype=np.float32)) if tokens is not None else None

    _legacy._shim = True
    _legacy._safe_number = _shim_safe_number
    _legacy._iso_ms = _shim_iso_ms
    _legacy._json_default = _shim_json_default
    _legacy._normalize_network = _shim_normalize_network
    _legacy._require_torch = _shim_require_torch
    _legacy._resolve_device = _shim_resolve_device
    _legacy._seed_everything = _shim_seed_everything
    _legacy._split_boundaries = _shim_split_boundaries
    _legacy._sample_split = _shim_sample_split
    _legacy._frame_close_times = _shim_frame_close_times
    _legacy._numpy_frame_from_compact = _shim_numpy_frame_from_compact
    _legacy._frame_from_live_bars = _shim_frame_from_live_bars
    _legacy._with_features = _shim_with_features
    _legacy._assemble_tokens = _shim_assemble_tokens
    _legacy._model_inputs_from_live_frames = _shim_model_inputs_from_live_frames
    _legacy._jobs = _shim_jobs
    _legacy._jobs_lock = _shim_jobs_lock

    def _shim_cleanup_jobs_locked(*_args):
        return None

    def _shim_job_snapshot(job):
        return json.loads(json.dumps(job, ensure_ascii=False, default=_shim_json_default))

    def _shim_lookup_job(run_id):
        with _shim_jobs_lock:
            return _shim_jobs.get(str(run_id))

    def _shim_training_job(run_id):
        """Return an active in-memory job or its persisted terminal record."""
        active = _shim_lookup_job(run_id)
        if active is not None:
            return _shim_job_snapshot(active)
        try:
            persisted = _shim_db.get_binance_ml_training_run(str(run_id))
        except Exception:
            persisted = None
        if not isinstance(persisted, dict):
            return None
        metrics = persisted.get("metrics") if isinstance(persisted.get("metrics"), dict) else {}
        epochs = metrics.get("epochs") if isinstance(metrics.get("epochs"), list) else []
        status = str(persisted.get("status") or "").upper()
        progress = {
            "phase": "COMPLETED" if status == "COMPLETED" else "FAILED" if status == "FAILED" else "UNKNOWN",
            "completed": int(len(epochs)) if epochs else 0,
            "total": int((persisted.get("config") or {}).get("epochs") or len(epochs) or 0),
            "message": "已从本地记录恢复训练任务状态。",
            "currentSymbol": None,
        }
        return {**persisted, "progress": progress}

    def _shim_update_job(run_id, *, status=None, phase=None, message=None, completed=None, total=None, current_symbol=None, dataset=None, metrics=None, artifact_path=None, error=None):
        with _shim_jobs_lock:
            job = _shim_jobs.get(str(run_id))
            if not job:
                return
            if status is not None:
                job["status"] = status
            if dataset is not None:
                job["dataset"] = dataset
            if metrics is not None:
                job["metrics"] = metrics
            if artifact_path is not None:
                job["artifactPath"] = artifact_path
            if error is not None:
                job["error"] = error
            progress = job.setdefault("progress", {})
            if phase is not None: progress["phase"] = phase
            if message is not None: progress["message"] = message
            if completed is not None: progress["completed"] = completed
            if total is not None: progress["total"] = total
            if current_symbol is not None: progress["currentSymbol"] = current_symbol
            job["updatedAt"] = int(time.time() * 1000)
        try:
            _shim_db.update_binance_ml_training_run(str(run_id), status=status, dataset=dataset, metrics=metrics, artifact_path=artifact_path, error=error)
        except Exception:
            pass

    _legacy._cleanup_jobs_locked = _shim_cleanup_jobs_locked
    _legacy._job_snapshot = _shim_job_snapshot
    _legacy._lookup_job = _shim_lookup_job
    _legacy._training_job = _shim_training_job
    _legacy._update_job = _shim_update_job

    def _shim_direct_policy_action_sample(rng, side):
        values = np.zeros(len(_legacy.DIRECT_PLAN_OUTPUT_NAMES) - 1, dtype=np.float32)
        by_name = {name: index for index, name in enumerate(_legacy.DIRECT_PLAN_OUTPUT_NAMES[1:])}
        # Keep the action space symmetric.  The critic must see both sides of
        # the reference price and learn the cost of a crossed/stale trigger
        # from replay, rather than having half of the geometries removed by a
        # directional sampler rule.  Serving handles stale triggers at the
        # final execution boundary instead of tightening training coverage.
        trigger = float(rng.uniform(-0.9, 0.9))
        if abs(trigger) < 0.05:
            trigger = 0.05 if rng.random() >= 0.5 else -0.05
        values[by_name["entryTriggerAtr"]] = trigger
        # Entry zones are retained in the model ABI for checkpoint
        # compatibility, but direct plans now use one executable price.
        values[by_name["entryZoneLowAtr"]] = trigger
        values[by_name["entryZoneHighAtr"]] = trigger
        # Swing actions must expose room for normal contract retracements.
        # Distances are expressed in the 15m ATR used by replay, not the noisy
        # 5m ATR.  The learner still sees smaller actions through the lower
        # tail of these log-uniform ranges, but the corpus is no longer
        # dominated by sub-hour stops and targets.
        risk = float(np.exp(rng.uniform(np.log(0.8), np.log(5.0))))
        first_r = float(np.exp(rng.uniform(np.log(0.8), np.log(5.0))))
        second_r = first_r + float(np.exp(rng.uniform(np.log(0.35), np.log(8.0))))
        values[by_name["stopDistanceAtr"]] = risk
        values[by_name["targetOneDistanceAtr"]] = risk * first_r
        values[by_name["targetTwoDistanceAtr"]] = risk * second_r
        first_ratio = float(rng.uniform(25.0, 72.0)); second_ratio = float(rng.uniform(12.0, max(13.0, 92.0 - first_ratio)))
        first_ratio, second_ratio = _normalize_model_target_ratios(first_ratio, second_ratio)
        values[by_name["targetOneRatio"]] = first_ratio; values[by_name["targetTwoRatio"]] = second_ratio
        values[by_name["initialActivationR"]] = float(rng.uniform(0.7, 2.4)); values[by_name["breakevenTriggerR"]] = float(rng.uniform(0.9, 2.8))
        values[by_name["breakevenBufferAtr"]] = float(rng.uniform(0.0, 0.55)); values[by_name["structureLookbackBars"]] = float(rng.integers(8, 64)); values[by_name["structureBufferAtr"]] = float(rng.uniform(0.0, 1.25)); values[by_name["atrPeriod"]] = float(rng.integers(10, 48)); values[by_name["atrMultiplier"]] = float(rng.uniform(1.25, 5.0)); values[by_name["tp1Bars"]] = float(rng.integers(8, 64)); values[by_name["tp2Bars"]] = float(rng.integers(16, 128)); values[by_name["exitBars"]] = float(rng.integers(32, 256))
        # Keep the explicit time-cost outputs coherent with the sampled
        # geometry so the model can learn them instead of seeing constant
        # zero targets.
        exit_bars = max(float(values[by_name["exitBars"]]), float(values[by_name["tp2Bars"]]), 1.0)
        risk_atr = max(float(values[by_name["stopDistanceAtr"]]), 1e-6)
        first_r = max(float(values[by_name["targetOneDistanceAtr"]]) / risk_atr, 0.0)
        values[by_name["timeEfficiency"]] = _swing_time_efficiency(first_r, exit_bars)
        values[by_name["holdingCost"]] = -0.0008 * exit_bars
        return values

    def _shim_direct_policy_replay_settings():
        settings = _shim_db.default_binance_strategy_settings() if hasattr(_shim_db, "default_binance_strategy_settings") else {}
        settings = dict(settings or {}); settings.update({"strategyEngine": "MODEL", "strategyMode": "MIDLINE", "entryConfirmationMode": "TRIGGER_ONLY", "entryConfirmationExpiryBars": MODEL_ENTRY_EXPIRY_BARS})
        return settings

    def _shim_direct_policy_plan_for_action(symbol, close, atr, side, action):
        prediction = {name: float(value) for name, value in zip(_legacy.DIRECT_PLAN_OUTPUT_NAMES[1:], action)}; prediction["direction"] = str(side).upper()
        plan = decode_direct_plan_prediction(prediction, reference_price=float(close), atr=float(atr), symbol=symbol, metadata={"modelVersion": MODEL_VERSION, "modelBranch": MODEL_BRANCH_BEST})
        if str(plan.get("direction") or "WAIT").upper() == "WAIT": return None
        plan["strategySettings"] = _shim_direct_policy_replay_settings(); return plan

    def _shim_direct_policy_fast_replay(frame, index, side, action, config):
        close = float(frame["close"][index]); atr = max(float(frame.get("atr14", np.asarray([0.0]))[index]), abs(close) * _legacy.MIN_RISK_FRACTION, 1e-12)
        plan = _shim_direct_policy_plan_for_action("", close, atr, side, action)
        if plan is None: return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0, "mfeR": 0.0, "maeR": 0.0, "maxDrawdownR": 0.0}
        entry = float(plan["entry"]["trigger"]); stop = float(plan["stopLoss"]); targets = plan.get("takeProfits") or []
        is_limit = str((plan.get("entry") or {}).get("orderType") or "STOP").upper() == "LIMIT"
        if len(targets) < 2: return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0, "mfeR": 0.0, "maeR": 0.0, "maxDrawdownR": 0.0}
        first, second = float(targets[0]["price"]), float(targets[1]["price"]); ratio1 = float(targets[0].get("ratio") or 0.0) / 100.0; ratio2 = float(targets[1].get("ratio") or 0.0) / 100.0; risk = abs(entry - stop); is_long = str(side).upper() == "LONG"; sign = 1.0 if is_long else -1.0
        horizon = min(len(frame["close"]) - index - 1, max(1, int(config.get("labelHorizonBars", 32)))); expiry = max(1, int(config.get("entryExpiryBars", MODEL_ENTRY_EXPIRY_BARS))); entered = False; remaining = 1.0; realized = 0.0; first_hit = False; second_hit = False; stop_hit = False; trailing_activated = False; active_stop = stop; mfe = 0.0; mae = 0.0; exit_duration = horizon; peak = entry; activation_r = max(float((plan.get("trailingStop", {}).get("INITIAL") or {}).get("activationR") or 1.0), 0.05); activation_level = entry + sign * risk * activation_r
        def path(i):
            opening, high, low, closing = float(frame["open"][i]), float(frame["high"][i]), float(frame["low"][i]), float(frame["close"][i]); return [opening, low, high, closing] if is_long else [opening, high, low, closing]
        def crossed(level, left, right): return left < level <= right if right > left else right <= level < left
        for offset in range(1, horizon + 1):
            points = path(index + offset)
            if not entered:
                if offset > expiry: break
                opens_at_entry = (points[0] <= entry if is_long else points[0] >= entry) if is_limit else (points[0] >= entry if is_long else points[0] <= entry)
                touched_entry = (min(points) <= entry if is_long else max(points) >= entry) if is_limit else (max(points) >= entry if is_long else min(points) <= entry)
                if opens_at_entry: entered = True; points = [points[0], *points[1:]]
                elif touched_entry:
                    if (min(points) <= stop and max(points) >= entry) if is_long else (max(points) >= stop and min(points) <= entry): break
                    entered = True; points = [entry] + points[1:]
                else: continue
            current = points[0]
            for destination in points[1:]:
                # The previous replay only advanced protection after a target
                # event.  Evaluate the activation level on every intrabar
                # segment so a move to 1R can protect the remaining position
                # even when no target was touched.
                events = [(active_stop, "STOP"), (first, "FIRST"), (second, "SECOND")]
                if not trailing_activated:
                    events.append((activation_level, "ACTIVATE"))
                hits = [(float(level), kind) for level, kind in events if (crossed(float(level), current, destination))]
                hits.sort(key=lambda item: item[0], reverse=destination > current)
                for price, kind in hits:
                    excursion = (price - entry) / risk if is_long else (entry - price) / risk
                    if kind == "STOP": realized += excursion * remaining; remaining = 0.0; stop_hit = True; exit_duration = offset; break
                    if kind == "ACTIVATE":
                        trailing_activated = True
                        buffer = float((plan.get("trailingStop", {}).get("BREAKEVEN") or {}).get("bufferAtr") or 0.0) * atr
                        candidate = entry + buffer if is_long else entry - buffer
                        active_stop = max(active_stop, candidate) if is_long else min(active_stop, candidate)
                        continue
                    if kind == "FIRST" and not first_hit: realized += excursion * ratio1; remaining -= ratio1; first_hit = True
                    if kind == "SECOND" and not second_hit: realized += excursion * ratio2; remaining -= ratio2; second_hit = True
                    peak = max(peak, price) if is_long else min(peak, price)
                    if remaining <= 1e-9: exit_duration = offset; break
                if remaining <= 1e-9: break
                if trailing_activated:
                    peak = max(peak, destination) if is_long else min(peak, destination)
                    multiplier = float((plan.get("trailingStop", {}).get("ATR_TRAILING") or {}).get("multiplier") or 2.5)
                    candidate = peak - multiplier * atr if is_long else peak + multiplier * atr
                    active_stop = max(active_stop, candidate) if is_long else min(active_stop, candidate)
                current = destination
            bar_high, bar_low = max(points), min(points); mfe = max(mfe, ((bar_high - entry) / risk if is_long else (entry - bar_low) / risk)); mae = max(mae, ((entry - bar_low) / risk if is_long else (bar_high - entry) / risk))
            if remaining <= 1e-9: break
        if not entered: return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0, "mfeR": mfe, "maeR": mae, "maxDrawdownR": mae}
        if remaining > 1e-9:
            final = float(frame["close"][min(index + horizon, len(frame["close"]) - 1)]); realized += ((final - entry) / risk if is_long else (entry - final) / risk) * remaining
        return {"outcome": "TARGET" if first_hit else "STOP" if stop_hit else "EXPIRE", "entered": True, "realizedR": float(np.clip(realized, -8.0, 8.0)), "durationBars": max(1, int(exit_duration)), "mfeR": float(np.clip(mfe, 0.0, 8.0)), "maeR": float(np.clip(mae, 0.0, 8.0)), "maxDrawdownR": float(np.clip(mae, 0.0, 8.0)), "firstTargetHit": bool(first_hit), "secondTargetHit": bool(second_hit), "trailingActivated": bool(trailing_activated), "exitReason": "TARGET" if second_hit or (first_hit and remaining <= 1e-9) else "TRAILING_STOP" if trailing_activated and stop_hit else "STRUCTURE_STOP" if stop_hit else "TIME_EXIT"}

    _legacy._direct_policy_action_sample = _shim_direct_policy_action_sample
    _legacy._direct_policy_replay_settings = _shim_direct_policy_replay_settings
    _legacy._direct_policy_plan_for_action = _shim_direct_policy_plan_for_action
    _legacy._direct_policy_fast_replay = _shim_direct_policy_fast_replay


def _holding_profile(profile: object) -> dict[str, Any]:
    key = str(profile or MODEL_HOLDING_PROFILE).strip().upper()
    return dict(HOLDING_PROFILES.get(key) or HOLDING_PROFILES[MODEL_HOLDING_PROFILE])


def _holding_profile_name(profile: object) -> str:
    key = str(profile or MODEL_HOLDING_PROFILE).strip().upper()
    return key if key in HOLDING_PROFILES else MODEL_HOLDING_PROFILE


def _normalize_training_config(value: object) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}

    def integer(key: str, low: int, high: int) -> int:
        try:
            value = int(source.get(key, DEFAULT_TRAINING_CONFIG[key]))
        except (TypeError, ValueError):
            value = int(DEFAULT_TRAINING_CONFIG[key])
        return min(max(value, low), high)

    def number(key: str, low: float, high: float) -> float:
        try:
            value = float(source.get(key, DEFAULT_TRAINING_CONFIG[key]))
        except (TypeError, ValueError):
            value = float(DEFAULT_TRAINING_CONFIG[key])
        return min(max(value if math.isfinite(value) else float(DEFAULT_TRAINING_CONFIG[key]), low), high)

    result = dict(DEFAULT_TRAINING_CONFIG)
    for key, low, high in (
        ("maxTrainSamples", 1_000, 120_000),
        ("maxValidationSamples", 500, 30_000),
        ("maxTestSamples", 500, 30_000),
        ("sampleStride", 1, 48),
        ("labelHorizonBars", 32, 672),
        ("entryExpiryBars", 4, 96),
        ("calibrationCandidatesPerState", 1, 8),
        ("epochs", 1, 50),
        ("batchSize", 16, 512),
        ("dModel", 32, 192),
        ("numHeads", 2, 8),
        ("numLayers", 1, 4),
        ("scanLimit", 1, 100),
        ("rolloutsPerState", 2, 12),
        ("stateSamplingMultiplier", 1, 3),
        ("proposalCount", 8, 64),
        ("retrievalBankSize", 32, 2_048),
        ("retrievalNeighbors", 1, 32),
        ("retrievalJitterCount", 0, 32),
    ):
        result[key] = integer(key, low, high)
    for key, low, high in (
        ("learningRate", 0.00001, 0.01),
        ("weightDecay", 0.0, 0.1),
        ("dropout", 0.0, 0.5),
        ("rewardRiskPenalty", 0.0, 2.0),
        ("rewardTimePenalty", 0.0, 0.1),
        ("rewardWaitPenalty", 0.0, 0.2),
        ("minimumTradeUtility", 0.0, 1.0),
        ("riskAversion", 0.0, 3.0),
        ("timeCostWeight", 0.0, 0.2),
        ("selectionMargin", 0.0, 2.0),
        ("selectionFloor", -1.0, 2.0),
        ("minimumFillProbability", 0.0, 1.0),
        ("minimumTargetProbability", 0.0, 1.0),
        ("targetProbabilityWeight", 0.0, 2.0),
        ("fillPositiveWeight", 0.1, 8.0),
        ("targetPositiveWeight", 0.1, 8.0),
        ("coverageTarget", 0.0, 0.5),
        ("minimumSelectionCoverage", 0.0, 0.5),
        ("coverageWeight", 0.0, 2.0),
        ("minimumWinRate", 0.0, 1.0),
        ("minimumCalibrationTrades", 4, 500),
        ("riskCoverageConfidence", 0.50, 0.999),
        ("minimumSafetyLowerBound", 0.0, 1.0),
        ("fallbackWinRateTolerance", 0.0, 0.25),
        ("frontierMinimumWinRate", 0.0, 1.0),
        ("frontierSafetyRelaxation", 0.0, 0.50),
        ("relaxedProbabilityFloor", 0.0, 1.0),
        ("calibratedSelectionMargin", -2.0, 2.0),
        ("calibratedSelectionFloor", -1.0, 2.0),
        ("uncertaintyPenalty", 0.0, 1.0),
        ("actorAdvantageTemperature", 0.05, 2.0),
        ("coverageFallbackMaxScoreGap", 0.0, 3.0),
        ("coverageFallbackMinDirectionProbability", 0.0, 1.0),
        ("coverageFallbackMinTargetProbability", 0.0, 1.0),
        ("coverageFallbackMaxUncertainty", 0.0, 12.0),
        ("coverageFallbackMaxDrawdown", 0.0, 12.0),
        ("coverageFallbackMinLowerReturn", -8.0, 2.0),
        ("qualityLossWeight", 0.0, 2.0),
        ("qualityScoreWeight", 0.0, 2.0),
        ("qualityRiskWeight", 0.0, 2.0),
        ("forecastSupportWeight", 0.0, 2.0),
        ("rankingLossWeight", 0.0, 2.0),
        ("forecastQuantileLossWeight", 0.0, 2.0),
        ("retrievalTemperature", 0.01, 2.0),
        ("crossSectionalRankWeight", 0.0, 2.0),
        ("forecastConflictPenalty", 0.0, 2.0),
        ("forecastWaitDominanceThreshold", 0.34, 0.99),
        ("forecastConflictMinSideSupport", 0.0, 1.0),
    ):
        if key == "minimumCalibrationTrades":
            result[key] = integer(key, low, high)
        else:
            result[key] = number(key, low, high)
    result["seed"] = integer("seed", 1, 2_147_483_647)
    result["device"] = str(source.get("device", result.get("device", "AUTO")) or "AUTO").strip().upper()
    if result["device"] not in {"AUTO", "CPU", "CUDA"}:
        result["device"] = "AUTO"
    result["inputProfile"] = INPUT_PROFILE_MULTI_TIMEFRAME
    result["planGeometryEnabled"] = False
    # Profile is the source of truth for label/replay cadence.  Do not let a
    # stale UI value such as the old 32-bar 5m default silently turn a swing
    # run back into a scalp.  Explicit advanced values remain supported when
    # the caller supplies them, but the profile defaults are always applied.
    profile_name = _holding_profile_name(source.get("holdingProfile", MODEL_HOLDING_PROFILE))
    profile = _holding_profile(profile_name)
    result["holdingProfile"] = profile_name
    result["holdingProfileVersion"] = HOLDING_PROFILE_VERSION
    result["referenceInterval"] = str(profile["referenceInterval"])
    result["executionInterval"] = str(profile["executionInterval"])
    result["labelHorizonBars"] = int(profile["labelHorizonBars"])
    result["entryExpiryBars"] = int(profile["entryExpiryBars"])
    result["rewardRiskPenalty"] = float(profile["rewardRiskPenalty"])
    result["rewardTimePenalty"] = float(profile["rewardTimePenalty"])
    result["timeCostWeight"] = float(profile["timeCostWeight"])
    result["selectionRule"] = str(source.get("selectionRule", result.get("selectionRule", "RISK_COVERAGE")) or "RISK_COVERAGE").strip().upper()
    if result["selectionRule"] not in {"RISK_COVERAGE", "LEGACY_OR"}:
        result["selectionRule"] = "RISK_COVERAGE"
    raw_q = source.get("criticQuantiles", result["criticQuantiles"])
    try:
        quantiles = sorted({float(item) for item in raw_q if 0.01 < float(item) < 0.99})
    except (TypeError, ValueError):
        quantiles = list(result["criticQuantiles"])
    result["criticQuantiles"] = quantiles if len(quantiles) >= 3 else list(DEFAULT_TRAINING_CONFIG["criticQuantiles"])
    result["minimumSelectionCoverage"] = number(
        "minimumSelectionCoverage", 0.0, 0.5,
    )
    result["coverageFallbackEnabled"] = bool(
        source.get("coverageFallbackEnabled", result.get("coverageFallbackEnabled", True))
    )
    result["forecastLossWeight"] = number("forecastLossWeight", 0.0, 2.0)
    result["forecastDirectionLossWeight"] = number("forecastDirectionLossWeight", 0.0, 1.0)
    result.update({
    "modelVersion": MODEL_VERSION,
        "taskType": MODEL_TASK_TYPE,
        "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION,
        "trainingObjective": TRAINING_OBJECTIVE,
    })
    # Keep the original Transformer head partition rule.
    heads = [head for head in (8, 4, 2) if head <= result["numHeads"] and result["dModel"] % head == 0]
    result["numHeads"] = heads[0] if heads else 2
    if result["dModel"] % result["numHeads"]:
        result["dModel"] = max(32, result["dModel"] - result["dModel"] % result["numHeads"])
    return result


if _using_shim:
    def _shim_normalize_token_array(tokens, normalization, intervals):
        values = np.asarray(tokens, dtype=np.float32).copy()
        offset = 0
        clip = float((normalization or {}).get("clip") or 8.0)
        for interval in intervals:
            width = WINDOWS[interval]
            item = (normalization or {}).get("intervals", {}).get(interval, {})
            mean = np.asarray(item.get("mean") or [0.0] * FEATURE_DIM, dtype=np.float32)
            std = np.maximum(np.asarray(item.get("std") or [1.0] * FEATURE_DIM, dtype=np.float32), 1e-5)
            values[:, offset:offset + width, :] = np.nan_to_num(np.clip((values[:, offset:offset + width, :] - mean) / std, -clip, clip), nan=0.0, posinf=clip, neginf=-clip)
            offset += width
        return values

    def _shim_direct_plan_bounds(targets):
        values = np.asarray(targets, dtype=np.float64)
        result = {}
        for index, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:]):
            if values.ndim == 2 and values.shape[0]:
                column = values[:, index]
                result[name] = {"min": float(np.nanpercentile(column, 1)), "max": float(np.nanpercentile(column, 99))}
        return result

    def _shim_stable_checkpoint_epoch(history, epochs):
        if not history: return int(epochs)
        tail = history[max(0, len(history) - 10):]
        scores = [_direct_policy_selection_score(item.get("validation") or {}) for item in tail]
        return int(tail[int(np.argmax(scores))].get("epoch") or epochs)

    def _shim_persist_model(run_id, payload, dataset_info, metrics):
        torch = _shim_require_torch(); MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
        branches = payload.get("branchPayloads") if isinstance(payload.get("branchPayloads"), dict) else {MODEL_BRANCH_BEST: payload}
        paths = {}
        for branch, branch_payload in branches.items():
            artifact_name = f"{run_id}.{str(branch).lower()}.pt"; artifact = MODEL_DIRECTORY / artifact_name; temporary = MODEL_DIRECTORY / f".{artifact_name}.{uuid4().hex}.tmp"; torch.save(branch_payload, temporary); os.replace(temporary, artifact); paths[branch] = str(Path("binance_ml_models") / artifact_name); branch_metric = (metrics.get("branches") or {}).get(branch) if isinstance(metrics.get("branches"), dict) else None
            if isinstance(branch_metric, dict): branch_metric["artifactPath"] = paths[branch]
        report = {"runId": run_id, "model": (branches.get(MODEL_BRANCH_BEST) or payload).get("metadata", {}), "dataset": dataset_info, "metrics": metrics}
        report_name = f"{run_id}.report.json"; report_path = MODEL_DIRECTORY / report_name; report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_shim_json_default), encoding="utf-8")
        metrics.setdefault("model", {})["branchArtifacts"] = paths; metrics["model"]["bestArtifactPath"] = paths.get(MODEL_BRANCH_BEST)
        return paths[MODEL_BRANCH_BEST], str(Path("binance_ml_models") / report_name)

    def _shim_persisted_job_snapshot(record):
        return record if isinstance(record, dict) else None

    def _shim_artifact_file(value):
        return Path(_legacy.db.ROOT_DIR) / "data" / str(value).replace("/", os.sep) if value else Path("__missing__")

    def _shim_record_artifact_path(record, branch="BEST"):
        metrics = record.get("metrics") if isinstance(record, dict) else {}
        model = metrics.get("model") if isinstance(metrics, dict) else {}
        paths = model.get("branchArtifacts") if isinstance(model, dict) else {}
        return paths.get(branch) if isinstance(paths, dict) else None

    def _shim_resolve_model_record(network, run_id=None):
        safe = _shim_normalize_network(network)
        if run_id:
            return _legacy.db.get_binance_ml_training_run(str(run_id))
        return _legacy.db.get_latest_binance_ml_training_run(safe, completed_only=True)

    def _shim_model_runtime_available():
        try:
            _shim_require_torch(); return True, None
        except Exception as exc: return False, str(exc)

    def _shim_invalidate_model_cache():
        """Drop loaded legacy models after a checkpoint is persisted.

        The recovered legacy module normally owns this hook.  The source
        compatibility runtime must provide the same lifecycle boundary so a
        newly completed training run can be selected without restarting the
        API process.
        """
        with _policy_cache_lock:
            _policy_model_cache.clear()

    _legacy._normalize_token_array = _shim_normalize_token_array
    _legacy._direct_plan_bounds_from_targets = _shim_direct_plan_bounds
    _legacy._stable_checkpoint_epoch = _shim_stable_checkpoint_epoch
    _legacy._persist_model = _shim_persist_model
    _legacy._persisted_job_snapshot = _shim_persisted_job_snapshot
    _legacy._artifact_file = _shim_artifact_file
    _legacy._record_artifact_path = _shim_record_artifact_path
    _legacy._resolve_model_record = _shim_resolve_model_record
    _legacy._model_runtime_available = _shim_model_runtime_available
    _legacy._invalidate_model_cache = _shim_invalidate_model_cache

_legacy._normalize_training_config = _normalize_training_config


def _input_intervals(config=None):
    return MACRO_INPUT_INTERVALS


def _input_windows(config=None):
    return dict(WINDOWS)


def _input_token_count(config=None):
    return int(sum(WINDOWS.values()))


def _model_frame_from_live_bars(items):
    """Adapt a live/list frame for the direct model without changing cadence.

    The compatibility diagnostic helpers historically accepted a 15m-only
    fixture.  Serving/training never uses that fallback: the model input
    contract requires the 5m stream and anchors on its last completed bar.
    """

    if _using_shim:
        return _shim_frame_from_live_bars(items)
    return _legacy._frame_from_live_bars(items)


def _model_prepared_frame(items):
    frame = _model_frame_from_live_bars(items)
    if frame is None:
        return None
    return _shim_with_features(frame) if _using_shim else _legacy._with_features(frame)


def _assemble_model_tokens(prepared, _anchor_index, anchor_time, intervals):
    """Assemble tokens causally at the completed 5m anchor.

    ``_anchor_index`` is retained for the old helper ABI.  Searching each
    stream by close time is intentional: higher timeframes may close between
    two 5m bars and must use the latest bar that was already closed.
    """

    chunks = []
    for interval in intervals:
        frame = prepared.get(interval)
        if frame is None:
            return None
        times = np.asarray(frame.get("closeTime", []), dtype=np.int64)
        index = int(np.searchsorted(times, int(anchor_time), side="right") - 1)
        width = int(WINDOWS[interval])
        if index < width - 1:
            return None
        features = np.asarray(frame.get("features"), dtype=np.float32)
        chunks.append(features[index - width + 1:index + 1])
    return np.concatenate(chunks, axis=0).astype(np.float32) if chunks else None


def _model_inputs_from_live_frames(frames, intervals, plan_config=None):
    intervals = tuple(intervals or MACRO_INPUT_INTERVALS)
    profile = _holding_profile(plan_config.get("holdingProfile") if isinstance(plan_config, dict) else MODEL_HOLDING_PROFILE)
    reference_interval = str(profile["referenceInterval"])
    if reference_interval not in intervals:
        return None
    prepared = {}
    for interval in intervals:
        prepared_frame = _model_prepared_frame((frames or {}).get(interval))
        if prepared_frame is None:
            return None
        prepared[interval] = prepared_frame
    reference = prepared[reference_interval]
    close_times = np.asarray(reference.get("closeTime", []), dtype=np.int64)
    if not len(close_times):
        return None
    anchor = int(close_times[-1])
    tokens = _assemble_model_tokens(prepared, len(close_times) - 1, anchor, intervals)
    # Live inference consumes one state; keep the batch dimension explicit so
    # it follows the same [batch, tokens, features] ABI as training.
    return (tokens[None, ...], np.zeros((len(CANDIDATE_PLAN_FEATURE_NAMES) if "CANDIDATE_PLAN_FEATURE_NAMES" in globals() else 1,), dtype=np.float32)) if tokens is not None else None


def _model_split_boundaries(config):
    warmup = max(WINDOWS[interval] * _legacy.INTERVAL_MS[interval] for interval in MACRO_INPUT_INTERVALS)
    profile = _holding_profile(config.get("holdingProfile") if isinstance(config, dict) else MODEL_HOLDING_PROFILE)
    reference_interval = str(profile["referenceInterval"])
    horizon = int(config.get("labelHorizonBars", profile["labelHorizonBars"])) * _legacy.INTERVAL_MS[reference_interval]
    start = FIXED_HISTORY_START_TIME + warmup
    end = FIXED_HISTORY_END_TIME - horizon
    span = end - start
    train_end = start + int(span * 0.62)
    validation_end = start + int(span * 0.82)
    return {
        "train": {"start": start, "end": train_end, "usableEnd": train_end - horizon},
        "validation": {"start": train_end + horizon, "end": validation_end, "usableEnd": validation_end - horizon},
        "test": {"start": validation_end + horizon, "end": end, "usableEnd": end},
    }


# The recovered implementation is still used for the model architecture and
# replay helpers, but all input/split helpers must use the current 5m ABI.
_legacy._assemble_tokens = _assemble_model_tokens
_legacy._model_inputs_from_live_frames = _model_inputs_from_live_frames
_legacy._split_boundaries = _model_split_boundaries


def _direct_number(prediction, name, default, low, high):
    try:
        value = float(prediction.get(name, default))
    except (TypeError, ValueError):
        value = default
    return float(np.clip(value if math.isfinite(value) else default, low, high))


def _normalize_model_target_ratios(ratio1, ratio2):
    """Return positive leg allocations within the executable target budget.

    This is geometry/execution normalization only.  It does not prefer either
    target and does not impose a minimum cumulative allocation for the second
    target.
    """

    first = max(5.0, float(ratio1) if math.isfinite(float(ratio1)) else 5.0)
    second = max(5.0, float(ratio2) if math.isfinite(float(ratio2)) else 5.0)
    if first + second > MAX_MODEL_FIXED_TARGET_RATIO:
        scale = MAX_MODEL_FIXED_TARGET_RATIO / max(first + second, 1e-12)
        first *= scale
        second *= scale
    return float(first), float(second)


def _swing_time_efficiency(realized_r, duration_bars, horizon_bars=None):
    """Measure capital efficiency without mechanically favouring scalps.

    ``R / sqrt(time)`` makes a two-hour trade dominate an equally sound
    multi-day trade by construction.  Swing training instead applies only a
    mild, horizon-normalised carrying cost; profitable plans are primarily
    ranked by outcome and drawdown, not by how quickly they finish.
    """

    try:
        reward = float(realized_r)
        duration = max(0.0, float(duration_bars))
        horizon = max(1.0, float(horizon_bars or DEFAULT_TRAINING_CONFIG.get("labelHorizonBars", 192)))
    except (TypeError, ValueError):
        return 0.0
    return reward / (1.0 + 0.12 * min(duration / horizon, 4.0))


def _bounded_entry_zone_atr(trigger_atr, zone_low_atr, zone_high_atr, risk_atr, first_target_atr, *, is_long=True):
    """Keep a model entry zone economically equivalent to its trigger.

    Stops and targets are intentionally defined from the trigger.  If a zone
    is allowed to be several ATR wide, an entry at its two ends has materially
    different loss distance and first-target R.  Compress only the span (not
    the trigger, stop, or targets) and retain any asymmetric shape proposed by
    the policy.  The conservative cap makes even a wholly one-sided zone move
    the first-target R by no more than ``MAX_ENTRY_ZONE_R_MULTIPLE_SPREAD``.
    """

    try:
        trigger = float(trigger_atr)
        low = float(zone_low_atr)
        high = float(zone_high_atr)
        risk = float(risk_atr)
        target = float(first_target_atr)
    except (TypeError, ValueError):
        return zone_low_atr, zone_high_atr, {}
    if not all(math.isfinite(value) for value in (trigger, low, high, risk, target)) or risk <= 0 or target <= 0:
        return min(low, trigger), max(high, trigger), {}

    low = min(low, trigger)
    high = max(high, trigger)
    raw_span = high - low
    first_r = target / risk
    max_span_from_r = risk * MAX_ENTRY_ZONE_R_MULTIPLE_SPREAD / (
        first_r + 1.0 + MAX_ENTRY_ZONE_R_MULTIPLE_SPREAD
    )
    max_span = max(0.0, min(risk * MAX_ENTRY_ZONE_RISK_FRACTION, max_span_from_r))
    if raw_span > max_span and raw_span > 0:
        scale = max_span / raw_span
        low = trigger - (trigger - low) * scale
        high = trigger + (high - trigger) * scale

    def r_multiple_at(entry):
        signed_delta = (entry - trigger) if is_long else (trigger - entry)
        loss = risk + signed_delta
        reward = target - signed_delta
        return reward / loss if loss > 0 else None

    r_low = r_multiple_at(low)
    r_trigger = r_multiple_at(trigger)
    r_high = r_multiple_at(high)
    valid_r = [value for value in (r_low, r_trigger, r_high) if value is not None and math.isfinite(value)]
    return low, high, {
        "version": ENTRY_ZONE_GEOMETRY_VERSION,
        "rawSpanAtr": raw_span,
        "maxSpanAtr": max_span,
        "spanAtr": high - low,
        "firstTargetRAtZoneLow": r_low,
        "firstTargetRAtTrigger": r_trigger,
        "firstTargetRAtZoneHigh": r_high,
        "firstTargetRSpread": max(valid_r) - min(valid_r) if valid_r else None,
        "maxFirstTargetRSpread": MAX_ENTRY_ZONE_R_MULTIPLE_SPREAD,
    }


def _sample_direct_policy_action(rng, side, config=None):
    """Sample a complete action with explicit allocation coverage.

    The replay corpus must expose the learner to materially different exit
    allocations at the same market state.  The allocation budget and split
    below are sampling ranges, not serving rules; every sampled action keeps
    its own realized outcome in the corpus.
    """

    action = np.asarray(_legacy._direct_policy_action_sample(rng, side), dtype=np.float32).copy()
    if action.shape != (len(DIRECT_PLAN_OUTPUT_NAMES) - 1,):
        raise ValueError("动作采样维度与直接计划输出不一致")
    names = {name: index for index, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
    profile = _holding_profile((config or {}).get("holdingProfile") if isinstance(config, dict) else MODEL_HOLDING_PROFILE)
    # Preserve the sampled trigger side.  Direction/sign consistency is not a
    # training-time gate: replay must retain both geometries and teach the
    # critic their actual fill and outcome distribution.  Only avoid a
    # zero-distance trigger, which is not an executable stop level.
    trigger_value = float(action[names["entryTriggerAtr"]])
    magnitude = abs(trigger_value) if math.isfinite(trigger_value) else 0.0
    magnitude = max(magnitude, 0.05)
    trigger_value = magnitude if rng.random() >= 0.5 else -magnitude
    action[names["entryTriggerAtr"]] = trigger_value
    stop_low, stop_high = profile["stopDistanceAtr"]
    target_low, target_high = profile["targetDistanceAtr"]
    # Sample physical risk and reward multiples independently.  The previous
    # target-distance sampler was clipped by ``risk * 3.8`` and concentrated
    # high-risk actions around roughly 1.2R, which made the top-ranked plans
    # look like copies of one narrow template.  This is exploration support,
    # not a serving-time minimum: replay outcomes still decide which geometry
    # is useful for each market state.
    risk_quantile = float(rng.random())
    if risk_quantile < 0.25:
        risk = float(rng.uniform(stop_low, stop_low + 0.35 * (stop_high - stop_low)))
    else:
        risk = float(rng.uniform(stop_low + 0.20 * (stop_high - stop_low), stop_high))
    # Oversample reachable first targets while retaining extension geometries.
    # This prevents the actor from regressing toward one remote two-R template.
    first_r = float(rng.uniform(0.9, 1.8) if rng.random() < 0.55 else rng.uniform(1.8, 4.5))
    second_r = first_r + float(rng.uniform(0.5, 4.5))
    first_distance = float(np.clip(risk * first_r, target_low, max(target_low, target_high - 0.10)))
    second_distance = float(np.clip(risk * second_r, first_distance + 0.05, target_high))
    action[names["stopDistanceAtr"]] = risk
    action[names["targetOneDistanceAtr"]] = first_distance
    action[names["targetTwoDistanceAtr"]] = second_distance
    # Draw the amount assigned to the two fixed targets independently from the
    # split between them.  This deliberately covers low, balanced, and
    # extension-heavy allocations instead of making the second leg a constant.
    target_budget = float(rng.uniform(0.32, 0.95) * 100.0)
    first_share = float(rng.uniform(0.08, 0.86))
    first_ratio = target_budget * first_share
    second_ratio = target_budget - first_ratio
    first_ratio, second_ratio = _normalize_model_target_ratios(first_ratio, second_ratio)
    action[names["targetOneRatio"]] = first_ratio
    action[names["targetTwoRatio"]] = second_ratio
    exit_bars = max(float(action[names["exitBars"]]), float(action[names["tp2Bars"]]), 1.0)
    risk_atr = max(float(action[names["stopDistanceAtr"]]), 1e-6)
    first_r = max(float(action[names["targetOneDistanceAtr"]]) / risk_atr, 0.0)
    action[names["timeEfficiency"]] = _swing_time_efficiency(first_r, exit_bars)
    action[names["holdingCost"]] = -float((config or {}).get("timeCostWeight", profile["timeCostWeight"])) * exit_bars
    return _clip_policy_action(action)


def _pending_action_at_anchor(frame, index, side, action, *, minimum_trigger_atr=0.02):
    """Return whether an action has a usable non-zero trigger for replay.

    This helper intentionally does not enforce a direction/sign convention;
    replay labels both crossed and un-crossed geometries using only the
    completed anchor candle and normalized trigger distance.  No future bar or
    1m data is consulted here.
    """
    try:
        names = {name: idx for idx, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
        trigger = float(np.asarray(action, dtype=np.float32)[names["entryTriggerAtr"]])
        close = float(frame["close"][int(index)])
        atr = max(float(frame.get("atr14", np.asarray([0.0]))[int(index)]), abs(close) * MIN_RISK_FRACTION, 1e-12)
        threshold = max(float(minimum_trigger_atr) * atr, abs(close) * 1e-10)
        trigger_price = close + trigger * atr
        # Do not use the side/sign as a training-time filter.  The historical
        # replay is the labeler: actions on either side are retained and a
        # stale/crossed trigger receives its actual no-fill outcome.  This
        # avoids the coverage loss caused by pre-filtering half the action
        # space while keeping a small non-zero trigger distance.
        return abs(trigger_price - close) > threshold
    except (KeyError, TypeError, ValueError, IndexError):
        return False
    return False


def _forecast_targets_at_anchor(frame, index):
    """Build causal training targets for the market-forecast head.

    Each horizon stores a conservative lower excursion, the endpoint log
    return, and an upper excursion.  These are quantile-like targets rather
    than a single point label, so the network learns both expected direction
    and the range of plausible outcomes used for plan risk sizing.
    """

    try:
        close = np.asarray(frame["close"], dtype=np.float64)
        high = np.asarray(frame["high"], dtype=np.float64)
        low = np.asarray(frame["low"], dtype=np.float64)
        anchor = float(close[int(index)])
        if not math.isfinite(anchor) or anchor <= 0:
            raise ValueError("invalid anchor close")
        values = []
        for horizon in FORECAST_HORIZONS:
            start = int(index) + 1
            end = min(len(close) - 1, int(index) + int(horizon))
            if start > end:
                endpoint = 0.0
                lower = 0.0
                upper = 0.0
            else:
                segment_high = np.asarray(high[start:end + 1], dtype=np.float64)
                segment_low = np.asarray(low[start:end + 1], dtype=np.float64)
                endpoint = math.log(max(float(close[end]), 1e-12) / anchor)
                lower = math.log(max(float(np.nanmin(segment_low)), 1e-12) / anchor)
                upper = math.log(max(float(np.nanmax(segment_high)), 1e-12) / anchor)
            values.extend((lower, endpoint, upper))
        result = np.asarray(np.clip(values, -2.0, 2.0), dtype=np.float32)
        far_index = 1 + 3 * (len(FORECAST_HORIZONS) - 1)
        direction = DIRECTION_LONG if result[far_index] > 0.001 else DIRECTION_SHORT if result[far_index] < -0.001 else DIRECTION_WAIT
        return result, int(direction)
    except (KeyError, TypeError, ValueError, IndexError):
        return np.zeros(len(FORECAST_OUTPUT_NAMES), dtype=np.float32), 1


def decode_direct_plan_prediction(prediction, *, reference_price, atr, symbol=None, metadata=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        reference = float(reference_price); safe_atr = max(float(atr), abs(reference) * MIN_RISK_FRACTION, 1e-12)
    except (TypeError, ValueError):
        reference, safe_atr = 0.0, 0.0
    direction = str(prediction.get("direction") or "WAIT").upper()
    base = {"symbol": symbol, "strategyEngine": "MODEL", "modelTask": MODEL_TASK_TYPE, "modelVersion": metadata.get("modelVersion") or MODEL_VERSION, "modelRunId": metadata.get("runId"), "modelBranch": metadata.get("modelBranch"), "modelReason": "MODEL_UNCERTAIN", "reason": "MODEL_UNCERTAIN"}
    if direction == "WAIT" or direction not in {"LONG", "SHORT"} or reference <= 0 or safe_atr <= 0:
        return {**base, "direction": "WAIT", "status": "WAIT", "entry": {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"}, "stopLoss": None, "takeProfits": [], "trailingStop": {}, "timeCost": {}}
    sign = 1.0 if direction == "LONG" else -1.0
    trigger = reference + _direct_number(prediction, "entryTriggerAtr", 0.0, -6.0, 6.0) * safe_atr
    # Keep the legacy zone fields in the output schema, but do not expose a
    # tradable interval.  A model plan has one concrete entry price only.
    # A direct plan may describe either a breakout stop or a pullback limit.
    # The sign of the trigger is therefore not tied to LONG/SHORT: LONG below
    # reference and SHORT above reference are valid pending pullbacks.  Keep
    # the order semantics on the decoded plan so the execution gate does not
    # accidentally discard that half of the action space.
    order_type = (
        "STOP"
        if (direction == "LONG" and trigger >= reference) or (direction == "SHORT" and trigger <= reference)
        else "LIMIT"
    )
    risk_atr = _direct_number(prediction, "stopDistanceAtr", 1.5, 0.5, 12.0)
    first_atr = _direct_number(prediction, "targetOneDistanceAtr", 2.0, 0.25, 20.0)
    second_atr = max(first_atr + 0.05, _direct_number(prediction, "targetTwoDistanceAtr", 4.0, 0.35, 30.0))
    trigger_atr = (trigger - reference) / safe_atr
    zone_low = trigger
    zone_high = trigger
    _zone_low, _zone_high, zone_geometry = _bounded_entry_zone_atr(
        trigger_atr,
        trigger_atr,
        trigger_atr,
        risk_atr,
        first_atr,
        is_long=direction == "LONG",
    )
    stop = trigger - sign * risk_atr * safe_atr
    first = trigger + sign * first_atr * safe_atr
    second = trigger + sign * second_atr * safe_atr
    # Ratios are leg allocations.  Keep a small runner for the moving stop
    # and expose cumulative boundaries as well, because the execution layer
    # consumes cumulative ratios while replay consumes leg ratios.
    ratio1, ratio2 = _normalize_model_target_ratios(
        _direct_number(prediction, "targetOneRatio", 50.0, 5.0, 90.0),
        _direct_number(prediction, "targetTwoRatio", 40.0, 5.0, 90.0),
    )
    cumulative_first = ratio1
    cumulative_second = ratio1 + ratio2
    initial_r = _direct_number(prediction, "initialActivationR", 1.0, 0.05, 8.0)
    be_r = max(initial_r, _direct_number(prediction, "breakevenTriggerR", 1.0, 0.05, 8.0))
    be_buffer = _direct_number(prediction, "breakevenBufferAtr", 0.0, 0.0, 2.0)
    lookback = int(round(_direct_number(prediction, "structureLookbackBars", 6.0, 1.0, 96.0)))
    structure_buffer = _direct_number(prediction, "structureBufferAtr", 0.0, 0.0, 4.0)
    atr_period = int(round(_direct_number(prediction, "atrPeriod", 14.0, 2.0, 96.0)))
    atr_multiplier = _direct_number(prediction, "atrMultiplier", 2.5, 0.1, 12.0)
    tp1_bars = max(4.0, first_atr, _direct_number(prediction, "tp1Bars", 24.0, 0.0, 672.0)); tp2_bars = max(tp1_bars + 1.0, second_atr, _direct_number(prediction, "tp2Bars", 64.0, 0.0, 672.0)); exit_bars = max(tp2_bars + 1.0, _direct_number(prediction, "exitBars", 192.0, 0.0, 672.0))
    r_multiple = abs(first - trigger) / max(abs(trigger - stop), 1e-12)
    holding_cost_rate = _direct_number(metadata, "timeCostWeight", 0.0008, 0.0, 0.2)
    return {**base, "direction": direction, "status": "ARMED", "modelReason": "ACTION_CONDITIONED_SELECTION", "reason": "ACTION_CONDITIONED_SELECTION", "entry": {"trigger": trigger, "zoneLow": zone_low, "zoneHigh": zone_high, "type": "MODEL_DIRECT", "orderType": order_type, "referencePrice": reference, "zoneGeometry": zone_geometry}, "stopLoss": stop, "takeProfits": [{"role": "FIRST_TARGET", "price": first, "ratio": ratio1, "legRatio": ratio1, "cumulativeRatio": cumulative_first, "rMultiple": r_multiple}, {"role": "EXTENSION_TARGET", "price": second, "ratio": ratio2, "legRatio": ratio2, "cumulativeRatio": cumulative_second, "rMultiple": abs(second - trigger) / max(abs(trigger - stop), 1e-12)}], "riskReward": r_multiple, "trailingStop": {"INITIAL": {"activationR": initial_r}, "BREAKEVEN": {"triggerR": be_r, "bufferAtr": be_buffer}, "STRUCTURE_TRAILING": {"lookbackBars": lookback, "bufferAtr": structure_buffer}, "ATR_TRAILING": {"period": atr_period, "multiplier": atr_multiplier}}, "timeCost": {"tp1Bars": tp1_bars, "tp2Bars": tp2_bars, "exitBars": exit_bars, "efficiency": _swing_time_efficiency(r_multiple, exit_bars), "holdingCost": -holding_cost_rate * exit_bars}}


def _direct_model_entry_state(plan, *, fallback_price=None, latest_bar=None, observed_bars=None):
    """Describe whether a direct-model trigger is still pending.

    Direct model plans have one concrete entry price. A point crossed by the
    current quote or an observed bar is an invalidated setup, not a price to
    re-arm.
    """

    if not isinstance(plan, dict):
        return {"state": "UNKNOWN", "pending": True, "currentPrice": None}
    direction = str(plan.get("direction") or "").strip().upper()
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    try:
        trigger = float(entry.get("trigger"))
    except (TypeError, ValueError):
        return {"state": "UNKNOWN", "pending": True, "currentPrice": None}
    zone_low = trigger
    zone_high = trigger
    order_type = str(entry.get("orderType") or "").strip().upper()
    raw_current = plan.get("lastPrice")
    if raw_current is None:
        raw_current = plan.get("latestPrice")
    if raw_current is None:
        raw_current = plan.get("markPrice")
    if raw_current is None:
        raw_current = fallback_price
    try:
        current = float(raw_current)
    except (TypeError, ValueError):
        current = None
    values = (trigger, zone_low, zone_high)
    if current is None or not math.isfinite(current) or current <= 0 or not all(math.isfinite(value) for value in values):
        return {"state": "UNKNOWN", "pending": True, "currentPrice": None, "trigger": trigger, "zoneLow": zone_low, "zoneHigh": zone_high}
    if order_type not in {"STOP", "LIMIT"}:
        # Persisted/hand-built plans from before the explicit field can still
        # be interpreted without imposing a LONG/SHORT sign convention.
        order_type = (
            "STOP"
            if (direction == "LONG" and trigger >= current) or (direction == "SHORT" and trigger <= current)
            else "LIMIT"
        )
    epsilon = max(abs(trigger) * 1e-10, 1e-12)
    bars_to_check = []
    if isinstance(latest_bar, dict):
        bars_to_check.append(latest_bar)
    if isinstance(observed_bars, (list, tuple)):
        bars_to_check.extend(item for item in observed_bars if isinstance(item, dict))
    bar_ranges = []
    for bar in bars_to_check:
        try:
            candidate_high = float(bar.get("high"))
            candidate_low = float(bar.get("low"))
            if math.isfinite(candidate_high) and math.isfinite(candidate_low):
                bar_ranges.append((candidate_high, candidate_low))
        except (TypeError, ValueError):
            continue
    # Both order geometries are valid.  STOP entries trigger in the direction
    # of the move (LONG above / SHORT below); LIMIT entries wait for a
    # pullback (LONG below / SHORT above).  The previous implementation used
    # only the STOP test, incorrectly rejecting every valid pullback plan.
    if order_type == "STOP":
        bar_crossed = any(
            (direction == "LONG" and high >= trigger - epsilon)
            or (direction == "SHORT" and low <= trigger + epsilon)
            for high, low in bar_ranges
        )
        quote_crossed = (direction == "LONG" and current >= trigger - epsilon) or (direction == "SHORT" and current <= trigger + epsilon)
    else:
        bar_crossed = any(
            (direction == "LONG" and low <= trigger + epsilon)
            or (direction == "SHORT" and high >= trigger - epsilon)
            for high, low in bar_ranges
        )
        quote_crossed = (direction == "LONG" and current <= trigger + epsilon) or (direction == "SHORT" and current >= trigger - epsilon)
    if bar_crossed or quote_crossed:
        state, pending, reason = "TRIGGER_ALREADY_CROSSED", False, "MODEL_TRIGGER_ALREADY_CROSSED"
    elif direction == "LONG":
        state = "WAITING_BELOW_ZONE" if current < trigger else "WAITING_ABOVE_ZONE"
        pending, reason = True, "MODEL_TRIGGER_PENDING"
    elif direction == "SHORT":
        state = "WAITING_ABOVE_ZONE" if current > trigger else "WAITING_BELOW_ZONE"
        pending, reason = True, "MODEL_TRIGGER_PENDING"
    else:
        state, pending, reason = "UNKNOWN", True, None
    return {
        "state": state,
        "pending": pending,
        "reason": reason,
        "currentPrice": current,
        "trigger": trigger,
        "zoneLow": zone_low,
        "zoneHigh": zone_high,
        "orderType": order_type,
        "latestBarCrossed": bool(bar_crossed),
    }


def _rearm_direct_model_trigger(plan, *, current_price, latest_bar, atr):
    """Re-arm a stale STOP/LIMIT trigger beyond the latest closed bar.

    The actor is trained around the bar close, while a live stop order cannot
    be armed after that same bar has already traded through its trigger.  For
    the bounded coverage fallback only, preserve the model geometry and shift
    the complete plan together with the trigger to the next safe side of the
    bar.  This creates a genuinely pending order instead of returning a
    direction that is immediately discarded as stale.
    """
    if not isinstance(plan, dict) or not isinstance(latest_bar, dict):
        return plan
    direction = str(plan.get("direction") or "").upper()
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    order_type = str(entry.get("orderType") or "STOP").upper()
    if order_type not in {"STOP", "LIMIT"}:
        order_type = "STOP"
    try:
        trigger = float(entry.get("trigger")); current = float(current_price); safe_atr = max(float(atr), abs(current) * MIN_RISK_FRACTION, 1e-12)
        high = float(latest_bar.get("high")); low = float(latest_bar.get("low"))
    except (TypeError, ValueError):
        return plan
    if order_type == "STOP":
        if direction == "LONG":
            if trigger > max(current, high):
                return plan
            safe_trigger = max(trigger, current, high) + 0.05 * safe_atr
        elif direction == "SHORT":
            if trigger < min(current, low):
                return plan
            safe_trigger = min(trigger, current, low) - 0.05 * safe_atr
        else:
            return plan
    else:
        # A LIMIT plan is the mirror image: LONG waits below the market and
        # SHORT waits above it.  If the old limit was touched, place the same
        # geometry just beyond the latest adverse excursion instead of
        # discarding the forecast as an already-missed stop entry.
        if direction == "LONG":
            if trigger < min(current, low):
                return plan
            safe_trigger = min(trigger, current, low) - 0.05 * safe_atr
        elif direction == "SHORT":
            if trigger > max(current, high):
                return plan
            safe_trigger = max(trigger, current, high) + 0.05 * safe_atr
        else:
            return plan
    delta = safe_trigger - trigger
    if abs(delta) <= 1e-12:
        return plan
    shifted = dict(plan)
    shifted_entry = dict(entry)
    for key in ("trigger", "zoneLow", "zoneHigh"):
        try:
            shifted_entry[key] = float(entry[key]) + delta
        except (TypeError, ValueError, KeyError):
            pass
    shifted["entry"] = shifted_entry
    try:
        shifted["stopLoss"] = float(plan.get("stopLoss")) + delta
    except (TypeError, ValueError):
        pass
    shifted_targets = []
    for target in plan.get("takeProfits") or []:
        if not isinstance(target, dict):
            continue
        item = dict(target)
        try:
            item["price"] = float(target.get("price")) + delta
        except (TypeError, ValueError):
            pass
        shifted_targets.append(item)
    shifted["takeProfits"] = shifted_targets
    shifted["entryRearmed"] = True
    shifted["entryRearmDelta"] = delta
    return shifted


def _validate_direct_plan(plan):
    direction = str(plan.get("direction") or "").upper()
    if direction == "WAIT": return True, None
    if direction not in {"LONG", "SHORT"}: return False, "direction_invalid"
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    try: trigger, low, high, stop = float(entry["trigger"]), float(entry["zoneLow"]), float(entry["zoneHigh"]), float(plan["stopLoss"])
    except (KeyError, TypeError, ValueError): return False, "price_missing"
    targets = [item for item in plan.get("takeProfits", []) if isinstance(item, dict)]
    if len(targets) < 2: return False, "targets_missing"
    try: first, second = float(targets[0]["price"]), float(targets[1]["price"])
    except (KeyError, TypeError, ValueError): return False, "target_price_invalid"
    valid = stop < trigger < first < second and low <= trigger <= high if direction == "LONG" else second < first < trigger < stop and low <= trigger <= high
    if not valid: return False, "price_order_invalid"
    ratios = [float(item.get("ratio")) for item in targets[:2]]
    return (True, None) if all(math.isfinite(value) and value > 0 for value in ratios) and sum(ratios) <= 100.0001 else (False, "ratio_invalid")


def _direct_5m_atr_from_frames(frames):
    """Return the direct model's ATR at its 5m execution/reference scale."""

    frame = _model_prepared_frame((frames or {}).get(MODEL_REFERENCE_INTERVAL))
    # Keep the old diagnostic helper usable for historical 15m-only fixtures;
    # production inference/training always supplies 5m and never takes this
    # compatibility branch.
    if frame is None and MODEL_REFERENCE_INTERVAL != "15m":
        frame = _model_prepared_frame((frames or {}).get("15m"))
    return float(frame["atr14"][-1]) if frame is not None and len(frame.get("atr14", [])) else None


def _direct_15m_atr_from_frames(frames):
    """Backward-compatible name; direct plans now use 5m ATR."""

    return _direct_5m_atr_from_frames(frames)


def _direct_plan_from_frames(frames):
    frame = _model_prepared_frame((frames or {}).get(MODEL_REFERENCE_INTERVAL))
    # Compatibility-only fallback for callers/tests that still pass the old
    # 15m diagnostic fixture.  The live MODEL route requires 5m.
    if frame is None and MODEL_REFERENCE_INTERVAL != "15m":
        frame = _model_prepared_frame((frames or {}).get("15m"))
    if frame is None or len(frame["close"]) < 20: return None
    close, atr = float(frame["close"][-1]), float(frame["atr14"][-1]); slope = float(frame["features"][-1, FEATURE_NAMES.index("momentum_4")])
    direction = "LONG" if slope > 0.002 else "SHORT" if slope < -0.002 else "WAIT"
    prediction = {"direction": direction, "entryTriggerAtr": 0.0, "entryZoneLowAtr": -0.3, "entryZoneHighAtr": 0.3, "stopDistanceAtr": 1.0, "targetOneDistanceAtr": 1.5, "targetTwoDistanceAtr": 2.5, "targetOneRatio": 50.0, "targetTwoRatio": 50.0}
    result = decode_direct_plan_prediction(prediction, reference_price=close, atr=atr)
    result["strategyEngine"] = "MODEL"; result["modelGenerated"] = False; result["modelReason"] = "FALLBACK_DIAGNOSTIC_ONLY"
    return result


def _direct_teacher_target(plan, outcome, atr):
    entry = float((plan.get("entry") or {}).get("trigger") or 0.0); stop = float(plan.get("stopLoss") or entry); targets = plan.get("takeProfits") or []; first = float(targets[0].get("price") or entry) if targets else entry; second = float(targets[1].get("price") or first) if len(targets) > 1 else first; risk = max(abs(entry - stop), 1e-12)
    values = np.asarray([0.0, 0.0, 0.0, abs(entry - stop) / max(float(atr), 1e-12), abs(first - entry) / max(float(atr), 1e-12), abs(second - entry) / max(float(atr), 1e-12)], dtype=np.float32)
    return values, None


class _DirectPolicyReservoir:
    """Compact grouped corpus: one state tensor, many action outcomes."""

    def __init__(self, capacity: int, token_count: int, seed: int) -> None:
        self.capacity = max(1, int(capacity))
        self.token_count = max(1, int(token_count))
        self.features = np.empty((self.capacity, self.token_count, FEATURE_DIM), dtype=np.float32)
        self.state_features = self.features
        self.state_symbols = np.empty(self.capacity, dtype="U32")
        self.state_times = np.empty(self.capacity, dtype=np.int64)
        self.state_ids = np.empty(self.capacity, dtype=np.int32)
        self.actions = np.empty((self.capacity, len(DIRECT_PLAN_OUTPUT_NAMES) - 1), dtype=np.float32)
        self.directions = np.empty(self.capacity, dtype=np.int64)
        self.utility = np.empty(self.capacity, dtype=np.float32)
        self.realized_r = np.empty(self.capacity, dtype=np.float32)
        self.duration_bars = np.empty(self.capacity, dtype=np.float32)
        self.regression = np.empty((self.capacity, 3), dtype=np.float32)
        self.forecast_returns = np.empty((self.capacity, len(FORECAST_OUTPUT_NAMES)), dtype=np.float32)
        self.forecast_direction = np.empty(self.capacity, dtype=np.int64)
        self.outcomes = np.empty(self.capacity, dtype=np.int8)
        self.entered = np.empty(self.capacity, dtype=np.int8)
        self.symbols = np.empty(self.capacity, dtype="U32")
        self.times = np.empty(self.capacity, dtype=np.int64)
        self.rollout_count = np.empty(self.capacity, dtype=np.int16)
        self.size = 0
        self.state_count = 0
        self.seen = 0
        self._lookup: dict[tuple[str, int], int] = {}

    def add(self, features, action, direction, utility, realized_r, duration_bars, regression, symbol, anchor_time, rollout_count, *, entered=False, outcome="EXPIRE", forecast_returns=None, forecast_direction=1):
        self.seen += 1
        if self.size >= self.capacity:
            return False
        key = (str(symbol), int(anchor_time))
        state_id = self._lookup.get(key)
        if state_id is None:
            if self.state_count >= self.capacity:
                return False
            state_id = self.state_count
            self.state_count += 1
            self._lookup[key] = state_id
            self.features[state_id] = np.asarray(features, dtype=np.float32)
            self.state_symbols[state_id] = str(symbol)
            self.state_times[state_id] = int(anchor_time)
        index = self.size
        self.size += 1
        self.state_ids[index] = int(state_id)
        self.actions[index] = np.asarray(action, dtype=np.float32)
        self.directions[index] = int(direction)
        self.utility[index] = float(utility)
        self.realized_r[index] = float(realized_r)
        self.duration_bars[index] = float(duration_bars)
        self.regression[index] = np.asarray(regression, dtype=np.float32)
        self.forecast_returns[index] = np.asarray(forecast_returns if forecast_returns is not None else np.zeros(len(FORECAST_OUTPUT_NAMES)), dtype=np.float32)
        self.forecast_direction[index] = int(forecast_direction)
        self.entered[index] = int(bool(entered))
        self.outcomes[index] = {"STOP": OUTCOME_STOP, "EXPIRE": OUTCOME_EXPIRE, "TARGET": OUTCOME_TARGET}.get(str(outcome).upper(), OUTCOME_EXPIRE)
        self.symbols[index] = str(symbol)
        self.times[index] = int(anchor_time)
        self.rollout_count[index] = int(rollout_count)
        return True

    @property
    def transition_features(self):
        return self.features[self.state_ids[:self.size]] if self.size else np.empty((0, self.token_count, FEATURE_DIM), dtype=np.float32)

    def summary(self):
        size = int(self.size)
        return {
            "sampleCount": size,
            "actionCount": size,
            "stateCount": int(self.state_count),
            "seenTransitions": int(self.seen),
            "directionCounts": {DIRECTION_LABELS[index]: int(np.sum(self.directions[:size] == index)) for index in range(len(DIRECTION_LABELS))},
            "outcomeCounts": {OUTCOME_LABELS[index]: int(np.sum(self.outcomes[:size] == index)) for index in range(len(OUTCOME_LABELS))},
            "positiveUtilityCount": int(np.sum(self.utility[:size] > 0.0)),
            "meanUtility": round(float(np.mean(self.utility[:size])), 6) if size else None,
            "meanRealizedR": round(float(np.mean(self.realized_r[:size])), 6) if size else None,
            "meanDurationBars": round(float(np.mean(self.duration_bars[:size])), 3) if size else None,
            "symbolCount": int(len(set(self.symbols[:size].tolist()))) if size else 0,
        }


class _TorchDirectPolicyDataset:
    def __init__(self, torch, reservoir, normalization, intervals):
        self.torch = torch
        self.reservoir = reservoir
        self.size = int(reservoir.size)
        self.features = _legacy._normalize_token_array(reservoir.transition_features, normalization, intervals)

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        return (
            self.torch.from_numpy(self.features[index]),
            self.torch.from_numpy(self.reservoir.actions[index]),
            self.torch.tensor(self.reservoir.directions[index], dtype=self.torch.long),
            self.torch.tensor(self.reservoir.utility[index], dtype=self.torch.float32),
            self.torch.tensor(self.reservoir.realized_r[index], dtype=self.torch.float32),
            self.torch.tensor(self.reservoir.duration_bars[index], dtype=self.torch.float32),
            self.torch.from_numpy(self.reservoir.regression[index]),
            self.torch.from_numpy(self.reservoir.forecast_returns[index]),
            self.torch.tensor(self.reservoir.forecast_direction[index], dtype=self.torch.long),
            self.torch.tensor(self.reservoir.outcomes[index], dtype=self.torch.long),
            self.torch.tensor(self.reservoir.entered[index], dtype=self.torch.float32),
            self.torch.tensor(self.reservoir.state_ids[index], dtype=self.torch.long),
        )


def _build_direct_policy_normalization(reservoir, intervals):
    if reservoir.state_count <= 0:
        raise ValueError("训练样本为空，无法拟合特征标准化参数")
    result = {"type": "per_timeframe_zscore_v33_profit_calibrated_profiled_wide_horizon", "clip": 8.0, "intervals": {}}
    offset = 0
    state_values = reservoir.features[:reservoir.state_count]
    for interval in intervals:
        width = WINDOWS[interval]
        values = state_values[:, offset:offset + width, :].reshape(-1, FEATURE_DIM).astype(np.float64)
        result["intervals"][interval] = {
            "mean": np.mean(values, axis=0).tolist(),
            "std": np.maximum(np.std(values, axis=0), 1e-5).tolist(),
        }
        offset += width
    result["actionFeatures"] = {"names": list(DIRECT_PLAN_OUTPUT_NAMES[1:]), "source": "all_historical_action_outcomes"}
    result["forecastFeatures"] = {"names": list(FORECAST_OUTPUT_NAMES), "horizonsBars": list(FORECAST_HORIZONS), "quantiles": list(FORECAST_QUANTILES), "target": "future_return_distribution_log_return"}
    result["planQualityFeatures"] = {"names": list(PLAN_QUALITY_OUTPUT_NAMES), "target": "causal_action_conditioned_replay_outcome"}
    return result


def _build_retrieval_bank(reservoir, normalization, intervals, config):
    """Persist a compact, training-only state/action memory for serving.

    RAFT-style retrieval is useful here because the historical market corpus
    already contains realised outcomes for many geometrically different plans.
    Keep the highest-utility action plus one materially different geometry per
    causal training state, then retain a diverse rank-stratified subset.  The bank is never built from the
    validation/test periods, so it cannot leak their future paths into serving
    or reported metrics.
    """

    state_count = int(getattr(reservoir, "state_count", 0) or 0)
    transition_count = int(getattr(reservoir, "size", 0) or 0)
    if state_count <= 0 or transition_count <= 0:
        return None
    state_ids = np.asarray(reservoir.state_ids[:transition_count], dtype=np.int64)
    directions = np.asarray(reservoir.directions[:transition_count], dtype=np.int64)
    utilities = np.asarray(reservoir.utility[:transition_count], dtype=np.float64)
    entered = np.asarray(reservoir.entered[:transition_count], dtype=np.int8)
    candidates = []
    action_names = DIRECT_PLAN_OUTPUT_NAMES[1:]
    action_index = {name: index for index, name in enumerate(action_names)}
    diversity_fields = [action_index[name] for name in ("targetOneDistanceAtr", "targetTwoDistanceAtr", "initialActivationR", "breakevenTriggerR", "targetOneRatio")]
    for state_id in range(state_count):
        rows = np.flatnonzero((state_ids == state_id) & (directions != DIRECTION_WAIT))
        if not len(rows):
            continue
        # Prefer actions that did execute when utilities are otherwise close;
        # retain a second geometry so the retrieval bank cannot collapse every
        # state to the same approximately-two-R plan.
        ordered = sorted(rows.tolist(), key=lambda row: (float(utilities[row]), int(entered[row]), -int(row)), reverse=True)
        best = int(ordered[0])
        candidates.append(best)
        if len(ordered) > 1:
            shortlist = ordered[: max(2, min(len(ordered), 6))]
            anchor = np.asarray(reservoir.actions[best], dtype=np.float64)[diversity_fields]
            diverse = max(
                shortlist[1:],
                key=lambda row: float(np.linalg.norm(np.asarray(reservoir.actions[row], dtype=np.float64)[diversity_fields] - anchor))
                + 0.05 * float(utilities[row]),
            )
            if diverse != best:
                candidates.append(int(diverse))
    if not candidates:
        return None
    candidates = np.asarray(candidates, dtype=np.int64)
    order = candidates[np.argsort(utilities[candidates])[::-1]]
    bank_size = min(max(1, int(config.get("retrievalBankSize", 256))), len(order))
    if len(order) > bank_size:
        # Take a quality-weighted frontier plus evenly spaced remaining ranks.
        top_count = max(1, bank_size // 2)
        tail_count = bank_size - top_count
        tail = np.linspace(top_count, len(order) - 1, num=tail_count, dtype=np.int64) if tail_count else np.empty(0, dtype=np.int64)
        selected = np.concatenate((order[:top_count], order[tail]))
    else:
        selected = order
    selected = np.unique(selected)[:bank_size]
    selected_state_ids = state_ids[selected]
    raw_features = np.asarray(reservoir.features[selected_state_ids], dtype=np.float32)
    features = _legacy._normalize_token_array(raw_features, normalization, intervals).astype(np.float32)
    return {
        "version": RETRIEVAL_BANK_VERSION,
        "features": features,
        "actions": np.asarray(reservoir.actions[selected], dtype=np.float32),
        "directions": np.asarray(directions[selected], dtype=np.int64),
        "utilities": np.asarray(utilities[selected], dtype=np.float32),
        "entered": np.asarray(entered[selected], dtype=np.int8),
        "stateCount": int(len(selected)),
        "source": "train_only_best_replayed_action_per_state",
    }


def _create_direct_policy_model(torch, config):
    nn = torch.nn
    functional = torch.nn.functional
    intervals = tuple(MACRO_INPUT_INTERVALS)
    token_count = sum(WINDOWS[interval] for interval in intervals)
    action_dim = len(DIRECT_PLAN_OUTPUT_NAMES) - 1
    action_names = DIRECT_PLAN_OUTPUT_NAMES[1:]
    ratio_indices = {
        "first": action_names.index("targetOneRatio"),
        "second": action_names.index("targetTwoRatio"),
    }
    timeframe_ids = np.concatenate([np.full(WINDOWS[interval], index, dtype=np.int64) for index, interval in enumerate(intervals)])
    latest_indices = np.cumsum([WINDOWS[interval] for interval in intervals], dtype=np.int64) - 1

    class MarketToPlanPolicy(nn.Module):
        def __init__(self):
            super().__init__()
            width = int(config["dModel"])
            direction_width = max(8, width // 4)
            self.input_projection = nn.Sequential(nn.Linear(FEATURE_DIM, width), nn.LayerNorm(width), nn.GELU())
            self.timeframe_embedding = nn.Embedding(len(intervals), width)
            self.position_embedding = nn.Parameter(torch.zeros(1, token_count + 1, width))
            self.class_token = nn.Parameter(torch.zeros(1, 1, width))
            self.latest_projection = nn.Sequential(nn.Linear(len(intervals) * FEATURE_DIM, width), nn.LayerNorm(width), nn.GELU())
            layer = nn.TransformerEncoderLayer(
                d_model=width,
                nhead=int(config["numHeads"]),
                dim_feedforward=width * 3,
                dropout=float(config["dropout"]),
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.encoder = nn.TransformerEncoder(layer, num_layers=int(config["numLayers"]))
            # Patch-style scale branches retain local microstructure while
            # reducing each resolution to a compact representation.  The
            # shared token encoder above captures cross-timeframe relations;
            # these branches add the decomposed fine/coarse context advocated
            # by PatchTST and TimeMixer without changing the input ABI.
            patch_sizes = {"4h": 4, "1h": 4, "15m": 4, "5m": 8}
            self.scale_patch = nn.ModuleDict()
            for interval in intervals:
                patch = min(int(patch_sizes.get(interval, 4)), max(1, int(WINDOWS[interval])))
                self.scale_patch[interval] = nn.Sequential(
                    nn.Conv1d(FEATURE_DIM, width, kernel_size=patch, stride=patch),
                    nn.GELU(),
                    nn.Conv1d(width, width, kernel_size=1),
                    nn.AdaptiveAvgPool1d(1),
                )
            self.scale_fusion = nn.Sequential(
                nn.Linear(width * len(intervals), width),
                nn.LayerNorm(width),
                nn.GELU(),
            )
            self.state_fusion = nn.Sequential(nn.Linear(width * 3, width), nn.LayerNorm(width), nn.GELU(), nn.Dropout(float(config["dropout"])))
            # Learn the exit allocation as a distribution over first target,
            # extension target and trailing-stop remainder.  This replaces the
            # old independent ratio regressors, which tended to collapse to a
            # low average leg allocation.
            # The plan is generated from both the encoded market state and the
            # probabilistic future forecast, rather than learning a detached
            # action vector that only imitates historical proposals.
            self.forecast_projection = nn.Sequential(nn.Linear(len(FORECAST_OUTPUT_NAMES) + len(DIRECTION_LABELS), width), nn.LayerNorm(width), nn.GELU())
            self.plan_fusion = nn.Sequential(nn.Linear(width * 2, width), nn.LayerNorm(width), nn.GELU())
            self.actor_plan_head = nn.Sequential(nn.Linear(width, width), nn.GELU(), nn.Linear(width, action_dim))
            self.actor_allocation_head = nn.Sequential(nn.Linear(width, width), nn.GELU(), nn.Linear(width, 3))
            self.actor_direction_head = nn.Linear(width, len(DIRECTION_LABELS))
            # Direct multi-horizon probabilistic forecast.  The three values
            # per horizon approximate lower/median/upper return quantiles and
            # are independent of a proposed trading action.
            self.forecast_head = nn.Linear(width, len(FORECAST_OUTPUT_NAMES))
            self.forecast_direction_head = nn.Linear(width, len(DIRECTION_LABELS))
            self.action_projection = nn.Sequential(nn.Linear(action_dim, width), nn.LayerNorm(width), nn.GELU())
            self.direction_embedding = nn.Embedding(len(DIRECTION_LABELS), direction_width)
            self.critic_fusion = nn.Sequential(nn.Linear(width * 2 + direction_width, width), nn.LayerNorm(width), nn.GELU(), nn.Dropout(float(config["dropout"])))
            self.utility_quantile_head = nn.Linear(width, len(config["criticQuantiles"]))
            self.drawdown_quantile_head = nn.Linear(width, 2)
            self.fill_head = nn.Linear(width, 1)
            self.target_head = nn.Linear(width, 1)
            self.outcome_head = nn.Linear(width, len(OUTCOME_LABELS))
            self.duration_head = nn.Linear(width, 1)
            self.regression_head = nn.Linear(width, 3)
            # Decision-focused quality vector.  This is learned from the
            # realised replay outcome of the concrete plan action, so the
            # selector can rank plans by expected R, win probability, risk,
            # time efficiency and confidence instead of treating one critic
            # quantile as a proxy for all five factors.
            self.plan_quality_head = nn.Sequential(
                nn.Linear(width, width),
                nn.LayerNorm(width),
                nn.GELU(),
                nn.Linear(width, len(PLAN_QUALITY_OUTPUT_NAMES)),
            )
            self.register_buffer("timeframe_ids", torch.from_numpy(timeframe_ids), persistent=False)
            self.register_buffer("latest_indices", torch.from_numpy(latest_indices), persistent=False)
            nn.init.normal_(self.position_embedding, mean=0.0, std=0.02)
            nn.init.normal_(self.class_token, mean=0.0, std=0.02)

        def encode(self, inputs):
            batch_size = inputs.shape[0]
            values = self.input_projection(inputs)
            values = values + self.timeframe_embedding(self.timeframe_ids).unsqueeze(0) + self.position_embedding[:, 1:, :]
            latest = inputs.index_select(1, self.latest_indices).reshape(batch_size, -1)
            latest_context = self.latest_projection(latest)
            cls = self.class_token.expand(batch_size, -1, -1) + self.position_embedding[:, :1, :]
            encoded = self.encoder(torch.cat((cls, values), dim=1))[:, 0, :]
            scale_contexts = []
            offset = 0
            for interval in intervals:
                width_i = int(WINDOWS[interval])
                segment = inputs[:, offset:offset + width_i, :].transpose(1, 2)
                scale_contexts.append(self.scale_patch[interval](segment).squeeze(-1))
                offset += width_i
            scale_context = self.scale_fusion(torch.cat(scale_contexts, dim=1))
            return self.state_fusion(torch.cat((encoded, latest_context, scale_context), dim=1))

        def forward(self, inputs, actions=None, directions=None):
            state = self.encode(inputs)
            forecast = self.forecast_head(state)
            forecast_direction = self.forecast_direction_head(state)
            # Monotonic quantile parameterization prevents q10/q50/q90
            # crossings, a common failure mode of independent regressors.
            forecast = forecast.view(forecast.shape[0], len(FORECAST_HORIZONS), len(FORECAST_QUANTILES))
            q10 = forecast[:, :, 0]
            q50 = q10 + functional.softplus(forecast[:, :, 1])
            q90 = q50 + functional.softplus(forecast[:, :, 2])
            forecast = torch.stack((q10, q50, q90), dim=2).reshape(forecast.shape[0], -1)
            forecast_prob = torch.softmax(forecast_direction, dim=1)
            plan_context = self.forecast_projection(torch.cat((forecast, forecast_prob), dim=1))
            plan_state = self.plan_fusion(torch.cat((state, plan_context), dim=1))
            actor_plan = self.actor_plan_head(plan_state)
            allocation_logits = self.actor_allocation_head(plan_state)
            allocation = torch.softmax(allocation_logits, dim=1) * 100.0
            actor_plan = actor_plan.clone()
            actor_plan[:, ratio_indices["first"]] = allocation[:, 0]
            actor_plan[:, ratio_indices["second"]] = allocation[:, 1]
            actor_direction = self.actor_direction_head(plan_state)
            if actions is None:
                actions = actor_plan
            if directions is None:
                directions = torch.argmax(actor_direction, dim=1)
            action_context = self.action_projection(actions)
            direction_context = self.direction_embedding(torch.clamp(directions.long(), 0, len(DIRECTION_LABELS) - 1))
            critic = self.critic_fusion(torch.cat((state, action_context, direction_context), dim=1))
            utility_quantiles = self.utility_quantile_head(critic)
            drawdown_quantiles = functional.softplus(self.drawdown_quantile_head(critic))
            quality_raw = self.plan_quality_head(critic)
            quality = torch.stack((
                torch.tanh(quality_raw[:, 0]),
                torch.sigmoid(quality_raw[:, 1]),
                functional.softplus(quality_raw[:, 2]),
                torch.tanh(quality_raw[:, 3]),
                torch.sigmoid(quality_raw[:, 4]),
            ), dim=1)
            return {
                "directPlan": actor_plan,
                "allocationLogits": allocation_logits,
                "allocation": allocation,
                "directDirection": actor_direction,
                "forecast": forecast,
                "forecastDirection": forecast_direction,
                "utilityQuantiles": utility_quantiles,
                "drawdownQuantiles": drawdown_quantiles,
                "utility": utility_quantiles[:, min(1, utility_quantiles.shape[1] - 1)],
                "fill": self.fill_head(critic).squeeze(1),
                "target": self.target_head(critic).squeeze(1),
                "outcome": self.outcome_head(critic),
                "duration": self.duration_head(critic).squeeze(1),
                "regression": self.regression_head(critic),
                "planQuality": quality,
            }

    return MarketToPlanPolicy()


def _quantile_huber_loss(torch, prediction, target, quantiles):
    error = target.unsqueeze(1) - prediction
    huber = torch.where(torch.abs(error) <= 1.0, 0.5 * error.square(), torch.abs(error) - 0.5)
    taus = torch.as_tensor(quantiles, dtype=prediction.dtype, device=prediction.device).view(1, -1)
    return torch.mean(torch.abs(taus - (error.detach() < 0).to(prediction.dtype)) * huber)


def _forecast_quantile_loss(torch, prediction, target):
    """Pinball supervision for the monotone q10/q50/q90 forecast head.

    The old smooth-L1-only target treated the three future interval values as
    three interchangeable regressions.  Quantile losses preserve their
    asymmetric meaning and therefore make the interval width usable as a
    genuine uncertainty signal for plan sizing and ranking.
    """

    shape = (prediction.shape[0], len(FORECAST_HORIZONS), len(FORECAST_QUANTILES))
    estimate = prediction.reshape(shape)
    observed = target.reshape(shape)
    levels = torch.as_tensor(FORECAST_QUANTILES, dtype=estimate.dtype, device=estimate.device).view(1, 1, -1)
    error = observed - estimate
    return torch.mean(torch.maximum(levels * error, (levels - 1.0) * error))


def _groupwise_action_ranking_loss(torch, predicted_utility, utility, groups):
    """Rank plans that shared one causal state instead of only regressing Q.

    The replay corpus deliberately has WAIT/LONG/SHORT and multiple plan
    geometries for a state.  A pairwise logistic objective asks the critic to
    put the better replayed plan above the worse one, which is much closer to
    the market-scan use case than an independent absolute-error loss.
    """

    if groups is None or predicted_utility.numel() < 2:
        return predicted_utility.new_zeros(())
    losses = []
    for group in torch.unique(groups):
        indexes = torch.nonzero(groups == group, as_tuple=False).reshape(-1)
        if indexes.numel() < 2:
            continue
        value = predicted_utility.index_select(0, indexes)
        target = utility.index_select(0, indexes)
        target_delta = target[:, None] - target[None, :]
        prediction_delta = value[:, None] - value[None, :]
        mask = target_delta > 0.05
        if torch.any(mask):
            weights = torch.clamp(target_delta[mask], min=0.05, max=4.0)
            losses.append(torch.nn.functional.softplus(-prediction_delta[mask]).mul(weights).mean())
    return torch.stack(losses).mean() if losses else predicted_utility.new_zeros(())


def _conservative_action_score(q, dd, duration, fill, config, target=None, quality=None):
    """Score one state/action prediction against the WAIT baseline.

    The lower return quantile and upper drawdown quantile make the selector
    conservative.  Quantile spread is an inexpensive uncertainty proxy for
    the single-critic runtime and is penalized so extrapolated actions do not
    win solely because their median prediction is optimistic.
    """
    q_values = np.asarray(q, dtype=np.float64).reshape(-1)
    dd_values = np.asarray(dd, dtype=np.float64).reshape(-1)
    if not len(q_values):
        return float("-inf")
    lower_return = float(q_values[0])
    upper_drawdown = float(dd_values[-1]) if len(dd_values) else 0.0
    uncertainty = max(0.0, float(q_values[-1] - q_values[0])) if len(q_values) > 1 else 0.0
    duration_value = max(0.0, float(duration))
    fill_value = float(np.clip(fill, 0.0, 1.0))
    target_value = float(np.clip(0.0 if target is None else target, 0.0, 1.0))
    target_weight = float(config.get("targetProbabilityWeight", 0.12))
    score = (
        lower_return
        - float(config.get("riskAversion", 0.35)) * upper_drawdown
        - float(config.get("timeCostWeight", 0.004)) * np.log1p(duration_value)
        - float(config.get("uncertaintyPenalty", 0.03)) * uncertainty
        + 0.05 * fill_value
        + target_weight * target_value
    )
    if quality is not None:
        try:
            quality_values = np.asarray(quality, dtype=np.float64).reshape(-1)
            if len(quality_values) >= len(PLAN_QUALITY_OUTPUT_NAMES) and np.all(np.isfinite(quality_values[:5])):
                expected_r, profit_probability, drawdown_risk, time_efficiency, confidence = quality_values[:5]
                # Keep the learned quality vector as a bounded ranking term;
                # the distributional critic and explicit risk penalty remain
                # the primary safety signals.
                quality_score = (
                    0.38 * float(np.clip(expected_r, -1.0, 1.0))
                    + 0.24 * (2.0 * float(np.clip(profit_probability, 0.0, 1.0)) - 1.0)
                    + 0.18 * float(np.clip(time_efficiency, -1.0, 1.0))
                    + 0.20 * (2.0 * float(np.clip(confidence, 0.0, 1.0)) - 1.0)
                )
                quality_risk = max(0.0, float(drawdown_risk))
                score += float(config.get("qualityScoreWeight", 0.18)) * quality_score
                score -= float(config.get("qualityRiskWeight", 0.08)) * min(quality_risk / 4.0, 2.0)
        except (TypeError, ValueError, OverflowError):
            pass
    return score


def _profitable_realized_r(value):
    """Use actual positive realised R as the user-facing win definition."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(math.isfinite(number) and number > 0.0)


def _one_action_per_state(candidate_rows):
    """Retain the highest-score non-WAIT action for every market state."""
    selected = {}
    for row in candidate_rows:
        if int(row.get("direction", DIRECTION_WAIT)) == DIRECTION_WAIT:
            continue
        try:
            group = int(row.get("group", -1))
            score = float(row.get("score", float("-inf")))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        previous = selected.get(group)
        if previous is None or score > float(previous.get("score", float("-inf"))):
            selected[group] = row
    return [selected[group] for group in sorted(selected)]


def _forecast_adjusted_action_scores(scores, directions, forecast, forecast_direction, config):
    """Apply identical forecast ranking terms during evaluation and serving."""
    adjusted = np.asarray(scores, dtype=np.float64).reshape(-1).copy()
    direction_array = np.asarray(directions, dtype=np.int64).reshape(-1)
    forecast_array = np.asarray(forecast, dtype=np.float64)
    direction_probabilities = np.asarray(forecast_direction, dtype=np.float64)
    if not len(adjusted):
        return adjusted
    if forecast_array.ndim == 1:
        forecast_array = np.repeat(forecast_array[None, :], len(adjusted), axis=0)
    if direction_probabilities.ndim == 1:
        direction_probabilities = np.repeat(direction_probabilities[None, :], len(adjusted), axis=0)
    if len(forecast_array) != len(adjusted) or len(direction_probabilities) != len(adjusted):
        return adjusted
    median_index = 1 + len(FORECAST_QUANTILES) * (len(FORECAST_HORIZONS) - 1)
    low_index = len(FORECAST_QUANTILES) * (len(FORECAST_HORIZONS) - 1)
    high_index = low_index + len(FORECAST_QUANTILES) - 1
    median_return = forecast_array[:, median_index]
    spread = np.maximum(0.0, forecast_array[:, high_index] - forecast_array[:, low_index])
    active = (direction_array == DIRECTION_LONG) | (direction_array == DIRECTION_SHORT)
    directional_edge = np.zeros(len(adjusted), dtype=np.float64)
    directional_edge[direction_array == DIRECTION_LONG] = median_return[direction_array == DIRECTION_LONG]
    directional_edge[direction_array == DIRECTION_SHORT] = -median_return[direction_array == DIRECTION_SHORT]
    alignment = np.clip(directional_edge / 0.01, -3.0, 3.0)
    valid_direction = (direction_array >= 0) & (direction_array < direction_probabilities.shape[1])
    support = np.zeros(len(adjusted), dtype=np.float64)
    row_indexes = np.arange(len(adjusted))
    support[valid_direction] = direction_probabilities[row_indexes[valid_direction], direction_array[valid_direction]]
    wait_probability = direction_probabilities[:, DIRECTION_WAIT] if direction_probabilities.shape[1] > DIRECTION_WAIT else np.zeros(len(adjusted))
    wait_threshold = float(config.get("forecastWaitDominanceThreshold", 0.75))
    side_floor = float(config.get("forecastConflictMinSideSupport", 0.05))
    conflict = np.maximum(0.0, wait_probability - wait_threshold)
    conflict *= np.maximum(0.0, side_floor - support) / max(side_floor, 1e-6)
    adjusted -= active * float(config.get("forecastConflictPenalty", 0.18)) * conflict
    adjusted += active * (0.06 * alignment - 0.01 * np.minimum(spread / 0.01, 3.0))
    adjusted += active * float(config.get("forecastSupportWeight", 0.05)) * (support - (1.0 / len(DIRECTION_LABELS)))
    return adjusted


def _wilson_lower_bound(successes, trials, confidence=0.90):
    """One-sided-safe estimate used by the validation risk-coverage gate.

    A raw 100% win rate on one or two selected states is not evidence that the
    operating point is safe.  Wilson's interval gives the selector a finite
    sample correction without requiring scipy or a fitted calibration model.
    ``confidence`` is the central interval confidence; the returned lower
    endpoint is the conservative bound used as the minimum-win constraint.
    """
    try:
        trials = int(trials)
        successes = float(successes)
        confidence = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if trials <= 0:
        return 0.0
    confidence = float(np.clip(confidence, 0.50, 0.999))
    successes = float(np.clip(successes, 0.0, float(trials)))
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    observed = successes / float(trials)
    z2 = z * z
    denominator = 1.0 + z2 / float(trials)
    center = (observed + z2 / (2.0 * float(trials))) / denominator
    spread = z * math.sqrt(
        observed * (1.0 - observed) / float(trials)
        + z2 / (4.0 * float(trials) * float(trials))
    ) / denominator
    return float(np.clip(center - spread, 0.0, 1.0))


def _calibrate_risk_coverage(candidate_rows, config):
    """Calibrate the widest useful operating point on a risk/coverage curve.

    Selective-classification work treats abstention as a tunable operating
    point, not as a second hard classifier.  The previous implementation made
    the probability heads, Wilson bound and WAIT margin conjunctive, so a
    mildly miscalibrated auxiliary head could erase every trade.  We now use
    those heads as a *soft behavior-support filter* and only make the score
    margin plus the empirical outcome constraint decisive.  If the requested
    confidence bound is unattainable, a limited point is retained when its
    observed win rate still clears the requested bar (or misses it only by the
    configured tolerance) and its conservative lower bound remains above the
    safety floor.  The status is persisted and shown to the user.
    """
    target_win = float(np.clip(config.get("minimumWinRate", 0.55), 0.0, 1.0))
    minimum_trades = max(4, int(config.get("minimumCalibrationTrades", 16)))
    confidence = float(np.clip(config.get("riskCoverageConfidence", 0.80), 0.50, 0.999))
    safety_floor = float(np.clip(
        config.get("minimumSafetyLowerBound", max(0.40, target_win - 0.10)),
        0.0,
        target_win,
    ))
    fallback_tolerance = float(np.clip(config.get("fallbackWinRateTolerance", 0.03), 0.0, 0.25))
    minimum_coverage = float(np.clip(
        config.get("minimumSelectionCoverage", config.get("coverageTarget", 0.08)),
        0.0,
        1.0,
    ))
    # Absolute critic values drift under market/regime shift.  The calibrated
    # decision is therefore relative to WAIT; retain the field only as a
    # diagnostic compatibility value and do not use it to discard candidates.
    score_floor = float(config.get("selectionFloor", -0.12))
    base_margin = max(0.0, float(config.get("selectionMargin", 0.01)))

    base_rows = [
        row for row in candidate_rows
        if int(row.get("direction", DIRECTION_WAIT)) != DIRECTION_WAIT
        and math.isfinite(float(row.get("score", float("-inf"))))
        and float(row.get("score", float("-inf"))) > float("-inf")
    ]
    # The fill/target heads are useful score features, but not independent
    # abstention classifiers.  Using them as hard gates caused distribution
    # shift in live quotes to turn every otherwise-qualified action into WAIT.
    # Calibrate on the complete non-WAIT frontier and persist zero calibrated
    # probability floors so serving follows this same rule.
    trade_rows = base_rows
    state_total = max(1, len({int(row.get("group", -1)) for row in candidate_rows}))
    probability_mode = "SOFT_SCORE"
    effective_fill_floor = 0.0
    effective_target_floor = 0.0

    # Calibrate over the complete margin curve, including actions that are
    # slightly below the noisy per-state WAIT estimate.  Selective prediction
    # treats abstention as a coverage operating point; clamping this set to
    # non-negative margins silently turned WAIT into a second hard classifier
    # and erased the useful part of the action frontier.
    thresholds = sorted({base_margin, 0.0, *(
        float(row.get("margin", 0.0)) for row in trade_rows
    )})

    def evaluate(threshold):
        selected = [row for row in trade_rows if float(row.get("margin", 0.0)) >= threshold]
        count = len(selected)
        if count < minimum_trades:
            return None
        wins = sum(bool(row.get("win")) for row in selected)
        raw_win = wins / max(1, count)
        lower = _wilson_lower_bound(wins, count, confidence)
        coverage = len({int(row.get("group", -1)) for row in selected}) / state_total
        utility = float(np.mean([float(row.get("utility", 0.0)) for row in selected])) if selected else 0.0
        drawdown = float(np.mean([float(row.get("drawdown", 0.0)) for row in selected])) if selected else 0.0
        return {
            "threshold": float(threshold),
            "count": int(count),
            "wins": int(wins),
            "rawWin": float(raw_win),
            "lower": float(lower),
            "coverage": float(coverage),
            "utility": float(utility),
            "drawdown": float(drawdown),
        }

    points = [point for point in (evaluate(threshold) for threshold in thresholds) if point]
    # A statistically safe point with 1-3% coverage is not a usable model;
    # it is an exceptional-state lookup table.  Require the same minimum
    # coverage during calibration that production checkpoint selection uses.
    safe_points = [point for point in points if point["coverage"] + 1e-12 >= minimum_coverage and point["lower"] + 1e-12 >= target_win]
    limited_points = [
        point for point in points
        if point["coverage"] + 1e-12 >= minimum_coverage
        and point["lower"] + 1e-12 >= safety_floor
        and point["rawWin"] + 1e-12 >= target_win
    ]
    degraded_points = [
        point for point in points
        if point["coverage"] + 1e-12 >= minimum_coverage
        and point["lower"] + 1e-12 >= safety_floor
        and point["rawWin"] + 1e-12 >= target_win - fallback_tolerance
    ]
    frontier_win = float(np.clip(config.get("frontierMinimumWinRate", max(0.50, target_win - 0.05)), 0.0, 1.0))
    frontier_floor = max(0.0, safety_floor - float(np.clip(config.get("frontierSafetyRelaxation", 0.08), 0.0, 0.50)))
    frontier_points = [
        point for point in points
        if point["coverage"] + 1e-12 >= minimum_coverage
        if point["lower"] + 1e-12 >= frontier_floor
        and point["rawWin"] + 1e-12 >= frontier_win
    ]

    if safe_points:
        # Coverage is the primary objective once the confidence constraint is
        # met; utility and lower-bound are tie-breakers.
        chosen = max(safe_points, key=lambda point: (point["count"], point["lower"], point["utility"], -point["drawdown"], -point["threshold"]))
        status = "SAFE_OPERATING_POINT"
    elif limited_points:
        chosen = max(limited_points, key=lambda point: (point["count"], point["lower"], point["utility"], -point["drawdown"], -point["threshold"]))
        status = "LIMITED_SAFE_OPERATING_POINT"
    elif degraded_points:
        chosen = max(degraded_points, key=lambda point: (point["count"], point["lower"], point["rawWin"], point["utility"], -point["drawdown"], -point["threshold"]))
        status = "DEGRADED_OPERATING_POINT"
    elif frontier_points:
        # A small holdout can make the Wilson lower bound unattainable even
        # when the empirical frontier still has a useful edge.  Prefer the
        # widest point that keeps a positive empirical edge and record the
        # relaxed status so the UI/backtest can distinguish it from SAFE.
        chosen = max(frontier_points, key=lambda point: (point["count"], point["rawWin"], point["utility"], -point["drawdown"], -point["threshold"]))
        status = "COVERAGE_FRONTIER_OPERATING_POINT"
    else:
        chosen = None
        status = "NO_SAFE_OPERATING_POINT"

    if chosen is None:
        # Keep the research checkpoint and its diagnostics, but do not invent
        # a serving threshold that validation could not support.
        threshold = base_margin
        return {
            "selectionRule": "RISK_COVERAGE",
            "calibratedSelectionMargin": float(threshold),
            "calibratedSelectionFloor": float(score_floor),
            "calibratedMinimumFillProbability": float(effective_fill_floor),
            "calibratedMinimumTargetProbability": float(effective_target_floor),
            "minimumWinRate": target_win,
            "minimumCalibrationTrades": minimum_trades,
            "riskCoverageConfidence": confidence,
            "minimumSafetyLowerBound": safety_floor,
            "minimumSelectionCoverage": minimum_coverage,
            "fallbackWinRateTolerance": fallback_tolerance,
            "selectionCalibrationStatus": status,
            "selectionCalibrationProbabilityMode": probability_mode,
            "selectionCalibrationTrades": 0,
            "selectionCalibrationWinRate": 0.0,
            "selectionCalibrationEmpiricalWinRate": 0.0,
            "selectionCalibrationLowerBound": 0.0,
            "selectionCalibrationCoverage": 0.0,
        }

    return {
        "selectionRule": "RISK_COVERAGE",
        "calibratedSelectionMargin": float(chosen["threshold"]),
        "calibratedSelectionFloor": float(score_floor),
        "calibratedMinimumFillProbability": float(effective_fill_floor),
        "calibratedMinimumTargetProbability": float(effective_target_floor),
        "minimumWinRate": target_win,
        "minimumCalibrationTrades": minimum_trades,
        "riskCoverageConfidence": confidence,
        "minimumSafetyLowerBound": safety_floor,
        "minimumSelectionCoverage": minimum_coverage,
        "fallbackWinRateTolerance": fallback_tolerance,
        "selectionCalibrationStatus": status,
        "selectionCalibrationProbabilityMode": probability_mode,
        "selectionCalibrationTrades": int(chosen["count"]),
        "selectionCalibrationWinRate": round(float(chosen["rawWin"]), 6),
        "selectionCalibrationEmpiricalWinRate": round(float(chosen["rawWin"]), 6),
        "selectionCalibrationLowerBound": round(float(chosen["lower"]), 6),
        "selectionCalibrationCoverage": round(float(chosen["coverage"]), 6),
        "selectionCalibrationUtility": round(float(chosen["utility"]), 6),
        "selectionCalibrationDrawdown": round(float(chosen["drawdown"]), 6),
    }


def _selection_accepts_action(score, wait_score, fill, target, config):
    """Apply the same risk-coverage gate in training, test and serving."""
    score = float(score)
    if not math.isfinite(score):
        return False
    rule = str(config.get("selectionRule", "RISK_COVERAGE") or "RISK_COVERAGE").upper()
    margin = float(config.get(
        "calibratedSelectionMargin" if rule == "RISK_COVERAGE" else "selectionMargin",
        config.get("selectionMargin", 0.05),
    ))
    calibration_status = str(config.get("selectionCalibrationStatus") or "").upper()
    if rule == "RISK_COVERAGE" and calibration_status == "NO_SAFE_OPERATING_POINT":
        return False
    score_floor = float(config.get(
        "calibratedSelectionFloor" if rule == "RISK_COVERAGE" else "selectionFloor",
        config.get("selectionFloor", -0.08),
    ))
    baseline_pass = score > float(wait_score) + margin
    if rule == "RISK_COVERAGE":
        return bool(baseline_pass)
    fill_floor = float(config.get("calibratedMinimumFillProbability", config.get("minimumFillProbability", 0.08)))
    target_floor = float(config.get("calibratedMinimumTargetProbability", config.get("minimumTargetProbability", 0.08)))
    calibrated_pass = score >= score_floor and float(fill) >= fill_floor and float(target) >= target_floor
    return bool(baseline_pass or calibrated_pass)


def _coverage_fallback_accepts_action(score, wait_score, q, dd, fill, target, direction_probability, config, *, forecast_support=None, forecast_wait_probability=None):
    """Bounded coverage fallback for a pessimistic offline critic.

    CQL/IQL-style critics intentionally assign low values to actions that are
    weakly represented in the replay set.  Treating that estimate as an
    unconditional abstention rule makes live coverage collapse even when the
    direction and outcome heads agree.  This fallback does not require the
    candidate to beat WAIT; it only keeps an absolute, configurable score
    floor (with the legacy gap as a broad anti-outlier bound), plus minimum
    directional/target support and explicit lower-tail/drawdown limits.  It is
    therefore a selective operating point, not a hard-coded trade signal.
    """
    if not bool(config.get("coverageFallbackEnabled", True)):
        return False
    try:
        score = float(score); wait_score = float(wait_score)
        q_values = np.asarray(q, dtype=np.float64).reshape(-1)
        dd_values = np.asarray(dd, dtype=np.float64).reshape(-1)
        direction_probability = float(direction_probability)
        target = float(target); fill = float(fill)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(score) and math.isfinite(wait_score) and len(q_values)):
        return False
    if not (math.isfinite(direction_probability) and direction_probability >= float(config.get("coverageFallbackMinDirectionProbability", 0.45))):
        return False
    if not (math.isfinite(target) and target >= float(config.get("coverageFallbackMinTargetProbability", 0.02))):
        return False
    if not (math.isfinite(fill) and fill > 0.0):
        return False
    if forecast_support is not None and forecast_wait_probability is not None:
        try:
            support = float(forecast_support)
            wait_probability = float(forecast_wait_probability)
            wait_threshold = float(config.get("forecastWaitDominanceThreshold", 0.75))
            side_floor = float(config.get("forecastConflictMinSideSupport", 0.05))
            # This is deliberately narrow: ordinary forecast disagreement is
            # left to the soft score.  Only a near-certain WAIT forecast plus
            # negligible side support blocks the coverage fallback.
            if wait_probability >= wait_threshold and support < side_floor:
                return False
        except (TypeError, ValueError):
            pass
    lower_return = float(q_values[0])
    uncertainty = max(0.0, float(q_values[-1] - q_values[0])) if len(q_values) > 1 else 0.0
    upper_drawdown = float(dd_values[-1]) if len(dd_values) else 0.0
    # Do not require the candidate to beat WAIT.  Keep the old gap only as a
    # deliberately broad absolute anti-outlier bound, and combine it with the
    # configured floor so this path can never become stricter than either
    # explicit coverage control.
    configured_floor = float(config.get(
        "calibratedSelectionFloor",
        config.get("selectionFloor", -0.12),
    ))
    max_gap = max(0.0, float(config.get("coverageFallbackMaxScoreGap", 1.50)))
    score_floor = min(configured_floor, float(wait_score) - max_gap)
    max_uncertainty = max(0.0, float(config.get("coverageFallbackMaxUncertainty", 4.0)))
    max_drawdown = max(0.0, float(config.get("coverageFallbackMaxDrawdown", 2.50)))
    min_lower_return = float(config.get("coverageFallbackMinLowerReturn", -1.25))
    return bool(
        score >= score_floor
        and uncertainty <= max_uncertainty
        and upper_drawdown <= max_drawdown
        and lower_return >= min_lower_return
    )


def _direct_policy_loss(torch, outputs, actions, directions, utility, realized_r, duration_bars, regression, outcomes, entered, forecast_returns, forecast_direction, config, *, groups=None):
    quantiles = tuple(config.get("criticQuantiles") or (0.10, 0.50, 0.90))
    utility_target = torch.clamp(utility, -8.0, 8.0)
    drawdown_target = torch.clamp(regression[:, 2], 0.0, 8.0)
    duration_target = torch.log1p(torch.clamp(duration_bars, min=0.0))
    critic_utility = _quantile_huber_loss(torch, outputs["utilityQuantiles"], utility_target, quantiles)
    critic_drawdown = _quantile_huber_loss(torch, outputs["drawdownQuantiles"], drawdown_target, (0.50, 0.90))
    duration_loss = torch.nn.functional.smooth_l1_loss(outputs["duration"], duration_target, beta=0.25)
    fill_pos_weight = torch.as_tensor(
        float(config.get("fillPositiveWeight", 1.25)),
        dtype=outputs["fill"].dtype,
        device=outputs["fill"].device,
    )
    fill_loss = torch.nn.functional.binary_cross_entropy_with_logits(
        outputs["fill"], entered, pos_weight=fill_pos_weight,
    )
    target_labels = (outcomes == OUTCOME_TARGET).to(outputs["target"].dtype)
    target_pos_weight = torch.as_tensor(
        float(config.get("targetPositiveWeight", 1.75)),
        dtype=outputs["target"].dtype,
        device=outputs["target"].device,
    )
    target_loss = torch.nn.functional.binary_cross_entropy_with_logits(
        outputs["target"], target_labels, pos_weight=target_pos_weight,
    )
    outcome_loss = torch.nn.functional.cross_entropy(outputs["outcome"], outcomes)
    regression_loss = torch.nn.functional.smooth_l1_loss(outputs["regression"], regression, beta=0.35)
    forecast_target = torch.clamp(forecast_returns, -2.0, 2.0)
    forecast_loss = (
        0.5 * torch.nn.functional.smooth_l1_loss(outputs["forecast"], forecast_target, beta=0.15)
        + 0.5 * _forecast_quantile_loss(torch, outputs["forecast"], forecast_target)
    )
    forecast_direction_loss = torch.nn.functional.cross_entropy(outputs["forecastDirection"], forecast_direction)
    # A separate quality vector turns the downstream decision objective into
    # explicit supervised signals.  It is intentionally derived from the
    # same causal replay result as the critic, not from a hand-written plan
    # teacher: expected R and time efficiency are continuous, profit
    # probability is the positive realised-R event, drawdown is the adverse
    # excursion, and confidence reflects both execution and drawdown quality.
    # Reaching the final target remains a separate target-probability task.
    quality_target = torch.stack((
        torch.tanh(realized_r / 2.0),
        (realized_r > 0.0).to(utility_target.dtype),
        torch.clamp(drawdown_target, 0.0, 8.0),
        torch.tanh(
            realized_r / (
                1.0
                + 0.12 * torch.clamp(
                    duration_bars / max(float(config.get("labelHorizonBars", 192)), 1.0),
                    min=0.0,
                    max=4.0,
                )
            )
        ),
        torch.clamp(
            entered * (1.0 - drawdown_target / 4.0),
            0.0,
            1.0,
        ),
    ), dim=1)
    quality_loss = torch.nn.functional.smooth_l1_loss(
        outputs["planQuality"], quality_target, beta=0.25,
    )
    # WAIT is the zero-utility baseline.  Non-WAIT actions are weighted by
    # their realized advantage over that baseline; negative actions remain in
    # the critic corpus but contribute little to the proposal policy.  The
    # action head is deliberately trained only on trade actions so averaging
    # them with the zero WAIT vector cannot create an untested geometry.
    temperature = max(float(config.get("actorAdvantageTemperature", 0.35)), 0.05)
    advantage_weight = torch.exp(torch.clamp(utility_target / temperature, min=-5.0, max=2.0))
    trade_mask = (directions != DIRECTION_WAIT).to(utility_target.dtype)
    policy_weights = torch.where(trade_mask > 0, advantage_weight, torch.ones_like(advantage_weight))
    direction_raw = torch.nn.functional.cross_entropy(outputs["directDirection"], directions, reduction="none")
    direction_loss = torch.sum(direction_raw * policy_weights) / torch.clamp(torch.sum(policy_weights), min=1.0)
    action_names = DIRECT_PLAN_OUTPUT_NAMES[1:]
    ratio_indexes = {
        action_names.index("targetOneRatio"),
        action_names.index("targetTwoRatio"),
    }
    non_ratio_indexes = [index for index in range(len(action_names)) if index not in ratio_indexes]
    action_raw = torch.nn.functional.smooth_l1_loss(
        outputs["directPlan"][:, non_ratio_indexes],
        actions[:, non_ratio_indexes],
        beta=0.35,
        reduction="none",
    ).mean(dim=1)
    action_loss = torch.sum(action_raw * advantage_weight * trade_mask) / torch.clamp(torch.sum(advantage_weight * trade_mask), min=1.0)
    ratio_one = actions[:, action_names.index("targetOneRatio")].clamp(min=0.0)
    ratio_two = actions[:, action_names.index("targetTwoRatio")].clamp(min=0.0)
    runner = torch.clamp(100.0 - ratio_one - ratio_two, min=0.0)
    allocation_target = torch.stack((ratio_one, ratio_two, runner), dim=1)
    allocation_target = allocation_target / torch.clamp(allocation_target.sum(dim=1, keepdim=True), min=1e-6)
    allocation_raw = -torch.sum(
        allocation_target * torch.nn.functional.log_softmax(outputs["allocationLogits"], dim=1),
        dim=1,
    )
    # Allocation is undefined for WAIT.  Including WAIT rows here would teach
    # the state-only allocation head to emit a 100% runner whenever a market
    # is uncertain, which is exactly the collapse this head is meant to avoid.
    allocation_weights = policy_weights * trade_mask
    allocation_loss = torch.sum(allocation_raw * allocation_weights) / torch.clamp(torch.sum(allocation_weights), min=1.0)
    ranking_loss = _groupwise_action_ranking_loss(
        torch,
        torch.tanh(outputs["utility"]),
        torch.tanh(utility_target / 2.0),
        groups,
    )
    total = (
        0.28 * critic_utility + 0.16 * critic_drawdown + 0.10 * duration_loss
        + 0.10 * fill_loss + 0.10 * target_loss + 0.10 * outcome_loss
        + 0.06 * regression_loss + 0.06 * direction_loss + 0.02 * action_loss
        + float(config.get("forecastLossWeight", 0.18)) * forecast_loss
        + float(config.get("forecastDirectionLossWeight", 0.06)) * forecast_direction_loss
        + 0.06 * allocation_loss
        + float(config.get("qualityLossWeight", 0.10)) * quality_loss
        + float(config.get("rankingLossWeight", 0.10)) * ranking_loss
    )
    return total, {
        "total": total,
        "criticUtility": critic_utility,
        "criticDrawdown": critic_drawdown,
        "duration": duration_loss,
        "fill": fill_loss,
        "target": target_loss,
        "outcome": outcome_loss,
        "regression": regression_loss,
        "direction": direction_loss,
        "action": action_loss,
        "allocation": allocation_loss,
        "forecast": forecast_loss,
        "forecastDirection": forecast_direction_loss,
        "quality": quality_loss,
        "ranking": ranking_loss,
    }


def _evaluate_direct_policy(torch, model, loader, device, config, *, include_values=False, fit_calibration=False, selection_calibration=None):
    model.eval()
    losses = []
    arrays = {key: [] for key in ("q", "dd", "durationPrediction", "fill", "target", "quality", "allocation", "forecast", "forecastDirection", "forecastReturns", "forecastDirections", "utility", "realizedR", "duration", "drawdown", "directions", "outcomes", "groups", "actions")}
    with torch.no_grad():
        for batch in loader:
            inputs, actions, directions, utility, realized_r, duration, regression, forecast_returns, forecast_direction, outcomes, entered, groups = batch
            inputs = inputs.to(device, non_blocking=device.type == "cuda")
            actions = actions.to(device, non_blocking=device.type == "cuda")
            directions = directions.to(device, non_blocking=device.type == "cuda")
            utility = utility.to(device, non_blocking=device.type == "cuda")
            realized_r = realized_r.to(device, non_blocking=device.type == "cuda")
            duration = duration.to(device, non_blocking=device.type == "cuda")
            regression = regression.to(device, non_blocking=device.type == "cuda")
            outcomes = outcomes.to(device, non_blocking=device.type == "cuda")
            entered = entered.to(device, non_blocking=device.type == "cuda")
            forecast_returns = forecast_returns.to(device, non_blocking=device.type == "cuda")
            forecast_direction = forecast_direction.to(device, non_blocking=device.type == "cuda")
            groups = groups.to(device, non_blocking=device.type == "cuda")
            outputs = model(inputs, actions, directions)
            loss, _parts = _direct_policy_loss(
                torch, outputs, actions, directions, utility, realized_r, duration,
                regression, outcomes, entered, forecast_returns, forecast_direction,
                config, groups=groups,
            )
            losses.append(float(loss.detach().item()) * int(inputs.shape[0]))
            arrays["q"].append(outputs["utilityQuantiles"].detach().float().cpu().numpy())
            arrays["dd"].append(outputs["drawdownQuantiles"].detach().float().cpu().numpy())
            arrays["durationPrediction"].append(torch.expm1(torch.clamp(outputs["duration"], min=0.0)).detach().float().cpu().numpy())
            arrays["fill"].append(torch.sigmoid(outputs["fill"]).detach().float().cpu().numpy())
            arrays["target"].append(torch.sigmoid(outputs["target"]).detach().float().cpu().numpy())
            arrays["quality"].append(outputs["planQuality"].detach().float().cpu().numpy())
            arrays["allocation"].append(outputs["allocation"].detach().float().cpu().numpy() / 100.0)
            arrays["forecast"].append(outputs["forecast"].detach().float().cpu().numpy())
            arrays["forecastDirection"].append(torch.softmax(outputs["forecastDirection"], dim=1).detach().float().cpu().numpy())
            arrays["forecastReturns"].append(forecast_returns.detach().float().cpu().numpy())
            arrays["forecastDirections"].append(forecast_direction.detach().cpu().numpy())
            arrays["utility"].append(utility.detach().cpu().numpy())
            arrays["realizedR"].append(realized_r.detach().cpu().numpy())
            arrays["duration"].append(duration.detach().cpu().numpy())
            arrays["drawdown"].append(regression[:, 2].detach().cpu().numpy())
            arrays["directions"].append(directions.detach().cpu().numpy())
            arrays["outcomes"].append(outcomes.detach().cpu().numpy())
            arrays["groups"].append(groups.detach().cpu().numpy())
            arrays["actions"].append(actions.detach().cpu().numpy())
    values = {key: np.concatenate(parts) if parts else np.empty((0,), dtype=np.float32) for key, parts in arrays.items()}
    q = values["q"]
    dd = values["dd"]
    groups = values["groups"]
    selected = []
    selected_scores = []
    selected_groups = set()
    wait_count = 0
    candidate_rows = []
    if len(groups):
        for group in np.unique(groups):
            indexes = np.flatnonzero(groups == group)
            scores = np.asarray([
                _conservative_action_score(
                    q[index],
                    dd[index],
                    values["durationPrediction"][index],
                    values["fill"][index],
                    config,
                    target=values["target"][index],
                    quality=values["quality"][index],
                )
                for index in indexes
            ], dtype=np.float64)
            scores = _forecast_adjusted_action_scores(
                scores,
                values["directions"][indexes],
                values["forecast"][indexes],
                values["forecastDirection"][indexes],
                config,
            )
            wait_mask = values["directions"][indexes] == DIRECTION_WAIT
            wait_score = float(np.max(scores[wait_mask])) if np.any(wait_mask) else 0.0
            for local in np.argsort(scores)[::-1]:
                candidate = int(indexes[int(local)])
                if not math.isfinite(float(scores[int(local)])):
                    continue
                candidate_rows.append({
                    "group": int(group),
                    "candidate": candidate,
                    "direction": int(values["directions"][candidate]),
                    "score": float(scores[int(local)]),
                    "waitScore": float(wait_score),
                    "margin": float(scores[int(local)] - wait_score),
                    "fill": float(values["fill"][candidate]),
                    "target": float(values["target"][candidate]),
                    "utility": float(values["utility"][candidate]),
                    "drawdown": float(values["drawdown"][candidate]),
                    "duration": float(values["duration"][candidate]),
                    "realizedR": float(values["realizedR"][candidate]),
                    "win": _profitable_realized_r(values["realizedR"][candidate]),
                    "quality": values["quality"][candidate].tolist(),
                })
    candidate_rows = _one_action_per_state(candidate_rows)
    effective_config = dict(config)
    calibration = dict(selection_calibration or {})
    if fit_calibration:
        calibration = _calibrate_risk_coverage(candidate_rows, config)
    if calibration:
        effective_config.update(calibration)
    for row in candidate_rows:
        if row["direction"] == DIRECTION_WAIT or not _selection_accepts_action(
            row["score"], row["waitScore"], row["fill"], row["target"], effective_config,
        ):
                wait_count += 1
        else:
            selected.append(int(row["candidate"]))
            selected_scores.append(float(row["score"]))
            selected_groups.add(int(row["group"]))
    selected = np.asarray(selected, dtype=np.int64)
    state_count = len(np.unique(groups)) if len(groups) else 0
    utility_mae = float(np.mean(np.abs(q[:, min(1, q.shape[1] - 1)] - values["utility"]))) if len(q) else None
    ratio_names = DIRECT_PLAN_OUTPUT_NAMES[1:]
    ratio_one_index = ratio_names.index("targetOneRatio")
    ratio_two_index = ratio_names.index("targetTwoRatio")
    stop_index = ratio_names.index("stopDistanceAtr")
    target_one_index = ratio_names.index("targetOneDistanceAtr")
    target_two_index = ratio_names.index("targetTwoDistanceAtr")
    allocation_target = np.stack((
        np.maximum(values["actions"][:, ratio_one_index], 0.0),
        np.maximum(values["actions"][:, ratio_two_index], 0.0),
        np.maximum(100.0 - values["actions"][:, ratio_one_index] - values["actions"][:, ratio_two_index], 0.0),
    ), axis=1) if len(values["actions"]) else np.empty((0, 3), dtype=np.float32)
    if len(allocation_target):
        allocation_target /= np.maximum(allocation_target.sum(axis=1, keepdims=True), 1e-6)
    allocation_mae = float(np.mean(np.abs(values["allocation"] - allocation_target))) if len(allocation_target) else None
    forecast_mae = float(np.mean(np.abs(values["forecast"] - values["forecastReturns"]))) if len(values["forecast"]) else None
    forecast_direction_accuracy = float(np.mean(np.argmax(values["forecastDirection"], axis=1) == values["forecastDirections"])) if len(values["forecastDirection"]) else None
    selected_allocation = None
    if len(selected):
        selected_allocation = np.stack((
            np.maximum(values["actions"][selected, ratio_one_index], 0.0),
            np.maximum(values["actions"][selected, ratio_two_index], 0.0),
            np.maximum(100.0 - values["actions"][selected, ratio_one_index] - values["actions"][selected, ratio_two_index], 0.0),
        ), axis=1)
        selected_allocation /= np.maximum(selected_allocation.sum(axis=1, keepdims=True), 1e-6)
    report = {
        "loss": round(float(sum(losses) / max(1, len(values["utility"]))), 6),
        "criticUtilityMae": round(utility_mae, 5) if utility_mae is not None else None,
        "utilityCalibrationMae": round(utility_mae, 5) if utility_mae is not None else None,
        "predictedMeanUtility": round(float(np.mean(q[:, min(1, q.shape[1] - 1)])), 5) if len(q) else None,
        "meanReplayUtility": round(float(np.mean(values["utility"])), 5) if len(values["utility"]) else None,
        "selectedRealizedUtility": round(float(np.mean(values["utility"][selected])), 5) if len(selected) else 0.0,
        "selectedRealizedR": round(float(np.mean(values["realizedR"][selected])), 5) if len(selected) else 0.0,
        "selectedMeanDrawdown": round(float(np.mean(values["drawdown"][selected])), 5) if len(selected) else 0.0,
        "selectedMeanDurationBars": round(float(np.mean(values["duration"][selected])), 3) if len(selected) else 0.0,
        "selectedWinRate": round(float(np.mean(values["realizedR"][selected] > 0.0)), 4) if len(selected) else 0.0,
        "selectedMeanStopDistanceAtr": round(float(np.mean(values["actions"][selected, stop_index])), 4) if len(selected) else 0.0,
        "selectedMeanFirstTargetR": round(float(np.mean(values["actions"][selected, target_one_index] / np.maximum(values["actions"][selected, stop_index], 1e-6))), 4) if len(selected) else 0.0,
        "selectedMeanSecondTargetR": round(float(np.mean(values["actions"][selected, target_two_index] / np.maximum(values["actions"][selected, stop_index], 1e-6))), 4) if len(selected) else 0.0,
        "selectedCoverage": round(float(len(selected_groups) / max(1, state_count)), 4),
        "waitRate": round(float(max(0, state_count - len(selected_groups)) / max(1, state_count)), 4),
        "meanDurationBars": round(float(np.mean(values["duration"])), 3) if len(values["duration"]) else None,
        "predictedDurationBars": round(float(np.mean(values["durationPrediction"])), 3) if len(values["durationPrediction"]) else None,
        "fillCalibrationMae": round(float(np.mean(np.abs(values["fill"] - (values["duration"] > 0).astype(np.float32)))), 5) if len(values["fill"]) else None,
        "targetCalibrationMae": round(float(np.mean(np.abs(values["target"] - (values["outcomes"] == OUTCOME_TARGET).astype(np.float32)))), 5) if len(values["target"]) else None,
        "qualityMean": [round(float(item), 5) for item in np.mean(values["quality"], axis=0)] if len(values["quality"]) else None,
        "qualityExpectedRMae": round(float(np.mean(np.abs(values["quality"][:, 0] - np.tanh(values["realizedR"] / 2.0)))), 5) if len(values["quality"]) else None,
        "qualityProfitProbabilityMae": round(float(np.mean(np.abs(values["quality"][:, 1] - (values["realizedR"] > 0.0).astype(np.float32)))), 5) if len(values["quality"]) else None,
        "qualityTimeEfficiencyMae": round(float(np.mean(np.abs(values["quality"][:, 3] - np.tanh(values["realizedR"] / np.sqrt(np.maximum(values["duration"], 0.0) + 1.0))))), 5) if len(values["quality"]) else None,
        "allocationMae": round(allocation_mae, 5) if allocation_mae is not None else None,
        "forecastMae": round(forecast_mae, 5) if forecast_mae is not None else None,
        "forecastDirectionAccuracy": round(forecast_direction_accuracy, 4) if forecast_direction_accuracy is not None else None,
        "predictedAllocationMean": [round(float(item), 4) for item in np.mean(values["allocation"], axis=0)] if len(values["allocation"]) else None,
        "replayAllocationMean": [round(float(item), 4) for item in np.mean(allocation_target, axis=0)] if len(allocation_target) else None,
        "selectedAllocationMean": [round(float(item * 100.0), 3) for item in np.mean(selected_allocation, axis=0)] if selected_allocation is not None else None,
        "actionDiversity": round(float(len({tuple(np.round(row, 3)) for row in values["actions"]}) / max(1, len(values["actions"]))), 5) if len(values["actions"]) else 0.0,
        "actionCount": int(len(values["utility"])),
        "stateCount": int(state_count),
        "selectionCalibration": calibration or {
            "selectionRule": effective_config.get("selectionRule", "RISK_COVERAGE"),
            "calibratedSelectionMargin": effective_config.get("calibratedSelectionMargin", effective_config.get("selectionMargin", 0.05)),
            "calibratedSelectionFloor": effective_config.get("calibratedSelectionFloor", effective_config.get("selectionFloor", -0.08)),
            "calibratedMinimumFillProbability": effective_config.get("calibratedMinimumFillProbability", effective_config.get("minimumFillProbability", 0.08)),
            "calibratedMinimumTargetProbability": effective_config.get("calibratedMinimumTargetProbability", effective_config.get("minimumTargetProbability", 0.08)),
            "minimumWinRate": effective_config.get("minimumWinRate", 0.55),
            "minimumCalibrationTrades": effective_config.get("minimumCalibrationTrades", 16),
            "riskCoverageConfidence": effective_config.get("riskCoverageConfidence", 0.80),
            "minimumSafetyLowerBound": effective_config.get("minimumSafetyLowerBound", 0.45),
        },
    }
    if include_values:
        values["selected"] = selected
        values["selectedScores"] = np.asarray(selected_scores, dtype=np.float32)
        return report, values
    return report


def _direct_policy_checkpoint_safety(metrics, config=None):
    """Return the validated operating-point inputs, or ``None`` when unsafe."""

    config = config or {}
    coverage = float(metrics.get("selectedCoverage") or 0.0)
    target = float(config.get("coverageTarget", 0.08))
    minimum_coverage = float(config.get("minimumSelectionCoverage", 0.08))
    minimum_win = float(config.get("minimumWinRate", 0.55))
    win_rate = float(metrics.get("selectedWinRate") or 0.0)
    calibration = metrics.get("selectionCalibration") if isinstance(metrics.get("selectionCalibration"), dict) else {}
    lower_bound = float(calibration.get("selectionCalibrationLowerBound", win_rate) or 0.0)
    safety_floor = float(config.get("minimumSafetyLowerBound", max(0.40, minimum_win - 0.10)))
    status = str(calibration.get("selectionCalibrationStatus") or "").upper()
    # Synthetic/legacy metric rows may not carry calibration metadata.  Do
    # not treat their raw win rate as a validated operating point merely
    # because the relaxed safety floor happens to equal it.
    if not status and win_rate + 1e-12 < minimum_win:
        return None
    if status == "COVERAGE_FRONTIER_OPERATING_POINT":
        safety_floor = max(0.0, safety_floor - float(config.get("frontierSafetyRelaxation", 0.08)))
    # A limited/degraded point is still eligible for checkpoint selection when
    # its conservative lower bound clears the explicit safety floor.  This is
    # what prevents the model from selecting an epoch with 100% on one trade
    # while also preventing a completely abstaining checkpoint from winning.
    if (
        coverage + 1e-12 < minimum_coverage
        or coverage <= 0.0
        or lower_bound + 1e-12 < safety_floor
        or status == "NO_SAFE_OPERATING_POINT"
        or not all(math.isfinite(value) for value in (coverage, target, minimum_win, win_rate, lower_bound, safety_floor))
    ):
        return None
    return {
        "coverage": coverage,
        "target": target,
        "minimumCoverage": minimum_coverage,
        "minimumWinRate": minimum_win,
        "winRate": win_rate,
        "lowerBound": lower_bound,
        "safetyFloor": safety_floor,
    }


def _direct_policy_selection_score(metrics, config=None):
    """Score STABLE checkpoints by constrained risk-coverage, not one lucky trade.

    The validation operating point is calibrated first.  A checkpoint that
    cannot clear the requested lower confidence bound on win rate is therefore
    ineligible, even if its few selected trades have a large R.  Among eligible
    checkpoints, coverage is the primary objective and utility/drawdown are
    secondary tie-breakers.
    """
    config = config or {}
    safety = _direct_policy_checkpoint_safety(metrics, config)
    if safety is None:
        return float("-inf")
    coverage = safety["coverage"]
    target = safety["target"]
    weight = float(config.get("coverageWeight", 0.85))
    minimum_win = safety["minimumWinRate"]
    win_rate = safety["winRate"]
    lower_bound = safety["lowerBound"]
    coverage_reward = min(coverage, target) / max(target, 1e-6)
    confidence_gap = max(0.0, minimum_win - lower_bound)
    return (
        weight * coverage_reward
        + 0.30 * float(metrics.get("selectedRealizedUtility") or 0.0)
        - 0.08 * float(metrics.get("selectedMeanDrawdown") or 0.0)
        - 0.05 * float(metrics.get("utilityCalibrationMae") or 0.0)
        + 0.10 * max(0.0, win_rate - minimum_win)
        - 0.50 * confidence_gap
    )


def _direct_policy_expected_return_score(metrics, config=None):
    """Score BEST checkpoints by expected realized return after safety gates.

    ``selectedRealizedR`` is the validation-set mean realised R of the one
    calibrated action selected in each market state.  It therefore measures
    the expected return of the actual operating point, unlike raw candidate
    utility or an uncalibrated target-probability prediction.  Coverage,
    drawdown and calibration remain deliberately small secondary terms: they
    resolve near-ties but cannot make a lower-return checkpoint win.
    """

    config = config or {}
    safety = _direct_policy_checkpoint_safety(metrics, config)
    if safety is None:
        return float("-inf")
    expected_r = float(metrics.get("selectedRealizedR") or 0.0)
    utility = float(metrics.get("selectedRealizedUtility") or 0.0)
    drawdown = float(metrics.get("selectedMeanDrawdown") or 0.0)
    calibration_mae = float(metrics.get("utilityCalibrationMae") or 0.0)
    if not all(math.isfinite(value) for value in (expected_r, utility, drawdown, calibration_mae)):
        return float("-inf")
    coverage_reward = min(safety["coverage"], safety["target"]) / max(safety["target"], 1e-6)
    confidence_surplus = max(0.0, safety["lowerBound"] - safety["safetyFloor"])
    return (
        expected_r
        + 0.08 * utility
        - 0.04 * drawdown
        - 0.03 * calibration_mae
        + 0.03 * coverage_reward
        + 0.02 * confidence_surplus
    )


def _stable_checkpoint_epoch(history, epochs, config=None):
    """Choose the strongest checkpoint in the late training tail.

    ``STABLE`` is meant to represent a late, settled operating point rather
    than a duplicate of an early lucky ``BEST`` epoch.  Score a genuinely
    late tail and smooth each point with its neighbours; ties prefer the later
    epoch.  This keeps coverage/win-rate behavior stable across adjacent
    epochs while retaining the same constrained risk-coverage objective.
    """
    if not history:
        return int(epochs)
    config = config or DEFAULT_TRAINING_CONFIG
    late_count = max(5, min(8, max(1, int(epochs) // 3)))
    tail_start = max(0, len(history) - late_count)
    tail = history[tail_start:]
    scores = [_direct_policy_selection_score(item.get("validation") or {}, config) for item in tail]
    smooth = []
    for index, value in enumerate(scores):
        neighbours = [scores[pos] for pos in range(max(0, index - 1), min(len(scores), index + 2)) if math.isfinite(scores[pos])]
        smooth.append(float(np.mean(neighbours)) if neighbours else float("-inf"))
    # A smoothed neighbour is useful for ranking a real operating point, but
    # must not make an epoch with ``-inf`` (no admissible risk/coverage point)
    # eligible by proximity alone.  The previous code could therefore select
    # the final unstable epoch as STABLE when only the preceding epoch had a
    # valid calibration.
    finite = [index for index, value in enumerate(scores) if math.isfinite(value)]
    if not finite:
        return int(tail[-1].get("epoch") or epochs)
    return int(tail[max(finite, key=lambda index: (smooth[index], int(tail[index].get("epoch") or 0)))].get("epoch") or epochs)


_legacy._stable_checkpoint_epoch = _stable_checkpoint_epoch


def _direct_policy_cache_path(network, config):
    profile = _holding_profile(config.get("holdingProfile", MODEL_HOLDING_PROFILE))
    source = {
        "version": DATASET_VERSION,
        "network": network,
        "intervals": MACRO_INPUT_INTERVALS,
        "windows": WINDOWS,
        "stride": config["sampleStride"],
        "horizon": config["labelHorizonBars"],
        "entryExpiryBars": config["entryExpiryBars"],
        "holdingProfile": config.get("holdingProfile", MODEL_HOLDING_PROFILE),
        "referenceInterval": profile["referenceInterval"],
        "executionInterval": profile["executionInterval"],
        "holdingProfileVersion": HOLDING_PROFILE_VERSION,
        "forecastHorizonsBars": FORECAST_HORIZONS,
        "stopDistanceAtr": profile["stopDistanceAtr"],
        "targetDistanceAtr": profile["targetDistanceAtr"],
        "rewardRiskPenalty": config["rewardRiskPenalty"],
        "rewardTimePenalty": config["rewardTimePenalty"],
        "rewardWaitPenalty": config["rewardWaitPenalty"],
        "scanLimit": config["scanLimit"],
        "rollouts": config["rolloutsPerState"],
        "seed": config["seed"],
        "entryZoneGeometry": ENTRY_ZONE_GEOMETRY_VERSION,
    }
    digest = hashlib.sha256(json.dumps(source, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return DIRECT_POLICY_CACHE_DIRECTORY / f"{network}-{digest}.npz"


def _save_direct_policy_cache(path, reservoirs, info):
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {
        "datasetInfo": np.asarray(json.dumps(info, ensure_ascii=False, default=_legacy._json_default), dtype="U"),
        "splits": np.asarray(json.dumps(list(reservoirs.keys())), dtype="U"),
    }
    for split, reservoir in reservoirs.items():
        prefix = f"{split}."
        arrays[prefix + "features"] = reservoir.features[:reservoir.state_count]
        arrays[prefix + "stateSymbols"] = reservoir.state_symbols[:reservoir.state_count]
        arrays[prefix + "stateTimes"] = reservoir.state_times[:reservoir.state_count]
        arrays[prefix + "forecast_returns"] = reservoir.forecast_returns[:reservoir.size]
        arrays[prefix + "forecast_direction"] = reservoir.forecast_direction[:reservoir.size]
        for name in ("state_ids", "actions", "directions", "utility", "realized_r", "duration_bars", "regression", "outcomes", "entered", "symbols", "times", "rollout_count"):
            arrays[prefix + name] = getattr(reservoir, name)[:reservoir.size]
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp.npz")
    try:
        np.savez_compressed(temporary, **arrays)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_direct_policy_cache(path, config):
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as payload:
            splits = json.loads(str(payload["splits"].item()))
            reservoirs = {}
            for split in splits:
                features = payload[f"{split}.features"]
                actions = payload[f"{split}.actions"]
                reservoir = _DirectPolicyReservoir(max(1, len(actions)), features.shape[1], int(config["seed"]))
                reservoir.features[:len(features)] = features
                reservoir.state_symbols[:len(features)] = payload[f"{split}.stateSymbols"]
                reservoir.state_times[:len(features)] = payload[f"{split}.stateTimes"]
                reservoir.state_count = len(features)
                reservoir._lookup = {(str(reservoir.state_symbols[i]), int(reservoir.state_times[i])): i for i in range(reservoir.state_count)}
                reservoir.size = len(actions)
                reservoir.forecast_returns[:reservoir.size] = payload[f"{split}.forecast_returns"]
                reservoir.forecast_direction[:reservoir.size] = payload[f"{split}.forecast_direction"]
                for name in ("state_ids", "actions", "directions", "utility", "realized_r", "duration_bars", "regression", "outcomes", "entered", "symbols", "times", "rollout_count"):
                    getattr(reservoir, name)[:reservoir.size] = payload[f"{split}.{name}"]
                reservoir.seen = reservoir.size
                reservoirs[split] = reservoir
            info = json.loads(str(payload["datasetInfo"].item()))
            info.setdefault("collection", {})["corpusCacheHit"] = True
            return reservoirs, info
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _build_direct_policy_dataset(network, config, run_id):
    cache_path = _direct_policy_cache_path(network, config)
    cached = _load_direct_policy_cache(cache_path, config)
    if cached is not None:
        reservoirs, info = cached
        _legacy._update_job(run_id, phase="DATASET", message="已加载本地动作—结果语料库，跳过历史回放。", completed=1, total=1)
        return reservoirs, info
    replay = _legacy
    # The replay module owns the market corpus and adverse path helpers.
    import importlib
    replay_engine = None
    replay_import_error = None
    for replay_name in ((f"{__package__}.binance_strategy_backtest" if __package__ else ""), "backend.services.binance_strategy_backtest", "binance_strategy_backtest"):
        if not replay_name:
            continue
        try:
            replay_engine = importlib.import_module(replay_name)
            break
        except ImportError as exc:
            replay_import_error = exc
    if replay_engine is None:
        raise replay_import_error or ImportError("无法加载历史回放模块")
    intervals = MACRO_INPUT_INTERVALS
    token_count = sum(WINDOWS[item] for item in intervals)
    boundaries = _legacy._split_boundaries(config)
    universe = replay_engine._get_replay_market_universe(network, required_intervals=intervals)
    quotes = [dict(item) for item in universe.get("quotes") or [] if isinstance(item, dict)]
    if not quotes:
        raise ValueError(f"本地历史库没有同时覆盖 {'、'.join(intervals)} 的合约")
    history = replay_engine._load_macro_history(run_id, network, quotes, required_intervals=intervals)
    reservoirs = {
        "train": _DirectPolicyReservoir(config["maxTrainSamples"], token_count, int(config["seed"]) + 11),
        "validation": _DirectPolicyReservoir(config["maxValidationSamples"], token_count, int(config["seed"]) + 17),
        "test": _DirectPolicyReservoir(config["maxTestSamples"], token_count, int(config["seed"]) + 23),
    }
    action_count = 1 + 2 * int(config["rolloutsPerState"])
    state_limits = {name: max(1, reservoir.capacity // action_count) for name, reservoir in reservoirs.items()}
    state_counts = {name: 0 for name in reservoirs}
    profile = _holding_profile(config.get("holdingProfile", MODEL_HOLDING_PROFILE))
    reference_interval = str(profile["referenceInterval"])
    execution_interval = str(profile["executionInterval"])
    reference_symbol = "BTCUSDT" if "BTCUSDT" in history.get("items", {}) else (history.get("symbols") or [None])[0]
    reference_frame = ((history.get("items") or {}).get(reference_symbol or "", {}).get("frames") or {}).get(reference_interval)
    if not reference_frame:
        raise ValueError(f"历史库缺少用于生成时间锚点的{reference_interval}数据")
    anchors = [int(value) for value in replay_engine._frame_close_times(reference_frame) if _legacy._sample_split(int(value), boundaries)]
    anchors = anchors[::max(1, int(config["sampleStride"]))]
    rng = np.random.default_rng(int(config["seed"]))
    stats = {
        "trainingObjective": TRAINING_OBJECTIVE,
        "symbolsWithCompleteMacroData": len(history.get("symbols") or []),
        "scannedTimeGroups": len(anchors),
        "stateCount": 0,
        "transitionCount": 0,
        "rolloutCount": 0,
        "positiveUtilityCount": 0,
        "waitStateCount": 0,
        "pendingRejectedCount": 0,
        "rolloutsPerState": int(config["rolloutsPerState"]),
        "actionCountPerState": action_count,
        "actionSpace": "sampled LONG/SHORT/WAIT actions with one pending entry point; points crossed by the anchor candle are excluded before replay; allocation budget and split are sampled per action; no classic teacher",
        "allocationSampling": {"targetBudgetPercent": [32.0, 95.0], "firstLegShare": [0.08, 0.86], "runnerIsResidual": True},
        "intrabarPathPolicy": f"CONSERVATIVE_{execution_interval.upper()}_OHLC_SHARED_REPLAY_STATE_MACHINE",
        "minuteDataSource": "NOT_USED",
    }
    prepared_cache = {}
    _legacy._update_job(run_id, phase="ROLLOUTS", message="正在回放完整动作集合并保留全部结果（无教师计划）。", completed=0, total=len(anchors))
    for anchor_index, anchor_time in enumerate(anchors, start=1):
        if all(state_counts[name] >= state_limits[name] for name in reservoirs):
            break
        ranked = replay_engine._slice_top_turnover_symbols(history, anchor_time, None, int(config["scanLimit"]), interval=execution_interval)
        for symbol, _turnover in ranked:
            split = _legacy._sample_split(int(anchor_time), boundaries)
            reservoir = reservoirs.get(split)
            if reservoir is None or state_counts[split] >= state_limits[split] or reservoir.size + action_count > reservoir.capacity:
                continue
            dataset = (history.get("items") or {}).get(symbol) or {}
            prepared = prepared_cache.get(symbol)
            if prepared is None:
                compact = dataset.get("frames") or {}
                raw_frames = {interval: _legacy._numpy_frame_from_compact(compact.get(interval)) for interval in intervals}
                if any(frame is None for frame in raw_frames.values()):
                    continue
                prepared = {interval: _legacy._with_features(frame) for interval, frame in raw_frames.items()}
                prepared_cache[symbol] = prepared
            execution_frame = prepared[execution_interval]
            index = int(np.searchsorted(execution_frame["closeTime"], int(anchor_time), side="left"))
            if index >= len(execution_frame["closeTime"]) or int(execution_frame["closeTime"][index]) != int(anchor_time):
                continue
            tokens = _legacy._assemble_tokens(prepared, index, int(anchor_time), intervals)
            if tokens is None:
                continue
            forecast_returns, forecast_direction = _forecast_targets_at_anchor(execution_frame, index)
            anchor_close = float(execution_frame["close"][index])
            anchor_high = float(execution_frame["high"][index])
            anchor_low = float(execution_frame["low"][index])
            anchor_atr = max(float(execution_frame.get("atr14", np.asarray([0.0]))[index]), abs(anchor_close) * MIN_RISK_FRACTION, 1e-12)
            rows = [(DIRECTION_WAIT, np.zeros(len(DIRECT_PLAN_OUTPUT_NAMES) - 1, dtype=np.float32), {"entered": False, "outcome": "EXPIRE", "realizedR": 0.0, "durationBars": 0.0, "mfeR": 0.0, "maeR": 0.0, "maxDrawdownR": 0.0})]
            for side, direction in (("LONG", DIRECTION_LONG), ("SHORT", DIRECTION_SHORT)):
                for _ in range(int(config["rolloutsPerState"])):
                    action = _sample_direct_policy_action(rng, side, config)
                    if not _entry_point_is_pending(
                        action,
                        direction,
                        reference_price=anchor_close,
                        current_high=anchor_high,
                        current_low=anchor_low,
                        atr=anchor_atr,
                    ):
                        stats["pendingRejectedCount"] += 1
                        continue
                    outcome = _legacy._direct_policy_fast_replay(execution_frame, index, side, action, config)
                    rows.append((direction, action, outcome))
            for direction, action, outcome in rows:
                realized = float(_legacy._safe_number(outcome.get("realizedR")))
                duration = max(0.0, float(_legacy._safe_number(outcome.get("durationBars"))))
                drawdown = max(0.0, float(_legacy._safe_number(outcome.get("maxDrawdownR")) or _legacy._safe_number(outcome.get("maeR"))))
                utility = 0.0 if direction == DIRECTION_WAIT else realized - float(config["rewardRiskPenalty"]) * drawdown - float(config["rewardTimePenalty"]) * duration
                if direction != DIRECTION_WAIT and not bool(outcome.get("entered")):
                    utility -= float(config["rewardWaitPenalty"])
                result_utility = float(np.clip(utility, -8.0, 8.0))
                reservoir.add(tokens, action, direction, result_utility, realized, duration, np.asarray([_legacy._safe_number(outcome.get("mfeR")), _legacy._safe_number(outcome.get("maeR")), drawdown], dtype=np.float32), symbol, int(anchor_time), action_count, entered=bool(outcome.get("entered")), outcome=str(outcome.get("outcome") or "EXPIRE"), forecast_returns=forecast_returns, forecast_direction=forecast_direction)
                stats["transitionCount"] += 1
                if direction != DIRECTION_WAIT:
                    stats["rolloutCount"] += 1
                    stats["positiveUtilityCount"] += int(result_utility > 0.0)
            state_counts[split] += 1
            stats["stateCount"] += 1
            stats["waitStateCount"] += 1
        if anchor_index == 1 or anchor_index % 10 == 0 or anchor_index == len(anchors):
            _legacy._update_job(run_id, phase="ROLLOUTS", message="每个行情状态的全部动作结果已保留，不选择最高分教师动作。", current_symbol=f"{datetime.fromtimestamp(anchor_time / 1000, tz=timezone.utc).isoformat()} · states {stats['stateCount']} · transitions {stats['transitionCount']}", completed=anchor_index, total=len(anchors))
    if any(reservoir.size < 100 for reservoir in reservoirs.values()):
        raise ValueError("历史动作—结果语料不足，无法完成时间外训练")
    split_info = {name: {"startTime": value["start"], "endTime": value["end"], "start": _legacy._iso_ms(value["start"]), "end": _legacy._iso_ms(value["end"]), "purgedUntil": _legacy._iso_ms(value["usableEnd"])} for name, value in boundaries.items()}
    info = {
        "datasetVersion": DATASET_VERSION,
        "trainingObjective": TRAINING_OBJECTIVE,
        "sourceRange": {"startTime": FIXED_HISTORY_START_TIME, "endTime": FIXED_HISTORY_END_TIME, "start": _legacy._iso_ms(FIXED_HISTORY_START_TIME), "end": _legacy._iso_ms(FIXED_HISTORY_END_TIME)},
        "inputProfile": INPUT_PROFILE_MULTI_TIMEFRAME,
            "intervals": list(intervals),
            "referenceInterval": reference_interval,
            "executionInterval": execution_interval,
            "windows": dict(WINDOWS),
        "tokenCount": token_count,
        "features": list(FEATURE_NAMES),
        "outputs": list(DIRECT_PLAN_OUTPUT_NAMES),
        "planQualityOutputs": list(PLAN_QUALITY_OUTPUT_NAMES),
        "forecastOutputs": list(FORECAST_OUTPUT_NAMES),
        "forecastHorizonsBars": list(FORECAST_HORIZONS),
        "forecastQuantiles": list(FORECAST_QUANTILES),
        "split": split_info,
        "labelDefinition": {"taskType": MODEL_TASK_TYPE, "trainingObjective": TRAINING_OBJECTIVE, "entrySemantics": ENTRY_SEMANTICS_VERSION, "entryZoneGeometry": ENTRY_ZONE_GEOMETRY_VERSION, "state": "causal four-timeframe bars only", "action": "all WAIT/LONG/SHORT actions use one concrete entry point; points touched by the anchor candle are excluded before replay", "outcome": f"future {execution_interval} adverse replay per pending action", "selection": "none during preprocessing; critic selection only at inference", "path": f"4h/1h/15m/5m input, {execution_interval} profile execution, no 1m", "horizonBars": int(config["labelHorizonBars"]), "entryExpiryBars": int(config["entryExpiryBars"]), "holdingProfile": config.get("holdingProfile", MODEL_HOLDING_PROFILE), "holdingProfileVersion": HOLDING_PROFILE_VERSION, "referenceInterval": reference_interval, "executionInterval": execution_interval},
        "collection": stats,
        "splits": {name: reservoir.summary() for name, reservoir in reservoirs.items()},
        "corpusPath": str(cache_path),
    }
    _save_direct_policy_cache(cache_path, reservoirs, info)
    return reservoirs, info


def _train_direct_policy(datasets, dataset_info, config, run_id):
    torch = _legacy._require_torch()
    _legacy._seed_everything(torch, int(config["seed"]))
    device = _legacy._resolve_device(torch, config["device"])
    intervals = MACRO_INPUT_INTERVALS
    normalization = _build_direct_policy_normalization(datasets["train"], intervals)
    retrieval_bank = _build_retrieval_bank(datasets["train"], normalization, intervals, config)
    dataset_info["normalizationType"] = normalization["type"]
    try:
        dataset_info["directPlanBounds"] = _legacy._direct_plan_bounds_from_targets(datasets["train"].actions[:datasets["train"].size])
    except Exception:
        dataset_info["directPlanBounds"] = {}
    train_data = _TorchDirectPolicyDataset(torch, datasets["train"], normalization, intervals)
    validation_data = _TorchDirectPolicyDataset(torch, datasets["validation"], normalization, intervals)
    test_data = _TorchDirectPolicyDataset(torch, datasets["test"], normalization, intervals)
    if not len(train_data) or not len(validation_data) or not len(test_data):
        raise ValueError("训练、验证或测试动作语料为空")
    pin_memory = device.type == "cuda"
    train_loader = torch.utils.data.DataLoader(train_data, batch_size=int(config["batchSize"]), shuffle=True, num_workers=0, pin_memory=pin_memory)
    validation_loader = torch.utils.data.DataLoader(validation_data, batch_size=int(config["batchSize"]), shuffle=False, num_workers=0, pin_memory=pin_memory)
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=int(config["batchSize"]), shuffle=False, num_workers=0, pin_memory=pin_memory)
    model = _create_direct_policy_model(torch, config).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(config["learningRate"]), weight_decay=float(config["weightDecay"]))
    use_amp = device.type == "cuda"
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    history = []
    checkpoint_states = {}
    best_state = None
    best_score = float("-inf")
    best_epoch = 0
    started = time.perf_counter()
    for epoch in range(1, int(config["epochs"]) + 1):
        model.train()
        sums = {key: 0.0 for key in ("total", "criticUtility", "criticDrawdown", "duration", "fill", "target", "outcome", "regression", "direction", "action", "allocation", "forecast", "forecastDirection", "quality", "ranking")}
        seen = 0
        for batch in train_loader:
            inputs, actions, directions, utility, realized_r, duration, regression, forecast_returns, forecast_direction, outcomes, entered, groups = batch
            inputs = inputs.to(device, non_blocking=use_amp)
            actions = actions.to(device, non_blocking=use_amp)
            directions = directions.to(device, non_blocking=use_amp)
            utility = utility.to(device, non_blocking=use_amp)
            realized_r = realized_r.to(device, non_blocking=use_amp)
            duration = duration.to(device, non_blocking=use_amp)
            regression = regression.to(device, non_blocking=use_amp)
            outcomes = outcomes.to(device, non_blocking=use_amp)
            entered = entered.to(device, non_blocking=use_amp)
            forecast_returns = forecast_returns.to(device, non_blocking=use_amp)
            forecast_direction = forecast_direction.to(device, non_blocking=use_amp)
            groups = groups.to(device, non_blocking=use_amp)
            optimizer.zero_grad(set_to_none=True)
            context = torch.autocast(device_type="cuda", dtype=torch.float16, enabled=use_amp) if use_amp else nullcontext()
            with context:
                outputs = model(inputs, actions, directions)
                loss, parts = _direct_policy_loss(
                    torch, outputs, actions, directions, utility, realized_r, duration,
                    regression, outcomes, entered, forecast_returns, forecast_direction,
                    config, groups=groups,
                )
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimizer)
            scaler.update()
            count = int(inputs.shape[0])
            seen += count
            for key, value in parts.items():
                sums[key] += float(value.detach().item()) * count
        validation, _validation_values = _evaluate_direct_policy(
            torch, model, validation_loader, device, config,
            include_values=True,
            fit_calibration=True,
        )
        calibration = validation.get("selectionCalibration") if isinstance(validation.get("selectionCalibration"), dict) else {}
        test = _evaluate_direct_policy(
            torch, model, test_loader, device, config,
            selection_calibration=calibration,
        )
        record = {"epoch": epoch, "trainLoss": {key: round(value / max(1, seen), 6) for key, value in sums.items()}, "validation": validation, "test": test}
        history.append(record)
        best_score_for_epoch = _direct_policy_expected_return_score(validation, config)
        stable_score_for_epoch = _direct_policy_selection_score(validation, config)
        record["checkpointScores"] = {
            "bestExpectedReturn": round(float(best_score_for_epoch), 8) if math.isfinite(best_score_for_epoch) else None,
            "stableRiskCoverage": round(float(stable_score_for_epoch), 8) if math.isfinite(stable_score_for_epoch) else None,
        }
        if best_score_for_epoch > best_score:
            best_score = best_score_for_epoch
            best_epoch = epoch
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
        if epoch >= max(1, int(config["epochs"]) - 9):
            checkpoint_states[epoch] = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
        _legacy._update_job(run_id, phase="TRAIN", message=f"正在训练动作条件化策略第 {epoch}/{config['epochs']} 轮，已更新测试集与选择指标。", completed=epoch, total=int(config["epochs"]), metrics={"epochs": history, "bestEpoch": best_epoch})
    if best_state is None:
        best_epoch = int(config["epochs"])
        best_state = checkpoint_states.get(best_epoch) or {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
    stable_epoch = _stable_checkpoint_epoch(history, int(config["epochs"]), config)
    stable_state = checkpoint_states.get(stable_epoch) or best_state
    if stable_epoch not in checkpoint_states and checkpoint_states:
        stable_epoch = max(checkpoint_states)
        stable_state = checkpoint_states[stable_epoch]

    def branch(branch_name, epoch, state):
        model.load_state_dict(state)
        validation, _validation_values = _evaluate_direct_policy(
            torch, model, validation_loader, device, config,
            include_values=True,
            fit_calibration=True,
        )
        calibration = validation.get("selectionCalibration") if isinstance(validation.get("selectionCalibration"), dict) else {}
        effective_config = {**config, **calibration}
        calibration_status = str(calibration.get("selectionCalibrationStatus") or "").upper()
        safety = _direct_policy_checkpoint_safety(validation, effective_config)
        production_ready = bool(
            calibration_status != "NO_SAFE_OPERATING_POINT"
            and safety is not None
            and float(validation.get("selectedCoverage") or 0.0)
            >= float(effective_config.get("minimumSelectionCoverage", effective_config.get("coverageTarget", 0.08)))
        )
        train = _evaluate_direct_policy(torch, model, train_loader, device, effective_config)
        test = _evaluate_direct_policy(
            torch, model, test_loader, device, effective_config,
            selection_calibration=calibration,
        )
        metadata = {
            "runId": run_id,
            "modelVersion": MODEL_VERSION,
            "datasetVersion": DATASET_VERSION,
            "taskType": MODEL_TASK_TYPE,
            "trainingObjective": TRAINING_OBJECTIVE,
            "checkpointSelection": CHECKPOINT_SELECTION_VERSION,
            "entrySemantics": ENTRY_SEMANTICS_VERSION,
            "entryZoneGeometry": ENTRY_ZONE_GEOMETRY_VERSION,
            "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION,
            "outputNames": list(DIRECT_PLAN_OUTPUT_NAMES),
            "forecastOutputNames": list(FORECAST_OUTPUT_NAMES),
            "forecastHorizonsBars": list(FORECAST_HORIZONS),
            "forecastQuantiles": list(FORECAST_QUANTILES),
            "modelBranch": branch_name,
            "selectedEpoch": int(epoch),
            "trainingEpochs": int(config["epochs"]),
            "holdingProfile": config.get("holdingProfile", MODEL_HOLDING_PROFILE),
            "holdingProfileVersion": HOLDING_PROFILE_VERSION,
            "config": dict(effective_config),
            "featureNames": list(FEATURE_NAMES),
            "inputProfile": INPUT_PROFILE_MULTI_TIMEFRAME,
            "intervals": list(intervals),
            "referenceInterval": str(_holding_profile(config.get("holdingProfile"))["referenceInterval"]),
            "executionInterval": str(_holding_profile(config.get("holdingProfile"))["executionInterval"]),
            "windows": dict(WINDOWS),
            "tokenCount": sum(WINDOWS.values()),
            "featureDim": FEATURE_DIM,
            "actionFeatures": list(DIRECT_PLAN_OUTPUT_NAMES[1:]),
            "planQualityOutputNames": list(PLAN_QUALITY_OUTPUT_NAMES),
            "profitCalibrationTarget": "REALIZED_R_GT_ZERO",
            "productionReady": production_ready,
            "candidateGenerator": CANDIDATE_GENERATOR_VERSION,
            "retrievalBankVersion": RETRIEVAL_BANK_VERSION if retrieval_bank else None,
            "retrievalBankSize": int(retrieval_bank.get("stateCount") or 0) if retrieval_bank else 0,
            "normalization": normalization,
            "corpusFingerprint": dataset_info.get("corpusPath"),
            "trainedAt": _legacy._iso_ms(int(time.time() * 1000)),
        }
        metrics = {
            "epoch": int(epoch),
            "model": {"architecture": "market-to-plan-forecast-multiscale-patch-transformer-v36-profit-calibrated-wide-horizon", "candidateGenerator": CANDIDATE_GENERATOR_VERSION, "retrievalBankVersion": RETRIEVAL_BANK_VERSION, "modelVersion": MODEL_VERSION, "taskType": MODEL_TASK_TYPE, "trainingObjective": TRAINING_OBJECTIVE, "checkpointSelection": CHECKPOINT_SELECTION_VERSION, "entrySemantics": ENTRY_SEMANTICS_VERSION, "entryZoneGeometry": ENTRY_ZONE_GEOMETRY_VERSION, "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION, "forecastOutputNames": list(FORECAST_OUTPUT_NAMES), "forecastHorizonsBars": list(FORECAST_HORIZONS), "forecastQuantiles": list(FORECAST_QUANTILES), "planQualityOutputNames": list(PLAN_QUALITY_OUTPUT_NAMES), "profitCalibrationTarget": "REALIZED_R_GT_ZERO", "device": str(device), "parameterCount": int(sum(parameter.numel() for parameter in model.parameters())), "selectedEpoch": int(epoch), "trainingEpochs": int(config["epochs"]), "productionReady": production_ready, "holdingProfile": config.get("holdingProfile", MODEL_HOLDING_PROFILE), "holdingProfileVersion": HOLDING_PROFILE_VERSION, "referenceInterval": str(_holding_profile(config.get("holdingProfile"))["referenceInterval"]), "executionInterval": str(_holding_profile(config.get("holdingProfile"))["executionInterval"])},
            "epochs": history,
            "train": train,
            "validation": validation,
            "test": test,
            "selection": {
                "riskAversion": config["riskAversion"],
                "timeCostWeight": config["timeCostWeight"],
                "selectionMargin": config["selectionMargin"],
                "selectionFloor": config.get("selectionFloor"),
                "minimumFillProbability": config.get("minimumFillProbability"),
                "minimumTargetProbability": config.get("minimumTargetProbability"),
                "relaxedProbabilityFloor": config.get("relaxedProbabilityFloor"),
                "coverageTarget": config.get("coverageTarget"),
                "minimumSelectionCoverage": effective_config.get("minimumSelectionCoverage"),
                "coverageWeight": config.get("coverageWeight"),
                "minimumWinRate": effective_config.get("minimumWinRate"),
                "minimumCalibrationTrades": effective_config.get("minimumCalibrationTrades"),
                "riskCoverageConfidence": effective_config.get("riskCoverageConfidence"),
                "minimumSafetyLowerBound": effective_config.get("minimumSafetyLowerBound"),
                "fallbackWinRateTolerance": effective_config.get("fallbackWinRateTolerance"),
                "frontierMinimumWinRate": effective_config.get("frontierMinimumWinRate"),
                "frontierSafetyRelaxation": effective_config.get("frontierSafetyRelaxation"),
                "calibratedSelectionMargin": effective_config.get("calibratedSelectionMargin"),
                "calibratedSelectionFloor": effective_config.get("calibratedSelectionFloor"),
                "calibratedMinimumFillProbability": effective_config.get("calibratedMinimumFillProbability"),
                "calibratedMinimumTargetProbability": effective_config.get("calibratedMinimumTargetProbability"),
                "selectionCalibrationStatus": calibration.get("selectionCalibrationStatus"),
                "selectionCalibrationProbabilityMode": calibration.get("selectionCalibrationProbabilityMode"),
                "selectionCalibrationEmpiricalWinRate": calibration.get("selectionCalibrationEmpiricalWinRate"),
                "selectionCalibrationLowerBound": calibration.get("selectionCalibrationLowerBound"),
            },
        }
        payload = {"stateDict": {key: value.detach().cpu() for key, value in model.state_dict().items()}, "metadata": metadata}
        if retrieval_bank:
            payload["retrievalBank"] = {
                "version": retrieval_bank["version"],
                "features": torch.from_numpy(retrieval_bank["features"]),
                "actions": torch.from_numpy(retrieval_bank["actions"]),
                "directions": torch.from_numpy(retrieval_bank["directions"]),
                "utilities": torch.from_numpy(retrieval_bank["utilities"]),
                "entered": torch.from_numpy(retrieval_bank["entered"]),
            }
        return payload, metrics

    payloads = {}
    branch_metrics = {}
    payloads[MODEL_BRANCH_BEST], branch_metrics[MODEL_BRANCH_BEST] = branch(MODEL_BRANCH_BEST, best_epoch, best_state)
    payloads[MODEL_BRANCH_STABLE], branch_metrics[MODEL_BRANCH_STABLE] = branch(MODEL_BRANCH_STABLE, stable_epoch, stable_state)
    elapsed = round(time.perf_counter() - started, 2)
    for value in branch_metrics.values():
        value["model"]["trainingSeconds"] = elapsed
    metrics = dict(branch_metrics[MODEL_BRANCH_BEST])
    metrics["model"].update({"bestEpoch": best_epoch, "stableEpoch": stable_epoch, "trainingObjective": TRAINING_OBJECTIVE, "checkpointSelection": CHECKPOINT_SELECTION_VERSION})
    metrics["branches"] = {key: dict(value) for key, value in branch_metrics.items()}
    metrics["interpretation"] = {"directPolicy": "模型从四周期历史行情提出完整计划，critic 根据每个具体动作的未来回放结果评估；预处理不选择教师动作。", "wait": "每个状态只选择一个最高分非 WAIT 动作与 WAIT 比较；线上先复用验证集校准点，只有在方向、目标概率、下尾收益、回撤和预测 WAIT 主导检查均通过时才允许有界 coverage fallback。", "calibration": "胜率按实际 realized R 大于零统计，完整到达最终目标仍作为独立概率任务；最低选择覆盖率与安全下界共同决定检查点能否正式推理。", "checkpointSelection": "BEST 在通过胜率置信下限、最低覆盖率和校准安全门槛后，优先选择验证集 selectedRealizedR 较高的 epoch；STABLE 保持后期风险覆盖率平滑选择。", "geometry": "训练动作覆盖更宽的 ATR 止损和多档 R 倍数，预测最远覆盖完整 SWING 持仓窗口；宽度由历史结果学习，不在推理端硬改固定 R。"}
    payload = {"stateDict": payloads[MODEL_BRANCH_BEST]["stateDict"], "metadata": payloads[MODEL_BRANCH_BEST]["metadata"], "branchPayloads": payloads}
    return payload, metrics


def _run_training_job(run_id):
    job = _legacy._lookup_job(run_id)
    if not job:
        return
    try:
        _legacy._update_job(run_id, status="RUNNING", phase="DATASET", message="正在校验本地四周期历史数据并查找可复用动作语料。")
        config = _normalize_training_config(job.get("config") or {})
        datasets, info = _build_direct_policy_dataset(job["network"], config, run_id)
        _legacy._update_job(run_id, dataset=info, phase="TRAIN", message="动作—结果语料已准备，开始训练 action-conditioned critic 和 policy。")
        payload, metrics = _train_direct_policy(datasets, info, config, run_id)
        artifact_path, report_path = _legacy._persist_model(run_id, payload, info, metrics)
        metrics = {**metrics, "reportPath": report_path}
        _legacy._update_job(run_id, status="COMPLETED", dataset=info, metrics=metrics, artifact_path=artifact_path, phase="COMPLETED", message=f"{int(config['epochs'])} epoch v32 宽周期计划训练与时间外测试完成，BEST/STABLE 已保存。", completed=1, total=1)
        _legacy._invalidate_model_cache()
    except Exception as exc:
        _legacy._update_job(run_id, status="FAILED", error=str(exc) or "时序模型训练失败", phase="FAILED", message="v32 SWING 宽周期计划训练未完成。")


_legacy._run_training_job = _run_training_job


if _using_shim:
    def start_binance_ml_training(network="mainnet", config=None, *, strategy_settings=None):
        safe_network = _legacy._normalize_network(network)
        settings = _normalize_training_config(config or {})
        settings["strategySettings"] = {"strategyEngine": "MODEL", "modelBranch": MODEL_BRANCH_BEST, "modelRunId": None}
        run_id = f"ml-{int(time.time() * 1000)}-{uuid4().hex[:8]}"
        now = int(time.time() * 1000)
        job = {"id": run_id, "network": safe_network, "status": "QUEUED", "createdAt": now, "updatedAt": now, "progress": {"phase": "QUEUED", "message": "等待构建动作—结果语料。", "completed": 0, "total": 0, "currentSymbol": None}, "config": settings, "dataset": {}, "metrics": {}, "error": None, "artifactPath": None}
        with _shim_jobs_lock:
            if any(item.get("status") in {"QUEUED", "RUNNING"} for item in _shim_jobs.values()):
                raise ValueError("已有时序模型训练任务正在运行")
            _shim_jobs[run_id] = job
        _shim_db.init_db(); _shim_db.create_binance_ml_training_run(run_id, safe_network, settings)
        Thread(target=_run_training_job, args=(run_id,), daemon=True, name=f"binance-ml-{run_id[-8:]}").start()
        return _shim_job_snapshot(job)
else:
    start_binance_ml_training = _legacy.start_binance_ml_training

if hasattr(_legacy, "get_binance_ml_training_job"):
    get_binance_ml_training_job = _legacy.get_binance_ml_training_job
else:
    get_binance_ml_training_job = _legacy._training_job


def _model_record(network, run_id=None):
    try:
        if run_id:
            return _legacy._resolve_model_record(network, run_id)
        # The database may contain newer experimental runs (for example v33)
        # that are not the selected production ABI.  Resolve the newest
        # completed run for the version currently selected by MODEL_VERSION.
        safe_network = _legacy._normalize_network(network)
        for candidate in _legacy.db.list_binance_ml_training_runs(safe_network, 100):
            config = candidate.get("config") if isinstance(candidate, dict) else {}
            if str(candidate.get("status") or "").upper() == "COMPLETED" and str(config.get("modelVersion") or "") == MODEL_VERSION:
                return candidate
        return None
    except Exception:
        return None


def _resolve_model_record(network, run_id=None):
    record = _model_record(network, run_id)
    if isinstance(record, dict) and str(record.get("network") or "").lower() != _legacy._normalize_network(network):
        return None
    return record


def _is_compatible_model_metadata(metadata):
    """Return whether a checkpoint belongs to the current direct-plan ABI.

    ``entrySemantics`` is intentionally part of the compatibility fingerprint:
    a checkpoint trained on already-crossed entries can have the same tensor
    shape and output schema as the current model, but it is unsafe to load for
    pending-trigger inference.
    """

    if not isinstance(metadata, dict):
        return False
    profile_name = _holding_profile_name(metadata.get("holdingProfile"))
    profile = _holding_profile(profile_name)
    required = {
        "modelVersion": MODEL_VERSION,
        "datasetVersion": DATASET_VERSION,
        "taskType": MODEL_TASK_TYPE,
        "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION,
        "trainingObjective": TRAINING_OBJECTIVE,
        "entrySemantics": ENTRY_SEMANTICS_VERSION,
        "referenceInterval": str(profile["referenceInterval"]),
        "executionInterval": str(profile["executionInterval"]),
        "holdingProfile": profile_name,
        "holdingProfileVersion": HOLDING_PROFILE_VERSION,
        "profitCalibrationTarget": "REALIZED_R_GT_ZERO",
    }
    if not all(metadata.get(key) == expected for key, expected in required.items()):
        return False
    plan_quality = metadata.get("planQualityOutputNames")
    # Legacy checkpoints have no compatible auxiliary plan-quality head; the remaining ABI and forecast
    # layout are unchanged.  Newer versions must still carry the full list.
    quality_compatible = (plan_quality in (None, [], tuple()) or tuple(plan_quality or ()) == PLAN_QUALITY_OUTPUT_NAMES) if MODEL_VERSION.endswith("v29-multiscale-forecast-conditioned") else tuple(plan_quality or ()) == PLAN_QUALITY_OUTPUT_NAMES
    return (
        tuple(metadata.get("forecastOutputNames") or ()) == FORECAST_OUTPUT_NAMES
        and tuple(metadata.get("forecastHorizonsBars") or ()) == FORECAST_HORIZONS
        and tuple(metadata.get("forecastQuantiles") or ()) == FORECAST_QUANTILES
        and quality_compatible
        and metadata.get("productionReady") is not False
    )


def _v15_artifact(record, branch):
    if not isinstance(record, dict) or str(record.get("status") or "").upper() != "COMPLETED":
        return None
    config = record.get("config") if isinstance(record.get("config"), dict) else {}
    if str(config.get("modelVersion") or "") != MODEL_VERSION:
        return None
    dataset = record.get("dataset") if isinstance(record.get("dataset"), dict) else {}
    if str(dataset.get("datasetVersion") or "") != DATASET_VERSION:
        return None
    try:
        path = _legacy._artifact_file(_legacy._record_artifact_path(record, branch))
    except Exception:
        return None
    return path if path and path.is_file() else None


def _load_v15_model(network="mainnet", *, branch=MODEL_BRANCH_BEST, allow_rejected=False, run_id=None):
    safe_branch = str(branch or MODEL_BRANCH_BEST).upper()
    if safe_branch not in MODEL_BRANCHES:
        safe_branch = MODEL_BRANCH_BEST
    record = _model_record(network, run_id)
    artifact = _v15_artifact(record, safe_branch)
    if artifact is None:
        return None
    cache_key = (str(record.get("id")), safe_branch, artifact.stat().st_mtime_ns)
    with _policy_cache_lock:
        cached = _policy_model_cache.get(cache_key)
        if cached is not None:
            return cached
    try:
        torch = _legacy._require_torch()
        raw = torch.load(artifact, map_location="cpu", weights_only=True)
        metadata = raw.get("metadata") if isinstance(raw, dict) else None
        if not _is_compatible_model_metadata(metadata):
            return None
        config = _normalize_training_config({**(metadata.get("config") or {}), "holdingProfile": metadata.get("holdingProfile", MODEL_HOLDING_PROFILE)})
        profile_name = _holding_profile_name(metadata.get("holdingProfile"))
        if tuple(metadata.get("intervals") or ()) != MACRO_INPUT_INTERVALS or int(metadata.get("tokenCount") or 0) != sum(WINDOWS.values()):
            return None
        normalization = metadata.get("normalization") if isinstance(metadata.get("normalization"), dict) else {}
        model = _create_direct_policy_model(torch, config)
        # Legacy checkpoints predate the auxiliary plan-quality head.  The
        # policy/critic tensors are identical; load the checkpoint weights
        # non-strictly and disable the newer quality re-ranking terms below.
        model.load_state_dict(raw.get("stateDict") or {}, strict=False)
        device = _legacy._resolve_device(torch, config.get("device", "AUTO"))
        model = model.to(device)
        model.eval()
        raw_bank = raw.get("retrievalBank") if isinstance(raw, dict) else None
        retrieval_bank = None
        if isinstance(raw_bank, dict) and str(raw_bank.get("version") or "") == RETRIEVAL_BANK_VERSION:
            try:
                retrieval_bank = {
                    key: raw_bank[key].detach().float().cpu().numpy()
                    for key in ("features", "actions", "directions", "utilities", "entered")
                    if hasattr(raw_bank.get(key), "detach")
                }
                if not all(key in retrieval_bank for key in ("features", "actions", "directions")):
                    retrieval_bank = None
            except (TypeError, ValueError, RuntimeError):
                retrieval_bank = None
        loaded = {"model": model, "torch": torch, "device": device, "metadata": {**metadata, "holdingProfile": profile_name, "holdingProfileVersion": HOLDING_PROFILE_VERSION, "runId": metadata.get("runId") or record.get("id"), "modelBranch": safe_branch}, "config": config, "profile": _holding_profile(profile_name), "branch": safe_branch, "retrievalBank": retrieval_bank}
        with _policy_cache_lock:
            _policy_model_cache.clear()
            _policy_model_cache[cache_key] = loaded
        return loaded
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        return None


def has_binance_ml_model(network="mainnet", branch=MODEL_BRANCH_BEST, *, allow_rejected=False, run_id=None):
    return _load_v15_model(network, branch=branch, allow_rejected=allow_rejected, run_id=run_id) is not None


def get_binance_ml_holding_profile(network="mainnet", *, run_id=None, branch=MODEL_BRANCH_BEST):
    """Return the selected checkpoint's frozen holding profile for replay/UI."""
    loaded = _load_v15_model(_legacy._normalize_network(network), branch=branch, allow_rejected=True, run_id=run_id)
    if loaded:
        metadata = loaded.get("metadata") or {}
        name = _holding_profile_name(metadata.get("holdingProfile"))
        return {"id": name, **_holding_profile(name), "version": HOLDING_PROFILE_VERSION}
    return None


def get_binance_ml_execution_interval(network="mainnet", *, run_id=None, branch=MODEL_BRANCH_BEST, fallback=MODEL_EXECUTION_INTERVAL):
    profile = get_binance_ml_holding_profile(network, run_id=run_id, branch=branch)
    return str(profile.get("executionInterval")) if isinstance(profile, dict) else str(fallback)


def get_binance_ml_status(network="mainnet"):
    safe_network = _legacy._normalize_network(network)
    latest = _model_record(safe_network)
    latest_metrics = latest.get("metrics") if isinstance(latest, dict) and isinstance(latest.get("metrics"), dict) else {}
    branches = {}
    for branch in MODEL_BRANCHES:
        artifact = _v15_artifact(latest, branch)
        latest_config = latest.get("config") if isinstance(latest, dict) and isinstance(latest.get("config"), dict) else {}
        branch_metric = (latest_metrics.get("branches") or {}).get(branch) if isinstance(latest_metrics.get("branches"), dict) else {}
        model_metric = branch_metric.get("model") if isinstance(branch_metric, dict) and isinstance(branch_metric.get("model"), dict) else {}
        production_ready = model_metric.get("productionReady") is not False
        profile_name = _holding_profile_name(latest_config.get("holdingProfile"))
        selectable = bool(artifact and production_ready)
        branches[branch] = {"branch": branch, "runId": latest.get("id") if isinstance(latest, dict) else None, "artifactAvailable": bool(artifact), "selectable": selectable, "modelVersion": MODEL_VERSION, "entrySemantics": ENTRY_SEMANTICS_VERSION, "holdingProfile": profile_name, "holdingProfileVersion": HOLDING_PROFILE_VERSION, "executionInterval": _holding_profile(profile_name)["executionInterval"], "availabilityReason": None if selectable else "验证集未找到安全工作点" if artifact else "模型文件不可用"}
    active = None
    with _legacy._jobs_lock:
        _legacy._cleanup_jobs_locked()
        active = next((_legacy._job_snapshot(job) for job in _legacy._jobs.values() if job.get("network") == safe_network and job.get("status") in {"QUEUED", "RUNNING"}), None)
    return {
        "network": safe_network,
        "activeJob": active,
        "latestModel": _legacy._persisted_job_snapshot(latest) if latest else None,
        "available": bool(branches.get(MODEL_BRANCH_BEST, {}).get("selectable")),
        "researchArtifactAvailable": bool(any(item["artifactAvailable"] for item in branches.values())),
        "explicitUseAvailable": bool(any(item["selectable"] for item in branches.values())),
        "availabilityReason": None if branches.get(MODEL_BRANCH_BEST, {}).get("selectable") else "尚无通过验证校准的 v32 宽周期直接计划模型",
        "branches": branches,
        "modelVersion": MODEL_VERSION,
        "taskType": MODEL_TASK_TYPE,
        "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION,
        "datasetVersion": DATASET_VERSION,
        "entrySemantics": ENTRY_SEMANTICS_VERSION,
        "holdingProfileVersion": HOLDING_PROFILE_VERSION,
        "holdingProfiles": {name: {**profile, "id": name} for name, profile in HOLDING_PROFILES.items()},
        "input": {"defaultProfile": INPUT_PROFILE_MULTI_TIMEFRAME, "profiles": {INPUT_PROFILE_MULTI_TIMEFRAME: list(MACRO_INPUT_INTERVALS), INPUT_PROFILE_MACRO_ONLY: list(MACRO_INPUT_INTERVALS)}, "windows": dict(WINDOWS), "referenceInterval": MODEL_REFERENCE_INTERVAL, "executionInterval": MODEL_EXECUTION_INTERVAL, "features": list(FEATURE_NAMES), "outputs": list(DIRECT_PLAN_OUTPUT_NAMES), "forecastOutputs": list(FORECAST_OUTPUT_NAMES), "forecastHorizonsBars": list(FORECAST_HORIZONS), "forecastQuantiles": list(FORECAST_QUANTILES), "planQualityOutputs": list(PLAN_QUALITY_OUTPUT_NAMES), "candidateGenerator": CANDIDATE_GENERATOR_VERSION, "retrievalBankVersion": RETRIEVAL_BANK_VERSION, "labels": {"direction": list(DIRECTION_LABELS), "outcome": list(OUTCOME_LABELS)}}
    }


def list_binance_ml_models(network="mainnet", limit=24):
    safe_network = _legacy._normalize_network(network)
    try:
        limit = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        limit = 24
    runs = []
    for record in _legacy.db.list_binance_ml_training_runs(safe_network, max(100, limit)):
        config = record.get("config") if isinstance(record.get("config"), dict) else {}
        version = str(config.get("modelVersion") or record.get("modelVersion") or "")
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        branches = []
        for branch in MODEL_BRANCHES:
            artifact = _v15_artifact(record, branch)
            branch_metric = (metrics.get("branches") or {}).get(branch) if isinstance(metrics.get("branches"), dict) else {}
            profile_name = _holding_profile_name(config.get("holdingProfile"))
            model_metric = branch_metric.get("model") if isinstance(branch_metric, dict) and isinstance(branch_metric.get("model"), dict) else {}
            production_ready = model_metric.get("productionReady") is not False
            compatible = bool(artifact and production_ready and version == MODEL_VERSION and config.get("holdingProfileVersion") == HOLDING_PROFILE_VERSION and profile_name in HOLDING_PROFILES)
            branches.append({"runId": record.get("id"), "branch": branch, "modelVersion": version or MODEL_VERSION, "entrySemantics": ENTRY_SEMANTICS_VERSION if version == MODEL_VERSION else None, "taskType": MODEL_TASK_TYPE if version == MODEL_VERSION else "LEGACY_CANDIDATE", "outputSchemaVersion": DIRECT_PLAN_SCHEMA_VERSION if version == MODEL_VERSION else None, "holdingProfile": profile_name if version == MODEL_VERSION else None, "holdingProfileVersion": config.get("holdingProfileVersion"), "referenceInterval": _holding_profile(profile_name)["referenceInterval"] if version == MODEL_VERSION else None, "executionInterval": _holding_profile(profile_name)["executionInterval"] if version == MODEL_VERSION else None, "selectedEpoch": branch_metric.get("epoch") if isinstance(branch_metric, dict) else None, "artifactPath": _legacy._record_artifact_path(record, branch), "artifactAvailable": bool(artifact), "selectable": compatible, "eligible": compatible, "availabilityReason": None if compatible else "验证集未找到安全工作点" if artifact and version == MODEL_VERSION and not production_ready else "模型任务、周期档案或版本不兼容", "validation": (branch_metric or {}).get("validation", {}) if isinstance(branch_metric, dict) else {}, "test": (branch_metric or {}).get("test", {}) if isinstance(branch_metric, dict) else {}, "config": config, "dataset": record.get("dataset") if isinstance(record.get("dataset"), dict) else {}, "backtestParameters": {"trainingObjective": (branch_metric or {}).get("model", {}).get("trainingObjective") if isinstance(branch_metric, dict) else TRAINING_OBJECTIVE, "holdingProfile": profile_name if version == MODEL_VERSION else None}})
        runs.append({"runId": record.get("id"), "network": record.get("network"), "status": record.get("status"), "createdAt": record.get("createdAt"), "updatedAt": record.get("updatedAt"), "error": record.get("error"), "config": config, "dataset": record.get("dataset") if isinstance(record.get("dataset"), dict) else {}, "metrics": metrics, "branches": branches})
        if len(runs) >= limit:
            break
    latest = _model_record(safe_network)
    return {"network": safe_network, "runs": runs, "models": [branch for run in runs for branch in run["branches"] if branch.get("selectable")], "latestRunId": latest.get("id") if isinstance(latest, dict) else None, "runtimeAvailable": _legacy._model_runtime_available()[0], "runtimeReason": _legacy._model_runtime_available()[1]}


def _clip_policy_action(action):
    result = np.asarray(action, dtype=np.float32).copy()
    bounds = getattr(_legacy, "DIRECT_PLAN_HARD_BOUNDS", {})
    for index, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:]):
        # The recovered legacy module capped both ratio fields at 16, which
        # silently turned every v16 plan into the old 16%/16% shape.  Ratio
        # geometry has its own executable budget; all other fields retain the
        # legacy numeric bounds for compatibility.
        if name in {"targetOneRatio", "targetTwoRatio"}:
            low, high = (0.5, MAX_MODEL_FIXED_TARGET_RATIO)
        elif name == "stopDistanceAtr":
            low, high = (0.5, 12.0)
        elif name in {"targetOneDistanceAtr", "targetTwoDistanceAtr"}:
            low, high = (0.25, 30.0)
        else:
            low, high = bounds.get(name, (-16.0, 16.0))
        value = float(result[index]) if math.isfinite(float(result[index])) else (float(low) + float(high)) / 2.0
        result[index] = np.clip(value, float(low), float(high))
    # Preserve action geometry before it reaches the critic or decoder.
    index = {name: idx for idx, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
    result[index["targetTwoDistanceAtr"]] = max(result[index["targetTwoDistanceAtr"]], result[index["targetOneDistanceAtr"]] + 0.05)
    result[index["entryZoneLowAtr"]] = result[index["entryTriggerAtr"]]
    result[index["entryZoneHighAtr"]] = result[index["entryTriggerAtr"]]
    ratio1, ratio2 = _normalize_model_target_ratios(
        float(result[index["targetOneRatio"]]),
        float(result[index["targetTwoRatio"]]),
    )
    result[index["targetOneRatio"]] = ratio1
    result[index["targetTwoRatio"]] = ratio2
    return result


def _entry_point_is_pending(action, direction, *, reference_price, current_high=None, current_low=None, atr=None):
    """Return whether an action's point has not traded in the current bar.

    The training and serving paths share this gate.  Breakout points must be
    beyond the current bar in the direction of the order; pullback points
    must be beyond the opposite side.  A point inside the current bar is
    already touched and is never a valid pending plan.
    """
    try:
        names = {name: idx for idx, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
        reference = float(reference_price)
        trigger_atr = float(np.asarray(action, dtype=np.float64).reshape(-1)[names["entryTriggerAtr"]])
        safe_atr = max(float(atr or 0.0), abs(reference) * MIN_RISK_FRACTION, 1e-12)
        trigger = reference + trigger_atr * safe_atr
        high = float(current_high) if current_high is not None else reference
        low = float(current_low) if current_low is not None else reference
        if not all(math.isfinite(value) for value in (reference, trigger, high, low)):
            return False
        side = int(direction)
        epsilon = max(abs(trigger) * 1e-10, safe_atr * 1e-6, 1e-12)
        if side == DIRECTION_LONG:
            return trigger > max(reference, high) + epsilon or trigger < min(reference, low) - epsilon
        if side == DIRECTION_SHORT:
            return trigger < min(reference, low) - epsilon or trigger > max(reference, high) + epsilon
    except (TypeError, ValueError, IndexError, OverflowError, FloatingPointError):
        return False
    return False


def _proposal_actions(base, count):
    base = _clip_policy_action(base)
    index = {name: idx for idx, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
    proposals = []
    # Deterministic perturbations make the policy reproducible and let the
    # critic compare nearby geometries rather than one averaged plan.  Exit
    # allocation is searched as a separate axis: the actor proposes a mean,
    # while the critic gets to decide whether the first leg, extension leg, or
    # moving-stop remainder is most useful for this market state.
    scales = (0.72, 0.86, 1.0, 1.16, 1.38, 1.65)
    trigger_offsets = (-0.20, 0.0, 0.20, -0.35, 0.35)
    base_first = float(base[index["targetOneRatio"]])
    base_second = float(base[index["targetTwoRatio"]])
    base_total = float(np.clip(base_first + base_second, 10.0, MAX_MODEL_FIXED_TARGET_RATIO))
    base_share = base_first / max(base_first + base_second, 1e-6)
    # These are search points, not policy thresholds.  They intentionally span
    # runner-heavy, balanced, and extension-heavy plans so the learned critic
    # can select the allocation from the state/action outcome distribution.
    # Search a smooth neighbourhood around the actor's own allocation.  The
    # critic still chooses among alternatives, but no fixed 16%/50%/90% split
    # is imposed as a hidden business rule.
    allocation_patterns = []
    share_offsets = (-0.30, -0.18, -0.08, 0.0, 0.08, 0.18, 0.30)
    total_scales = (0.70, 0.85, 1.0, 1.15, 1.35)
    for offset_index in range(max(1, int(count))):
        total = float(np.clip(base_total * total_scales[offset_index % len(total_scales)], 10.0, MAX_MODEL_FIXED_TARGET_RATIO))
        share = float(np.clip(base_share + share_offsets[offset_index % len(share_offsets)], 0.06, 0.94))
        allocation_patterns.append(_normalize_model_target_ratios(total * share, total * (1.0 - share)))
    for offset_index in range(max(1, int(count))):
        candidate = base.copy()
        scale = scales[offset_index % len(scales)]
        candidate[index["stopDistanceAtr"]] *= scale
        candidate[index["targetOneDistanceAtr"]] *= scale
        candidate[index["targetTwoDistanceAtr"]] *= scale
        candidate[index["entryTriggerAtr"]] += trigger_offsets[offset_index % len(trigger_offsets)]
        candidate[index["entryZoneLowAtr"]] = candidate[index["entryTriggerAtr"]]
        candidate[index["entryZoneHighAtr"]] = candidate[index["entryTriggerAtr"]]
        allocation = allocation_patterns[offset_index % len(allocation_patterns)]
        candidate[index["targetOneRatio"]], candidate[index["targetTwoRatio"]] = allocation
        exit_bars = max(float(candidate[index["exitBars"]]), float(candidate[index["tp2Bars"]]), 1.0)
        risk_atr = max(float(candidate[index["stopDistanceAtr"]]), 1e-6)
        first_r = max(float(candidate[index["targetOneDistanceAtr"]]) / risk_atr, 0.0)
        candidate[index["timeEfficiency"]] = _swing_time_efficiency(first_r, exit_bars)
        candidate[index["holdingCost"]] = -float(DEFAULT_TRAINING_CONFIG.get("timeCostWeight", 0.0008)) * exit_bars
        proposals.append(_clip_policy_action(candidate))
    return proposals


def _forecast_plan_features(forecast, direction_probabilities=None):
    """Summarize the future distribution for plan generation/display."""
    values = np.asarray(forecast, dtype=np.float64).reshape(-1)
    if len(values) < len(FORECAST_HORIZONS) * len(FORECAST_QUANTILES):
        return {}
    grouped = values[: len(FORECAST_HORIZONS) * len(FORECAST_QUANTILES)].reshape(len(FORECAST_HORIZONS), len(FORECAST_QUANTILES))
    q10, q50, q90 = grouped[:, 0], grouped[:, 1], grouped[:, 2]
    spread = np.maximum(0.0, q90 - q10)
    result = {
        "medianReturns": [round(float(value), 6) for value in q50],
        "lowerReturns": [round(float(value), 6) for value in q10],
        "upperReturns": [round(float(value), 6) for value in q90],
        "uncertainty": [round(float(value), 6) for value in spread],
        "favorableMedianLong": round(float(np.mean(q50)), 6),
        "favorableMedianShort": round(float(np.mean(-q50)), 6),
        "directionalPersistence": round(float(np.mean(np.sign(q50))), 4),
        "horizonsBars": list(FORECAST_HORIZONS),
        "horizonInterval": MODEL_REFERENCE_INTERVAL,
    }
    if direction_probabilities is not None:
        probs = np.asarray(direction_probabilities, dtype=np.float64).reshape(-1)
        if len(probs) >= 3:
            result["directionProbabilities"] = {label: round(float(probs[index]), 4) for index, label in enumerate(DIRECTION_LABELS)}
    return result


def _forecast_guided_actions(base, forecast, side, count):
    """Create a small forecast-conditioned plan lattice around the actor.

    The actor gives a state-conditioned centre point; the future-distribution
    head supplies the direction, room and uncertainty that should be explored
    around that point.  This is deliberately a proposal mechanism, not a
    hand-written trading rule: every proposal is still scored by the same
    action-conditioned distributional critic before it can be selected.
    """
    base = _clip_policy_action(base)
    values = np.asarray(forecast, dtype=np.float64).reshape(-1)
    if len(values) < len(FORECAST_HORIZONS) * len(FORECAST_QUANTILES):
        return []
    names = {name: idx for idx, name in enumerate(DIRECT_PLAN_OUTPUT_NAMES[1:])}
    grouped = values[: len(FORECAST_HORIZONS) * len(FORECAST_QUANTILES)].reshape(len(FORECAST_HORIZONS), len(FORECAST_QUANTILES))
    q10, q50, q90 = grouped[:, 0], grouped[:, 1], grouped[:, 2]
    sign = 1.0 if int(side) == DIRECTION_LONG else -1.0
    favourable = sign * q50
    uncertainty = np.maximum(0.0, q90 - q10)
    persistence = float(np.tanh(np.mean(favourable) / max(float(np.mean(uncertainty)), 0.002)))
    near_edge = float(np.tanh(favourable[0] / max(float(uncertainty[0]), 0.002)))
    far_edge = float(np.tanh(favourable[-1] / max(float(uncertainty[-1]), 0.002)))
    room_scale = float(np.clip(1.0 + 0.30 * far_edge + 0.18 * persistence, 0.70, 1.55))
    risk_scale = float(np.clip(1.0 + 0.16 * (float(np.mean(uncertainty)) / 0.02 - 1.0), 0.78, 1.35))
    # Keep both breakout and pullback trigger geometries in the lattice.  The
    # critic decides which one is useful for this state; no sign is filtered.
    trigger_offsets = (0.22 * near_edge, -0.22 * near_edge, 0.34 * far_edge, -0.34 * far_edge)
    proposals = []
    for index in range(max(1, int(count))):
        candidate = base.copy()
        candidate[names["stopDistanceAtr"]] *= risk_scale
        candidate[names["targetOneDistanceAtr"]] *= float(np.clip(room_scale * (0.92 + 0.08 * (index % 3)), 0.65, 1.70))
        candidate[names["targetTwoDistanceAtr"]] *= float(np.clip(room_scale * (1.0 + 0.12 * ((index + 1) % 3)), 0.70, 1.95))
        offset = trigger_offsets[index % len(trigger_offsets)]
        candidate[names["entryTriggerAtr"]] += offset
        candidate[names["entryZoneLowAtr"]] = candidate[names["entryTriggerAtr"]]
        candidate[names["entryZoneHighAtr"]] = candidate[names["entryTriggerAtr"]]
        exit_bars = max(float(candidate[names["exitBars"]]), float(candidate[names["tp2Bars"]]), 1.0)
        risk_atr = max(float(candidate[names["stopDistanceAtr"]]), 1e-6)
        first_r = max(float(candidate[names["targetOneDistanceAtr"]]) / risk_atr, 0.0)
        candidate[names["timeEfficiency"]] = _swing_time_efficiency(first_r, exit_bars)
        candidate[names["holdingCost"]] = -float(DEFAULT_TRAINING_CONFIG.get("timeCostWeight", 0.0008)) * exit_bars
        proposals.append(_clip_policy_action(candidate))
    return proposals


def _retrieval_guided_actions(tokens, loaded, count):
    """Return actions from the nearest train-only causal market states.

    This is a small RAFT-style retrieval layer, deliberately subordinate to
    the live critic: retrieved actions are proposals, not copied decisions.
    Similarity uses the already-normalized multi-scale tensor and combines
    coarse pooled context with the latest-token context so a matching regime
    at a different price scale can still be found.
    """

    bank = loaded.get("retrievalBank") if isinstance(loaded, dict) else None
    if not isinstance(bank, dict):
        return []
    try:
        features = np.asarray(bank["features"], dtype=np.float32)
        actions = np.asarray(bank["actions"], dtype=np.float32)
        directions = np.asarray(bank["directions"], dtype=np.int64)
        if features.ndim != 3 or actions.ndim != 2 or len(features) != len(actions):
            return []
        query = np.asarray(tokens, dtype=np.float32)
        if query.ndim == 3:
            query = query[0]
        if query.ndim != 2 or query.shape != features.shape[1:]:
            return []
        # Normalize each state by its own energy to compare regimes instead of
        # absolute feature magnitude; retain latest-token detail as a tie-break.
        pooled = features.mean(axis=1)
        query_pooled = query.mean(axis=0)
        pooled_dist = np.mean((pooled - query_pooled[None, :]) ** 2, axis=1)
        latest_dist = np.mean((features[:, -min(16, features.shape[1]):, :] - query[-min(16, query.shape[0]):, :]) ** 2, axis=(1, 2))
        distances = 0.65 * pooled_dist + 0.35 * latest_dist
        neighbours = max(1, min(int(count), len(distances)))
        indexes = np.argsort(distances)[:neighbours]
        return [(int(directions[index]), np.asarray(actions[index], dtype=np.float32).copy()) for index in indexes]
    except (KeyError, TypeError, ValueError, IndexError):
        return []


def _predict_v15_candidates(tokens, loaded, *, reference_price=None, current_bar=None, atr=None):
    torch = loaded["torch"]
    model = loaded["model"]
    device = loaded["device"]
    metadata = loaded["metadata"]
    config = loaded["config"]
    raw_tokens = np.asarray(tokens, dtype=np.float32)
    if raw_tokens.ndim == 2:
        raw_tokens = raw_tokens[None, ...]
    normalized = _legacy._normalize_token_array(raw_tokens, metadata.get("normalization") or {}, MACRO_INPUT_INTERVALS)
    if normalized.ndim == 2:
        normalized = normalized[None, ...]
    values = torch.from_numpy(normalized).to(device)
    with _policy_cache_lock, torch.no_grad():
        actor = model(values)
        actor_plan = actor["directPlan"].detach().float().cpu().numpy()[0]
        direction_logits = actor["directDirection"].detach().float().cpu().numpy()[0]
        forecast = actor["forecast"].detach().float().cpu().numpy()[0]
        forecast_direction_logits = actor["forecastDirection"].detach().float().cpu().numpy()[0]
        forecast_direction_shifted = forecast_direction_logits - float(np.max(forecast_direction_logits))
        forecast_direction_probabilities = np.exp(np.clip(forecast_direction_shifted, -60.0, 60.0))
        forecast_direction_probabilities /= max(float(np.sum(forecast_direction_probabilities)), 1e-12)
        shifted = direction_logits - float(np.max(direction_logits))
        probabilities = np.exp(np.clip(shifted, -60.0, 60.0))
        probabilities /= max(float(np.sum(probabilities)), 1e-12)
    proposals = []
    proposal_count = int(config.get("proposalCount", 16))
    forecast_plan_features = _forecast_plan_features(forecast, forecast_direction_probabilities)
    for direction in (DIRECTION_LONG, DIRECTION_SHORT):
        local_actions = _proposal_actions(actor_plan, max(2, proposal_count // 2))
        local_actions.extend(_forecast_guided_actions(actor_plan, forecast, direction, max(2, proposal_count // 4)))
        for action in local_actions:
            if _entry_point_is_pending(
                action,
                direction,
                reference_price=reference_price,
                current_high=(current_bar or {}).get("high") if isinstance(current_bar, dict) else None,
                current_low=(current_bar or {}).get("low") if isinstance(current_bar, dict) else None,
                atr=atr,
            ):
                proposals.append((direction, action))
    # Add a small number of nearest historical actions.  The critic evaluates
    # these with the current state, so retrieval improves coverage without
    # turning a historical match into an unconditional trade signal.
    retrieval_actions = _retrieval_guided_actions(
        normalized,
        loaded,
        max(1, int(config.get("retrievalNeighbors", 8))),
    )
    for direction, action in retrieval_actions:
        clipped = _clip_policy_action(action)
        if _entry_point_is_pending(
            clipped,
            direction,
            reference_price=reference_price,
            current_high=(current_bar or {}).get("high") if isinstance(current_bar, dict) else None,
            current_low=(current_bar or {}).get("low") if isinstance(current_bar, dict) else None,
            atr=atr,
        ):
            proposals.append((direction, clipped))
    if not proposals:
            return {"wait": True, "reason": "MODEL_NO_PENDING_ENTRY_POINTS", "directionProbabilities": probabilities, "forecastPlanFeatures": forecast_plan_features}
    action_array = np.stack([item[1] for item in proposals]).astype(np.float32)
    direction_array = np.asarray([item[0] for item in proposals], dtype=np.int64)
    token_array = np.repeat(normalized, len(proposals), axis=0)
    with _policy_cache_lock, torch.no_grad():
        outputs = model(torch.from_numpy(token_array).to(device), torch.from_numpy(action_array).to(device), torch.from_numpy(direction_array).to(device))
        q = outputs["utilityQuantiles"].detach().float().cpu().numpy()
        dd = outputs["drawdownQuantiles"].detach().float().cpu().numpy()
        duration = torch.expm1(torch.clamp(outputs["duration"], min=0.0)).detach().float().cpu().numpy()
        fill = torch.sigmoid(outputs["fill"]).detach().float().cpu().numpy()
        target = torch.sigmoid(outputs["target"]).detach().float().cpu().numpy()
        quality = outputs["planQuality"].detach().float().cpu().numpy()
    scores = np.asarray([
        _conservative_action_score(
            q[index], dd[index], duration[index], fill[index], config,
            target=target[index], quality=quality[index],
        )
        for index in range(len(q))
    ], dtype=np.float64)
    # Couple plan direction to the state-only forecast distribution.  The
    # critic still owns execution quality, while this small term prevents a
    # LONG plan from winning when the median forecast is clearly negative
    # (and vice versa).  The q10/q90 spread is treated as forecast uncertainty.
    # Prefer the farthest horizon for the swing direction gate.  The critic
    # still decides the concrete action; this only aligns the plan side with
    # the multi-day forecast rather than a 3-hour return.
    far_horizon = max(FORECAST_HORIZONS)
    # Forecast output names are execution-profile qualified.  Do not retain
    # the old fixed-5m suffix here: SWING/POSITION labels are evaluated on
    # their declared execution cadence and the serving index must follow the
    # same schema.
    execution_suffix = "execution"
    forecast_median_index = FORECAST_OUTPUT_NAMES.index(f"return_{far_horizon}x{execution_suffix}_q50")
    forecast_low_index = FORECAST_OUTPUT_NAMES.index(f"return_{far_horizon}x{execution_suffix}_q10")
    forecast_high_index = FORECAST_OUTPUT_NAMES.index(f"return_{far_horizon}x{execution_suffix}_q90")
    median_return = float(forecast[forecast_median_index])
    forecast_spread = max(0.0, float(forecast[forecast_high_index] - forecast[forecast_low_index]))
    directional_edge = np.where(direction_array == DIRECTION_LONG, median_return, -median_return)
    forecast_alignment = np.clip(directional_edge / 0.01, -3.0, 3.0)
    forecast_support = np.asarray([
        float(forecast_direction_probabilities[int(direction)])
        if int(direction) < len(forecast_direction_probabilities) else 0.0
        for direction in direction_array
    ], dtype=np.float64)
    forecast_wait_probability = (
        float(forecast_direction_probabilities[DIRECTION_WAIT])
        if len(forecast_direction_probabilities) > DIRECTION_WAIT else 0.0
    )
    # A state-only forecast and an action-conditioned critic answer different
    # questions.  Do not hard-veto either one, but make a candidate pay a
    # visible cost when the forecast head is overwhelmingly WAIT while its
    # chosen side has almost no forecast support.  This prevents the fallback
    # route from turning an actor-side artifact into a trade without shrinking
    # normal LONG/SHORT coverage.
    forecast_score = float(0.06 * np.mean(forecast_alignment) - 0.01 * min(forecast_spread / 0.01, 3.0))
    scores = _forecast_adjusted_action_scores(
        scores,
        direction_array,
        forecast,
        forecast_direction_probabilities,
        config,
    )
    # Every proposal has already passed the same current-bar pending-point
    # gate used by dataset construction.  No stale point is re-armed here.
    scores_for_selection = scores
    # Evaluate the model's explicit WAIT action with the same critic.  The
    # selected trade must beat this learned no-position baseline, not merely
    # be positive according to an arbitrary zero threshold.
    wait_tokens = np.asarray(normalized, dtype=np.float32)
    wait_action = np.zeros((1, action_array.shape[1]), dtype=np.float32)
    wait_direction = np.asarray([DIRECTION_WAIT], dtype=np.int64)
    with _policy_cache_lock, torch.no_grad():
        wait_output = model(
            torch.from_numpy(wait_tokens).to(device),
            torch.from_numpy(wait_action).to(device),
            torch.from_numpy(wait_direction).to(device),
        )
        wait_q = wait_output["utilityQuantiles"].detach().float().cpu().numpy()[0]
        wait_dd = wait_output["drawdownQuantiles"].detach().float().cpu().numpy()[0]
        wait_duration = float(torch.expm1(torch.clamp(wait_output["duration"], min=0.0)).detach().float().cpu().numpy()[0])
        wait_fill = float(torch.sigmoid(wait_output["fill"]).detach().float().cpu().numpy()[0])
        wait_target = float(torch.sigmoid(wait_output["target"]).detach().float().cpu().numpy()[0])
        wait_quality = wait_output["planQuality"].detach().float().cpu().numpy()[0]
    wait_score = _conservative_action_score(wait_q, wait_dd, wait_duration, wait_fill, config, target=wait_target, quality=wait_quality)
    best = int(np.argmax(scores_for_selection))
    selection_fallback = False
    selection_reason = "ACTION_CONDITIONED_SELECTION"
    selection_accepts = _selection_accepts_action(
        scores[best], wait_score, fill[best], target[best], config,
    )
    if not selection_accepts:
        # A calibrated margin can still be too conservative under live regime
        # shift.  Permit a bounded recovery path only after the normal gate
        # fails and only when the explicit side/target/tail/drawdown checks
        # agree.  This path is deliberately observable in the response.
        calibration_status = str(config.get("selectionCalibrationStatus") or "").upper()
        calibrated_coverage = float(config.get("selectionCalibrationCoverage", 0.0) or 0.0)
        minimum_coverage = float(config.get("minimumSelectionCoverage", config.get("coverageTarget", 0.08)))
        selection_fallback = (
            calibration_status in {
                "SAFE_OPERATING_POINT",
                "LIMITED_SAFE_OPERATING_POINT",
                "DEGRADED_OPERATING_POINT",
                "COVERAGE_FRONTIER_OPERATING_POINT",
            }
            and calibrated_coverage + 1e-12 >= minimum_coverage
            and _coverage_fallback_accepts_action(
                scores[best],
                wait_score,
                q[best],
                dd[best],
                fill[best],
                target[best],
                probabilities[int(direction_array[best])] if int(direction_array[best]) < len(probabilities) else 0.0,
                config,
                forecast_support=float(forecast_support[best]) if len(forecast_support) else None,
                forecast_wait_probability=forecast_wait_probability,
            )
        )
        if selection_fallback:
            selection_reason = "BOUNDED_COVERAGE_FALLBACK"
    if not selection_accepts and not selection_fallback:
        return {"wait": True, "reason": "MODEL_BELOW_CALIBRATED_OPERATING_POINT", "directionProbabilities": probabilities, "forecast": forecast, "forecastDirectionProbabilities": forecast_direction_probabilities, "forecastPlanFeatures": forecast_plan_features, "forecastHorizonBars": list(FORECAST_HORIZONS), "forecastQuantiles": list(FORECAST_QUANTILES), "bestScore": float(scores[best]) if len(scores) else 0.0, "waitScore": float(wait_score), "uncertainty": float(max(0.0, q[best, -1] - q[best, 0])) if len(q) else 0.0, "bestDirection": int(direction_array[best]) if len(direction_array) else DIRECTION_WAIT, "bestFillProbability": float(fill[best]) if len(fill) else 0.0, "bestTargetProbability": float(target[best]) if len(target) else 0.0, "bestLowerReturn": float(q[best, 0]) if len(q) else 0.0, "bestDrawdownP90": float(dd[best, -1]) if len(dd) else 0.0, "bestPlanQuality": quality[best].tolist() if len(quality) else [], "selectionFallbackRejected": True}
    return {
        "wait": False,
        "direction": int(direction_array[best]),
        "action": action_array[best],
        "score": float(scores[best]),
        "waitScore": float(wait_score),
        "utilityQuantiles": q[best],
        "drawdownQuantiles": dd[best],
        "durationBars": float(max(0.0, duration[best])),
        "fillProbability": float(fill[best]),
        "targetProbability": float(target[best]),
        "planQuality": quality[best],
        "uncertainty": float(max(0.0, q[best, -1] - q[best, 0])),
        "directionProbabilities": probabilities,
        "forecast": forecast,
        "forecastDirectionProbabilities": forecast_direction_probabilities,
        "forecastPlanFeatures": forecast_plan_features,
        "forecastHorizonBars": list(FORECAST_HORIZONS),
        "forecastQuantiles": list(FORECAST_QUANTILES),
        "forecastScore": forecast_score,
        "forecastSupport": float(forecast_support[best]) if len(forecast_support) else 0.0,
        "proposalCount": len(proposals),
        "selectionFallback": selection_fallback,
        "selectionReason": selection_reason,
    }


def _predict_candidate_batch(tokens, plan_features, loaded):
    """Compatibility hook for older diagnostics; v16 serving never uses it."""

    return []


def _direct_plan_with_model(frames, loaded, *, base_plan=None):
    metadata = loaded.get("metadata") if isinstance(loaded.get("metadata"), dict) else {}
    profile_name = _holding_profile_name(metadata.get("holdingProfile") or (loaded.get("config") or {}).get("holdingProfile"))
    profile = _holding_profile(profile_name)
    reference_interval = str(profile["referenceInterval"])
    base = {key: value for key, value in (base_plan or {}).items() if not str(key).startswith("_")}
    symbol = str(base.get("symbol") or "").upper() or None

    def wait_result(reason):
        return {
            **base,
            "symbol": symbol,
            "direction": "WAIT",
            "status": "WAIT",
            "strategyEngine": "MODEL",
            "modelTask": MODEL_TASK_TYPE,
            "trainingObjective": TRAINING_OBJECTIVE,
            "modelVersion": MODEL_VERSION,
            "modelRunId": metadata.get("runId"),
            "modelBranch": metadata.get("modelBranch"),
            "modelGenerated": False,
            "modelReason": reason,
            "reason": reason,
            "conditionMet": 0,
            "conditionTotal": 1,
            "conditionCompleteness": 0,
            "quality": 0,
            "trialEligible": False,
            "blockedReasons": [reason],
            "missingConditions": [reason],
            "entry": {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"},
            "stopLoss": None,
            "takeProfits": [],
            "trailingStop": {},
            "timeCost": {},
        }

    model_inputs = _legacy._model_inputs_from_live_frames(frames, MACRO_INPUT_INTERVALS, loaded.get("config"))
    frame = _model_frame_from_live_bars(frames.get(reference_interval) if isinstance(frames, dict) else None)
    if model_inputs is None or frame is None:
        return wait_result("MODEL_INVALID")
    prepared = _shim_with_features(frame) if _using_shim else _legacy._with_features(frame)
    close = float(prepared["close"][-1])
    atr = max(float(prepared["atr14"][-1]), abs(close) * MIN_RISK_FRACTION, 1e-12)
    live_reference = close
    try:
        candidate_reference = float(base.get("lastPrice") or base.get("markPrice") or close)
        if math.isfinite(candidate_reference) and candidate_reference > 0:
            live_reference = candidate_reference
    except (TypeError, ValueError):
        pass
    try:
        selection = _predict_v15_candidates(
            model_inputs[0],
            loaded,
            # Candidate gating must use the same close/ATR reference that the
            # decoder below uses to materialize the final order price. Fold
            # the newest quote into the current candle range so an intrabar
            # touch cannot slip through between the bar snapshot and serving.
            reference_price=close,
            current_bar={
                "high": max(float(frame["high"][-1]), live_reference),
                "low": min(float(frame["low"][-1]), live_reference),
            },
            atr=atr,
        )
        forecast_values = selection.get("forecast")
        if not isinstance(forecast_values, (list, tuple, np.ndarray)):
            forecast_values = []
        forecast_probability_values = selection.get("forecastDirectionProbabilities")
        if not isinstance(forecast_probability_values, (list, tuple, np.ndarray)):
            forecast_probability_values = probabilities if "probabilities" in locals() else np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
        if selection.get("wait"):
            result = wait_result(str(selection.get("reason") or "MODEL_WAIT"))
            raw_probabilities = selection.get("directionProbabilities")
            probabilities = np.asarray(raw_probabilities if raw_probabilities is not None else [1.0, 0.0, 0.0], dtype=np.float64)
            forecast_probability_values = selection.get("forecastDirectionProbabilities")
            if not isinstance(forecast_probability_values, (list, tuple, np.ndarray)):
                forecast_probability_values = probabilities
            result["modelPrediction"] = {"direct": True, "actionConditioned": True, "trainingObjective": TRAINING_OBJECTIVE, "forecast": [round(float(value), 6) for value in forecast_values], "forecastDirectionProbabilities": {label: round(float(forecast_probability_values[index]), 4) for index, label in enumerate(DIRECTION_LABELS)}, "forecastPlanFeatures": selection.get("forecastPlanFeatures") or {}, "forecastHorizonBars": list(FORECAST_HORIZONS), "forecastQuantiles": list(FORECAST_QUANTILES), "directionProbabilities": {label: round(float(probabilities[index]), 4) for index, label in enumerate(DIRECTION_LABELS)}, "selectionScore": round(float(selection.get("bestScore") or 0.0), 4), "waitScore": round(float(selection.get("waitScore") or 0.0), 4), "uncertainty": round(float(selection.get("uncertainty") or 0.0), 4), "bestDirection": DIRECTION_LABELS[int(selection.get("bestDirection"))] if int(selection.get("bestDirection", DIRECTION_WAIT)) in range(len(DIRECTION_LABELS)) else None, "fillProbability": round(float(selection.get("bestFillProbability") or 0.0), 4), "targetProbability": round(float(selection.get("bestTargetProbability") or 0.0), 4), "lowerTailR": round(float(selection.get("bestLowerReturn") or 0.0), 4), "drawdownP90": round(float(selection.get("bestDrawdownP90") or 0.0), 4), "planQuality": {name: round(float(value), 4) for name, value in zip(PLAN_QUALITY_OUTPUT_NAMES, selection.get("bestPlanQuality") or [])}, "fallbackRejected": bool(selection.get("selectionFallbackRejected")), "verdict": "WAIT"}
            return result
        direction = DIRECTION_LABELS[int(selection["direction"])]
        action_values = _clip_policy_action(selection["action"])
        prediction = {name: float(value) for name, value in zip(DIRECT_PLAN_OUTPUT_NAMES[1:], action_values)}
        prediction["direction"] = direction
        decode_metadata = {**metadata, **(loaded.get("config") if isinstance(loaded.get("config"), dict) else {})}
        decoded = decode_direct_plan_prediction(prediction, reference_price=close, atr=atr, symbol=symbol, metadata=decode_metadata)
        if str(decoded.get("direction") or "WAIT").upper() == "WAIT":
            return wait_result("MODEL_INVALID")
        # Preserve the live/historical quote context on the decoded plan.  The
        # decoder is intentionally pure and therefore only knows its ATR
        # reference; the serving layer must attach the actual current price so
        # the shared execution gate can tell a pending trigger from one that
        # has already been crossed.
        decoded = {**base, **decoded}
        latest_bar = {
            "high": float(frame["high"][-1]),
            "low": float(frame["low"][-1]),
        }
        live_bars = frames.get("_liveBars") if isinstance(frames, dict) else None
        observed_bars = [latest_bar]
        if isinstance(live_bars, dict):
            live_bar = live_bars.get(reference_interval)
            if isinstance(live_bar, dict):
                observed_bars.append(live_bar)
        # Never move a stale trigger after the model has produced it.  A
        # point that has already traded is an invalidated setup, not a new
        # pending order at an arbitrary re-armed price.
        entry_state = _direct_model_entry_state(decoded, fallback_price=close, observed_bars=observed_bars)
        decoded["entryTiming"] = entry_state
        if not entry_state.get("pending", True):
            result = wait_result(str(entry_state.get("reason") or "MODEL_TRIGGER_ALREADY_CROSSED"))
            result["entryTiming"] = entry_state
            result["modelPrediction"] = {
                "direct": True,
                "actionConditioned": True,
                "trainingObjective": TRAINING_OBJECTIVE,
                "direction": direction,
                "forecast": [round(float(value), 6) for value in forecast_values],
                "forecastDirectionProbabilities": {label: round(float(forecast_probability_values[index]), 4) for index, label in enumerate(DIRECTION_LABELS)},
                "forecastHorizonBars": list(FORECAST_HORIZONS),
                "forecastQuantiles": list(FORECAST_QUANTILES),
                "normalized": prediction,
                "verdict": "WAIT",
            }
            return result
        # Keep the request snapshot (including the selected run/branch) on
        # the returned plan.  The replay defaults are only a fallback for
        # callers that invoke the decoder without a live analysis context.
        decoded["strategySettings"] = (
            base.get("strategySettings")
            if isinstance(base.get("strategySettings"), dict)
            else _legacy._direct_policy_replay_settings()
        )
        duration = max(1.0, float(selection.get("durationBars") or 1.0))
        raw_quantiles = selection.get("utilityQuantiles")
        raw_drawdowns = selection.get("drawdownQuantiles")
        quantiles = np.asarray(raw_quantiles if raw_quantiles is not None else [0.0, 0.0, 0.0], dtype=np.float64)
        drawdowns = np.asarray(raw_drawdowns if raw_drawdowns is not None else [0.0, 0.0], dtype=np.float64)
        time_cost = dict(decoded.get("timeCost") or {})
        time_cost.update({"tp1Bars": max(1.0, duration * 0.55), "tp2Bars": max(1.0, duration * 0.80), "exitBars": duration, "efficiency": _swing_time_efficiency(quantiles[min(1, len(quantiles) - 1)], duration, loaded["config"].get("labelHorizonBars")), "holdingCost": -abs(duration * float(loaded["config"].get("timeCostWeight", profile["timeCostWeight"]))), "holdingProfile": profile_name, "referenceInterval": reference_interval, "executionInterval": str(profile["executionInterval"]), "utilityQuantiles": [round(float(value), 4) for value in quantiles], "drawdownQuantiles": [round(float(value), 4) for value in drawdowns]})
        selection_reason = str(selection.get("selectionReason") or "ACTION_CONDITIONED_SELECTION")
        quality_values = np.asarray(selection.get("planQuality") if selection.get("planQuality") is not None else [], dtype=np.float64).reshape(-1)
        quality_score = 50.0
        if len(quality_values) >= len(PLAN_QUALITY_OUTPUT_NAMES):
            expected_component = (float(np.clip(quality_values[0], -1.0, 1.0)) + 1.0) * 0.5
            win_component = float(np.clip(quality_values[1], 0.0, 1.0))
            risk_component = 1.0 - min(max(float(quality_values[2]), 0.0) / 4.0, 1.0)
            time_component = (float(np.clip(quality_values[3], -1.0, 1.0)) + 1.0) * 0.5
            confidence_component = float(np.clip(quality_values[4], 0.0, 1.0))
            quality_score = 100.0 * float(np.clip(
                0.28 * expected_component
                + 0.28 * win_component
                + 0.16 * risk_component
                + 0.12 * time_component
                + 0.16 * confidence_component,
                0.0,
                1.0,
            ))
        decoded.update({"status": "ARMED", "modelTask": MODEL_TASK_TYPE, "trainingObjective": TRAINING_OBJECTIVE, "modelGenerated": True, "modelReason": selection_reason, "reason": selection_reason, "timeCost": time_cost, "quality": int(np.clip(round(quality_score), 0.0, 100.0)), "conditionMet": 1, "conditionTotal": 1, "conditionCompleteness": 100, "trialEligible": False, "blockedReasons": [], "missingConditions": [], "conditionChecks": [{"id": "conservativeUtility", "passed": True, "score": round(float(selection.get("score") or 0.0), 4), "waitScore": round(float(selection.get("waitScore") or 0.0), 4), "fallback": bool(selection.get("selectionFallback"))}, {"id": "triggerPending", "passed": bool(entry_state.get("pending")), "reason": entry_state.get("reason")} ]})
        raw_probabilities = selection.get("directionProbabilities")
        probabilities = np.asarray(raw_probabilities if raw_probabilities is not None else [0.0, 0.0, 0.0], dtype=np.float64)
        decoded["modelStrategy"] = {"engine": "MODEL", "trainingObjective": TRAINING_OBJECTIVE, "branch": metadata.get("modelBranch"), "holdingProfile": profile_name, "referenceInterval": reference_interval, "executionInterval": str(profile["executionInterval"]), "active": True, "accepted": True, "selectionFallback": bool(selection.get("selectionFallback")), "selectionReason": selection_reason, "selectionScore": round(float(selection["score"]), 4), "forecastScore": round(float(selection.get("forecastScore") or 0.0), 4), "forecastSupport": round(float(selection.get("forecastSupport") or 0.0), 4), "waitScore": round(float(selection.get("waitScore") or 0.0), 4), "expectedR": round(float(quantiles[min(1, len(quantiles) - 1)]), 4), "lowerTailR": round(float(quantiles[0]), 4), "drawdownP90": round(float(drawdowns[-1]), 4), "timeEfficiency": round(float(time_cost["efficiency"]), 4), "expectedDurationBars": round(duration, 2), "fillProbability": round(float(selection.get("fillProbability") or 0.0), 4), "targetProbability": round(float(selection.get("targetProbability") or 0.0), 4), "uncertainty": round(float(selection.get("uncertainty") or 0.0), 4), "planQuality": {name: round(float(value), 4) for name, value in zip(PLAN_QUALITY_OUTPUT_NAMES, quality_values)}, "proposalCount": int(selection.get("proposalCount") or 0)}
        decoded["modelPrediction"] = {"direct": True, "actionConditioned": True, "trainingObjective": TRAINING_OBJECTIVE, "direction": direction, "directionProbabilities": {label: round(float(probabilities[index]), 4) for index, label in enumerate(DIRECTION_LABELS)}, "forecast": [round(float(value), 6) for value in forecast_values], "forecastDirectionProbabilities": {label: round(float(forecast_probability_values[index]), 4) for index, label in enumerate(DIRECTION_LABELS)}, "forecastPlanFeatures": _forecast_plan_features(forecast_values, forecast_probability_values), "forecastHorizonBars": list(FORECAST_HORIZONS), "forecastQuantiles": list(FORECAST_QUANTILES), "forecastScore": round(float(selection.get("forecastScore") or 0.0), 4), "forecastSupport": round(float(selection.get("forecastSupport") or 0.0), 4), "selectionScore": round(float(selection["score"]), 4), "waitScore": round(float(selection.get("waitScore") or 0.0), 4), "expectedR": round(float(quantiles[min(1, len(quantiles) - 1)]), 4), "lowerTailR": round(float(quantiles[0]), 4), "utilityQuantiles": [round(float(value), 4) for value in quantiles], "drawdownQuantiles": [round(float(value), 4) for value in drawdowns], "planQuality": {name: round(float(value), 4) for name, value in zip(PLAN_QUALITY_OUTPUT_NAMES, quality_values)}, "expectedDurationBars": round(duration, 2), "fillProbability": round(float(selection.get("fillProbability") or 0.0), 4), "targetProbability": round(float(selection.get("targetProbability") or 0.0), 4), "uncertainty": round(float(selection.get("uncertainty") or 0.0), 4), "normalized": prediction, "selectionFallback": bool(selection.get("selectionFallback")), "selectionReason": selection_reason, "plan": {key: decoded.get(key) for key in ("entry", "stopLoss", "riskReward", "takeProfits", "trailingStop", "timeCost", "status")}, "verdict": "FAVORABLE"}
        return decoded
    except (TypeError, ValueError, KeyError, IndexError, OverflowError, FloatingPointError) as exc:
        # Keep the public decision contract safe (WAIT) but retain a compact
        # diagnostic for offline replay/research.  Without it an integration
        # mismatch is indistinguishable from a genuinely invalid plan.
        result = wait_result("MODEL_INVALID")
        result["modelError"] = f"{type(exc).__name__}:{str(exc)[:180]}"
        return result


def predict_binance_futures_frames(frames, *, network="mainnet", plan=None, branch=MODEL_BRANCH_BEST, allow_rejected_research_model=False, model_run_id=None):
    loaded = _load_v15_model(_legacy._normalize_network(network), branch=branch, allow_rejected=allow_rejected_research_model, run_id=model_run_id)
    if not loaded:
        return None
    result = _direct_plan_with_model(frames, loaded, base_plan=plan if isinstance(plan, dict) else None)
    if result is None:
        return None
    result.setdefault("modelPrediction", {})["productionUse"] = True
    return result


def attach_binance_ml_predictions(plans, network="mainnet", *, allow_rejected_research_model=False, branch=MODEL_BRANCH_BEST, model_run_id=None):
    loaded = _load_v15_model(_legacy._normalize_network(network), branch=branch, allow_rejected=allow_rejected_research_model, run_id=model_run_id)
    for plan in plans:
        frames = plan.pop("_mlFrames", None)
        if not loaded or not isinstance(frames, dict):
            if isinstance(plan, dict) and loaded is None:
                plan.update({"strategyEngine": "MODEL", "modelGenerated": False, "direction": "WAIT", "status": "WAIT", "modelReason": "MODEL_UNAVAILABLE", "reason": "MODEL_UNAVAILABLE"})
            continue
        direct = _direct_plan_with_model(frames, loaded, base_plan=plan)
        if direct is None:
            plan.update({"strategyEngine": "MODEL", "modelGenerated": False, "direction": "WAIT", "status": "WAIT", "modelReason": "MODEL_INVALID", "reason": "MODEL_INVALID"})
            continue
        plan.clear()
        plan.update(direct)
