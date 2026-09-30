from __future__ import annotations

import math
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4

try:
    from . import database as db
    from .binance_client import CONNECTION_ERROR_MESSAGE, get_klines as get_spot_klines, get_spot_markets
    from .binance_futures_client import get_futures_klines, get_futures_markets
    from .binance_ml import has_binance_ml_model, predict_binance_futures_frames
except ImportError:
    import database as db
    from binance_client import CONNECTION_ERROR_MESSAGE, get_klines as get_spot_klines, get_spot_markets
    from binance_futures_client import get_futures_klines, get_futures_markets
    from binance_ml import has_binance_ml_model, predict_binance_futures_frames


MAX_ANALYZED_CONTRACTS = 500
DEFAULT_ANALYZED_CONTRACTS = 24
# The classic checklist remains based on these closed macro frames. The
# direct-plan model additionally consumes the completed 5m stream.
PRIMARY_ANALYSIS_INTERVALS = ("4h", "1h", "15m")
ENTRY_TIMING_INTERVAL = "5m"
MODEL_ANALYSIS_INTERVALS = (*PRIMARY_ANALYSIS_INTERVALS, ENTRY_TIMING_INTERVAL)
STRATEGY_MODE_MIDLINE = "MIDLINE"
STRATEGY_MODE_SHORT_TERM = "SHORT_TERM"
STRATEGY_MODES = frozenset({STRATEGY_MODE_MIDLINE, STRATEGY_MODE_SHORT_TERM})
STRATEGY_MODE_LABELS = {
    STRATEGY_MODE_MIDLINE: "中线模式",
    STRATEGY_MODE_SHORT_TERM: "短线模式",
}
STRATEGY_ENGINE_CLASSIC = "CLASSIC"
STRATEGY_ENGINE_MODEL = "MODEL"
MODEL_BRANCH_BEST = "BEST"
MODEL_BRANCH_STABLE = "STABLE"
MIN_PRIMARY_BARS = 80
MIN_ENTRY_TIMING_BARS = 8
MIN_SHORT_TERM_READING_BARS = 16
# These are calibration values for the binary score, not new trading rules.
# The source material treats close location, EMA distance and volume as
# contextual evidence. Keeping the thresholds explicit makes the relaxed
# screen auditable without weakening the hard risk/room gates below.
TREND_STATE_SCORE_THRESHOLD = 3
SIGNAL_CLOSE_POSITION_THRESHOLD = 0.55
PULLBACK_EMA_DISTANCE_ATR = 1.5
RELATIVE_VOLUME_MIN_RATIO = 0.5
REBOUND_MINIMUM_TARGET_R = 1.25
# Trend continuation must come from a current completed pullback/retest, not
# an EMA touch many bars ago. The following values are a versioned engineering
# calibration for that freshness and terminal-extension screen; they are not
# presented as fixed numbers from the source books.
TREND_PULLBACK_LOOKBACK_BARS = 6
TREND_PULLBACK_MAX_AGE_BARS = 3
TREND_TERMINAL_LOOKBACK_BARS = 12
TREND_TERMINAL_EMA_DISTANCE_ATR = 2.0
TREND_TERMINAL_STREAK_EMA_DISTANCE_ATR = 2.5
TREND_TERMINAL_MIN_DIRECTIONAL_STREAK = 4
TREND_TERMINAL_LARGE_BAR_ATR = 1.5
TREND_TERMINAL_MIN_LARGE_BARS = 2
TREND_TERMINAL_MIN_PUSHES = 3
TREND_PUSH_MIN_ADVANCE_ATR = 0.45
# A near-term protection target must come from the recent local structure. Do
# not let it inherit the full analysis cache, which can turn a nearby partial
# exit into a distant historical extreme.
NEAR_TERM_LOOKBACK_BARS = 20
# A near-term target is an execution aid, not the main room gate. Keep a
# positive minimum so it cannot collapse into the trigger, while allowing a
# confirmed local magnet that is closer than the main target.
# The near target is a structure-backed first realization rather than the
# main room gate. Callers can override this default through strategy settings.
NEAR_TERM_MIN_R = 0.5
NEAR_TERM_PLATFORM_MIN_TOUCHES = 2
NEAR_TERM_PLATFORM_MIN_SPAN_BARS = 1
# Each candidate analysis already fetches 15m / 1h / 4h in parallel. Four
# candidate workers keep the request burst bounded while removing the old
# one-symbol-at-a-time bottleneck.
MAX_MARKET_SCAN_WORKERS = 4
MIN_QUOTE_VOLUME = 5_000_000
KLINE_LIMIT = 180
# The direct model consumes a causal window of 288 completed 5m bars.  The
# former shared 180-bar request made every MODEL scan fail before inference
# with MODEL_DATA_UNAVAILABLE, even though the higher-timeframe windows fit.
# Keep the larger request limited to the model route; classic scans retain the
# existing request size and API cadence.
MODEL_KLINE_LIMITS = {
    "4h": 96,
    "1h": 144,
    "15m": 176,
    "5m": 360,
}
JOB_TTL_SECONDS = 30 * 60
PROFILE_ID = "binance-market-price-action-v1"
PROFILE_VERSION = "2026-09-trend-entry-v4"
LEVEL_STRATEGY_EXTREME = "STRUCTURE_EXTREME"
LEVEL_STRATEGY_PLATFORM = "CONFIRMED_PLATFORM"
PLATFORM_LOOKBACK_BARS = 24
PLATFORM_MIN_TOUCHES = 3
PLATFORM_TOLERANCE_ATR_MULTIPLIER = 0.35
# A target is a magnet zone, not a promise that price will print the far
# edge.  This is an execution-side engineering parameter: move the order a
# small distance into the zone so an exact high/low touch is not required.
TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER = 0.08
# A structural invalidation must sit beyond the complete trigger structure,
# not just one price tick beyond its defensive edge. The span component is a
# bounded interpretation aid for the full signal structure; the ATR component
# remains user-configurable in the strategy settings.
TRIGGER_ZONE_STOP_BUFFER_SPAN_FRACTION = 0.25
# The entry zone is an execution tolerance around one trigger.  Its bounds
# must not turn the same plan into materially different stop/target R trades.
# These limits mirror the direct-model decoder so CLASSIC and MODEL plans have
# the same user-facing entry geometry contract.
MAX_TRIGGER_ZONE_R_MULTIPLE_SPREAD = 0.30
MAX_TRIGGER_ZONE_RISK_FRACTION = 0.12
# Volume and an otherwise-qualified signal bar are contextual soft items that
# may be handled through the explicit 9/10 trial route. Missing location,
# trigger, structure, or room is never a trial entry. A qualified
# high-timeframe continuation pullback may add its still-unrecovered 15m
# direction as one explicit trial gap; it remains 9/10 and needs manual
# execution confirmation.
TRIAL_ALLOWED_MISSING_CONDITIONS = frozenset({"relativeVolume", "signalBar"})
SOURCE_PAGES = {
    "candlestick": ["15-338"],
    "trend": ["70-405"],
    "range": ["18-436"],
    "reversal": ["37-420"],
    "trendEntry": ["trend:177-178", "range:18-24", "range:214-223", "reversal:54", "candlestick:161-198"],
}
_analysis_jobs: dict[str, dict[str, Any]] = {}
_analysis_jobs_lock = Lock()
_analysis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="futures-analysis-job")


def _strategy_mode(value: object = None) -> str:
    """Return the validated analysis route, defaulting to the legacy midline path."""

    if isinstance(value, dict):
        raw = value.get("strategyMode")
    else:
        raw = value
    mode = str(raw or STRATEGY_MODE_MIDLINE).strip().upper()
    return mode if mode in STRATEGY_MODES else STRATEGY_MODE_MIDLINE


def _strategy_intervals(settings: object = None) -> tuple[str, ...]:
    """Return the closed frames required by the selected strategy route."""

    # Direct MODEL inference always anchors on a completed 5m bar.  Do not
    # let the user's classic MIDLINE/SHORT_TERM setting silently turn the
    # model input contract back into the old 15m-only route.
    if _strategy_engine(settings) == STRATEGY_ENGINE_MODEL:
        return MODEL_ANALYSIS_INTERVALS
    return (*PRIMARY_ANALYSIS_INTERVALS, ENTRY_TIMING_INTERVAL) if _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM else PRIMARY_ANALYSIS_INTERVALS


def _strategy_mode_label(settings: object = None) -> str:
    return STRATEGY_MODE_LABELS[_strategy_mode(settings)]


def _strategy_engine(settings: object = None) -> str:
    raw = settings.get("strategyEngine") if isinstance(settings, dict) else settings
    value = str(raw or STRATEGY_ENGINE_CLASSIC).strip().upper()
    return value if value in {STRATEGY_ENGINE_CLASSIC, STRATEGY_ENGINE_MODEL} else STRATEGY_ENGINE_CLASSIC


def _model_branch(settings: object = None) -> str:
    raw = settings.get("modelBranch") if isinstance(settings, dict) else None
    value = str(raw or MODEL_BRANCH_BEST).strip().upper()
    return value if value in {MODEL_BRANCH_BEST, MODEL_BRANCH_STABLE} else MODEL_BRANCH_BEST


def _model_run_id(settings: object = None) -> str | None:
    raw = settings.get("modelRunId") if isinstance(settings, dict) else None
    value = str(raw or "").strip()
    return value or None


@dataclass(frozen=True)
class MarketReading:
    state: str
    bias: str
    close: float
    ema20: float
    ema50: float
    atr: float
    range_low: float
    range_high: float
    range_mid: float
    score: int
    evidence: list[str]


