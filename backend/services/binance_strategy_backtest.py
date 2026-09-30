from __future__ import annotations

from array import array
from datetime import datetime, timezone
import json
import math
import os
import random
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import FIRST_COMPLETED, CancelledError, TimeoutError as FutureTimeoutError, ThreadPoolExecutor, as_completed, wait
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

try:
    from . import database as db
    from .binance_client import BinanceApiError
    from .binance_futures_client import get_all_futures_markets, get_futures_historical_klines
    from .binance_ml import attach_binance_ml_predictions, get_binance_ml_status, get_binance_ml_execution_interval, has_binance_ml_model, predict_binance_futures_frames, MODEL_EXECUTION_INTERVAL as DIRECT_MODEL_EXECUTION_INTERVAL
    from .binance_futures_strategy import (
        _completed_bars,
        _entry_timing,
        _initial_level_basis,
        _plan_is_actionable,
        _plan_is_trial_eligible,
        _read_market,
        _strategy_mode,
        _strategy_engine,
        _model_strategy_settings,
        _model_branch,
        _atr,
        _model_run_id,
        _apply_model_strategy,
        MIN_SHORT_TERM_READING_BARS,
        STRATEGY_MODE_MIDLINE,
        STRATEGY_MODE_SHORT_TERM,
        analyze_futures_history,
    )
    from .binance_simulated_portfolio import (
        STOP_MANAGEMENT_STAGE_ORDER,
        TRAILING_LOOKBACK,
        _advance_stop_management_stage,
        _moving_stop_after_activation,
        _stop_is_tighter,
        _tighten,
        _trend_continuation_confirmed,
    )
except ImportError:
    import database as db
    from binance_client import BinanceApiError
    from binance_futures_client import get_all_futures_markets, get_futures_historical_klines
    from binance_ml import attach_binance_ml_predictions, get_binance_ml_status, get_binance_ml_execution_interval, has_binance_ml_model, predict_binance_futures_frames, MODEL_EXECUTION_INTERVAL as DIRECT_MODEL_EXECUTION_INTERVAL
    from binance_futures_strategy import (
        _completed_bars,
        _entry_timing,
        _initial_level_basis,
        _plan_is_actionable,
        _plan_is_trial_eligible,
        _read_market,
        _strategy_mode,
        _strategy_engine,
        _model_strategy_settings,
        _model_branch,
        _atr,
        _model_run_id,
        _apply_model_strategy,
        MIN_SHORT_TERM_READING_BARS,
        STRATEGY_MODE_MIDLINE,
        STRATEGY_MODE_SHORT_TERM,
        analyze_futures_history,
    )
    from binance_simulated_portfolio import (
        STOP_MANAGEMENT_STAGE_ORDER,
        TRAILING_LOOKBACK,
        _advance_stop_management_stage,
        _moving_stop_after_activation,
        _stop_is_tighter,
        _tighten,
        _trend_continuation_confirmed,
    )


INITIAL_BALANCE = 5.0
BACKTEST_DAYS = 7
BACKTEST_DEFAULT_MODE = "RANGE"
BACKTEST_INTERVAL_MS = 15 * 60 * 1000
SHORT_TERM_INTERVAL_MS = 5 * 60 * 1000
BACKTEST_DAY_MS = 24 * 60 * 60 * 1000
BACKTEST_MIN_DAYS = 1
BACKTEST_MAX_DAYS = 30
BACKTEST_MODE_RANDOM = "RANDOM"
BACKTEST_MODE_RANGE = "RANGE"
HISTORY_WARMUP_BARS = 80
# The fixed corpus contains June through August 2026 in Asia/Shanghai. Its
# start is midnight on June 1 locally; ``FIXED_HISTORY_END_TIME`` is exclusive.
FIXED_HISTORY_START_TIME = int(datetime(2026, 5, 31, 16, tzinfo=timezone.utc).timestamp() * 1000)
FIXED_HISTORY_END_TIME = int(datetime(2026, 9, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
# The earliest selectable replay start leaves exactly 80 completed 4h candles
# available for warm-up.  The default remains a 30-day window, while the UI
# can select any valid range from this boundary through the end of August.
EARLIEST_BACKTEST_START_TIME = int(datetime(2026, 6, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
DEFAULT_BACKTEST_START_TIME = int(datetime(2026, 8, 2, 0, tzinfo=timezone.utc).timestamp() * 1000)
DEFAULT_BACKTEST_END_TIME = FIXED_HISTORY_END_TIME
HISTORY_FETCH_PRIORITY = ("4h", "1h", "15m", "5m", "1m")
# The fixed corpus now includes the four streams used by direct-plan training
# and serving.  Classic midline analysis may still request only the macro
# subset; 1m is deliberately never part of replay and ambiguous candles use
# the direction-adverse OHLC path instead of making a network request.
MODEL_HISTORY_INTERVALS = ("4h", "1h", "15m", "5m")
FIXED_HISTORY_INTERVALS = MODEL_HISTORY_INTERVALS
MACRO_HISTORY_INTERVALS = ("4h", "1h", "15m")
SHORT_TERM_HISTORY_INTERVALS = ("4h", "1h", "15m", "5m")
HISTORY_PAGE_LIMIT = 1500
# Missing K-lines are network-bound. A single global pacer still spaces every
# request start by 0.5s (well below the public weighted limit); four workers
# keep response latency and SQLite writes from idling that pacing slot.
HISTORY_DOWNLOAD_WORKERS = 4
HISTORY_REQUEST_MIN_INTERVAL_SECONDS = 0.5
# SQLite already serves the bounded batch scans efficiently.  Multiple large
# readers contend for the same database pages on disk and were slower than a
# single sequential reader in practice, so keep local preparation serial while
# retaining high concurrency for network downloads below.
HISTORY_MEMORY_WORKERS = 1
# A bounded batch keeps the local 5m result small enough to compact as soon as
# it is read, while reducing 523 per-symbol queries to a few dozen per period.
HISTORY_MEMORY_SYMBOL_CHUNK = 32
BACKTEST_JOB_TTL_SECONDS = 30 * 60
HISTORY_COMPLETENESS_CACHE_TTL_SECONDS = 3.0
HISTORY_UNIVERSE_CACHE_TTL_SECONDS = 60.0
# The status/fill UI must remain useful when Binance's public market metadata
# endpoint is slow or rate-limited. A local coverage fallback is sufficient to
# inspect and repair already-known contracts, so do not hold the request for
# the client's full retry budget.
HISTORY_MARKET_LOOKUP_TIMEOUT_SECONDS = 8.0
POSITION_MARGIN_FRACTION = 0.25
MAX_MANAGED_POSITIONS = 4
MAX_LEVERAGE_CAP = 20
MAX_LEVERAGE = MAX_LEVERAGE_CAP
LEVERAGE_MODE_RISK_BUDGET = "RISK_BUDGET"
LEVERAGE_MODE_FIXED = "FIXED"
LEVERAGE_MODES = frozenset({LEVERAGE_MODE_RISK_BUDGET, LEVERAGE_MODE_FIXED})
DEFAULT_LEVERAGE_MODE = LEVERAGE_MODE_RISK_BUDGET
BACKTEST_SCAN_UNIVERSE_LIMIT = 100
# Backtest entries follow the same liquid-universe discipline as the live
# "寻找目标" scan, but use the only turnover known at a 15m slice's open:
# the immediately preceding completed 15m candle.  Do not search the full
# historical universe for every slice.
BACKTEST_SCAN_LIMIT = 100
MODEL_EVALUATION_MODE_OFF = "OFF"
MODEL_EVALUATION_MODE_COMPARE = "COMPARE"
MODEL_EVALUATION_MODES = frozenset({MODEL_EVALUATION_MODE_OFF, MODEL_EVALUATION_MODE_COMPARE})
MODEL_SELECTION_OFF = "OFF"
MODEL_SELECTION_FILTER = "FILTER"
EPSILON = 1e-10
BACKTEST_RECORDS_DIR = Path(
    os.environ.get("BINANCE_BACKTEST_RECORD_DIR")
    or (Path(__file__).resolve().parents[2] / "data" / "backtest_records")
)
BACKTEST_RECORD_VERSION = "binance-backtest-record-v2"
BAR_PATH_POLICY = "ADVERSE_FIRST_WITHOUT_1M_API"
BAR_PATH_POLICY_LABEL = "回测不调用1m；无法由最小执行K线确认先后时，做多先走低点、做空先走高点，采用方向不利的保守路径。"
MAX_FILTER_SAMPLES_PER_SCAN = 12
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
ENTRY_CONFIRMATION_RETEST = "RETEST_REQUIRED"
ENTRY_TYPE_STOP_TRIGGER = "STOP_TRIGGER"
ENTRY_TYPE_LIMIT_RETEST = "LIMIT_RETEST"

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = Lock()
_job_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="binance-strategy-backtest")
_history_market_lookup_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-history-market")
_history_db_write_lock = Lock()
_history_request_pacer_lock = Lock()
_history_completeness_lock = Lock()
_history_completeness_cache: dict[str, dict[str, Any]] = {}
# The history-status/fill path is allowed to fall back to every locally known
# symbol, including partially downloaded series.  Replay must never reuse that
# broader set as its strict universe, so cache the two resolution policies
# independently.
_history_market_universe_cache: dict[tuple[str, bool], dict[str, Any]] = {}
_replay_history_market_universe_cache: dict[str, dict[str, Any]] = {}
_history_request_next_at = 0.0


def _strategy_settings(value: object = None) -> dict[str, Any]:
    try:
        settings = db.normalize_binance_strategy_settings(value)
    except ValueError:
        settings = db.default_binance_strategy_settings()
    if _strategy_engine(settings) == "MODEL":
        return _model_strategy_settings(settings)
    return settings


def _parse_backtest_timestamp(value: object, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name}不能为空")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{field_name}无效")
        timestamp = int(value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"{field_name}不能为空")
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name}格式无效，请使用 UTC ISO 时间") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    return timestamp


def _normalize_backtest_number(
    value: object,
    field_name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if value is None or value == "":
        number = default
    else:
        if isinstance(value, bool):
            raise ValueError(f"{field_name}必须是数字")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name}必须是数字") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{field_name}必须在 {minimum:g} 至 {maximum:g} 之间")
    return int(number) if integer else number


def _normalize_backtest_options(
    value: object = None,
    *,
    strategy_mode: object = None,
    strategy_engine: object = None,
) -> dict[str, Any]:
    """Validate the replay window and execution parameters before a job starts."""

    if value is None:
        source: dict[str, Any] = {}
    elif isinstance(value, dict):
        source = value
    else:
        raise ValueError("回测窗口配置格式无效")

    mode = str(source.get("mode") or BACKTEST_DEFAULT_MODE).strip().upper()
    if mode not in {BACKTEST_MODE_RANDOM, BACKTEST_MODE_RANGE}:
        raise ValueError("回测模式无效，应为 RANDOM 或 RANGE")

    raw_strategy_mode = source.get("strategyMode", strategy_mode)
    strategy_mode_value = str(raw_strategy_mode or STRATEGY_MODE_MIDLINE).strip().upper()
    if strategy_mode_value not in {STRATEGY_MODE_MIDLINE, STRATEGY_MODE_SHORT_TERM}:
        raise ValueError("策略模式无效，应为 MIDLINE 或 SHORT_TERM")
    raw_strategy_engine = source.get("strategyEngine", strategy_engine)
    strategy_engine_value = str(raw_strategy_engine or "CLASSIC").strip().upper()
    if strategy_engine_value not in {"CLASSIC", "MODEL"}:
        raise ValueError("策略引擎无效，应为 CLASSIC 或 MODEL")
    replay_interval_ms = (
        SHORT_TERM_INTERVAL_MS
        if strategy_engine_value == "MODEL" or strategy_mode_value == STRATEGY_MODE_SHORT_TERM
        else BACKTEST_INTERVAL_MS
    )

    earliest_start = EARLIEST_BACKTEST_START_TIME
    initial_balance = _normalize_backtest_number(
        source.get("initialBalance"),
        "初始资金",
        default=INITIAL_BALANCE,
        minimum=0.000001,
        maximum=1_000_000_000,
    )
    position_margin_fraction = _normalize_backtest_number(
        source.get("positionMarginFraction"),
        "单仓位保证金比例",
        default=POSITION_MARGIN_FRACTION,
        minimum=0.000001,
        maximum=1,
    )
    max_managed_positions = _normalize_backtest_number(
        source.get("maxManagedPositions"),
        "管理仓位上限",
        default=MAX_MANAGED_POSITIONS,
        minimum=1,
        maximum=20,
        integer=True,
    )
    scan_limit = _normalize_backtest_number(
        source.get("scanLimit"),
        "扫描范围",
        default=BACKTEST_SCAN_UNIVERSE_LIMIT,
        minimum=1,
        maximum=1000,
        integer=True,
    )
    max_leverage = _normalize_backtest_number(
        source.get("maxLeverage"),
        "最高杠杆",
        default=MAX_LEVERAGE_CAP,
        minimum=1,
        maximum=MAX_LEVERAGE_CAP,
        integer=True,
    )
    raw_leverage_mode = source.get("leverageMode")
    raw_leverage = source.get("leverage")
    leverage_mode = str(raw_leverage_mode or (LEVERAGE_MODE_FIXED if raw_leverage is not None else DEFAULT_LEVERAGE_MODE)).strip().upper()
    if leverage_mode not in LEVERAGE_MODES:
        raise ValueError("杠杆模式无效，应为 RISK_BUDGET 或 FIXED")
    leverage = None
    if raw_leverage is not None and raw_leverage != "":
        leverage = _normalize_backtest_number(
            raw_leverage,
            "固定杠杆",
            default=1,
            minimum=1,
            maximum=MAX_LEVERAGE_CAP,
            integer=True,
        )
    if leverage_mode == LEVERAGE_MODE_FIXED and leverage is None:
        raise ValueError("FIXED 杠杆模式必须提供固定杠杆")
    if leverage_mode == LEVERAGE_MODE_FIXED and leverage > max_leverage:
        raise ValueError("固定杠杆不能高于最高杠杆")
    model_evaluation_mode = str(source.get("modelEvaluationMode") or MODEL_EVALUATION_MODE_OFF).strip().upper()
    if model_evaluation_mode not in MODEL_EVALUATION_MODES:
        raise ValueError("模型评估模式无效，应为 OFF 或 COMPARE")
    # Direct MODEL replay is already the model-vs-market path.  The legacy
    # baseline/filter comparison invokes the classic analyzer and would make a
    # model selection appear to use hidden classic settings, so never allow it
    # to leak into a MODEL job (including stale/API-supplied options).
    if strategy_engine_value == "MODEL":
        model_evaluation_mode = MODEL_EVALUATION_MODE_OFF
    raw_model_run_id = source.get("modelRunId")
    model_run_id = str(raw_model_run_id).strip() if raw_model_run_id is not None else ""
    if len(model_run_id) > 160:
        raise ValueError("回测模型训练任务标识过长")
    model_run_id = model_run_id or None
    if mode == BACKTEST_MODE_RANDOM:
        if model_evaluation_mode == MODEL_EVALUATION_MODE_COMPARE:
            raise ValueError("模型对照必须使用指定的留出测试区间，不能随机选择回测窗口")
        raw_days = source.get("days", BACKTEST_DAYS)
        if isinstance(raw_days, bool):
            raise ValueError("随机回测天数必须是整数")
        try:
            numeric_days = float(raw_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("随机回测天数必须是整数") from exc
        if not math.isfinite(numeric_days) or not numeric_days.is_integer():
            raise ValueError("随机回测天数必须是整数")
        days = int(numeric_days)
        available_days = max(0, (FIXED_HISTORY_END_TIME - earliest_start) // BACKTEST_DAY_MS)
        maximum = min(BACKTEST_MAX_DAYS, available_days)
        if days < BACKTEST_MIN_DAYS or days > maximum:
            raise ValueError(f"随机回测天数应为 {BACKTEST_MIN_DAYS} 至 {maximum} 天")
        return {
            "mode": BACKTEST_MODE_RANDOM,
            "strategyMode": strategy_mode_value,
            "strategyEngine": strategy_engine_value,
            "days": days,
            "startTime": None,
            "endTime": None,
            "initialBalance": initial_balance,
            "positionMarginFraction": position_margin_fraction,
            "maxManagedPositions": max_managed_positions,
            "scanLimit": scan_limit,
            "maxLeverage": max_leverage,
            "leverageMode": leverage_mode,
            "leverage": leverage,
            "modelEvaluationMode": model_evaluation_mode,
            "modelRunId": model_run_id,
        }

    start_time = _parse_backtest_timestamp(source.get("startTime", DEFAULT_BACKTEST_START_TIME), "开始时间")
    end_time = _parse_backtest_timestamp(source.get("endTime", DEFAULT_BACKTEST_END_TIME), "结束时间")
    if start_time % replay_interval_ms or end_time % replay_interval_ms:
        alignment = "5 分钟" if strategy_engine_value == "MODEL" or strategy_mode_value == STRATEGY_MODE_SHORT_TERM else "15 分钟"
        raise ValueError(f"开始时间和结束时间必须按 {alignment} 对齐")
    if start_time < earliest_start:
        earliest = datetime.fromtimestamp(earliest_start / 1000, tz=timezone.utc).isoformat()
        raise ValueError(f"开始时间过早，至少需要 {HISTORY_WARMUP_BARS} 根 4h 预热K线（不早于 {earliest}）")
    if end_time > FIXED_HISTORY_END_TIME:
        latest = datetime.fromtimestamp(FIXED_HISTORY_END_TIME / 1000, tz=timezone.utc).isoformat()
        raise ValueError(f"结束时间超出本地历史库范围（不晚于 {latest}）")
    if end_time <= start_time:
        raise ValueError("结束时间必须晚于开始时间")
    duration = end_time - start_time
    if duration > BACKTEST_MAX_DAYS * BACKTEST_DAY_MS:
        raise ValueError(f"指定回测区间不能超过 {BACKTEST_MAX_DAYS} 天")
    if model_evaluation_mode == MODEL_EVALUATION_MODE_COMPARE and strategy_mode_value != STRATEGY_MODE_MIDLINE:
        raise ValueError("当前模型对照只使用中线模式留出测试期；模型实际执行粒度固定为5m")
    return {
        "mode": BACKTEST_MODE_RANGE,
        "strategyMode": strategy_mode_value,
        "strategyEngine": strategy_engine_value,
        "days": None,
        "startTime": start_time,
        "endTime": end_time,
        "initialBalance": initial_balance,
        "positionMarginFraction": position_margin_fraction,
        "maxManagedPositions": max_managed_positions,
        "scanLimit": scan_limit,
        "maxLeverage": max_leverage,
        "leverageMode": leverage_mode,
        "leverage": leverage,
        "modelEvaluationMode": model_evaluation_mode,
        "modelRunId": model_run_id,
    }


def _position_strategy_settings(position: dict[str, Any] | None) -> dict[str, Any]:
    source = position if isinstance(position, dict) else {}
    raw = dict(source.get("strategySettings") or {}) if isinstance(source.get("strategySettings"), dict) else {}
    for key in ("protectiveTakeProfitRatio", "firstTakeProfitRatio", "secondTakeProfitRatio"):
        if key in source and key not in raw:
            raw[key] = source[key]
    return _strategy_settings(raw)


def _execution_settings(value: object = None) -> dict[str, Any]:
    normalized = _normalize_backtest_options(value)
    return {
        "strategyMode": normalized["strategyMode"],
        "initialBalance": float(normalized["initialBalance"]),
        "positionMarginFraction": float(normalized["positionMarginFraction"]),
        "maxManagedPositions": int(normalized["maxManagedPositions"]),
        "scanLimit": int(normalized["scanLimit"]),
        "maxLeverage": int(normalized["maxLeverage"]),
        "leverageMode": str(normalized["leverageMode"]),
        "leverage": int(normalized["leverage"]) if normalized.get("leverage") is not None else None,
    }


def _portfolio_execution_settings(portfolio: dict[str, Any] | None) -> dict[str, Any]:
    source = portfolio.get("_executionSettings") if isinstance(portfolio, dict) else None
    return _execution_settings(source if isinstance(source, dict) else None)


def _job_strategy_settings(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(str(job_id or "")) or {}
        return _strategy_settings(job.get("strategySettings"))


def _job_backtest_options(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(str(job_id or "")) or {}
        return _normalize_backtest_options(job.get("backtestOptions"))


def _model_evaluation_spec(network: str, model_run_id: object = None) -> dict[str, Any]:
    """Return a selected model's untouched test range and selection contract."""

    selected_id = str(model_run_id or "").strip()
    if selected_id:
        latest = db.get_binance_ml_training_run(selected_id) or {}
        if str(latest.get("network") or "").strip().lower() != str(network or "").strip().lower() or str(latest.get("status") or "").upper() != "COMPLETED":
            raise ValueError("模型对照选择的训练任务不存在或尚未完成")
        status = get_binance_ml_status(network)
        model_metrics_for_gate = latest.get("metrics") if isinstance(latest.get("metrics"), dict) else {}
        if not model_metrics_for_gate:
            raise ValueError("模型对照选择的训练任务缺少测试报告")
    else:
        status = get_binance_ml_status(network)
        if not bool(status.get("researchArtifactAvailable", status.get("available"))):
            reason = str(status.get("availabilityReason") or "本地时序模型不可用")
            raise ValueError(f"模型对照不可用：{reason}")
        latest = status.get("latestModel") if isinstance(status.get("latestModel"), dict) else {}
    dataset = latest.get("dataset") if isinstance(latest.get("dataset"), dict) else {}
    split = dataset.get("split") if isinstance(dataset.get("split"), dict) else {}
    test = split.get("test") if isinstance(split.get("test"), dict) else {}
    try:
        start_time = int(test.get("startTime"))
        end_time = int(test.get("endTime"))
    except (TypeError, ValueError) as exc:
        raise ValueError("模型对照缺少可验证的留出测试区间") from exc
    if start_time >= end_time:
        raise ValueError("模型对照的留出测试区间无效")
    model_metrics = latest.get("metrics") if isinstance(latest.get("metrics"), dict) else {}
    model_info = model_metrics.get("model") if isinstance(model_metrics.get("model"), dict) else {}
    return {
        "mode": MODEL_EVALUATION_MODE_COMPARE,
        "modelRunId": latest.get("id") or selected_id,
        "modelVersion": str(status.get("modelVersion") or ""),
        "architecture": str(model_info.get("architecture") or ""),
        "qualityGatePassed": bool(status.get("available")),
        "qualityGateReason": str(status.get("availabilityReason") or ""),
        "testStartTime": start_time,
        "testEndTime": end_time,
        "selectionRule": "仅接受模型方向与纪律方向一致、达到验证期时间效率阈值的候选，并按预测时间效率、预测R、成交额排序。研究回放允许评估被实时质量门拒绝的模型。",
    }


def _validated_model_evaluation_spec(network: str, options: dict[str, Any]) -> dict[str, Any] | None:
    if str(options.get("modelEvaluationMode") or MODEL_EVALUATION_MODE_OFF).upper() != MODEL_EVALUATION_MODE_COMPARE:
        return None
    spec = _model_evaluation_spec(network, options.get("modelRunId"))
    if int(options["startTime"]) != int(spec["testStartTime"]) or int(options["endTime"]) != int(spec["testEndTime"]):
        start = datetime.fromtimestamp(int(spec["testStartTime"]) / 1000, tz=timezone.utc).isoformat()
        end = datetime.fromtimestamp(int(spec["testEndTime"]) / 1000, tz=timezone.utc).isoformat()
        raise ValueError(f"模型对照只能使用该模型未参与训练的测试区间：{start} 至 {end}")
    return spec


def get_fixed_history_completeness(network: str = "mainnet") -> dict[str, Any]:
    """Return strict local-cache completeness for all active USDT perpetuals.

    This inspection never starts a history download; it only reports data that
    has already been persisted by an explicitly started backtest job.
    """

    safe_network = str(network or "mainnet").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("网络参数无效")

    now = time.monotonic()
    with _history_completeness_lock:
        cached = _history_completeness_cache.get(safe_network)
        if cached and now - float(cached.get("createdAt") or 0) < HISTORY_COMPLETENESS_CACHE_TTL_SECONDS:
            return deepcopy(cached["status"])

    universe = _get_fixed_history_market_universe(safe_network, allow_partial_local=True)
    symbols = list(universe["symbols"])

    status = db.summarize_binance_futures_history_completeness(
        safe_network,
        symbols,
        FIXED_HISTORY_INTERVALS,
        FIXED_HISTORY_START_TIME,
        FIXED_HISTORY_END_TIME,
    )
    status["updatedAt"] = _now_ms()
    status["marketUpdatedAt"] = _integer(universe.get("updatedAt"))
    status["marketStale"] = bool(universe.get("stale"))
    status["historySource"] = str(universe.get("source") or "LIVE_MARKET")
    status["offlineOnly"] = bool(universe.get("offlineOnly"))
    status["excludedSymbols"] = deepcopy(universe.get("excluded") or [])
    status["excludedCount"] = len(status["excludedSymbols"])

    with _history_completeness_lock:
        _history_completeness_cache[safe_network] = {
            "createdAt": time.monotonic(),
            "symbols": symbols,
            "status": deepcopy(status),
        }
    return status


def _get_fixed_history_market_universe(network: str, *, allow_partial_local: bool = False) -> dict[str, Any]:
    """Resolve the fixed-history universe, with an optional local fallback.

    Normally the current USDT perpetual list defines the universe.  If that
    public endpoint is unavailable (including an IP rate limit), a complete
    persisted corpus is enough for a deterministic historical replay.  The
    fallback remains strict for replay unless status/fill explicitly allow a
    partial local corpus. This keeps saved progress visible during outages.
    """

    now = time.monotonic()
    cache_key = (str(network or "").strip().lower(), bool(allow_partial_local))
    with _history_completeness_lock:
        cached = _history_market_universe_cache.get(cache_key)
        if cached and now - float(cached.get("createdAt") or 0) < HISTORY_UNIVERSE_CACHE_TTL_SECONDS:
            return deepcopy(cached)

    try:
        if allow_partial_local:
            # This call is only used to enrich the status/fill universe. Keep a
            # bounded wait so a temporary exchange outage does not make the
            # local history page appear broken for tens of seconds.
            future = _history_market_lookup_executor.submit(get_all_futures_markets, network)
            try:
                market_snapshot = future.result(timeout=HISTORY_MARKET_LOOKUP_TIMEOUT_SECONDS)
            except FutureTimeoutError as exc:
                future.cancel()
                raise BinanceApiError("读取交易所合约列表超时，已切换本地历史库", status_code=504) from exc
        else:
            market_snapshot = get_all_futures_markets(network)
    except BinanceApiError as exc:
        universe = _local_fixed_history_market_universe(
            network,
            rate_limit_error=exc,
            require_complete=not allow_partial_local,
        )
        if not universe.get("symbols"):
            raise
    else:
        selected_quotes, excluded_quotes = _select_fixed_history_quotes(market_snapshot.get("items") or [])
        if selected_quotes:
            universe = {
                "createdAt": now,
                "symbols": sorted({
                    str(item.get("symbol") or "").strip().upper()
                    for item in selected_quotes
                    if isinstance(item, dict) and str(item.get("symbol") or "").strip()
                }),
                "quotes": selected_quotes,
                "excluded": excluded_quotes,
                "updatedAt": _integer(market_snapshot.get("updatedAt")),
                "stale": bool(market_snapshot.get("stale")),
                "offlineOnly": False,
                "source": "LIVE_MARKET",
                "marketSnapshot": market_snapshot,
            }
        else:
            # A successful but empty market response is not enough to identify
            # the historical universe. Reuse local symbols for status/fill so
            # a temporary exchange response cannot hide saved progress.
            universe = _local_fixed_history_market_universe(
                network,
                rate_limit_error=RuntimeError("交易所未返回可用合约列表"),
                require_complete=not allow_partial_local,
            )
            if not universe.get("symbols"):
                universe = {
                    "createdAt": now,
                    "symbols": [],
                    "quotes": [],
                    "excluded": excluded_quotes,
                    "updatedAt": _integer(market_snapshot.get("updatedAt")),
                    "stale": True,
                    "offlineOnly": True,
                    "source": "LIVE_MARKET",
                    "marketSnapshot": market_snapshot,
                }
    with _history_completeness_lock:
        universe["createdAt"] = time.monotonic()
        _history_market_universe_cache[cache_key] = deepcopy(universe)
    return universe


def _get_replay_market_universe(
    network: str,
    *,
    required_intervals: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Prefer a verified local corpus for deterministic backtest replay.

    History preparation needs the current exchange universe so it can discover
    missing contracts. A replay of the fixed historical window is different:
    once a local corpus is complete for every bar the strategy consumes, using
    it first avoids needless public calls and keeps the result reproducible.
    """

    intervals = _ordered_history_intervals(required_intervals or FIXED_HISTORY_INTERVALS)
    cache_key = (network, intervals)
    now = time.monotonic()
    with _history_completeness_lock:
        cached = _replay_history_market_universe_cache.get(cache_key)
        if cached and now - float(cached.get("createdAt") or 0) < HISTORY_UNIVERSE_CACHE_TTL_SECONDS:
            return deepcopy(cached)
    universe = _local_fixed_history_market_universe(
        network,
        rate_limit_error=RuntimeError("固定历史回测优先使用本地已验证语料"),
        required_intervals=intervals,
    )
    if not universe.get("symbols"):
        universe = _get_fixed_history_market_universe(network)
    with _history_completeness_lock:
        universe["createdAt"] = time.monotonic()
        _replay_history_market_universe_cache[cache_key] = deepcopy(universe)
    return universe


def _local_fixed_history_market_universe(
    network: str,
    *,
    rate_limit_error: Exception,
    required_intervals: tuple[str, ...] | list[str] | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Build replay quotes from locally stored candles without network I/O.

    Replay prefers the strict intersection of complete series. History status
    and repair can request the broader set of symbols with any saved candle,
    which keeps partial local progress visible and repairable.
    """

    intervals = _ordered_history_intervals(required_intervals or FIXED_HISTORY_INTERVALS)
    if require_complete:
        symbols: set[str] | None = None
        for interval in intervals:
            complete_for_interval = set(db.list_complete_binance_futures_history_symbols(
                network,
                [interval],
                _offline_replay_history_start(interval),
                FIXED_HISTORY_END_TIME,
            ))
            symbols = complete_for_interval if symbols is None else symbols.intersection(complete_for_interval)
            if not symbols:
                break
        symbols = sorted(symbols or set())
    else:
        symbols = db.list_binance_futures_history_coverage_symbols(
            network,
            intervals,
            FIXED_HISTORY_START_TIME,
            FIXED_HISTORY_END_TIME,
        )
        if not symbols:
            # Older local databases may predate coverage metadata.  Preserve
            # the fallback by checking candle rows only when the compact index
            # has no entries at all.
            symbols = db.list_binance_futures_history_symbols(
                network,
                intervals,
                FIXED_HISTORY_START_TIME,
                FIXED_HISTORY_END_TIME,
            )
    if not symbols:
        return {
            "symbols": [],
            "quotes": [],
            "excluded": [],
            "updatedAt": _now_ms(),
            "stale": True,
            "offlineOnly": True,
            "source": "LOCAL_HISTORY",
            "marketSnapshot": {
                "items": [],
                "updatedAt": _now_ms(),
                "stale": True,
                "source": "LOCAL_HISTORY",
                "rateLimitMessage": str(rate_limit_error),
            },
        }
    # When 5m is part of the required execution corpus (MODEL and classic
    # SHORT_TERM), rank the replay universe from the same smallest completed
    # candle instead of silently using the legacy 15m quote snapshot.
    quote_interval = "5m" if "5m" in intervals else "15m" if "15m" in intervals else intervals[-1]
    quote_interval_ms = {
        "1m": 60_000,
        "5m": SHORT_TERM_INTERVAL_MS,
        "15m": BACKTEST_INTERVAL_MS,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
    }.get(quote_interval, BACKTEST_INTERVAL_MS)
    latest_bars = db.list_binance_futures_history_klines_batch(
        network,
        symbols,
        quote_interval,
        max(FIXED_HISTORY_START_TIME, FIXED_HISTORY_END_TIME - quote_interval_ms),
        FIXED_HISTORY_END_TIME,
    )
    quotes: list[dict[str, Any]] = []
    for symbol in symbols:
        bars = latest_bars.get(symbol) or []
        latest = bars[-1] if bars else {}
        quote_volume = _number(latest.get("quoteVolume"))
        if quote_volume <= 0:
            quote_volume = max(0.0, _number(latest.get("volume"))) * max(0.0, _number(latest.get("close")))
        quotes.append({
            "symbol": symbol,
            "quoteVolume": quote_volume,
            "onboardDate": 0,
            "historySource": "LOCAL_HISTORY",
        })
    quotes.sort(key=lambda item: (-_number(item.get("quoteVolume")), str(item.get("symbol") or "")))
    updated_at = _now_ms()
    return {
        "symbols": sorted(symbols),
        "quotes": quotes,
        "excluded": [],
        "updatedAt": updated_at,
        "stale": True,
        "offlineOnly": True,
        "source": "LOCAL_HISTORY",
        "marketSnapshot": {
            "items": deepcopy(quotes),
            "updatedAt": updated_at,
            "stale": True,
            "source": "LOCAL_HISTORY",
            "rateLimitMessage": str(rate_limit_error),
        },
    }


def _offline_replay_history_start(interval: str) -> int:
    """Return the earliest local candle needed to replay any allowed window."""

    interval_ms = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
    }.get(str(interval), BACKTEST_INTERVAL_MS)
    if interval == "1m":
        # One-minute candles only establish ordered paths inside a current 15m
        # bar, so they have no analytical warm-up requirement.
        required_start = EARLIEST_BACKTEST_START_TIME
    elif interval == "5m":
        # The entry-timing loader asks for six hours before the replay start.
        required_start = EARLIEST_BACKTEST_START_TIME - 6 * 60 * 60 * 1000
    else:
        required_start = EARLIEST_BACKTEST_START_TIME - HISTORY_WARMUP_BARS * interval_ms
    return max(FIXED_HISTORY_START_TIME, required_start)


def _invalidate_fixed_history_completeness(network: str) -> None:
    with _history_completeness_lock:
        safe_network = str(network or "").strip().lower()
        _history_completeness_cache.pop(safe_network, None)
        for key in list(_history_market_universe_cache):
            if key == safe_network or (isinstance(key, tuple) and key[0] == safe_network):
                _history_market_universe_cache.pop(key, None)
        for key in list(_replay_history_market_universe_cache):
            if key == safe_network or (isinstance(key, tuple) and key[0] == safe_network):
                _replay_history_market_universe_cache.pop(key, None)


def _select_fixed_history_quotes(quotes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only markets that could have a complete fixed history window.

    A currently trading contract launched after the fixed window began cannot
    ever satisfy the strict full-month check. Excluding it here prevents an
    empty pre-listing range from being retried on every history job while
    retaining older symbols whose K-line request can still be repaired.
    """

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for quote in quotes:
        if not isinstance(quote, dict):
            continue
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        onboard_date = _integer(quote.get("onboardDate"))
        if onboard_date > FIXED_HISTORY_START_TIME:
            excluded.append(
                {
                    "symbol": symbol,
                    "reason": "上线时间晚于固定历史区间开始时间，无法覆盖整段 6 至 8 月数据",
                    "onboardDate": onboard_date,
                }
            )
            continue
        selected.append(quote)
    excluded.sort(key=lambda item: (int(item.get("onboardDate") or 0), str(item.get("symbol") or "")))
    return selected, excluded


def start_current_strategy_backtest(
    network: str = "mainnet",
    *,
    user_id: int | None = None,
    backtest_options: object = None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Start a local, public-data replay. It never creates a Binance order."""

    if strategy_settings is not None:
        resolved_strategy_settings = _strategy_settings(strategy_settings)
    else:
        resolved_strategy_settings = (
            db.get_user_binance_strategy_settings(int(user_id))
            if user_id is not None
            else db.default_binance_strategy_settings()
        )
        if isinstance(backtest_options, dict) and backtest_options.get("strategyMode") is not None:
            # The CLI/API may provide the mode in the replay options when no
            # separate strategy snapshot was supplied. Promote it into the
            # same snapshot used by live analysis so a replay cannot silently
            # run a different route than the caller selected.
            resolved_strategy_settings = _strategy_settings({
                **resolved_strategy_settings,
                "strategyMode": backtest_options.get("strategyMode"),
            })
    normalized_options = _normalize_backtest_options(
        backtest_options,
        strategy_mode=resolved_strategy_settings.get("strategyMode"),
        strategy_engine=_strategy_engine(resolved_strategy_settings),
    )
    if normalized_options["strategyMode"] != resolved_strategy_settings["strategyMode"]:
        raise ValueError("回测参数中的策略模式必须与策略设置快照一致")
    safe_network, job_id, job = _create_history_job(
        network,
        "BACKTEST",
        strategy_settings=resolved_strategy_settings,
        backtest_options=normalized_options,
    )
    _job_executor.submit(_run_backtest_job, job_id, safe_network)
    return _job_snapshot(job)


def start_fixed_history_fill(network: str = "mainnet") -> dict[str, Any]:
    """Fill missing fixed-history K-lines without starting a replay."""

    safe_network, job_id, job = _create_history_job(network, "HISTORY_FILL")
    _job_executor.submit(_run_fixed_history_fill_job, job_id, safe_network)
    return _job_snapshot(job)


def _create_history_job(
    network: str,
    kind: str,
    *,
    strategy_settings: object = None,
    backtest_options: object = None,
) -> tuple[str, str, dict[str, Any]]:
    safe_network = str(network or "mainnet").strip().lower()
    if safe_network not in {"mainnet", "testnet"}:
        raise ValueError("网络参数无效")
    now = _now_ms()
    job_id = uuid4().hex
    job = {
        "id": job_id,
        "kind": kind,
        "network": safe_network,
        "status": "QUEUED",
        "createdAt": now,
        "updatedAt": now,
        "error": None,
        "progress": {
            "phase": "QUEUED",
            "message": "等待读取合约历史数据。",
            "completed": 0,
            "total": 0,
            "currentSymbol": None,
            "historyCached": False,
            "historyReady": False,
            "historyMissingRanges": 0,
            "downloadedBars": 0,
        },
        "result": None,
        "replayResult": None,
        "strategySettings": _strategy_settings(strategy_settings) if kind == "BACKTEST" else None,
        "backtestOptions": _normalize_backtest_options(backtest_options) if kind == "BACKTEST" else None,
    }
    with _jobs_lock:
        _cleanup_jobs_locked(now)
        _jobs[job_id] = job
    return safe_network, job_id, job


def get_current_strategy_backtest(job_id: str) -> dict[str, Any] | None:
    return _get_history_job(job_id, "BACKTEST")


def get_fixed_history_fill(job_id: str) -> dict[str, Any] | None:
    return _get_history_job(job_id, "HISTORY_FILL")


def _get_history_job(job_id: str, kind: str) -> dict[str, Any] | None:
    with _jobs_lock:
        _cleanup_jobs_locked(_now_ms())
        job = _jobs.get(str(job_id or ""))
        if not job or job.get("kind") != kind:
            return None
        return _job_snapshot(job)


def _run_backtest_job(job_id: str, network: str) -> None:
    strategy_settings = _job_strategy_settings(job_id)
    backtest_options = _job_backtest_options(job_id)
    if _model_run_id(strategy_settings) and not backtest_options.get("modelRunId"):
        backtest_options["modelRunId"] = _model_run_id(strategy_settings)
    strategy_mode = _strategy_mode(strategy_settings)
    required_intervals = (
        MODEL_HISTORY_INTERVALS
        if _strategy_engine(strategy_settings) == "MODEL"
        else SHORT_TERM_HISTORY_INTERVALS
        if strategy_mode == STRATEGY_MODE_SHORT_TERM
        else MACRO_HISTORY_INTERVALS
    )
    market_snapshot: dict[str, Any] | None = None
    quotes: list[dict[str, Any]] = []
    history_status: dict[str, Any] | None = None
    history: dict[str, Any] | None = None
    replay_result: dict[str, Any] | None = None
    try:
        _update_job(job_id, status="RUNNING", phase="MARKETS", message="正在确定本地固定历史语料中的可回测 USDT 永续合约。")
        universe = _get_replay_market_universe(
            network,
            required_intervals=required_intervals,
        )
        quotes = [dict(item) for item in (universe.get("quotes") or []) if isinstance(item, dict)]
        if not quotes:
            raise BinanceApiError("没有可用于回测的合约行情")
        # MODEL direct plans require all four model resolutions. Classic
        # short-term still loads 5m for execution; 1m intrabar ordering is
        # disabled and ambiguous candles use the adverse OHLC path below.
        if universe.get("offlineOnly"):
            history_status = _offline_history_status(quotes, required_intervals=required_intervals)
            _update_job(
                job_id,
                phase="HISTORY_READY",
                message="Binance 已限频；已确认完整本地历史库，离线回放继续执行。",
                history_cached=True,
                history_ready=True,
                history_missing_ranges=0,
            )
        else:
            history_status = _ensure_fixed_history(
                job_id,
                network,
                quotes,
                required_intervals=required_intervals,
            )
        if history_status["rateLimited"]:
            _mark_history_rate_limited(job_id, history_status)
            preparation_result = _history_preparation_result(history_status)
            preparation_result["backtestOptions"] = deepcopy(backtest_options)
            _persist_backtest_record(
                job_id,
                status="RATE_LIMITED",
                network=network,
                strategy_settings=strategy_settings,
                history_status=history_status,
                market_snapshot=market_snapshot,
                result=preparation_result,
                error="交易所限制历史请求",
            )
            return
        if not history_status["ready"]:
            remaining = history_status["missingRangeCount"]
            preparation_result = _history_preparation_result(history_status)
            preparation_result["backtestOptions"] = deepcopy(backtest_options)
            _update_job(
                job_id,
                status="HISTORY_INCOMPLETE",
                phase="HISTORY_INCOMPLETE",
                message=f"固定历史库尚有 {remaining} 个区间待补齐；本轮已保存成功数据，再次发起只会请求缺口。",
                history_cached=False,
                history_ready=False,
                history_missing_ranges=remaining,
                result=preparation_result,
            )
            _persist_backtest_record(
                job_id,
                status="HISTORY_INCOMPLETE",
                network=network,
                strategy_settings=strategy_settings,
                history_status=history_status,
                market_snapshot=market_snapshot,
                result=preparation_result,
                error=f"固定历史库尚有 {remaining} 个区间待补齐",
            )
            return
        history = _load_macro_history(
            job_id,
            network,
            quotes,
            required_intervals=required_intervals,
        )
        history["offlineOnly"] = bool(universe.get("offlineOnly"))
        start_time, end_time = _resolve_backtest_window(
            history,
            backtest_options,
            direct_model=_strategy_engine(strategy_settings) == "MODEL",
        )
        # Midline keeps 5m lazy because it is only an optional entry-timing
        # aid. Short-term mode has already loaded 5m as a required execution
        # frame and therefore never falls back to a macro-only scan.
        micro_history: dict[str, dict[str, Any]] = {}
        model_evaluation = _validated_model_evaluation_spec(network, backtest_options)
        model_engine_active = _strategy_engine(strategy_settings) == "MODEL" and has_binance_ml_model(
            network,
            _model_branch(strategy_settings),
            allow_rejected=True,
            run_id=_model_run_id(strategy_settings),
        )
        selected_model_branch = _model_branch(strategy_settings)
        selected_model_run_id = _model_run_id(strategy_settings)
        if model_evaluation:
            # Both paths read the same bars and use the same strategy
            # snapshot. Only the model filter differs; neither path requests
            # 1m data during replay.
            _update_job(
                job_id,
                phase="BASELINE",
                message="正在回放模型留出测试期的基线纪律策略。",
                completed=0,
                total=0,
            )
            baseline_result = _run_replay(
                job_id,
                network,
                history,
                micro_history,
                start_time,
                end_time,
                strategy_settings=strategy_settings,
                execution_options=backtest_options,
                model_branch=selected_model_branch,
                model_run_id=selected_model_run_id,
                publish_progress=False,
                replay_label="基线纪律策略",
            )
            _update_job(
                job_id,
                phase="MODEL_REPLAY",
                message="基线完成，正在按直接计划模型生成点位并回放完整仓位管理。",
                completed=0,
                total=0,
            )
            replay_result = _run_replay(
                job_id,
                network,
                history,
                micro_history,
                start_time,
                end_time,
                strategy_settings=strategy_settings,
                execution_options=backtest_options,
                model_selection=MODEL_SELECTION_FILTER,
                model_branch=selected_model_branch,
                model_run_id=selected_model_run_id,
                model_evaluation=model_evaluation,
                replay_label="模型过滤策略",
            )
            replay_result["modelEvaluation"] = _model_evaluation_result(
                baseline_result,
                replay_result,
                model_evaluation,
            )
        else:
            replay_result = _run_replay(
                job_id,
                network,
                history,
                micro_history,
                start_time,
                end_time,
                strategy_settings=strategy_settings,
                execution_options=backtest_options,
                model_selection=MODEL_SELECTION_FILTER if _strategy_engine(strategy_settings) == "MODEL" else MODEL_SELECTION_OFF,
                model_branch=selected_model_branch,
                model_run_id=selected_model_run_id,
            )
        result = replay_result
        result["strategySettings"] = strategy_settings
        result["backtestOptions"] = deepcopy(backtest_options)
        result["historyStale"] = False
        result["historyLoadedAt"] = _now_ms()
        result["marketCount"] = len(history["symbols"])
        result["historyRange"] = {
            "startTime": FIXED_HISTORY_START_TIME,
            "endTime": FIXED_HISTORY_END_TIME,
            "intervals": list(required_intervals),
            "requiredIntervals": list(required_intervals),
            "universeCount": len(quotes),
        }
        record_file = _persist_backtest_record(
            job_id,
            status="COMPLETED",
            network=network,
            strategy_settings=strategy_settings,
            history_status=history_status,
            market_snapshot=market_snapshot,
            result=result,
        )
        if record_file:
            result["recordFile"] = record_file
        _update_job(
            job_id,
            status="COMPLETED",
            phase="COMPLETED",
            message="回测完成。",
            result=result,
            replay_result=result,
        )
    except BinanceApiError as exc:
        if _is_history_rate_limited(exc):
            _update_job(
                job_id,
                status="RATE_LIMITED",
                phase="RATE_LIMITED",
                message="交易所已限制请求，本次回测已中断。",
            )
            _persist_backtest_record(
                job_id,
                status="RATE_LIMITED",
                network=network,
                strategy_settings=strategy_settings,
                history_status=history_status,
                market_snapshot=market_snapshot,
                result=replay_result,
                error=str(exc) or "交易所已限制请求",
            )
            return
        error_message = str(exc) or "回测失败"
        _update_job(job_id, status="FAILED", phase="FAILED", message="回测失败。", error=error_message)
        _persist_backtest_record(
            job_id,
            status="FAILED",
            network=network,
            strategy_settings=strategy_settings,
            history_status=history_status,
            market_snapshot=market_snapshot,
            result=replay_result,
            error=error_message,
        )
    except Exception as exc:
        error_message = str(exc) or "回测失败"
        _update_job(job_id, status="FAILED", phase="FAILED", message="回测失败。", error=error_message)
        _persist_backtest_record(
            job_id,
            status="FAILED",
            network=network,
            strategy_settings=strategy_settings,
            history_status=history_status,
            market_snapshot=market_snapshot,
            result=replay_result,
            error=error_message,
        )


def _persist_backtest_record(
    job_id: str,
    *,
    status: str,
    network: str,
    strategy_settings: dict[str, Any],
    history_status: dict[str, Any] | None,
    market_snapshot: dict[str, Any] | None,
    result: dict[str, Any] | None,
    error: str | None = None,
) -> str | None:
    """Write one self-contained replay/audit record without partial files."""

    try:
        directory = Path(BACKTEST_RECORDS_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        completed_at = _now_ms()
        file_name = f"{_record_timestamp(completed_at)}_{str(job_id or 'backtest')}.json"
        record_path = directory / file_name
        relative_path = _record_display_path(record_path)
        with _jobs_lock:
            job = dict(_jobs.get(str(job_id or "")) or {})
        replay = result if isinstance(result, dict) else {}
        timeline = replay.get("timeline") if isinstance(replay.get("timeline"), list) else []
        summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
        record = {
            "recordVersion": BACKTEST_RECORD_VERSION,
            "recordFile": relative_path,
            "job": {
                "id": str(job_id or ""),
                "kind": job.get("kind") or "BACKTEST",
                "status": str(status or "FAILED").upper(),
                "network": str(network or "mainnet").lower(),
                "createdAt": job.get("createdAt"),
                "completedAt": completed_at,
                "error": error,
                "backtestOptions": deepcopy(job.get("backtestOptions")) if isinstance(job.get("backtestOptions"), dict) else None,
            },
            "strategySettings": deepcopy(strategy_settings or {}),
            "backtestOptions": deepcopy(
                replay.get("backtestOptions")
                or job.get("backtestOptions")
                or _normalize_backtest_options()
            ),
            "sourcePolicy": {
                "id": "trading-discipline-source-policy",
                "version": "v2",
                "localOnly": True,
            },
            "history": {
                "fixedRange": replay.get("historyRange"),
                "preparation": deepcopy(history_status) if isinstance(history_status, dict) else None,
                "market": {
                    "updatedAt": market_snapshot.get("updatedAt") if isinstance(market_snapshot, dict) else None,
                    "stale": bool(market_snapshot.get("stale")) if isinstance(market_snapshot, dict) else None,
                    "universeCount": len(market_snapshot.get("items") or []) if isinstance(market_snapshot, dict) else None,
                },
            },
            "dataQuality": {
                "timeframe": replay.get("timeframe") or "15m",
                "oneMinuteChecks": summary.get("oneMinuteChecks", 0),
                "oneMinuteFallbacks": summary.get("oneMinuteFallbacks", 0),
                "conservativeIntrabarPaths": summary.get("conservativeIntrabarPaths", 0),
                "oneMinutePolicy": "DISABLED",
                "barPathPolicy": BAR_PATH_POLICY,
                "barPathPolicyLabel": BAR_PATH_POLICY_LABEL,
                "futureDataPolicy": f"每个候选只使用该{replay.get('timeframe') or '15m'}时间片开盘前已经收盘的K线。",
            },
            "replay": {
                "window": {
                    "startTime": replay.get("startTime"),
                    "endTime": replay.get("endTime"),
                    "asOfTime": replay.get("asOfTime"),
                },
                "timeframe": replay.get("timeframe") or "15m",
                "marketCount": replay.get("marketCount"),
                "processedSlices": replay.get("processedSlices", 0),
                "totalSlices": replay.get("totalSlices", 0),
                "summary": deepcopy(summary),
                "performance": deepcopy(replay.get("performance")) if isinstance(replay.get("performance"), dict) else None,
                "modelSelection": replay.get("modelSelection") or MODEL_SELECTION_OFF,
                "modelEvaluation": deepcopy(replay.get("modelEvaluation")) if isinstance(replay.get("modelEvaluation"), dict) else None,
                "timeline": deepcopy(timeline),
                "candidateScans": [
                    deepcopy(row.get("entryScan"))
                    for row in timeline
                    if isinstance(row, dict) and isinstance(row.get("entryScan"), dict)
                ],
                "tradeLedger": _trade_ledger(timeline),
            },
            "result": {
                "initialBalance": replay.get("initialBalance"),
                "finalAvailableBalance": replay.get("finalAvailableBalance"),
                "finalEquity": replay.get("finalEquity"),
                "totalPnl": replay.get("totalPnl"),
                "totalPnlPercent": replay.get("totalPnlPercent"),
                "performance": deepcopy(replay.get("performance")) if isinstance(replay.get("performance"), dict) else None,
                "modelEvaluation": deepcopy(replay.get("modelEvaluation")) if isinstance(replay.get("modelEvaluation"), dict) else None,
                "isPartial": bool(replay.get("isPartial")),
            },
        }
        payload = _json_safe(record)
        temporary_path = directory / f".{file_name}.{uuid4().hex}.tmp"
        try:
            with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            os.replace(temporary_path, record_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return relative_path
    except Exception:
        # A read-only or full disk must not turn a completed market replay
        # into a failed trading task. The API result remains available in
        # memory, while callers can inspect the missing record through logs.
        return None


def _record_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _record_display_path(path: Path) -> str:
    project_root = Path(__file__).resolve().parents[2]
    try:
        return str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _trade_ledger(timeline: list[Any]) -> list[dict[str, Any]]:
    """Convert timeline actions into compact per-entry trade histories."""

    open_trades: dict[tuple[str, str], list[dict[str, Any]]] = {}
    completed: list[dict[str, Any]] = []
    sequence = 0
    for row in timeline:
        if not isinstance(row, dict):
            continue
        event_time = row.get("time")
        actions = row.get("actions") if isinstance(row.get("actions"), list) else []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("type") or "").upper()
            symbol = str(action.get("symbol") or "").upper()
            side = str(action.get("side") or "").upper()
            if not symbol or side not in {"LONG", "SHORT"}:
                continue
            key = (symbol, side)
            if kind == "OPEN":
                sequence += 1
                open_trades[key] = [
                    {
                        "tradeId": f"{symbol}-{side}-{sequence}",
                        "symbol": symbol,
                        "side": side,
                        "entry": {
                            "time": event_time,
                            "price": action.get("price"),
                            "quantity": action.get("quantity"),
                        },
                        "events": [],
                        "realizedPnl": 0.0,
                        "status": "OPEN",
                    }
                ]
                continue
            trade_bucket = open_trades.get(key)
            if not trade_bucket:
                continue
            trade = trade_bucket[0]
            if kind not in {"TAKE_PROFIT", "STOP"}:
                continue
            trade["events"].append(
                {
                    "type": kind,
                    "time": event_time,
                    "price": action.get("price"),
                    "quantity": action.get("quantity"),
                    "pnl": action.get("pnl"),
                    "targetStage": action.get("targetStage"),
                    "note": action.get("note"),
                }
            )
            pnl = _number(action.get("pnl"))
            trade["realizedPnl"] = round(float(trade.get("realizedPnl") or 0.0) + pnl, 8)
            if kind == "STOP":
                trade["status"] = "STOPPED"
                trade["closedAt"] = event_time
                completed.append(trade)
                open_trades.pop(key, None)
    completed.extend(bucket[0] for bucket in open_trades.values())
    return _json_safe(completed)


def _replay_performance(result: dict[str, Any]) -> dict[str, Any]:
    """Calculate strategy metrics from executed exits and the replay equity curve."""

    timeline = result.get("timeline") if isinstance(result.get("timeline"), list) else []
    initial_balance = max(EPSILON, _number(result.get("initialBalance")))
    final_equity = _number(result.get("finalEquity"))
    closed_trades = [
        trade
        for trade in _trade_ledger(timeline)
        if isinstance(trade, dict) and str(trade.get("status") or "").upper() == "STOPPED"
    ]
    closed_pnls = [_number(trade.get("realizedPnl")) for trade in closed_trades]
    wins = [pnl for pnl in closed_pnls if pnl > EPSILON]
    losses = [pnl for pnl in closed_pnls if pnl < -EPSILON]
    interval_ms = SHORT_TERM_INTERVAL_MS if str(result.get("timeframe") or "15m").lower() == "5m" else BACKTEST_INTERVAL_MS
    holding_bars: list[int] = []
    winning_holding_bars: list[int] = []
    losing_holding_bars: list[int] = []
    first_target_before_stop_count = 0
    for trade in closed_trades:
        entry_time = _integer((trade.get("entry") or {}).get("time"))
        closed_at = _integer(trade.get("closedAt"))
        bars = max(1, max(0, closed_at - entry_time) // interval_ms + 1)
        holding_bars.append(bars)
        trade_pnl = _number(trade.get("realizedPnl"))
        if trade_pnl > EPSILON:
            winning_holding_bars.append(bars)
        elif trade_pnl < -EPSILON:
            losing_holding_bars.append(bars)
        events = trade.get("events") if isinstance(trade.get("events"), list) else []
        first_target_before_stop_count += int(
            any(
                str(event.get("type") or "").upper() == "TAKE_PROFIT"
                and (
                    str(event.get("targetStage") or "").upper() == "FIRST"
                    or str(event.get("note") or "").startswith("第一止盈")
                )
                for event in events
                if isinstance(event, dict)
            )
        )
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = gross_profit / len(wins) if wins else None
    average_loss = gross_loss / len(losses) if losses else None
    mean_holding_bars = sum(holding_bars) / len(holding_bars) if holding_bars else None
    ordered_holding_bars = sorted(holding_bars)
    if ordered_holding_bars:
        middle = len(ordered_holding_bars) // 2
        median_holding_bars = (
            float(ordered_holding_bars[middle])
            if len(ordered_holding_bars) % 2
            else (ordered_holding_bars[middle - 1] + ordered_holding_bars[middle]) / 2
        )
    else:
        median_holding_bars = None
    interval_hours = interval_ms / (60 * 60 * 1000)
    total_holding_hours = sum(holding_bars) * interval_hours
    equity_peak = initial_balance
    maximum_drawdown = 0.0
    maximum_drawdown_percent = 0.0
    drawdown_peak_time: int | None = None
    drawdown_trough_time: int | None = None
    peak_time: int | None = None
    for row in timeline:
        if not isinstance(row, dict):
            continue
        equity = _number(row.get("equity"))
        timestamp = _integer(row.get("time")) or None
        if equity > equity_peak + EPSILON:
            equity_peak = equity
            peak_time = timestamp
        drawdown = max(0.0, equity_peak - equity)
        drawdown_percent = drawdown / max(equity_peak, EPSILON) * 100
        if drawdown > maximum_drawdown + EPSILON:
            maximum_drawdown = drawdown
            maximum_drawdown_percent = drawdown_percent
            drawdown_peak_time = peak_time
            drawdown_trough_time = timestamp
    result = {
        "initialBalance": _round(initial_balance),
        "finalEquity": _round(final_equity),
        "netPnl": _round(final_equity - initial_balance),
        "totalReturnPercent": _round((final_equity / initial_balance - 1) * 100),
        "closedTradeCount": len(closed_pnls),
        "winningTradeCount": len(wins),
        "losingTradeCount": len(losses),
        "winRatePercent": _round(len(wins) / len(closed_pnls) * 100) if closed_pnls else None,
        "firstTargetBeforeStopCount": first_target_before_stop_count,
        "firstTargetBeforeStopRatePercent": _round(first_target_before_stop_count / len(closed_pnls) * 100) if closed_pnls else None,
        "grossProfit": _round(gross_profit),
        "grossLoss": _round(gross_loss),
        "profitFactor": _round(gross_profit / gross_loss) if gross_loss > EPSILON else None,
        "averageWin": _round(average_win),
        "averageLoss": _round(average_loss),
        "averageWinLossRatio": _round(average_win / average_loss) if average_win is not None and average_loss is not None and average_loss > EPSILON else None,
        "expectancyPerClosedTrade": _round(sum(closed_pnls) / len(closed_pnls)) if closed_pnls else None,
        "meanHoldingBars": _round(mean_holding_bars),
        "medianHoldingBars": _round(median_holding_bars),
        "meanHoldingHours": _round(mean_holding_bars * interval_hours) if mean_holding_bars is not None else None,
        "winningMeanHoldingHours": _round(sum(winning_holding_bars) / len(winning_holding_bars) * interval_hours) if winning_holding_bars else None,
        "losingMeanHoldingHours": _round(sum(losing_holding_bars) / len(losing_holding_bars) * interval_hours) if losing_holding_bars else None,
        "realizedPnlPerHoldingHour": _round(sum(closed_pnls) / total_holding_hours) if total_holding_hours > EPSILON else None,
        "openPositionCount": _integer((result.get("summary") or {}).get("openPositions")),
        "maximumDrawdown": _round(maximum_drawdown),
        "maximumDrawdownPercent": _round(maximum_drawdown_percent),
        "maximumDrawdownPeakTime": drawdown_peak_time,
        "maximumDrawdownTroughTime": drawdown_trough_time,
        "equityCurveDefinition": "每个执行时间片完成止盈、止损和收盘盯市后的权益快照。",
    }
    return result


def _model_filter_statistics(timeline: list[Any]) -> dict[str, Any]:
    result = {
        "disciplineEligibleCount": 0,
        "modelEvaluatedCount": 0,
        "modelAcceptedCount": 0,
        "modelRejectedCount": 0,
        "rejectedByReason": {},
    }
    for row in timeline:
        scan = row.get("entryScan") if isinstance(row, dict) and isinstance(row.get("entryScan"), dict) else {}
        model = scan.get("model") if isinstance(scan.get("model"), dict) else {}
        result["disciplineEligibleCount"] += _integer(scan.get("disciplineEligibleCount"))
        result["modelEvaluatedCount"] += _integer(model.get("evaluatedCount"))
        result["modelAcceptedCount"] += _integer(model.get("acceptedCount"))
        result["modelRejectedCount"] += _integer(model.get("rejectedCount"))
        target = result["rejectedByReason"]
        for reason, count in (model.get("rejectedByReason") or {}).items():
            target[str(reason)] = int(target.get(str(reason)) or 0) + _integer(count)
    return result


def _model_evaluation_result(
    baseline_result: dict[str, Any],
    model_result: dict[str, Any],
    specification: dict[str, Any],
) -> dict[str, Any]:
    baseline = _replay_performance(baseline_result)
    model_filtered = _replay_performance(model_result)
    return {
        "mode": MODEL_EVALUATION_MODE_COMPARE,
        "model": deepcopy(specification),
        "baseline": baseline,
        "modelFiltered": model_filtered,
        "selection": _model_filter_statistics(model_result.get("timeline") or []),
        "delta": {
            "netPnl": _round(_number(model_filtered.get("netPnl")) - _number(baseline.get("netPnl"))),
            "totalReturnPercent": _round(_number(model_filtered.get("totalReturnPercent")) - _number(baseline.get("totalReturnPercent"))),
            "maximumDrawdown": _round(_number(model_filtered.get("maximumDrawdown")) - _number(baseline.get("maximumDrawdown"))),
            "maximumDrawdownPercent": _round(_number(model_filtered.get("maximumDrawdownPercent")) - _number(baseline.get("maximumDrawdownPercent"))),
            "profitFactor": _round(_number(model_filtered.get("profitFactor")) - _number(baseline.get("profitFactor"))) if baseline.get("profitFactor") is not None and model_filtered.get("profitFactor") is not None else None,
            "winRatePercent": _round(_number(model_filtered.get("winRatePercent")) - _number(baseline.get("winRatePercent"))) if baseline.get("winRatePercent") is not None and model_filtered.get("winRatePercent") is not None else None,
            "firstTargetBeforeStopRatePercent": _round(_number(model_filtered.get("firstTargetBeforeStopRatePercent")) - _number(baseline.get("firstTargetBeforeStopRatePercent"))) if baseline.get("firstTargetBeforeStopRatePercent") is not None and model_filtered.get("firstTargetBeforeStopRatePercent") is not None else None,
            "averageWinLossRatio": _round(_number(model_filtered.get("averageWinLossRatio")) - _number(baseline.get("averageWinLossRatio"))) if baseline.get("averageWinLossRatio") is not None and model_filtered.get("averageWinLossRatio") is not None else None,
            "expectancyPerClosedTrade": _round(_number(model_filtered.get("expectancyPerClosedTrade")) - _number(baseline.get("expectancyPerClosedTrade"))) if baseline.get("expectancyPerClosedTrade") is not None and model_filtered.get("expectancyPerClosedTrade") is not None else None,
            "meanHoldingHours": _round(_number(model_filtered.get("meanHoldingHours")) - _number(baseline.get("meanHoldingHours"))) if baseline.get("meanHoldingHours") is not None and model_filtered.get("meanHoldingHours") is not None else None,
            "realizedPnlPerHoldingHour": _round(_number(model_filtered.get("realizedPnlPerHoldingHour")) - _number(baseline.get("realizedPnlPerHoldingHour"))) if baseline.get("realizedPnlPerHoldingHour") is not None and model_filtered.get("realizedPnlPerHoldingHour") is not None else None,
            "closedTradeCount": _integer(model_filtered.get("closedTradeCount")) - _integer(baseline.get("closedTradeCount")),
        },
        "assumptions": [
            "基线与模型直接计划路径使用同一历史窗口、同一仓位与杠杆规则，并共享同一方向不利的OHLC保守路径策略；回测不调用1m。",
            "模型路径直接从4h/1h/15m/5m历史K线生成方向、点位、分批比例和移动止损参数；仅保留基础执行安全校验，不回退经典策略。",
            "最大回撤基于每个执行时间片完成止盈、止损和收盘盯市后的权益快照，不把未保留的K线内部瞬时波动伪装成精确权益路径。",
        ],
    }


def _json_safe(value: Any) -> Any:
    """Convert runtime values to strict JSON, including non-finite floats."""

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _run_fixed_history_fill_job(job_id: str, network: str) -> None:
    try:
        _update_job(job_id, status="RUNNING", phase="MARKETS", message="正在读取全部可交易 USDT 永续合约。")
        universe = _get_fixed_history_market_universe(network, allow_partial_local=True)
        quotes = [dict(item) for item in (universe.get("quotes") or []) if isinstance(item, dict)]
        if not quotes:
            raise BinanceApiError("没有可用于补全的合约行情")
        history_status = _ensure_fixed_history(job_id, network, quotes)
        if history_status["rateLimited"]:
            _mark_history_rate_limited(job_id, history_status)
            return
        if history_status["ready"]:
            _update_job(
                job_id,
                status="COMPLETED",
                phase="COMPLETED",
                message="固定历史库已补全。",
                result=_history_preparation_result(history_status),
            )
            return
        remaining = history_status["missingRangeCount"]
        _update_job(
            job_id,
            status="HISTORY_INCOMPLETE",
            phase="HISTORY_INCOMPLETE",
            message=f"固定历史库尚有 {remaining} 个区间待补齐；本轮已保存成功数据。",
            history_cached=False,
            history_ready=False,
            history_missing_ranges=remaining,
            result=_history_preparation_result(history_status),
        )
    except BinanceApiError as exc:
        if _is_history_rate_limited(exc):
            _update_job(
                job_id,
                status="RATE_LIMITED",
                phase="RATE_LIMITED",
                message="交易所已限制请求，本次历史补全已中断。",
            )
            return
        _update_job(job_id, status="FAILED", phase="FAILED", message="历史补全失败。", error=str(exc) or "历史补全失败")
    except Exception as exc:
        _update_job(job_id, status="FAILED", phase="FAILED", message="历史补全失败。", error=str(exc) or "历史补全失败")


def _mark_history_rate_limited(job_id: str, history_status: dict[str, Any]) -> None:
    remaining = history_status["missingRangeCount"]
    _update_job(
        job_id,
        status="RATE_LIMITED",
        phase="RATE_LIMITED",
        message="交易所已限制历史请求，本次任务已中断；已保存数据可稍后继续补全。",
        history_cached=False,
        history_ready=False,
        history_missing_ranges=remaining,
        result=_history_preparation_result(history_status),
    )


def _ordered_history_intervals(intervals: tuple[str, ...] | list[str] | None = None) -> tuple[str, ...]:
    configured = FIXED_HISTORY_INTERVALS if intervals is None else intervals
    allowed = {
        str(interval or "").strip()
        for interval in configured
        if str(interval or "").strip() in HISTORY_FETCH_PRIORITY
    }
    return tuple(interval for interval in HISTORY_FETCH_PRIORITY if interval in allowed)


def _history_range_is_candle_aligned(intervals: tuple[str, ...]) -> bool:
    durations = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
    }
    return all(
        FIXED_HISTORY_START_TIME % durations[interval] == 0
        and FIXED_HISTORY_END_TIME % durations[interval] == 0
        for interval in intervals
        if interval in durations
    )


def _ensure_fixed_history(
    job_id: str,
    network: str,
    quotes: list[dict[str, Any]],
    required_intervals: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Fill the fixed corpus in priority stages, with concurrency per stage."""

    intervals = _ordered_history_intervals(required_intervals)
    initial_check = db.summarize_binance_futures_history_completeness(
        network,
        [str(quote.get("symbol") or "").upper() for quote in quotes],
        intervals,
        FIXED_HISTORY_START_TIME,
        FIXED_HISTORY_END_TIME,
    )
    missing_scopes = _history_missing_scopes(network, quotes, intervals)
    strict_data_gate = _history_range_is_candle_aligned(intervals)
    if not missing_scopes and (
        initial_check["completeSeries"] == initial_check["requiredSeries"]
        or not strict_data_gate
    ):
        _update_job(
            job_id,
            phase="HISTORY_CACHE",
            message="所需固定历史库已完整，直接从本地数据库读取。",
            completed=0,
            total=0,
            history_cached=True,
            history_ready=True,
            history_missing_ranges=0,
        )
        return {
            "ready": True,
            "universeCount": len(quotes),
            "missingRangeCount": 0,
            "failedScopes": [],
            "rateLimited": False,
            "rateLimitMessage": "",
            "intervals": list(intervals),
            "requiredIntervals": list(intervals),
            "completeness": initial_check,
        }

    estimated_pages = sum(
        _estimate_history_pages(range_start, range_end, scope["interval"])
        for scope in missing_scopes
        for range_start, range_end in scope["ranges"]
    )
    progress = {"completed": 0, "downloadedBars": 0}
    progress_lock = Lock()
    _update_job(
        job_id,
        phase="HISTORY_DOWNLOAD",
        message="正在按 4h → 1h → 15m → 5m 优先级补齐固定历史 K 线。",
        completed=0,
        total=max(1, estimated_pages),
        history_cached=False,
        history_ready=False,
        history_missing_ranges=len(missing_scopes),
    )

    def on_page(symbol: str, interval: str, bars: int) -> None:
        _invalidate_fixed_history_completeness(network)
        with progress_lock:
            progress["completed"] += 1
            progress["downloadedBars"] += max(0, bars)
            completed = progress["completed"]
            downloaded_bars = progress["downloadedBars"]
        _update_job(
            job_id,
            phase="HISTORY_DOWNLOAD",
            message=f"正在补齐 {interval} 固定历史 K 线。",
            current_symbol=f"{symbol} · {interval}",
            completed=completed,
            total=max(1, estimated_pages),
            history_ready=False,
            downloaded_bars=downloaded_bars,
        )

    failures: list[dict[str, str]] = []
    rate_limit_message = ""
    abort_event = Event()
    worker_count = max(1, int(HISTORY_DOWNLOAD_WORKERS))
    scopes_by_interval = {interval: [] for interval in intervals}
    for scope in missing_scopes:
        scopes_by_interval.setdefault(scope["interval"], []).append(scope)

    # Completing a higher-priority interval is a barrier before the next one
    # starts, so the strategy's required macro data becomes usable first.
    for interval in intervals:
        stage_scopes = scopes_by_interval.get(interval) or []
        if not stage_scopes or abort_event.is_set():
            continue
        scope_iterator = iter(stage_scopes)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="binance-backtest-history") as executor:
            pending: dict[Any, dict[str, Any]] = {}

            def submit_next_scope() -> bool:
                if abort_event.is_set():
                    return False
                try:
                    scope = next(scope_iterator)
                except StopIteration:
                    return False
                future = executor.submit(_download_history_scope, network, scope, on_page, abort_event)
                pending[future] = scope
                return True

            for _ in range(min(worker_count, len(stage_scopes))):
                submit_next_scope()

            while pending:
                completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed_futures:
                    scope = pending.pop(future)
                    if future.cancelled():
                        continue
                    try:
                        failure = future.result()
                    except CancelledError:
                        continue
                    except Exception as exc:
                        failure = {"kind": "FAILED", "message": str(exc) or "历史下载失败"}
                    if not failure:
                        continue
                    if failure.get("kind") == "RATE_LIMITED":
                        abort_event.set()
                        rate_limit_message = failure.get("message") or rate_limit_message
                        continue
                    failures.append({
                        "symbol": scope["symbol"],
                        "interval": scope["interval"],
                        "message": failure.get("message") or "历史下载失败",
                    })

                if abort_event.is_set():
                    for future in pending:
                        future.cancel()
                    continue
                while len(pending) < worker_count and submit_next_scope():
                    pass

    final_check = db.summarize_binance_futures_history_completeness(
        network,
        [str(quote.get("symbol") or "").upper() for quote in quotes],
        intervals,
        FIXED_HISTORY_START_TIME,
        FIXED_HISTORY_END_TIME,
    )
    remaining_scopes = _history_missing_scopes(network, quotes, intervals)
    ready = (
        not remaining_scopes
        and (
            final_check["completeSeries"] == final_check["requiredSeries"]
            or not strict_data_gate
        )
    )
    rate_limited = bool(rate_limit_message)
    _update_job(
        job_id,
        phase="RATE_LIMITED" if rate_limited else ("HISTORY_CACHE" if ready else "HISTORY_INCOMPLETE"),
        message=(
            "交易所已限制历史请求，本次下载已中断。"
            if rate_limited
            else ("所需固定历史库已完整。" if ready else "本轮历史下载结束，仍有不完整区间待下次补齐。")
        ),
        completed=max(1, estimated_pages) if ready else progress["completed"],
        total=max(1, estimated_pages),
        history_cached=ready,
        history_ready=ready,
        history_missing_ranges=len(remaining_scopes),
        downloaded_bars=progress["downloadedBars"],
    )
    return {
        "ready": ready,
        "universeCount": len(quotes),
        "missingRangeCount": len(remaining_scopes),
        "failedScopes": failures,
        "rateLimited": rate_limited,
        "rateLimitMessage": rate_limit_message,
        "intervals": list(intervals),
        "requiredIntervals": list(intervals),
        "completeness": final_check,
    }


def _history_missing_scopes(
    network: str,
    quotes: list[dict[str, Any]],
    intervals: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return coverage or failed-completeness gaps in download priority order."""

    ordered_intervals = _ordered_history_intervals(intervals)
    symbols: list[str] = []
    seen_symbols: set[str] = set()
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").upper()
        if symbol and symbol not in seen_symbols:
            seen_symbols.add(symbol)
            symbols.append(symbol)
    checks = db.get_binance_futures_history_completeness_checks(
        network,
        symbols,
        ordered_intervals,
        FIXED_HISTORY_START_TIME,
        FIXED_HISTORY_END_TIME,
    )
    scopes: list[dict[str, Any]] = []
    for interval in ordered_intervals:
        for symbol in symbols:
            check = checks.get((symbol, interval))
            if check and check.get("checked") and check.get("complete"):
                continue
            ranges = db.get_binance_futures_history_missing_ranges(
                network,
                symbol,
                interval,
                FIXED_HISTORY_START_TIME,
                FIXED_HISTORY_END_TIME,
            )
            if not ranges and _history_range_is_candle_aligned(ordered_intervals):
                # A coverage-complete but data-incomplete series must be
                # fetched again so malformed or missing candle rows can be
                # repaired; the next integrity check decides whether it is
                # actually complete.
                ranges = [(FIXED_HISTORY_START_TIME, FIXED_HISTORY_END_TIME)]
            if not ranges:
                continue
            scopes.append({"symbol": symbol, "interval": interval, "ranges": ranges})
    return scopes


def _wait_for_history_request_slot(abort_event: Event | None = None) -> bool:
    """Reserve one conservative Binance public-history request slot.

    Historical fills run concurrently by symbol, while Binance applies a
    shared weighted limit to the source IP.  Serializing request starts keeps
    the aggregate rate bounded without serializing parsing or SQLite writes.
    """

    global _history_request_next_at
    minimum_interval = max(0.0, float(HISTORY_REQUEST_MIN_INTERVAL_SECONDS))
    if minimum_interval <= 0:
        return abort_event is None or not abort_event.is_set()
    while True:
        if abort_event is not None and abort_event.is_set():
            return False
        with _history_request_pacer_lock:
            now = time.monotonic()
            wait_seconds = _history_request_next_at - now
            if wait_seconds <= 0:
                _history_request_next_at = now + minimum_interval
                return True
        # Check an aborted fill regularly instead of sleeping through its
        # whole pacing interval.
        time.sleep(min(wait_seconds, 0.25))


def _download_history_scope(network: str, scope: dict[str, Any], on_page, abort_event: Event) -> dict[str, str] | None:
    symbol = str(scope["symbol"])
    interval = str(scope["interval"])
    for range_start, range_end in scope["ranges"]:
        cursor = int(range_start)
        while cursor < int(range_end):
            if abort_event.is_set():
                return None
            try:
                if not _wait_for_history_request_slot(abort_event):
                    return None
                payload = get_futures_historical_klines(
                    network,
                    symbol,
                    interval,
                    cursor,
                    int(range_end) - 1,
                    limit=HISTORY_PAGE_LIMIT,
                    with_meta=True,
                )
            except BinanceApiError as exc:
                if _is_history_rate_limited(exc):
                    abort_event.set()
                    return {"kind": "RATE_LIMITED", "message": str(exc) or "交易所请求过于频繁"}
                return {"kind": "FAILED", "message": str(exc) or "历史下载失败"}
            except Exception as exc:
                return {"kind": "FAILED", "message": str(exc) or "历史下载失败"}
            raw_page = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(raw_page, list):
                return {"kind": "FAILED", "message": "交易所返回的历史 K 线页格式无效"}
            page = [
                item for item in raw_page
                if isinstance(item, dict) and cursor <= _integer(item.get("openTime")) < int(range_end)
            ]
            if not page:
                with _history_db_write_lock:
                    db.upsert_binance_futures_history_chunk(network, symbol, interval, [], cursor, int(range_end))
                on_page(symbol, interval, 0)
                break
            last_close = max((_integer(item.get("closeTime")) for item in page), default=0)
            covered_end = min(int(range_end), last_close + 1)
            if covered_end <= cursor:
                return {"kind": "FAILED", "message": "交易所返回了无法推进的历史 K 线页"}
            with _history_db_write_lock:
                db.upsert_binance_futures_history_chunk(network, symbol, interval, page, cursor, covered_end)
                if len(raw_page) < HISTORY_PAGE_LIMIT and covered_end < int(range_end):
                    # A short page means the exchange has no later candle in
                    # this requested range (for example, before a listing).
                    db.upsert_binance_futures_history_chunk(network, symbol, interval, [], covered_end, int(range_end))
            on_page(symbol, interval, len(page))
            if len(raw_page) < HISTORY_PAGE_LIMIT or covered_end >= int(range_end):
                break
            cursor = covered_end
    return None


def _is_history_rate_limited(error: Exception) -> bool:
    status_code = _integer(getattr(error, "status_code", 0))
    exchange_code = _integer(getattr(error, "exchange_code", 0))
    if status_code in {418, 429} or exchange_code == -1003:
        return True
    message = str(error or "").lower()
    return any(marker in message for marker in ("too many requests", "rate limit", "request weight"))


def _estimate_history_pages(start_time: int, end_time: int, interval: str) -> int:
    interval_ms = {
        "1m": 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
    }.get(interval, 60_000)
    bars = max(1, math.ceil((int(end_time) - int(start_time)) / interval_ms))
    return max(1, math.ceil(bars / HISTORY_PAGE_LIMIT))


def _offline_history_status(
    quotes: list[dict[str, Any]],
    *,
    required_intervals: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Describe an already proven local corpus without any database writes."""

    required = list(_ordered_history_intervals(required_intervals or MACRO_HISTORY_INTERVALS))
    return {
        "ready": True,
        "universeCount": len(quotes),
        "missingRangeCount": 0,
        "failedScopes": [],
        "rateLimited": False,
        "rateLimitMessage": "",
        "intervals": required,
        "requiredIntervals": required,
        "completeness": {
            "source": "LOCAL_HISTORY",
            "offlineOnly": True,
            "symbolCount": len(quotes),
        },
    }


def _history_preparation_result(status: dict[str, Any]) -> dict[str, Any]:
    intervals = list(status.get("intervals") or FIXED_HISTORY_INTERVALS)
    return {
        "historyReady": bool(status.get("ready")),
        "historyRange": {
            "startTime": FIXED_HISTORY_START_TIME,
            "endTime": FIXED_HISTORY_END_TIME,
            "intervals": intervals,
            "requiredIntervals": list(status.get("requiredIntervals") or intervals),
            "universeCount": status["universeCount"],
        },
        "missingRangeCount": status["missingRangeCount"],
        "failedScopes": status["failedScopes"][:20],
        "rateLimited": bool(status.get("rateLimited")),
        "rateLimitMessage": str(status.get("rateLimitMessage") or ""),
    }


def _load_macro_history(
    job_id: str,
    network: str,
    quotes: list[dict[str, Any]],
    *,
    required_intervals: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "loadedAt": _now_ms(),
        "startTime": FIXED_HISTORY_START_TIME,
        "endTime": FIXED_HISTORY_END_TIME,
        "symbols": [],
        "items": {},
        "stale": False,
    }
    symbols = [str(quote.get("symbol") or "").strip().upper() for quote in quotes]
    symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    quotes_by_symbol = {
        str(quote.get("symbol") or "").strip().upper(): dict(quote)
        for quote in quotes
        if str(quote.get("symbol") or "").strip()
    }
    frames_by_symbol: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
    incomplete_symbols: set[str] = set()
    completed = 0
    intervals = _ordered_history_intervals(required_intervals or MACRO_HISTORY_INTERVALS)
    interval_label = "、".join(intervals)
    _update_job(
        job_id,
        phase="HISTORY_MEMORY",
        message=f"正在从本地数据库批量装载 {interval_label} 回测K线。",
        completed=0,
        total=len(symbols) * len(intervals),
        history_cached=True,
    )

    def load_macro_chunk(chunk: list[str], interval: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
        # Compact inside the worker so completed futures never retain several
        # hundred thousand raw Python candle dictionaries.  This matters most
        # for the 15m month and keeps memory roughly proportional to replay's
        # compact arrays rather than to the temporary SQL result size.
        rows_by_symbol = db.list_binance_futures_history_klines_batch_rows(
            network,
            chunk,
            interval,
            FIXED_HISTORY_START_TIME,
            FIXED_HISTORY_END_TIME,
        )
        compacted: dict[str, dict[str, Any]] = {}
        incomplete: set[str] = set()
        for symbol in chunk:
            frame = _compact_frame_from_rows(rows_by_symbol.get(symbol) or [])
            if not frame or len(_frame_close_times(frame)) < HISTORY_WARMUP_BARS:
                incomplete.add(symbol)
            else:
                compacted[symbol] = frame
        return compacted, incomplete

    # Read one timeframe at a time.  Each bounded batch shares one SQLite
    # read connection, then is compacted before the next batch is retained.
    for interval in intervals:
        symbol_chunks = [
            symbols[offset:offset + HISTORY_MEMORY_SYMBOL_CHUNK]
            for offset in range(0, len(symbols), HISTORY_MEMORY_SYMBOL_CHUNK)
        ]
        worker_count = max(1, min(int(HISTORY_MEMORY_WORKERS), len(symbol_chunks)))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="binance-backtest-memory") as executor:
            futures = {
                executor.submit(load_macro_chunk, chunk, interval): chunk
                for chunk in symbol_chunks
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    compacted, incomplete = future.result()
                except Exception:
                    compacted, incomplete = {}, set(chunk)
                incomplete_symbols.update(incomplete)
                for symbol in chunk:
                    if symbol in compacted:
                        frames_by_symbol[symbol][interval] = compacted[symbol]
                    completed += 1
                _update_job(
                    job_id,
                    phase="HISTORY_MEMORY",
                    message=f"正在从本地数据库批量装载 {interval_label} 回测K线。",
                    current_symbol=f"{chunk[0]} · {interval}" if chunk else None,
                    completed=completed,
                    total=max(1, len(symbols) * len(intervals)),
                )

    for symbol in symbols:
        if symbol in incomplete_symbols or len(frames_by_symbol[symbol]) != len(intervals):
            continue
        data["symbols"].append(symbol)
        data["items"][symbol] = {
            "quote": quotes_by_symbol[symbol],
            "frames": frames_by_symbol[symbol],
            "stale": False,
        }
    if not data["symbols"]:
        raise BinanceApiError("没有拥有足够历史K线的合约")
    data["symbols"].sort(key=lambda symbol: _number(data["items"][symbol]["quote"].get("quoteVolume")), reverse=True)
    return data


def _load_micro_history(
    job_id: str,
    network: str,
    history: dict[str, Any],
    start_time: int,
    end_time: int,
) -> dict[str, dict[str, Any]]:
    """Load 5m history locally, filling only symbols that need it."""

    micro_start = max(FIXED_HISTORY_START_TIME, start_time - 6 * 60 * 60 * 1000)
    result: dict[str, dict[str, Any]] = {}
    symbols = list(history["symbols"])
    _update_job(
        job_id,
        phase="MICRO_HISTORY",
        message="正在从本地数据库装载本周所需的 5m 入场确认K线。",
        completed=0,
        total=len(symbols),
    )

    def load_symbol(symbol: str) -> tuple[str, dict[str, Any], bool]:
        items = db.list_binance_futures_history_klines(network, symbol, "5m", micro_start, end_time)
        bars = _completed_bars(items)
        if not _history_window_is_usable(bars, micro_start, end_time, 5 * 60_000):
            ranges = db.get_binance_futures_history_missing_ranges(
                network,
                symbol,
                "5m",
                micro_start,
                end_time,
            )
            if not ranges:
                # Coverage may have been recorded before a malformed or
                # partial response was discovered. Re-read the requested
                # window once to repair the persisted rows.
                ranges = [(micro_start, end_time)]
            failure = _download_history_scope(
                network,
                {"symbol": symbol, "interval": "5m", "ranges": ranges},
                lambda *_args: None,
                Event(),
            )
            if failure:
                raise BinanceApiError(failure.get("message") or f"{symbol} 的5m历史K线获取失败")
            items = db.list_binance_futures_history_klines(network, symbol, "5m", micro_start, end_time)
            bars = _completed_bars(items)
        if not _history_window_is_usable(bars, micro_start, end_time, 5 * 60_000):
            raise BinanceApiError(f"{symbol} 的5m入场K线不足")
        return symbol, _compact_frame(bars), False

    def load_micro_chunk(chunk: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        rows_by_symbol = db.list_binance_futures_history_klines_batch_rows(
            network,
            chunk,
            "5m",
            micro_start,
            end_time,
        )
        compacted: dict[str, dict[str, Any]] = {}
        incomplete: list[str] = []
        for symbol in chunk:
            frame = _compact_frame_from_rows(rows_by_symbol.get(symbol) or [])
            if frame and _history_frame_window_is_usable(frame, micro_start, end_time, 5 * 60_000):
                compacted[symbol] = {"frame": frame, "stale": False}
            else:
                incomplete.append(symbol)
        return compacted, incomplete

    # The initial local read is batched as well.  Symbols that are incomplete
    # are then handled individually because only they may need a REST fill.
    pending_symbols: list[str] = []
    completed = 0
    symbol_chunks = [
        symbols[offset:offset + HISTORY_MEMORY_SYMBOL_CHUNK]
        for offset in range(0, len(symbols), HISTORY_MEMORY_SYMBOL_CHUNK)
    ]
    read_worker_count = max(1, min(int(HISTORY_MEMORY_WORKERS), len(symbol_chunks)))
    with ThreadPoolExecutor(max_workers=read_worker_count, thread_name_prefix="binance-backtest-micro-read") as executor:
        futures = {
            executor.submit(load_micro_chunk, chunk): chunk
            for chunk in symbol_chunks
        }
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                compacted, incomplete = future.result()
            except Exception:
                compacted, incomplete = {}, list(chunk)
            for symbol in chunk:
                if symbol in compacted:
                    result[symbol] = compacted[symbol]
                    completed += 1
                elif symbol in incomplete:
                    pending_symbols.append(symbol)
            _update_job(
                job_id,
                phase="MICRO_HISTORY",
                message="正在从本地数据库批量装载本周所需的 5m 入场确认K线。",
                current_symbol=f"{chunk[0]} · 5m" if chunk else None,
                completed=completed,
                total=len(symbols),
            )

    worker_count = max(1, min(int(HISTORY_DOWNLOAD_WORKERS), len(pending_symbols)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="binance-backtest-micro") as executor:
        futures = {executor.submit(load_symbol, symbol): symbol for symbol in pending_symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                loaded_symbol, frame, stale = future.result()
            except BinanceApiError as exc:
                if _is_history_rate_limited(exc):
                    raise
                _update_job(
                    job_id,
                    phase="MICRO_HISTORY",
                    message="部分合约缺少本地 5m 入场K线，回测时将跳过。",
                    current_symbol=symbol,
                    completed=completed,
                    total=len(symbols),
                )
                continue
            except Exception:
                _update_job(
                    job_id,
                    phase="MICRO_HISTORY",
                    message="部分合约缺少本地 5m 入场K线，回测时将跳过。",
                    current_symbol=symbol,
                    completed=completed,
                    total=len(symbols),
                )
                continue
            result[loaded_symbol] = {"frame": frame, "stale": stale}
            completed += 1
            _update_job(
                job_id,
                phase="MICRO_HISTORY",
                message="正在从本地数据库批量装载本周所需的 5m 入场确认K线。",
                current_symbol=loaded_symbol,
                completed=completed,
                total=len(symbols),
            )
    return result


def _load_replay_micro_symbol(
    network: str,
    symbol: str,
    start_time: int,
    end_time: int,
) -> dict[str, Any] | None:
    """Load one symbol's 5m replay window on demand.

    Macro frames are enough to decide whether a plan is complete.  This
    helper is intentionally called only for a macro match, so the normal
    replay path does not read or download a month of 5m candles for every
    contract.  A non-rate-limit failure simply leaves the plan on its primary
    trigger; a rate-limit response remains terminal for the replay job.
    """

    safe_symbol = str(symbol or "").strip().upper()
    if not safe_symbol:
        return None
    micro_start = max(FIXED_HISTORY_START_TIME, int(start_time) - 6 * 60 * 60 * 1000)
    interval = "5m"
    try:
        items = db.list_binance_futures_history_klines(network, safe_symbol, interval, micro_start, int(end_time))
        bars = _completed_bars(items)
        if not _history_window_is_usable(bars, micro_start, int(end_time), 5 * 60_000):
            ranges = db.get_binance_futures_history_missing_ranges(
                network,
                safe_symbol,
                interval,
                micro_start,
                int(end_time),
            )
            if not ranges:
                ranges = [(micro_start, int(end_time))]
            failure = _download_history_scope(
                network,
                {"symbol": safe_symbol, "interval": interval, "ranges": ranges},
                lambda *_args: None,
                Event(),
            )
            if failure:
                if failure.get("kind") == "RATE_LIMITED":
                    raise BinanceApiError(failure.get("message") or "交易所请求过于频繁")
                return None
            items = db.list_binance_futures_history_klines(network, safe_symbol, interval, micro_start, int(end_time))
            bars = _completed_bars(items)
        if not _history_window_is_usable(bars, micro_start, int(end_time), 5 * 60_000):
            return None
        return {"frame": _compact_frame(bars), "stale": False}
    except BinanceApiError as exc:
        # Optional 5m timing must not turn a fully local 15m replay into a
        # network-dependent failure. Preserve the explicit rate-limit stop so
        # a history fill can be retried later, but otherwise keep the macro
        # plan and its primary trigger.
        if _is_history_rate_limited(exc):
            raise
        return None
    except Exception:
        return None


def _history_window_is_usable(
    bars: list[dict[str, float]],
    start_time: int,
    end_time: int,
    interval_ms: int,
) -> bool:
    """Check that a local micro window has enough contiguous completed bars."""

    if len(bars) < HISTORY_WARMUP_BARS:
        return False
    ordered = sorted(bars, key=lambda item: int(item["openTime"]))
    first_open = int(ordered[0]["openTime"])
    last_close = int(ordered[-1]["closeTime"])
    if first_open > int(start_time) + interval_ms:
        return False
    if last_close < int(end_time) - interval_ms:
        return False
    previous_open = first_open
    for item in ordered[1:]:
        current_open = int(item["openTime"])
        if current_open - previous_open != interval_ms:
            return False
        previous_open = current_open
    return True


def _compact_frame_from_rows(rows: list[tuple]) -> dict[str, Any] | None:
    """Compact SQL tuples directly, avoiding an intermediate dict per candle."""

    now = int(time.time() * 1000)
    open_times = array("q")
    close_times = array("q")
    opens = array("d")
    highs = array("d")
    lows = array("d")
    closes = array("d")
    volumes = array("d")
    for row in rows:
        try:
            open_time = int(row[0])
            close_time = int(row[1])
            open_price = float(row[2])
            high_price = float(row[3])
            low_price = float(row[4])
            close_price = float(row[5])
            volume = float(row[6])
        except (IndexError, TypeError, ValueError):
            continue
        if close_time >= now or not (high_price >= low_price > 0 and close_price > 0):
            continue
        open_times.append(open_time)
        close_times.append(close_time)
        opens.append(open_price)
        highs.append(high_price)
        lows.append(low_price)
        closes.append(close_price)
        volumes.append(volume)
    if not close_times:
        return None
    return {
        "compact": True,
        "openTimes": open_times,
        "closeTimes": close_times,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
    }


def _history_frame_window_is_usable(
    frame: dict[str, Any],
    start_time: int,
    end_time: int,
    interval_ms: int,
) -> bool:
    """Check a compact frame without expanding every candle to a dict."""

    open_times = frame.get("openTimes") or []
    close_times = frame.get("closeTimes") or []
    if len(close_times) < HISTORY_WARMUP_BARS or len(open_times) != len(close_times):
        return False
    if int(open_times[0]) > int(start_time) + interval_ms:
        return False
    if int(close_times[-1]) < int(end_time) - interval_ms:
        return False
    return all(
        int(right) - int(left) == interval_ms
        for left, right in zip(open_times, open_times[1:])
    )


def _resolve_backtest_window(
    history: dict[str, Any],
    options: dict[str, Any],
    *,
    direct_model: bool = False,
) -> tuple[int, int]:
    normalized = _normalize_backtest_options(options)
    if normalized["mode"] == BACKTEST_MODE_RANGE:
        return int(normalized["startTime"]), int(normalized["endTime"])
    days = int(normalized["days"])
    interval_ms = SHORT_TERM_INTERVAL_MS if direct_model or normalized["strategyMode"] == STRATEGY_MODE_SHORT_TERM else BACKTEST_INTERVAL_MS
    start_time = _select_random_start_time(history, days, interval_ms=interval_ms)
    return start_time, start_time + days * BACKTEST_DAY_MS


def _select_random_start_time(
    history: dict[str, Any],
    days: int = BACKTEST_DAYS,
    *,
    interval_ms: int = BACKTEST_INTERVAL_MS,
) -> int:
    reference_symbol = "BTCUSDT" if "BTCUSDT" in history["items"] else history["symbols"][0]
    timeframe = "5m" if int(interval_ms) == SHORT_TERM_INTERVAL_MS else "15m"
    reference = history["items"][reference_symbol]["frames"].get(timeframe)
    if reference is None:
        raise BinanceApiError(f"基准{timeframe}历史K线为空")
    open_times = reference.get("openTimes")
    if not open_times:
        open_times = [int(_frame_bar(reference, index)["openTime"]) for index in range(len(_frame_close_times(reference)))]
    if not open_times:
        raise BinanceApiError(f"基准{timeframe}历史K线为空")
    duration = int(days) * BACKTEST_DAY_MS
    # The macro warm-up remains the limiting discipline boundary.  The
    # execution frame only changes the replay cadence, not how much context
    # must precede the first candidate.
    first_allowed = max(int(history["startTime"]) + HISTORY_WARMUP_BARS * 4 * 60 * 60 * 1000, EARLIEST_BACKTEST_START_TIME)
    last_allowed = int(history["endTime"]) - duration
    candidates = [
        int(open_time)
        for open_time in open_times
        if first_allowed <= int(open_time) <= last_allowed
    ]
    if not candidates:
        raise BinanceApiError(f"固定历史数据不足以随机选择完整的 {days} 天回测区间")
    return random.SystemRandom().choice(candidates)


def _run_replay(
    job_id: str,
    network: str,
    history: dict[str, Any],
    micro_history: dict[str, dict[str, Any]],
    start_time: int,
    end_time: int,
    *,
    strategy_settings: object = None,
    execution_options: object = None,
    model_selection: str = MODEL_SELECTION_OFF,
    model_branch: str = "BEST",
    model_run_id: str | None = None,
    model_evaluation: dict[str, Any] | None = None,
    minute_cache: dict[tuple[str, int], list[dict[str, float]]] | None = None,
    publish_progress: bool = True,
    replay_label: str = "当前策略",
) -> dict[str, Any]:
    provided_execution_mode = (
        execution_options.get("strategyMode")
        if isinstance(execution_options, dict)
        else None
    )
    if provided_execution_mode is not None:
        provided_mode_text = str(provided_execution_mode or "").strip().upper()
        if provided_mode_text not in {STRATEGY_MODE_MIDLINE, STRATEGY_MODE_SHORT_TERM}:
            raise ValueError("回放参数中的策略模式无效，应为 MIDLINE 或 SHORT_TERM")
    # The replay helper is also used directly by tests and optimization
    # callers.  When they provide only the run options, carry that selected
    # route into the same strategy snapshot used by live analysis instead of
    # silently falling back to the midline 15m cadence.
    raw_strategy_settings = strategy_settings
    if raw_strategy_settings is None and provided_execution_mode is not None:
        raw_strategy_settings = {"strategyMode": provided_execution_mode}
    strategy_settings = _strategy_settings(raw_strategy_settings)
    strategy_mode = _strategy_mode(strategy_settings)
    if provided_execution_mode is not None and _strategy_mode(provided_execution_mode) != strategy_mode:
        raise ValueError("回放参数中的策略模式必须与策略设置快照一致")
    normalized_execution_options = (
        {**execution_options, "strategyMode": strategy_mode, "strategyEngine": _strategy_engine(strategy_settings)}
        if isinstance(execution_options, dict)
        else {"strategyMode": strategy_mode, "strategyEngine": _strategy_engine(strategy_settings)}
    )
    execution_settings = _execution_settings(normalized_execution_options)
    model_selection = str(model_selection or MODEL_SELECTION_OFF).upper()
    if model_selection not in {MODEL_SELECTION_OFF, MODEL_SELECTION_FILTER}:
        raise ValueError("回放中的模型筛选模式无效")
    direct_model_mode = model_selection == MODEL_SELECTION_FILTER and _strategy_engine(strategy_settings) == "MODEL"
    # Direct MODEL replays on its selected checkpoint's frozen holding scale;
    # 5m is only an input/timing stream for SWING/POSITION. CLASSIC retains
    # its existing mode-specific cadence.
    direct_interval = get_binance_ml_execution_interval(
        network, run_id=str(model_run_id or "").strip() or _model_run_id(strategy_settings), branch=model_branch,
        fallback=DIRECT_MODEL_EXECUTION_INTERVAL,
    ) if direct_model_mode else None
    timeframe = direct_interval if direct_model_mode else ("5m" if strategy_mode == STRATEGY_MODE_SHORT_TERM else "15m")
    reference_symbol = "BTCUSDT" if "BTCUSDT" in history["items"] else history["symbols"][0]
    reference_frame = history["items"][reference_symbol]["frames"].get(timeframe)
    if reference_frame is None:
        raise BinanceApiError(f"基准{timeframe}历史K线为空")
    # The end boundary is exclusive so a random window is not one bar longer.
    timeline_bars = _frame_bars_between(reference_frame, start_time, end_time)
    if not timeline_bars:
        raise BinanceApiError(f"回测区间没有可用{timeframe} K线")
    portfolio = {
        "cash": execution_settings["initialBalance"],
        "initialBalance": execution_settings["initialBalance"],
        "positions": [],
        "tradeCount": 0,
        "openedCount": 0,
        "stoppedCount": 0,
        "protectiveTargetCount": 0,
        "firstTargetCount": 0,
        "secondTargetCount": 0,
        "oneMinuteChecks": 0,
        "oneMinuteFallbacks": 0,
        "conservativeIntrabarPaths": 0,
        "pending": {},
        "pendingExpiredCount": 0,
        "pendingConfirmedCount": 0,
        "entryBlockedByRiskCount": 0,
        "entryFailureExitCount": 0,
        "dailyRiskState": None,
        "dailyRealizedPnl": 0.0,
        "_currentEntryScan": None,
        "_executionSettings": execution_settings,
        "_strategySettings": deepcopy(strategy_settings),
        "_strategyMode": strategy_mode,
        "_timeframe": timeframe,
        "_modelExecutionInterval": direct_interval,
        "_modelSelection": model_selection,
        "_modelBranch": _model_branch({"modelBranch": model_branch}),
        "_modelRunId": str(model_run_id or "").strip() or _model_run_id(strategy_settings),
        "_modelEvaluation": deepcopy(model_evaluation) if isinstance(model_evaluation, dict) else None,
    }
    minute_cache = minute_cache if isinstance(minute_cache, dict) else {}
    timeline: list[dict[str, Any]] = []
    micro_loader = lambda symbol: _load_replay_micro_symbol(network, symbol, start_time, end_time)
    if publish_progress:
        _update_job(
            job_id,
            phase="REPLAY",
            message=f"正在按{timeframe}时间片重放{replay_label}。",
            completed=0,
            total=len(timeline_bars),
        )

    for index, bar in enumerate(timeline_bars):
        slice_open_time = int(bar["openTime"])
        close_time = int(bar["closeTime"])
        actions: list[dict[str, Any]] = []
        _roll_daily_risk_state(portfolio, slice_open_time)
        portfolio["_currentEntryScan"] = {
            "asOf": slice_open_time - 1,
            "status": "SKIPPED",
            "reason": "等待本时间片的入场扫描。",
        }
        # Reserve this slice's available management slots before any intrabar
        # stop or target action runs. A position that stops during this same
        # candle cannot free a slot for another fresh entry in the candle.
        entry_slots = max(
            0,
            execution_settings["maxManagedPositions"]
            - _managed_position_count(portfolio["positions"]),
        )
        symbols_held_at_open = {str(position.get("symbol") or "").upper() for position in portfolio["positions"]}
        active_by_symbol = {position["symbol"]: position for position in portfolio["positions"]}
        for symbol, position in list(active_by_symbol.items()):
            dataset = history["items"].get(symbol)
            if not dataset:
                continue
            symbol_bar = _bar_at_close(dataset["frames"][timeframe], close_time)
            if symbol_bar is None:
                continue
            context = _management_context(
                dataset,
                close_time - 1,
                position,
                strategy_settings=_position_strategy_settings(position),
                execution_interval=timeframe,
            )
            _refresh_management(position, context, position.get("currentPrice") or position["entryPrice"], actions)
            path_bars = [symbol_bar]
            if _requires_one_minute_resolution(position, symbol_bar, context):
                # 1m ordering is intentionally disabled for replay.  Leaving
                # ``path_bars`` as the source candle makes _bar_path choose
                # the direction-adverse OHLC route without network I/O.
                portfolio["conservativeIntrabarPaths"] = portfolio.get("conservativeIntrabarPaths", 0) + 1
            if not _process_position_bars(position, path_bars, context, portfolio, actions):
                portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]
            elif _strategy_engine(_position_strategy_settings(position)) != "MODEL" and not _process_entry_failure(position, symbol_bar, context, portfolio, actions):
                portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]

        # A condition plan is calculated with data available just before this
        # candle opens, then may trigger only inside this exact execution candle.
        # It is deliberately not stored for a later slice: carrying it forward
        # turns a missed setup into a stale entry and introduces look-ahead.
        if entry_slots:
            candidates = _scan_for_entries(
                history,
                micro_history,
                portfolio,
                slice_open_time - 1,
                actions,
                network=network,
                excluded_symbols=symbols_held_at_open,
                micro_loader=micro_loader,
                strategy_settings=strategy_settings,
                execution_interval=timeframe,
                model_selection=model_selection,
                model_branch=_model_branch({"modelBranch": model_branch}),
                model_run_id=portfolio.get("_modelRunId"),
            )
        else:
            candidates = []
            portfolio["_currentEntryScan"] = {
                "asOf": slice_open_time - 1,
                "status": "SKIPPED",
                "reason": "已达到管理中仓位上限，本时间片不再扫描入场。",
                "rankedUniverseCount": 0,
                "evaluatedCount": 0,
                "armedCount": 0,
                "filterSummary": {},
                "filteredSamples": [],
                "candidatePlans": [],
            }
        if strategy_settings["entryConfirmationMode"] == ENTRY_CONFIRMATION_RETEST:
            _queue_pending_entries(portfolio, candidates, interval_ms=SHORT_TERM_INTERVAL_MS if timeframe == "5m" else BACKTEST_INTERVAL_MS)
            _process_pending_entries(
                network,
                history,
                micro_history,
                portfolio,
                bar,
                close_time,
                minute_cache,
                actions,
                entry_slots=entry_slots,
                micro_loader=micro_loader,
                strategy_settings=strategy_settings,
                interval=timeframe,
            )
        else:
            _process_slice_entries(
                network,
                history,
                portfolio,
                candidates,
                bar,
                entry_slots,
                minute_cache,
                actions,
                strategy_settings=strategy_settings,
                interval=timeframe,
            )
        _mark_positions_to_close(history, portfolio["positions"], close_time, interval=timeframe)
        timeline.append(_timeline_record(close_time, actions, portfolio))
        # Publish the completed slice immediately.  The row is a snapshot and
        # the copied list prevents a later append from changing an already
        # published task result while the frontend is reading it.
        if publish_progress:
            _update_job(
                job_id,
                phase="REPLAY",
                message=f"正在按{timeframe}时间片重放{replay_label}。",
                current_symbol=None,
                completed=index + 1,
                total=len(timeline_bars),
                replay_result=_replay_result_snapshot(
                    timeline,
                    portfolio,
                    start_time,
                    end_time,
                    index + 1,
                    len(timeline_bars),
                    as_of_time=close_time,
                    market_count=len(history["symbols"]),
                    partial=True,
                    timeframe=timeframe,
                ),
            )

    return _replay_result_snapshot(
        timeline,
        portfolio,
        start_time,
        end_time,
        len(timeline_bars),
        len(timeline_bars),
        as_of_time=int(timeline[-1]["time"]) if timeline else None,
        market_count=len(history["symbols"]),
        partial=False,
        timeframe=timeframe,
    )


def _process_slice_entries(
    network: str,
    history: dict[str, Any],
    portfolio: dict[str, Any],
    candidates: list[dict[str, Any]],
    reference_bar: dict[str, float],
    entry_slots: int,
    minute_cache: dict[tuple[str, int], list[dict[str, float]]],
    actions: list[dict[str, Any]],
    *,
    strategy_settings: object = None,
    interval: str = "15m",
) -> None:
    """Open only candidates generated for this exact execution slice.

    ``entry_slots`` is captured at the slice open by ``_run_replay``.  It is
    intentionally not recomputed after a same-candle stop, which avoids a
    stop → replacement-entry loop inside one historical bar.
    """

    opened_this_slice = 0
    reserved_symbols = {str(position.get("symbol") or "").upper() for position in portfolio["positions"]}
    reference_close_time = int(reference_bar["closeTime"])
    for candidate in candidates:
        if opened_this_slice >= entry_slots:
            break
        symbol = str(candidate.get("symbol") or "").upper()
        dataset = history["items"].get(symbol)
        if not dataset or not symbol or symbol in reserved_symbols:
            continue
        symbol_bar = _bar_at_close(dataset["frames"][interval], reference_close_time)
        if symbol_bar is None or not _entry_triggered(candidate, symbol_bar):
            continue
        candidate_settings = _strategy_settings(candidate.get("strategySettings") or strategy_settings)
        context = _management_context(
            dataset,
            int(symbol_bar["openTime"]) - 1,
            # The candidate has no entry price yet, but its side is required
            # for direction-sensitive continuation and stop calculations.
            candidate,
            strategy_settings=candidate_settings,
            execution_interval=interval,
        )
        path_bars = [symbol_bar]
        if _entry_requires_one_minute_resolution(candidate, symbol_bar, context):
            # The source candle remains in place so _entry_path uses the
            # direction-adverse OHLC order; do not call the 1m endpoint.
            portfolio["conservativeIntrabarPaths"] = portfolio.get("conservativeIntrabarPaths", 0) + 1
        entry_path = _entry_path(candidate, path_bars)
        if not entry_path:
            continue
        if _managed_position_count(portfolio["positions"]) >= _portfolio_execution_settings(portfolio)["maxManagedPositions"]:
            continue
        position = _open_position(
            candidate,
            float(entry_path[0]["open"]),
            portfolio,
            strategy_settings=candidate_settings,
        )
        if position is None:
            _record_entry_block(portfolio, actions, candidate)
            continue
        opened_this_slice += 1
        portfolio["positions"].append(position)
        reserved_symbols.add(symbol)
        portfolio["openedCount"] += 1
        actions.append(
            _action(
                "OPEN",
                symbol,
                position["side"],
                price=position["entryPrice"],
                quantity=position["quantity"],
                note=(
                    f"当前{interval}内触发开仓；按总权益"
                    f"{_portfolio_execution_settings(portfolio)['positionMarginFraction'] * 100:g}%"
                    f"保证金开仓，{position['leverage']}x杠杆，实际结构止损风险不高于账户风险预算。"
                ),
            )
        )
        if not _process_position_bars(position, entry_path, context, portfolio, actions):
            portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]
        elif _strategy_engine(candidate_settings) != "MODEL" and not _process_entry_failure(
            position,
            _entry_failure_evaluation_bar(entry_path, symbol_bar),
            context,
            portfolio,
            actions,
        ):
                portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]


def replay_discipline_candidate_label(
    history: dict[str, Any],
    candidate: dict[str, Any],
    as_of: int,
    horizon_bars: int,
    *,
    strategy_settings: object = None,
    interval: str = "15m",
) -> dict[str, Any]:
    """Replay one discipline candidate independently with the live state machine.

    The candidate is allowed to fill only in the next execution candle.  The
    subsequent path reuses the same partial-target, early-failure, structure
    and ATR trailing logic as the portfolio replay.  Historical 1m data is not
    persisted, so ambiguous candles deliberately use the replay's adverse
    OHLC order instead of performing network I/O during model training.
    """

    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "").upper()
    dataset = (history.get("items") or {}).get(symbol)
    if not isinstance(dataset, dict) or side not in {"LONG", "SHORT"}:
        return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0}
    frame = (dataset.get("frames") or {}).get(interval)
    if not isinstance(frame, dict):
        return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0}
    interval_ms = SHORT_TERM_INTERVAL_MS if interval == "5m" else BACKTEST_INTERVAL_MS
    start_time = int(as_of) + 1
    end_time = start_time + max(1, int(horizon_bars)) * interval_ms
    future_bars = _frame_bars_between(frame, start_time, end_time)
    if not future_bars:
        return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0}

    first_bar = future_bars[0]
    if not _entry_triggered(candidate, first_bar):
        return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0}
    entry_path = _entry_path(candidate, [first_bar])
    if not entry_path:
        return {
            "outcome": "EXPIRE",
            "entered": False,
            "invalidatedBeforeEntry": True,
            "realizedR": 0.0,
            "durationBars": 0,
        }

    settings = _strategy_settings(strategy_settings or candidate.get("strategySettings"))
    execution_settings = {
        "strategyMode": _strategy_mode(settings),
        "initialBalance": 1_000.0,
        "positionMarginFraction": 1.0,
        "maxManagedPositions": 1,
        "scanLimit": 100,
        "maxLeverage": 1,
        "leverageMode": LEVERAGE_MODE_FIXED,
        "leverage": 1,
    }
    portfolio = {
        "cash": 1_000.0,
        "initialBalance": 1_000.0,
        "positions": [],
        "tradeCount": 0,
        "openedCount": 0,
        "stoppedCount": 0,
        "protectiveTargetCount": 0,
        "firstTargetCount": 0,
        "secondTargetCount": 0,
        "entryFailureExitCount": 0,
        "dailyRealizedPnl": 0.0,
        "dailyRiskState": {"day": "MODEL", "startEquity": 1_000.0},
        "_executionSettings": execution_settings,
        "_strategySettings": settings,
    }
    position = _open_position(
        candidate,
        float(entry_path[0]["open"]),
        portfolio,
        strategy_settings=settings,
    )
    if position is None:
        return {"outcome": "EXPIRE", "entered": False, "realizedR": 0.0, "durationBars": 0}
    portfolio["positions"].append(position)
    initial_risk_value = max(EPSILON, float(position["risk"]) * float(position["initialQuantity"]))
    entry_price = float(position["entryPrice"])
    is_long = side == "LONG"
    mfe_r = 0.0
    mae_r = 0.0
    max_drawdown_r = 0.0
    first_target_duration: int | None = None
    second_target_duration: int | None = None
    stop_duration: int | None = None
    last_price = entry_price

    for offset, bar in enumerate(future_bars, start=1):
        context = _management_context(
            dataset,
            int(bar["openTime"]) - 1,
            position,
            strategy_settings=settings,
            execution_interval=interval,
        )
        path_bars = entry_path if offset == 1 else [bar]
        for observed in path_bars:
            high = float(observed["high"])
            low = float(observed["low"])
            mfe_r = max(mfe_r, (high - entry_price) / position["risk"] if is_long else (entry_price - low) / position["risk"])
            mae_r = max(mae_r, (entry_price - low) / position["risk"] if is_long else (high - entry_price) / position["risk"])
        actions: list[dict[str, Any]] = []
        _refresh_management(position, context, position.get("currentPrice") or entry_price, actions)
        alive = _process_position_bars(position, path_bars, context, portfolio, actions)
        if alive and _strategy_engine(settings) != "MODEL":
            alive = _process_entry_failure(
                position,
                _entry_failure_evaluation_bar(path_bars, bar),
                context,
                portfolio,
                actions,
            )
        if first_target_duration is None and bool(position.get("firstTargetReached")):
            first_target_duration = offset
        if second_target_duration is None and bool(position.get("secondTargetReached")):
            second_target_duration = offset
        if not alive or not _position_alive(position):
            stop_duration = offset
            last_price = float(actions[-1].get("price") or position.get("currentPrice") or entry_price) if actions else float(position.get("currentPrice") or entry_price)
            break
        last_price = float(bar["close"])
        position["currentPrice"] = last_price
        unrealized = (
            (last_price - entry_price) * position["quantity"]
            if is_long
            else (entry_price - last_price) * position["quantity"]
        )
        equity_r = (float(position["realizedPnl"]) + unrealized) / initial_risk_value
        max_drawdown_r = max(max_drawdown_r, -equity_r)

    alive = _position_alive(position)
    unrealized = 0.0
    if alive:
        unrealized = (
            (last_price - entry_price) * position["quantity"]
            if is_long
            else (entry_price - last_price) * position["quantity"]
        )
    realized_r = (float(position["realizedPnl"]) + unrealized) / initial_risk_value
    if first_target_duration is not None:
        outcome = "TARGET"
        duration = first_target_duration
    elif not alive:
        outcome = "STOP"
        duration = stop_duration or 1
    else:
        outcome = "EXPIRE"
        duration = len(future_bars)
    time_efficiency = realized_r / math.sqrt(max(duration, 1))
    return {
        "outcome": outcome,
        "entered": True,
        "invalidatedBeforeEntry": False,
        "realizedR": float(max(-4.0, min(8.0, realized_r))),
        "durationBars": int(duration),
        "mfeR": float(max(0.0, min(8.0, mfe_r))),
        "maeR": float(max(0.0, min(8.0, mae_r))),
        "maxDrawdownR": float(max(0.0, min(8.0, max(max_drawdown_r, mae_r)))),
        "stopSource": position.get("activeStopSource"),
        "firstTargetReached": first_target_duration is not None,
        "secondTargetReached": bool(position.get("secondTargetReached")),
        "protectiveTargetReached": bool(position.get("protectiveTargetReached")),
        "tp1Bars": first_target_duration,
        "tp2Bars": second_target_duration,
        "exitBars": duration,
        "timeEfficiency": float(time_efficiency),
        "holdingCost": float(-0.001 * duration),
    }


def _scan_for_entries(
    history: dict[str, Any],
    micro_history: dict[str, dict[str, Any]],
    portfolio: dict[str, Any],
    as_of: int,
    actions: list[dict[str, Any]],
    *,
    network: str = "mainnet",
    excluded_symbols: set[str] | None = None,
    micro_loader=None,
    strategy_settings: object = None,
    execution_interval: str = "15m",
    model_selection: str = MODEL_SELECTION_OFF,
    model_branch: str = "BEST",
    model_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find candidates from the preceding completed bar's top-100 universe.

    The live scanner ranks contracts by turnover before it evaluates their
    plan. A historical replay cannot know a candle's final turnover at that
    candle's open, so it ranks by the previous completed execution candle.
    That preserves the live scan's liquidity gate without using future data.
    """

    existing_symbols = {position["symbol"] for position in portfolio["positions"]}
    existing_symbols.update(str(symbol or "").upper() for symbol in (excluded_symbols or set()))
    model_selection = str(model_selection or MODEL_SELECTION_OFF).upper()
    if model_selection not in {MODEL_SELECTION_OFF, MODEL_SELECTION_FILTER}:
        raise ValueError("入场扫描中的模型筛选模式无效")
    mode = _strategy_mode(strategy_settings)
    direct_model_mode = model_selection == MODEL_SELECTION_FILTER and _strategy_engine(strategy_settings) == "MODEL"
    execution_interval = (
        str(portfolio.get("_modelExecutionInterval") or get_binance_ml_execution_interval(
            network, run_id=model_run_id, branch=model_branch, fallback=DIRECT_MODEL_EXECUTION_INTERVAL,
        ))
        if direct_model_mode
        else ("5m" if mode == STRATEGY_MODE_SHORT_TERM else "15m")
    )
    # The top-100 boundary is defined before removing already-held symbols;
    # otherwise a held high-volume contract would incorrectly pull the 101st
    # symbol into the analysis universe.
    scan_limit = _portfolio_execution_settings(portfolio)["scanLimit"]
    ranked_symbols = _slice_top_turnover_symbols(
        history,
        as_of,
        None,
        scan_limit,
        interval=execution_interval,
    )
    audit: dict[str, Any] = {
        "asOf": as_of,
        "status": "COMPLETED",
        "rankedUniverseCount": len(ranked_symbols),
        "evaluatedCount": 0,
        "armedCount": 0,
        "trialCount": 0,
        "eligibleCount": 0,
        "disciplineEligibleCount": 0,
        "skippedHeldCount": 0,
        "filterSummary": {},
        "filteredSamples": [],
        "candidatePlans": [],
        "model": {
            "mode": model_selection,
            "evaluatedCount": 0,
            "acceptedCount": 0,
            "rejectedCount": 0,
            "rejectedByReason": {},
        },
    }
    filter_summary: dict[str, int] = {}

    def record_filter(symbol: str, reason: str, plan: dict[str, Any] | None = None) -> None:
        key = str(reason or "unknown").strip() or "unknown"
        filter_summary[key] = filter_summary.get(key, 0) + 1
        samples = audit["filteredSamples"]
        if len(samples) >= MAX_FILTER_SAMPLES_PER_SCAN:
            return
        item: dict[str, Any] = {"symbol": symbol, "reason": key}
        if isinstance(plan, dict):
            item.update(_plan_audit_snapshot(plan, include_all_checks=False))
        samples.append(item)

    matches: list[dict[str, Any]] = []
    for symbol, quote_turnover in ranked_symbols:
        if symbol in existing_symbols:
            audit["skippedHeldCount"] += 1
            continue
        dataset = history["items"][symbol]
        # Go through the shared frame adapter so callers that provide a
        # historical frame provider (and tests) use exactly the same path as
        # the live-compatible analysis adapter. Short-term mode and direct
        # MODEL inference pass their required 5m frame on this first pass;
        # classic midline remains macro-only until a candidate needs optional
        # micro timing.
        micro_frame = (
            (dataset.get("frames") or {}).get("5m")
            if mode == STRATEGY_MODE_SHORT_TERM or direct_model_mode
            else None
        )
        frames = _analysis_frames(
            dataset,
            micro_frame,
            as_of,
            strategy_mode=mode,
            require_micro=direct_model_mode,
        )
        if frames is None:
            record_filter(symbol, "history_unavailable")
            continue
        audit["evaluatedCount"] += 1
        quote = dict(dataset["quote"])
        # ``_analysis_frames`` normally guarantees a non-empty execution
        # stream.  Keep the adapter tolerant of a synthetic/partially loaded
        # frame provider as well: direct MODEL prediction can still validate
        # its own input and return WAIT instead of raising an IndexError.
        execution_bars = frames.get(execution_interval) if isinstance(frames, dict) else None
        latest_close = (
            execution_bars[-1].get("close")
            if isinstance(execution_bars, list) and execution_bars and isinstance(execution_bars[-1], dict)
            else quote.get("markPrice") or quote.get("lastPrice")
        )
        if latest_close is not None:
            quote["markPrice"] = latest_close
            quote["lastPrice"] = latest_close
        else:
            # Synthetic tests and incomplete local rows may not carry a
            # quote price.  Preserve a harmless numeric placeholder for the
            # prediction adapter; a real model will reject missing frames.
            quote.setdefault("markPrice", 0.0)
            quote.setdefault("lastPrice", quote.get("markPrice", 0.0))
        quote["quoteVolume"] = quote_turnover
        if direct_model_mode:
            # The MODEL engine owns direction and all price levels.  The
            # classic analyzer is intentionally bypassed; its output cannot
            # become a hidden fallback in a model-selected replay.
            plan = predict_binance_futures_frames(
                {interval: list(value) for interval, value in frames.items() if interval in MODEL_HISTORY_INTERVALS},
                network=network,
                plan={"symbol": symbol, "quoteVolume": quote_turnover, "lastPrice": quote.get("lastPrice"), "markPrice": quote.get("markPrice"), "strategySettings": strategy_settings},
                branch=_model_branch({"modelBranch": model_branch}),
                model_run_id=model_run_id,
                allow_rejected_research_model=True,
            )
            if plan is None:
                record_filter(symbol, "model:prediction_unavailable")
                continue
        else:
            plan = analyze_futures_history(quote, frames, strategy_settings=strategy_settings)
        if not plan:
            record_filter(symbol, "analysis_unavailable")
            continue
        # 5m only refines entry timing in midline mode. Short-term mode already
        # evaluated it as the required execution frame and must not be
        # downgraded to a macro-only candidate.
        if not direct_model_mode and mode != STRATEGY_MODE_SHORT_TERM:
            micro_item = micro_history.get(symbol)
            if _plan_is_actionable(plan) and micro_item is None and callable(micro_loader):
                loaded_micro = micro_loader(symbol)
                # Cache both success and failure. A symbol with an unavailable
                # 5m window must not trigger the same database/network attempt on
                # every later 15m slice.
                micro_history[symbol] = loaded_micro or {"frame": None, "stale": True}
                micro_item = micro_history[symbol]
            optional_micro_frame = micro_item.get("frame") if isinstance(micro_item, dict) else None
            if _plan_is_actionable(plan) and isinstance(optional_micro_frame, dict):
                enriched_frames = _analysis_frames(dataset, optional_micro_frame, as_of, strategy_mode=mode)
                if enriched_frames is not None:
                    enriched = analyze_futures_history(quote, enriched_frames, strategy_settings=strategy_settings)
                    if enriched:
                        plan = enriched
        # A plan generated with the previous completed bar is the one that is
        # eligible for this candle. Never use the current candle's result to
        # retroactively change this scan.
        plan["quoteVolume"] = quote_turnover
        if direct_model_mode:
            if str(plan.get("direction") or "").upper() == "WAIT":
                record_filter(symbol, "model:wait", plan)
                continue
            # Keep the direct model on its own geometry/status contract.  A
            # malformed or already-crossed plan must never reach the classic
            # entry state machine as a hidden fallback candidate.
            if not _plan_is_actionable(plan):
                record_filter(symbol, "model:invalid", plan)
                continue
            matches.append(plan)
        elif _plan_is_actionable(plan):
            if model_selection == MODEL_SELECTION_FILTER:
                # Feed only the bars that had already closed before this
                # execution candle. The model may filter a plan but never
                # supplies its direction or price levels.
                plan["_mlFrames"] = {
                    interval: list(frames[interval])
                    for interval in ("4h", "1h", "15m", "5m")
                    if isinstance(frames.get(interval), list)
                }
            matches.append(plan)
        else:
            failed_ids = [
                str(check.get("id") or "unknown")
                for check in plan.get("conditionChecks") or []
                if isinstance(check, dict) and not check.get("passed")
            ]
            record_filter(symbol, "condition:" + ",".join(failed_ids or [str(plan.get("status") or "not_armed")]), plan)
    audit["armedCount"] = sum(1 for plan in matches if str(plan.get("status") or "").upper() == "ARMED")
    audit["trialCount"] = sum(1 for plan in matches if _plan_is_trial_eligible(plan))
    audit["disciplineEligibleCount"] = len(matches)
    if model_selection == MODEL_SELECTION_FILTER and matches and not direct_model_mode:
        try:
            attach_binance_ml_predictions(
                matches,
                network=network,
                allow_rejected_research_model=True,
                branch=_model_branch({"modelBranch": model_branch}),
                model_run_id=model_run_id,
            )
        except TypeError as exc:
            # Keep compatibility with test/integration adapters that still
            # expose the pre-run and pre-branch prediction signatures.
            if "model_run_id" not in str(exc) and "branch" not in str(exc):
                raise
            try:
                attach_binance_ml_predictions(
                    matches,
                    network=network,
                    allow_rejected_research_model=True,
                    branch=_model_branch({"modelBranch": model_branch}),
                )
            except TypeError as nested_exc:
                if "branch" not in str(nested_exc):
                    raise
                attach_binance_ml_predictions(
                    matches,
                    network=network,
                    allow_rejected_research_model=True,
                )
        for plan in matches:
            _apply_model_strategy(plan, _strategy_settings(strategy_settings), _model_branch({"modelBranch": model_branch}))
        model_audit = audit["model"]
        accepted: list[dict[str, Any]] = []
        for plan in matches:
            symbol = str(plan.get("symbol") or "").upper()
            model_audit["evaluatedCount"] += 1
            reason = _model_prediction_rejection_reason(plan)
            if reason:
                model_audit["rejectedCount"] += 1
                rejected_by_reason = model_audit["rejectedByReason"]
                rejected_by_reason[reason] = int(rejected_by_reason.get(reason) or 0) + 1
                record_filter(symbol, f"model:{reason}", plan)
                continue
            model_audit["acceptedCount"] += 1
            accepted.append(plan)
        matches = accepted
    audit["eligibleCount"] = len(matches)
    audit["candidatePlans"] = [_plan_audit_snapshot(plan) for plan in matches]
    audit["filterSummary"] = filter_summary
    if not matches:
        portfolio["_currentEntryScan"] = audit
        return []
    matches.sort(key=_candidate_ranking_key, reverse=True)
    candidates: list[dict[str, Any]] = []
    for plan in matches:
        symbol = str(plan.get("symbol") or "").upper()
        if not symbol or symbol in existing_symbols:
            continue
        candidate = _slice_entry_candidate(plan, as_of + 1, strategy_settings=strategy_settings)
        if candidate is None:
            record_filter(symbol, "candidate_level_invalid", plan)
            continue
        candidates.append(candidate)
        existing_symbols.add(symbol)
    audit["candidateCount"] = len(candidates)
    portfolio["_currentEntryScan"] = audit
    if not candidates:
        return []
    actions.append(
        _action(
            "SCAN_MATCH",
            "",
            "",
            note=f"在上一根已收盘{execution_interval}成交额前{scan_limit}范围内发现 {audit['armedCount']} 个符合计划和 {audit['trialCount']} 个试错计划；仅允许在当前{execution_interval}内触发开仓。",
            count=len(matches),
        )
    )
    return candidates


def _model_prediction_rejection_reason(plan: dict[str, Any]) -> str | None:
    prediction = plan.get("modelPrediction") if isinstance(plan.get("modelPrediction"), dict) else None
    if prediction is None:
        return "prediction_unavailable"
    direction = str(plan.get("direction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return "plan_direction_invalid"
    if str(prediction.get("verdict") or "").upper() != "FAVORABLE":
        return "below_validation_threshold"
    if str(prediction.get("direction") or "").upper() != direction:
        return "direction_mismatch"
    side_prediction = prediction.get(direction.lower())
    probability = _number(side_prediction.get("planFeasibilityProbability")) if isinstance(side_prediction, dict) else 0.0
    if probability <= 0.0 or probability > 1.0:
        return "probability_invalid"
    expected_r = _number(side_prediction.get("expectedR")) if isinstance(side_prediction, dict) else 0.0
    time_efficiency = _number(side_prediction.get("timeEfficiencyScore")) if isinstance(side_prediction, dict) else 0.0
    if not math.isfinite(expected_r) or not math.isfinite(time_efficiency):
        return "utility_invalid"
    return None


def _candidate_ranking_key(plan: dict[str, Any]) -> tuple[float, float, float, float]:
    """Rank scarce portfolio slots by model utility, then liquidity.

    Baseline replays have no prediction and therefore retain the historical
    quote-volume/discipline-quality order.  Filtered replays prefer the model's
    validation-gated time-adjusted utility; turnover remains the deterministic
    tie-breaker rather than the primary model-selection signal.
    """

    direction = str(plan.get("direction") or "").strip().lower()
    prediction = plan.get("modelPrediction") if isinstance(plan.get("modelPrediction"), dict) else {}
    side = prediction.get(direction) if isinstance(prediction.get(direction), dict) else {}
    has_model_score = 1.0 if side else 0.0
    return (
        has_model_score * _number(side.get("selectionScore") if side.get("selectionScore") is not None else side.get("timeEfficiencyScore")),
        has_model_score * _number(side.get("expectedR")),
        _number(plan.get("quoteVolume")),
        _number(plan.get("quality")),
    )


def _plan_audit_snapshot(plan: dict[str, Any], *, include_all_checks: bool = True) -> dict[str, Any]:
    """Keep the plan facts needed to explain an entry or its rejection."""

    checks = []
    for check in plan.get("conditionChecks") or []:
        if not isinstance(check, dict):
            continue
        if include_all_checks or not check.get("passed"):
            checks.append(
                {
                    "id": check.get("id"),
                    "label": check.get("label"),
                    "passed": bool(check.get("passed")),
                    "reason": check.get("reason"),
                    "missing": check.get("missing"),
                }
            )
    return {
        "symbol": plan.get("symbol"),
        "direction": plan.get("direction"),
        "status": plan.get("status"),
        "marketMode": plan.get("marketMode"),
        "strategyMode": plan.get("strategyMode"),
        "strategyModeLabel": plan.get("strategyModeLabel"),
        "timeframeRoles": deepcopy(plan.get("timeframeRoles") or {}),
        "levelStrategy": plan.get("levelStrategy"),
        "conditionMet": plan.get("conditionMet"),
        "conditionTotal": plan.get("conditionTotal"),
        "conditionCompleteness": plan.get("conditionCompleteness"),
        "trialEligible": bool(plan.get("trialEligible")),
        "entry": deepcopy(plan.get("entry")),
        "entryTiming": deepcopy(plan.get("entryTiming") or {}),
        "entryConfirmation": deepcopy(plan.get("entryConfirmation") or {}),
        "stopLoss": plan.get("stopLoss"),
        "takeProfits": deepcopy(plan.get("takeProfits") or []),
        "modelPrediction": deepcopy(plan.get("modelPrediction")) if isinstance(plan.get("modelPrediction"), dict) else None,
        "minimumTargetR": plan.get("minimumTargetR"),
        "levelBasis": deepcopy(plan.get("levelBasis")),
        "conditionChecks": checks,
        "missingConditions": deepcopy(plan.get("missingConditions") or []),
        "blockedReasons": deepcopy(plan.get("blockedReasons") or []),
    }


def _target_prices_by_role(targets: list[Any]) -> tuple[float | None, float | None, float | None]:
    """Read role-based target snapshots and preserve the legacy two-item form."""

    by_role = {
        str(item.get("role") or "").strip().upper(): _positive(item.get("price"))
        for item in targets
        if isinstance(item, dict) and str(item.get("role") or "").strip()
    }
    if by_role:
        return by_role.get("PROTECTIVE_TARGET"), by_role.get("FIRST_TARGET"), by_role.get("EXTENSION_TARGET")
    if len(targets) >= 3:
        return (
            _positive(targets[0].get("price")) if isinstance(targets[0], dict) else None,
            _positive(targets[1].get("price")) if isinstance(targets[1], dict) else None,
            _positive(targets[2].get("price")) if isinstance(targets[2], dict) else None,
        )
    return (
        None,
        _positive(targets[0].get("price")) if targets and isinstance(targets[0], dict) else None,
        _positive(targets[1].get("price")) if len(targets) > 1 and isinstance(targets[1], dict) else None,
    )


def _target_cumulative_ratios(
    targets: list[Any],
    settings: dict[str, Any],
) -> tuple[float | None, float, float]:
    """Read effective cumulative allocations frozen on a plan's targets."""

    by_role = {
        str(item.get("role") or "").strip().upper(): item
        for item in targets
        if isinstance(item, dict) and str(item.get("role") or "").strip()
    }
    protective_item = by_role.get("PROTECTIVE_TARGET")
    protective = _positive((protective_item or {}).get("cumulativeRatio"))
    first = _positive((by_role.get("FIRST_TARGET") or {}).get("cumulativeRatio"))
    extension = _positive((by_role.get("EXTENSION_TARGET") or {}).get("cumulativeRatio"))
    # A direct-model plan with two targets intentionally has no near
    # protective target.  Its default account ratio must not manufacture a
    # third simulated exit or make the plan fail the three-level validation.
    has_protective_target = _positive((protective_item or {}).get("price")) is not None
    if not has_protective_target:
        protective = None
    elif protective is None:
        protective = float(settings["protectiveTakeProfitRatio"])
    first = first if first is not None else float(settings["firstTakeProfitRatio"])
    extension = extension if extension is not None else float(settings["secondTakeProfitRatio"])
    # An unavailable extension is intentionally represented by the first
    # effective boundary, leaving only the moving-stop runner beyond it.
    extension_item = by_role.get("EXTENSION_TARGET")
    has_extension_target = (
        _positive(extension_item.get("price")) is not None
        and extension_item.get("available", True) is not False
        if isinstance(extension_item, dict)
        else len(targets) >= 2 if not by_role else False
    )
    if not has_extension_target:
        # With no confirmed extension, the first structural target is the
        # final fixed target. Promote its effective cumulative allocation to
        # the configured second boundary, leaving the rest to the runner.
        first = extension
        if first is None:
            first = float(settings["secondTakeProfitRatio"])
        extension = first
    return protective, first, extension


def _slice_entry_candidate(
    plan: dict[str, Any],
    slice_open_time: int,
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    trigger = _positive(entry.get("trigger"))
    stop = _positive(plan.get("stopLoss"))
    targets = plan.get("takeProfits") if isinstance(plan.get("takeProfits"), list) else []
    protective, first, second = _target_prices_by_role(targets)
    side = str(plan.get("direction") or "").upper()
    # Rebound/range plans may legitimately have no confirmed continuation
    # target yet.  The first target is enough to replay the managed position;
    # a missing second target must not become a numeric comparison error.
    if trigger is None or stop is None or first is None or side not in {"LONG", "SHORT"}:
        return None
    if side == "LONG" and not (stop < trigger < first):
        return None
    if side == "SHORT" and not (stop > trigger > first):
        return None
    if second is not None and (
        (side == "LONG" and second <= first)
        or (side == "SHORT" and second >= first)
    ):
        return None
    timing = deepcopy(plan.get("entryTiming") or {}) if isinstance(plan.get("entryTiming"), dict) else {}
    confirmation = plan.get("entryConfirmation") if isinstance(plan.get("entryConfirmation"), dict) else {}
    model_order_type = str(entry.get("orderType") or "").strip().upper()
    entry_type = ENTRY_TYPE_LIMIT_RETEST if model_order_type == "LIMIT" else ENTRY_TYPE_STOP_TRIGGER
    settings = _strategy_settings(strategy_settings or plan.get("strategySettings"))
    trailing = plan.get("trailingStop") if isinstance(plan.get("trailingStop"), dict) else {}
    breakeven = trailing.get("BREAKEVEN") if isinstance(trailing.get("BREAKEVEN"), dict) else {}
    structure = trailing.get("STRUCTURE_TRAILING") if isinstance(trailing.get("STRUCTURE_TRAILING"), dict) else {}
    atr_trailing = trailing.get("ATR_TRAILING") if isinstance(trailing.get("ATR_TRAILING"), dict) else {}
    model_overrides = {
        "movingStopActivationR": breakeven.get("triggerR") or (trailing.get("INITIAL") or {}).get("activationR"),
        "breakevenBufferAtrMultiplier": breakeven.get("bufferAtr"),
        "structureStopAtrMultiplier": structure.get("bufferAtr"),
        "trailingAtrMultiplier": atr_trailing.get("multiplier"),
    }
    trailing_structure_lookback = int(_number(structure.get("lookbackBars")) or TRAILING_LOOKBACK)
    trailing_atr_period = int(_number(atr_trailing.get("period")) or 14)
    if any(value is not None for value in model_overrides.values()):
        settings = _strategy_settings({**settings, **{key: value for key, value in model_overrides.items() if value is not None}})
    frames = plan.get("timeframes") if isinstance(plan.get("timeframes"), dict) else {}
    environment_timeframe = "1h" if _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM else "4h"
    environment_frame = frames.get(environment_timeframe) if isinstance(frames.get(environment_timeframe), dict) else {}
    protective_ratio, first_ratio, second_ratio = _target_cumulative_ratios(targets, settings)
    return {
        "symbol": str(plan.get("symbol") or "").upper(),
        "side": side,
        "trigger": trigger,
        "macroTrigger": trigger,
        "entryType": entry_type,
        "initialStop": stop,
        "targetProtection": protective,
        "targetOne": first,
        "targetTwo": second,
        "protectiveTakeProfitRatio": protective_ratio,
        "firstTakeProfitRatio": first_ratio,
        "secondTakeProfitRatio": second_ratio,
        "minimumTargetR": _plan_minimum_target_r(plan, settings),
        "entryZoneLow": _positive(entry.get("zoneLow")),
        "entryZoneHigh": _positive(entry.get("zoneHigh")),
        # The pending 5m confirmation must use the same environment range as
        # the plan builder. Short-term mode is anchored to 1h; using the 4h
        # range here would validate a retest against a different structure.
        "rangeLow": _positive(environment_frame.get("rangeLow")),
        "rangeHigh": _positive(environment_frame.get("rangeHigh")),
        "createdAt": slice_open_time,
        "marketMode": plan.get("marketMode"),
        "entryTiming": timing,
        "entryConfirmationRequired": bool(confirmation.get("required")) or settings["entryConfirmationMode"] == ENTRY_CONFIRMATION_RETEST,
        "entryConfirmationExpiryBars": int(confirmation.get("expiryBars") or settings["entryConfirmationExpiryBars"]),
        "strategySettings": settings,
        "structureTrailingLookbackBars": max(1, min(96, trailing_structure_lookback)),
        "atrTrailingPeriod": max(2, min(96, trailing_atr_period)),
    }


def _plan_minimum_target_r(plan: dict[str, Any], settings: dict[str, Any] | None = None) -> float:
    """Return the plan's auditable first-target room requirement."""

    explicit = _positive(plan.get("minimumTargetR"))
    if explicit is not None:
        return explicit
    resolved = settings or _strategy_settings(plan.get("strategySettings"))
    mode = str(plan.get("marketMode") or "TREND").upper()
    if mode == "RANGE":
        return float(resolved["rangeMinimumTargetR"])
    if mode == "REBOUND":
        # Must match the transition rule used by the strategy plan builder.
        return 1.25
    return float(resolved["trendMinimumTargetR"])


def _retest_confirmation_failure_reason(candidate: dict[str, Any], timing: dict[str, Any]) -> str | None:
    """Reject a late retest if it no longer leaves the planned room to target."""

    if str(timing.get("state") or "").upper() != "READY":
        return None
    reference = _positive(timing.get("recommendedLimitPrice")) or _positive(timing.get("referencePrice"))
    stop = _positive(candidate.get("initialStop"))
    target = _positive(candidate.get("targetOne"))
    side = str(candidate.get("side") or "").upper()
    if reference is None or stop is None or target is None or side not in {"LONG", "SHORT"}:
        return "retest_price_invalid"
    if side == "LONG" and not (stop < reference < target):
        return "retest_price_invalid"
    if side == "SHORT" and not (stop > reference > target):
        return "retest_price_invalid"
    risk = abs(reference - stop)
    room = abs(target - reference)
    if risk <= EPSILON or room / risk + EPSILON < _plan_minimum_target_r(candidate):
        return "retest_target_space_insufficient"
    return None


def _pending_entry(plan: dict[str, Any], close_time: int) -> dict[str, Any] | None:
    """Build a one-candle legacy pending entry for direct callers/tests."""

    candidate = _slice_entry_candidate(plan, close_time)
    if candidate is not None:
        candidate["expiresAt"] = int(close_time) + 15 * 60 * 1000
        # Older callers used this helper for direct trigger entries. The
        # replay's discipline queue is created by _queue_pending_entries.
        candidate["entryConfirmationRequired"] = False
    return candidate


def _expire_pending_entries(portfolio: dict[str, Any], close_time: int, actions: list[dict[str, Any]]) -> None:
    for key, candidate in list((portfolio.get("pending") or {}).items()):
        # Replay-created plans always carry an explicit deadline.  Keep
        # legacy/direct callers usable when they hand us an already-ready
        # candidate without one; treating a missing deadline as zero would
        # silently cancel it before the first evaluation.
        expires_at = candidate.get("expiresAt")
        if expires_at in (None, "") or int(expires_at) >= int(close_time):
            continue
        portfolio["pending"].pop(key, None)
        portfolio["pendingExpiredCount"] = int(portfolio.get("pendingExpiredCount") or 0) + 1
        actions.append(_action("CANCEL", candidate.get("symbol", ""), candidate.get("side", ""), note="确认窗口已过，取消未成交条件计划。"))


def _pending_entry_key(candidate: dict[str, Any]) -> str:
    return f"{str(candidate.get('symbol') or '').upper()}:{str(candidate.get('side') or '').upper()}"


def _queue_pending_entries(
    portfolio: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    interval_ms: int = BACKTEST_INTERVAL_MS,
) -> None:
    """Hold macro plans briefly while waiting for an auditable 5m retest."""

    pending = portfolio.setdefault("pending", {})
    for candidate in candidates:
        key = _pending_entry_key(candidate)
        if not key or key in pending:
            continue
        queued = deepcopy(candidate)
        expiry_bars = max(1, int(queued.get("entryConfirmationExpiryBars") or 1))
        queued["expiresAt"] = int(queued.get("createdAt") or 0) + expiry_bars * int(interval_ms)
        queued["pendingState"] = "WAITING_CONFIRMATION" if queued.get("entryConfirmationRequired") else "READY"
        pending[key] = queued


def _confirmed_retest_candidate(candidate: dict[str, Any], timing: dict[str, Any], confirmed_at: int) -> dict[str, Any] | None:
    """Turn a completed 5m retest into a limit candidate without moving risk."""

    if _retest_confirmation_failure_reason(candidate, timing) is not None:
        return None
    reference = _positive(timing.get("recommendedLimitPrice")) or _positive(timing.get("referencePrice"))
    stop = _positive(candidate.get("initialStop"))
    target = _positive(candidate.get("targetOne"))
    side = str(candidate.get("side") or "").upper()
    if reference is None or stop is None or target is None or side not in {"LONG", "SHORT"}:
        return None
    if side == "LONG" and not (stop < reference < target):
        return None
    if side == "SHORT" and not (stop > reference > target):
        return None
    confirmed = deepcopy(candidate)
    confirmed["trigger"] = reference
    confirmed["entryType"] = ENTRY_TYPE_LIMIT_RETEST
    confirmed["entryTiming"] = deepcopy(timing)
    confirmed["confirmedAt"] = int(confirmed_at)
    confirmed["confirmationSetup"] = timing.get("setup")
    confirmed["pendingState"] = "CONFIRMED"
    return confirmed


def _refresh_pending_confirmation(
    candidate: dict[str, Any],
    history: dict[str, Any],
    micro_history: dict[str, dict[str, Any]],
    as_of: int,
    *,
    micro_loader=None,
    execution_interval: str = "15m",
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a fillable candidate only after its original 5m setup is ready."""

    if not candidate.get("entryConfirmationRequired"):
        return candidate, None
    if str(candidate.get("entryType") or "").upper() == ENTRY_TYPE_LIMIT_RETEST:
        return candidate, None
    symbol = str(candidate.get("symbol") or "").upper()
    dataset = (history.get("items") or {}).get(symbol)
    if not dataset:
        return None, "history_unavailable"
    frames = dataset.get("frames") if isinstance(dataset.get("frames"), dict) else {}
    if any(interval not in frames for interval in ("15m", "1h", "4h")):
        return None, "history_unavailable"
    candidate_mode = _strategy_mode(candidate.get("strategySettings"))
    if candidate_mode == STRATEGY_MODE_SHORT_TERM and "5m" not in frames:
        return None, "history_unavailable"
    macro = _macro_frames(dataset, as_of)
    if macro is None:
        return None, "history_unavailable"
    latest = macro["15m"][-1]
    stop = _positive(candidate.get("initialStop"))
    side = str(candidate.get("side") or "").upper()
    if stop is not None and (
        (side == "LONG" and latest["close"] <= stop)
        or (side == "SHORT" and latest["close"] >= stop)
    ):
        return None, "macro_structure_failed"

    micro_item = micro_history.get(symbol)
    if candidate_mode == STRATEGY_MODE_SHORT_TERM:
        micro_frame = frames.get("5m")
    elif micro_item is None and callable(micro_loader):
        micro_history[symbol] = micro_loader(symbol) or {"frame": None, "stale": True}
        micro_item = micro_history[symbol]
        micro_frame = micro_item.get("frame") if isinstance(micro_item, dict) else None
    else:
        micro_frame = micro_item.get("frame") if isinstance(micro_item, dict) else None
    micro = _bars_until(micro_frame, as_of) if isinstance(micro_frame, dict) else []
    execution_price = micro[-1]["close"] if micro else latest["close"]
    timing = _entry_timing(
        micro,
        side,
        execution_price,
        _positive(candidate.get("macroTrigger")) or _positive(candidate.get("trigger")),
        mode=str(candidate.get("marketMode") or "TREND").upper(),
        range_low=_positive(candidate.get("rangeLow")),
        range_high=_positive(candidate.get("rangeHigh")),
        stop_loss=stop,
        range_timeframe="1h" if candidate_mode == STRATEGY_MODE_SHORT_TERM else "4h",
    )
    candidate["entryTiming"] = deepcopy(timing)
    failure_reason = _retest_confirmation_failure_reason(candidate, timing)
    if failure_reason is not None:
        return None, failure_reason
    confirmed = _confirmed_retest_candidate(candidate, timing, as_of)
    return confirmed, None


def _process_pending_entries(
    network: str,
    history: dict[str, Any],
    micro_history: dict[str, dict[str, Any]],
    portfolio: dict[str, Any],
    reference_bar: dict[str, float],
    close_time: int,
    minute_cache: dict[tuple[str, int], list[dict[str, float]]],
    actions: list[dict[str, Any]],
    *,
    entry_slots: int | None = None,
    micro_loader=None,
    strategy_settings: object = None,
    interval: str = "15m",
) -> None:
    """Evaluate pending macro plans against only the current slice's known data."""

    pending = portfolio.setdefault("pending", {})
    current_open = int(reference_bar.get("openTime") or 0)
    if current_open:
        _expire_pending_entries(portfolio, current_open, actions)
    if entry_slots is None:
        entry_slots = max(
            0,
            _portfolio_execution_settings(portfolio)["maxManagedPositions"]
            - _managed_position_count(portfolio.get("positions") or []),
        )
    opened_this_slice = 0
    reserved_symbols = {str(position.get("symbol") or "").upper() for position in portfolio.get("positions") or []}
    for key, candidate in list(pending.items()):
        if opened_this_slice >= entry_slots:
            break
        symbol = str(candidate.get("symbol") or "").upper()
        dataset = history.get("items", {}).get(symbol)
        if not dataset or symbol in reserved_symbols:
            pending.pop(key, None)
            continue
        active_candidate, cancellation = _refresh_pending_confirmation(
            candidate,
            history,
            micro_history,
            current_open - 1 if current_open else int(close_time) - 1,
            micro_loader=micro_loader,
            execution_interval=interval,
        )
        if cancellation:
            pending.pop(key, None)
            if cancellation == "retest_target_space_insufficient":
                minimum_target_r = _plan_minimum_target_r(candidate)
                note = f"5m 回测限价使第一结构目标空间不足 {minimum_target_r:g}R，取消条件计划，不追价。"
            elif cancellation == "retest_price_invalid":
                note = "5m 回测限价已不在结构止损与第一目标之间，取消条件计划。"
            else:
                note = "等待确认期间主结构失效，取消条件计划。"
            actions.append(_action("CANCEL", symbol, candidate.get("side", ""), note=note))
            continue
        if active_candidate is None:
            continue
        if active_candidate is not candidate:
            pending[key] = active_candidate
            candidate = active_candidate
            portfolio["pendingConfirmedCount"] = int(portfolio.get("pendingConfirmedCount") or 0) + 1
            actions.append(_action("CONFIRM", symbol, candidate["side"], price=candidate["trigger"], note=f"5m {candidate.get('confirmationSetup') or '回测'} 已确认，等待回测价成交。"))
        symbol_bar = _bar_at_close(dataset["frames"][interval], int(close_time))
        if symbol_bar is None or not _entry_triggered(candidate, symbol_bar):
            continue
        candidate_settings = _strategy_settings(candidate.get("strategySettings"))
        context = _management_context(
            dataset,
            int(symbol_bar["openTime"]) - 1,
            # Preserve the pending candidate's side while building the first
            # management context; otherwise short entries use long-side
            # continuation evidence for their initial candle.
            candidate,
            strategy_settings=candidate_settings,
            execution_interval=interval,
        )
        path_bars = [symbol_bar]
        if _entry_requires_one_minute_resolution(candidate, symbol_bar, context):
            # Do not fetch 1m for a pending/retest entry either.  The
            # direction-adverse OHLC path is the explicit replay policy.
            portfolio["conservativeIntrabarPaths"] = portfolio.get("conservativeIntrabarPaths", 0) + 1
        entry_path = _entry_path(candidate, path_bars)
        if not entry_path:
            continue
        position = _open_position(
            candidate,
            float(entry_path[0]["open"]),
            portfolio,
            strategy_settings=candidate_settings or strategy_settings,
        )
        if position is None:
            _record_entry_block(portfolio, actions, candidate)
            continue
        opened_this_slice += 1
        pending.pop(key, None)
        portfolio.setdefault("positions", []).append(position)
        reserved_symbols.add(symbol)
        portfolio["openedCount"] = portfolio.get("openedCount", 0) + 1
        actions.append(
            _action(
                "OPEN",
                symbol,
                position["side"],
                price=position["entryPrice"],
                quantity=position["quantity"],
                note=(
                    f"{ '5m 回测确认后' if candidate.get('entryType') == ENTRY_TYPE_LIMIT_RETEST else '按触发价' }开仓；按总权益{_portfolio_execution_settings(portfolio)['positionMarginFraction'] * 100:g}%保证金开仓，"
                    f"{position['leverage']}x杠杆，实际结构止损风险不高于账户风险预算。"
                ),
            )
        )
        if not _process_position_bars(position, entry_path, context, portfolio, actions):
            portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]
        elif _strategy_engine(candidate_settings) != "MODEL" and not _process_entry_failure(
            position,
            _entry_failure_evaluation_bar(entry_path, symbol_bar),
            context,
            portfolio,
            actions,
        ):
            portfolio["positions"] = [item for item in portfolio["positions"] if item is not position]


def _slice_top_turnover_symbols(
    history: dict[str, Any],
    as_of: int,
    excluded_symbols: set[str] | None = None,
    limit: int = BACKTEST_SCAN_LIMIT,
    *,
    interval: str = "15m",
) -> list[tuple[str, float]]:
    """Return the liquid historical universe known at one execution slice open.

    The fixed historical table currently stores base volume and OHLC.  For
    USDT perpetuals, ``volume × close`` is the available historical turnover
    proxy and is comparable across symbols, unlike raw base-asset volume.
    """

    excluded = {str(symbol or "").upper() for symbol in (excluded_symbols or set())}
    ranked: list[tuple[str, float]] = []
    for symbol in history.get("symbols") or []:
        safe_symbol = str(symbol or "").upper()
        if not safe_symbol or safe_symbol in excluded:
            continue
        dataset = (history.get("items") or {}).get(safe_symbol)
        frame = dataset.get("frames", {}).get(interval) if isinstance(dataset, dict) else None
        if not isinstance(frame, dict):
            continue
        bar = _bar_at_close(frame, as_of)
        if bar is None:
            continue
        turnover = max(0.0, _number(bar.get("volume"))) * max(0.0, _number(bar.get("close")))
        ranked.append((safe_symbol, turnover))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:max(1, int(limit or BACKTEST_SCAN_LIMIT))]


def _open_position(
    candidate: dict[str, Any],
    entry_price: float,
    portfolio: dict[str, Any],
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    portfolio.pop("_lastEntryBlockReason", None)
    equity = _portfolio_equity(portfolio)
    execution_settings = _portfolio_execution_settings(portfolio)
    available = max(0.0, portfolio["cash"])
    is_long = candidate["side"] == "LONG"
    structure_distance = abs(entry_price - candidate["initialStop"])
    if structure_distance <= EPSILON:
        return None
    settings = _strategy_settings(strategy_settings or candidate.get("strategySettings"))
    risk_budget = max(equity * settings["maxAccountLossRatio"] / 100, EPSILON)
    max_margin = min(available, equity * execution_settings["positionMarginFraction"])
    if max_margin <= EPSILON:
        return None

    if execution_settings["leverageMode"] == LEVERAGE_MODE_FIXED:
        leverage = int(execution_settings["leverage"] or 1)
        leverage_rule = "FIXED"
    else:
        ideal_leverage = risk_budget * entry_price / (max_margin * structure_distance)
        leverage = _risk_budget_leverage(ideal_leverage, execution_settings["maxLeverage"])
        leverage_rule = "RISK_BUDGET"

    # Margin is reduced when the selected leverage would otherwise exceed the
    # account risk budget.  This keeps the one-way leverage decision useful at
    # wide stops and makes the minimum 1x case risk-safe instead of silently
    # oversizing the position.
    risk_safe_margin = risk_budget * entry_price / (leverage * structure_distance)
    margin = min(max_margin, risk_safe_margin)
    if margin <= EPSILON:
        return None
    quantity = margin * leverage / entry_price
    if quantity <= EPSILON:
        return None
    structural_risk = structure_distance * quantity
    admission_reason = _portfolio_admission_reason(
        portfolio,
        candidate,
        structural_risk,
        equity,
        settings,
    )
    if admission_reason:
        portfolio["_lastEntryBlockReason"] = admission_reason
        return None
    risk_stop_distance = risk_budget / quantity
    position_risk_stop = entry_price - risk_stop_distance if is_long else entry_price + risk_stop_distance
    # The price stop represents the invalidation of the price-action premise.
    # Account risk belongs in position sizing and portfolio admission, not as
    # a closer substitute stop inside the structural invalidation zone.
    selected_stop = candidate["initialStop"]
    selected_source = "STRUCTURE"
    portfolio["cash"] -= margin
    return {
        "symbol": candidate["symbol"],
        "side": candidate["side"],
        "entryPrice": entry_price,
        "quantity": quantity,
        "initialQuantity": quantity,
        "margin": margin,
        "initialMargin": margin,
        "leverage": leverage,
        "leverageMode": leverage_rule,
        "initialStop": candidate["initialStop"],
        "positionRiskStop": position_risk_stop,
        "targetProtection": candidate.get("targetProtection"),
        "targetOne": candidate["targetOne"],
        "targetTwo": candidate["targetTwo"],
        "entryTrigger": _positive(candidate.get("macroTrigger")) or entry_price,
        "entryZoneLow": _positive(candidate.get("entryZoneLow")),
        "entryZoneHigh": _positive(candidate.get("entryZoneHigh")),
        "marketMode": candidate.get("marketMode"),
        "entryType": candidate.get("entryType") or ENTRY_TYPE_STOP_TRIGGER,
        "entryFailureBarsObserved": 0,
        "entryFailureWarning": None,
        "entryFailureLastCloseTime": None,
        "risk": max(structure_distance, entry_price * 0.0004 * 0.35),
        "peakPrice": entry_price,
        "favorableExtreme": entry_price,
        "favorableExtremeAt": candidate["createdAt"],
        "movingStop": None,
        "protectedStop": selected_stop,
        "activeStopSource": selected_source or "STRUCTURE",
        "stopManagementStage": "INITIAL",
        "protectiveTargetReached": False,
        "firstTargetReached": False,
        "secondTargetReached": False,
        "runner": False,
        "strategySettings": settings,
        "protectiveTakeProfitRatio": (
            candidate.get("protectiveTakeProfitRatio")
            if _positive(candidate.get("targetProtection")) is not None
            else None
        ),
        "firstTakeProfitRatio": candidate.get("firstTakeProfitRatio", settings["firstTakeProfitRatio"]),
        "secondTakeProfitRatio": candidate.get("secondTakeProfitRatio", settings["secondTakeProfitRatio"]),
        "structureTrailingLookbackBars": int(candidate.get("structureTrailingLookbackBars") or TRAILING_LOOKBACK),
        "atrTrailingPeriod": int(candidate.get("atrTrailingPeriod") or 14),
        "realizedPnl": 0.0,
        "currentPrice": entry_price,
        "openedAt": candidate["createdAt"],
    }


def _position_open_risk(position: dict[str, Any]) -> float:
    """Return the remaining loss to the active structural defense line."""

    quantity = max(0.0, _number(position.get("quantity")))
    if quantity <= EPSILON:
        return 0.0
    current = _positive(position.get("currentPrice")) or _positive(position.get("entryPrice"))
    stop = _positive(position.get("protectedStop")) or _positive(position.get("initialStop"))
    if current is None or stop is None:
        return 0.0
    if str(position.get("side") or "LONG").upper() == "LONG":
        return max(0.0, current - stop) * quantity
    return max(0.0, stop - current) * quantity


def _same_side_position_count(positions: list[dict[str, Any]], side: str) -> int:
    return sum(
        1
        for position in positions
        if str(position.get("side") or "").upper() == side
        and _number(position.get("quantity")) > EPSILON
    )


def _portfolio_admission_reason(
    portfolio: dict[str, Any],
    candidate: dict[str, Any],
    candidate_risk: float,
    equity: float,
    settings: dict[str, Any],
) -> str | None:
    """Block new exposure without replacing a price-action structural stop."""

    side = str(candidate.get("side") or "").upper()
    positions = portfolio.get("positions") or []
    if _same_side_position_count(positions, side) >= int(settings["maxSameSidePositions"]):
        return "same_side_limit"
    existing_risk = sum(_position_open_risk(position) for position in positions)
    portfolio_limit = max(0.0, equity) * settings["maxPortfolioRiskRatio"] / 100
    if existing_risk + candidate_risk > portfolio_limit + EPSILON:
        return "portfolio_risk_limit"
    daily_state = portfolio.get("dailyRiskState") if isinstance(portfolio.get("dailyRiskState"), dict) else {}
    daily_start = _positive(daily_state.get("startEquity")) or max(0.0, equity)
    daily_realized = _number(portfolio.get("dailyRealizedPnl"))
    daily_limit = daily_start * settings["dailyLossLimitRatio"] / 100
    projected_daily_loss = max(0.0, -daily_realized) + existing_risk + candidate_risk
    if projected_daily_loss > daily_limit + EPSILON:
        return "daily_risk_limit"
    return None


def _record_entry_block(portfolio: dict[str, Any], actions: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    reason = str(portfolio.pop("_lastEntryBlockReason", "") or "")
    if not reason:
        return
    portfolio["entryBlockedByRiskCount"] = int(portfolio.get("entryBlockedByRiskCount") or 0) + 1
    labels = {
        "same_side_limit": "同向风险仓位已达上限，阻断新仓。",
        "portfolio_risk_limit": "组合结构风险将超过上限，阻断新仓。",
        "daily_risk_limit": "当日已实现亏损与剩余结构风险将超过日限额，阻断新仓。",
    }
    actions.append(_action("ENTRY_BLOCKED", candidate.get("symbol", ""), candidate.get("side", ""), note=labels.get(reason, "风险准入未通过，阻断新仓。")))


def _risk_budget_leverage(ideal_leverage: float, max_leverage: int = MAX_LEVERAGE_CAP) -> int:
    """Pick integer leverage without ever rounding above the risk budget."""

    safe_cap = max(1, min(MAX_LEVERAGE_CAP, int(max_leverage)))
    return max(1, min(safe_cap, math.floor(max(0.0, ideal_leverage) + 1e-12)))


def _management_context(
    dataset: dict[str, Any],
    as_of: int,
    position: dict[str, Any] | None,
    *,
    strategy_settings: object = None,
    execution_interval: str = "15m",
) -> dict[str, Any]:
    settings = _strategy_settings(
        strategy_settings if strategy_settings is not None else _position_strategy_settings(position)
    )
    # Caller supplies the direct checkpoint's frozen execution interval. Do
    # not overwrite POSITION (1h) or SHORT (5m) management with the default
    # SWING cadence here.
    frames = _macro_frames(dataset, as_of)
    if frames is None:
        return {"atr": 0.0, "structureStop": position.get("initialStop") if position else None, "movingCandidate": None, "trendConfirmed": False}
    # The 15m frame remains the structure layer for classic plans. A direct
    # checkpoint may declare 5m, 15m, or 1h as its own holding cadence; use
    # that frame for model position management instead of silently applying
    # the SWING default to every checkpoint.
    model_mode = _strategy_engine(settings) == "MODEL"
    _read_market(frames["15m"], "15m")
    execution_bars = frames["15m"]
    if model_mode and execution_interval in frames:
        execution_bars = frames[execution_interval]
    if execution_interval == "5m":
        candidate_frame = (dataset.get("frames") or {}).get("5m")
        if isinstance(candidate_frame, dict):
            candidate_bars = _bars_until(candidate_frame, as_of)
            # Short-term plans require a smaller closed 5m sample than the
            # macro indicator warm-up. Keep the execution layer active once
            # its own minimum is available instead of silently evaluating
            # continuation on 15m bars.
            minimum_execution_bars = (
                MIN_SHORT_TERM_READING_BARS
                if _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM
                else HISTORY_WARMUP_BARS
            )
            if len(candidate_bars) >= minimum_execution_bars:
                execution_bars = candidate_bars
    execution_reading = _read_market(execution_bars, execution_interval)
    one_hour = _read_market(frames["1h"], "1h")
    current = execution_bars[-1]["close"]
    if model_mode:
        # Direct plans choose their own ATR period and structure lookback.  Do
        # not silently replace those outputs with the classic 15m/ATR14 level
        # builder while replaying a position.
        atr_period = max(2, min(96, int((position or {}).get("atrTrailingPeriod") or 14)))
        atr_values = _atr(execution_bars, atr_period)
        atr = max(float(atr_values[-1]) if atr_values else 0.0, current * 0.0002)
        is_long = position is None or position["side"] == "LONG"
        lookback = max(1, min(96, int((position or {}).get("structureTrailingLookbackBars") or TRAILING_LOOKBACK)))
        structure_window = execution_bars[-lookback:]
        structure_edge = (
            min(bar["low"] for bar in structure_window)
            if is_long
            else max(bar["high"] for bar in structure_window)
        )
        structure_stop = structure_edge - atr * settings["structureStopAtrMultiplier"] if is_long else structure_edge + atr * settings["structureStopAtrMultiplier"]
        if position is None:
            recent_high = max(bar["high"] for bar in structure_window)
            recent_low = min(bar["low"] for bar in structure_window)
            moving_candidate = recent_high - atr * settings["trailingAtrMultiplier"] if is_long else recent_low + atr * settings["trailingAtrMultiplier"]
        else:
            moving_candidate = _moving_candidate_from_extreme(position, atr)
    else:
        atr = max(execution_reading.atr, current * 0.0004)
        is_long = position is None or position["side"] == "LONG"
        level_basis = _initial_level_basis(
            frames["15m"],
            direction="LONG" if is_long else "SHORT",
            trigger=(position.get("entryPrice") if position else current),
            atr=atr,
            strategy=settings["levelStrategy"],
            platform_allowed=True,
        )
        structure_edge = float(level_basis["initialStop"]["structureEdge"])
        structure_stop = structure_edge - atr * settings["structureStopAtrMultiplier"] if is_long else structure_edge + atr * settings["structureStopAtrMultiplier"]
        if position is None:
            lookback = max(1, min(96, int((position or {}).get("structureTrailingLookbackBars") or TRAILING_LOOKBACK)))
            recent_high = max(bar["high"] for bar in execution_bars[-lookback:])
            recent_low = min(bar["low"] for bar in execution_bars[-lookback:])
            moving_candidate = recent_high - atr * settings["trailingAtrMultiplier"] if is_long else recent_low + atr * settings["trailingAtrMultiplier"]
        else:
            moving_candidate = _moving_candidate_from_extreme(position, atr)
    return {
        "atr": atr,
        "structureStop": structure_stop,
        "movingCandidate": moving_candidate,
        "trendConfirmed": _trend_continuation_confirmed(execution_bars, execution_reading, one_hour, is_long),
    }


def _refresh_management(position: dict[str, Any], context: dict[str, Any], price: float, actions: list[dict[str, Any]] | None = None) -> None:
    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    settings = _position_strategy_settings(position)
    current_price = float(price)
    if is_long:
        position["peakPrice"] = max(float(position["peakPrice"]), current_price)
    else:
        position["peakPrice"] = min(float(position["peakPrice"]), current_price)
    position["favorableExtreme"] = position["peakPrice"]
    favorable_profit = position["peakPrice"] - position["entryPrice"] if is_long else position["entryPrice"] - position["peakPrice"]
    earned_one_r = favorable_profit / max(position["risk"], EPSILON) >= settings["movingStopActivationR"]
    before_stage = position["stopManagementStage"]
    stage = _advance_stop_management_stage(
        before_stage,
        earned_one_r=earned_one_r,
        trend_continuation_confirmed=bool(context.get("trendConfirmed")),
        first_target_reached=bool(position["firstTargetReached"]),
    )
    position["stopManagementStage"] = stage
    atr = max(_number(context.get("atr")), position["entryPrice"] * 0.0004)
    breakeven_stop = position["entryPrice"] + atr * settings["breakevenBufferAtrMultiplier"] if is_long else position["entryPrice"] - atr * settings["breakevenBufferAtrMultiplier"]
    moving_stop = position.get("movingStop")
    moving_candidate = _moving_candidate_from_extreme(position, atr) or _positive(context.get("movingCandidate"))
    if stage != "INITIAL":
        moving_stop = _moving_stop_after_activation(
            moving_stop,
            moving_candidate,
            breakeven_stop,
            entry_price=position["entryPrice"],
            is_long=is_long,
        )
    position["movingStop"] = moving_stop
    technical_stop = position["initialStop"]
    if STOP_MANAGEMENT_STAGE_ORDER[stage] >= STOP_MANAGEMENT_STAGE_ORDER["STRUCTURE_TRAILING"]:
        technical_stop = _tighten(technical_stop, _positive(context.get("structureStop")), is_long)
    management_stop = technical_stop
    management_source = "STRUCTURE"
    if moving_stop is not None and _stop_is_tighter(moving_stop, technical_stop, is_long):
        management_stop = moving_stop
        management_source = "MOVING"
    if management_stop is not None:
        position["protectedStop"] = _tighten(position.get("protectedStop"), management_stop, is_long)
        position["activeStopSource"] = management_source
    if actions is not None and stage != before_stage:
        labels = {
            "BREAKEVEN": f"达到{settings['movingStopActivationR']:g}R，盈利侧直接使用极点ATR移动止损；亏损侧才使用保本保护",
            "STRUCTURE_TRAILING": "趋势延续确认，开始结构跟踪",
            "ATR_TRAILING": "第一止盈后趋势确认，开始ATR跟踪",
        }
        actions.append(_action("STOP_STAGE", position["symbol"], position["side"], price=position.get("protectedStop"), note=labels.get(stage, "更新止损管理阶段。")))


def _requires_one_minute_resolution(position: dict[str, Any], bar: dict[str, float], context: dict[str, Any]) -> bool:
    """Return whether a candle contains an intrabar order ambiguity.

    A currently active trail can tighten on any new favorable extreme, not only
    at 1R or a target.  If that favorable leg and an adverse stop touch occur
    in one candle, the OHLC cannot tell which happened first.  The caller now
    resolves this with the direction-adverse OHLC path and does not request 1m.
    """

    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    settings = _position_strategy_settings(position)
    entry = _positive(position.get("entryPrice"))
    risk = _positive(position.get("risk"))
    if entry is None:
        return False

    bar_high = float(bar["high"])
    bar_low = float(bar["low"])
    r_price = None
    if risk is not None:
        r_price = entry + risk * settings["movingStopActivationR"] if is_long else entry - risk * settings["movingStopActivationR"]
    reached_one_r = r_price is not None and _favorable_reached(bar_high if is_long else bar_low, r_price, is_long)
    target_protection = _positive(position.get("targetProtection"))
    target_one = _positive(position.get("targetOne"))
    target_two = _positive(position.get("targetTwo"))
    reached_protective_target = (
        not bool(position.get("protectiveTargetReached"))
        and target_protection is not None
        and _favorable_reached(bar_high if is_long else bar_low, target_protection, is_long)
    )
    reached_first_target = (
        not bool(position.get("firstTargetReached"))
        and target_one is not None
        and _favorable_reached(bar_high if is_long else bar_low, target_one, is_long)
    )
    reached_second_target = (
        not bool(position.get("secondTargetReached"))
        and target_two is not None
        and _favorable_reached(bar_high if is_long else bar_low, target_two, is_long)
    )
    previous_extreme = (
        _positive(position.get("peakPrice"))
        or _positive(position.get("favorableExtreme"))
        or entry
    )
    new_favorable_extreme = (
        bar_high > previous_extreme + EPSILON
        if is_long
        else bar_low < previous_extreme - EPSILON
    )
    favorable = new_favorable_extreme or reached_one_r or reached_protective_target or reached_first_target or reached_second_target
    if not favorable:
        return False

    stage = str(position.get("stopManagementStage") or "INITIAL").upper()
    stage_order = STOP_MANAGEMENT_STAGE_ORDER.get(stage, STOP_MANAGEMENT_STAGE_ORDER["INITIAL"])
    atr = max(_number(context.get("atr")), entry * 0.0004)
    stop_levels: list[float | None] = [
        _positive(position.get("protectedStop")),
        _positive(position.get("initialStop")),
    ]
    if reached_one_r or stage_order >= STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"]:
        stop_levels.append(
            entry + atr * settings["breakevenBufferAtrMultiplier"]
            if is_long
            else entry - atr * settings["breakevenBufferAtrMultiplier"]
        )
    if bool(context.get("trendConfirmed")) and (
        reached_one_r or stage_order >= STOP_MANAGEMENT_STAGE_ORDER["STRUCTURE_TRAILING"]
    ):
        stop_levels.append(_positive(context.get("structureStop")))
    if reached_one_r or stage_order >= STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"]:
        projected_extreme = _projected_favorable_extreme(position, bar_high if is_long else bar_low)
        stop_levels.append(_moving_candidate_from_extreme(position, atr, extreme=projected_extreme))
    return _adverse_extreme_touches(bar, stop_levels, is_long)


def _projected_favorable_extreme(position: dict[str, Any], price: float) -> float:
    """Include the current candle's favorable side before checking its retrace."""

    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    current_extreme = (
        _positive(position.get("peakPrice"))
        or _positive(position.get("favorableExtreme"))
        or position["entryPrice"]
    )
    return max(current_extreme, price) if is_long else min(current_extreme, price)


def _moving_candidate_from_extreme(
    position: dict[str, Any],
    atr: float,
    *,
    extreme: float | None = None,
) -> float | None:
    favorable_extreme = _positive(extreme)
    if favorable_extreme is None:
        favorable_extreme = _positive(position.get("peakPrice")) or _positive(position.get("favorableExtreme"))
    if favorable_extreme is None:
        return None
    trailing_multiplier = _position_strategy_settings(position)["trailingAtrMultiplier"]
    if str(position.get("side") or "LONG").upper() == "LONG":
        return favorable_extreme - atr * trailing_multiplier
    return favorable_extreme + atr * trailing_multiplier


def _process_position_bars(
    position: dict[str, Any],
    bars: list[dict[str, float]],
    context: dict[str, Any],
    portfolio: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    side = str(position.get("side") or "").upper()
    for bar in bars:
        if not _process_position_path(position, _bar_path(bar, side), context, portfolio, actions):
            return False
    return True


def _entry_failure_evaluation_bar(
    entry_path: list[dict[str, float]],
    reference_bar: dict[str, float],
) -> dict[str, float]:
    """Build the post-entry part of a 15m candle for confirmation review."""

    if not entry_path:
        return reference_bar
    first = entry_path[0]
    if int(first.get("openTime") or 0) == int(reference_bar.get("openTime") or 0):
        return first
    path = entry_path
    return {
        "openTime": reference_bar["openTime"],
        "closeTime": reference_bar["closeTime"],
        "open": float(first["open"]),
        "high": max(float(item["high"]) for item in path),
        "low": min(float(item["low"]) for item in path),
        "close": float(path[-1]["close"]),
        "volume": float(reference_bar.get("volume") or 0),
    }


def _process_entry_failure(
    position: dict[str, Any],
    bar: dict[str, float],
    context: dict[str, Any],
    portfolio: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    """Exit only a confirmed, early failure; ordinary overlap remains a hold."""

    settings = _position_strategy_settings(position)
    limit = int(settings["entryFailureExitBars"])
    if limit <= 0 or bool(position.get("protectiveTargetReached")) or bool(position.get("firstTargetReached")):
        return True
    close_time = int(bar.get("closeTime") or 0)
    if close_time and close_time == int(position.get("entryFailureLastCloseTime") or 0):
        return True
    observed = int(position.get("entryFailureBarsObserved") or 0)
    if observed >= limit:
        return True
    position["entryFailureLastCloseTime"] = close_time
    observed += 1
    position["entryFailureBarsObserved"] = observed

    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    atr = max(_number(context.get("atr")), float(position["entryPrice"]) * 0.0004)
    span = max(float(bar["high"]) - float(bar["low"]), atr * 0.15)
    body = abs(float(bar["close"]) - float(bar["open"]))
    close_position = (float(bar["close"]) - float(bar["low"])) / span
    directional = (
        float(bar["close"]) < float(bar["open"]) and close_position <= 0.35
        if is_long
        else float(bar["close"]) > float(bar["open"]) and close_position >= 0.65
    )
    boundary = _positive(position.get("entryZoneLow")) if is_long else _positive(position.get("entryZoneHigh"))
    boundary = boundary or _positive(position.get("entryTrigger")) or float(position["entryPrice"])
    crossed_back = float(bar["close"]) < boundary if is_long else float(bar["close"]) > boundary
    strong_failure = directional and body >= atr * settings["entryFailureBodyAtrMultiplier"] and crossed_back
    warning = position.get("entryFailureWarning") if isinstance(position.get("entryFailureWarning"), dict) else None
    follow_through = bool(warning) and (
        float(bar["close"]) < float(warning["low"]) if is_long else float(bar["close"]) > float(warning["high"])
    )
    if strong_failure and (limit == 1 or follow_through):
        _close_position(
            position,
            float(bar["close"]),
            portfolio,
            actions,
            reason="触发后早期强反向K重新穿回触发区，并有后续跟随，按失败退出纪律离场",
        )
        portfolio["entryFailureExitCount"] = int(portfolio.get("entryFailureExitCount") or 0) + 1
        return False
    if strong_failure and observed < limit:
        position["entryFailureWarning"] = {
            "low": float(bar["low"]),
            "high": float(bar["high"]),
            "closeTime": close_time,
        }
        execution_interval = DIRECT_MODEL_EXECUTION_INTERVAL if _strategy_engine(settings) == "MODEL" else ("5m" if _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM else "15m")
        actions.append(_action("FAILURE_WARNING", position["symbol"], position["side"], price=bar["close"], note=f"入场后强反向{execution_interval} K重新穿回触发区，等待下一根{execution_interval}跟随确认。"))
    elif warning:
        # A normal overlap bar is explicitly not a failure confirmation.
        position["entryFailureWarning"] = None
    if observed >= limit:
        position["entryFailureWarning"] = None
    return True


def _process_position_path(
    position: dict[str, Any],
    points: list[float],
    context: dict[str, Any],
    portfolio: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    if not points:
        return True
    current = float(points[0])
    position["currentPrice"] = current
    _refresh_management(position, context, current, actions)
    if _stop_hit(position, current):
        _close_position(position, current, portfolio, actions, reason="开盘已越过当前有效止损")
        return False
    if not _process_favorable_events_at_price(position, current, context, portfolio, actions):
        return False
    for destination in points[1:]:
        destination = float(destination)
        while not math.isclose(current, destination, rel_tol=0.0, abs_tol=EPSILON):
            event = _next_position_event(position, current, destination)
            if event is None:
                position["currentPrice"] = destination
                _refresh_management(position, context, destination, actions)
                current = destination
                break
            kind, event_price = event
            position["currentPrice"] = event_price
            if kind == "STOP":
                _close_position(position, event_price, portfolio, actions, reason="触及当前有效止损")
                return False
            event_kinds = _events_at_price(position, event_price)
            if kind not in event_kinds:
                event_kinds.insert(0, kind)
            _refresh_management(position, context, event_price, actions)
            for event_kind in event_kinds:
                if event_kind == "R_ONE":
                    continue
                if event_kind == "TARGET_PROTECTION":
                    _take_profit(
                        position,
                        event_price,
                        _target_exit_quantity(position, protective=True),
                        portfolio,
                        actions,
                        context=context,
                        protective=True,
                    )
                if event_kind == "TARGET_ONE":
                    _take_profit(position, event_price, _target_exit_quantity(position, first=True), portfolio, actions, context=context, first=True)
                elif event_kind == "TARGET_TWO":
                    _take_profit(position, event_price, _target_exit_quantity(position, second=True), portfolio, actions, context=context, second=True)
                if not _position_alive(position):
                    return False
            current = event_price
        if not _position_alive(position):
            return False
    return True


def _next_position_event(position: dict[str, Any], current: float, destination: float) -> tuple[str, float] | None:
    if math.isclose(current, destination, rel_tol=0.0, abs_tol=EPSILON):
        return None
    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    ascending = destination > current
    events: list[tuple[str, float]] = []
    if (is_long and not ascending) or (not is_long and ascending):
        stop = (
            _positive(position.get("protectedStop"))
            or _positive(position.get("initialStop"))
        )
        if stop is not None and _between(stop, current, destination):
            events.append(("STOP", stop))
    if (is_long and ascending) or (not is_long and not ascending):
        entry = _positive(position.get("entryPrice"))
        risk = _positive(position.get("risk"))
        activation_r = _position_strategy_settings(position)["movingStopActivationR"]
        stage = str(position.get("stopManagementStage") or "INITIAL").upper()
        stage_order = STOP_MANAGEMENT_STAGE_ORDER.get(stage, STOP_MANAGEMENT_STAGE_ORDER["INITIAL"])
        r_price = (
            entry + risk * activation_r
            if is_long and entry is not None and risk is not None
            else entry - risk * activation_r
            if entry is not None and risk is not None
            else None
        )
        if r_price is not None and stage_order < STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"] and _between(r_price, current, destination):
            events.append(("R_ONE", r_price))
        target_protection = _positive(position.get("targetProtection"))
        target_one = _positive(position.get("targetOne"))
        target_two = _positive(position.get("targetTwo"))
        if not bool(position.get("protectiveTargetReached")) and target_protection is not None and _between(target_protection, current, destination):
            events.append(("TARGET_PROTECTION", target_protection))
        if not bool(position.get("firstTargetReached")) and target_one is not None and _between(target_one, current, destination):
            events.append(("TARGET_ONE", target_one))
        if not bool(position.get("secondTargetReached")) and target_two is not None and _between(target_two, current, destination):
            events.append(("TARGET_TWO", target_two))
    if not events:
        return None
    return min(events, key=lambda item: item[1]) if ascending else max(events, key=lambda item: item[1])


def _process_favorable_events_at_price(
    position: dict[str, Any],
    price: float,
    context: dict[str, Any],
    portfolio: dict[str, Any],
    actions: list[dict[str, Any]],
) -> bool:
    """Fill thresholds already crossed by a bar opening gap before its path."""

    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    while _position_alive(position):
        entry = _positive(position.get("entryPrice"))
        risk = _positive(position.get("risk"))
        activation_r = _position_strategy_settings(position)["movingStopActivationR"]
        events: list[tuple[str, float]] = []
        if entry is not None and risk is not None:
            r_price = entry + risk * activation_r if is_long else entry - risk * activation_r
            stage = str(position.get("stopManagementStage") or "INITIAL").upper()
            if STOP_MANAGEMENT_STAGE_ORDER.get(stage, 0) < STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"] and _favorable_reached(price, r_price, is_long):
                events.append(("R_ONE", r_price))
        target_protection = _positive(position.get("targetProtection"))
        target_one = _positive(position.get("targetOne"))
        target_two = _positive(position.get("targetTwo"))
        if not bool(position.get("protectiveTargetReached")) and target_protection is not None and _favorable_reached(price, target_protection, is_long):
            events.append(("TARGET_PROTECTION", target_protection))
        if not bool(position.get("firstTargetReached")) and target_one is not None and _favorable_reached(price, target_one, is_long):
            events.append(("TARGET_ONE", target_one))
        if not bool(position.get("secondTargetReached")) and target_two is not None and _favorable_reached(price, target_two, is_long):
            events.append(("TARGET_TWO", target_two))
        if not events:
            return True
        kind, event_price = min(events, key=lambda item: item[1]) if is_long else max(events, key=lambda item: item[1])
        position["currentPrice"] = event_price
        _refresh_management(position, context, event_price, actions)
        if kind == "R_ONE":
            continue
        if kind == "TARGET_PROTECTION":
            _take_profit(position, event_price, _target_exit_quantity(position, protective=True), portfolio, actions, context=context, protective=True)
        elif kind == "TARGET_ONE":
            _take_profit(position, event_price, _target_exit_quantity(position, first=True), portfolio, actions, context=context, first=True)
        else:
            _take_profit(position, event_price, _target_exit_quantity(position, second=True), portfolio, actions, context=context, second=True)
    return False


def _events_at_price(position: dict[str, Any], price: float) -> list[str]:
    """Return favorable events sharing one price, in management order."""

    events: list[str] = []
    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    entry = _positive(position.get("entryPrice"))
    risk = _positive(position.get("risk"))
    if entry is not None and risk is not None:
        activation_r = _position_strategy_settings(position)["movingStopActivationR"]
        r_price = entry + risk * activation_r if is_long else entry - risk * activation_r
        stage = str(position.get("stopManagementStage") or "INITIAL").upper()
        if STOP_MANAGEMENT_STAGE_ORDER.get(stage, 0) < STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"] and _price_equal(price, r_price):
            events.append("R_ONE")
    target_protection = _positive(position.get("targetProtection"))
    target_one = _positive(position.get("targetOne"))
    target_two = _positive(position.get("targetTwo"))
    if not bool(position.get("protectiveTargetReached")) and target_protection is not None and _price_equal(price, target_protection):
        events.append("TARGET_PROTECTION")
    if not bool(position.get("firstTargetReached")) and target_one is not None and _price_equal(price, target_one):
        events.append("TARGET_ONE")
    if not bool(position.get("secondTargetReached")) and target_two is not None and _price_equal(price, target_two):
        events.append("TARGET_TWO")
    return events


def _target_exit_quantity(
    position: dict[str, Any],
    *,
    protective: bool = False,
    first: bool = False,
    second: bool = False,
) -> float:
    settings = _position_strategy_settings(position)
    has_protective_target = _positive(position.get("targetProtection")) is not None
    protective_ratio = _number(position.get("protectiveTakeProfitRatio"))
    if has_protective_target and protective_ratio <= EPSILON:
        protective_ratio = settings["protectiveTakeProfitRatio"]
    first_ratio = _number(position.get("firstTakeProfitRatio")) or settings["firstTakeProfitRatio"]
    second_ratio = _number(position.get("secondTakeProfitRatio")) or settings["secondTakeProfitRatio"]
    if _positive(position.get("targetTwo")) is None:
        # Without a confirmed extension platform the first structural target
        # is the final fixed target. Promote its cumulative allocation to the
        # second configured boundary, leaving the balance for
        # the moving-stop runner.
        first_ratio = second_ratio
    if protective:
        ratio = protective_ratio if has_protective_target else 0.0
    elif first:
        ratio = first_ratio - (
            protective_ratio
            if _positive(position.get("targetProtection")) is not None
            else 0
        )
    elif second:
        ratio = second_ratio - first_ratio
    else:
        return 0.0
    return float(position["initialQuantity"]) * ratio / 100


def _take_profit(
    position: dict[str, Any],
    price: float,
    requested_quantity: float,
    portfolio: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    protective: bool = False,
    first: bool = False,
    second: bool = False,
) -> None:
    quantity = min(position["quantity"], max(0.0, requested_quantity))
    if quantity <= EPSILON:
        return
    pnl, released_margin = _reduce_position(position, price, quantity, portfolio)
    if protective:
        position["protectiveTargetReached"] = True
        portfolio["protectiveTargetCount"] = portfolio.get("protectiveTargetCount", 0) + 1
        label = f"近端保护目标累计{_position_strategy_settings(position)['protectiveTakeProfitRatio']:g}%"
        target_stage = "PROTECTIVE"
    elif first:
        position["firstTargetReached"] = True
        portfolio["firstTargetCount"] += 1
        label = f"第一止盈累计{_position_strategy_settings(position)['firstTakeProfitRatio']:g}%"
        target_stage = "FIRST"
    else:
        position["secondTargetReached"] = True
        position["runner"] = True
        portfolio["secondTargetCount"] += 1
        label = f"第二止盈累计{_position_strategy_settings(position)['secondTakeProfitRatio']:g}%"
        target_stage = "SECOND"
    # Targets only unlock the next state when the same completed-bar context
    # confirms continuation. Do not invent a bullish/bearish confirmation at a
    # target touch.
    _refresh_management(position, context, price, actions)
    actions.append(_action("TAKE_PROFIT", position["symbol"], position["side"], price=price, quantity=quantity, pnl=pnl, target_stage=target_stage, note=f"{label}，释放保证金 {_round(released_margin)} USDT。"))


def _close_position(position: dict[str, Any], price: float, portfolio: dict[str, Any], actions: list[dict[str, Any]], *, reason: str) -> None:
    quantity = position["quantity"]
    pnl, released_margin = _reduce_position(position, price, quantity, portfolio)
    portfolio["tradeCount"] += 1
    portfolio["stoppedCount"] += 1
    actions.append(_action("STOP", position["symbol"], position["side"], price=price, quantity=quantity, pnl=pnl, note=f"{reason}，释放保证金 {_round(released_margin)} USDT。"))


def _reduce_position(position: dict[str, Any], price: float, quantity: float, portfolio: dict[str, Any]) -> tuple[float, float]:
    before_quantity = position["quantity"]
    ratio = min(1.0, quantity / max(before_quantity, EPSILON))
    released_margin = position["margin"] * ratio
    is_long = position["side"] == "LONG"
    pnl = (price - position["entryPrice"]) * quantity if is_long else (position["entryPrice"] - price) * quantity
    position["quantity"] = max(0.0, before_quantity - quantity)
    position["margin"] = max(0.0, position["margin"] - released_margin)
    position["realizedPnl"] += pnl
    portfolio["cash"] += released_margin + pnl
    portfolio["dailyRealizedPnl"] = _number(portfolio.get("dailyRealizedPnl")) + pnl
    return pnl, released_margin


def _entry_triggered(candidate: dict[str, Any], bar: dict[str, float]) -> bool:
    is_limit = str(candidate.get("entryType") or "").upper() == ENTRY_TYPE_LIMIT_RETEST
    if is_limit:
        return bar["low"] <= candidate["trigger"] if candidate["side"] == "LONG" else bar["high"] >= candidate["trigger"]
    return bar["high"] >= candidate["trigger"] if candidate["side"] == "LONG" else bar["low"] <= candidate["trigger"]


def _entry_requires_one_minute_resolution(
    candidate: dict[str, Any],
    bar: dict[str, float],
    context: dict[str, Any] | None = None,
) -> bool:
    """Check a new-entry candle for an intrabar order conflict.

    The entry is allowed only after its trigger is crossed.  Any adverse move
    through the trigger can therefore be either pre-entry noise or a post-entry
    stop, and cannot be resolved from the source candle's OHLC alone.  Replay
    applies its direction-adverse OHLC policy instead of requesting 1m.  The
    same applies when a favorable leg can promote a protection line before a
    retracement.
    """

    is_long = str(candidate.get("side") or "LONG").upper() == "LONG"
    if str(candidate.get("entryType") or "").upper() == ENTRY_TYPE_LIMIT_RETEST:
        # The low/high touch that fills a retest limit can occur before or
        # after a structural breach inside one 15m candle. Always use the
        # observed minute ordering when it is available.
        return True
    settings = _strategy_settings(candidate.get("strategySettings"))
    entry = _positive(candidate.get("trigger"))
    initial_stop = _positive(candidate.get("initialStop"))
    target_protection = _positive(candidate.get("targetProtection"))
    target_one = _positive(candidate.get("targetOne"))
    target_two = _positive(candidate.get("targetTwo"))
    if entry is None or initial_stop is None:
        return False

    bar_high = float(bar["high"])
    bar_low = float(bar["low"])
    if not _favorable_reached(bar_high if is_long else bar_low, entry, is_long):
        return False

    # A move back through the trigger is the exact case where a 15m OHLC path
    # cannot prove whether the stop was touched before or after entry.  Include
    # position-risk stops when a caller has already calculated one.
    adverse_crossed_entry = (
        bar_low < entry - EPSILON
        if is_long
        else bar_high > entry + EPSILON
    )
    risk = abs(entry - initial_stop)
    if risk <= EPSILON:
        return adverse_crossed_entry
    r_one = entry + risk * settings["movingStopActivationR"] if is_long else entry - risk * settings["movingStopActivationR"]
    favorable_price = bar_high if is_long else bar_low
    reached_one_r = _favorable_reached(favorable_price, r_one, is_long)
    reached_protective_target = target_protection is not None and _favorable_reached(favorable_price, target_protection, is_long)
    reached_first_target = target_one is not None and _favorable_reached(favorable_price, target_one, is_long)
    reached_second_target = target_two is not None and _favorable_reached(favorable_price, target_two, is_long)

    context = context or {}
    atr = max(_number(context.get("atr")), entry * 0.0004)
    breakeven_stop = entry + atr * settings["breakevenBufferAtrMultiplier"] if is_long else entry - atr * settings["breakevenBufferAtrMultiplier"]
    potential_stops: list[float | None] = [
        initial_stop,
    ]
    if reached_one_r or reached_protective_target or reached_first_target or reached_second_target:
        potential_stops.append(breakeven_stop)
    if bool(context.get("trendConfirmed")):
        potential_stops.append(_positive(context.get("structureStop")))
    if reached_one_r or reached_first_target or reached_second_target:
        projected_extreme = bar_high if is_long else bar_low
        potential_stops.append(
            projected_extreme - atr * settings["trailingAtrMultiplier"]
            if is_long
            else projected_extreme + atr * settings["trailingAtrMultiplier"]
        )

    return adverse_crossed_entry or _adverse_extreme_touches(bar, potential_stops, is_long)


def _entry_path(candidate: dict[str, Any], bars: list[dict[str, float]]) -> list[dict[str, float]] | None:
    side = str(candidate.get("side") or "").upper()
    is_long = side == "LONG"
    is_limit = str(candidate.get("entryType") or "").upper() == ENTRY_TYPE_LIMIT_RETEST
    if side not in {"LONG", "SHORT"}:
        return None
    for index, bar in enumerate(bars):
        points = _bar_path(bar, side)
        entry_price = candidate["trigger"]
        stop = _positive(candidate.get("initialStop"))
        if is_limit and is_long and points[0] <= entry_price:
            if stop is not None and points[0] <= stop:
                return None
            return [{**bar, "open": points[0], "high": max(points), "low": min(points), "close": points[-1], "_replayPath": points}] + bars[index + 1:]
        if is_limit and not is_long and points[0] >= entry_price:
            if stop is not None and points[0] >= stop:
                return None
            return [{**bar, "open": points[0], "high": max(points), "low": min(points), "close": points[-1], "_replayPath": points}] + bars[index + 1:]
        if not is_limit and is_long and points[0] >= entry_price:
            return [{**bar, "open": points[0], "high": max(points), "low": min(points), "close": points[-1], "_replayPath": points}] + bars[index + 1:]
        if not is_limit and not is_long and points[0] <= entry_price:
            return [{**bar, "open": points[0], "high": max(points), "low": min(points), "close": points[-1], "_replayPath": points}] + bars[index + 1:]
        for point_index, (left, right) in enumerate(zip(points, points[1:])):
            if is_limit:
                crosses = left > entry_price >= right if is_long else left < entry_price <= right
            else:
                crosses = left < entry_price <= right if is_long else left > entry_price >= right
            if not crosses:
                continue
            remainder = [entry_price, right, *points[point_index + 2:]]
            # Preserve the actual remaining OHLC traversal. Reconstructing a
            # synthetic candle and deriving a fresh OHLC path can reverse its
            # high/low sequence after entry and invert a stop/target result.
            synthetic = {
                **bar,
                "open": remainder[0],
                "high": max(remainder),
                "low": min(remainder),
                "close": remainder[-1],
                "_replayPath": remainder,
            }
            return [synthetic] + bars[index + 1:]
    return None


def _analysis_frames(
    dataset: dict[str, Any],
    micro_frame: dict[str, Any] | None,
    as_of: int,
    *,
    strategy_mode: object = STRATEGY_MODE_MIDLINE,
    require_micro: bool = False,
) -> dict[str, list[dict[str, float]]] | None:
    macro = _macro_frames(dataset, as_of)
    if macro is None:
        return None
    mode = _strategy_mode(strategy_mode)
    # Short-term mode makes 5m a required execution frame. A macro-only result
    # must never be passed to the shared analyzer because that would silently
    # turn a short-term request into a midline plan.
    # Direct MODEL replay uses 5m as a timing stream only for SHORT/SWING. A
    # POSITION checkpoint is managed on 1h but still receives the complete
    # four-timeframe input tensor from the model caller.
    if mode == STRATEGY_MODE_SHORT_TERM or require_micro:
        source_frame = micro_frame
        if not isinstance(source_frame, dict):
            source_frame = (dataset.get("frames") or {}).get("5m")
        if not isinstance(source_frame, dict):
            return None
        micro = _bars_until(source_frame, as_of)
        if len(micro) < MIN_SHORT_TERM_READING_BARS:
            return None
        return {**macro, "5m": micro}
    # 5m is optional for plan completeness in midline mode. Callers that
    # already loaded it get the timing layer; macro-only callers avoid a
    # needless per-symbol read while following the live strategy's ARMED
    # decision.
    if isinstance(micro_frame, dict):
        micro = _bars_until(micro_frame, as_of)
        if len(micro) >= HISTORY_WARMUP_BARS:
            return {**macro, "5m": micro}
    return macro


def _macro_frames(dataset: dict[str, Any], as_of: int) -> dict[str, list[dict[str, float]]] | None:
    frames = {interval: _bars_until(dataset["frames"][interval], as_of) for interval in ("15m", "1h", "4h")}
    return frames if all(len(bars) >= HISTORY_WARMUP_BARS for bars in frames.values()) else None


def _bars_until(frame: dict[str, Any], as_of: int) -> list[dict[str, float]]:
    close_times = _frame_close_times(frame)
    index = bisect_right(close_times, as_of)
    # Direct-plan checkpoints consume a 288-bar 5m sequence.  The replay
    # adapter must retain at least that many causal bars for every stream;
    # retaining the former 180 bars silently made every current model input
    # incomplete and was then reported as MODEL_INVALID.
    start = max(0, index - 288)
    return [_frame_bar(frame, item_index) for item_index in range(start, index)]


def _bar_at_close(frame: dict[str, Any], close_time: int) -> dict[str, float] | None:
    close_times = _frame_close_times(frame)
    index = bisect_right(close_times, close_time) - 1
    if index < 0 or int(close_times[index]) != int(close_time):
        return None
    return _frame_bar(frame, index)


def _mark_positions_to_close(
    history: dict[str, Any],
    positions: list[dict[str, Any]],
    close_time: int,
    *,
    interval: str = "15m",
) -> None:
    for position in positions:
        dataset = history["items"].get(position["symbol"])
        if not dataset:
            continue
        bar = _bar_at_close(dataset["frames"][interval], close_time)
        if bar is not None:
            position["currentPrice"] = bar["close"]


def _frame(bars: list[dict[str, float]]) -> dict[str, Any]:
    return {"bars": bars, "closeTimes": [int(bar["closeTime"]) for bar in bars]}


def _compact_frame(bars: list[dict[str, float]]) -> dict[str, Any]:
    """Keep the multi-contract replay corpus compact while preserving its bar API."""

    return {
        "compact": True,
        "openTimes": array("q", (int(bar["openTime"]) for bar in bars)),
        "closeTimes": array("q", (int(bar["closeTime"]) for bar in bars)),
        "opens": array("d", (float(bar["open"]) for bar in bars)),
        "highs": array("d", (float(bar["high"]) for bar in bars)),
        "lows": array("d", (float(bar["low"]) for bar in bars)),
        "closes": array("d", (float(bar["close"]) for bar in bars)),
        "volumes": array("d", (float(bar["volume"]) for bar in bars)),
    }


def _frame_close_times(frame: dict[str, Any]):
    return frame.get("closeTimes") or []


def _frame_bar(frame: dict[str, Any], index: int) -> dict[str, float]:
    if not frame.get("compact"):
        return frame["bars"][index]
    return {
        "openTime": int(frame["openTimes"][index]),
        "closeTime": int(frame["closeTimes"][index]),
        "open": float(frame["opens"][index]),
        "high": float(frame["highs"][index]),
        "low": float(frame["lows"][index]),
        "close": float(frame["closes"][index]),
        "volume": float(frame["volumes"][index]),
    }


def _frame_bars_between(frame: dict[str, Any], start_time: int, end_time: int) -> list[dict[str, float]]:
    close_times = _frame_close_times(frame)
    start = bisect_left(close_times, int(start_time))
    end = bisect_left(close_times, int(end_time))
    return [_frame_bar(frame, item_index) for item_index in range(start, end)]


def _timeline_record(close_time: int, actions: list[dict[str, Any]], portfolio: dict[str, Any]) -> dict[str, Any]:
    used_margin = sum(position["margin"] for position in portfolio["positions"])
    equity = _portfolio_equity(portfolio)
    return {
        "time": close_time,
        "actions": actions,
        "positions": [_position_snapshot(position) for position in portfolio["positions"]],
        "positionCount": len(portfolio["positions"]),
        "portfolioRisk": _round(sum(_position_open_risk(position) for position in portfolio["positions"])),
        "dailyRealizedPnl": _round(portfolio.get("dailyRealizedPnl")),
        "dailyRiskState": deepcopy(portfolio.get("dailyRiskState")),
        "availableBalance": _round(portfolio["cash"]),
        "usedMargin": _round(used_margin),
        "equity": _round(equity),
        "totalPnl": _round(equity - portfolio["initialBalance"]),
        "entryScan": deepcopy(portfolio.get("_currentEntryScan")),
        "pendingEntryCount": len(portfolio.get("pending") or {}),
    }


def _replay_result_snapshot(
    timeline: list[dict[str, Any]],
    portfolio: dict[str, Any],
    start_time: int,
    end_time: int,
    processed_slices: int,
    total_slices: int,
    *,
    as_of_time: int | None = None,
    market_count: int | None = None,
    partial: bool,
    timeframe: str = "15m",
) -> dict[str, Any]:
    """Build the result visible while a replay is still running.

    Rows already appended to ``timeline`` are immutable snapshots, so a
    shallow list copy is enough here.  Avoiding a deep copy on every 15m bar
    keeps the replay responsive; ``_job_snapshot`` deep-copies the current
    task only when a client polls it.
    """

    final_equity = _portfolio_equity(portfolio)
    execution_settings = _portfolio_execution_settings(portfolio)
    strategy_settings = _strategy_settings(portfolio.get("_strategySettings"))
    initial_balance = float(portfolio.get("initialBalance") or execution_settings["initialBalance"])
    position_margin_percent = execution_settings["positionMarginFraction"] * 100
    runner_percent = max(0.0, 100.0 - float(strategy_settings["secondTakeProfitRatio"]))
    summary = {
        "openedCount": portfolio["openedCount"],
        "tradeCount": portfolio["tradeCount"],
        "stoppedCount": portfolio["stoppedCount"],
        "protectiveTargetCount": portfolio["protectiveTargetCount"],
        "firstTargetCount": portfolio["firstTargetCount"],
        "secondTargetCount": portfolio["secondTargetCount"],
        "openPositions": len(portfolio["positions"]),
        "oneMinuteChecks": portfolio["oneMinuteChecks"],
        "oneMinuteFallbacks": portfolio["oneMinuteFallbacks"],
        "conservativeIntrabarPaths": portfolio.get("conservativeIntrabarPaths", 0),
        "pendingEntryCount": len(portfolio.get("pending") or {}),
        "pendingExpiredCount": portfolio.get("pendingExpiredCount", 0),
        "pendingConfirmedCount": portfolio.get("pendingConfirmedCount", 0),
        "entryBlockedByRiskCount": portfolio.get("entryBlockedByRiskCount", 0),
        "entryFailureExitCount": portfolio.get("entryFailureExitCount", 0),
        "assumptions": [
            f"初始资金{initial_balance:g} USDT；不计手续费、资金费、滑点与强平。",
            f"每个管理中仓位最多按当时总权益的{position_margin_percent:g}%占用保证金；第二止盈后的剩余{runner_percent:g}%作为runner，不占新的仓位名额。",
            f"杠杆按账户风险预算向下取整，最高{execution_settings['maxLeverage']:g}x；因上限截断时，实际结构止损风险低于账户风险预算。",
            f"回测不调用1m；同一根{timeframe}无法由OHLC确认止盈/R与止损先后时，做多按开盘-低点-高点-收盘、做空按开盘-高点-低点-收盘处理，采用方向不利的保守路径。",
        ],
    }
    result = {
        "initialBalance": _round(initial_balance),
        "finalAvailableBalance": _round(portfolio["cash"]),
        "finalEquity": _round(final_equity),
        "totalPnl": _round(final_equity - initial_balance),
        "totalPnlPercent": _round((final_equity / initial_balance - 1) * 100),
        "startTime": start_time,
        "endTime": end_time,
        "asOfTime": as_of_time,
        "timeframe": str(timeframe or "15m"),
        "strategyMode": str(portfolio.get("_strategyMode") or STRATEGY_MODE_MIDLINE),
        "strategyEngine": _strategy_engine(strategy_settings),
        "timeline": list(timeline),
        "summary": summary,
        "isPartial": bool(partial),
        "processedSlices": max(0, int(processed_slices)),
        "totalSlices": max(0, int(total_slices)),
        "marketCount": market_count,
        "modelSelection": str(portfolio.get("_modelSelection") or MODEL_SELECTION_OFF),
        "modelBranch": str(portfolio.get("_modelBranch") or "BEST"),
        "modelRunId": portfolio.get("_modelRunId"),
    }
    if not partial:
        result["performance"] = _replay_performance(result)
    return result


def _position_snapshot(position: dict[str, Any]) -> dict[str, Any]:
    current = position["currentPrice"]
    is_long = position["side"] == "LONG"
    unrealized = (current - position["entryPrice"]) * position["quantity"] if is_long else (position["entryPrice"] - current) * position["quantity"]
    total_pnl = unrealized + position["realizedPnl"]
    initial_margin = _positive(position.get("initialMargin")) or _positive(position.get("margin"))
    pnl_percent = total_pnl / initial_margin * 100 if initial_margin is not None else None
    return {
        "symbol": position["symbol"],
        "side": position["side"],
        "quantity": _round(position["quantity"]),
        "entryPrice": _round(position["entryPrice"]),
        "currentPrice": _round(current),
        "margin": _round(position["margin"]),
        "initialMargin": _round(initial_margin),
        "leverage": position["leverage"],
        "unrealizedPnl": _round(unrealized),
        "realizedPnl": _round(position["realizedPnl"]),
        "totalPnl": _round(total_pnl),
        "pnlPercent": _round(pnl_percent),
        "initialStop": _round(position["initialStop"]),
        "positionRiskStop": _round(position["positionRiskStop"]),
        "favorableExtreme": _round(position.get("favorableExtreme") or position.get("peakPrice")),
        "movingStop": _round(position["movingStop"]) if position["movingStop"] is not None else None,
        "activeStop": _round(position["protectedStop"]),
        "activeStopSource": position["activeStopSource"],
        "stopManagementStage": position["stopManagementStage"],
        "targetProtection": _round(position.get("targetProtection")),
        "targetOne": _round(position["targetOne"]),
        "targetTwo": _round(position["targetTwo"]),
        "protectiveTargetReached": position.get("protectiveTargetReached", False),
        "firstTargetReached": position["firstTargetReached"],
        "secondTargetReached": position["secondTargetReached"],
        "runner": position["runner"],
    }


def _portfolio_equity(portfolio: dict[str, Any]) -> float:
    total = float(portfolio["cash"])
    for position in portfolio["positions"]:
        current = float(position.get("currentPrice") or position["entryPrice"])
        pnl = (current - position["entryPrice"]) * position["quantity"] if position["side"] == "LONG" else (position["entryPrice"] - current) * position["quantity"]
        total += position["margin"] + pnl
    return total


def _trading_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=LOCAL_TIMEZONE).date().isoformat()


def _roll_daily_risk_state(portfolio: dict[str, Any], timestamp_ms: int) -> None:
    """Reset only the daily risk ledger at a Shanghai calendar boundary."""

    day = _trading_day(timestamp_ms)
    state = portfolio.get("dailyRiskState") if isinstance(portfolio.get("dailyRiskState"), dict) else {}
    if state.get("day") == day:
        return
    portfolio["dailyRiskState"] = {
        "day": day,
        "startEquity": _portfolio_equity(portfolio),
    }
    portfolio["dailyRealizedPnl"] = 0.0


def _managed_position_count(positions: list[dict[str, Any]]) -> int:
    return sum(1 for position in positions if not position.get("runner"))


def _stop_hit(position: dict[str, Any], price: float) -> bool:
    stop = _positive(position.get("protectedStop"))
    if stop is None:
        return False
    return price <= stop if str(position.get("side") or "LONG").upper() == "LONG" else price >= stop


def _position_alive(position: dict[str, Any]) -> bool:
    return position["quantity"] > EPSILON


def _bar_path(bar: dict[str, float], side: str | None = None) -> list[float]:
    replay_path = bar.get("_replayPath")
    if isinstance(replay_path, list) and len(replay_path) >= 2:
        normalized = [float(value) for value in replay_path]
        if all(math.isfinite(value) for value in normalized):
            return normalized
    direction = str(side or "").upper()
    if direction == "LONG":
        return [bar["open"], bar["low"], bar["high"], bar["close"]]
    if direction == "SHORT":
        return [bar["open"], bar["high"], bar["low"], bar["close"]]
    raise ValueError("OHLC回测路径必须明确持仓方向")


def _between(value: float, start: float, end: float) -> bool:
    if end > start:
        return start + EPSILON < value <= end + EPSILON
    return end - EPSILON <= value < start - EPSILON


def _price_equal(left: float, right: float) -> bool:
    tolerance = max(EPSILON, max(abs(float(left)), abs(float(right))) * 1e-9)
    return abs(float(left) - float(right)) <= tolerance


def _favorable_reached(price: float, level: float, is_long: bool) -> bool:
    return float(price) >= float(level) - EPSILON if is_long else float(price) <= float(level) + EPSILON


def _adverse_extreme_touches(
    bar: dict[str, float],
    stop_levels: list[float | None],
    is_long: bool,
) -> bool:
    usable = [float(level) for level in stop_levels if _positive(level) is not None]
    if not usable:
        return False
    boundary = max(usable) if is_long else min(usable)
    if is_long:
        return float(bar["low"]) <= boundary + EPSILON
    return float(bar["high"]) >= boundary - EPSILON


def _action(kind: str, symbol: str, side: str, *, price: float | None = None, quantity: float | None = None, pnl: float | None = None, target_stage: str | None = None, note: str = "", count: int | None = None) -> dict[str, Any]:
    return {
        "type": kind,
        "symbol": symbol,
        "side": side,
        "price": _round(price) if price is not None else None,
        "quantity": _round(quantity) if quantity is not None else None,
        "pnl": _round(pnl) if pnl is not None else None,
        "targetStage": str(target_stage or "").upper() or None,
        "note": note,
        "count": count,
    }


def _update_job(
    job_id: str,
    *,
    status: str | None = None,
    phase: str | None = None,
    message: str | None = None,
    current_symbol: str | None = None,
    completed: int | None = None,
    total: int | None = None,
    history_cached: bool | None = None,
    history_ready: bool | None = None,
    history_missing_ranges: int | None = None,
    downloaded_bars: int | None = None,
    result: dict[str, Any] | None = None,
    replay_result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if status is not None:
            job["status"] = status
        if error is not None:
            job["error"] = error
        if result is not None:
            job["result"] = result
        if replay_result is not None:
            job["replayResult"] = replay_result
        progress = job["progress"]
        if phase is not None:
            progress["phase"] = phase
        if message is not None:
            progress["message"] = message
        if current_symbol is not None:
            progress["currentSymbol"] = current_symbol
        if completed is not None:
            progress["completed"] = completed
        if total is not None:
            progress["total"] = total
        if history_cached is not None:
            progress["historyCached"] = history_cached
        if history_ready is not None:
            progress["historyReady"] = history_ready
        if history_missing_ranges is not None:
            progress["historyMissingRanges"] = max(0, int(history_missing_ranges))
        if downloaded_bars is not None:
            progress["downloadedBars"] = max(0, int(downloaded_bars))
        job["updatedAt"] = _now_ms()


def _job_snapshot(job: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a job without recursively copying every historical candle row.

    Replay rows are immutable after ``_timeline_record`` returns.  The replay
    result itself is published with a fresh timeline list on every slice, so
    copying the list and the small metadata dictionaries is sufficient for a
    safe API snapshot.  A recursive copy here made each poll hold
    ``_jobs_lock`` for longer as the timeline grew, which progressively slowed
    the replay.
    """

    if not job:
        return {}
    snapshot = dict(job)
    snapshot["progress"] = dict(job.get("progress") or {})
    for field in ("result", "replayResult"):
        payload = job.get(field)
        if not isinstance(payload, dict):
            continue
        payload_snapshot = dict(payload)
        timeline = payload.get("timeline")
        if isinstance(timeline, list):
            payload_snapshot["timeline"] = list(timeline)
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary_snapshot = dict(summary)
            assumptions = summary.get("assumptions")
            if isinstance(assumptions, list):
                summary_snapshot["assumptions"] = list(assumptions)
            payload_snapshot["summary"] = summary_snapshot
        history_range = payload.get("historyRange")
        if isinstance(history_range, dict):
            payload_snapshot["historyRange"] = dict(history_range)
        snapshot[field] = payload_snapshot
    return snapshot


def _cleanup_jobs_locked(now: int) -> None:
    expired = [
        job_id
        for job_id, job in _jobs.items()
        if now - int(job.get("updatedAt") or now) > BACKTEST_JOB_TTL_SECONDS * 1000
    ]
    for job_id in expired:
        _jobs.pop(job_id, None)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _integer(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _positive(value: object) -> float | None:
    number = _number(value)
    return number if number > 0 else None


def _round(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), 8)