def start_market_analysis(
    market_type: str = "FUTURES",
    network: str = "mainnet",
    limit: int = DEFAULT_ANALYZED_CONTRACTS,
    symbol: str | None = None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Start a public, local-only spot or futures screen."""

    safe_market_type = _market_type(market_type)
    safe_limit = _scan_limit(limit)
    target_symbol = _normalize_target_symbol(symbol)
    settings = _strategy_settings(strategy_settings)
    job_id = uuid4().hex
    now = int(time.time() * 1000)
    job = {
        "id": job_id,
        "status": "QUEUED",
        "createdAt": now,
        "updatedAt": now,
        "error": None,
        "targetSymbol": target_symbol,
        "strategySettings": settings,
        "progress": {
            "currentSymbol": target_symbol,
            "completed": 0,
            "total": 1 if target_symbol else safe_limit,
            "matched": 0,
            "failed": 0,
            "parallelWorkers": 0,
            "plans": [],
            "matchedPlans": [],
            "message": (
                f"正在读取指定{_market_label(safe_market_type)} {target_symbol} 行情。"
                if target_symbol
                else f"正在读取{_market_label(safe_market_type)}候选列表，准备按成交额扫描。"
            ),
        },
        "result": None,
    }
    with _analysis_jobs_lock:
        _cleanup_analysis_jobs(now)
        _analysis_jobs[job_id] = job
    _analysis_executor.submit(_run_market_analysis_job, job_id, safe_market_type, network, safe_limit, target_symbol, settings)
    return _job_snapshot(job)


def get_market_analysis(job_id: str) -> dict[str, Any] | None:
    with _analysis_jobs_lock:
        _cleanup_analysis_jobs(int(time.time() * 1000))
        job = _analysis_jobs.get(str(job_id or ""))
        return _job_snapshot(job) if job else None


def start_futures_market_analysis(
    network: str = "mainnet",
    limit: int = DEFAULT_ANALYZED_CONTRACTS,
    *,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Compatibility wrapper for existing futures callers."""

    return start_market_analysis("FUTURES", network, limit, strategy_settings=strategy_settings)


def _is_exchange_connection_error(exc: BaseException) -> bool:
    message = str(exc or "").strip()
    return bool(message) and (
        message == CONNECTION_ERROR_MESSAGE
        or CONNECTION_ERROR_MESSAGE in message
        or "无法连接交易所" in message
    )


def get_futures_market_analysis(job_id: str) -> dict[str, Any] | None:
    """Compatibility wrapper for existing futures callers."""

    return get_market_analysis(job_id)


def analyze_futures_market(
    network: str = "mainnet",
    limit: int = DEFAULT_ANALYZED_CONTRACTS,
    *,
    progress_callback=None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Compatibility wrapper for a futures-only market scan."""

    return analyze_market(
        "FUTURES",
        network,
        limit,
        progress_callback=progress_callback,
        strategy_settings=strategy_settings,
    )


def analyze_futures_history(
    quote: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    """Evaluate the existing futures plan against already-loaded historical bars.

    The replay service owns the historical cache. Keeping this adapter here
    ensures its market/condition calculation stays aligned with "寻找目标".
    """

    settings = _strategy_settings(strategy_settings)
    # Midline keeps the established macro contract and treats 5m as an
    # optional timing aid. Short-term mode makes 5m a required, closed
    # execution frame while retaining all three higher-timeframe inputs.
    required = _strategy_intervals(settings)
    if not isinstance(quote, dict) or not str(quote.get("symbol") or "").strip():
        return None
    if any(
        not isinstance(frames.get(interval), list)
        or len(frames[interval]) < (MIN_SHORT_TERM_READING_BARS if interval == ENTRY_TIMING_INTERVAL else MIN_PRIMARY_BARS)
        for interval in required
    ):
        return None
    four_hour = _read_market(frames["4h"], "4h")
    one_hour = _read_market(frames["1h"], "1h")
    micro_bars = frames.get(ENTRY_TIMING_INTERVAL)
    five_minute = (
        _read_market(micro_bars, ENTRY_TIMING_INTERVAL)
        if isinstance(micro_bars, list) and len(micro_bars) >= MIN_SHORT_TERM_READING_BARS
        else None
    )
    return _build_plan(
        quote,
        frames["15m"],
        four_hour,
        one_hour,
        False,
        market_type="FUTURES",
        bars_5m=micro_bars if isinstance(micro_bars, list) else None,
        five_minute=five_minute,
        strategy_settings=settings,
    )


def analyze_futures_position_history(
    quote: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    *,
    direction: str,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    """Build a monitoring plan for an already-open futures position.

    The standard analyzer compares both directions because it is looking for a
    new entry.  An existing exchange position needs the same structure and
    risk levels, but it must never silently flip the holder from long to short
    (or vice versa).  This adapter therefore reuses the exact directional
    rules while making the live position direction explicit.
    """

    safe_direction = str(direction or "").strip().upper()
    if safe_direction not in {"LONG", "SHORT"}:
        raise ValueError("真实合约持仓方向必须是做多或做空")
    settings = _strategy_settings(strategy_settings)
    required = _strategy_intervals(settings)
    if not isinstance(quote, dict) or not str(quote.get("symbol") or "").strip():
        return None
    if any(
        not isinstance(frames.get(interval), list)
        or len(frames[interval]) < (MIN_SHORT_TERM_READING_BARS if interval == ENTRY_TIMING_INTERVAL else MIN_PRIMARY_BARS)
        for interval in required
    ):
        return None
    four_hour = _read_market(frames["4h"], "4h")
    one_hour = _read_market(frames["1h"], "1h")
    bars_15m = frames["15m"]
    micro_bars = frames.get(ENTRY_TIMING_INTERVAL)
    five_minute = (
        _read_market(micro_bars, ENTRY_TIMING_INTERVAL)
        if isinstance(micro_bars, list) and len(micro_bars) >= MIN_SHORT_TERM_READING_BARS
        else None
    )
    return _direction_plan(
        safe_direction,
        quote,
        bars_15m,
        bars_15m[-1],
        bars_15m[-2],
        four_hour,
        one_hour,
        _read_market(bars_15m, "15m"),
        False,
        market_type="FUTURES",
        bars_5m=micro_bars if isinstance(micro_bars, list) else None,
        five_minute=five_minute,
        strategy_settings=settings,
    )


def analyze_spot_market(
    network: str = "mainnet",
    limit: int = DEFAULT_ANALYZED_CONTRACTS,
    *,
    progress_callback=None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Scan liquid public spot pairs locally. This function never submits an order."""

    return analyze_market(
        "SPOT",
        network,
        limit,
        progress_callback=progress_callback,
        strategy_settings=strategy_settings,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 2)


def _timing_distribution(values: list[float]) -> dict[str, float | int]:
    """Summarize durations without exposing one symbol's private scan state."""

    samples = sorted(float(value) for value in values if math.isfinite(float(value)) and float(value) >= 0.0)
    if not samples:
        return {"sampleCount": 0, "totalMs": 0.0, "meanMs": 0.0, "p50Ms": 0.0, "p95Ms": 0.0}

    def percentile(fraction: float) -> float:
        index = min(len(samples) - 1, max(0, math.ceil(len(samples) * fraction) - 1))
        return round(samples[index], 2)

    return {
        "sampleCount": len(samples),
        "totalMs": round(sum(samples), 2),
        "meanMs": round(sum(samples) / len(samples), 2),
        "p50Ms": percentile(0.50),
        "p95Ms": percentile(0.95),
    }


def _market_scan_timing_summary(
    plans: list[dict[str, Any]],
    *,
    total_wall_ms: float,
    model_availability_ms: float,
    market_list_ms: float,
    contract_analysis_ms: float,
    cross_section_rank_ms: float,
) -> dict[str, Any]:
    """Return wall-clock and per-contract timing evidence for one scan."""

    timings = [
        plan.get("_analysisTiming")
        for plan in plans
        if isinstance(plan, dict) and isinstance(plan.get("_analysisTiming"), dict)
    ]
    interval_values: dict[str, list[float]] = {}
    for timing in timings:
        for interval, value in (timing.get("intervalFetchMs") or {}).items():
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                interval_values.setdefault(str(interval), []).append(float(value))
    return {
        "totalWallMs": round(float(total_wall_ms), 2),
        "modelAvailabilityMs": round(float(model_availability_ms), 2),
        "marketListMs": round(float(market_list_ms), 2),
        "contractAnalysisWallMs": round(float(contract_analysis_ms), 2),
        "crossSectionalRankMs": round(float(cross_section_rank_ms), 2),
        "contractTotal": _timing_distribution([float(timing.get("totalMs") or 0.0) for timing in timings]),
        "klineFetchWall": _timing_distribution([float(timing.get("klineFetchWallMs") or 0.0) for timing in timings]),
        "modelInference": _timing_distribution([float(timing.get("modelInferenceMs") or 0.0) for timing in timings]),
        "strategyBuild": _timing_distribution([float(timing.get("strategyBuildMs") or 0.0) for timing in timings]),
        "intervalFetch": {interval: _timing_distribution(values) for interval, values in sorted(interval_values.items())},
    }


def analyze_market(
    market_type: str,
    network: str = "mainnet",
    limit: int = DEFAULT_ANALYZED_CONTRACTS,
    *,
    symbol: str | None = None,
    progress_callback=None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    """Scan liquid public spot or futures symbols in quote-volume order."""

    scan_started = time.perf_counter()
    safe_market_type = _market_type(market_type)
    safe_limit = _scan_limit(limit)
    target_symbol = _normalize_target_symbol(symbol)
    settings = _strategy_settings(strategy_settings)
    model_engine_requested = safe_market_type == "FUTURES" and _strategy_engine(settings) == STRATEGY_ENGINE_MODEL
    selected_model_branch = _model_branch(settings)
    selected_model_run_id = _model_run_id(settings)
    # MODEL is an explicit route. Its worker remains model-only even when the
    # selected artifact is unavailable: that case produces WAIT rather than a
    # classic interim candidate.
    model_availability_started = time.perf_counter()
    model_prediction_enabled = model_engine_requested and has_binance_ml_model(
        network,
        selected_model_branch,
        allow_rejected=True,
        run_id=selected_model_run_id,
    )
    model_availability_ms = _elapsed_ms(model_availability_started)
    market_loader = get_futures_markets if safe_market_type == "FUTURES" else get_spot_markets
    # Target finding always uses REST market lists and historical klines.
    # connectionMode only controls the account/live-market snapshot workers.
    market_list_started = time.perf_counter()
    market_snapshot = market_loader(network)
    market_list_ms = _elapsed_ms(market_list_started)
    all_contracts = list(market_snapshot.get("items") or [])
    if target_symbol:
        candidates = [item for item in all_contracts if str(item.get("symbol") or "").upper() == target_symbol]
        if not candidates:
            raise ValueError(f"当前{_market_label(safe_market_type)}市场不存在交易对 {target_symbol}")
    else:
        liquid = [item for item in all_contracts if _number(item.get("quoteVolume")) >= MIN_QUOTE_VOLUME]
        if not liquid:
            liquid = all_contracts
        candidates = liquid[:_scan_limit(limit)]
    market_label = _market_label(safe_market_type)
    analyzer = _analyze_contract if safe_market_type == "FUTURES" else _analyze_spot
    worker_count = _market_scan_worker_count(len(candidates))
    evaluated_by_index: list[dict[str, Any] | None] = [None] * len(candidates)
    failures = 0
    completed = 0
    candidate_iter = iter(enumerate(candidates))
    pending: dict[Any, tuple[int, dict[str, Any]]] = {}

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        try:
            index, item = next(candidate_iter)
        except StopIteration:
            return False
        # Keep the analyzer's two-argument call contract intact for existing
        # callers/tests while transporting the immutable job snapshot with the
        # quote itself.
        pending[executor.submit(
            analyzer,
            network,
            {
                **item,
                "_strategySettings": settings,
                "_marketStale": bool(market_snapshot.get("stale")),
                "_includeModelFrames": model_engine_requested,
                "_modelPredictionEnabled": model_prediction_enabled,
                "_modelBranch": selected_model_branch,
                "_modelRunId": selected_model_run_id,
                # 5m is an entry-timing aid, not one of the ten macro
                # conditions.  Batch scans defer it until a candidate is
                # already 10/10 or an eligible 9/10 trial.
                "_deferMicro": not bool(target_symbol),
            },
        )] = (index, item)
        return True

    def ordered_results() -> list[dict[str, Any]]:
        # Completion order is nondeterministic; preserve turnover order in the
        # progress payload and final result.
        return [plan for plan in evaluated_by_index if plan]

    def active_symbols() -> str | None:
        symbols = [str(item.get("symbol") or "--") for _, item in pending.values()]
        return "、".join(symbols) if symbols else None

    contract_analysis_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"{safe_market_type.lower()}-market-scan") as executor:
        while len(pending) < worker_count and submit_next(executor):
            pass
        _report_progress(
            progress_callback,
            current_symbol=active_symbols(),
            completed=0,
            total=len(candidates),
            matched=0,
            failed=0,
            parallel_workers=worker_count,
            plans=[],
            matched_plans=[],
            message=(
                f"已锁定 {target_symbol}，正在读取 4h、1h、15m、5m 并进行时序模型推理。"
                if target_symbol and model_engine_requested
                else f"已锁定 {target_symbol}，正在进行 15m、1h、4h 主周期分析，并读取 5m 入场与短线结构。"
                if target_symbol
                else f"已按 24h 报价成交额排序，正在以 {worker_count} 路并行进行时序模型推理。"
                if model_engine_requested
                else f"已按 24h 报价成交额排序，正在以 {worker_count} 路并行分析{market_label}。"
            ),
        )
        while pending:
            finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                index, item = pending.pop(future)
                symbol = str(item.get("symbol") or "--")
                try:
                    result = future.result()
                except Exception as exc:
                    if _is_exchange_connection_error(exc):
                        raise RuntimeError(CONNECTION_ERROR_MESSAGE) from exc
                    failures += 1
                    result = None
                if result:
                    evaluated_by_index[index] = result
                completed += 1
                while len(pending) < worker_count and submit_next(executor):
                    pass
                current_plans = ordered_results()
                armed_plans = [
                    plan
                    for plan in current_plans
                    if plan.get("status") == "ARMED" and _plan_is_actionable(plan)
                ]
                trial_plans = [plan for plan in current_plans if _plan_is_trial_eligible(plan)]
                # A model scan is an auditable ranking of every completed
                # inference. Do not hide WAIT or blocked results mid-scan.
                visible_plans = current_plans
                visible_armed = armed_plans
                active = active_symbols()
                _report_progress(
                    progress_callback,
                    current_symbol=active,
                    completed=completed,
                    total=len(candidates),
                    matched=len(visible_armed) + len(trial_plans),
                    failed=failures,
                    parallel_workers=worker_count,
                    # MODEL scans must stream every model result, including a
                    # WAIT verdict.  A WAIT is a completed inference, not a
                    # missing-data condition, and hiding it made the UI look
                    # as if the scan had not run.  Classic turnover scans
                    # still stream only executable plans; named-symbol
                    # analysis continues to expose its complete monitoring
                    # plan even when the discipline score is not executable.
                    plans=[_progress_plan(plan) for plan in visible_plans],
                    matched_plans=[
                        _progress_plan(plan)
                        for plan in visible_armed
                    ],
                    trial_plans=[_progress_plan(plan) for plan in trial_plans],
                    message=(
                        f"已完成 {symbol} 的时序模型推理，正在推理 {active}。"
                        if active and model_engine_requested
                        else f"已完成 {symbol}，正在并行分析 {active} 的 15m、1h、4h K线，并读取 5m 入场与短线结构。"
                        if active and _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM
                        else f"已完成 {symbol}，正在并行分析 {active} 的 15m、1h、4h K线。"
                        if active
                        else f"已完成 {symbol}，正在汇总扫描结果。"
                    ),
                )
    contract_analysis_ms = _elapsed_ms(contract_analysis_started)
    evaluated_plans = ordered_results()
    analyzed_plans = evaluated_plans
    cross_section_rank_started = time.perf_counter()
    if model_engine_requested:
        # Every worker has already completed its own direct inference. Only
        # the aggregate needs a global quality sort; no classic plan is
        # generated or replaced after the scan.
        analyzed_plans = _apply_model_cross_sectional_rank(analyzed_plans)
    cross_section_rank_ms = _elapsed_ms(cross_section_rank_started)
    timing_summary = _market_scan_timing_summary(
        evaluated_plans,
        total_wall_ms=_elapsed_ms(scan_started),
        model_availability_ms=model_availability_ms,
        market_list_ms=market_list_ms,
        contract_analysis_ms=contract_analysis_ms,
        cross_section_rank_ms=cross_section_rank_ms,
    )
    for plan in analyzed_plans:
        if isinstance(plan, dict):
            plan.pop("_analysisTiming", None)

    # Carry the exact server-side execution decision with every returned
    # plan.  The UI used to reconstruct this gate independently and could
    # therefore label a plan as "符合" while the persistence endpoint (which
    # performs the authoritative geometry/ratio check) rejected it.  Keeping
    # this diagnostic flag on the response makes both sides use one contract;
    # the endpoint still recomputes it and never trusts the client flag.
    for plan in analyzed_plans:
        if isinstance(plan, dict):
            plan["executionEligible"] = bool(_plan_is_actionable(plan))
    if target_symbol:
        # "分析标的" is a monitoring workflow, not a relaxed version of
        # "寻找目标". Preserve every derived level for the requested symbol
        # while retaining its original score/status for risk disclosure and
        # execution-policy checks.
        for plan in analyzed_plans:
            plan["analysisScope"] = "TARGET"
            plan["monitoringPlan"] = True
            plan["monitoringPlanReason"] = "指定标的分析保留完整点位，未达寻找目标条件时仅可创建监控计划。"
    eligible_plans = _eligible_plans(analyzed_plans)
    armed = sorted((item for item in eligible_plans if item.get("status") == "ARMED"), key=_sort_key, reverse=True)
    trials = sorted((item for item in eligible_plans if _plan_is_trial_eligible(item)), key=_sort_key, reverse=True)
    ranked = sorted(eligible_plans, key=_sort_key, reverse=True)
    # A model scan is complete even when its best decision is WAIT.  Keep the
    # highest-ranked model inference as the recommendation so callers can see
    # the selected score, wait baseline and reason.  Falling through to the
    # classic empty recommendation here incorrectly reported missing 4h/1h/
    # 15m/5m data and discarded the model decision.
    if target_symbol and model_engine_requested:
        recommendation = (analyzed_plans or [_empty_model_recommendation(
            safe_market_type,
            branch=selected_model_branch,
            run_id=selected_model_run_id,
        )])[0]
    elif target_symbol:
        recommendation = (analyzed_plans or [_empty_recommendation(safe_market_type)])[0]
    elif model_engine_requested:
        # WAIT decisions stay out of the public candidate list, but retain
        # the best evaluated decision as the recommendation so an all-WAIT
        # scan still identifies what the model evaluated and why it declined.
        recommendation = (ranked or analyzed_plans or evaluated_plans or [_empty_model_recommendation(
            safe_market_type,
            branch=selected_model_branch,
            run_id=selected_model_run_id,
        )])[0]
    else:
        recommendation = (ranked or [_empty_recommendation(safe_market_type)])[0]
    result = {
        "marketType": safe_market_type,
        "strategySettings": settings,
        "strategyEngine": _strategy_engine(settings),
        "modelBranch": selected_model_branch,
        "modelRunId": selected_model_run_id,
        "recommendation": recommendation,
        "targetPlan": recommendation if target_symbol else None,
        "plans": analyzed_plans,
        "matchedPlans": armed,
        "trialPlans": trials,
        "alternatives": [item for item in ranked if item.get("symbol") != recommendation.get("symbol")][:3],
        "marketScan": {
            "totalContracts": len(all_contracts),
            "liquidCandidates": len(candidates),
            "analyzedContracts": len(evaluated_plans),
            "returnedPlans": len(analyzed_plans),
            "failedContracts": failures,
            "parallelWorkers": worker_count,
            "stale": bool(market_snapshot.get("stale")),
            "analyzedAt": int(time.time() * 1000),
            "scope": (f"指定 USDT {market_label} {target_symbol}，进行单目标多周期纪律分析。" if target_symbol else f"USDT {market_label}，按成交额选取前 {len(candidates)} 个逐个完成多周期模型推理，并按模型分数返回全部结果。"),
            "modelScan": {
                "evaluatedPlans": len(evaluated_plans) if model_engine_requested else 0,
                "returnedPlans": len(analyzed_plans) if model_engine_requested else 0,
                "selectionLimit": None,
            },
            "timings": timing_summary,
        },
    }
    _report_progress(
        progress_callback,
        current_symbol=None,
        completed=len(candidates),
        total=len(candidates),
        matched=len(armed) + len(trials),
        failed=failures,
        parallel_workers=worker_count,
        # Keep the completed progress snapshot as small as the live updates;
        # the complete audit set is still returned under result.plans.
        plans=[
            _progress_plan(plan)
            for plan in (analyzed_plans if target_symbol or model_engine_requested else eligible_plans)
        ],
        matched_plans=[_progress_plan(plan) for plan in armed],
        trial_plans=[_progress_plan(plan) for plan in trials],
        message="扫描完成。",
    )
    return result


def _run_market_analysis_job(
    job_id: str,
    market_type: str,
    network: str,
    limit: int,
    target_symbol: str | None = None,
    strategy_settings: object = None,
) -> None:
    _update_analysis_job(job_id, status="RUNNING")

    def report(progress: dict[str, Any]) -> None:
        _update_analysis_job(job_id, status="RUNNING", progress=progress)

    try:
        result = analyze_market(
            market_type,
            network,
            limit,
            symbol=target_symbol,
            progress_callback=report,
            strategy_settings=strategy_settings,
        )
    except Exception as exc:
        error_message = CONNECTION_ERROR_MESSAGE if _is_exchange_connection_error(exc) else (str(exc) or f"{_market_label(market_type)}扫描失败")
        _update_analysis_job(job_id, status="FAILED", error=error_message)
        return
    _update_analysis_job(job_id, status="COMPLETED", result=result)


def _update_analysis_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with _analysis_jobs_lock:
        job = _analysis_jobs.get(job_id)
        if not job:
            return
        if status:
            job["status"] = status
        if progress is not None:
            job["progress"] = dict(progress)
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error
        job["updatedAt"] = int(time.time() * 1000)


def _cleanup_analysis_jobs(now: int) -> None:
    stale = [job_id for job_id, job in _analysis_jobs.items() if now - int(job.get("updatedAt") or now) > JOB_TTL_SECONDS * 1000]
    for job_id in stale:
        _analysis_jobs.pop(job_id, None)


def _job_snapshot(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    return {
        "id": job["id"],
        "status": job["status"],
        "createdAt": job["createdAt"],
        "updatedAt": job["updatedAt"],
        "error": job.get("error"),
        "progress": dict(job.get("progress") or {}),
        "result": job.get("result"),
    }


def _progress_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Strip transient model-frame transport before emitting job progress."""

    result = {key: value for key, value in plan.items() if not key.startswith("_")}
    # Progress snapshots are consumed before the final aggregate is ready;
    # expose the same authoritative gate there as well so an early click
    # cannot use a looser client-side reconstruction.
    if isinstance(plan, dict):
        result["executionEligible"] = bool(_plan_is_actionable(plan))
    return result


def _report_progress(callback, **progress: Any) -> None:
    if callback:
        field_names = {
            "current_symbol": "currentSymbol",
            "matched_plans": "matchedPlans",
            "trial_plans": "trialPlans",
            "parallel_workers": "parallelWorkers",
        }
        callback({field_names.get(key, key): value for key, value in progress.items()})


def _scan_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_ANALYZED_CONTRACTS
    return min(max(number, 1), MAX_ANALYZED_CONTRACTS)


def _market_scan_worker_count(candidate_count: int) -> int:
    count = int(candidate_count or 1)
    # Tiny scans are kept serial for predictable progress semantics and lower
    # thread overhead; normal "寻找好位置" scans still use the bounded pool.
    if count <= 3:
        return 1
    return min(MAX_MARKET_SCAN_WORKERS, count)


def _normalize_target_symbol(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().upper()
    if not 5 <= len(normalized) <= 20 or not normalized.isalnum():
        raise ValueError("指定交易对格式无效")
    return normalized


def _market_type(value: Any) -> str:
    normalized = str(value or "FUTURES").strip().upper()
    if normalized not in {"SPOT", "FUTURES"}:
        raise ValueError("市场类型必须选择现货或合约")
    return normalized


def _market_label(market_type: str) -> str:
    return "现货" if _market_type(market_type) == "SPOT" else "合约"


def _direction_context_is_compatible(reading: MarketReading, expected_bias: str) -> bool:
    """Accept neutral lower-timeframe balance without accepting opposition."""

    if reading.bias == expected_bias:
        return True
    return reading.bias == "NEUTRAL" and reading.state in {"RANGE", "TRANSITION"}


def _four_hour_veto_is_clear(reading: MarketReading, expected_bias: str) -> bool:
    """Keep 4h as a short-term veto without making it a second entry trigger.

    A short-term plan still follows the discipline order: 1h supplies the
    environment and 15m/5m supply location and execution. A neutral 4h
    balance is therefore allowed, while a confirmed 4h trend in the opposite
    direction is a hard conflict.
    """

    opposite_bias = "BEAR" if expected_bias == "BULL" else "BULL"
    return reading.bias != opposite_bias


def _trend_pullback_trial_context(
    direction: str,
    four_hour: MarketReading,
    one_hour: MarketReading,
    fifteen: MarketReading,
    *,
    signal_bar: bool,
    position_ok: bool,
    trigger_pending: bool,
) -> bool:
    """Recognize a qualified high-timeframe continuation pullback.

    A 15m counter-trend state is not automatically a reversal.  In a 4h/1h
    aligned trend it can be the pullback that precedes continuation.  It is
    still kept as the one missing condition for a 9/10 trial: the lower
    timeframe has not fully resumed the plan direction yet.  Requiring the
    closed signal bar, valid pullback location and untouched trigger prevents
    an arbitrary counter-trend signal from becoming executable.
    """

    expected_bias = "BULL" if direction == "LONG" else "BEAR"
    return (
        four_hour.bias == expected_bias
        and _direction_context_is_compatible(one_hour, expected_bias)
        and fifteen.bias in {"BULL", "BEAR"}
        and fifteen.bias != expected_bias
        and signal_bar
        and position_ok
        and trigger_pending
    )


def _model_strategy_settings(value: object = None) -> dict[str, Any]:
    """Normalize settings and suppress classic route knobs for direct MODEL."""
    try:
        settings = db.normalize_binance_strategy_settings(value)
    except ValueError:
        settings = db.default_binance_strategy_settings()
    # MODEL is a separate plan-producing engine.  Keep the account/risk and
    # model-emitted numeric parameters intact (the replay decoder overlays its
    # four trailing-stage values here), but neutralize only classic route knobs
    # so an old persisted preference cannot change a direct-model plan after the
    # UI hides it.
    if _strategy_engine(settings) == STRATEGY_ENGINE_MODEL:
        settings.update(
            {
                "strategyMode": STRATEGY_MODE_MIDLINE,
                "levelStrategy": LEVEL_STRATEGY_EXTREME,
                "entryConfirmationMode": "TRIGGER_ONLY",
                "entryConfirmationExpiryBars": 1,
            }
        )
    return settings


def _strategy_settings(value: object = None) -> dict[str, Any]:
    """Use the same validated settings contract for live and historical plans."""

    return _model_strategy_settings(value)


def _plan_is_trial_eligible(plan: dict[str, Any] | None) -> bool:
    """Return whether a plan may use the explicit one-soft-condition trial path."""

    if not isinstance(plan, dict):
        return False
    status = str(plan.get("status") or "").strip().upper()
    if status not in {"WATCH", "TRIAL"} or plan.get("trialEligible") is not True:
        return False
    def integral_score(value: object) -> int | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not number.is_integer():
            return None
        return int(number)

    condition_met = integral_score(plan.get("conditionMet"))
    condition_total = integral_score(plan.get("conditionTotal"))
    if condition_met is None or condition_total is None:
        return False
    return condition_met == 9 and condition_total == 10


def _direct_model_plan_is_valid(plan: dict[str, Any] | None) -> bool:
    """Return whether a generated direct-model plan has executable geometry.

    Classic plans use the ten-condition discipline score.  Direct MODEL
    plans intentionally use a one-condition contract (the model's
    action-conditioned selection), so applying the classic ``10/10`` check
    would discard every valid model plan.  Keep this small geometry check at
    the shared execution gate so a malformed model payload still cannot enter
    the executable result set.
    """

    if not isinstance(plan, dict):
        return False
    if str(plan.get("strategyEngine") or "").strip().upper() != STRATEGY_ENGINE_MODEL:
        return False
    if str(plan.get("modelTask") or "").strip().upper() != "DIRECT_PLAN":
        return False
    if plan.get("modelGenerated") is not True:
        return False
    direction = str(plan.get("direction") or "").strip().upper()
    if direction not in {"LONG", "SHORT"}:
        return False
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    targets = [item for item in (plan.get("takeProfits") or []) if isinstance(item, dict)]
    try:
        trigger = float(entry.get("trigger"))
        zone_low = float(entry.get("zoneLow"))
        zone_high = float(entry.get("zoneHigh"))
        stop = float(plan.get("stopLoss"))
        first = float(targets[0].get("price"))
        second = float(targets[1].get("price"))
        ratios = [float(targets[index].get("ratio")) for index in range(2)]
    except (IndexError, TypeError, ValueError):
        return False
    values = (trigger, zone_low, zone_high, stop, first, second, *ratios)
    if not all(math.isfinite(value) for value in values):
        return False
    if not zone_low <= trigger <= zone_high:
        return False
    if any(value <= 0 for value in ratios) or sum(ratios) > 100.0001:
        return False
    if direction == "LONG":
        geometry_valid = stop < trigger < first < second
    else:
        geometry_valid = second < first < trigger < stop
    if not geometry_valid:
        return False

    # Direct MODEL plans are conditional stop entries, just like classic
    # breakout plans.  A plan can be outside its trigger zone while price is
    # still approaching from the safe side, but it is no longer executable
    # after the trigger has already been crossed.  The decoder marks this
    # explicitly; keep a small fallback here for persisted/manual snapshots
    # that only contain the current price and levels.
    entry_timing = plan.get("entryTiming") if isinstance(plan.get("entryTiming"), dict) else {}
    if entry_timing.get("pending") is False:
        return False
    current = _finite_model_number(
        plan.get("lastPrice")
        if plan.get("lastPrice") is not None
        else plan.get("latestPrice")
        if plan.get("latestPrice") is not None
        else plan.get("markPrice")
    )
    order_type = str(entry.get("orderType") or "").strip().upper()
    if order_type not in {"STOP", "LIMIT"}:
        reference = current if current is not None else trigger
        order_type = (
            "STOP"
            if (direction == "LONG" and trigger >= reference)
            or (direction == "SHORT" and trigger <= reference)
            else "LIMIT"
        )
    if current is not None:
        epsilon = max(abs(trigger) * 1e-10, 1e-12)
        crossed = (
            (direction == "LONG" and current >= trigger - epsilon)
            or (direction == "SHORT" and current <= trigger + epsilon)
            if order_type == "STOP"
            else (direction == "LONG" and current <= trigger + epsilon)
            or (direction == "SHORT" and current >= trigger - epsilon)
        )
        if crossed:
            return False
    return True


def _plan_is_actionable(plan: dict[str, Any] | None) -> bool:
    """Keep classic and direct-model plans on their respective scan paths."""

    if not isinstance(plan, dict):
        return False
    if str(plan.get("strategyEngine") or "").strip().upper() == STRATEGY_ENGINE_MODEL:
        # Direct-plan checkpoints emit conditionMet/conditionTotal as 1/1;
        # they do not participate in the classic ten-condition checklist.
        return str(plan.get("status") or "").strip().upper() == "ARMED" and _direct_model_plan_is_valid(plan)
    if str(plan.get("status") or "").strip().upper() == "ARMED":
        met = plan.get("conditionMet")
        total = plan.get("conditionTotal")
        if met is None and total is None:
            # Legacy snapshots predate the score fields; their ARMED status is
            # still the persisted representation of a full-score plan.
            return True
        try:
            met_number = float(met)
            total_number = float(total)
        except (TypeError, ValueError):
            return False
        return (
            math.isfinite(met_number)
            and math.isfinite(total_number)
            and met_number.is_integer()
            and total_number.is_integer()
            and int(met_number) == 10
            and int(total_number) == 10
        )
    return _plan_is_trial_eligible(plan)


def _eligible_plans(plans: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Expose only full-score plans and explicitly permitted 9/10 trials."""

    return [plan for plan in (plans or []) if _plan_is_actionable(plan)]


def _apply_model_cross_sectional_rank(plans: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Annotate and order the model's current-market quality frontier.

    Absolute critic values drift between contracts and volatility regimes.  A
    market scan therefore needs a cross-sectional rank in addition to the
    per-symbol score.  This is a presentation/ranking layer only: it never
    turns a WAIT or malformed plan into an executable candidate.
    """

    ordered = sorted(
        [plan for plan in (plans or []) if isinstance(plan, dict)],
        key=_model_inference_sort_key,
        reverse=True,
    )
    actionable = [plan for plan in ordered if _plan_is_actionable(plan)]
    total = len(actionable)
    for rank, plan in enumerate(actionable, start=1):
        model = plan.setdefault("modelStrategy", {}) if isinstance(plan.get("modelStrategy"), dict) else {}
        if not isinstance(plan.get("modelStrategy"), dict):
            plan["modelStrategy"] = model
        percentile = 100.0 * (total - rank + 1) / max(1, total)
        model["marketRank"] = rank
        model["marketUniverseSize"] = total
        model["marketPercentile"] = round(percentile, 2)
    for plan in ordered:
        if plan not in actionable:
            model = plan.setdefault("modelStrategy", {}) if isinstance(plan.get("modelStrategy"), dict) else {}
            if not isinstance(plan.get("modelStrategy"), dict):
                plan["modelStrategy"] = model
            model.setdefault("marketRank", None)
            model.setdefault("marketUniverseSize", total)
            model.setdefault("marketPercentile", 0.0)
    return ordered


def _directional_continuation_bar(bar: dict[str, float], direction: str, atr: float) -> bool:
    """Return a completed directional bar suitable for a continuation count."""

    span = max(bar["high"] - bar["low"], atr * 0.15)
    close_position = (bar["close"] - bar["low"]) / span
    body = abs(bar["close"] - bar["open"])
    if direction == "LONG":
        return bar["close"] > bar["open"] and close_position >= 0.56 and body >= atr * 0.12
    return bar["close"] < bar["open"] and close_position <= 0.44 and body >= atr * 0.12


def _counter_or_pause_bar(
    bar: dict[str, float],
    previous: dict[str, float],
    direction: str,
    atr: float,
) -> bool:
    """Identify a completed counter move or pause without treating a wick as one."""

    tolerance = atr * 0.05
    if direction == "LONG":
        return (
            bar["close"] <= bar["open"]
            or bar["close"] < previous["close"] - tolerance
            or bar["low"] < previous["low"] - tolerance
        )
    return (
        bar["close"] >= bar["open"]
        or bar["close"] > previous["close"] + tolerance
        or bar["high"] > previous["high"] + tolerance
    )


def _trend_push_count(bars: list[dict[str, float]], direction: str, atr: float) -> int:
    """Count separated directional pushes without pretending every bar is a leg."""

    if len(bars) < 3:
        return 0
    is_long = direction == "LONG"
    pushes = 0
    last_push_extreme: float | None = None
    waiting_for_new_push = True
    for index in range(1, len(bars)):
        bar = bars[index]
        previous = bars[index - 1]
        if _counter_or_pause_bar(bar, previous, direction, atr):
            waiting_for_new_push = True
            continue
        if not _directional_continuation_bar(bar, direction, atr):
            continue
        extreme = bar["high"] if is_long else bar["low"]
        if last_push_extreme is None:
            advanced = True
        elif is_long:
            advanced = extreme - last_push_extreme >= atr * TREND_PUSH_MIN_ADVANCE_ATR
        else:
            advanced = last_push_extreme - extreme >= atr * TREND_PUSH_MIN_ADVANCE_ATR
        if waiting_for_new_push and advanced:
            pushes += 1
            last_push_extreme = extreme
            waiting_for_new_push = False
        elif last_push_extreme is None or (extreme > last_push_extreme if is_long else extreme < last_push_extreme):
            last_push_extreme = extreme
    return pushes


def _trend_entry_context(
    bars: list[dict[str, float]],
    direction: str,
    atr: float,
    ema20: float,
) -> dict[str, Any]:
    """Require a fresh continuation setup and reject a terminal trend chase.

    The detector deliberately separates a fresh pullback/retest from a prior
    EMA touch. It also treats a terminal extension as a reason to wait for a
    new completed structure, not as permission to guess an immediate reversal.
    """

    result: dict[str, Any] = {
        "passed": False,
        "setup": None,
        "pullbackAgeBars": None,
        "pullbackDepthAtr": None,
        "emaDistanceAtr": None,
        "directionalStreak": 0,
        "largeDirectionalBars": 0,
        "pushCount": 0,
        "terminalExtension": False,
        "structureStartIndex": None,
        "sourcePages": list(SOURCE_PAGES["trendEntry"]),
        "reason": "等待当前 15m 回撤、旗形或突破回测；不能使用更早的 EMA 接触作为入场依据。",
        "missing": "等待新的已收盘回撤/回测与方向信号，再重新定义触发区和结构止损。",
        "structureBars": None,
    }
    if len(bars) < 4 or atr <= 0:
        result.update(reason="15m K线不足，无法确认当前回撤是否新鲜。")
        return result

    is_long = direction == "LONG"
    latest = bars[-1]
    search_start = max(1, len(bars) - TREND_PULLBACK_LOOKBACK_BARS)
    pullback_start: int | None = None
    pullback_end: int | None = None
    pullback_depth = 0.0
    near_ema = False
    for candidate_end in range(len(bars) - 2, search_start - 1, -1):
        if not _counter_or_pause_bar(bars[candidate_end], bars[candidate_end - 1], direction, atr):
            continue
        candidate_start = candidate_end
        while candidate_start > search_start and _counter_or_pause_bar(
            bars[candidate_start - 1], bars[candidate_start - 2], direction, atr
        ):
            candidate_start -= 1
        prior = bars[max(0, candidate_start - 4):candidate_start]
        pullback = bars[candidate_start:candidate_end + 1]
        if not prior or not pullback:
            continue
        prior_extreme = max(item["high"] for item in prior) if is_long else min(item["low"] for item in prior)
        pullback_extreme = min(item["low"] for item in pullback) if is_long else max(item["high"] for item in pullback)
        depth = prior_extreme - pullback_extreme if is_long else pullback_extreme - prior_extreme
        near_ema_candidate = (
            pullback_extreme <= ema20 + atr * PULLBACK_EMA_DISTANCE_ATR
            if is_long
            else pullback_extreme >= ema20 - atr * PULLBACK_EMA_DISTANCE_ATR
        )
        counter_body = max(abs(item["close"] - item["open"]) for item in pullback)
        if depth >= atr * 0.25 or near_ema_candidate or counter_body >= atr * 0.15:
            pullback_start = candidate_start
            pullback_end = candidate_end
            pullback_depth = max(0.0, depth)
            near_ema = near_ema_candidate
            break

    terminal_window = bars[-TREND_TERMINAL_LOOKBACK_BARS:]
    prior_terminal = terminal_window[:-1]
    latest_extreme = (
        latest["high"] >= max(item["high"] for item in prior_terminal) - atr * 0.1
        if is_long
        else latest["low"] <= min(item["low"] for item in prior_terminal) + atr * 0.1
    )
    ema_distance = (latest["close"] - ema20) / atr if is_long else (ema20 - latest["close"]) / atr
    ema_distance = max(0.0, ema_distance)
    directional_streak = 0
    for item in reversed(terminal_window):
        if not _directional_continuation_bar(item, direction, atr):
            break
        directional_streak += 1
    large_directional_bars = sum(
        1
        for item in terminal_window[-4:]
        if _directional_continuation_bar(item, direction, atr)
        and item["high"] - item["low"] >= atr * TREND_TERMINAL_LARGE_BAR_ATR
    )
    push_count = _trend_push_count(terminal_window, direction, atr)
    terminal_extension = latest_extreme and (
        (push_count >= TREND_TERMINAL_MIN_PUSHES and ema_distance >= TREND_TERMINAL_EMA_DISTANCE_ATR)
        or (
            directional_streak >= TREND_TERMINAL_MIN_DIRECTIONAL_STREAK
            and ema_distance >= TREND_TERMINAL_STREAK_EMA_DISTANCE_ATR
        )
        or (
            large_directional_bars >= TREND_TERMINAL_MIN_LARGE_BARS
            and directional_streak >= 3
            and ema_distance >= TREND_TERMINAL_EMA_DISTANCE_ATR
        )
    )
    result.update(
        emaDistanceAtr=round(ema_distance, 2),
        directionalStreak=directional_streak,
        largeDirectionalBars=large_directional_bars,
        pushCount=push_count,
        terminalExtension=terminal_extension,
    )
    if terminal_extension:
        result.update(
            reason=(
                f"15m 位于趋势外侧，距20EMA约 {ema_distance:.2f} ATR，"
                f"出现 {push_count} 段推进/连续 {directional_streak} 根方向K；"
                "这是趋势末端延伸警示，等待新的回撤或回测。"
            ),
            missing="趋势末端延伸，不在末端继续追随；等待价格回到新的旗形、回撤或突破回测后再评估。",
        )
        return result
    if pullback_start is None or pullback_end is None:
        return result

    age = len(bars) - 1 - pullback_end
    result.update(
        pullbackAgeBars=age,
        pullbackDepthAtr=round(pullback_depth / atr, 2),
        structureStartIndex=pullback_start,
    )
    if age > TREND_PULLBACK_MAX_AGE_BARS:
        result.update(
            reason=(
                f"最近可识别的15m回撤已过去 {age} 根K，已不是当前入场结构；"
                "不能把旧回撤延用为新的追价理由。"
            ),
            missing="等待新的已收盘回撤、旗形或突破回测。",
        )
        return result
    result.update(
        passed=True,
        setup="EMA_RETEST" if near_ema else "FRESH_PULLBACK",
        structureBars=bars[pullback_start:],
        reason=(
            f"15m 当前回撤距今 {age} 根K，深度约 {pullback_depth / atr:.2f} ATR，"
            + ("已测试EMA附近并等待顺势恢复。" if near_ema else "属于新鲜旗形/浅回撤，等待顺势触发。")
        ),
        missing="",
    )
    return result


def _analyze_contract(
    network: str,
    quote: dict[str, Any],
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    settings = strategy_settings if strategy_settings is not None else quote.get("_strategySettings")
    return _analyze_symbol("FUTURES", network, quote, strategy_settings=settings)


def _analyze_spot(
    network: str,
    quote: dict[str, Any],
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    settings = strategy_settings if strategy_settings is not None else quote.get("_strategySettings")
    return _analyze_symbol("SPOT", network, quote, strategy_settings=settings)


def _model_plan_context(
    quote: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    *,
    stale: bool,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Build display/context metadata without constructing a classic plan."""

    context = {
        key: value
        for key, value in quote.items()
        if not str(key).startswith("_")
    }
    context.update(
        {
            "marketType": "FUTURES",
            "strategySettings": settings,
            "stale": bool(stale),
            "lastPrice": _round_number(_number(quote.get("lastPrice"))),
            "latestPrice": _round_number(_number(quote.get("lastPrice"))),
            "markPrice": _round_number(_number(quote.get("markPrice"))) if _number(quote.get("markPrice")) > 0 else None,
            "quoteVolume": _round_number(_number(quote.get("quoteVolume"))),
            "timeframes": {
                interval: _timeframe_payload(_read_market(frames[interval], interval))
                for interval in MODEL_ANALYSIS_INTERVALS
                if interval in frames
            },
            "reasons": ["时序模型直接读取 4h、1h、15m、5m 已收盘K线生成交易计划。"],
            "missingConditions": [],
            "cancellationConditions": [],
            "sourcePages": SOURCE_PAGES,
            "sourcePolicy": {"id": "trading-discipline-source-policy", "version": "v2", "localOnly": True},
            "profile": {"id": PROFILE_ID, "version": PROFILE_VERSION},
            "automatedOrder": False,
        }
    )
    return context


def _model_wait_plan(
    quote: dict[str, Any],
    settings: dict[str, Any],
    *,
    stale: bool,
    reason: str,
    frames: dict[str, list[dict[str, float]]] | None = None,
) -> dict[str, Any]:
    """Return a non-executable MODEL result without exposing classic levels."""

    context = (
        _model_plan_context(quote, frames, stale=stale, settings=settings)
        if isinstance(frames, dict) and all(interval in frames for interval in PRIMARY_ANALYSIS_INTERVALS)
        else {
            key: value
            for key, value in quote.items()
            if not str(key).startswith("_")
        }
    )
    branch = str(quote.get("_modelBranch") or MODEL_BRANCH_BEST).upper()
    run_id = str(quote.get("_modelRunId") or "").strip() or None
    context.update(
        {
            "marketType": "FUTURES",
            "strategySettings": settings,
            "direction": "WAIT",
            "status": "WAIT",
            "conditionMet": 0,
            "conditionTotal": 10,
            "conditionCompleteness": 0,
            "quality": 0,
            "trialEligible": False,
            "strategyEngine": STRATEGY_ENGINE_MODEL,
            "modelTask": "DIRECT_PLAN",
            "modelRunId": run_id,
            "modelBranch": branch,
            "modelGenerated": False,
            "modelReason": reason,
            "reason": reason,
            "entry": {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"},
            "stopLoss": None,
            "takeProfits": [],
            "trailingStop": {},
            "timeCost": {},
            "modelStrategy": {
                "engine": STRATEGY_ENGINE_MODEL,
                "branch": branch,
                "runId": run_id,
                "active": False,
                "fallback": reason,
            },
        }
    )
    return context


def _analyze_symbol(
    market_type: str,
    network: str,
    quote: dict[str, Any],
    *,
    strategy_settings: object = None,
) -> dict[str, Any] | None:
    analysis_started = time.perf_counter()
    timing: dict[str, Any] = {
        "intervalFetchMs": {},
        "klineFetchWallMs": 0.0,
        "modelInferenceMs": 0.0,
        "strategyBuildMs": 0.0,
    }

    def finish(result: dict[str, Any] | None) -> dict[str, Any] | None:
        if isinstance(result, dict):
            timing["totalMs"] = _elapsed_ms(analysis_started)
            result["_analysisTiming"] = timing
        return result

    safe_market_type = _market_type(market_type)
    settings = _strategy_settings(strategy_settings if strategy_settings is not None else quote.get("_strategySettings"))
    short_term_mode = _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM
    symbol = str(quote.get("symbol") or "")
    if not symbol:
        return None
    kline_loader = get_futures_klines if safe_market_type == "FUTURES" else get_spot_klines
    frames: dict[str, list[dict[str, float]]] = {}
    stale = bool(quote.get("_marketStale"))
    defer_micro = bool(quote.get("_deferMicro"))
    include_model_frames = safe_market_type == "FUTURES" and bool(quote.get("_includeModelFrames"))
    model_prediction_enabled = bool(quote.get("_modelPredictionEnabled"))
    live_model_bars: dict[str, dict[str, float]] = {}
    if include_model_frames and not model_prediction_enabled:
        return finish(_model_wait_plan(
            quote,
            settings,
            stale=stale,
            reason="MODEL_UNAVAILABLE",
        ))
    # Direct MODEL inference requires the four completed model streams.
    # Classic scans may defer 5m in midline mode, while classic short-term
    # mode still loads it as its execution frame.
    if include_model_frames:
        intervals = MODEL_ANALYSIS_INTERVALS
    elif defer_micro and not short_term_mode:
        intervals = PRIMARY_ANALYSIS_INTERVALS
    else:
        intervals = (*PRIMARY_ANALYSIS_INTERVALS, ENTRY_TIMING_INTERVAL)
    kline_fetch_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(intervals), thread_name_prefix=f"{safe_market_type.lower()}-timeframe") as executor:
        requests = {
            executor.submit(
                kline_loader,
                network,
                symbol,
                interval,
                MODEL_KLINE_LIMITS.get(interval, KLINE_LIMIT)
                if include_model_frames
                else KLINE_LIMIT,
                with_meta=True,
            ): (interval, time.perf_counter())
            for interval in intervals
        }
        for future in as_completed(requests):
            interval, request_started = requests[future]
            try:
                result = future.result()
            except Exception:
                # 5m is only an entry/short-term reference. A temporary
                # failure there must not hide a valid classic 4h/1h/15m
                # plan. MODEL has no valid fallback without its 5m stream.
                if interval == ENTRY_TIMING_INTERVAL and not include_model_frames:
                    continue
                raise
            timing["intervalFetchMs"][interval] = _elapsed_ms(request_started)
            if not isinstance(result, dict):
                if interval == ENTRY_TIMING_INTERVAL:
                    continue
                raise ValueError(f"{interval} K线响应格式无效")
            raw_items = result.get("items") or []
            # Direct MODEL uses 5m as its causal/execution anchor.  Keep the
            # forming 5m bar only for trigger-crossing diagnostics; it is not
            # included in the feature tensor, which remains completed bars.
            if include_model_frames and interval == ENTRY_TIMING_INTERVAL:
                live_bar = _incomplete_bar(raw_items)
                if live_bar is not None:
                    live_model_bars[interval] = live_bar
            bars = _completed_bars(raw_items)
            minimum_bars = (
                MIN_SHORT_TERM_READING_BARS
                if interval == ENTRY_TIMING_INTERVAL and short_term_mode
                else MIN_ENTRY_TIMING_BARS
                if interval == ENTRY_TIMING_INTERVAL
                else MIN_PRIMARY_BARS
            )
            if len(bars) < minimum_bars:
                if interval == ENTRY_TIMING_INTERVAL and not include_model_frames:
                    continue
                return None
            frames[interval] = bars
            if interval != ENTRY_TIMING_INTERVAL:
                stale = stale or bool(result.get("stale"))
    timing["klineFetchWallMs"] = _elapsed_ms(kline_fetch_started)
    required_intervals = MODEL_ANALYSIS_INTERVALS if include_model_frames else _strategy_intervals(settings)
    if any(interval not in frames for interval in required_intervals):
        return None
    if include_model_frames:
        context = _model_plan_context(quote, frames, stale=stale, settings=settings)
        model_frames = {interval: list(value) for interval, value in frames.items()}
        if live_model_bars:
            model_frames["_liveBars"] = live_model_bars
        model_inference_started = time.perf_counter()
        direct_plan = predict_binance_futures_frames(
            model_frames,
            network=network,
            plan=context,
            branch=str(quote.get("_modelBranch") or MODEL_BRANCH_BEST),
            allow_rejected_research_model=True,
            model_run_id=str(quote.get("_modelRunId") or "").strip() or None,
        )
        timing["modelInferenceMs"] = _elapsed_ms(model_inference_started)
        if direct_plan is None:
            return finish(_model_wait_plan(
                quote,
                settings,
                stale=stale,
                reason="MODEL_INVALID",
                frames=frames,
            ))
        return finish(direct_plan)
    four_hour = _read_market(frames["4h"], "4h")
    one_hour = _read_market(frames["1h"], "1h")
    micro_bars = frames.get(ENTRY_TIMING_INTERVAL)
    five_minute = (
        _read_market(micro_bars, ENTRY_TIMING_INTERVAL)
        if isinstance(micro_bars, list) and len(micro_bars) >= MIN_SHORT_TERM_READING_BARS
        else None
    )
    strategy_build_started = time.perf_counter()
    plan = _build_plan(
        quote,
        frames["15m"],
        four_hour,
        one_hour,
        stale,
        market_type=safe_market_type,
        bars_5m=micro_bars,
        five_minute=five_minute,
        strategy_settings=strategy_settings,
    )
    timing["strategyBuildMs"] += _elapsed_ms(strategy_build_started)
    if not defer_micro or ENTRY_TIMING_INTERVAL in frames or not _plan_is_actionable(plan):
        return finish(_with_model_frames(plan, frames, include_model_frames))

    # Complete the optional short-term layer only after the macro screen has
    # identified a usable result.  A failure here must not erase a valid
    # macro plan because 5m does not contribute to completeness.
    try:
        micro_result = kline_loader(network, symbol, ENTRY_TIMING_INTERVAL, KLINE_LIMIT, with_meta=True)
    except Exception:
        return finish(plan)
    if not isinstance(micro_result, dict):
        return finish(plan)
    micro_bars = _completed_bars(micro_result.get("items") or [])
    if len(micro_bars) < MIN_ENTRY_TIMING_BARS:
        return finish(_with_model_frames(plan, frames, include_model_frames))
    frames[ENTRY_TIMING_INTERVAL] = micro_bars
    strategy_build_started = time.perf_counter()
    plan = _build_plan(
        quote,
        frames["15m"],
        four_hour,
        one_hour,
        stale,
        market_type=safe_market_type,
        bars_5m=micro_bars,
        five_minute=(
            _read_market(micro_bars, ENTRY_TIMING_INTERVAL)
            if len(micro_bars) >= MIN_SHORT_TERM_READING_BARS
            else None
        ),
        strategy_settings=strategy_settings,
    )
    timing["strategyBuildMs"] += _elapsed_ms(strategy_build_started)
    return finish(_with_model_frames(plan, frames, include_model_frames))


def _with_model_frames(
    plan: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    enabled: bool,
) -> dict[str, Any]:
    """Keep completed bars private until scanner-side batched inference."""

    if enabled and all(interval in frames for interval in PRIMARY_ANALYSIS_INTERVALS):
        plan["_mlFrames"] = {
            interval: list(frames[interval])
            for interval in PRIMARY_ANALYSIS_INTERVALS
            if interval in frames
        }
    return plan


def _completed_bars(raw_bars: list[dict[str, Any]]) -> list[dict[str, float]]:
    now = int(time.time() * 1000)
    bars = []
    for raw in raw_bars:
        try:
            open_time = int(raw.get("openTime") or 0)
            close_time = int(raw.get("closeTime") or 0)
            bar = {
                "openTime": float(open_time),
                "closeTime": float(close_time),
                "open": float(raw.get("open") or 0),
                "high": float(raw.get("high") or 0),
                "low": float(raw.get("low") or 0),
                "close": float(raw.get("close") or 0),
                "volume": float(raw.get("volume") or 0),
            }
        except (TypeError, ValueError):
            continue
        if close_time < now and bar["high"] >= bar["low"] > 0 and bar["close"] > 0:
            bars.append(bar)
    return bars


def _incomplete_bar(raw_bars: list[dict[str, Any]]) -> dict[str, float] | None:
    """Return the currently forming bar for trigger invalidation only.

    Direct-model features and predictions continue to use ``_completed_bars``.
    Keeping this one raw bar separately lets the execution gate notice a
    trigger that was crossed and then retraced inside the live candle without
    introducing a 1m request or leaking the unfinished candle into training.
    """

    now = int(time.time() * 1000)
    for raw in reversed(raw_bars or []):
        try:
            close_time = int(raw.get("closeTime") or 0)
            bar = {
                "openTime": float(raw.get("openTime") or 0),
                "closeTime": float(close_time),
                "open": float(raw.get("open") or 0),
                "high": float(raw.get("high") or 0),
                "low": float(raw.get("low") or 0),
                "close": float(raw.get("close") or 0),
                "volume": float(raw.get("volume") or 0),
            }
        except (TypeError, ValueError):
            continue
        if close_time >= now and bar["high"] >= bar["low"] > 0 and bar["close"] > 0:
            return bar
    return None


def _read_market(bars: list[dict[str, float]], timeframe: str) -> MarketReading:
    closes = [bar["close"] for bar in bars]
    ema20 = _ema(closes, 20)[-1]
    ema50_values = _ema(closes, 50)
    ema50 = ema50_values[-1]
    atr = _atr(bars, 14)[-1]
    window = bars[-20:]
    range_low = min(bar["low"] for bar in window)
    range_high = max(bar["high"] for bar in window)
    range_mid = (range_low + range_high) / 2
    close = closes[-1]
    ema20_values = _ema(closes, 20)
    slope = (ema20_values[-1] - ema20_values[-6]) / max(atr, 0.00000001)
    highs_recent = max(bar["high"] for bar in bars[-8:])
    highs_prior = max(bar["high"] for bar in bars[-16:-8])
    lows_recent = min(bar["low"] for bar in bars[-8:])
    lows_prior = min(bar["low"] for bar in bars[-16:-8])
    up_score = int(ema20 > ema50) + int(slope >= 0.18) + int(close >= ema20) + int(highs_recent > highs_prior) + int(lows_recent >= lows_prior)
    down_score = int(ema20 < ema50) + int(slope <= -0.18) + int(close <= ema20) + int(highs_recent <= highs_prior) + int(lows_recent < lows_prior)
    overlap = _overlap_ratio(window[-12:])
    range_width_atr = (range_high - range_low) / max(atr, 0.00000001)
    range_like = overlap >= 0.42 and abs(slope) <= 0.45 and range_width_atr <= 16
    if range_like:
        state, bias, score = "RANGE", "NEUTRAL", 0
    elif up_score >= TREND_STATE_SCORE_THRESHOLD and up_score > down_score:
        state, bias, score = "BULL_TREND", "BULL", up_score
    elif down_score >= TREND_STATE_SCORE_THRESHOLD and down_score > up_score:
        state, bias, score = "BEAR_TREND", "BEAR", -down_score
    else:
        state, bias, score = "TRANSITION", "NEUTRAL", up_score - down_score
    evidence = [
        f"{timeframe} 已收盘K线，收盘 { _format_price(close) }，20EMA { _format_price(ema20) }。",
        f"EMA 斜率约 { _round_number(slope) } ATR，近段高低点结构已纳入判断。",
    ]
    if state == "RANGE":
        evidence.append(f"近20根区间 { _format_price(range_low) } - { _format_price(range_high) }，中线 { _format_price(range_mid) }。")
    return MarketReading(state, bias, close, ema20, ema50, atr, range_low, range_high, range_mid, score, evidence)


def _build_plan(
    quote: dict[str, Any],
    bars_15m: list[dict[str, float]],
    four_hour: MarketReading,
    one_hour: MarketReading,
    stale: bool,
    *,
    market_type: str = "FUTURES",
    bars_5m: list[dict[str, float]] | None = None,
    five_minute: MarketReading | None = None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    safe_market_type = _market_type(market_type)
    settings = _strategy_settings(strategy_settings)
    latest = bars_15m[-1]
    previous = bars_15m[-2]
    reading_15m = _read_market(bars_15m, "15m")
    directions = ["LONG"] if safe_market_type == "SPOT" else ["LONG", "SHORT"]
    plans = [
        _direction_plan(
            direction,
            quote,
            bars_15m,
            latest,
            previous,
            four_hour,
            one_hour,
            reading_15m,
            stale,
            market_type=safe_market_type,
            bars_5m=bars_5m,
            five_minute=five_minute,
            strategy_settings=settings,
        )
        for direction in directions
    ]
    plans.sort(key=_sort_key, reverse=True)
    return plans[0]


def _direction_plan(
    direction: str,
    quote: dict[str, Any],
    bars: list[dict[str, float]],
    latest: dict[str, float],
    previous: dict[str, float],
    four_hour: MarketReading,
    one_hour: MarketReading,
    fifteen: MarketReading,
    stale: bool,
    *,
    market_type: str = "FUTURES",
    bars_5m: list[dict[str, float]] | None = None,
    five_minute: MarketReading | None = None,
    strategy_settings: object = None,
) -> dict[str, Any]:
    safe_market_type = _market_type(market_type)
    settings = _strategy_settings(strategy_settings)
    short_term_mode = _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM
    model_mode = _strategy_engine(settings) == STRATEGY_ENGINE_MODEL
    execution_on_5m = short_term_mode or model_mode
    execution_interval = ENTRY_TIMING_INTERVAL if execution_on_5m else "15m"
    execution_bars = (
        bars_5m
        if execution_on_5m and isinstance(bars_5m, list) and len(bars_5m) >= MIN_SHORT_TERM_READING_BARS
        else bars
    )
    execution_available = (
        isinstance(bars_5m, list)
        and len(bars_5m) >= MIN_SHORT_TERM_READING_BARS
        if execution_on_5m
        else True
    )
    is_long = direction == "LONG"
    expected_bias = "BULL" if is_long else "BEAR"
    # Analysis and target progress follow the latest traded price.  Mark
    # price remains available for risk/stop consumers and must not silently
    # replace the price used to describe an entry opportunity.
    last_price = _number(quote.get("lastPrice")) or _number(quote.get("markPrice")) or latest["close"]
    mark_price = _number(quote.get("markPrice"))
    price = last_price
    atr = max(five_minute.atr if model_mode and five_minute is not None else fifteen.atr, price * (0.0002 if model_mode else 0.0004))
    execution_latest = execution_bars[-1]
    execution_previous = execution_bars[-2]
    execution_atr = (
        max(five_minute.atr, price * 0.0002)
        if execution_on_5m and five_minute is not None
        else atr
    )
    range_size = max(execution_latest["high"] - execution_latest["low"], execution_atr * 0.15)
    close_position = (execution_latest["close"] - execution_latest["low"]) / range_size
    signal_close = (
        close_position >= SIGNAL_CLOSE_POSITION_THRESHOLD
        if is_long
        else close_position <= 1 - SIGNAL_CLOSE_POSITION_THRESHOLD
    )
    signal_color = execution_latest["close"] > execution_latest["open"] if is_long else execution_latest["close"] < execution_latest["open"]
    execution_opposite = (
        execution_on_5m
        and five_minute is not None
        and five_minute.bias not in {"NEUTRAL", expected_bias}
    )
    signal_bar = signal_close and signal_color and execution_available and not execution_opposite
    pullback_window = bars[-8:-1]
    non_trend_pullback_near_ema = (
        min(bar["low"] for bar in pullback_window) <= fifteen.ema20 + atr * PULLBACK_EMA_DISTANCE_ATR
        if is_long
        else max(bar["high"] for bar in pullback_window) >= fifteen.ema20 - atr * PULLBACK_EMA_DISTANCE_ATR
    )
    trigger = (
        max(execution_latest["high"], execution_previous["high"]) + execution_atr * 0.05
        if is_long
        else min(execution_latest["low"], execution_previous["low"]) - execution_atr * 0.05
    )
    signal_zone_low = min(execution_latest["low"], execution_previous["low"])
    signal_zone_high = max(execution_latest["high"], execution_previous["high"])
    zone_low, zone_high = (signal_zone_low, trigger) if is_long else (trigger, signal_zone_high)
    # Midline keeps 4h as the environment source. Short-term mode moves that
    # decision down to 1h and retains 4h only as a hidden opposite-direction
    # veto; this is the 1h -> 15m -> 5m execution route described in the UI.
    environment_reading = one_hour if short_term_mode or model_mode else four_hour
    mode = "TREND" if environment_reading.state in {"BULL_TREND", "BEAR_TREND"} else ("RANGE" if environment_reading.state == "RANGE" else "REBOUND")
    reversal_context = _reversal_context(bars, direction) if mode == "REBOUND" else None
    trend_entry_context = _trend_entry_context(bars, direction, atr, fifteen.ema20) if mode == "TREND" else None
    minimum_target_r = (
        settings["rangeMinimumTargetR"]
        if mode == "RANGE"
        else REBOUND_MINIMUM_TARGET_R
        if mode == "REBOUND"
        else settings["trendMinimumTargetR"]
    )
    level_basis = _initial_level_basis(
        bars,
        direction=direction,
        trigger=trigger,
        atr=atr,
        strategy=settings["levelStrategy"],
        platform_allowed=(mode != "REBOUND" or bool((reversal_context or {}).get("passed"))),
        structure_bars=(trend_entry_context or {}).get("structureBars") if mode == "TREND" else None,
    )
    structure_edge = float(level_basis["initialStop"]["structureEdge"])
    structure_stop = structure_edge - atr * settings["structureStopAtrMultiplier"] if is_long else structure_edge + atr * settings["structureStopAtrMultiplier"]
    stop = _force_stop_outside_zone(
        structure_stop,
        zone_low,
        zone_high,
        is_long,
        atr,
        buffer_multiplier=settings["structureStopAtrMultiplier"],
        zone_buffer_multiplier=settings["triggerZoneStopBufferAtrMultiplier"],
    )
    zone_buffer = _trigger_zone_stop_buffer(
        zone_low,
        zone_high,
        atr,
        atr_multiplier=settings["triggerZoneStopBufferAtrMultiplier"],
    )
    defense_boundary = min(zone_low, zone_high) if is_long else max(zone_low, zone_high)
    zone_buffer_stop = defense_boundary - zone_buffer if is_long else defense_boundary + zone_buffer
    zone_buffer_enforced = zone_buffer_stop < structure_stop if is_long else zone_buffer_stop > structure_stop
    initial_stop_basis = level_basis["initialStop"]
    initial_stop_basis["triggerZoneBuffer"] = {
        "atrMultiplier": _round_number(settings["triggerZoneStopBufferAtrMultiplier"]),
        "zoneSpanFraction": TRIGGER_ZONE_STOP_BUFFER_SPAN_FRACTION,
        "minimumDistance": _round_number(zone_buffer),
        "defenseBoundary": _round_number(defense_boundary),
        "enforced": zone_buffer_enforced,
        "policy": "止损至少越过触发区防守侧的波动/区间缓冲；回撤或回测结构极值更远时仍取更远者。",
    }
    if zone_buffer_enforced:
        initial_stop_basis["label"] = f"{initial_stop_basis['label']}；触发区防假突破缓冲"
    risk = (trigger - stop) if is_long else (stop - trigger)
    risk = max(risk, atr * 0.35)
    current_location_in_range = (
        (price - environment_reading.range_low)
        / max(environment_reading.range_high - environment_reading.range_low, atr)
    )
    range_edge_fraction = settings["rangeEdgeFraction"]
    at_range_edge = (
        current_location_in_range <= range_edge_fraction
        if is_long
        else current_location_in_range >= 1 - range_edge_fraction
    )
    range_fade = mode == "RANGE" and at_range_edge
    target_environment = one_hour if short_term_mode else four_hour
    target_one, target_two, target_one_source, target_two_source = _target_levels(
        direction=direction,
        trigger=trigger,
        risk=risk,
        mode=mode,
        range_fade=range_fade,
        # In short-term mode the 1h range is the executable environment.  The
        # 4h reading remains available above solely for the opposite-trend veto.
        four_hour=target_environment,
        one_hour=one_hour,
        fifteen=fifteen,
        target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    target_one, target_two, target_one_source, target_two_source, target_basis = _apply_platform_targets(
        direction=direction,
        trigger=trigger,
        risk=risk,
        minimum_target_r=minimum_target_r,
        basis=level_basis,
        fallback_first=target_one,
        fallback_second=target_two,
        fallback_first_source=target_one_source,
        fallback_second_source=target_two_source,
        atr=atr,
        target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    # The raw two-candle signal range can be much wider than the executable
    # risk. Keep its structural stop (already calculated above) unchanged,
    # but expose a bounded execution zone so its lower/upper entries retain
    # comparable first-target risk/reward.
    zone_low, zone_high, entry_zone_geometry = _bound_trigger_zone_for_risk_reward(
        zone_low,
        zone_high,
        trigger=trigger,
        stop=stop,
        first_target=target_one,
        is_long=is_long,
    )
    initial_stop_basis["entryZoneGeometry"] = entry_zone_geometry
    protective_target, protective_target_source, protective_target_timeframe = _near_term_target(
        direction=direction,
        trigger=trigger,
        risk=risk,
        mode=mode,
        bars_15m=bars,
        bars_5m=bars_5m,
        five_minute=five_minute,
        main_target=target_one,
        minimum_target_r=settings["nearTermMinimumTargetR"],
        target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    target_basis["executionBuffer"] = {
        "atrMultiplier": TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
        "policy": "目标区前侧提前成交，做多下移、做空上移；不要求触及技术位极值。",
    }
    target_basis["protectiveTarget"] = {
        "price": _round_number(protective_target),
        "source": protective_target_source,
        "timeframe": protective_target_timeframe,
        "available": protective_target is not None,
    }
    level_basis["targets"] = target_basis
    platform_target_exception = mode == "RANGE" and range_fade and target_one is not None
    if settings["levelStrategy"] == LEVEL_STRATEGY_PLATFORM:
        if level_basis["initialStop"]["usesPlatform"] and target_basis["usesPlatform"]:
            level_basis["resolvedStrategy"] = LEVEL_STRATEGY_PLATFORM
        elif level_basis["initialStop"]["usesPlatform"] and platform_target_exception:
            level_basis["resolvedStrategy"] = "CONFIRMED_PLATFORM_RANGE_MIDPOINT"
        else:
            level_basis["resolvedStrategy"] = "STRUCTURE_EXTREME_FALLBACK"
    # A confirmed platform is a preferred structural source, not an extra
    # condition from the discipline rules.  When the detector has no valid
    # repeated platform, _initial_level_basis already retains the recent
    # structural extreme and _apply_platform_targets retains the structural
    # target.  Those fallbacks remain executable when the ordinary stop and
    # target-space checks below pass.
    level_route_ready = math.isfinite(structure_edge) and structure_edge > 0
    level_basis["executionReady"] = level_route_ready
    if not level_route_ready:
        level_basis["executionBlockReason"] = (
            "结构止损边界无效，不能生成可执行计划。"
        )
    entry_timing = _entry_timing(
        bars_5m,
        direction,
        price,
        trigger,
        mode=mode,
        range_low=environment_reading.range_low,
        range_high=environment_reading.range_high,
        stop_loss=stop,
        reversal_context=reversal_context,
        range_timeframe="1h" if short_term_mode else "4h",
    )
    # The macro plan remains valid while it waits for a short-lived 5m
    # confirmation. The replay/execution layer turns RETEST_REQUIRED plans
    # into a pending state instead of treating this initial signal as a fill.
    micro_ok = entry_timing.get("state") == "READY"
    micro_reason = str(entry_timing.get("reason") or "5m 入场结构尚未确认。")
    micro_evidence = [
        f"{execution_interval} 入场模板：{entry_timing.get('setup') or '等待确认'}。",
        micro_reason,
    ]
    if mode == "RANGE":
        position_ok = at_range_edge
        position_label = "15m 位于区间边缘"
        position_reason = "15m 价格位于区间边缘，第一目标优先看中线。"
        position_missing = "价格仍在区间中部，区间中部不追单。"
    elif mode == "TREND":
        position_ok = bool((trend_entry_context or {}).get("passed"))
        position_label = "15m 回撤/回测新鲜且非趋势末端"
        position_reason = str((trend_entry_context or {}).get("reason") or "15m 新鲜回撤结构已确认。")
        position_missing = str((trend_entry_context or {}).get("missing") or "等待新的已收盘回撤/回测。")
    else:
        position_ok = non_trend_pullback_near_ema
        position_label = "15m 反转后回测位置可定义风险"
        position_reason = "15m 回撤或浅回调仍在可接受的 EMA 距离内，可定义风险。"
        position_missing = "15m 回撤距离 20EMA 过远，等待更合理的回调位置。"
    risk_reward = abs(target_one - trigger) / risk if target_one is not None and risk > 0 else 0.0
    trigger_pending = price < trigger if is_long else price > trigger
    four_hour_veto_clear = _four_hour_veto_is_clear(four_hour, expected_bias)
    if mode == "TREND":
        environment_ok = (
            one_hour.bias == expected_bias and four_hour_veto_clear
            if short_term_mode
            else four_hour.bias == expected_bias
        )
        one_hour_ok = _direction_context_is_compatible(one_hour, expected_bias)
        fifteen_direction_ok = _direction_context_is_compatible(fifteen, expected_bias)
        fifteen_pullback_trial = (
            not fifteen_direction_ok
            and signal_bar
            and position_ok
            and trigger_pending
            and (
                _trend_pullback_trial_context(
                    direction,
                    four_hour,
                    one_hour,
                    fifteen,
                    signal_bar=signal_bar,
                    position_ok=position_ok,
                    trigger_pending=trigger_pending,
                )
                if not short_term_mode
                else one_hour.bias == expected_bias and four_hour_veto_clear
            )
        )
    elif mode == "RANGE":
        # A range fade is selected by location at the edge, not by a trend
        # label. In short-term mode the edge belongs to 1h; 4h can be neutral
        # but a confirmed opposite trend remains a hard conflict.
        environment_ok = (
            environment_reading.state == "RANGE"
            and at_range_edge
            and (four_hour_veto_clear if short_term_mode else True)
        )
        one_hour_ok = _direction_context_is_compatible(one_hour, expected_bias)
        fifteen_direction_ok = _direction_context_is_compatible(fifteen, expected_bias)
        fifteen_pullback_trial = False
    else:
        # REBOUND is a transition with evidence, not a free pass for either
        # direction. At least one lower timeframe must already point toward
        # the candidate direction; 5m only confirms timing. Short-term mode
        # anchors the transition in 1h while retaining the 4h veto.
        environment_ok = (
            environment_reading.state == "TRANSITION"
            and (one_hour.bias == expected_bias or fifteen.bias == expected_bias)
            and (four_hour_veto_clear if short_term_mode else True)
        )
        one_hour_ok = one_hour.bias == expected_bias
        fifteen_direction_ok = fifteen.bias == expected_bias
        fifteen_pullback_trial = False
    structure_stop_ok = _stop_is_outside_zone(stop, trigger, zone_low, zone_high, is_long)
    first_target_direction_ok = target_one is not None and (target_one > trigger if is_long else target_one < trigger)
    target_space_ok = target_one is not None and first_target_direction_ok and risk_reward >= minimum_target_r
    volume_bars = execution_bars if short_term_mode else bars
    volume_ratio = execution_latest["volume"] / max(sum(bar["volume"] for bar in volume_bars[-21:-1]) / 20, 0.00000001)
    macro_volume_ratio = latest["volume"] / max(sum(bar["volume"] for bar in bars[-21:-1]) / 20, 0.00000001)
    volume_ok = volume_ratio >= RELATIVE_VOLUME_MIN_RATIO
    if mode == "TREND":
        if short_term_mode:
            environment_reason = (
                f"1h { '多头' if is_long else '空头' }环境成立，4h 未出现相反强趋势否决。"
                if environment_ok
                else "4h 出现与短线计划相反的强趋势，触发方向否决。"
                if not four_hour_veto_clear
                else f"1h 尚未形成可执行的{ '多头' if is_long else '空头' }环境。"
            )
        else:
            environment_reason = (
                f"4h { '多头' if is_long else '空头' }趋势与方向一致。"
                if environment_ok
                else f"4h 当前为{ '空头' if is_long else '多头' }背景，与计划方向冲突。"
            )
    elif mode == "RANGE":
        environment_reason = (
            f"{ '1h' if short_term_mode else '4h' } 区间靠近{ '下沿' if is_long else '上沿' }，不在中部追价。"
            if environment_ok
            else "4h 出现相反强趋势否决，或 1h 尚未形成可在边缘交易的区间。"
            if short_term_mode
            else "4h 尚未形成可在边缘交易的区间，不能把过渡行情当成区间反转。"
        )
    else:
        environment_reason = (
            f"{ '1h' if short_term_mode else '4h' } 处于过渡环境，低周期已有同向反转证据。"
            if environment_ok
            else "4h 出现相反强趋势否决，或 1h 尚未进入可确认的过渡反转环境。"
            if short_term_mode
            else "4h 尚未进入可确认的过渡反转环境，不能仅凭低周期信号入场。"
        )
    signal_condition = {
        "id": "signalBar",
        "label": f"{execution_interval} 已收盘信号 K",
        "passed": signal_bar,
        "reason": (
            f"{execution_interval} 出现收盘靠近{ '高位' if is_long else '低位' }的方向信号 K。"
            + ("且没有覆盖 4h 背景的反向执行结构。" if short_term_mode else "")
        ),
        "missing": f"等待 {execution_interval} 出现合格信号 K 并突破触发位。",
    }
    if mode == "REBOUND":
        reversal_details = reversal_context or {}
        reversal_ok = bool(reversal_details.get("passed"))
        signal_condition = {
            "id": "reversalStructure",
            "label": "15m 反转架构与当前方向信号 K 完整",
            "passed": reversal_ok and signal_bar,
            "reason": (
                f"{reversal_details.get('reason') or '15m 反转架构已确认。'} 当前方向信号 K 已收盘确认。"
                if reversal_ok and signal_bar
                else str(reversal_details.get("reason") or "15m 反转架构尚未确认。")
                if not reversal_ok
                else "15m 反转架构已确认，等待当前方向信号 K 收盘确认。"
            ),
            "missing": (
                str(reversal_details.get("missing") or "等待 15m 反转架构完成。")
                if not reversal_ok
                else "等待 15m 出现合格的当前方向信号 K，不以反向 K 代替确认。"
            ),
        }
    condition_checks = [
        {
            "id": "dataFresh",
            "label": "分析周期均为已收盘且非缓存行情",
            "passed": not stale,
            "reason": "行情为最新成功读取。",
            "missing": "行情来自最近成功缓存，恢复实时数据后重新确认。",
        },
        {
            "id": "environment",
            "label": "1h 环境与位置符合方向，且 4h 未触发反向否决" if short_term_mode else "4h 背景与位置符合方向",
            "passed": environment_ok,
            "reason": environment_reason,
            "missing": (
                "等待 1h 环境与位置成立，且 4h 不出现相反强趋势；不能仅凭 5m 信号入场。"
                if short_term_mode
                else "4h 背景未与方向一致，或区间仍在中部，或反弹尚无低周期同向证据。"
            ),
        },
        {
            "id": "oneHourDirection",
            "label": "1h 方向与计划一致",
            "passed": one_hour_ok,
            "reason": (
                f"1h 结构与 { '多头' if is_long else '空头' }方向一致。"
                if one_hour_ok and one_hour.bias == expected_bias
                else "1h 处于区间/过渡状态，暂未与计划方向形成冲突。"
                if one_hour_ok
                else f"1h 当前为{ '空头' if is_long else '多头' }结构，与计划方向相反。"
            ),
            "missing": "等待 1h 结构与计划方向一致，不用反向结构替代确认。",
        },
        {
            "id": "fifteenDirection",
            "label": "15m 方向与计划一致",
            "passed": fifteen_direction_ok,
            "reason": (
                f"15m 结构与 { '多头' if is_long else '空头' }方向一致。"
                if fifteen_direction_ok
                else f"15m 当前为{ '空头' if is_long else '多头' }结构，但 4h/1h 同向且当前位置、收盘信号和触发价均符合高周期趋势回撤；仍保留为唯一方向缺口，只允许 9/10 试错。"
                if fifteen_pullback_trial
                else f"15m 当前为{ '空头' if is_long else '多头' }结构，与计划方向相反，尚未形成可确认的高周期趋势回撤。"
            ),
            "missing": (
                "15m 尚未完全恢复计划方向；该趋势回撤仅可作为 9/10 试错，不能算完整计划。"
                if fifteen_pullback_trial
                else "等待 15m 结构与计划方向一致，反向 K 不能覆盖方向条件。"
            ),
        },
        signal_condition,
        {
            "id": "pullback",
            "label": position_label,
            "passed": position_ok,
            "reason": position_reason,
            "missing": position_missing,
        },
        {
            "id": "triggerPending",
            "label": "触发价尚未被当前价格越过",
            "passed": trigger_pending,
            "reason": (
                "当前价格仍在触发价外侧，等待进场 K 确认。"
                if trigger_pending
                else "当前价格已经越过或触及触发价，不能把已错过的入场当作等待。"
            ),
            "missing": "当前价格已越过或触及触发价，重新读取收盘确认后再评估。",
        },
        {
            "id": "structuralStop",
            "label": "结构止损位于完整触发区外",
            "passed": structure_stop_ok,
            "reason": "结构止损位于信号与回撤结构的另一侧。",
            "missing": "结构止损不能落入触发区内部，暂不生成可执行计划。",
        },
        {
            "id": "targetSpace",
            "label": f"第一目标方向正确且空间不少于 {minimum_target_r:g}R",
            "passed": target_space_ok,
            "reason": f"第一目标空间约 {risk_reward:.2f}R，且目标顺着计划方向。{target_one_source}",
            "missing": f"第一目标方向或空间不足 {minimum_target_r:g}R，不追价。",
        },
        {
            "id": "relativeVolume",
        "label": f"{execution_interval} 相对成交量通过复核",
            "passed": volume_ok,
            "reason": f"{execution_interval} 相对成交量约 {volume_ratio:.2f}x。",
            "missing": f"{execution_interval} 相对成交量偏低，触发前需复核。",
        },
    ]
    condition_met = sum(1 for check in condition_checks if check["passed"])
    condition_total = len(condition_checks)
    condition_completeness = round(condition_met / condition_total * 100) if condition_total else 0
    reasons = [check["reason"] for check in condition_checks if check["passed"]]
    missing = [check["missing"] for check in condition_checks if not check["passed"]]
    missing_ids = {check["id"] for check in condition_checks if not check["passed"]}
    trial_blockers = {"dataFresh", "triggerPending", "structuralStop", "targetSpace", "reversalStructure"}
    trial_allowed_missing = set(TRIAL_ALLOWED_MISSING_CONDITIONS)
    if fifteen_pullback_trial:
        trial_allowed_missing.add("fifteenDirection")
    trial_eligible = (
        condition_met == condition_total - 1
        and missing_ids.issubset(trial_allowed_missing)
        and not missing_ids.intersection(trial_blockers)
        and level_route_ready
        and math.isfinite(risk)
        and risk > 0
    )
    status = "ARMED" if condition_met == condition_total else "WATCH"
    blocked_reasons: list[str] = []
    if stale:
        blocked_reasons.append("data_incomplete")
    if not math.isfinite(risk) or risk <= 0 or not structure_stop_ok:
        blocked_reasons.append("stop_unknown")
    if not target_space_ok:
        blocked_reasons.append("no_room")
    if not level_route_ready:
        blocked_reasons.append("level_route_unconfirmed")
    if bool((trend_entry_context or {}).get("terminalExtension")):
        blocked_reasons.append("late_trend_extension")
    if blocked_reasons:
        status = "BLOCKED"
    quality = 35
    quality += 25 if environment_ok else 0
    quality += 20 if one_hour_ok else 0
    quality += 14 if signal_bar else 0
    quality += 8 if position_ok else 0
    quality += 6 if volume_ok else 0
    quality += 7 if risk_reward >= minimum_target_r else 0
    quality += 5 if micro_ok else 0
    if stale:
        quality -= 25
    quality = max(0, min(100, int(quality)))
    cancellation = [
        f"{execution_interval} 完整K收盘{ '跌破' if is_long else '突破' }结构防守 { _format_price(stop) }，取消该计划。",
        f"触发后 1-3 根 {execution_interval} K 未守住触发区 { _format_price(zone_low) } - { _format_price(zone_high) }，取消并重新评估。",
        "若 1h 或 4h 出现相反方向的确认结构，不继续沿用本计划。",
    ]
    if mode == "RANGE":
        cancellation.append("5m 回到区间中线或边缘失败突破重新形成跟随时，取消边缘反向计划。")
    elif mode == "REBOUND":
        cancellation.append("15m 回测重新突破原趋势极点，或 5m 反转回测失守时，取消反转计划。")
    # Ratios on the plan are cumulative portions of the original position.
    # When no extension structure is confirmed, the first structural target
    # is also the final fixed target. Keep account defaults unchanged; freeze
    # the effective allocation on the target records so every executor agrees.
    effective_first_ratio = (
        settings["firstTakeProfitRatio"]
        if target_two is not None
        else max(settings["firstTakeProfitRatio"], settings["secondTakeProfitRatio"])
    )
    effective_second_ratio = settings["secondTakeProfitRatio"]
    target_records = []
    if protective_target is not None:
        target_records.append(
            {
                "role": "PROTECTIVE_TARGET",
                "label": "近端保护目标",
                "price": _round(protective_target),
                "rMultiple": round(abs(protective_target - trigger) / risk, 2),
                "source": protective_target_source,
                "timeframe": protective_target_timeframe,
                "cumulativeRatio": settings["protectiveTakeProfitRatio"],
                "legRatio": settings["protectiveTakeProfitRatio"],
                "orderType": "TAKE_PROFIT_MARKET",
                "workingType": "CONTRACT_PRICE",
                "priceBasis": "LATEST_PRICE",
                "semantics": "优先使用5m/15m已确认的近端磁力区，按最新成交价条件止盈；不是脱离结构的固定百分比。",
            }
        )
    target_records.append(
        {
            "role": "FIRST_TARGET",
            "label": "第一目标",
            "price": _round(target_one),
            "rMultiple": round(risk_reward, 2),
            "source": target_one_source,
            "timeframe": "5m/15m/1h" if short_term_mode else "15m/1h",
            "cumulativeRatio": effective_first_ratio,
            "legRatio": effective_first_ratio - (
                settings["protectiveTakeProfitRatio"] if protective_target is not None else 0
            ),
            "orderType": "TAKE_PROFIT_MARKET",
            "workingType": "CONTRACT_PRICE",
            "priceBasis": "LATEST_PRICE",
            "semantics": "前方主要结构磁力位的可达侧，按最新成交价条件分批止盈。",
        }
    )
    target_two_record = {
        "role": "EXTENSION_TARGET",
        "label": "第二目标",
        "price": _round(target_two),
        "rMultiple": round(abs(target_two - trigger) / risk, 2) if target_two else None,
        "source": target_two_source,
        "timeframe": "1h",
        "cumulativeRatio": effective_second_ratio,
        "legRatio": effective_second_ratio - effective_first_ratio,
        "orderType": "TAKE_PROFIT_MARKET",
        "workingType": "CONTRACT_PRICE",
        "priceBasis": "LATEST_PRICE",
        "available": bool(target_two),
        "semantics": "第一目标后的1h结构延伸，按最新成交价条件止盈；仅在趋势延续和新确认后管理，不机械外推。",
    }
    target_records.append(target_two_record)
    return {
        "symbol": quote.get("symbol"),
        "marketType": safe_market_type,
        "direction": direction,
        "status": status,
        "opportunityType": None,
        "opportunityLabel": None,
        "marketMode": mode,
        "strategyEngine": _strategy_engine(settings),
        "modelBranch": _model_branch(settings),
        "strategyMode": _strategy_mode(settings),
        "strategyModeLabel": _strategy_mode_label(settings),
        "timeframeRoles": {
            "4h": "背景方向与强反向否决" if short_term_mode else "主趋势/区间背景",
            "1h": "短线方向环境" if short_term_mode else "方向与结构确认",
            "15m": "位置、回撤与主要结构",
            "5m": "执行信号、触发与短线失败确认" if short_term_mode else "入场时机参考",
        },
        "levelStrategy": settings["levelStrategy"],
        "levelBasis": level_basis,
        "strategySettings": settings,
        "entry": {
            "trigger": _round(trigger),
            "zoneLow": _round(min(zone_low, zone_high)),
            "zoneHigh": _round(max(zone_low, zone_high)),
            "type": "CONDITIONAL_BREAKOUT",
            "structure": (trend_entry_context or {}).get("setup") if mode == "TREND" else None,
        },
        "stopLoss": _round(stop),
        "entryTiming": entry_timing,
        "trendEntryContext": {
            key: value
            for key, value in (trend_entry_context or {}).items()
            if key != "structureBars"
        } if mode == "TREND" else None,
        "entryConfirmation": {
            "mode": settings["entryConfirmationMode"],
            "required": settings["entryConfirmationMode"] == "RETEST_REQUIRED",
            "expiryBars": settings["entryConfirmationExpiryBars"],
            "state": entry_timing.get("state"),
            "setup": entry_timing.get("setup"),
        },
        "takeProfits": target_records,
        # Keep the exact room rule with the plan so an execution adapter can
        # re-check it if a confirmed retest changes the eventual entry price.
        "minimumTargetR": round(minimum_target_r, 2),
        "riskReward": round(risk_reward, 2),
        "confidence": condition_completeness,
        "conditionCompleteness": condition_completeness,
        "conditionMet": condition_met,
        "conditionTotal": condition_total,
        "conditionChecks": condition_checks,
        "trialEligible": trial_eligible,
        "trialMissingCondition": next(
            (check["id"] for check in condition_checks if not check["passed"]),
            None,
        ) if trial_eligible else None,
        "blockedReasons": blocked_reasons,
        "quality": quality,
        "lastPrice": _round(price),
        "latestPrice": _round(price),
        "markPrice": _round(mark_price) if mark_price > 0 else None,
        "quoteVolume": _round_number(_number(quote.get("quoteVolume"))),
        "fundingRate": quote.get("fundingRate"),
        "stale": stale,
        "timeframes": {
            "4h": _timeframe_payload(four_hour),
            "1h": _timeframe_payload(one_hour),
            "15m": _timeframe_payload(fifteen, extra=[f"15m 相对成交量约 {macro_volume_ratio:.2f}x。"]),
            **({"5m": _timeframe_payload(five_minute, extra=micro_evidence + [
                f"5m 相对成交量约 {volume_ratio:.2f}x。",
                "短线模式：5m 只负责执行信号与触发，不能单独创造方向，也不能覆盖 4h 强反向趋势。"
                if short_term_mode
                else "中线模式：5m 仅用于入场时机确认，不覆盖 1h/4h 背景。",
            ])} if five_minute else {}),
        },
        "reasons": reasons,
        "missingConditions": missing,
        "cancellationConditions": cancellation,
        "sourcePages": SOURCE_PAGES,
        "sourcePolicy": {"id": "trading-discipline-source-policy", "version": "v2", "localOnly": True},
        "profile": {"id": PROFILE_ID, "version": PROFILE_VERSION},
        "automatedOrder": False,
    }


def _model_side_details(plan: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    prediction = plan.get("modelPrediction") if isinstance(plan.get("modelPrediction"), dict) else None
    direction = str(plan.get("direction") or "").strip().upper()
    if not prediction or direction not in {"LONG", "SHORT"}:
        return None, direction
    details = prediction.get(direction.lower())
    return details if isinstance(details, dict) else None, direction


def _finite_model_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _apply_model_strategy(plan: dict[str, Any], settings: dict[str, Any], branch: str) -> dict[str, Any]:
    """Make a validation-gated model part of target admission and levels.

    The model never invents a direction or a free-form price. It can reject a
    classic candidate, rank an admitted candidate, and move the first target
    only along the already-confirmed target ladder while retaining the
    structural stop/trigger boundaries.
    """

    if not isinstance(plan, dict):
        return plan
    details, direction = _model_side_details(plan)
    prediction = plan.get("modelPrediction") if isinstance(plan.get("modelPrediction"), dict) else None
    engine = _strategy_engine(settings)
    if engine != STRATEGY_ENGINE_MODEL:
        return plan
    selected_branch = _model_branch({"modelBranch": branch})
    model_state: dict[str, Any] = {
        "engine": engine,
        "branch": selected_branch,
        "active": bool(prediction and details),
        "accepted": False,
        "direction": direction or None,
        "qualityGateAccepted": bool(prediction.get("qualityGateAccepted")) if isinstance(prediction, dict) else False,
        "explicitBranchUse": bool(prediction.get("explicitBranchUse")) if isinstance(prediction, dict) else False,
    }
    if not prediction or not details:
        plan["modelStrategy"] = {**model_state, "fallback": "PREDICTION_UNAVAILABLE"}
        return plan

    verdict = str(prediction.get("verdict") or "").strip().upper()
    prediction_direction = str(prediction.get("direction") or "").strip().upper()
    probability = _finite_model_number(details.get("planFeasibilityProbability"))
    score = _finite_model_number(details.get("selectionScore"))
    expected_r = _finite_model_number(details.get("expectedR"))
    expected_mfe = _finite_model_number(details.get("expectedMfeR"))
    expected_mae = _finite_model_number(details.get("expectedMaeR"))
    accepted = verdict == "FAVORABLE" and prediction_direction == direction and probability is not None and 0 < probability <= 1
    model_state.update(
        {
            "accepted": accepted,
            "verdict": verdict,
            "predictionDirection": prediction_direction or None,
            "planFeasibilityProbability": round(probability, 4) if probability is not None else None,
            "selectionScore": round(score, 4) if score is not None else None,
            "expectedR": round(expected_r, 4) if expected_r is not None else None,
            "expectedMfeR": round(expected_mfe, 3) if expected_mfe is not None else None,
            "expectedMaeR": round(expected_mae, 3) if expected_mae is not None else None,
        }
    )
    if not accepted:
        model_state["fallback"] = "BELOW_VALIDATION_THRESHOLD" if verdict != "FAVORABLE" else "DIRECTION_MISMATCH"
        if str(plan.get("status") or "").upper() in {"ARMED", "TRIAL", "WATCH"}:
            plan["status"] = "BLOCKED"
        blocked = list(plan.get("blockedReasons") or [])
        if "model_rejected" not in blocked:
            blocked.append("model_rejected")
        plan["blockedReasons"] = blocked
        missing = list(plan.get("missingConditions") or [])
        if "时序模型验证门槛未通过，暂不纳入目标候选。" not in missing:
            missing.append("时序模型验证门槛未通过，暂不纳入目标候选。")
        plan["missingConditions"] = missing
        plan["modelStrategy"] = model_state
        return plan

    # Bounded target adjustment. The classic target remains the lower/upper
    # structural anchor; a confirmed extension is the only allowed outward
    # cap. If the model expects less room, it can move the first target toward
    # the trigger but never below the configured minimum-R gate.
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    trigger = _finite_model_number(entry.get("trigger"))
    stop = _finite_model_number(plan.get("stopLoss"))
    targets = plan.get("takeProfits") if isinstance(plan.get("takeProfits"), list) else []
    first = next((item for item in targets if isinstance(item, dict) and str(item.get("role") or "").upper() == "FIRST_TARGET"), None)
    extension = next((item for item in targets if isinstance(item, dict) and str(item.get("role") or "").upper() == "EXTENSION_TARGET" and item.get("available", True) is not False), None)
    first_price = _finite_model_number(first.get("price")) if first else None
    extension_price = _finite_model_number(extension.get("price")) if extension else None
    risk = abs(trigger - stop) if trigger is not None and stop is not None else None
    minimum_r = _finite_model_number(plan.get("minimumTargetR")) or 0.5
    current_r = abs(first_price - trigger) / risk if first_price is not None and trigger is not None and risk and risk > 0 else None
    extension_r = abs(extension_price - trigger) / risk if extension_price is not None and trigger is not None and risk and risk > 0 else None
    adjustment: dict[str, Any] = {"applied": False, "reason": "结构目标不可计算"}
    if current_r is not None and risk and risk > 0 and expected_mfe is not None and expected_mfe > 0 and first_price is not None and trigger is not None:
        cap_r = max(current_r, extension_r or current_r)
        desired_r = min(cap_r, max(minimum_r, expected_mfe))
        if abs(desired_r - current_r) >= 0.05:
            desired_price = trigger + risk * desired_r if direction == "LONG" else trigger - risk * desired_r
            # Never cross the confirmed extension or reverse the target side.
            if extension_price is not None:
                desired_price = min(desired_price, extension_price) if direction == "LONG" else max(desired_price, extension_price)
            if (direction == "LONG" and desired_price > trigger) or (direction == "SHORT" and desired_price < trigger):
                first["price"] = _round(desired_price)
                first["rMultiple"] = round(abs(desired_price - trigger) / risk, 2)
                first["source"] = "时序模型预期路径（受经典结构目标边界约束）"
                plan["targetOne"] = _round(desired_price)
                plan["riskReward"] = round(abs(desired_price - trigger) / risk, 2)
                adjustment = {
                    "applied": True,
                    "fromR": round(current_r, 3),
                    "toR": round(desired_r, 3),
                    "expectedMfeR": round(expected_mfe, 3),
                    "boundary": "CLASSIC_TARGET_LADDER",
                }
            else:
                adjustment = {"applied": False, "reason": "模型目标建议未通过方向边界"}
        else:
            adjustment = {"applied": False, "reason": "模型建议与经典第一目标接近"}
    model_state["targetAdjustment"] = adjustment
    model_state["stopPolicy"] = "CLASSIC_STRUCTURAL_STOP_UNCHANGED"
    plan["modelStrategy"] = model_state
    plan["strategyEngine"] = STRATEGY_ENGINE_MODEL
    plan["modelBranch"] = selected_branch
    plan["modelAccepted"] = True
    plan["quality"] = max(0, min(100, int(plan.get("quality") or 0) + min(15, int(round((probability or 0) * 15)))))
    reasons = list(plan.get("reasons") or [])
    reasons.append(
        f"时序模型 {selected_branch} 分支已启用，目标候选按时间效率排序并受经典边界约束。"
        if model_state.get("explicitBranchUse") and not model_state.get("qualityGateAccepted")
        else f"时序模型 {selected_branch} 分支通过验证门槛，目标候选按时间效率排序。"
    )
    plan["reasons"] = list(dict.fromkeys(reasons))
    return plan


def _initial_level_basis(
    bars: list[dict[str, float]],
    *,
    direction: str,
    trigger: float,
    atr: float,
    strategy: str,
    platform_allowed: bool,
    structure_bars: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Select a defensible initial invalidation boundary for the chosen route.

    A platform is deliberately a repeated, ATR-bounded group of completed-bar
    tests. It is not an averaged price and it never replaces a stop merely to
    make the risk look smaller.
    """

    is_long = direction == "LONG"
    stop_window = structure_bars if structure_bars else bars[-8:]
    extreme_edge = min(bar["low"] for bar in stop_window) if is_long else max(bar["high"] for bar in stop_window)
    basis: dict[str, Any] = {
        "selectedStrategy": strategy,
        "resolvedStrategy": LEVEL_STRATEGY_EXTREME,
        "executionReady": True,
        "initialStop": {
            "source": "FRESH_TREND_PULLBACK" if structure_bars else "RECENT_EXTREME",
            "label": "当前新鲜趋势回撤/回测的结构极值" if structure_bars else "近期 8 根 15m K 线的结构极值",
            "structureEdge": extreme_edge,
            "usesPlatform": False,
            "zone": None,
        },
        "detector": None,
        "fallbackReason": None,
    }
    if strategy != LEVEL_STRATEGY_PLATFORM:
        return basis
    if not platform_allowed:
        basis["resolvedStrategy"] = "STRUCTURE_EXTREME_FALLBACK"
        basis["fallbackReason"] = "过渡/反转环境尚未完成反转确认，不能用平台替代原有结构。"
        return basis

    platforms = _confirmed_platform_zones(bars, atr)
    stop_zone = _nearest_platform_zone(
        platforms["support"] if is_long else platforms["resistance"],
        trigger,
        atr,
        below_trigger=is_long,
    )
    target_zones = _directional_platform_targets(platforms, direction, trigger, atr)
    basis["detector"] = {
        "timeframe": "15m",
        "lookbackBars": min(PLATFORM_LOOKBACK_BARS, len(bars)),
        "minTouches": PLATFORM_MIN_TOUCHES,
        "toleranceAtrMultiplier": PLATFORM_TOLERANCE_ATR_MULTIPLIER,
        "tolerance": _round_number(platforms["tolerance"]),
        "supportPlatforms": len(platforms["support"]),
        "resistancePlatforms": len(platforms["resistance"]),
    }
    # The private key is consumed before serialization by _apply_platform_targets.
    basis["_targetZones"] = target_zones
    if stop_zone is not None:
        structure_edge = stop_zone["low"] if is_long else stop_zone["high"]
        basis["initialStop"] = {
            "source": "CONFIRMED_PLATFORM",
            "label": "已确认的支撑/阻力平台外沿",
            "structureEdge": structure_edge,
            "usesPlatform": True,
            "zone": _platform_zone_payload(stop_zone),
        }
    else:
        basis["resolvedStrategy"] = "STRUCTURE_EXTREME_FALLBACK"
        basis["fallbackReason"] = "近 24 根已收盘 15m K 线没有位于触发价另一侧、可用于止损的确认平台，保留近期结构极值。"
    return basis


def _apply_platform_targets(
    *,
    direction: str,
    trigger: float,
    risk: float,
    minimum_target_r: float,
    basis: dict[str, Any],
    fallback_first: float | None,
    fallback_second: float | None,
    fallback_first_source: str,
    fallback_second_source: str,
    atr: float | None = None,
    target_buffer_atr_multiplier: float = 0.0,
) -> tuple[float | None, float | None, str, str, dict[str, Any]]:
    """Use the reachable side of confirmed target platforms when space allows."""

    target_zones = basis.pop("_targetZones", [])
    target_basis: dict[str, Any] = {
        "usesPlatform": False,
        "firstZone": None,
        "secondZone": None,
        "fallbackExtension": False,
        "firstRawPrice": None,
        "secondRawPrice": None,
        "executionBuffer": {
            "atrMultiplier": max(0.0, float(target_buffer_atr_multiplier or 0.0)),
            "atr": _round_number(atr),
        },
        "reason": "当前路线使用原有结构目标。",
    }
    if basis.get("selectedStrategy") != LEVEL_STRATEGY_PLATFORM:
        return fallback_first, fallback_second, fallback_first_source, fallback_second_source, target_basis
    if not target_zones:
        target_basis["reason"] = "未找到位于触发价前方的确认平台，保留原有结构目标。"
        return fallback_first, fallback_second, fallback_first_source, fallback_second_source, target_basis

    is_long = direction == "LONG"
    first_zone = None
    first_price = None
    first_raw_price = None
    for zone in target_zones:
        raw_candidate = zone["low"] if is_long else zone["high"]
        candidate = _execution_target_price(
            raw_candidate,
            direction,
            atr,
            trigger,
            target_buffer_atr_multiplier,
        )
        if candidate is None:
            continue
        distance_r = (candidate - trigger) / risk if is_long else (trigger - candidate) / risk
        if distance_r >= minimum_target_r:
            first_zone = zone
            first_price = candidate
            first_raw_price = raw_candidate
            break
    if first_zone is None or first_price is None:
        target_basis["reason"] = f"确认平台的最近可达边界不足 {minimum_target_r:g}R，保留原有结构目标。"
        return fallback_first, fallback_second, fallback_first_source, fallback_second_source, target_basis

    second_zone = None
    second_price = None
    second_raw_price = None
    for zone in target_zones[target_zones.index(first_zone) + 1:]:
        raw_candidate = zone["low"] if is_long else zone["high"]
        candidate = _execution_target_price(
            raw_candidate,
            direction,
            atr,
            first_price,
            target_buffer_atr_multiplier,
        )
        if candidate is None:
            continue
        if candidate > first_price if is_long else candidate < first_price:
            second_zone = zone
            second_price = candidate
            second_raw_price = raw_candidate
            break

    target_basis.update(
        {
            "usesPlatform": True,
            "firstZone": _platform_zone_payload(first_zone),
            "secondZone": _platform_zone_payload(second_zone) if second_zone else None,
            "firstRawPrice": _round_number(first_raw_price),
            "secondRawPrice": _round_number(second_raw_price),
            "reason": "第一目标取下一确认平台的可达侧，并在平台前侧留执行缓冲，避免把目标钉在单根极值。",
        }
    )
    first_source = "确认平台/区域：第一目标取前方阻力/支撑平台的可达侧，并在目标区前侧提前成交。"
    if second_price is not None:
        second_source = "确认平台/区域：扩展目标取下一确认平台的可达侧，并在目标区前侧提前成交。"
        return first_price, second_price, first_source, second_source, target_basis

    target_basis["reason"] = "第一目标使用确认平台；没有更远且方向有效的平台，不机械外推扩展目标。"
    return first_price, None, first_source, "确认平台不足，暂不机械外推扩展目标。", target_basis


def _confirmed_platform_zones(bars: list[dict[str, float]], atr: float) -> dict[str, Any]:
    """Find repeated 15m support/resistance tests inside an ATR-sized band."""

    window = list(bars[-PLATFORM_LOOKBACK_BARS:])
    if not window:
        return {"support": [], "resistance": [], "tolerance": 0.0}
    close = abs(_number(window[-1].get("close")))
    price_tick = 10 ** -_price_decimal_places(close) if close > 0 else 0.00000001
    tolerance = max(abs(atr) * PLATFORM_TOLERANCE_ATR_MULTIPLIER, close * 0.0001, price_tick)
    return {
        "support": _cluster_platform_zones(window, "low", tolerance),
        "resistance": _cluster_platform_zones(window, "high", tolerance),
        "tolerance": tolerance,
    }


def _cluster_platform_zones(
    bars: list[dict[str, float]],
    field: str,
    tolerance: float,
    *,
    min_touches: int = PLATFORM_MIN_TOUCHES,
    min_span_bars: int | None = None,
) -> list[dict[str, Any]]:
    required_touches = max(1, int(min_touches))
    required_span = (
        max(0, int(min_span_bars))
        if min_span_bars is not None
        else max(0, required_touches)
    )
    points = _platform_test_points(bars, field, tolerance)
    candidates: list[dict[str, Any]] = []
    for _index, anchor in points:
        touches = [(index, value) for index, value in points if abs(value - anchor) <= tolerance]
        if len(touches) < required_touches:
            continue
        touch_indexes = [index for index, _value in touches]
        if touch_indexes[-1] - touch_indexes[0] < required_span:
            continue
        touching_bars = [bars[index] for index in touch_indexes]
        common_low = max(float(bar["low"]) for bar in touching_bars)
        common_high = min(float(bar["high"]) for bar in touching_bars)
        if common_low > common_high + tolerance:
            continue
        values = [value for _index, value in touches]
        # The full test band, rather than each test's distance to one anchor,
        # must fit inside the configured ATR tolerance. This prevents a
        # steadily sloping sequence from being misclassified as a platform.
        if max(values) - min(values) > tolerance:
            continue
        candidates.append(
            {
                "low": min(values),
                "high": max(values),
                "touches": len(touches),
                "spanBars": touch_indexes[-1] - touch_indexes[0],
                "lastIndex": touch_indexes[-1],
            }
        )
    candidates.sort(key=lambda zone: (zone["touches"], zone["spanBars"], zone["lastIndex"]), reverse=True)
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        overlaps_existing = any(
            candidate["low"] <= existing["high"] + tolerance
            and candidate["high"] >= existing["low"] - tolerance
            for existing in unique
        )
        if not overlaps_existing:
            unique.append(candidate)
    return sorted(unique, key=lambda zone: (zone["low"], zone["high"]))


def _platform_test_points(
    bars: list[dict[str, float]],
    field: str,
    tolerance: float,
) -> list[tuple[int, float]]:
    """Keep only local tests so a smoothly sloping channel is not a platform."""

    points: list[tuple[int, float]] = []
    local_slack = tolerance * 0.25
    for index in range(1, len(bars) - 1):
        value = _number(bars[index].get(field))
        previous = _number(bars[index - 1].get(field))
        following = _number(bars[index + 1].get(field))
        if value <= 0 or not math.isfinite(value):
            continue
        is_test = (
            value <= previous + local_slack and value <= following + local_slack
            if field == "low"
            else value >= previous - local_slack and value >= following - local_slack
        )
        if is_test:
            points.append((index, value))
    return points


def _nearest_platform_zone(
    zones: list[dict[str, Any]],
    trigger: float,
    atr: float,
    *,
    below_trigger: bool,
) -> dict[str, Any] | None:
    clearance = max(abs(atr) * 0.05, abs(trigger) * 0.00005)
    if below_trigger:
        candidates = [zone for zone in zones if zone["high"] < trigger - clearance]
        return min(candidates, key=lambda zone: (trigger - zone["high"], -zone["touches"], -zone["lastIndex"]), default=None)
    candidates = [zone for zone in zones if zone["low"] > trigger + clearance]
    return min(candidates, key=lambda zone: (zone["low"] - trigger, -zone["touches"], -zone["lastIndex"]), default=None)


def _directional_platform_targets(
    platforms: dict[str, Any],
    direction: str,
    trigger: float,
    atr: float,
) -> list[dict[str, Any]]:
    is_long = direction == "LONG"
    clearance = max(abs(atr) * 0.05, abs(trigger) * 0.00005)
    zones = platforms["resistance"] if is_long else platforms["support"]
    if is_long:
        return sorted((zone for zone in zones if zone["low"] > trigger + clearance), key=lambda zone: zone["low"])
    return sorted((zone for zone in zones if zone["high"] < trigger - clearance), key=lambda zone: zone["high"], reverse=True)


def _platform_zone_payload(zone: dict[str, Any] | None) -> dict[str, Any] | None:
    if zone is None:
        return None
    return {
        "low": _round_number(zone.get("low")),
        "high": _round_number(zone.get("high")),
        "touches": int(zone.get("touches") or 0),
        "spanBars": int(zone.get("spanBars") or 0),
    }


def _target_levels(
    *,
    direction: str,
    trigger: float,
    risk: float,
    mode: str,
    range_fade: bool,
    four_hour: MarketReading,
    one_hour: MarketReading,
    fifteen: MarketReading | None = None,
    target_buffer_atr_multiplier: float = 0.0,
) -> tuple[float | None, float | None, str, str]:
    """Return structure-based targets without forcing every setup into 3R.

    The 4h reading decides the environment, while executable targets use the
    nearer 15m/1h structure.  Range fades still aim at the established 4h
    midpoint first, because that is the first decision boundary of the range.
    Extensions are only returned when a nearer 15m/1h structure exists; no
    distant 4h projection is invented merely to keep a second target populated.

    ``fifteen`` remains optional for callers that used this helper directly
    before the short-target ladder was introduced.  The live and replay
    paths always pass it, so only those paths use the narrowed target basis.
    """

    is_long = direction == "LONG"
    if mode == "RANGE" and range_fade:
        first = four_hour.range_mid
        if fifteen is None:
            # Direct legacy callers did not provide a short-period reading.
            # Keep their old result stable while the actual plan path uses the
            # bounded 15m/1h lookup below.
            extension = four_hour.range_high if is_long else four_hour.range_low
        else:
            extension = _nearest_directional_reading_level(
                (("15m", fifteen), ("1h", one_hour)),
                direction,
                first,
                risk,
                minimum_r=0.25,
                target_buffer_atr_multiplier=target_buffer_atr_multiplier,
            )
            first = _execution_target_price(
                first,
                direction,
                four_hour.atr,
                trigger,
                target_buffer_atr_multiplier,
            )
        return (
            first,
            extension
            if extension is not None and (extension > first if is_long else extension < first)
            else None,
            "区间边缘反向：第一目标优先取区间中线。",
            "区间边缘反向：扩展目标取近侧15m/1h确认结构，仅在中线后仍有跟随时观察。"
            if fifteen is not None
            else "区间边缘反向：扩展目标取区间另一边，仅在中线后仍有跟随时观察。",
        )

    if mode == "RANGE":
        # A confirmed range breakout is not a fade.  Use the nearest bounded
        # 15m/1h structure for both targets.  A full 4h-width projection is
        # deliberately excluded: it is often too far to be a realistic first
        # execution target and can leave the position waiting for a price that
        # the current structure never confirmed.
        if fifteen is None:
            # Compatibility callers may omit 15m, but that must not bring the
            # old full-4h-width projection back into an executable target.
            first = _nearest_directional_reading_level(
                (("1h", one_hour),),
                direction,
                trigger,
                risk,
                minimum_r=1.0,
                target_buffer_atr_multiplier=target_buffer_atr_multiplier,
            )
            extension = None
        else:
            first = _nearest_directional_reading_level(
                (("15m", fifteen), ("1h", one_hour)),
                direction,
                trigger,
                risk,
                minimum_r=1.0,
                target_buffer_atr_multiplier=target_buffer_atr_multiplier,
            )
            extension = (
                _nearest_directional_reading_level(
                    (("1h", one_hour), ("15m", fifteen)),
                    direction,
                    first,
                    risk,
                    minimum_r=0.25,
                    target_buffer_atr_multiplier=target_buffer_atr_multiplier,
                )
                if first is not None
                else None
            )
        return (
            first,
            extension if extension is not None and (extension > first if is_long else extension < first) else None,
            "区间突破：第一目标取最近的15m/1h边界磁力位。" if fifteen is not None else "区间突破：第一目标取最近的1h边界磁力位。",
            "区间突破：扩展目标取更近的1h/15m确认结构，需等待突破后的跟随。"
            if fifteen is not None
            else "区间突破：没有15m确认，暂不机械外推第二目标。",
        )

    if mode == "REBOUND":
        readings = [reading for reading in (fifteen, one_hour) if reading is not None]
        first = next(
            (
                candidate
                for reading in readings
                for candidate in [
                    _directional_reading_level(
                        reading,
                        direction,
                        trigger,
                        risk,
                        1.25,
                        target_buffer_atr_multiplier=target_buffer_atr_multiplier,
                    )
                ]
                if candidate is not None
            ),
            None,
        )
        extension_candidate = (
            _directional_reading_level(
                one_hour,
                direction,
                first,
                risk,
                0.5,
                target_buffer_atr_multiplier=target_buffer_atr_multiplier,
            )
            if first is not None
            else None
        )
        extension = extension_candidate if extension_candidate is not None and (
            extension_candidate > first if is_long else extension_candidate < first
        ) else None
        first_valid = first is not None and (first > trigger if is_long else first < trigger)
        if not first_valid:
            first = None
        if extension is not None and not (extension > first if is_long else extension < first):
            extension = None
        return (
            first,
            extension,
            "反弹：第一目标取最近的 15m/1h 结构磁力位，并在目标区前侧提前成交；没有磁力位则阻断。",
            "反弹：扩展目标取更远的 1h 磁力位，只有新确认和跟随后才启用。" if extension else "反弹：当前没有更远且方向有效的 1h 磁力位，暂不机械外推。",
        )

    if fifteen is not None:
        first = _directional_reading_level(
            fifteen,
            direction,
            trigger,
            risk,
            1.5,
            target_buffer_atr_multiplier=target_buffer_atr_multiplier,
        )
        if first is not None:
            extension = _directional_reading_level(
                one_hour,
                direction,
                first,
                risk,
                0.5,
                target_buffer_atr_multiplier=target_buffer_atr_multiplier,
            )
            if extension is not None and not (extension > first if is_long else extension < first):
                extension = None
            return (
                first,
                extension,
                "趋势：第一目标取15m结构磁力位，不足时回看1h最近可达结构。",
                "趋势延续：第二目标取1h下一结构，需新确认和跟随。" if extension else "趋势：没有更远且方向有效的1h结构，暂不机械外推第二目标。",
            )
        first = _directional_reading_level(
            one_hour,
            direction,
            trigger,
            risk,
            1.5,
            target_buffer_atr_multiplier=target_buffer_atr_multiplier,
        )
        if first is not None:
            return (
                first,
                None,
                "趋势：15m空间不足，第一目标回看1h最近可达结构。",
                "趋势：没有更远且方向有效的1h结构，暂不机械外推第二目标。",
            )

    # 4h remains an environment input, not an executable extension source.
    # If local readings cannot supply a target, leave it unknown and let the
    # plan become BLOCKED. A fixed R number is a risk metric, not a magnetic
    # level and must not be presented as an executable target.
    first = _nearest_directional_reading_level(
        (("1h", one_hour), ("15m", fifteen)) if fifteen is not None else (("1h", one_hour),),
        direction,
        trigger,
        risk,
        minimum_r=1.5,
        target_buffer_atr_multiplier=target_buffer_atr_multiplier,
    )
    return first, None, "趋势：第一目标取已确认的短周期结构并在目标区前侧成交；结构不足则阻断。", "趋势：没有已确认的1h延伸结构，暂不机械外推第二目标。"


def _nearest_directional_reading_level(
    readings: tuple[tuple[str, MarketReading], ...],
    direction: str,
    reference: float,
    risk: float,
    *,
    minimum_r: float,
    target_buffer_atr_multiplier: float = 0.0,
) -> float | None:
    """Choose the nearest usable boundary from bounded timeframe readings."""

    if risk <= 0 or reference <= 0:
        return None
    is_long = direction == "LONG"
    candidates: list[tuple[float, int, str]] = []
    for priority, (timeframe, reading) in enumerate(readings):
        raw_candidate = reading.range_high if is_long else reading.range_low
        candidate = _execution_target_price(
            raw_candidate,
            direction,
            reading.atr,
            reference,
            target_buffer_atr_multiplier,
        )
        if candidate is None:
            continue
        distance = candidate - reference if is_long else reference - candidate
        if candidate <= 0 or distance <= 0 or distance / risk < minimum_r:
            continue
        candidates.append((distance, priority, timeframe))
    if not candidates:
        return None
    selected_distance, _priority, _timeframe = min(candidates)
    return reference + selected_distance if is_long else reference - selected_distance


def _directional_reading_level(
    reading: MarketReading,
    direction: str,
    reference: float,
    risk: float,
    minimum_r: float,
    *,
    target_buffer_atr_multiplier: float = 0.0,
) -> float | None:
    """Return a recent timeframe boundary only when it has usable room."""

    raw_candidate = reading.range_high if direction == "LONG" else reading.range_low
    candidate = _execution_target_price(
        raw_candidate,
        direction,
        reading.atr,
        reference,
        target_buffer_atr_multiplier,
    )
    if candidate is None:
        return None
    distance = (candidate - reference) if direction == "LONG" else (reference - candidate)
    if candidate <= 0 or distance <= 0 or risk <= 0 or distance / risk < minimum_r:
        return None
    return candidate


def _execution_target_price(
    raw_price: float | None,
    direction: str,
    atr: float | None,
    reference: float,
    target_buffer_atr_multiplier: float = 0.0,
) -> float | None:
    """Move a structural target into the reachable side of its zone."""

    if raw_price is None or not math.isfinite(float(raw_price)) or float(raw_price) <= 0:
        return None
    raw = float(raw_price)
    multiplier = max(0.0, float(target_buffer_atr_multiplier or 0.0))
    if multiplier <= 0:
        return raw
    reference_price = max(abs(float(reference or 0.0)), 0.0)
    volatility = abs(float(atr or 0.0))
    buffer = max(volatility * multiplier, reference_price * 0.00005)
    adjusted = raw - buffer if direction == "LONG" else raw + buffer
    return adjusted if adjusted > 0 else None


def _near_term_target(
    *,
    direction: str,
    trigger: float,
    risk: float,
    mode: str,
    bars_15m: list[dict[str, float]],
    bars_5m: list[dict[str, float]] | None,
    five_minute: MarketReading | None,
    main_target: float | None,
    minimum_target_r: float = NEAR_TERM_MIN_R,
    target_buffer_atr_multiplier: float = 0.0,
) -> tuple[float | None, str | None, str | None]:
    """Find a nearer confirmed 5m/15m platform for a partial protection exit.

    The target is available for all three price-action environments. The mode
    determines the main target and management context; this helper still
    requires a directional, recently tested local area before that target.
    """

    normalized_mode = str(mode or "").strip().upper()
    if normalized_mode not in {"TREND", "RANGE", "REBOUND"} or risk <= 0 or trigger <= 0:
        return None, None, None
    candidates: list[tuple[float, str, str, int]] = []

    def add_candidate(
        price: float | None,
        source: str,
        timeframe: str,
        atr: float | None,
        priority: int = 1,
    ) -> None:
        if price is None or price <= 0:
            return
        execution_price = _execution_target_price(
            price,
            direction,
            atr,
            trigger,
            target_buffer_atr_multiplier,
        )
        if execution_price is None:
            return
        distance = (execution_price - trigger) if direction == "LONG" else (trigger - execution_price)
        if distance <= 0 or distance / risk < minimum_target_r:
            return
        if main_target is not None:
            # Do not duplicate the main target after rounding, but do not
            # discard a genuine nearby magnet merely because it is close to
            # the first target.
            clearance = max(abs(risk) * 0.01, abs(trigger) * 0.00002)
            if direction == "LONG" and execution_price >= main_target - clearance:
                return
            if direction == "SHORT" and execution_price <= main_target + clearance:
                return
        candidates.append((execution_price, source, timeframe, priority))

    def collect_local_candidates(
        window: list[dict[str, float]],
        timeframe: str,
        local_atr: float,
    ) -> None:
        """Collect progressively weaker, but still auditable, local magnets.

        A near-term exit is an execution aid.  The main target keeps the hard
        room gate; this helper may use a two-test platform or a confirmed
        local swing so a valid plan does not lose its first practical exit only
        because the last twenty bars did not produce three identical tests.
        """

        if not window:
            return
        is_long = direction == "LONG"
        field = "high" if is_long else "low"
        close = abs(_number(window[-1].get("close")))
        price_tick = 10 ** -_price_decimal_places(close) if close > 0 else 0.00000001
        tolerance = max(abs(local_atr) * PLATFORM_TOLERANCE_ATR_MULTIPLIER, close * 0.0001, price_tick)

        strict_platforms = _confirmed_platform_zones(window, local_atr)
        for zone in _directional_platform_targets(strict_platforms, direction, trigger, local_atr):
            add_candidate(
                zone["low"] if is_long else zone["high"],
                f"{timeframe}确认平台/区域：近端保护目标取可达侧。",
                timeframe,
                local_atr,
                priority=3,
            )

        relaxed_platforms = {
            "support": _cluster_platform_zones(
                window,
                "low",
                tolerance,
                min_touches=NEAR_TERM_PLATFORM_MIN_TOUCHES,
                min_span_bars=NEAR_TERM_PLATFORM_MIN_SPAN_BARS,
            ),
            "resistance": _cluster_platform_zones(
                window,
                "high",
                tolerance,
                min_touches=NEAR_TERM_PLATFORM_MIN_TOUCHES,
                min_span_bars=NEAR_TERM_PLATFORM_MIN_SPAN_BARS,
            ),
            "tolerance": tolerance,
        }
        for zone in _directional_platform_targets(relaxed_platforms, direction, trigger, local_atr):
            add_candidate(
                zone["low"] if is_long else zone["high"],
                f"{timeframe}两次确认平台/区域：近端保护目标取可达侧。",
                timeframe,
                local_atr,
                priority=2,
            )

        # A local swing is a valid magnet even when it has only one confirmed
        # test.  _platform_test_points excludes the still-forming endpoints,
        # so it does not turn the current unfinished bar into a target.
        for _index, value in reversed(_platform_test_points(window, field, tolerance)):
            add_candidate(
                value,
                f"{timeframe}已收盘局部{ '高点' if is_long else '低点' }：作为近端磁力位。",
                timeframe,
                local_atr,
                priority=1,
            )

        # In a clean one-way move there may be no interior pivot at all.  The
        # last short completed segment still supplies an observable local
        # extreme and is the lowest-priority fallback; all hard direction,
        # distance and first-target checks remain in add_candidate().
        recent_segment = window[-3:]
        recent_extreme = (
            max(_number(bar.get("high")) for bar in recent_segment)
            if is_long
            else min(_number(bar.get("low")) for bar in recent_segment)
        )
        add_candidate(
            recent_extreme,
            f"{timeframe}最近三根已收盘K线极值：近端磁力位后备。",
            timeframe,
            local_atr,
            priority=0,
        )

    short_bars_5m = list(bars_5m[-NEAR_TERM_LOOKBACK_BARS:]) if bars_5m else []
    if short_bars_5m:
        micro_atr_values = _atr(short_bars_5m, 14)
        micro_atr = max(
            five_minute.atr if five_minute is not None else 0.0,
            micro_atr_values[-1] if micro_atr_values else 0.0,
            trigger * 0.0004,
        )
        collect_local_candidates(short_bars_5m, "5m", micro_atr)

    short_bars_15m = list(bars_15m[-NEAR_TERM_LOOKBACK_BARS:]) if bars_15m else []
    if not short_bars_15m:
        return (None, None, None)
    fifteen_atr_values = _atr(short_bars_15m, 14)
    fifteen_atr = max(
        short_bars_15m[-1]["close"] * 0.0004,
        fifteen_atr_values[-1] if fifteen_atr_values else 0.0,
    )
    collect_local_candidates(short_bars_15m, "15m", fifteen_atr)

    if not candidates:
        return None, None, None
    selected = min(
        candidates,
        key=lambda item: (abs(item[0] - trigger), -item[3], 0 if item[2] == "5m" else 1),
    )
    return selected[0], selected[1], selected[2]


def _micro_confirmation(
    bars: list[dict[str, float]] | None,
    direction: str,
) -> tuple[bool, str, str, list[str]]:
    """Check a small, completed 5m trigger without changing higher-timeframe bias."""

    if not bars or len(bars) < 3:
        return False, "没有足够完整的 5m K 线，不能用小周期猜测入场。", "等待至少 3 根已收盘 5m K 线形成方向确认。", []
    latest, previous = bars[-1], bars[-2]
    atr_values = _atr(bars, 14)
    atr = max(atr_values[-1] if atr_values else 0.0, latest["close"] * 0.00025)
    span = max(latest["high"] - latest["low"], atr * 0.15)
    close_position = (latest["close"] - latest["low"]) / span
    is_long = direction == "LONG"
    directional_close = close_position >= 0.55 if is_long else close_position <= 0.45
    candle_color = latest["close"] > latest["open"] if is_long else latest["close"] < latest["open"]
    local_break = latest["high"] > previous["high"] if is_long else latest["low"] < previous["low"]
    passed = directional_close and candle_color and local_break
    if passed:
        return True, f"5m 已收盘方向 K 突破前一根局部{ '高点' if is_long else '低点' }，仅作为入场时机确认。", "", ["5m 确认不改变 1h/4h 背景；下一根强反向 K 仍需取消。"]
    return False, "5m 尚未形成收盘靠近极值且突破前一根局部结构的确认。", "等待 5m 方向 K 收盘并突破前一根局部结构；不以单根影线确认。", [f"5m 当前收盘位置约 {close_position:.2f}，相对波动约 {atr:.6g}。"]


def _entry_timing(
    bars: list[dict[str, float]] | None,
    direction: str,
    price: float,
    trigger: float | None,
    *,
    mode: str = "TREND",
    range_low: float | None = None,
    range_high: float | None = None,
    stop_loss: float | None = None,
    reversal_context: dict[str, Any] | None = None,
    range_timeframe: str = "4h",
) -> dict[str, Any]:
    """Return only a mode-specific, completed 5m entry structure.

    Trend entries use continuation pullbacks.  Range entries use an edge
    rejection or failed breakout, never the middle of a range.  Reversal
    entries require an already-confirmed 15m reversal context before the 5m
    first pullback/H2-L2 timing can be considered.
    """

    safe_mode = mode if mode in {"TREND", "RANGE", "REBOUND"} else "TREND"
    labels = {
        "TREND": "等待趋势 5m 入场结构",
        "RANGE": "等待区间边缘 5m 结构",
        "REBOUND": "等待反转确认后的 5m 回调",
    }
    source_pages = {
        "TREND": ["trend:188-194", "trend:246-262", "trend:360-405"],
        "RANGE": ["range:18-24", "range:55-70", "range:267-275", "range:373-395"],
        "REBOUND": ["reversal:37-70", "reversal:71-105", "reversal:157-190", "reversal:379-384"],
    }
    result: dict[str, Any] = {
        "timeframe": "5m",
        "state": "UNAVAILABLE",
        "setup": None,
        "recommendedLimitPrice": None,
        "referencePrice": None,
        "triggerPrice": _round(trigger) if trigger else None,
        "direction": direction,
        "mode": safe_mode,
        "label": labels[safe_mode],
        "reason": "只使用已收盘 5m K 线；单根 K 线或最新价本身不能构成挂单依据。",
        "riskNote": "5m 挂单价只是回测参考，不保证成交；原触发价和主周期计划不变。",
        "sourcePages": source_pages[safe_mode],
        "role": "ENTRY_AND_SHORT_TERM",
        "countsTowardCompleteness": False,
    }
    if not bars or len(bars) < MIN_ENTRY_TIMING_BARS:
        result.update(
            state="UNAVAILABLE",
            reason="至少需要一段已收盘的 5m 结构来识别模式与回测。",
        )
        return result
    if any(bar.get("completed") is False for bar in bars):
        result.update(
            state="UNAVAILABLE",
            reason="入场时机只允许使用已收盘 K 线，未收盘 K 线不能用于确认。",
        )
        return result

    recent = bars[-12:]
    atr_values = _atr(recent, 14)
    atr = max(atr_values[-1] if atr_values else 0.0, recent[-1]["close"] * 0.00025)
    if safe_mode == "RANGE":
        return _range_entry_timing(
            result,
            recent,
            direction,
            price,
            atr,
            range_low,
            range_high,
            stop_loss,
            range_timeframe=range_timeframe,
        )
    if safe_mode == "REBOUND":
        return _rebound_entry_timing(result, recent, direction, price, atr, stop_loss, reversal_context)
    return _trend_entry_timing(result, recent, direction, price, atr, stop_loss)


def _directional_bar(bar: dict[str, float], direction: str, atr: float, *, strong: bool = False) -> bool:
    span = max(bar["high"] - bar["low"], atr * 0.15)
    close_position = (bar["close"] - bar["low"]) / span
    minimum_body = atr * (0.22 if strong else 0.12)
    is_long = direction == "LONG"
    return (
        bar["close"] > bar["open"]
        and close_position >= (0.62 if strong else 0.56)
        and bar["close"] - bar["open"] >= minimum_body
        if is_long
        else bar["close"] < bar["open"]
        and close_position <= (0.38 if strong else 0.44)
        and bar["open"] - bar["close"] >= minimum_body
    )


def _timing_ready(
    result: dict[str, Any],
    *,
    direction: str,
    price: float,
    reference: float,
    atr: float,
    stop_loss: float | None,
    setup: str,
    label: str,
    reason: str,
) -> dict[str, Any]:
    is_long = direction == "LONG"
    result.update(setup=setup, referencePrice=_round(reference))
    if price <= 0 or reference <= 0 or (is_long and reference >= price) or (not is_long and reference <= price):
        result.update(
            state="WAITING",
            label="等待价格回测",
            reason="形态已出现，但当前价尚未位于可执行回测价的正确一侧。",
        )
        return result
    if stop_loss and ((is_long and reference <= stop_loss) or (not is_long and reference >= stop_loss)):
        result.update(
            state="WAITING",
            label="等待可定义风险的回测",
            reason="候选回测价已落入结构止损一侧，不能为了挂单压缩结构风险。",
        )
        return result
    if abs(price - reference) > atr * 1.5:
        result.update(
            state="WAITING",
            label="等待新的回调",
            reason="价格已经远离最近的 5m 结构位，迟到入场不追价。",
        )
        return result
    result.update(
        state="READY",
        recommendedLimitPrice=_round(reference),
        label=label,
        reason=reason,
    )
    return result


def _trend_entry_timing(
    result: dict[str, Any],
    recent: list[dict[str, float]],
    direction: str,
    price: float,
    atr: float,
    stop_loss: float | None,
) -> dict[str, Any]:
    """Trend: completed breakout retest, micro-channel pullback, or H2/L2."""

    is_long = direction == "LONG"
    candidates: list[dict[str, Any]] = []
    for breakout_index in range(max(3, len(recent) - 8), len(recent) - 1):
        before = recent[breakout_index - 3:breakout_index]
        breakout = recent[breakout_index]
        level = max(bar["high"] for bar in before) if is_long else min(bar["low"] for bar in before)
        broke = _directional_bar(breakout, direction, atr) and (breakout["close"] > level + atr * 0.03 if is_long else breakout["close"] < level - atr * 0.03)
        if not broke:
            continue
        for index in range(breakout_index + 1, len(recent)):
            retest = recent[index]
            held = (
                retest["low"] <= level + atr * 0.25 and retest["close"] >= level and retest["close"] <= retest["open"] and retest["low"] <= breakout["close"] - atr * 0.2
                if is_long
                else retest["high"] >= level - atr * 0.25 and retest["close"] <= level and retest["close"] >= retest["open"] and retest["high"] >= breakout["close"] + atr * 0.2
            )
            if held and index >= len(recent) - 3:
                prior_attempts = sum(
                    1
                    for candidate in recent[max(0, breakout_index - 5):breakout_index]
                    if ((candidate["high"] >= level - atr * 0.08 and candidate["close"] <= level) if is_long else (candidate["low"] <= level + atr * 0.08 and candidate["close"] >= level))
                )
                channel = all((left["low"] <= right["low"] and left["close"] <= right["close"]) if is_long else (left["high"] >= right["high"] and left["close"] >= right["close"]) for left, right in zip(before, before[1:]))
                candidates.append({"index": index, "level": level, "setup": "H2_RETEST" if prior_attempts >= 2 else "MICRO_CHANNEL_PULLBACK" if channel else "BREAKOUT_RETEST"})
                break
    if not candidates:
        result.update(state="WAITING", label="等待趋势回测", reason="5m 尚未形成顺势突破后的守位回测、微型通道回调或 H2/L2。")
        return result
    candidate = max(candidates, key=lambda item: item["index"])
    return _timing_ready(
        result,
        direction=direction,
        price=price,
        reference=float(candidate["level"]),
        atr=atr,
        stop_loss=stop_loss,
        setup=candidate["setup"],
        label="趋势 5m 回测挂单参考",
        reason="主周期方向不变；5m 仅以顺势突破后的已收盘回测优化入场。",
    )


def _range_entry_timing(
    result: dict[str, Any],
    recent: list[dict[str, float]],
    direction: str,
    price: float,
    atr: float,
    range_low: float | None,
    range_high: float | None,
    stop_loss: float | None,
    *,
    range_timeframe: str = "4h",
) -> dict[str, Any]:
    """Range: only an edge rejection/failed breakout followed by a retest."""

    is_long = direction == "LONG"
    boundary = float(range_low or 0) if is_long else float(range_high or 0)
    width = abs(float(range_high or 0) - float(range_low or 0))
    if boundary <= 0 or width <= atr:
        result.update(
            state="UNAVAILABLE",
            reason="区间边界或波动范围不足，不能把 5m 小波动当作区间入场。",
        )
        return result
    at_edge = price <= boundary + width * 0.34 if is_long else price >= boundary - width * 0.34
    if not at_edge:
        result.update(
            state="WAITING",
            label="等待区间边缘",
            reason=f"当前价格不在对应的 {range_timeframe} 区间边缘。",
        )
        return result

    candidates: list[dict[str, Any]] = []
    for probe_index in range(2, len(recent) - 2):
        probe = recent[probe_index]
        tested_edge = probe["low"] <= boundary + atr * 0.35 if is_long else probe["high"] >= boundary - atr * 0.35
        if not tested_edge:
            continue
        failed_breakout = probe["low"] < boundary - atr * 0.03 and probe["close"] >= boundary if is_long else probe["high"] > boundary + atr * 0.03 and probe["close"] <= boundary
        signal_index = next(
            (
                index
                for index in range(probe_index + 1, len(recent) - 1)
                if _directional_bar(recent[index], direction, atr, strong=True)
                and (recent[index]["close"] > probe["high"] if is_long else recent[index]["close"] < probe["low"])
            ),
            None,
        )
        if signal_index is None:
            continue
        signal = recent[signal_index]
        for index in range(signal_index + 1, len(recent)):
            retest = recent[index]
            held = (
                retest["low"] <= max(boundary, signal["low"]) + atr * 0.25 and retest["close"] >= boundary and retest["close"] <= retest["open"] and retest["low"] <= signal["close"] - atr * 0.2
                if is_long
                else retest["high"] >= min(boundary, signal["high"]) - atr * 0.25 and retest["close"] <= boundary and retest["close"] >= retest["open"] and retest["high"] >= signal["close"] + atr * 0.2
            )
            if held and index >= len(recent) - 3:
                reference = max(boundary, signal["low"]) if is_long else min(boundary, signal["high"])
                candidates.append({"index": index, "reference": reference, "setup": "RANGE_FAILED_BREAKOUT_RETEST" if failed_breakout else "RANGE_EDGE_REVERSAL_RETEST"})
                break
    if not candidates:
        result.update(state="WAITING", label="等待区间边缘确认", reason="5m 尚未形成区间边缘拒绝/失败突破后的强反向 K 与守位回测。")
        return result
    candidate = max(candidates, key=lambda item: item["index"])
    return _timing_ready(
        result,
        direction=direction,
        price=price,
        reference=float(candidate["reference"]),
        atr=atr,
        stop_loss=stop_loss,
        setup=candidate["setup"],
        label="区间边缘回测挂单参考",
        reason="区间只在边缘交易：失败突破/反转 K 先确认，随后回测守住边缘才给出限价参考。",
    )


def _reversal_context(bars: list[dict[str, float]], direction: str) -> dict[str, Any]:
    """Require 15m counter-impulse, extreme retest, and follow-through first."""

    result: dict[str, Any] = {"passed": False, "referencePrice": None, "reason": "15m 反转架构尚未确认。", "missing": "等待反向冲击、极点回测未恢复及后续跟随。"}
    if len(bars) < 14:
        result.update(reason="15m K 线不足，不能验证反转架构。", missing="等待足够的已收盘 15m K 线。")
        return result
    is_long = direction == "LONG"
    recent = bars[-20:]
    atr_values = _atr(recent, 14)
    atr = max(atr_values[-1] if atr_values else 0.0, recent[-1]["close"] * 0.0004)
    candidates: list[dict[str, Any]] = []
    for impulse_index in range(3, len(recent) - 4):
        first, second = recent[impulse_index], recent[impulse_index + 1]
        before = recent[impulse_index - 3:impulse_index]
        level = max(bar["high"] for bar in before) if is_long else min(bar["low"] for bar in before)
        impulse_break = _directional_bar(first, direction, atr, strong=True) and _directional_bar(second, direction, atr, strong=True) and (second["close"] > level + atr * 0.03 if is_long else second["close"] < level - atr * 0.03)
        if not impulse_break:
            continue
        extreme = min(bar["low"] for bar in recent[:impulse_index]) if is_long else max(bar["high"] for bar in recent[:impulse_index])
        if (second["close"] - extreme if is_long else extreme - second["close"]) < atr:
            continue
        for retest_index in range(impulse_index + 2, len(recent) - 1):
            retest = recent[retest_index]
            holds = (
                retest["close"] <= retest["open"] and retest["low"] > extreme + atr * 0.1 and retest["low"] <= second["close"] - atr * 0.2
                if is_long
                else retest["close"] >= retest["open"] and retest["high"] < extreme - atr * 0.1 and retest["high"] >= second["close"] + atr * 0.2
            )
            follow = recent[retest_index + 1]
            followed = _directional_bar(follow, direction, atr, strong=True) and (follow["close"] > retest["high"] if is_long else follow["close"] < retest["low"])
            if holds and followed:
                candidates.append({"index": retest_index + 1, "level": level})
                break
    if not candidates:
        return result
    candidate = max(candidates, key=lambda item: item["index"])
    result.update(
        passed=True,
        referencePrice=_round(float(candidate["level"])),
        reason="15m 已出现两根反向冲击、极点回测未恢复原趋势，并有反向跟随。",
        missing="",
    )
    return result


def _rebound_entry_timing(
    result: dict[str, Any],
    recent: list[dict[str, float]],
    direction: str,
    price: float,
    atr: float,
    stop_loss: float | None,
    reversal_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reversal: after the 15m architecture, wait for a 5m pullback/H2-L2."""

    if not reversal_context or not reversal_context.get("passed"):
        result.update(state="WAITING", label="等待 15m 反转架构", reason=str((reversal_context or {}).get("reason") or "15m 反转架构尚未确认。"))
        return result
    is_long = direction == "LONG"
    candidates: list[dict[str, Any]] = []
    for impulse_index in range(3, len(recent) - 3):
        first, second = recent[impulse_index], recent[impulse_index + 1]
        before = recent[impulse_index - 3:impulse_index]
        level = max(bar["high"] for bar in before) if is_long else min(bar["low"] for bar in before)
        impulse = _directional_bar(first, direction, atr, strong=True) and _directional_bar(second, direction, atr, strong=True) and (second["close"] > level + atr * 0.03 if is_long else second["close"] < level - atr * 0.03)
        if not impulse:
            continue
        for retest_index in range(impulse_index + 2, len(recent) - 1):
            retest = recent[retest_index]
            holds = (
                retest["low"] <= level + atr * 0.25 and retest["close"] >= level and retest["close"] <= retest["open"] and retest["low"] <= second["close"] - atr * 0.2
                if is_long
                else retest["high"] >= level - atr * 0.25 and retest["close"] <= level and retest["close"] >= retest["open"] and retest["high"] >= second["close"] + atr * 0.2
            )
            follow = recent[retest_index + 1]
            confirmed = _directional_bar(follow, direction, atr, strong=True) and (follow["close"] > retest["high"] if is_long else follow["close"] < retest["low"])
            if holds and confirmed and retest_index >= len(recent) - 4:
                candidates.append({"index": retest_index + 1, "level": level, "setup": "REVERSAL_H2_RETEST"})
                break
    if not candidates:
        result.update(state="WAITING", label="等待反转后的 5m 回调", reason="15m 反转架构已出现，但 5m 尚未形成反向冲击后的第一回调/H2-L2 与再次确认。")
        return result
    candidate = max(candidates, key=lambda item: item["index"])
    return _timing_ready(
        result,
        direction=direction,
        price=price,
        reference=float(candidate["level"]),
        atr=atr,
        stop_loss=stop_loss,
        setup=candidate["setup"],
        label="反转确认后回测挂单参考",
        reason="5m 只细化已确认反转的入场；它不能替代 15m 的反向冲击、极点回测与跟随。",
    )


def _timeframe_payload(reading: MarketReading, extra: list[str] | None = None) -> dict[str, Any]:
    return {
        "state": reading.state,
        "bias": reading.bias,
        "close": _round(reading.close),
        "ema20": _round(reading.ema20),
        "ema50": _round(reading.ema50),
        "atr": _round(reading.atr),
        "rangeLow": _round(reading.range_low),
        "rangeHigh": _round(reading.range_high),
        "rangeMid": _round(reading.range_mid),
        "evidence": [*reading.evidence, *(extra or [])],
    }


def _empty_recommendation(market_type: str = "FUTURES") -> dict[str, Any]:
    return _empty_market_recommendation(market_type)


def _empty_model_recommendation(
    market_type: str = "FUTURES",
    *,
    branch: str = MODEL_BRANCH_BEST,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return an explicit MODEL WAIT when no symbol produced a plan."""

    result = _empty_market_recommendation(market_type)
    result.update(
        {
            "direction": "WAIT",
            "status": "WAIT",
            "strategyEngine": STRATEGY_ENGINE_MODEL,
            "modelTask": "DIRECT_PLAN",
            "modelBranch": str(branch or MODEL_BRANCH_BEST).upper(),
            "modelRunId": str(run_id or "").strip() or None,
            "modelGenerated": False,
            "modelReason": "MODEL_DATA_UNAVAILABLE",
            "reason": "MODEL_DATA_UNAVAILABLE",
            "missingConditions": ["当前扫描没有返回可用的完整多周期模型输入。"],
            "entry": {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"},
        }
    )
    result["modelPrediction"] = {"direct": True, "verdict": "WAIT", "productionUse": False}
    return result


def _empty_market_recommendation(market_type: str = "FUTURES") -> dict[str, Any]:
    safe_market_type = _market_type(market_type)
    return {
        "symbol": None,
        "marketType": safe_market_type,
        "direction": "NONE",
        "status": "BLOCKED",
        "marketMode": "REBOUND",
        "entry": {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "CONDITIONAL_BREAKOUT"},
        "stopLoss": None,
        "entryTiming": {
            "timeframe": "5m",
            "state": "UNAVAILABLE",
            "setup": None,
            "recommendedLimitPrice": None,
            "referencePrice": None,
            "triggerPrice": None,
            "direction": "NONE",
            "label": "等待 5m 入场结构",
            "reason": "没有足够完整的 5m K 线。",
            "riskNote": "5m 挂单价只是回测参考，不保证成交。",
            "sourcePages": ["trend:188-194", "trend:246-262", "trend:360-405"],
            "role": "ENTRY_AND_SHORT_TERM",
            "countsTowardCompleteness": False,
        },
        "takeProfits": [],
        "riskReward": 0,
        "confidence": 0,
        "conditionCompleteness": 0,
        "conditionMet": 0,
        "conditionTotal": 0,
        "conditionChecks": [],
        "quality": 0,
        "timeframes": {},
        "reasons": [],
        "missingConditions": [f"没有足够完整的 15m、1h、4h 主周期 { _market_label(safe_market_type) }K线，暂不生成计划。"],
        "cancellationConditions": [],
        "sourcePages": SOURCE_PAGES,
        "automatedOrder": False,
    }


def _sort_key(plan: dict[str, Any]) -> tuple[float, float, float, float, float, float, float, float, float, float]:
    model = plan.get("modelStrategy") if isinstance(plan.get("modelStrategy"), dict) else {}
    prediction = plan.get("modelPrediction") if isinstance(plan.get("modelPrediction"), dict) else {}
    direction = str(plan.get("direction") or "").lower()
    details = prediction.get(direction) if isinstance(prediction.get(direction), dict) else {}
    model_score = (
        _finite_model_number(model.get("selectionScore"))
        if _finite_model_number(model.get("selectionScore")) is not None
        else _finite_model_number(details.get("selectionScore"))
    )
    if model_score is None:
        # Direct MODEL WAIT predictions store their comparison score at the
        # prediction root because they have no LONG/SHORT side detail.
        model_score = _finite_model_number(prediction.get("selectionScore"))
    model_score = model_score if model_score is not None else _finite_model_number(details.get("timeEfficiencyScore")) or 0.0
    time_efficiency = _finite_model_number(model.get("timeEfficiency"))
    if time_efficiency is None:
        time_efficiency = _finite_model_number(details.get("timeEfficiencyScore"))
    if time_efficiency is None:
        time_efficiency = _finite_model_number(prediction.get("timeEfficiencyScore"))
    time_efficiency = time_efficiency if time_efficiency is not None else 0.0
    expected_r = _finite_model_number(model.get("expectedR"))
    if expected_r is None:
        expected_r = _finite_model_number(details.get("expectedR"))
    if expected_r is None:
        expected_r = _finite_model_number(prediction.get("expectedR"))
    expected_r = expected_r if expected_r is not None else 0.0
    # Keep the model's calibrated score as the primary ranking signal, but use
    # the executable plan geometry as an explicit tie-breaker.  Direct-plan
    # checkpoints expose this as the first target's actual R; older/blocked
    # snapshots simply contribute zero here.
    plan_risk_reward = _finite_model_number(plan.get("riskReward")) or 0.0
    model_active = 1.0 if model.get("active") else 0.0
    admission = 2.0 if str(model.get("admission") or "").upper() == "ARMED" else 1.0 if str(model.get("admission") or "").upper() == "TRIAL" else 0.0
    return (
        model_active,
        admission,
        model_score,
        time_efficiency,
        expected_r,
        plan_risk_reward,
        1.0 if plan.get("status") == "ARMED" else 0.0,
        float(plan.get("conditionCompleteness", plan.get("confidence") or 0) or 0),
        float(plan.get("quality") or 0),
        float(plan.get("quoteVolume") or 0),
    )


def _model_inference_sort_key(plan: dict[str, Any]) -> tuple[float, float, float, float, float, float, float, float]:
    """Rank a completed model scan by inference, not execution admission."""

    ranked = _sort_key(plan)
    return (
        ranked[2],
        ranked[3],
        ranked[4],
        ranked[5],
        ranked[7],
        ranked[8],
        ranked[9],
        1.0 if str(plan.get("symbol") or "") else 0.0,
    )


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def _atr(bars: list[dict[str, float]], period: int) -> list[float]:
    if not bars:
        return []
    ranges = []
    for index, bar in enumerate(bars):
        previous_close = bars[index - 1]["close"] if index else bar["close"]
        ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous_close), abs(bar["low"] - previous_close)))
    return _ema(ranges, period)


def _overlap_ratio(bars: list[dict[str, float]]) -> float:
    if len(bars) < 2:
        return 0.0
    overlaps = 0
    for previous, current in zip(bars, bars[1:]):
        if min(previous["high"], current["high"]) >= max(previous["low"], current["low"]):
            overlaps += 1
    return overlaps / (len(bars) - 1)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _round_number(value: float | None) -> float | None:
    # Some valid plans (notably a rebound without a confirmed continuation)
    # deliberately do not have an extension target.  Keep the optional value
    # optional all the way through the response instead of passing None to
    # math.isfinite().
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 4)


def _format_price(value: float) -> str:
    rounded = _round(value)
    if rounded is None:
        return "--"
    return f"{rounded:.{_price_decimal_places(rounded)}f}".rstrip("0").rstrip(".")


def _force_stop_outside_zone(
    stop: float,
    zone_low: float,
    zone_high: float,
    is_long: bool,
    atr: float,
    *,
    buffer_multiplier: float = 0.28,
    zone_buffer_multiplier: float = 0.5,
) -> float:
    """Keep the structural invalidation beyond the complete trigger structure."""

    boundary = zone_low if is_long else zone_high
    # Preserve at least one visible price tick at the precision used for this
    # price, while keeping the structural volatility buffer intact.
    try:
        configured_multiplier = float(buffer_multiplier)
    except (TypeError, ValueError):
        configured_multiplier = 0.28
    if not math.isfinite(configured_multiplier) or configured_multiplier < 0:
        configured_multiplier = 0.28
    zone_buffer = _trigger_zone_stop_buffer(
        zone_low,
        zone_high,
        atr,
        atr_multiplier=zone_buffer_multiplier,
    )
    buffer = max(
        abs(atr) * configured_multiplier,
        zone_buffer,
        abs(boundary) * 0.0004,
        10 ** -_price_decimal_places(boundary),
    )
    return min(stop, boundary - buffer) if is_long else max(stop, boundary + buffer)


def _trigger_zone_stop_buffer(
    zone_low: float,
    zone_high: float,
    atr: float,
    *,
    atr_multiplier: float = 0.5,
) -> float:
    """Return the defensive-side clearance required for a complete trigger zone.

    A narrow zone gets a volatility floor. A wide completed signal/retest
    structure also gets a modest proportional allowance, so a short wick
    beyond its edge alone does not count as structural invalidation.
    """

    try:
        configured_multiplier = float(atr_multiplier)
    except (TypeError, ValueError):
        configured_multiplier = 0.5
    if not math.isfinite(configured_multiplier) or configured_multiplier < 0:
        configured_multiplier = 0.5
    try:
        span = abs(float(zone_high) - float(zone_low))
    except (TypeError, ValueError):
        span = 0.0
    if not math.isfinite(span):
        span = 0.0
    return max(abs(atr) * configured_multiplier, span * TRIGGER_ZONE_STOP_BUFFER_SPAN_FRACTION)


def _bound_trigger_zone_for_risk_reward(
    zone_low: float,
    zone_high: float,
    *,
    trigger: float,
    stop: float,
    first_target: float | None,
    is_long: bool,
) -> tuple[float, float, dict[str, Any]]:
    """Compress a signal zone until its bounds retain comparable first-target R.

    The stop and targets belong to the trigger price.  A completed signal bar
    can be much wider than its defensible risk distance, so exposing the full
    bar as an entry zone made the two edges describe very different trades.
    Preserve the trigger and the zone's original asymmetry, but scale its
    width down to a bound derived from the actual stop and first target.
    """

    try:
        low = min(float(zone_low), float(zone_high), float(trigger))
        high = max(float(zone_low), float(zone_high), float(trigger))
        trigger_price = float(trigger)
        stop_price = float(stop)
        target_price = float(first_target)
    except (TypeError, ValueError):
        return zone_low, zone_high, {"applied": False, "reason": "触发区或止损/第一目标价格无效"}
    if not all(math.isfinite(value) for value in (low, high, trigger_price, stop_price, target_price)):
        return low, high, {"applied": False, "reason": "触发区或止损/第一目标价格无效"}

    risk = trigger_price - stop_price if is_long else stop_price - trigger_price
    reward = target_price - trigger_price if is_long else trigger_price - target_price
    if risk <= 0 or reward <= 0:
        return low, high, {"applied": False, "reason": "止损或第一目标方向无效"}
    first_r = reward / risk
    raw_span = high - low
    max_span_from_r = risk * MAX_TRIGGER_ZONE_R_MULTIPLE_SPREAD / (
        first_r + 1.0 + MAX_TRIGGER_ZONE_R_MULTIPLE_SPREAD
    )
    max_span = max(0.0, min(risk * MAX_TRIGGER_ZONE_RISK_FRACTION, max_span_from_r))
    applied = raw_span > max_span and raw_span > 0
    if applied:
        scale = max_span / raw_span
        low = trigger_price - (trigger_price - low) * scale
        high = trigger_price + (high - trigger_price) * scale

    def r_multiple_at(entry_price: float) -> float | None:
        loss = entry_price - stop_price if is_long else stop_price - entry_price
        gain = target_price - entry_price if is_long else entry_price - target_price
        return gain / loss if loss > 0 else None

    r_low = r_multiple_at(low)
    r_trigger = r_multiple_at(trigger_price)
    r_high = r_multiple_at(high)
    r_values = [value for value in (r_low, r_trigger, r_high) if value is not None and math.isfinite(value)]
    return low, high, {
        "applied": applied,
        "rawSpan": _round_number(raw_span),
        "maxSpan": _round_number(max_span),
        "span": _round_number(high - low),
        "firstTargetRAtZoneLow": _round_number(r_low),
        "firstTargetRAtTrigger": _round_number(r_trigger),
        "firstTargetRAtZoneHigh": _round_number(r_high),
        "firstTargetRSpread": _round_number(max(r_values) - min(r_values)) if r_values else None,
        "maxFirstTargetRSpread": MAX_TRIGGER_ZONE_R_MULTIPLE_SPREAD,
        "policy": "触发区上下界相对同一止损和第一目标的风险回报差异受限。",
    }


def _stop_is_outside_zone(stop: float | None, trigger: float | None, zone_low: float | None, zone_high: float | None, is_long: bool) -> bool:
    if stop is None or stop <= 0:
        return False
    if zone_low is not None and zone_high is not None and zone_low > 0 and zone_high > 0:
        boundary = min(zone_low, zone_high) if is_long else max(zone_low, zone_high)
    elif trigger is not None and trigger > 0:
        boundary = trigger
    else:
        return True
    return stop < boundary if is_long else stop > boundary


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return round(number, _price_decimal_places(number))


def _price_decimal_places(value: float) -> int:
    """Use four useful digits for sub-unit prices and up to four decimals otherwise."""

    absolute = abs(float(value))
    if absolute >= 1:
        return 4
    return max(0, 4 - math.floor(math.log10(absolute)) - 1)
