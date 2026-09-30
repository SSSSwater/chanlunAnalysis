from __future__ import annotations

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any

try:
    from . import database as db
    from .binance_account_worker import (
        get_binance_account_snapshot,
        get_cached_binance_account_snapshot,
        request_binance_account_refresh,
        update_cached_binance_futures_position_protection,
    )
    from .binance_client import BinanceApiError, get_klines as get_spot_klines
    from .binance_futures_client import (
        apply_futures_plan_protection,
        apply_futures_plan_take_profits,
        cancel_futures_direction_orders,
        close_futures_position_market,
        get_futures_klines,
        normalize_futures_stop_loss_price,
        update_futures_position_stop_loss,
    )
    from .binance_futures_strategy import (
        _completed_bars,
        analyze_futures_position_history,
        _direction_context_is_compatible,
        _apply_model_strategy,
        _apply_platform_targets,
        _force_stop_outside_zone,
        _initial_level_basis,
        _near_term_target,
        _plan_is_actionable,
        _plan_is_trial_eligible,
        _read_market,
        _atr,
        _stop_is_outside_zone,
        _four_hour_veto_is_clear,
        _strategy_mode,
        _strategy_engine,
        _model_strategy_settings,
        _model_branch,
        _model_run_id,
        _target_levels,
        _timeframe_payload,
        MIN_SHORT_TERM_READING_BARS,
        STRATEGY_MODE_SHORT_TERM,
        TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    from .binance_ml import (
        predict_binance_futures_frames,
        has_binance_ml_model,
        MACRO_INPUT_INTERVALS as MODEL_ANALYSIS_INTERVALS,
        MODEL_REFERENCE_INTERVAL,
        WINDOWS as MODEL_INPUT_WINDOWS,
    )
except ImportError:
    import database as db
    from binance_account_worker import (
        get_binance_account_snapshot,
        get_cached_binance_account_snapshot,
        request_binance_account_refresh,
        update_cached_binance_futures_position_protection,
    )
    from binance_client import BinanceApiError, get_klines as get_spot_klines
    from binance_futures_client import (
        apply_futures_plan_protection,
        apply_futures_plan_take_profits,
        cancel_futures_direction_orders,
        close_futures_position_market,
        get_futures_klines,
        normalize_futures_stop_loss_price,
        update_futures_position_stop_loss,
    )
    from binance_futures_strategy import (
        _completed_bars,
        analyze_futures_position_history,
        _direction_context_is_compatible,
        _apply_model_strategy,
        _strategy_engine,
        _model_strategy_settings,
        _model_branch,
        _model_run_id,
        _apply_platform_targets,
        _force_stop_outside_zone,
        _initial_level_basis,
        _near_term_target,
        _plan_is_actionable,
        _plan_is_trial_eligible,
        _read_market,
        _atr,
        _stop_is_outside_zone,
        _four_hour_veto_is_clear,
        _strategy_mode,
        _target_levels,
        _timeframe_payload,
        MIN_SHORT_TERM_READING_BARS,
        STRATEGY_MODE_SHORT_TERM,
        TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    from binance_ml import (
        predict_binance_futures_frames,
        has_binance_ml_model,
        MACRO_INPUT_INTERVALS as MODEL_ANALYSIS_INTERVALS,
        MODEL_REFERENCE_INTERVAL,
        WINDOWS as MODEL_INPUT_WINDOWS,
    )


# Binance USD-M includes valid perpetual symbols whose base asset contains
# Chinese characters (for example ``币安人生USDT``). Keep the same Unicode
# word-character contract used by the futures REST/client and snapshot
# layers, while still rejecting separators and punctuation.
POSITION_SYMBOL_PATTERN = re.compile(r"^[^\W_]{5,20}$", re.UNICODE)
POSITION_MODES = {"SPOT", "FUTURES"}
POSITION_SIDES = {"LONG", "SHORT"}
POSITION_NETWORKS = {"mainnet", "testnet"}
KLINE_LIMIT = 180
TRAILING_LOOKBACK = 20
TRAILING_ATR_MULTIPLIER = 2.5
STRUCTURE_STOP_ATR_MULTIPLIER = 0.28
BREAKEVEN_BUFFER_ATR_MULTIPLIER = 0.05
POSITION_RISK_STOP_FRACTION = 0.10
MAX_FUTURES_LEVERAGE = 20
AUTO_PROTECTION_RETRY_COOLDOWN_SECONDS = 15
LIVE_PRICE_CACHE_MAX_AGE_SECONDS = 15
PENDING_ENTRY = "PENDING_ENTRY"
EXECUTING = "EXECUTING"
STOPPED = "STOPPED"
MISSING_LIVE_POSITION_CONFIRMATIONS = 2
STOP_MANAGEMENT_STAGE_ORDER = {
    "INITIAL": 0,
    "BREAKEVEN": 1,
    "STRUCTURE_TRAILING": 2,
    "ATR_TRAILING": 3,
}

logger = logging.getLogger(__name__)
_refresh_lock = RLock()
_portfolio_snapshot_lock = RLock()
_portfolio_snapshots: dict[int, dict] = {}
_auto_protection_lock = RLock()
_auto_protection_state: dict[tuple[Any, ...], dict[str, Any]] = {}


def _cache_portfolio_snapshot(user_id: int, items: list[dict], *, stale: bool = False, error: str | None = None) -> dict:
    snapshot = {
        "items": deepcopy(items),
        "updatedAt": int(time.time() * 1000),
        "stale": bool(stale),
        "error": str(error or "") or None,
    }
    with _portfolio_snapshot_lock:
        _portfolio_snapshots[int(user_id)] = snapshot
        return deepcopy(snapshot)


def _merge_cached_position(user_id: int, item: dict) -> None:
    with _portfolio_snapshot_lock:
        previous = deepcopy(_portfolio_snapshots.get(int(user_id)) or {"items": []})
        old_items = previous.get("items", [])
        items = []
        replaced = False
        for candidate in old_items:
            if candidate.get("id") == item.get("id"):
                items.append(deepcopy(item))
                replaced = True
            else:
                items.append(candidate)
        if not replaced:
            items.append(deepcopy(item))
        _portfolio_snapshots[int(user_id)] = {
            "items": items,
            "updatedAt": int(time.time() * 1000),
            "stale": False,
            "error": None,
        }


def get_cached_binance_simulated_portfolio(user_id: int) -> dict | None:
    with _portfolio_snapshot_lock:
        snapshot = _portfolio_snapshots.get(int(user_id))
        return deepcopy(snapshot) if snapshot is not None else None


def clear_cached_binance_simulated_portfolio(user_id: int) -> None:
    with _portfolio_snapshot_lock:
        _portfolio_snapshots.pop(int(user_id), None)


def refresh_binance_simulated_positions_once() -> None:
    """Refresh all active holding-monitor plans once without a frontend request."""

    refreshed_by_user: dict[int, dict[int, dict]] = {}
    errors_by_user: dict[int, dict[int, str]] = {}
    positions_to_refresh = list(db.list_binance_simulated_positions_for_refresh())
    credentials_by_user = _load_refresh_credentials() if positions_to_refresh else {}
    if positions_to_refresh:
        with ThreadPoolExecutor(
            max_workers=min(8, len(positions_to_refresh)),
            thread_name_prefix="binance-plan-refresh",
        ) as executor:
            futures = {
                executor.submit(_refresh_position, user_id, position, credentials_by_user): (user_id, position)
                for user_id, position in positions_to_refresh
            }
            for future in as_completed(futures):
                user_id, position = futures[future]
                try:
                    result = future.result()
                    refreshed_by_user.setdefault(user_id, {})[int(position["id"])] = result
                except Exception as exc:
                    errors_by_user.setdefault(user_id, {})[int(position["id"])] = str(exc) or "行情暂时不可用"
                    logger.warning(
                        "Binance simulated position refresh failed for position %s (%s)",
                        position.get("id"),
                        position.get("symbol"),
                        exc_info=True,
                    )
    for user_id in set(refreshed_by_user) | set(errors_by_user):
        cached = get_cached_binance_simulated_portfolio(user_id) or {"items": []}
        cached_by_id = {int(item.get("id")): item for item in cached.get("items", []) if item.get("id") is not None}
        items = []
        for position in db.list_binance_simulated_positions(user_id):
            position_id = int(position["id"])
            if position.get("executionStatus") == "STOPPED":
                items.append(_stopped_position_result(position))
            elif position_id in refreshed_by_user.get(user_id, {}):
                items.append(refreshed_by_user[user_id][position_id])
            elif position_id in errors_by_user.get(user_id, {}):
                items.append(
                    _merge_position_refresh_failure(
                        position,
                        cached_by_id.get(position_id),
                        errors_by_user[user_id][position_id],
                    )
                )
            elif position_id in cached_by_id:
                items.append(cached_by_id[position_id])
        _cache_portfolio_snapshot(
            user_id,
            items,
            stale=bool(errors_by_user.get(user_id)),
            error=next(iter(errors_by_user.get(user_id, {}).values()), None),
        )


def _load_refresh_credentials() -> dict[int, dict[str, Any]]:
    """Load only the credentials needed by the backend worker into memory."""

    try:
        return {
            int(item["userId"]): item
            for item in db.list_user_binance_credentials_for_refresh()
            if isinstance(item, dict) and item.get("userId") is not None
        }
    except Exception:
        logger.warning("Binance automatic protection credential load failed", exc_info=True)
        return {}


def get_binance_simulated_portfolio(user_id: int) -> dict:
    cached = get_cached_binance_simulated_portfolio(user_id)
    if cached is not None:
        return cached
    items = []
    for position in db.list_binance_simulated_positions(user_id):
        if position.get("executionStatus") == "STOPPED":
            items.append(_stopped_position_result(position))
            continue
        try:
            items.append(_refresh_position(user_id, position))
        except Exception as exc:
            items.append(_persisted_position_fallback(position, str(exc) or "行情暂时不可用"))
    return _cache_portfolio_snapshot(user_id, items)


def preview_live_futures_position_monitor(user_id: int, payload: object) -> dict[str, Any]:
    """Return a non-persistent monitoring plan for one real futures position.

    This endpoint deliberately relies on the authenticated account snapshot
    already maintained by the worker.  It is a planning read only: no order
    is placed and no existing protection order is changed until the caller
    explicitly saves the returned plan as an execution monitor.
    """

    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "").strip().upper()
    if not POSITION_SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("交易对格式无效")
    if side not in POSITION_SIDES:
        raise ValueError("持仓方向必须选择做多或做空")

    account_snapshot = get_cached_binance_account_snapshot(user_id)
    if not isinstance(account_snapshot, dict):
        raise ValueError("合约账户快照暂不可用，请等待后台刷新后重试")
    probe = {
        "network": "mainnet",
        "marketMode": "FUTURES",
        "symbol": symbol,
        "side": side,
    }
    actual = _cached_real_futures_position(user_id, probe, account_snapshot)
    # A WebSocket reconnect can temporarily mark a previously valid account
    # snapshot stale even though it still contains the real position shown in
    # the account drawer.  Planning from that last known holding is safe: the
    # confirmation path still refuses to submit protection orders until the
    # normal account refresh obtains a reliable snapshot again.
    if actual is None:
        account = account_snapshot.get("account") if isinstance(account_snapshot.get("account"), dict) else {}
        futures = account.get("futures") if isinstance(account.get("futures"), dict) else {}
        actual_positions = futures.get("positions") if isinstance(futures.get("positions"), list) else []
        actual = next(
            (
                item
                for item in actual_positions
                if isinstance(item, dict)
                and str(item.get("symbol") or "").upper() == symbol
                and str(item.get("side") or "").upper() == side
                and _as_optional_number(item.get("quantity"))
            ),
            None,
        )
    if actual is None:
        raise ValueError("未找到对应的真实合约持仓，请等待账户快照同步")

    quantity = _as_optional_number(actual.get("quantity"))
    cost_price = _as_optional_number(actual.get("entryPrice"))
    leverage = _as_optional_number(actual.get("leverage")) or 1.0
    if quantity is None or quantity <= 0 or cost_price is None or cost_price <= 0:
        raise ValueError("真实持仓的数量或开仓均价无效，暂时不能创建监控")

    settings = db.get_user_binance_strategy_settings(user_id)
    frames = _live_position_monitor_frames("mainnet", symbol, settings)
    quote = {
        "symbol": symbol,
        "lastPrice": _as_optional_number(actual.get("lastPrice")) or _as_optional_number(actual.get("markPrice")),
        "markPrice": _as_optional_number(actual.get("markPrice")),
    }
    plan = analyze_futures_position_history(
        quote,
        frames,
        direction=side,
        strategy_settings=settings,
    )
    if not isinstance(plan, dict):
        raise ValueError("历史 K 线不足，暂时无法生成持仓监控计划")
    # Position monitoring uses the same selected model branch as target
    # discovery. MODEL owns the complete plan; classic level adjustment is
    # intentionally bypassed.
    if _strategy_engine(settings) == "MODEL":
        if has_binance_ml_model("mainnet", _model_branch(settings), allow_rejected=True, run_id=_model_run_id(settings)):
            direct_plan = predict_binance_futures_frames(
                {interval: list(frames[interval]) for interval in MODEL_ANALYSIS_INTERVALS if isinstance(frames.get(interval), list)},
                network="mainnet",
                plan=plan,
                branch=_model_branch(settings),
                allow_rejected_research_model=True,
                model_run_id=_model_run_id(settings),
            )
            if isinstance(direct_plan, dict):
                plan = direct_plan
            else:
                plan["strategyEngine"] = "MODEL"
                plan["modelGenerated"] = False
                plan["modelReason"] = "MODEL_INVALID"
                plan["reason"] = "MODEL_INVALID"
                plan["direction"] = "WAIT"
                plan["status"] = "WAIT"
                plan["entry"] = {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"}
                plan["stopLoss"] = None
                plan["takeProfits"] = []
        else:
            plan["modelStrategy"] = {"engine": "MODEL", "branch": _model_branch(settings), "active": False, "fallback": "MODEL_ARTIFACT_UNAVAILABLE"}
            plan["strategyEngine"] = "MODEL"
            plan["modelGenerated"] = False
            plan["modelReason"] = "MODEL_UNAVAILABLE"
            plan["reason"] = "MODEL_UNAVAILABLE"
            plan["direction"] = "WAIT"
            plan["status"] = "WAIT"
            plan["entry"] = {"trigger": None, "zoneLow": None, "zoneHigh": None, "type": "MODEL_DIRECT"}
            plan["stopLoss"] = None
            plan["takeProfits"] = []

    monitor_position = {
        "network": "mainnet",
        "marketMode": "FUTURES",
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "costPrice": cost_price,
        "leverage": leverage,
        "planSnapshot": {"strategySettings": settings},
    }
    risk_snapshot = dict(account_snapshot)
    risk_snapshot["stale"] = False
    risk_context = _position_risk_context(
        user_id,
        monitor_position,
        risk_snapshot,
        actual,
        strategy_settings=settings,
    )
    risk_stop = _position_risk_stop(risk_context, side == "LONG")
    plan = _build_live_position_monitor_plan(
        analysis_plan=plan,
        position=monitor_position,
        actual=actual,
        frames=frames,
        quote=quote,
        account_snapshot=risk_snapshot,
        risk_context=risk_context,
    )
    plan["monitoringPlan"] = True
    plan["analysisScope"] = "LIVE_POSITION"
    plan["monitoringPlanReason"] = "按当前真实持仓方向、成本、杠杆和历史 K 线生成；仅用于持仓监控，不代表新的入场建议。"
    plan["livePositionRiskStopPreview"] = _round_price(risk_stop) if risk_stop is not None else None

    account = account_snapshot.get("account") if isinstance(account_snapshot.get("account"), dict) else {}
    futures = account.get("futures") if isinstance(account.get("futures"), dict) else {}
    notional = abs(_as_optional_number(actual.get("notional")) or quantity * cost_price)
    initial_margin = abs(_as_optional_number(actual.get("initialMargin")) or notional / leverage)
    margin_total = _futures_account_total(account_snapshot)
    return {
        "plan": plan,
        "position": {
            "symbol": symbol,
            "side": side,
            "positionSide": str(actual.get("positionSide") or "BOTH").upper(),
            "quantity": quantity,
            "costPrice": cost_price,
            "leverage": leverage,
            "lastPrice": _as_optional_number(actual.get("lastPrice")),
            "markPrice": _as_optional_number(actual.get("markPrice")),
            "notional": notional,
            "initialMargin": initial_margin,
            "marginBalance": margin_total,
            "marginRatio": initial_margin / margin_total if margin_total and margin_total > 0 else None,
            "availableBalance": _as_optional_number(futures.get("availableBalance")),
            "positionRiskStop": _round_price(risk_stop) if risk_stop is not None else None,
            "positionRiskBudget": risk_context.get("riskBudget") if risk_context else None,
            "accountSnapshotStale": bool(account_snapshot.get("stale")),
        },
    }


def _build_live_position_monitor_plan(
    *,
    analysis_plan: dict[str, Any],
    position: dict[str, Any],
    actual: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    quote: dict[str, Any],
    account_snapshot: dict[str, Any],
    risk_context: dict[str, float] | None,
) -> dict[str, Any]:
    """Re-anchor a monitoring plan to a holding's real entry, never a new trigger.

    The directional analyzer is still useful for the market regime, condition
    evidence and frozen strategy route.  Its trigger zone, however, describes
    a *new* entry and must not leak into an already-filled holding.  Seed the
    management calculation with the live cost instead so the structure stop,
    targets, R progress and moving-stop activation all share the real fill as
    their reference.
    """

    side = str(position.get("side") or "LONG").upper()
    is_long = side == "LONG"
    cost_price = _positive_number(position.get("costPrice"), "真实持仓成本")
    quantity = _positive_number(position.get("quantity"), "真实持仓数量")
    position_snapshot = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    settings = _strategy_settings_for_plan(
        {"strategySettings": analysis_plan.get("strategySettings") or position_snapshot.get("strategySettings")}
    )
    anchored_entry = {
        "type": "LIVE_POSITION",
        "label": "真实持仓成本",
        "costPrice": _round_price(cost_price),
    }
    opened_at = _timestamp_ms(actual.get("updateTime"))
    seed_plan = {
        "symbol": str(position.get("symbol") or "").upper(),
        "marketType": "FUTURES",
        "direction": side,
        "status": str(analysis_plan.get("status") or "WATCH"),
        "strategyMode": analysis_plan.get("strategyMode"),
        "strategyModeLabel": analysis_plan.get("strategyModeLabel"),
        "timeframeRoles": deepcopy(analysis_plan.get("timeframeRoles") or {}),
        "strategySettings": settings,
        "entry": anchored_entry,
        "monitoringPlan": True,
        "analysisScope": "LIVE_POSITION",
        "automatedOrder": False,
    }
    if opened_at is not None:
        seed_plan["openedAt"] = opened_at

    # Do not seed an entry-plan stop or target here.  Those prices are tied to
    # the old trigger; _build_plan will derive a clean structural ladder from
    # the actual cost and the same K-line discipline used by live monitoring.
    management_position = {
        **position,
        "quantity": quantity,
        "costPrice": cost_price,
        "planSnapshot": seed_plan,
        "openedAt": opened_at,
    }
    model_mode = _strategy_engine(settings) == "MODEL"
    reference_frame = MODEL_REFERENCE_INTERVAL if model_mode else "15m"
    current_price = (
        _as_optional_number(actual.get("markPrice"))
        or _as_optional_number(actual.get("lastPrice"))
        or _as_optional_number(quote.get("markPrice"))
        or _as_optional_number(quote.get("lastPrice"))
        or _as_optional_number((frames.get(reference_frame) or [{}])[-1].get("close"))
    )
    if current_price is None:
        raise ValueError("真实持仓监控缺少当前标记价")
    dynamic = _build_plan(
        management_position,
        frames,
        current_price,
        False,
        account_snapshot=account_snapshot,
        actual=actual,
    )
    initial_stop = _as_optional_number(dynamic.get("initialStop"))
    targets = deepcopy((dynamic.get("executionPlan") or {}).get("takeProfits") or [])
    if initial_stop is not None and not _target_prices_by_role(targets)[1]:
        risk = abs(cost_price - initial_stop)
        minimum_r = max(float(settings["nearTermMinimumTargetR"]), 0.5)
        fallback_target = cost_price + risk * minimum_r if is_long else cost_price - risk * minimum_r
        targets.append(
            {
                "role": "FIRST_TARGET",
                "label": "第一目标",
                "price": _round_price(fallback_target),
                "rMultiple": round(minimum_r, 2),
                "source": "真实持仓成本与结构风险回退目标",
                "timeframe": "持仓成本/R",
                "cumulativeRatio": settings["secondTakeProfitRatio"],
                "semantics": "当前未确认更近的结构目标时，按实际成本和已定义结构风险保留最低可执行目标。",
            }
        )
    _, first_target, _, _, _, _ = _plan_protection_targets({"takeProfits": targets, "strategySettings": settings})
    if initial_stop is None or first_target is None:
        raise ValueError("当前结构无法为真实持仓生成有效止损和第一止盈")

    plan = {
        key: deepcopy(value)
        for key, value in analysis_plan.items()
        if key not in {"entry", "entryTiming", "stopLoss", "takeProfits", "strategySettings"}
    }
    plan.update(
        {
            "symbol": seed_plan["symbol"],
            "marketType": "FUTURES",
            "direction": side,
            "entry": anchored_entry,
            "stopLoss": _round_price(initial_stop),
            "takeProfits": targets,
            "strategySettings": settings,
            "openedAt": opened_at,
            "initialStop": dynamic.get("initialStop"),
            "activeStop": dynamic.get("activeStop"),
            "activeStopSource": dynamic.get("activeStopSource"),
            "movingStop": dynamic.get("movingStop"),
            "movingStopActive": bool(dynamic.get("movingStopActive")),
            "movingStopActivationR": dynamic.get("movingStopActivationR"),
            "movingStopActivationPrice": dynamic.get("movingStopActivationPrice"),
            "movingStopActivationAt": dynamic.get("movingStopActivationAt"),
            "favorableExtreme": dynamic.get("favorableExtreme"),
            "rMultiple": dynamic.get("rMultiple"),
            "favorableRMultiple": dynamic.get("favorableRMultiple"),
            "positionRiskStop": dynamic.get("positionRiskStop"),
            "positionRiskStopLoss": dynamic.get("positionRiskStopLoss"),
            "positionRiskBudget": dynamic.get("positionRiskBudget"),
            "status": dynamic.get("status") or plan.get("status") or "WATCH",
            "statusLabel": dynamic.get("statusLabel"),
            "timeframes": dynamic.get("timeframes") or deepcopy(analysis_plan.get("timeframes") or {}),
        }
    )
    plan.update(_position_risk_snapshot(risk_context, is_long))
    dynamic_reasons = dynamic.get("reasons") if isinstance(dynamic.get("reasons"), list) else []
    analysis_reasons = analysis_plan.get("reasons") if isinstance(analysis_plan.get("reasons"), list) else []
    plan["reasons"] = list(dict.fromkeys([*dynamic_reasons, *analysis_reasons]))
    dynamic_missing = dynamic.get("missingConditions") if isinstance(dynamic.get("missingConditions"), list) else []
    analysis_missing = analysis_plan.get("missingConditions") if isinstance(analysis_plan.get("missingConditions"), list) else []
    plan["missingConditions"] = list(dict.fromkeys([*dynamic_missing, *analysis_missing]))
    return plan


def _live_position_monitor_frames(
    network: str,
    symbol: str,
    strategy_settings: object,
) -> dict[str, list[dict[str, float]]]:
    """Load only the K-line frames required to preview an existing position."""

    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover - direct module execution compatibility
        import binance_snapshot_worker as snapshot_worker

    short_term_mode = _strategy_mode(strategy_settings) == STRATEGY_MODE_SHORT_TERM
    model_mode = _strategy_engine(strategy_settings) == "MODEL"
    required = list(MODEL_ANALYSIS_INTERVALS) if model_mode else ["4h", "1h", "15m"] + (["5m"] if short_term_mode else [])
    frames: dict[str, list[dict[str, float]]] = {}
    missing: list[str] = []
    for interval in required:
        cached = snapshot_worker.get_cached_kline_snapshot(network, "FUTURES", symbol, interval)
        bars = _completed_bars(cached.get("items") if isinstance(cached, dict) else [])
        minimum = int(MODEL_INPUT_WINDOWS.get(interval, 80)) if model_mode else MIN_SHORT_TERM_READING_BARS if interval == "5m" else 80
        if len(bars) >= minimum:
            frames[interval] = bars
        else:
            missing.append(interval)

    if missing:
        with ThreadPoolExecutor(max_workers=len(missing), thread_name_prefix="binance-live-monitor-preview") as executor:
            requests = {
                executor.submit(get_futures_klines, network, symbol, interval, KLINE_LIMIT, with_meta=True): interval
                for interval in missing
            }
            for future in as_completed(requests):
                interval = requests[future]
                result = future.result()
                if not isinstance(result, dict):
                    raise ValueError(f"{interval} K 线响应格式无效")
                bars = _completed_bars(result.get("items") or [])
                minimum = int(MODEL_INPUT_WINDOWS.get(interval, 80)) if model_mode else MIN_SHORT_TERM_READING_BARS if interval == "5m" else 80
                if len(bars) < minimum:
                    raise ValueError(f"{interval} 历史 K 线不足")
                frames[interval] = bars
                snapshot_worker.cache_kline_snapshot(network, "FUTURES", symbol, interval, result)
    return frames


def save_binance_simulated_position(user_id: int, payload: object, position_id: int | None = None) -> dict:
    position = _normalize_position(payload, require_plan=position_id is None)
    if position_id is None and position.get("plan"):
        position["plan"] = _capture_execution_strategy_settings(user_id, position["plan"])
        # Freeze expected target PnL from the creation-time plan quantity and
        # cost before any live-position synchronization can reduce quantity.
        _, target_snapshot = _freeze_target_pnl_snapshot(
            user_id,
            position,
            position.get("plan"),
        )
        if target_snapshot:
            position["plan"] = {
                **position["plan"],
                "targetPnlSnapshot": target_snapshot,
            }
        position["plan"] = {
            **position["plan"],
            "initialQuantity": position["quantity"],
            "initialCostPrice": position["costPrice"],
        }
    if position_id is not None:
        existing = db.get_binance_simulated_position(user_id, int(position_id))
        if not existing:
            raise ValueError("持仓计划监控不存在")
        if existing.get("executionStatus") == STOPPED:
            raise ValueError("已止损的持仓计划监控不能修改，请删除后重新创建")
        if existing.get("planSnapshot"):
            immutable_fields = ("network", "marketMode", "symbol", "side")
            if any(position[field] != existing[field] for field in immutable_fields):
                raise ValueError("持仓计划监控的网络、市场、交易对和方向不能修改")
            old_snapshot = existing.get("planSnapshot")
            old_target_snapshot = old_snapshot.get("targetPnlSnapshot") if isinstance(old_snapshot, dict) else None
            if isinstance(old_target_snapshot, dict) and isinstance(position.get("plan"), dict):
                # Expected target PnL is an execution-creation invariant; an
                # edit must not silently replace it with a quantity-dependent
                # recalculation.
                position["plan"] = {
                    **position["plan"],
                    "targetPnlSnapshot": old_target_snapshot,
                }
    if position_id is None:
        saved = db.insert_binance_simulated_position(user_id, position)
    else:
        saved = db.update_binance_simulated_position(user_id, int(position_id), position)
    _notify_snapshot_change()
    try:
        # Plan creation is the only synchronous K-line bootstrap. The periodic
        # refresh path below reads this process-local cache and never falls
        # back to an exchange request.
        _bootstrap_position_kline_cache(saved)
        result = _refresh_position(user_id, saved, apply_real_protection=True)
        _merge_cached_position(user_id, result)
        return result
    except Exception as exc:
        result = _persisted_position_fallback(saved, str(exc) or "行情暂时不可用")
        _merge_cached_position(user_id, result)
        return result


def create_pending_binance_entry_monitor(user_id: int, payload: object) -> dict:
    """Persist a pending monitor before a real limit entry is submitted.

    A pending monitor is deliberately created without an exchange request. It
    gives the entry-order endpoint a durable safety anchor while the order is
    still waiting for a fill, and keeps the monitor visible immediately.
    """

    position = _normalize_position(payload, require_plan=True)
    if position.get("executionStatus") != PENDING_ENTRY:
        position["executionStatus"] = PENDING_ENTRY
    if position.get("plan"):
        position["plan"] = _capture_execution_strategy_settings(user_id, position["plan"])
    saved = db.insert_binance_simulated_position(user_id, position)
    pending_plan = _waiting_monitor_plan(
        saved,
        execution_status=PENDING_ENTRY,
        reason="已建立计划监控，等待真实限价入场单成交。",
    )
    result = _result_from_plan(
        saved,
        pending_plan,
        execution_status=PENDING_ENTRY,
        include_live_metrics=False,
    )
    _merge_cached_position(user_id, result)
    _notify_snapshot_change()
    return result


def discard_pending_binance_entry_monitor(user_id: int, position_id: int) -> bool:
    """Remove only a pending monitor created for a rejected entry order.

    This intentionally does not call the exchange order-cleanup routine: a
    rejected entry must not cancel unrelated orders for the same symbol and
    direction.
    """

    existing = db.get_binance_simulated_position(user_id, int(position_id))
    if not existing or existing.get("executionStatus") != PENDING_ENTRY:
        return False
    deleted = db.delete_binance_simulated_position(user_id, int(position_id))
    if not deleted:
        return False
    symbol = str(existing.get("symbol") or "").upper()
    with _auto_protection_lock:
        for key in list(_auto_protection_state):
            if key and key[0] == int(user_id) and len(key) > 1 and str(key[1]).upper() == symbol:
                _auto_protection_state.pop(key, None)
    with _portfolio_snapshot_lock:
        snapshot = _portfolio_snapshots.get(int(user_id))
        if snapshot is not None:
            snapshot["items"] = [
                item for item in snapshot.get("items", [])
                if item.get("id") != int(position_id)
            ]
            snapshot["updatedAt"] = int(time.time() * 1000)
    _notify_snapshot_change()
    return True


def remove_binance_simulated_position(user_id: int, position_id: int) -> dict[str, Any]:
    """Delete one monitor after clearing its matching real futures orders."""

    existing = db.get_binance_simulated_position(user_id, int(position_id))
    if not existing:
        return {"deleted": False, "orderCleanup": {"status": "SKIPPED", "cancelledCount": 0}}

    order_cleanup: dict[str, Any] = {"status": "SKIPPED", "cancelledCount": 0}
    if existing.get("marketMode") == "FUTURES":
        credentials = _load_user_credentials(user_id, None)
        if credentials:
            plan_network = str(existing.get("network") or "").strip().lower()
            credential_network = str(credentials.get("network") or "").strip().lower()
            if plan_network and credential_network and plan_network != credential_network:
                raise ValueError("当前 Binance API 网络与持仓计划监控不一致，无法安全取消交易所同向委托")
            order_cleanup = {
                "status": "CANCELLED",
                **cancel_futures_direction_orders(
                    credentials["network"],
                    credentials["apiKey"],
                    credentials["apiSecret"],
                    symbol=str(existing.get("symbol") or ""),
                    direction=str(existing.get("side") or ""),
                ),
            }
            request_binance_account_refresh(user_id)
        else:
            order_cleanup = {
                "status": "SKIPPED_NO_CREDENTIALS",
                "cancelledCount": 0,
                "reason": "未配置当前 Binance API，未执行交易所委托清理",
            }

    deleted = db.delete_binance_simulated_position(user_id, int(position_id))
    if deleted:
        symbol = str(existing.get("symbol") or "").upper()
        with _auto_protection_lock:
            for key in list(_auto_protection_state):
                if key and key[0] == int(user_id) and len(key) > 1 and str(key[1]).upper() == symbol:
                    _auto_protection_state.pop(key, None)
        with _portfolio_snapshot_lock:
            snapshot = _portfolio_snapshots.get(int(user_id))
            if snapshot is not None:
                snapshot["items"] = [item for item in snapshot.get("items", []) if item.get("id") != int(position_id)]
                snapshot["updatedAt"] = int(time.time() * 1000)
        _notify_snapshot_change()
    return {"deleted": deleted, "orderCleanup": order_cleanup}


def restore_binance_simulated_position(user_id: int, position_id: int) -> dict[str, Any]:
    """Resume a stopped holding monitor after confirming the live position exists."""

    existing = db.get_binance_simulated_position(user_id, int(position_id))
    if not existing or existing.get("executionStatus") != STOPPED:
        raise ValueError("只有已止损的持仓计划监控可以恢复")
    account_snapshot = get_cached_binance_account_snapshot(user_id)
    if _futures_account_snapshot_unavailable(account_snapshot):
        raise ValueError("合约账户快照暂不可用，无法确认真实持仓，暂不能恢复监控")
    if _cached_real_futures_position(user_id, existing, account_snapshot) is None:
        raise ValueError("未找到同一合约和方向的真实持仓，不能恢复持仓计划监控")

    restored = db.restore_binance_simulated_position(user_id, int(position_id))
    if not restored:
        raise ValueError("只有已止损的持仓计划监控可以恢复")
    restored_symbol = str(restored.get("symbol") or "").upper()
    with _auto_protection_lock:
        for key in list(_auto_protection_state):
            if key and key[0] == int(user_id) and len(key) > 1 and str(key[1]).upper() == restored_symbol:
                _auto_protection_state.pop(key, None)
    try:
        result = _refresh_position(user_id, restored)
    except Exception as exc:
        result = _persisted_position_fallback(restored, str(exc) or "恢复后等待行情更新")
    _merge_cached_position(user_id, result)
    _notify_snapshot_change()
    return result


def _normalize_position(payload: object, *, require_plan: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求格式无效")
    network = str(payload.get("network") or "mainnet").strip().lower()
    if network not in POSITION_NETWORKS:
        raise ValueError("网络必须选择主网或测试网")
    market_mode = str(payload.get("marketMode") or payload.get("market_mode") or "FUTURES").strip().upper()
    if market_mode not in POSITION_MODES:
        raise ValueError("市场类型必须选择现货或合约")
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not POSITION_SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("交易对格式无效")
    side = str(payload.get("side") or "LONG").strip().upper()
    if side not in POSITION_SIDES:
        raise ValueError("持仓方向必须选择做多或做空")
    if market_mode == "SPOT" and side != "LONG":
        raise ValueError("现货模拟持仓只支持做多")
    quantity = _positive_number(payload.get("quantity"), "持仓数量")
    cost_price = _positive_number(payload.get("costPrice", payload.get("cost_price")), "持仓成本")
    leverage = _positive_number(payload.get("leverage", 1), "杠杆")
    if leverage < 1 or leverage > MAX_FUTURES_LEVERAGE:
        raise ValueError(f"杠杆必须在 1 到 {MAX_FUTURES_LEVERAGE} 倍之间")
    if market_mode == "SPOT":
        leverage = 1
    source_plan = _normalize_plan_snapshot(payload.get("plan"))
    if require_plan and source_plan is None:
        raise ValueError("必须从分析计划创建持仓计划监控")
    if require_plan:
        if source_plan.get("symbol") != symbol or source_plan.get("direction") != side:
            raise ValueError("执行计划的交易对和方向必须与分析计划一致")
        if source_plan.get("marketType") not in (None, market_mode):
            raise ValueError("执行计划的市场类型必须与持仓一致")
        if payload.get("existingPositionMonitor") is True:
            # An already-open account position is not a new strategy entry.
            # Only protection levels matter; condition completeness and plan
            # execution eligibility must not block adding its monitor.
            stop_loss = _as_optional_number(source_plan.get("stopLoss"))
            targets = source_plan.get("takeProfits") if isinstance(source_plan.get("takeProfits"), list) else []
            first_target = next(
                (
                    _as_optional_number(target.get("price"))
                    for target in targets
                    if isinstance(target, dict)
                    and str(target.get("role") or "FIRST_TARGET").upper() == "FIRST_TARGET"
                    and _as_optional_number(target.get("price")) is not None
                ),
                None,
            )
            if stop_loss is None or first_target is None:
                raise ValueError("真实持仓监控必须包含有效止损和第一止盈")
            if (side == "LONG" and (stop_loss >= cost_price or first_target <= cost_price)) or (
                side == "SHORT" and (stop_loss <= cost_price or first_target >= cost_price)
            ):
                raise ValueError("真实持仓监控的止损和第一止盈必须位于开仓价两侧")
        else:
            is_actionable = _plan_is_actionable(source_plan)
            is_monitoring_plan = _plan_is_monitoring_candidate(source_plan)
            if not is_actionable and not is_monitoring_plan:
                raise ValueError("只能执行条件完整、明确允许试错或指定标的监控分析计划")
            if _plan_is_trial_eligible(source_plan) and payload.get("allowTrial") is not True:
                raise ValueError("试错执行只能用于明确允许的 9/10 计划")
            if not is_actionable and payload.get("allowMonitoringPlan") is not True:
                raise ValueError("指定标的监控计划需要明确确认未满足寻找目标条件")
    note = str(payload.get("note") or "").strip()
    if len(note) > 500:
        raise ValueError("备注不能超过 500 个字符")
    has_existing_position = payload.get("hasExistingPosition")
    execution_status = (
        EXECUTING
        if has_existing_position is True
        else PENDING_ENTRY
        if has_existing_position is False or payload.get("submitRealLimitOrder") is True
        else EXECUTING
    )
    return {
        "network": network,
        "marketMode": market_mode,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "costPrice": cost_price,
        "leverage": leverage,
        "plan": source_plan,
        "executionStatus": execution_status,
        "note": note,
    }


def _target_pnl_snapshot(
    plan: dict[str, Any] | None,
    *,
    entry_price: float | None,
    quantity: float | None,
    side: str,
) -> dict[str, Any]:
    """Freeze each target's incremental quantity and expected PnL.

    Target ratios in execution plans are cumulative.  The displayed/realized
    amount for a later target is therefore only the newly exited slice, not
    the whole original position.  This snapshot is intentionally calculated
    once from the creation-time entry and quantity and is never derived from
    the live (possibly reduced) position again.
    """

    entry = _as_optional_number(entry_price)
    base_quantity = _as_optional_number(quantity)
    if entry is None or entry <= 0 or base_quantity is None or base_quantity <= 0:
        return {}
    source = plan if isinstance(plan, dict) else {}
    targets = source.get("takeProfits") if isinstance(source.get("takeProfits"), list) else []
    if not targets and isinstance(source.get("executionPlan"), dict):
        execution = source["executionPlan"]
        targets = execution.get("takeProfits") if isinstance(execution.get("takeProfits"), list) else []
    if not targets:
        return {}

    protective, first, extension, protective_ratio, first_ratio, second_ratio = _plan_protection_targets(source)
    prices = {
        "PROTECTIVE_TARGET": protective,
        "FIRST_TARGET": first,
        "EXTENSION_TARGET": extension,
    }
    ratios = {
        "PROTECTIVE_TARGET": protective_ratio,
        "FIRST_TARGET": first_ratio,
        "EXTENSION_TARGET": second_ratio,
    }
    # Normalize older index-only snapshots to role names first.
    normalized_targets: list[dict[str, Any]] = []
    target_count = len(targets)
    for index, raw_item in enumerate(targets):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        role = str(item.get("role") or "").strip().upper()
        if not role:
            if target_count >= 3 and index < 3:
                role = ("PROTECTIVE_TARGET", "FIRST_TARGET", "EXTENSION_TARGET")[index]
            elif target_count == 2:
                role = ("FIRST_TARGET", "EXTENSION_TARGET")[index]
            elif target_count == 1:
                role = "FIRST_TARGET"
        if role:
            item["role"] = role
            normalized_targets.append(item)

    # Preserve explicit cumulative ratios when present, while retaining the
    # normalized two-target/no-extension semantics from _plan_protection_targets.
    extension_is_present = extension is not None
    for item in normalized_targets:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().upper()
        if role not in prices:
            continue
        explicit_ratio = _as_optional_number(item.get("cumulativeRatio"))
        # When there is no extension target, _plan_protection_targets promotes
        # the remaining fixed target to the effective second boundary (the
        # quantity actually sent to Binance). Do not overwrite that promoted
        # ratio with an old first-leg diagnostic value.
        if explicit_ratio is not None and explicit_ratio > 0 and (
            role != "FIRST_TARGET" or extension_is_present
        ):
            ratios[role] = explicit_ratio
        explicit_price = _as_optional_number(item.get("price"))
        if explicit_price is not None and explicit_price > 0:
            prices[role] = explicit_price

    result: dict[str, Any] = {
        "entryPrice": _round_price(entry),
        "quantity": base_quantity,
        "side": "SHORT" if str(side or "").upper() == "SHORT" else "LONG",
        "levels": {},
    }
    previous_cumulative = 0.0
    is_long = result["side"] == "LONG"
    for role in ("PROTECTIVE_TARGET", "FIRST_TARGET", "EXTENSION_TARGET"):
        price = _as_optional_number(prices.get(role))
        cumulative = _as_optional_number(ratios.get(role))
        if price is None or price <= 0 or cumulative is None or cumulative <= 0:
            continue
        cumulative = max(previous_cumulative, float(cumulative))
        incremental_ratio = max(0.0, cumulative - previous_cumulative)
        level_quantity = base_quantity * incremental_ratio / 100.0
        pnl = ((price - entry) if is_long else (entry - price)) * level_quantity
        result["levels"][role] = {
            "price": _round_price(price),
            "cumulativeRatio": round(cumulative, 6),
            "incrementalRatio": round(incremental_ratio, 6),
            "quantity": level_quantity,
            "pnl": _round_money(pnl),
        }
        previous_cumulative = cumulative
    return result if result["levels"] else {}


def _freeze_target_pnl_snapshot(
    user_id: int,
    position: dict[str, Any],
    plan: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the immutable target-PnL snapshot and persist it on first use."""

    source = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    existing = source.get("targetPnlSnapshot")
    if isinstance(existing, dict) and isinstance(existing.get("levels"), dict) and existing.get("levels"):
        return position, existing
    live_entry = _as_optional_number(actual.get("entryPrice")) if isinstance(actual, dict) else None
    live_quantity = _as_optional_number(actual.get("quantity")) if isinstance(actual, dict) else None
    entry = (
        _as_optional_number(source.get("initialCostPrice"))
        or live_entry
        or _as_optional_number(position.get("costPrice"))
        or _as_optional_number(source.get("positionRiskEntryPrice"))
    )
    quantity = (
        _as_optional_number(source.get("initialQuantity"))
        or live_quantity
        or _as_optional_number(position.get("quantity"))
        or _as_optional_number(source.get("positionRiskQuantity"))
    )
    snapshot = _target_pnl_snapshot(
        plan or source,
        entry_price=entry,
        quantity=quantity,
        side=str(position.get("side") or (plan or {}).get("direction") or "LONG"),
    )
    if not snapshot:
        return position, {}
    if position.get("id") is not None:
        saved = db.merge_binance_simulated_position_plan_snapshot(
            user_id,
            int(position["id"]),
            {"targetPnlSnapshot": snapshot},
        )
        if saved:
            position = saved
    else:
        position = {**position, "planSnapshot": {**source, "targetPnlSnapshot": snapshot}}
    return position, snapshot


def _normalize_plan_snapshot(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("分析计划格式无效")
    snapshot: dict[str, Any] = {}
    for key in (
        "symbol",
        "direction",
        "status",
        "opportunityType",
        "marketType",
        "marketMode",
        "strategyEngine",
        "executionEligible",
        "modelTask",
        "modelVersion",
        "modelRunId",
        "modelBranch",
        "modelGenerated",
        "modelReason",
        "trainingObjective",
        "strategyMode",
        "strategyModeLabel",
        "timeframeRoles",
        "entry",
        "entryTiming",
        "stopLoss",
        "takeProfits",
        "riskReward",
        "confidence",
        "conditionCompleteness",
        "conditionMet",
        "conditionTotal",
        "conditionChecks",
        "trialEligible",
        "trialMissingCondition",
        "protectiveTakeProfitRatio",
        "firstTakeProfitRatio",
        "secondTakeProfitRatio",
        "strategySettings",
        "quality",
        "reasons",
        "missingConditions",
        "cancellationConditions",
        "timeframes",
        "sourcePages",
        "sourcePolicy",
        "profile",
        "analysisScope",
        "monitoringPlan",
        "monitoringPlanReason",
        "openedAt",
        "positionRiskStop",
        "positionRiskBudget",
        "positionRiskAccountTotal",
        "positionRiskFraction",
        "positionRiskEntryPrice",
        "positionRiskQuantity",
        "positionRiskLeverage",
        "positionRiskCapturedAt",
        "initialQuantity",
        "initialCostPrice",
        "targetPnlSnapshot",
        "automatedOrder",
    ):
        if key in value:
            snapshot[key] = value[key]
    if snapshot.get("marketType") not in (None, "SPOT", "FUTURES"):
        raise ValueError("分析计划的市场类型无效")
    if snapshot.get("automatedOrder") is True:
        raise ValueError("分析计划不能启用自动下单")
    _normalize_snapshot_prices(snapshot)
    entry = snapshot.get("entry") if isinstance(snapshot.get("entry"), dict) else {}
    source_stop = _as_optional_number(snapshot.get("stopLoss"))
    source_trigger = _as_optional_number(entry.get("trigger"))
    source_zone_low = _as_optional_number(entry.get("zoneLow"))
    source_zone_high = _as_optional_number(entry.get("zoneHigh"))
    if source_stop is not None and not _stop_is_outside_zone(
        source_stop,
        source_trigger,
        source_zone_low,
        source_zone_high,
        str(snapshot.get("direction") or "LONG").upper() != "SHORT",
    ):
        raise ValueError("分析计划的结构止损必须位于触发区外")
    return snapshot


def _plan_is_monitoring_candidate(plan: dict[str, Any] | None) -> bool:
    """Accept only a level-complete named-symbol plan as monitoring-only.

    This is deliberately separate from ``_plan_is_actionable``: a low-score
    plan can be persisted for monitoring, but it remains ineligible for the
    real limit-entry endpoint and never becomes a batch-search result.
    """

    if not isinstance(plan, dict):
        return False
    if plan.get("monitoringPlan") is not True or str(plan.get("analysisScope") or "").upper() not in {"TARGET", "LIVE_POSITION"}:
        return False
    direction = str(plan.get("direction") or "").upper()
    if direction not in POSITION_SIDES:
        return False
    entry = plan.get("entry") if isinstance(plan.get("entry"), dict) else {}
    trigger = _as_optional_number(entry.get("trigger"))
    zone_low = _as_optional_number(entry.get("zoneLow"))
    zone_high = _as_optional_number(entry.get("zoneHigh"))
    stop_loss = _as_optional_number(plan.get("stopLoss"))
    is_long = direction == "LONG"
    is_live_position_monitor = (
        str(plan.get("analysisScope") or "").upper() == "LIVE_POSITION"
        and (
            str(entry.get("type") or "").upper() == "LIVE_POSITION"
            or _as_optional_number(entry.get("costPrice")) is not None
        )
    )
    if is_live_position_monitor:
        cost_price = _as_optional_number(entry.get("costPrice"))
        if cost_price is None or stop_loss is None:
            return False
        if (is_long and stop_loss >= cost_price) or (not is_long and stop_loss <= cost_price):
            return False
        for target in plan.get("takeProfits") or []:
            if not isinstance(target, dict) or str(target.get("role") or "").upper() != "FIRST_TARGET":
                continue
            price = _as_optional_number(target.get("price"))
            if price is not None and (price > cost_price if is_long else price < cost_price):
                return True
        return False
    if (
        trigger is None
        or zone_low is None
        or zone_high is None
        or stop_loss is None
        or not _stop_is_outside_zone(stop_loss, trigger, zone_low, zone_high, is_long)
    ):
        return False
    for target in plan.get("takeProfits") or []:
        if not isinstance(target, dict) or str(target.get("role") or "").upper() != "FIRST_TARGET":
            continue
        price = _as_optional_number(target.get("price"))
        if price is not None and (price > trigger if is_long else price < trigger):
            return True
    return False


def _capture_execution_strategy_settings(user_id: int, plan: dict[str, Any]) -> dict[str, Any]:
    """Freeze account defaults on a new plan while honoring target overrides."""

    settings = db.get_user_binance_strategy_settings(int(user_id))
    planned_settings = plan.get("strategySettings") if isinstance(plan.get("strategySettings"), dict) else {}
    planned_mode = str(
        planned_settings.get("strategyMode")
        or plan.get("strategyMode")
        or ""
    ).strip().upper()
    if planned_mode in db.BINANCE_STRATEGY_MODES:
        # The analysis route is part of the execution contract.  Freeze the
        # route that produced the displayed plan even if account defaults are
        # changed before the user starts monitoring it.
        settings["strategyMode"] = planned_mode
    planned_route = str(planned_settings.get("levelStrategy") or "").strip().upper()
    if planned_route in db.BINANCE_LEVEL_STRATEGIES:
        # A displayed plan already derived its fixed entry/initial-stop levels
        # from this route. Do not relabel it if the user changes settings
        # between analysis and clicking "start execution".
        settings["levelStrategy"] = planned_route
    targets = plan.get("takeProfits") if isinstance(plan.get("takeProfits"), list) else []
    extension_record = next(
        (
            item
            for item in targets
            if isinstance(item, dict)
            and str(item.get("role") or "").strip().upper() == "EXTENSION_TARGET"
        ),
        None,
    )
    extension_is_unavailable = isinstance(extension_record, dict) and (
        extension_record.get("available") is False
        or _as_optional_number(extension_record.get("price")) is None
    )
    target_roles = {
        str(item.get("role") or "").strip().upper()
        for item in targets
        if isinstance(item, dict) and str(item.get("role") or "").strip()
    }
    protective_record = next(
        (
            item
            for item in targets
            if isinstance(item, dict)
            and str(item.get("role") or "").strip().upper() == "PROTECTIVE_TARGET"
        ),
        None,
    )
    # Older snapshots omitted roles and represented the protective leg as the
    # first item in a three-target list.  Count it only when it has a usable
    # price; a role-only placeholder (or ``available: false``) is still a
    # plan with no actual near-term target.
    if protective_record is None and not target_roles and len(targets) >= 3:
        protective_record = targets[0] if isinstance(targets[0], dict) else None
    has_protective_target = (
        isinstance(protective_record, dict)
        and protective_record.get("available", True) is not False
        and _as_optional_number(protective_record.get("price")) is not None
    )
    # The absence of a near-term target is a plan-shape decision, not a
    # strategy-engine decision.  Direct MODEL plans normally omit it, but a
    # classic plan can also have no confirmed protective structure.  In both
    # cases a root-level ``protectiveTakeProfitRatio`` of 0 (or a stale value
    # from an older client) must not overwrite the legacy settings schema,
    # which intentionally requires a valid internal default ratio.
    without_protective_target = not has_protective_target
    for key in ("protectiveTakeProfitRatio", "firstTakeProfitRatio", "secondTakeProfitRatio"):
        if key == "protectiveTakeProfitRatio" and without_protective_target:
            # Keep a valid persisted account boundary for the legacy settings
            # schema, but never import a plan's zero/missing ratio when there
            # is no actual near-term target to execute.
            continue
        if key == "firstTakeProfitRatio" and extension_is_unavailable:
            # The UI promotes the only confirmed target to the second
            # cumulative boundary for execution. Keep the original first
            # boundary in the frozen strategy settings so it does not become
            # an invalid duplicate global configuration.
            planned_first = planned_settings.get(key)
            planned_second = planned_settings.get("secondTakeProfitRatio", plan.get("secondTakeProfitRatio"))
            try:
                planned_first_number = float(planned_first)
                planned_second_number = float(planned_second)
            except (TypeError, ValueError):
                planned_first_number = planned_second_number = math.nan
            if math.isfinite(planned_first_number) and (
                not math.isfinite(planned_second_number)
                or planned_first_number < planned_second_number
            ):
                settings[key] = planned_first_number
            continue
        if key in plan:
            settings[key] = plan[key]
    if without_protective_target:
        # The persisted account-settings schema still requires a valid
        # protective boundary, but a direct MODEL plan may allocate its first
        # fixed target below the account default (for example 5%/10%).  Keep
        # that model allocation and lower only the internal schema boundary;
        # _plan_protection_targets() remains None for the absent target, so no
        # phantom near-term order is ever created or submitted.
        try:
            first_ratio = float(settings.get("firstTakeProfitRatio"))
        except (TypeError, ValueError):
            first_ratio = float(db.default_binance_strategy_settings()["firstTakeProfitRatio"])
        if not math.isfinite(first_ratio) or first_ratio <= 1.0:
            first_ratio = 1.1
            settings["firstTakeProfitRatio"] = first_ratio
        try:
            protective_ratio = float(settings.get("protectiveTakeProfitRatio"))
        except (TypeError, ValueError):
            protective_ratio = math.nan
        if (
            not math.isfinite(protective_ratio)
            or protective_ratio < 1.0
            or protective_ratio >= first_ratio
        ):
            settings["protectiveTakeProfitRatio"] = max(1.0, first_ratio - 0.1)
    settings = db.normalize_binance_strategy_settings(settings)
    snapshot = dict(plan)
    snapshot["protectiveTakeProfitRatio"] = settings["protectiveTakeProfitRatio"]
    snapshot["firstTakeProfitRatio"] = settings["firstTakeProfitRatio"]
    snapshot["secondTakeProfitRatio"] = settings["secondTakeProfitRatio"]
    snapshot["strategySettings"] = settings
    return snapshot


def _strategy_settings_for_plan(source_plan: object) -> dict[str, Any]:
    plan = source_plan if isinstance(source_plan, dict) else {}
    raw = dict(plan.get("strategySettings") or {}) if isinstance(plan.get("strategySettings"), dict) else {}
    # Older snapshots stored target overrides at the plan root. Preserve those
    # values while giving new plans a complete strategy-settings snapshot.
    for key in ("protectiveTakeProfitRatio", "firstTakeProfitRatio", "secondTakeProfitRatio"):
        if key in plan and key not in raw:
            raw[key] = plan[key]
    try:
        settings = db.normalize_binance_strategy_settings(raw)
    except ValueError:
        settings = db.default_binance_strategy_settings()
    if _strategy_engine(settings) == "MODEL":
        settings = _model_strategy_settings(settings)
    return settings


def _strategy_settings_for_position(position: dict[str, Any]) -> dict[str, Any]:
    source_plan = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    return _strategy_settings_for_plan(source_plan)


def _normalize_snapshot_prices(snapshot: dict[str, Any]) -> None:
    entry = snapshot.get("entry")
    if isinstance(entry, dict):
        for key in ("trigger", "zoneLow", "zoneHigh", "costPrice"):
            value = _as_optional_number(entry.get(key))
            if value is not None:
                entry[key] = _round_price(value)
    stop = _as_optional_number(snapshot.get("stopLoss"))
    if stop is not None:
        snapshot["stopLoss"] = _round_price(stop)
    targets = snapshot.get("takeProfits")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            value = _as_optional_number(target.get("price"))
            if value is not None:
                target["price"] = _round_price(value)


def _positive_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是正数") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须是正数")
    return number


def _load_user_credentials(user_id: int, credentials_by_user: dict[int, dict[str, Any]] | None) -> dict[str, Any] | None:
    if credentials_by_user is not None:
        return credentials_by_user.get(int(user_id))
    try:
        credentials = db.get_user_binance_credentials(int(user_id), include_secrets=True)
    except Exception:
        return None
    return credentials if credentials.get("configured") and credentials.get("apiKey") and credentials.get("apiSecret") else None


def _normalized_stop_values(
    network: str,
    symbol: str,
    side: str,
    left: object,
    right: object,
) -> tuple[float | None, float | None] | None:
    """Normalize two stops once so equality and directional checks agree."""

    left_number = _as_optional_number(left)
    right_number = _as_optional_number(right)
    try:
        normalized_left = (
            normalize_futures_stop_loss_price(network, symbol, side, left_number)
            if left_number is not None
            else None
        )
        normalized_right = (
            normalize_futures_stop_loss_price(network, symbol, side, right_number)
            if right_number is not None
            else None
        )
    except Exception:
        # Without exchange tick metadata, do not risk a needless cancel/repost
        # cycle. The next account refresh will retry the comparison.
        return None
    return normalized_left, normalized_right


def _normalized_stop_equal(network: str, symbol: str, side: str, left: object, right: object) -> bool | None:
    """Compare stops after applying the exchange's directional tick rounding."""

    values = _normalized_stop_values(network, symbol, side, left, right)
    return None if values is None else values[0] == values[1]


def _live_position_value_equal(left: object, right: object) -> bool:
    """Compare live quantity/cost values without the protection-price tolerance."""

    left_number = _as_optional_number(left)
    right_number = _as_optional_number(right)
    if left_number is None or right_number is None:
        return left_number is None and right_number is None
    return abs(left_number - right_number) <= max(1e-12, abs(right_number) * 1e-9)


def _futures_account_total(account_snapshot: dict[str, Any] | None) -> float | None:
    """Return the futures account equity used by the position risk boundary."""

    if not isinstance(account_snapshot, dict) or account_snapshot.get("stale"):
        return None
    account = account_snapshot.get("account") if isinstance(account_snapshot.get("account"), dict) else {}
    futures = account.get("futures") if isinstance(account.get("futures"), dict) else {}
    if futures.get("available") is False:
        return None
    for key in ("totalWalletBalance", "totalMarginBalance"):
        value = _as_optional_number(futures.get(key))
        if value is not None:
            return value
    assets = futures.get("assets") if isinstance(futures.get("assets"), list) else []
    usdt = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and str(item.get("asset") or "").upper() == "USDT"
        ),
        None,
    )
    if usdt:
        return _as_optional_number(usdt.get("walletBalance")) or _as_optional_number(usdt.get("marginBalance"))
    return None


def _position_risk_context(
    user_id: int | None,
    position: dict[str, Any],
    account_snapshot: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
    strategy_settings: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """Use the live account position when available for the account risk stop."""

    if position.get("marketMode") != "FUTURES":
        return None
    snapshot = (
        account_snapshot
        if account_snapshot is not None
        else (get_cached_binance_account_snapshot(user_id) if user_id is not None else None)
    )
    account_total = _futures_account_total(snapshot)
    if account_total is None:
        return None
    actual_position = (
        actual
        if actual is not None
        else (_cached_real_futures_position(user_id, position, snapshot) if user_id is not None else None)
    )
    source = actual_position or position
    quantity = _as_optional_number(source.get("quantity"))
    entry_price = _as_optional_number(source.get("entryPrice")) or _as_optional_number(source.get("costPrice"))
    leverage = _as_optional_number(source.get("leverage")) or _as_optional_number(position.get("leverage")) or 1.0
    if quantity is None or entry_price is None:
        return None
    settings = strategy_settings or _strategy_settings_for_position(position)
    risk_fraction = float(settings["maxAccountLossRatio"]) / 100
    return {
        "accountTotal": account_total,
        "riskBudget": account_total * risk_fraction,
        "riskFraction": risk_fraction,
        "quantity": quantity,
        "entryPrice": entry_price,
        "leverage": leverage,
    }


def _position_risk_stop(context: dict[str, float] | None, is_long: bool) -> float | None:
    if not context:
        return None
    quantity = context["quantity"]
    entry_price = context["entryPrice"]
    if quantity <= 0 or entry_price <= 0:
        return None
    distance = context["riskBudget"] / quantity
    stop = entry_price - distance if is_long else entry_price + distance
    # A non-positive long stop means the full move to zero is still within the
    # budget, so there is no usable exchange price for this boundary.
    return stop if stop > 0 else None


def _position_risk_snapshot(context: dict[str, float] | None, is_long: bool) -> dict[str, Any]:
    """Capture the account-risk boundary at the instant an execution plan starts."""

    stop = _position_risk_stop(context, is_long)
    if stop is None or not context:
        return {}
    return {
        "positionRiskStop": stop,
        "positionRiskBudget": context["riskBudget"],
        "positionRiskAccountTotal": context["accountTotal"],
        "positionRiskFraction": context.get("riskFraction", POSITION_RISK_STOP_FRACTION),
        "positionRiskEntryPrice": context["entryPrice"],
        "positionRiskQuantity": context["quantity"],
        "positionRiskLeverage": context["leverage"],
        "positionRiskCapturedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _stored_position_risk_snapshot(source_plan: dict[str, Any]) -> tuple[float | None, dict[str, float] | None]:
    """Read the immutable account-risk inputs saved with an execution plan."""

    stop = _as_optional_number(source_plan.get("positionRiskStop"))
    if stop is None:
        return None, None
    context = {
        "accountTotal": _as_optional_number(source_plan.get("positionRiskAccountTotal")),
        "riskBudget": _as_optional_number(source_plan.get("positionRiskBudget")),
        "quantity": _as_optional_number(source_plan.get("positionRiskQuantity")),
        "entryPrice": _as_optional_number(source_plan.get("positionRiskEntryPrice")),
        "leverage": _as_optional_number(source_plan.get("positionRiskLeverage")),
        "riskFraction": _as_optional_number(source_plan.get("positionRiskFraction")),
    }
    return stop, {key: value for key, value in context.items() if value is not None}


def _capture_position_risk_snapshot(
    user_id: int,
    position: dict[str, Any],
    account_snapshot: dict[str, Any] | None,
    actual: dict[str, Any] | None,
) -> dict[str, Any]:
    """Persist a risk boundary once; later account balance changes must not move it."""

    source_plan = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    if _as_optional_number(source_plan.get("positionRiskStop")) is not None:
        return position
    if not account_snapshot or account_snapshot.get("stale"):
        return position
    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    strategy_settings = _strategy_settings_for_plan(source_plan)
    snapshot = _position_risk_snapshot(
        _position_risk_context(
            user_id,
            position,
            account_snapshot,
            actual,
            strategy_settings=strategy_settings,
        ),
        is_long,
    )
    if not snapshot or position.get("id") is None:
        return position
    saved = db.merge_binance_simulated_position_plan_snapshot(user_id, int(position["id"]), snapshot)
    return saved or position


def _stop_loss_amount(entry_price: float, stop_price: float, quantity: float, is_long: bool) -> float:
    signed_distance = entry_price - stop_price if is_long else stop_price - entry_price
    return max(0.0, signed_distance) * quantity


def _plan_protection_targets(
    plan: dict[str, Any],
) -> tuple[float | None, float | None, float | None, float | None, float, float]:
    """Read target roles while keeping older two-target snapshots executable."""

    # Dynamic snapshots wrap the immutable source under executionPlan, while
    # the initial-protection path passes the raw planSnapshot directly. Treat
    # both shapes as the source plan so target prices and ratio overrides are
    # never silently dropped during the first real-position sync.
    source_plan = plan.get("executionPlan") if isinstance(plan.get("executionPlan"), dict) else plan
    if not isinstance(source_plan, dict):
        source_plan = {}
    targets = source_plan.get("takeProfits") if isinstance(source_plan.get("takeProfits"), list) else []
    protective, first, extension = _target_prices_by_role(targets)
    protective = protective or _as_optional_number(plan.get("targetProtection")) or _as_optional_number(source_plan.get("targetProtection"))
    first = first or _as_optional_number(plan.get("targetOne")) or _as_optional_number(source_plan.get("targetOne"))
    extension = extension or _as_optional_number(plan.get("targetTwo")) or _as_optional_number(source_plan.get("targetTwo"))
    settings = _strategy_settings_for_plan(source_plan)
    by_role = {
        str(item.get("role") or "").strip().upper(): item
        for item in targets
        if isinstance(item, dict) and str(item.get("role") or "").strip()
    }
    # The two-target direct-model plan deliberately has no near protective
    # target.  Do not turn that absence into a synthetic 1% allocation from
    # account defaults: callers use this value to decide whether to submit a
    # third take-profit order.
    protective_ratio = (
        _as_optional_number((by_role.get("PROTECTIVE_TARGET") or {}).get("cumulativeRatio"))
        if protective is not None
        else None
    )
    first_ratio = _as_optional_number((by_role.get("FIRST_TARGET") or {}).get("cumulativeRatio"))
    second_ratio = _as_optional_number((by_role.get("EXTENSION_TARGET") or {}).get("cumulativeRatio"))
    if protective is not None:
        protective_ratio = (
            protective_ratio
            if protective_ratio is not None
            else settings["protectiveTakeProfitRatio"]
        )
    first_ratio = first_ratio if first_ratio is not None else settings["firstTakeProfitRatio"]
    second_ratio = second_ratio if second_ratio is not None else settings["secondTakeProfitRatio"]
    extension_record = by_role.get("EXTENSION_TARGET")
    extension_unavailable = isinstance(extension_record, dict) and extension_record.get("available") is False
    if extension is None or extension_unavailable:
        # An unavailable extension may still carry a diagnostic price in older
        # snapshots. Never pass that price to the exchange as a real target.
        extension = None
        # Without a confirmed extension, the first structural target is the
        # final fixed target. Promote its effective cumulative allocation to
        # the configured second boundary, leaving the rest for the moving-stop
        # runner.
        first_ratio = second_ratio
        second_ratio = first_ratio
    # Keep a small execution runner when a persisted plan reports a fixed
    # target at 100%; this is order-safety normalization, not a model
    # preference for any particular allocation.
    if extension is not None:
        second_ratio = min(max(float(second_ratio), 2.0), 99.9)
        first_ratio = min(max(float(first_ratio), 1.1), second_ratio - 0.1)
        if protective_ratio is not None:
            protective_ratio = min(max(float(protective_ratio), 1.0), first_ratio - 0.1)
    else:
        second_ratio = min(max(float(second_ratio), 1.1), 99.9)
        first_ratio = second_ratio
        if protective_ratio is not None:
            protective_ratio = min(max(float(protective_ratio), 1.0), first_ratio - 0.1)
    return (
        protective,
        first,
        extension,
        protective_ratio,
        first_ratio,
        second_ratio,
    )


def _plan_take_profit_ratio_kwargs(
    protective_ratio: float | None,
    first_ratio: float,
    second_ratio: float,
    *,
    has_protective_target: bool,
) -> dict[str, float]:
    """Pass the near-target ratio only for new ladder snapshots."""

    kwargs = {"first_take_profit_ratio": first_ratio}
    if has_protective_target and protective_ratio is not None:
        kwargs["protective_take_profit_ratio"] = protective_ratio
    default_second = db.default_binance_strategy_settings()["secondTakeProfitRatio"]
    if not math.isclose(second_ratio, default_second, rel_tol=0.0, abs_tol=1e-9):
        kwargs["second_take_profit_ratio"] = second_ratio
    return kwargs


def _missing_plan_take_profit(actual: dict[str, Any], plan: dict[str, Any]) -> str | None:
    """Return which immutable quantity take-profit is absent from the live position."""

    # Only account payloads produced by get_futures_account carry this field.
    # Test fixtures and older cached payloads may not, so do not infer a
    # missing order from an incomplete legacy response.
    if "partialTakeProfitLevels" not in actual:
        return None
    levels = actual.get("partialTakeProfitLevels")
    protection_orders = actual.get("protectionOrders")
    if not isinstance(levels, list) and not isinstance(protection_orders, list):
        return "FIRST_TARGET"
    protective_target, first_target, extension_target, _, _, _ = _plan_protection_targets(plan)
    expected_targets = [
        ("PROTECTIVE_TARGET", protective_target),
        ("FIRST_TARGET", first_target),
        ("EXTENSION_TARGET", extension_target),
    ]
    if first_target is None:
        return None
    source_plan = plan.get("executionPlan") if isinstance(plan.get("executionPlan"), dict) else plan
    direction = str(
        plan.get("direction")
        or (source_plan.get("direction") if isinstance(source_plan, dict) else None)
        or "LONG"
    ).strip().upper()
    is_long = direction != "SHORT"
    favorable_extreme = _as_optional_number(plan.get("favorableExtreme"))
    reached_by_role = {
        "PROTECTIVE_TARGET": bool(plan.get("protectiveTargetReached")),
        "FIRST_TARGET": bool(plan.get("firstTargetReached")),
        "EXTENSION_TARGET": bool(plan.get("extensionTargetReached")),
    }
    # A target order disappears after it fills. Once any immutable target has
    # been reached, the remaining live position is smaller than the original
    # plan and must not be used to rebuild the ladder with new quantities.
    # This also covers older snapshots by using the persisted favorable
    # extreme as a monotonic evidence of a prior target touch.
    if any(
        target is not None
        and (
            reached_by_role.get(role, False)
            or (
                favorable_extreme is not None
                and _target_reached(favorable_extreme, target, is_long)
            )
        )
        for role, target in expected_targets
    ):
        return None
    for role, target in expected_targets:
        if target is None:
            continue
        if isinstance(protection_orders, list):
            # Only a quantity-based latest-price conditional target satisfies
            # the plan. Legacy LIMIT and closePosition targets are replaced.
            target_exists = any(
                isinstance(order, dict)
                and str(order.get("orderSource") or "").upper() == "ALGO"
                and str(order.get("type") or "").upper() == "TAKE_PROFIT_MARKET"
                and str(order.get("closePosition") or "").lower() not in {"true", "1"}
                and _as_optional_number(order.get("triggerPrice") or order.get("stopPrice")) is not None
                and _as_optional_number(order.get("quantity") or order.get("origQty")) is not None
                and _as_optional_number(order.get("quantity") or order.get("origQty")) > 0
                and abs(float(order.get("triggerPrice") or order.get("stopPrice")) - target) <= max(abs(target) * 1e-8, 1e-8)
                for order in protection_orders
            )
        else:
            target_exists = any(
                isinstance(level, dict)
                and _as_optional_number(level.get("price")) is not None
                and abs(float(level["price"]) - target) <= max(abs(target) * 1e-8, 1e-8)
                for level in levels
            )
        if not target_exists:
            return role
    return None


def _apply_current_plan_protection(
    user_id: int,
    position: dict[str, Any],
    plan: dict[str, Any],
    actual: dict[str, Any],
    credentials: dict[str, Any],
) -> dict[str, Any]:
    """Restore the initial target set using the current effective stop."""

    protective_target, first_target, extension_target, protective_ratio, first_ratio, second_ratio = _plan_protection_targets(plan)
    stop_loss, _stop_source = _effective_plan_stop(position, plan, actual)
    if stop_loss is None or first_target is None:
        raise ValueError("当前执行计划缺少有效止损或第一目标")
    return apply_futures_plan_protection(
        credentials["network"],
        credentials["apiKey"],
        credentials["apiSecret"],
        symbol=position["symbol"],
        position_side=str(actual.get("positionSide") or "BOTH").upper(),
        stop_loss=stop_loss,
        first_take_profit=first_target,
        protective_take_profit=protective_target,
        extension_take_profit=extension_target,
        **_plan_take_profit_ratio_kwargs(
            protective_ratio,
            first_ratio,
            second_ratio,
            has_protective_target=protective_target is not None,
        ),
    )


def _apply_current_plan_take_profits(
    position: dict[str, Any],
    plan: dict[str, Any],
    actual: dict[str, Any],
    credentials: dict[str, Any],
) -> dict[str, Any]:
    protective_target, first_target, extension_target, protective_ratio, first_ratio, second_ratio = _plan_protection_targets(plan)
    if first_target is None:
        raise ValueError("当前执行计划缺少第一目标")
    target_kwargs = _plan_take_profit_ratio_kwargs(
        protective_ratio,
        first_ratio,
        second_ratio,
        has_protective_target=protective_target is not None,
    )
    if protective_target is not None:
        target_kwargs["protective_take_profit"] = protective_target
    return apply_futures_plan_take_profits(
        credentials["network"],
        credentials["apiKey"],
        credentials["apiSecret"],
        symbol=position["symbol"],
        position_side=str(actual.get("positionSide") or "BOTH").upper(),
        first_take_profit=first_target,
        extension_take_profit=extension_target,
        **target_kwargs,
    )


def _select_effective_stop(
    candidates: list[tuple[str, float | None]],
    *,
    entry_price: float,
    quantity: float,
    is_long: bool,
) -> tuple[float | None, str | None, float | None]:
    """Choose the stop that produces the best exit outcome for this side."""

    usable = [
        (source, float(stop), _stop_outcome_amount(entry_price, float(stop), quantity, is_long))
        for source, stop in candidates
        if _as_optional_number(stop) is not None
    ]
    if not usable:
        return None, None, None
    source, stop, loss = min(usable, key=lambda item: item[2])
    return stop, source, loss


def _effective_plan_stop(
    position: dict[str, Any],
    plan: dict[str, Any],
    actual: dict[str, Any],
) -> tuple[float | None, str | None]:
    """Return the strictest stop known to the monitor.

    ``activeStop`` is the current candidate, while ``state.protectedStop``
    and the database column are monotonic protection state.  The latter must
    win when it is tighter; otherwise a transiently wider recalculation can
    make the real exchange stop fall behind the already protected plan.
    """

    state = plan.get("state") if isinstance(plan.get("state"), dict) else {}
    candidates = [
        ("PLAN", _as_optional_number(plan.get("activeStop"))),
        (
            "POSITION_RISK",
            _as_optional_number(plan.get("positionRiskStop"))
            or _as_optional_number(
                (plan.get("executionPlan") or {}).get("positionRiskStop")
                if isinstance(plan.get("executionPlan"), dict)
                else None
            ),
        ),
        ("PLAN_PROTECTED", _as_optional_number(state.get("protectedStop"))),
        ("PERSISTED_PROTECTED", _as_optional_number(position.get("protectedStop"))),
    ]
    entry_price = _as_optional_number(actual.get("entryPrice")) or _as_optional_number(position.get("costPrice"))
    quantity = _as_optional_number(actual.get("quantity")) or _as_optional_number(position.get("quantity"))
    is_long = str(position.get("side") or actual.get("side") or "LONG").upper() == "LONG"
    if entry_price is not None and quantity is not None:
        selected, source, _loss = _select_effective_stop(
            candidates,
            entry_price=entry_price,
            quantity=quantity,
            is_long=is_long,
        )
        return selected, source
    usable = [(source, stop) for source, stop in candidates if stop is not None]
    if not usable:
        return None, None
    selected_source, selected_stop = (
        max(usable, key=lambda item: item[1])
        if is_long
        else min(usable, key=lambda item: item[1])
    )
    return selected_stop, selected_source


def _auto_close_if_stop_crossed(
    user_id: int,
    position: dict[str, Any],
    actual: dict[str, Any],
    credentials: dict[str, Any],
    *,
    normalized_actual_stop: float | None,
    normalized_expected_stop: float,
    mark_price: float | None,
    is_long: bool,
    key: tuple[Any, ...],
    fingerprint: float,
    force: bool = False,
) -> dict[str, Any] | None:
    """Exit immediately when a planned stop is already beyond mark price.

    Binance will reject a STOP_MARKET trigger on the already-triggered side
    of mark price. Returning ``None`` means the caller can continue with the
    regular STOP_MARKET replacement path.
    """

    crossed = mark_price is not None and (
        (is_long and normalized_expected_stop >= mark_price)
        or (not is_long and normalized_expected_stop <= mark_price)
    )
    if not crossed and not force:
        return None

    now = time.monotonic()
    with _auto_protection_lock:
        previous = _auto_protection_state.get(key)
        if (
            previous
            and previous.get("action") == "MARKET_CLOSE"
            and previous.get("fingerprint") == fingerprint
            and now - float(previous.get("submittedAt") or 0) < AUTO_PROTECTION_RETRY_COOLDOWN_SECONDS
        ):
            return {
                "status": "APPLIED",
                "action": "MARKET_CLOSE",
                "reason": "止损已越过标记价，市价全平已提交，等待账户回读确认",
                "planStop": normalized_expected_stop,
                "realStop": normalized_actual_stop,
                "markPrice": mark_price,
                "submittedAt": previous.get("submittedAtWall"),
            }
        _auto_protection_state[key] = {
            "action": "MARKET_CLOSE",
            "fingerprint": fingerprint,
            "submittedAt": now,
            "submittedAtWall": int(time.time() * 1000),
        }

    try:
        close_result = close_futures_position_market(
            credentials["network"],
            credentials["apiKey"],
            credentials["apiSecret"],
            symbol=position["symbol"],
            position_side=str(actual.get("positionSide") or "BOTH").upper(),
        )
        request_binance_account_refresh(user_id, force_rest=True)
        return {
            "status": "APPLIED",
            "action": "MARKET_CLOSE",
            "reason": "止损已越过标记价，已提交市价全平；等待账户回读确认",
            "planStop": normalized_expected_stop,
            "realStop": normalized_actual_stop,
            "markPrice": mark_price,
            "updatedAt": int(time.time() * 1000),
            "protection": close_result,
        }
    except Exception as exc:
        with _auto_protection_lock:
            state = _auto_protection_state.get(key)
            if state and state.get("action") == "MARKET_CLOSE" and state.get("fingerprint") == fingerprint:
                _auto_protection_state.pop(key, None)
        logger.warning(
            "Binance automatic market close failed for user %s position %s",
            user_id,
            position.get("symbol"),
            exc_info=True,
        )
        return {
            "status": "ERROR",
            "action": "MARKET_CLOSE",
            "reason": f"止损已越过标记价，市价全平失败：{str(exc) or '交易所未确认平仓'}",
            "planStop": normalized_expected_stop,
            "realStop": normalized_actual_stop,
            "markPrice": mark_price,
        }


def _is_stop_crossing_validation_error(exc: BaseException) -> bool:
    """Recognize the exchange/client response for an already-triggered stop."""

    if getattr(exc, "exchange_code", None) == -2021:
        return True
    message = str(exc or "").strip().lower()
    return any(
        marker in message
        for marker in (
            "止损价必须位于当前标记价的防守侧",
            "保护价按交易所精度处理后不在标记价的正确一侧",
            "would immediately trigger",
            "immediately trigger",
        )
    )


def _cached_real_futures_position(
    user_id: int,
    position: dict[str, Any],
    account_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Find the live futures position matching one holding-monitor plan."""

    if position.get("marketMode") != "FUTURES":
        return None
    account_snapshot = account_snapshot if account_snapshot is not None else get_cached_binance_account_snapshot(user_id)
    if _futures_account_snapshot_unavailable(account_snapshot):
        return None
    account = account_snapshot.get("account") if isinstance(account_snapshot.get("account"), dict) else {}
    futures = account.get("futures") if isinstance(account.get("futures"), dict) else {}
    actual_positions = futures.get("positions") if isinstance(futures.get("positions"), list) else []
    live_position = next(
        (
            item
            for item in actual_positions
            if isinstance(item, dict)
            and str(item.get("symbol") or "").upper() == str(position.get("symbol") or "").upper()
            and str(item.get("side") or "").upper() == str(position.get("side") or "").upper()
            and _as_optional_number(item.get("quantity"))
        ),
        None,
    )
    if live_position is None:
        return None

    # The account snapshot is the single source of truth for both the account
    # panel and execution monitors. Do not overwrite it with the separate
    # public market stream, otherwise the two views can show different prices
    # for the same position.
    return live_position


def _sync_plan_live_metrics_from_account(
    plan: dict[str, Any],
    actual: dict[str, Any] | None,
) -> dict[str, Any]:
    """Use the account WebSocket snapshot as the display source of truth.

    Local calculations still determine R multiples and trailing-stop levels,
    but the account panel and a plan monitor must show the same exchange
    mark/last price, quantity and unrealized PnL for one live position.
    """

    if not isinstance(plan, dict) or not isinstance(actual, dict):
        return plan
    mark_price = _as_optional_number(actual.get("markPrice"))
    last_price = _as_optional_number(actual.get("lastPrice"))
    unrealized_profit = _as_optional_number(
        actual.get("unrealizedProfit") or actual.get("unRealizedProfit")
    )
    roe_percent = _as_optional_number(actual.get("roePercent"))
    if mark_price is not None and mark_price > 0:
        plan["currentPrice"] = _round_price(mark_price)
        plan["markPrice"] = _round_price(mark_price)
    if last_price is not None and last_price > 0:
        plan["latestPrice"] = _round_price(last_price)
        plan["lastPrice"] = _round_price(last_price)
    elif mark_price is not None and mark_price > 0:
        plan["latestPrice"] = _round_price(mark_price)
        plan["lastPrice"] = _round_price(mark_price)
    if unrealized_profit is not None:
        plan["unrealizedPnl"] = _round_money(unrealized_profit)
    if roe_percent is not None:
        plan["unrealizedPnlPercent"] = round(roe_percent, 4)
    return plan


def _futures_account_snapshot_unavailable(account_snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(account_snapshot, dict) or account_snapshot.get("stale"):
        return True
    account = account_snapshot.get("account") if isinstance(account_snapshot.get("account"), dict) else {}
    futures = account.get("futures") if isinstance(account.get("futures"), dict) else {}
    return (
        not isinstance(futures, dict)
        or futures.get("available") is False
        or futures.get("snapshotAvailable") is False
        or futures.get("stale") is True
        or not isinstance(futures.get("positions"), list)
    )


def _account_snapshot_version(account_snapshot: dict[str, Any] | None) -> str | None:
    if not isinstance(account_snapshot, dict):
        return None
    for key in ("updatedAt", "checkedAt"):
        value = account_snapshot.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _live_position_missing_checks(plan: dict[str, Any]) -> int:
    try:
        return max(0, int(plan.get("livePositionMissingChecks") or 0))
    except (TypeError, ValueError):
        return 0


def _position_live_metrics_confirmed(
    position: dict[str, Any],
    dynamic_plan: dict[str, Any] | None = None,
) -> bool:
    """Recognize both current confirmation markers and older saved metrics."""

    plan = dynamic_plan if isinstance(dynamic_plan, dict) else (
        position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    )
    if plan.get("livePositionConfirmed") is True or plan.get("livePositionConfirmedAt"):
        return True
    if any(
        _as_optional_number(plan.get(key)) is not None
        for key in ("currentPrice", "rMultiple", "favorableRMultiple")
    ):
        return True
    return any(
        _as_optional_number(position.get(key)) is not None
        for key in ("lastPrice", "lastUnrealizedPnl", "lastUnrealizedPnlPercent")
    )


def _source_execution_plan(position: dict[str, Any]) -> dict[str, Any]:
    source = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    return {
        "entry": source.get("entry"),
        "entryTiming": source.get("entryTiming"),
        "stopLoss": source.get("stopLoss"),
        "takeProfits": source.get("takeProfits") or [],
        "protectiveTakeProfitRatio": source.get("protectiveTakeProfitRatio"),
        "firstTakeProfitRatio": source.get("firstTakeProfitRatio"),
        "secondTakeProfitRatio": source.get("secondTakeProfitRatio"),
        "targetPnlSnapshot": source.get("targetPnlSnapshot"),
        "strategySettings": source.get("strategySettings"),
        "reasons": source.get("reasons") or [],
        "missingConditions": source.get("missingConditions") or [],
        "cancellationConditions": source.get("cancellationConditions") or [],
    }


def _snapshot_target_price(targets: list[Any], index: int) -> float | None:
    if index < 0 or index >= len(targets) or not isinstance(targets[index], dict):
        return None
    return _as_optional_number(targets[index].get("price"))


def _target_prices_by_role(targets: list[Any]) -> tuple[float | None, float | None, float | None]:
    """Read the new role-based ladder and fall back to legacy indexes."""

    by_role = {
        str(item.get("role") or "").strip().upper(): _as_optional_number(item.get("price"))
        for item in targets
        if isinstance(item, dict) and str(item.get("role") or "").strip()
    }
    if by_role:
        return (
            by_role.get("PROTECTIVE_TARGET"),
            by_role.get("FIRST_TARGET"),
            by_role.get("EXTENSION_TARGET"),
        )
    if len(targets) >= 3:
        return (
            _snapshot_target_price(targets, 0),
            _snapshot_target_price(targets, 1),
            _snapshot_target_price(targets, 2),
        )
    return None, _snapshot_target_price(targets, 0), _snapshot_target_price(targets, 1)


def _waiting_monitor_latest_price(position: dict[str, Any]) -> tuple[float | None, bool]:
    """Read a pending order's display price at its active execution scale."""

    settings = _strategy_settings_for_position(position)
    interval = MODEL_REFERENCE_INTERVAL if _strategy_engine(settings) == "MODEL" else "5m" if _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM else "15m"
    snapshot = _realtime_kline_snapshot(position, interval)
    items = snapshot.get("items") if isinstance(snapshot, dict) else []
    latest_price = _last_close(items)
    return (_round_price(latest_price) if latest_price > 0 else None, bool(snapshot.get("stale")))


def _waiting_monitor_plan(
    position: dict[str, Any],
    *,
    execution_status: str,
    reason: str,
    missing_checks: int = 0,
    missing_snapshot_version: str | None = None,
    live_position_confirmed: bool | None = None,
) -> dict[str, Any]:
    """Build a no-PnL plan view while a real holding is not confirmed."""

    source = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    targets = source.get("takeProfits") if isinstance(source.get("takeProfits"), list) else []
    target_protection, target_one, target_two = _target_prices_by_role(targets)
    plan = dict(position.get("dynamicPlan") or {})
    previous_current_price = _as_optional_number(plan.get("currentPrice"))
    previous_latest_price = _as_optional_number(plan.get("latestPrice"))
    previous_r_multiple = plan.get("rMultiple")
    previous_favorable_r_multiple = plan.get("favorableRMultiple")
    latest_price, latest_price_stale = _waiting_monitor_latest_price(position)
    if latest_price is None:
        latest_price = previous_latest_price or previous_current_price or _as_optional_number(position.get("lastPrice"))
        latest_price_stale = latest_price is not None
    confirmed = (
        _position_live_metrics_confirmed(position, plan)
        if live_position_confirmed is None
        else bool(live_position_confirmed)
    )
    plan.pop("latestPrice", None)
    for key in ("currentPrice", "rMultiple", "favorableRMultiple"):
        plan.pop(key, None)
    if confirmed:
        current_price = previous_current_price or latest_price or _as_optional_number(position.get("lastPrice"))
        if current_price is not None:
            plan["currentPrice"] = _round_price(current_price)
        if previous_r_multiple is not None:
            plan["rMultiple"] = previous_r_multiple
        if previous_favorable_r_multiple is not None:
            plan["favorableRMultiple"] = previous_favorable_r_multiple
    plan.update(
        {
            "status": "PENDING_ENTRY" if execution_status == PENDING_ENTRY else "MONITORING",
            "statusLabel": "已挂单" if execution_status == PENDING_ENTRY else "监控中",
            "executionStatus": execution_status,
            "monitoringStatus": "PENDING_ENTRY" if execution_status == PENDING_ENTRY else "MONITORING",
            "monitoringStatusLabel": "已挂单" if execution_status == PENDING_ENTRY else "监控中",
            "livePositionConfirmed": confirmed,
            "livePositionMissingChecks": max(0, int(missing_checks or 0)),
            "realProtectionSync": {"status": "WAITING", "reason": reason},
            "latestPrice": _round_price(latest_price) if latest_price is not None else None,
            "latestPriceStale": latest_price_stale,
            "initialStop": _as_optional_number(plan.get("initialStop")) or _as_optional_number(source.get("stopLoss")),
            "activeStop": _as_optional_number(position.get("protectedStop")) or _as_optional_number(plan.get("activeStop")) or _as_optional_number(source.get("stopLoss")),
            "targetProtection": _as_optional_number(plan.get("targetProtection")) or target_protection,
            "targetOne": _as_optional_number(plan.get("targetOne")) or target_one,
            "targetTwo": _as_optional_number(plan.get("targetTwo")) or target_two,
            "executionPlan": plan.get("executionPlan") or _source_execution_plan(position),
            "targetPnlSnapshot": plan.get("targetPnlSnapshot") or source.get("targetPnlSnapshot"),
            "direction": position.get("side"),
            "reasons": [reason],
            "missingConditions": [],
            "managementNote": reason,
            "automatedOrder": False,
            "stale": latest_price_stale,
            "updatedAt": int(time.time() * 1000),
        }
    )
    if missing_snapshot_version is not None:
        plan["lastMissingLivePositionSnapshotVersion"] = missing_snapshot_version
    return plan


def _mark_live_position_present(plan: dict[str, Any], position: dict[str, Any]) -> None:
    dynamic_plan = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    confirmed_at = dynamic_plan.get("livePositionConfirmedAt") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan.update(
        {
            "executionStatus": EXECUTING,
            "monitoringStatus": "MONITORING",
            "monitoringStatusLabel": "监控中",
            "livePositionConfirmed": True,
            "livePositionConfirmedAt": confirmed_at,
            "livePositionLastSeenAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "livePositionMissingChecks": 0,
            "lastMissingLivePositionSnapshotVersion": None,
        }
    )


def _sync_position_with_real_futures_position(user_id: int, position: dict[str, Any]) -> dict[str, Any]:
    """Keep an executing plan's quantity and cost at the live account values."""

    actual = _cached_real_futures_position(user_id, position)
    if actual is None:
        return position
    live_quantity = _as_optional_number(actual.get("quantity"))
    live_cost_price = _as_optional_number(actual.get("entryPrice"))
    if live_quantity is None and live_cost_price is None:
        return position
    quantity_changed = live_quantity is not None and not _live_position_value_equal(position.get("quantity"), live_quantity)
    cost_changed = live_cost_price is not None and not _live_position_value_equal(position.get("costPrice"), live_cost_price)
    if not quantity_changed and not cost_changed:
        return position
    synced = db.sync_binance_simulated_position_live_values(
        user_id,
        int(position["id"]),
        quantity=live_quantity if quantity_changed else None,
        cost_price=live_cost_price if cost_changed else None,
    )
    return synced or position


def _persist_initial_targets_applied(user_id: int, position: dict[str, Any]) -> None:
    """Durably remember that this monitor matched a live position once.

    The in-memory refresh result is persisted at the end of the normal cycle,
    but the exchange request can succeed immediately before a process restart
    or another worker failure.  Write the marker as soon as the initial target
    set is known/accepted so a later refresh cannot infer a fresh ladder from
    a reduced post-take-profit quantity.
    """

    position_id = position.get("id")
    if position_id is None:
        return
    dynamic = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    if dynamic.get("initialTargetsApplied") is True:
        return
    dynamic = dict(dynamic)
    dynamic["initialTargetsApplied"] = True
    try:
        db.update_binance_simulated_position_monitoring_metadata(
            user_id,
            int(position_id),
            dynamic_plan=dynamic,
        )
    except Exception:
        # The current refresh still carries the marker and will retry the
        # durable write on its final state update; never turn a successful
        # protection request into a monitor failure because metadata storage
        # was temporarily unavailable.
        logger.warning(
            "Binance initial target marker persistence failed for user %s position %s",
            user_id,
            position_id,
            exc_info=True,
        )


def _auto_apply_plan_protection(
    user_id: int,
    position: dict[str, Any],
    plan: dict[str, Any],
    credentials_by_user: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep a live position's full-position stop aligned with the plan.

    Profit targets are set at initial plan application and are intentionally
    not refreshed here. Only a stricter current stop may be replaced as the
    discipline trail advances.
    """

    if position.get("marketMode") != "FUTURES":
        return {"status": "SKIPPED", "reason": "仅对实时合约计划同步仓位止损"}

    account_snapshot = get_cached_binance_account_snapshot(user_id)
    if _futures_account_snapshot_unavailable(account_snapshot):
        return {"status": "WAITING", "reason": "合约账户暂不可用，保留保护单"}
    actual = _cached_real_futures_position(user_id, position, account_snapshot)
    if actual is None:
        return {"status": "SKIPPED", "reason": "未找到对应真实合约持仓"}
    protection_state = str(actual.get("protectionState") or "").strip().upper()
    if protection_state in {"UNAVAILABLE", "STALE"}:
        request_binance_account_refresh(user_id)
        return {"status": "WAITING", "reason": "真实保护单状态尚未由 REST 对账确认，暂不修改仓位止损"}
    if protection_state == "UNKNOWN":
        request_binance_account_refresh(user_id)
        # account.status is a fast position/balance stream and intentionally
        # omits conditional exit orders.  It marks the position UNKNOWN even
        # when the live mark/size are usable.  Keep the REST reconciliation in
        # flight, but do not skip a monotonic stop tightening: the exchange
        # endpoint re-reads the position and this path never widens a known
        # live stop.  Skipping here left refreshed moving stops unapplied for
        # the entire websocket/REST reconciliation gap.

    credentials = _load_user_credentials(user_id, credentials_by_user)
    if not credentials:
        return {"status": "WAITING", "reason": "未取得当前账户的 Binance API 配置"}

    # ``activeStop`` is the current candidate.  The persisted protected stop
    # is monotonic and may be tighter than that candidate after a prior
    # refresh, so always synchronize the strictest known plan value.
    expected_stop, _expected_source = _effective_plan_stop(position, plan, actual)
    if expected_stop is None:
        return {"status": "SKIPPED", "reason": "当前计划缺少有效仓位止损"}

    normalized_stops = _normalized_stop_values(
        credentials["network"],
        str(position.get("symbol") or ""),
        "LONG" if str(position.get("side") or "").upper() == "LONG" else "SHORT",
        actual.get("positionStopLoss") or actual.get("stopLoss"),
        expected_stop,
    )
    if normalized_stops is None:
        return {"status": "WAITING", "reason": "暂无法取得交易所价格精度，暂不重复提交仓位止损"}
    normalized_actual_stop, normalized_expected_stop = normalized_stops
    is_long = str(position.get("side") or "").upper() == "LONG"
    mark_price = _as_optional_number(actual.get("markPrice"))
    key = (int(user_id), str(position.get("symbol") or "").upper(), str(actual.get("positionSide") or "BOTH").upper())
    # Use the normalized trigger as the fingerprint. Two raw calculations
    # that land on the same exchange tick must not cause repeated orders.
    fingerprint = round(float(normalized_expected_stop), 12)
    actual_stop_missing = normalized_actual_stop is None
    now = time.monotonic()
    stop_submission_deferred = False
    with _auto_protection_lock:
        previous_stop = _auto_protection_state.get(key)
        if (
            actual_stop_missing
            and previous_stop
            and previous_stop.get("fingerprint") == fingerprint
            and previous_stop.get("missingStopSubmitted") is True
            and now - float(previous_stop.get("submittedAt") or 0) < AUTO_PROTECTION_RETRY_COOLDOWN_SECONDS
        ):
            # The previous request succeeded but the next account read has not
            # observed the order yet. Avoid duplicate orders during that short
            # propagation window; a later confirmed REST read clears this state
            # when the stop is genuinely absent.
            stop_submission_deferred = True
    crossed_result = _auto_close_if_stop_crossed(
        user_id,
        position,
        actual,
        credentials,
        normalized_actual_stop=normalized_actual_stop,
        normalized_expected_stop=normalized_expected_stop,
        mark_price=mark_price,
        is_long=is_long,
        key=key,
        fingerprint=fingerprint,
    )
    if crossed_result is not None:
        return crossed_result

    # Target orders belong to the initial match between this monitor and the
    # real position.  Persist a one-way marker in dynamicPlan so a later
    # refresh (or a process restart) cannot rebuild an already-consumed target
    # ladder from the reduced live quantity.  The only exception is the
    # first matching refresh, where an incomplete initial submission may be
    # repaired once.
    dynamic_snapshot = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    initial_targets_applied = bool(
        plan.get("initialTargetsApplied")
        or dynamic_snapshot.get("initialTargetsApplied")
    )
    if initial_targets_applied:
        plan["initialTargetsApplied"] = True
    missing_target = None if initial_targets_applied else _missing_plan_take_profit(actual, plan)
    target_key = (int(user_id), str(position.get("symbol") or "").upper(), str(actual.get("positionSide") or "BOTH").upper(), "TARGETS")
    target_fingerprint = tuple(
        round(float(value), 12) if value is not None else None
        for value in (
            _plan_protection_targets(plan)[0],
            _plan_protection_targets(plan)[1],
            _plan_protection_targets(plan)[2],
            _plan_protection_targets(plan)[3],
            _plan_protection_targets(plan)[4],
            _plan_protection_targets(plan)[5],
            _as_optional_number(actual.get("quantity")),
        )
    )
    with _auto_protection_lock:
        previous_target = _auto_protection_state.get(target_key)
    if (
        missing_target == "EXTENSION_TARGET"
        and previous_target
        and previous_target.get("fingerprint") == target_fingerprint
        and previous_target.get("ignoreMissingTarget") == "EXTENSION_TARGET"
    ):
        missing_target = None
    target_restore_note = None
    target_restore_protection = None
    if missing_target:
        now = time.monotonic()
        with _auto_protection_lock:
            previous_target = _auto_protection_state.get(target_key)
            if (
                previous_target
                and previous_target.get("fingerprint") == target_fingerprint
                and now - float(previous_target.get("submittedAt") or 0) < AUTO_PROTECTION_RETRY_COOLDOWN_SECONDS
            ):
                # Do not return here.  The target restore is still waiting
                # for account reconciliation, but a newly tightened moving
                # stop must be compared and applied in this same refresh.
                target_restore_note = "计划止盈已提交，等待账户接口回读确认"
                target_restore_protection = None
                target_restore_error = None
                missing_target = None
            else:
                _auto_protection_state[target_key] = {
                    "fingerprint": target_fingerprint,
                    "submittedAt": now,
                    "submittedAtWall": int(time.time() * 1000),
                }
            if missing_target:
                try:
                    target_restore_protection = _apply_current_plan_take_profits(position, plan, actual, credentials)
                    request_binance_account_refresh(user_id, force_rest=True)
                    target_restore_note = f"创建计划时未完成的{ {'PROTECTIVE_TARGET': '近端保护目标', 'FIRST_TARGET': '第一目标', 'EXTENSION_TARGET': '扩展目标'}.get(missing_target, '计划目标') }已自动补挂"
                    ignore_missing_target = (
                        "EXTENSION_TARGET"
                        if isinstance(target_restore_protection, dict)
                        and target_restore_protection.get("fallback") in {
                            "FIRST_TARGET_FULL_QUANTITY",
                            "EXTENSION_TARGET_HALF_POSITION",
                        }
                        else None
                    )
                    with _auto_protection_lock:
                        state = _auto_protection_state.get(target_key)
                        if state and state.get("fingerprint") == target_fingerprint and ignore_missing_target:
                            state["ignoreMissingTarget"] = ignore_missing_target
                except Exception as exc:
                    with _auto_protection_lock:
                        state = _auto_protection_state.get(target_key)
                        if state and state.get("fingerprint") == target_fingerprint:
                            _auto_protection_state.pop(target_key, None)
                    logger.warning(
                        "Binance automatic initial target restore failed for user %s position %s",
                        user_id,
                        position.get("symbol"),
                        exc_info=True,
                    )
                    # Continue into the normal stop comparison so a failed target
                    # restore cannot prevent the dynamic full-position stop from being
                    # maintained.
                    target_restore_error = str(exc) or "自动补挂计划止盈失败"
                else:
                    target_restore_error = None
                    # Mark the initial target ladder as handled only after the
                    # exchange accepted the replacement set.  This flag is
                    # written with the same dynamic plan as the stop state
                    # below, so it survives the next refresh/process restart.
                    plan["initialTargetsApplied"] = True
                    _persist_initial_targets_applied(user_id, position)
    else:
        target_restore_error = None
        # A definitive account response with existing target metadata means
        # this monitor has already been matched to the live position.  A
        # reached target is also definitive evidence that an initial target
        # existed.  Do not set the marker while the account adapter has not
        # supplied protection fields yet.
        protection_metadata_available = (
            isinstance(actual.get("partialTakeProfitLevels"), list)
            and bool(actual.get("partialTakeProfitLevels"))
        )
        target_reached_evidence = any(
            bool(plan.get(field))
            for field in (
                "protectiveTargetReached",
                "firstTargetReached",
                "extensionTargetReached",
            )
        )
        if protection_metadata_available or target_reached_evidence:
            plan["initialTargetsApplied"] = True
            _persist_initial_targets_applied(user_id, position)

    stop_matches = normalized_actual_stop == normalized_expected_stop
    if stop_matches:
        with _auto_protection_lock:
            _auto_protection_state.pop(key, None)
        if target_restore_note:
            return {
                "status": "APPLIED",
                "reason": f"{target_restore_note}；仓位止损已与当前纪律止损一致",
                "updatedAt": int(time.time() * 1000),
                "protection": target_restore_protection,
            }
        return {"status": "SYNCED", "reason": target_restore_error or "仓位止损已与当前纪律止损一致；计划止盈保持初始挂单"}
    if normalized_actual_stop is not None and not _stop_is_tighter(normalized_expected_stop, normalized_actual_stop, is_long):
        with _auto_protection_lock:
            _auto_protection_state.pop(key, None)
        return {
            "status": "SYNCED",
            "reason": "真实仓位止损比当前计划止损更严格，按纪律不放宽；计划止盈保持初始挂单",
        }
    if stop_submission_deferred:
        with _auto_protection_lock:
            previous = _auto_protection_state.get(key)
        return {
            "status": "APPLIED",
            "reason": (
                f"{target_restore_note}；仓位止损已提交，等待账户接口回读确认"
                if target_restore_note
                else "仓位止损已提交，等待账户接口回读确认"
            ),
            "submittedAt": previous.get("submittedAtWall") if previous else None,
        }

    now = time.monotonic()
    with _auto_protection_lock:
        _auto_protection_state[key] = {
            "fingerprint": fingerprint,
            "submittedAt": now,
            "submittedAtWall": int(time.time() * 1000),
            "missingStopSubmitted": actual_stop_missing,
        }

    try:
        protection = update_futures_position_stop_loss(
            credentials["network"],
            credentials["apiKey"],
            credentials["apiSecret"],
            symbol=position["symbol"],
            position_side=str(actual.get("positionSide") or "BOTH").upper(),
            stop_loss=float(normalized_expected_stop),
        )
        applied_stop = (
            _as_optional_number(protection.get("stopLoss"))
            if isinstance(protection, dict)
            else None
        ) or normalized_expected_stop
        update_cached_binance_futures_position_protection(
            user_id,
            str(position.get("symbol") or ""),
            str(actual.get("positionSide") or "BOTH").upper(),
            stop_loss=applied_stop,
        )
        request_binance_account_refresh(user_id, force_rest=True)
        return {
            "status": "APPLIED",
            "reason": f"{target_restore_note}；仓位止损已按当前纪律更新" if target_restore_note else "仓位止损已按当前纪律更新；计划止盈保持初始挂单",
            "updatedAt": int(time.time() * 1000),
            "protection": {
                key: protection.get(key)
                for key in ("stopLoss",)
                if isinstance(protection, dict)
            },
        }
    except Exception as exc:
        # The mark price can cross the candidate between the pre-check above
        # and the client's second position-risk read. In that race Binance
        # rejects STOP_MARKET with -2021 (or the client rejects the same side
        # locally). A crossed stop is already a risk event, so close the live
        # position immediately instead of leaving it unprotected.
        if _is_stop_crossing_validation_error(exc):
            forced_close = _auto_close_if_stop_crossed(
                user_id,
                position,
                actual,
                credentials,
                normalized_actual_stop=normalized_actual_stop,
                normalized_expected_stop=normalized_expected_stop,
                mark_price=mark_price,
                is_long=is_long,
                key=key,
                fingerprint=fingerprint,
                force=True,
            )
            if forced_close is not None:
                return forced_close
        with _auto_protection_lock:
            state = _auto_protection_state.get(key)
            if state and state.get("fingerprint") == fingerprint:
                _auto_protection_state.pop(key, None)
        logger.warning(
            "Binance automatic protection sync failed for user %s position %s",
            user_id,
            position.get("symbol"),
            exc_info=True,
        )
        return {"status": "ERROR", "reason": str(exc) or "自动应用保护点位失败"}


def _frozen_stop_metrics(position: dict[str, Any], dynamic_plan: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Calculate the stopped result from the effective stop, never a stale price snapshot."""

    stop_price = _as_optional_number(position.get("protectedStop")) or _as_optional_number(dynamic_plan.get("activeStop"))
    cost_price = _as_optional_number(position.get("costPrice"))
    quantity = _as_optional_number(position.get("quantity"))
    leverage = _as_optional_number(position.get("leverage")) or 1.0
    if stop_price is None or cost_price is None or quantity is None or cost_price <= 0 or quantity <= 0:
        return stop_price, None, None
    is_long = str(position.get("side") or "LONG").upper() == "LONG"
    pnl = (stop_price - cost_price) * quantity if is_long else (cost_price - stop_price) * quantity
    margin = cost_price * quantity / max(leverage, 1.0)
    pnl_percent = pnl / margin * 100 if margin > 0 else None
    return _round_price(stop_price), _round_money(pnl), round(pnl_percent, 4) if pnl_percent is not None else None


def _refresh_position(
    user_id: int,
    position: dict[str, Any],
    credentials_by_user: dict[int, dict[str, Any]] | None = None,
    *,
    apply_real_protection: bool = True,
) -> dict[str, Any]:
    with _refresh_lock:
        execution_status = str(position.get("executionStatus") or EXECUTING).upper()
        if execution_status == STOPPED:
            return _stopped_position_result(position)

        account_snapshot = get_cached_binance_account_snapshot(user_id)
        actual = _cached_real_futures_position(user_id, position, account_snapshot)
        account_unavailable = position.get("marketMode") == "FUTURES" and _futures_account_snapshot_unavailable(account_snapshot)

        if position.get("marketMode") == "FUTURES":
            if execution_status == PENDING_ENTRY and actual is None:
                reason = "合约账户暂不可用，继续保留已挂单状态。" if account_unavailable else "等待真实限价单成交并由账户快照确认持仓。"
                pending_plan = _waiting_monitor_plan(
                    position,
                    execution_status=PENDING_ENTRY,
                    reason=reason,
                )
                saved = db.update_binance_simulated_position_monitoring_metadata(
                    user_id,
                    int(position["id"]),
                    execution_status=PENDING_ENTRY,
                    dynamic_plan=pending_plan,
                    clear_live_metrics=True,
                )
                return _result_from_plan(
                    saved or position,
                    pending_plan,
                    execution_status=PENDING_ENTRY,
                    include_live_metrics=False,
                )

            if execution_status == PENDING_ENTRY and actual is not None:
                transition_plan = _waiting_monitor_plan(
                    position,
                    execution_status=EXECUTING,
                    reason="已确认真实合约持仓，开始持续同步计划点位。",
                    live_position_confirmed=True,
                )
                _mark_live_position_present(transition_plan, position)
                saved = db.update_binance_simulated_position_monitoring_metadata(
                    user_id,
                    int(position["id"]),
                    execution_status=EXECUTING,
                    dynamic_plan=transition_plan,
                    clear_live_metrics=True,
                )
                position = saved or {**position, "executionStatus": EXECUTING, "dynamicPlan": transition_plan}
                execution_status = EXECUTING

            if execution_status == EXECUTING and actual is None:
                dynamic_plan = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
                live_position_confirmed = _position_live_metrics_confirmed(position, dynamic_plan)
                if account_unavailable:
                    missing_checks = _live_position_missing_checks(dynamic_plan)
                    missing_snapshot_version = None
                    reason = "合约账户快照暂不可用，保留监控状态并等待下一次可靠回读。"
                elif not live_position_confirmed:
                    missing_checks = 0
                    missing_snapshot_version = None
                    reason = "尚未从可靠账户快照确认对应真实持仓，继续监控但不计算浮动盈亏。"
                else:
                    missing_snapshot_version = _account_snapshot_version(account_snapshot)
                    previous_snapshot_version = dynamic_plan.get("lastMissingLivePositionSnapshotVersion")
                    missing_checks = _live_position_missing_checks(dynamic_plan)
                    if missing_snapshot_version is not None and missing_snapshot_version != str(previous_snapshot_version or ""):
                        missing_checks += 1
                    if missing_checks >= MISSING_LIVE_POSITION_CONFIRMATIONS:
                        stopped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        stop_price, frozen_pnl, frozen_pnl_percent = _frozen_stop_metrics(position, dynamic_plan)
                        stop_display = _format_price(stop_price) if stop_price is not None else "当前有效止损"
                        stopped_plan = dict(dynamic_plan)
                        stopped_plan.update(
                            {
                                "status": STOPPED,
                                "statusLabel": "已止损",
                                "executionStatus": STOPPED,
                                "monitoringStatus": STOPPED,
                                "monitoringStatusLabel": "已止损",
                                "livePositionConfirmed": True,
                                "livePositionMissingChecks": missing_checks,
                                "stoppedAt": stopped_at,
                                "stopPrice": stop_price,
                                "frozenPrice": stop_price,
                                "frozenPnl": frozen_pnl,
                                "frozenPnlPercent": frozen_pnl_percent,
                                "currentPrice": stop_price,
                                "latestPrice": stop_price,
                                "stopReason": f"合约账户已连续两次可靠确认未找到对应真实持仓，按当前有效止损 {stop_display} 冻结本次止损收益；请以交易所成交记录确认实际成交价。",
                                "realProtectionSync": {"status": "SKIPPED", "reason": "真实持仓已结束，停止同步计划保护点位。"},
                            }
                        )
                        saved = db.mark_binance_simulated_position_stopped(
                            user_id,
                            int(position["id"]),
                            stop_price=stop_price,
                            stop_reason=stopped_plan["stopReason"],
                            dynamic_plan=stopped_plan,
                            current_price=stop_price,
                            unrealized_pnl=frozen_pnl,
                            unrealized_pnl_percent=frozen_pnl_percent,
                        )
                        return _result_from_plan(
                            saved or position,
                            stopped_plan,
                            execution_status=STOPPED,
                            include_live_metrics=True,
                        )
                    reason = (
                        f"合约账户暂未找到对应真实持仓，正在进行离场复核（{missing_checks}/"
                        f"{MISSING_LIVE_POSITION_CONFIRMATIONS}）。"
                    )

                waiting_plan = _waiting_monitor_plan(
                    position,
                    execution_status=EXECUTING,
                    reason=reason,
                    missing_checks=missing_checks,
                    missing_snapshot_version=missing_snapshot_version,
                    live_position_confirmed=live_position_confirmed,
                )
                saved = db.update_binance_simulated_position_monitoring_metadata(
                    user_id,
                    int(position["id"]),
                    dynamic_plan=waiting_plan,
                    clear_live_metrics=not live_position_confirmed,
                )
                return _result_from_plan(
                    saved or position,
                    waiting_plan,
                    execution_status=EXECUTING,
                    include_live_metrics=live_position_confirmed,
                )

        position = _sync_position_with_real_futures_position(user_id, position)
        position = _capture_position_risk_snapshot(user_id, position, account_snapshot, actual)
        try:
            frames, kline_price, stale = _load_frames(position)
        except RuntimeError as exc:
            # A WebSocket stream supplies the current candle, but it cannot
            # backfill the historical bars needed for a fresh ATR/structure
            # calculation.  Once the authenticated account line has confirmed
            # the live position, keep the persisted plan and live mark price
            # usable while the snapshot worker's REST history task catches up.
            # Do not turn this expected bootstrap gap into a visible monitor
            # failure or alter existing protection levels.
            if (
                actual is not None
                and not account_unavailable
                and "实时 K 线正在初始化" in str(exc)
                and isinstance(position.get("dynamicPlan"), dict)
            ):
                fallback = _persisted_position_fallback(
                    position,
                    "WebSocket 已同步当前持仓；历史 K 线由 REST 后台更新移动止损",
                )
                fallback_plan = fallback.get("plan") if isinstance(fallback.get("plan"), dict) else {}
                live_mark = _as_optional_number(actual.get("markPrice")) if isinstance(actual, dict) else None
                live_last = _as_optional_number(actual.get("lastPrice")) if isinstance(actual, dict) else None
                live_price = live_mark or live_last
                if live_price is not None:
                    fallback_plan["currentPrice"] = _round_price(live_price)
                    fallback_plan["markPrice"] = _round_price(live_mark) if live_mark is not None else fallback_plan.get("markPrice")
                    fallback_plan["latestPrice"] = _round_price(live_last) if live_last is not None else fallback_plan.get("latestPrice")
                    fallback["currentPrice"] = fallback_plan["currentPrice"]
                _sync_plan_live_metrics_from_account(fallback_plan, actual)
                fallback["currentPrice"] = fallback_plan.get("currentPrice")
                fallback["unrealizedPnl"] = fallback_plan.get("unrealizedPnl")
                fallback["unrealizedPnlPercent"] = fallback_plan.get("unrealizedPnlPercent")
                fallback_plan["historyUpdatePending"] = True
                fallback_plan["historyUpdateMessage"] = "历史 K 线由 REST 后台更新移动止损"
                fallback["plan"] = fallback_plan
                fallback.pop("marketError", None)
                return fallback
            raise
        # Futures risk is settled from mark price. Keep the last traded price
        # separately for exchange take-profit semantics and display only.
        latest_price = _as_optional_number(actual.get("lastPrice")) if isinstance(actual, dict) else None
        mark_price = _as_optional_number(actual.get("markPrice")) if isinstance(actual, dict) else None
        current_price = mark_price or kline_price or latest_price
        plan = _build_plan(
            position,
            frames,
            current_price,
            stale,
            account_snapshot=account_snapshot,
            actual=actual,
        )
        _sync_plan_live_metrics_from_account(plan, actual)
        # Existing monitors created before target snapshots were introduced
        # are upgraded once, using the best immutable entry/quantity evidence
        # available. New monitors already carry this field from save().
        position, target_pnl_snapshot = _freeze_target_pnl_snapshot(
            user_id,
            position,
            plan,
            actual,
        )
        if target_pnl_snapshot:
            plan["targetPnlSnapshot"] = target_pnl_snapshot
        state = plan["state"]
        if position.get("marketMode") == "FUTURES":
            _mark_live_position_present(plan, position)
        # A stale K-line flag means at least one cache refresh failed; it does
        # not mean the live account snapshot is unusable.  With a fresh
        # account/position, the plan state is still monotonic and may safely
        # persist a stricter stop and reconcile the real full-position stop.
        protection_context_ready = (
            position.get("marketMode") != "FUTURES"
            and not stale
            or position.get("marketMode") == "FUTURES"
            and not account_unavailable
            and actual is not None
        )
        if protection_context_ready:
            plan["realProtectionSync"] = (
                _auto_apply_plan_protection(user_id, position, plan, credentials_by_user)
                if apply_real_protection
                else {"status": "WAITING", "reason": "计划已恢复，暂不修改真实保护单"}
            )
        else:
            plan["realProtectionSync"] = {
                "status": "WAITING",
                "reason": (
                    "合约账户暂不可用，暂不自动修改真实保护单"
                    if position.get("marketMode") == "FUTURES" and account_unavailable
                    else "未确认真实持仓，暂不自动修改真实保护单"
                    if position.get("marketMode") == "FUTURES"
                    else "行情来自缓存，暂不自动修改真实保护单"
                ),
            }
        dynamic_plan = _public_plan(plan)
        persist_dynamic_state = (
            not stale
            or position.get("marketMode") == "FUTURES"
            and not account_unavailable
            and actual is not None
        )
        if persist_dynamic_state:
            saved = db.update_binance_simulated_position_state(
                user_id,
                int(position["id"]),
                protected_stop=state["protectedStop"],
                moving_stop=state["movingStop"],
                moving_stop_active=state["movingStopActive"],
                moving_stop_activation_price=state["movingStopActivationPrice"],
                moving_stop_activation_at=state["movingStopActivationAt"],
                dynamic_plan=dynamic_plan,
                current_price=plan["currentPrice"],
                unrealized_pnl=plan["unrealizedPnl"],
                unrealized_pnl_percent=plan["unrealizedPnlPercent"],
            )
            if saved:
                position = saved
                if saved.get("executionStatus") == STOPPED:
                    return _stopped_position_result(saved)
        return _result_from_plan(position, plan)


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"state", "unrealizedPnl", "unrealizedPnlPercent"}
    }


def _result_from_plan(
    position: dict[str, Any],
    plan: dict[str, Any],
    *,
    execution_status: str | None = None,
    include_live_metrics: bool = True,
) -> dict[str, Any]:
    result = dict(position)
    result["executionStatus"] = execution_status or position.get("executionStatus") or EXECUTING
    if include_live_metrics:
        result["currentPrice"] = plan.get("currentPrice")
        if result["currentPrice"] is None:
            result["currentPrice"] = plan.get("markPrice") or position.get("markPrice") or position.get("lastPrice")
        result["unrealizedPnl"] = plan.get("unrealizedPnl")
        if result["unrealizedPnl"] is None:
            result["unrealizedPnl"] = position.get("lastUnrealizedPnl")
        result["unrealizedPnlPercent"] = plan.get("unrealizedPnlPercent")
        if result["unrealizedPnlPercent"] is None:
            result["unrealizedPnlPercent"] = position.get("lastUnrealizedPnlPercent")
    else:
        result["currentPrice"] = None
        result["unrealizedPnl"] = None
        result["unrealizedPnlPercent"] = None
    result["plan"] = _public_plan(plan)
    return result


def _persisted_position_fallback(position: dict[str, Any], error: str) -> dict[str, Any]:
    """Return the last successful monitor state when a refresh cannot run."""

    plan = dict(position.get("dynamicPlan") or {})
    if plan:
        last_price = _as_optional_number(position.get("lastPrice"))
        mark_price = _as_optional_number(plan.get("markPrice")) or _as_optional_number(position.get("markPrice")) or last_price
        if _as_optional_number(plan.get("currentPrice")) is None and mark_price is not None:
            plan["currentPrice"] = _round_price(mark_price)
        if _as_optional_number(plan.get("latestPrice")) is None and last_price is not None:
            plan["latestPrice"] = _round_price(last_price)
        plan["stale"] = True
        plan["latestPriceStale"] = True
        plan.setdefault(
            "realProtectionSync",
            {"status": "WAITING", "reason": "行情或账户数据暂时不可用，保留最近一次成功的保护点位"},
        )
    execution_status = str(position.get("executionStatus") or EXECUTING).upper()
    confirmed = _position_live_metrics_confirmed(position, plan)
    if execution_status == EXECUTING:
        plan.setdefault("livePositionConfirmed", confirmed)
    include_live_metrics = (
        position.get("marketMode") != "FUTURES"
        or execution_status == STOPPED
        or confirmed
    )
    result = _result_from_plan(
        position,
        plan,
        execution_status=execution_status,
        include_live_metrics=include_live_metrics,
    )
    result["marketError"] = error
    return result


def _merge_position_refresh_failure(
    position: dict[str, Any],
    cached_item: dict[str, Any] | None,
    error: str,
) -> dict[str, Any]:
    """Combine the last in-memory result with the durable fallback state."""

    fallback = _persisted_position_fallback(position, error)
    if not isinstance(cached_item, dict):
        return fallback
    result = dict(cached_item)
    cached_plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    fallback_plan = fallback.get("plan") if isinstance(fallback.get("plan"), dict) else {}
    if fallback_plan:
        result["plan"] = {**fallback_plan, **cached_plan}
        for key, value in fallback_plan.items():
            if result["plan"].get(key) is None and value is not None:
                result["plan"][key] = value
    for key in ("currentPrice", "unrealizedPnl", "unrealizedPnlPercent"):
        if result.get(key) is None and fallback.get(key) is not None:
            result[key] = fallback[key]
    result["marketError"] = error
    return result


def _apply_initial_plan_protection(user_id: int, position: dict[str, Any]) -> dict[str, Any]:
    """Apply the plan's initial targets once when a matching live position exists."""

    if position.get("marketMode") != "FUTURES":
        return {"status": "SKIPPED", "reason": "仅对合约执行计划设置真实保护单"}
    account_snapshot = get_cached_binance_account_snapshot(user_id)
    actual = _cached_real_futures_position(user_id, position, account_snapshot)
    credentials = _load_user_credentials(user_id, None)
    if actual is None:
        # The account worker and the create request can race just after a
        # position is opened. Make one synchronous account read here so the
        # initial quantity take-profits are not lost merely because the 5s
        # background snapshot has not completed yet.
        if credentials:
            try:
                fresh_account = get_binance_account_snapshot(
                    credentials["network"], credentials["apiKey"], credentials["apiSecret"]
                )
                account_snapshot = {"stale": False, "account": fresh_account}
                actual = _cached_real_futures_position(user_id, position, account_snapshot)
            except Exception:
                logger.warning(
                    "Binance initial protection account read failed for user %s position %s",
                    user_id,
                    position.get("symbol"),
                    exc_info=True,
                )
    if actual is None:
        if account_snapshot is None or _futures_account_snapshot_unavailable(account_snapshot):
            return {"status": "WAITING", "reason": "合约账户暂不可用，稍后补挂止盈"}
        return {"status": "SKIPPED", "reason": "未找到对应真实合约持仓，稍后重试"}
    if not credentials:
        return {"status": "WAITING", "reason": "创建计划时未取得当前账户的 Binance API 配置"}
    position = _capture_position_risk_snapshot(user_id, position, account_snapshot, actual)
    source_plan = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    protective_take_profit, first_take_profit, extension_take_profit, protective_ratio, first_ratio, second_ratio = _plan_protection_targets(source_plan)
    structure_stop = _as_optional_number(source_plan.get("stopLoss"))
    position_risk_stop, risk_context = _stored_position_risk_snapshot(source_plan)
    if position_risk_stop is None:
        strategy_settings = _strategy_settings_for_plan(source_plan)
        risk_context = _position_risk_context(
            user_id=user_id,
            position=position,
            account_snapshot=account_snapshot,
            actual=actual,
            strategy_settings=strategy_settings,
        )
        position_risk_stop = _position_risk_stop(
            risk_context,
            str(position.get("side") or actual.get("side") or "LONG").upper() == "LONG",
        )
    entry_price = _as_optional_number(actual.get("entryPrice")) or _as_optional_number(position.get("costPrice"))
    quantity = _as_optional_number(actual.get("quantity")) or _as_optional_number(position.get("quantity"))
    is_long = str(position.get("side") or actual.get("side") or "LONG").upper() == "LONG"
    stop_loss, stop_source, _stop_loss_amount = _select_effective_stop(
        [("STRUCTURE", structure_stop), ("POSITION_RISK", position_risk_stop)],
        entry_price=entry_price or 0.0,
        quantity=quantity or 0.0,
        is_long=is_long,
    )
    # Keep the price-action invalidation as the fallback when account-risk
    # inputs are not available yet. When both are available, the smaller-loss
    # boundary is the actual initial protection submitted to the exchange.
    stop_loss = stop_loss or structure_stop
    if stop_loss is None or first_take_profit is None:
        return {"status": "SKIPPED", "reason": "创建计划时缺少有效初始止损或第一目标"}
    try:
        existing_stop = actual.get("positionStopLoss") or actual.get("stopLoss")
        target_kwargs = _plan_take_profit_ratio_kwargs(
            protective_ratio,
            first_ratio,
            second_ratio,
            has_protective_target=protective_take_profit is not None,
        )
        if protective_take_profit is not None:
            target_kwargs["protective_take_profit"] = protective_take_profit
        if _as_optional_number(existing_stop) is not None:
            protection = apply_futures_plan_take_profits(
                credentials["network"],
                credentials["apiKey"],
                credentials["apiSecret"],
                symbol=position["symbol"],
                position_side=str(actual.get("positionSide") or "BOTH").upper(),
                first_take_profit=first_take_profit,
                extension_take_profit=extension_take_profit,
                **target_kwargs,
            )
        else:
            protection = apply_futures_plan_protection(
                credentials["network"],
                credentials["apiKey"],
                credentials["apiSecret"],
                symbol=position["symbol"],
                position_side=str(actual.get("positionSide") or "BOTH").upper(),
                stop_loss=stop_loss,
                first_take_profit=first_take_profit,
                extension_take_profit=extension_take_profit,
                **target_kwargs,
            )
        target_key = (
            int(user_id),
            str(position.get("symbol") or "").upper(),
            str(actual.get("positionSide") or "BOTH").upper(),
            "TARGETS",
        )
        target_fingerprint = (
            round(protective_take_profit, 12) if protective_take_profit is not None else None,
            round(first_take_profit, 12),
            round(extension_take_profit, 12) if extension_take_profit is not None else None,
            round(protective_ratio, 12) if protective_ratio is not None else None,
            round(first_ratio, 12),
            round(second_ratio, 12),
            round(float(actual.get("quantity") or 0), 12),
        )
        with _auto_protection_lock:
            _auto_protection_state[target_key] = {
                "fingerprint": target_fingerprint,
                "submittedAt": time.monotonic(),
                "submittedAtWall": int(time.time() * 1000),
            }
        request_binance_account_refresh(user_id, force_rest=True)
        target_description = "第一档计划数量型止盈，余仓由移动止损管理" if extension_take_profit is None else "两档计划数量型止盈"
        stop_description = "持仓风险止损" if stop_source == "POSITION_RISK" else "结构止损"
        return {
            "status": "APPLIED",
            "reason": f"创建计划时已设置{stop_description}和{target_description}",
            "protection": protection,
        }
    except Exception as exc:
        logger.warning(
            "Binance initial plan protection setup failed for user %s position %s",
            user_id,
            position.get("symbol"),
            exc_info=True,
        )
        return {"status": "ERROR", "reason": str(exc) or "创建计划时设置初始保护单失败"}


def _stopped_position_result(position: dict[str, Any]) -> dict[str, Any]:
    plan = dict(position.get("dynamicPlan") or {})
    plan.setdefault("activeStop", position.get("protectedStop"))
    plan.setdefault("movingStop", position.get("movingStop"))
    plan.setdefault("movingStopActive", bool(position.get("movingStopActive")))
    plan.setdefault("executionPlan", _source_execution_plan(position))
    plan["status"] = STOPPED
    plan["statusLabel"] = "已止损"
    plan["executionStatus"] = STOPPED
    plan["monitoringStatus"] = STOPPED
    plan["monitoringStatusLabel"] = "已止损"
    plan["livePositionConfirmed"] = bool(plan.get("livePositionConfirmedAt"))
    plan["stoppedAt"] = position.get("stoppedAt")
    plan["stopPrice"] = position.get("stopPrice")
    plan["stopReason"] = position.get("stopReason") or "已确认对应真实持仓结束，停止同步计划点位。"
    return _result_from_plan(position, plan, execution_status=STOPPED, include_live_metrics=True)


def _stop_triggered(current_price: float, active_stop: float | None, is_long: bool) -> bool:
    stop = _as_optional_number(active_stop)
    if stop is None:
        return False
    return current_price <= stop if is_long else current_price >= stop


def _stop_reason(
    current_price: float,
    active_stop: float | None,
    is_long: bool,
    *,
    reference_label: str = "当前价",
) -> str:
    relation = "跌破" if is_long else "突破"
    return f"{reference_label} {_format_price(current_price)} 已{relation}当前有效止损 {_format_price(active_stop)}，按纪律退出并停止动态更新。"


def _load_frames(position: dict[str, Any]) -> tuple[dict[str, list[dict[str, float]]], float, bool]:
    """Read execution-plan K-lines exclusively from the backend realtime cache.

    Historical bars are seeded by the snapshot worker when a plan is created;
    this five-second path must never create per-position REST traffic.
    """

    frames: dict[str, list[dict[str, float]]] = {}
    strategy_settings = _strategy_settings_for_position(position)
    short_term_mode = _strategy_mode(strategy_settings) == STRATEGY_MODE_SHORT_TERM
    model_mode = _strategy_engine(strategy_settings) == "MODEL"
    observed_15m: list[dict[str, float]] = []
    observed_5m: list[dict[str, float]] = []
    current_by_interval: dict[str, float] = {}
    stale = False
    missing_intervals: list[str] = []
    for interval in ("4h", "1h", "15m"):
        cached = _realtime_kline_snapshot(position, interval)
        raw_items = cached.get("items") if isinstance(cached, dict) else []
        if interval == "15m":
            # Keep the still-open candle available for favorable-extreme
            # recovery.  Indicator and structure calculations continue to use
            # completed bars only.
            observed_15m = _valid_kline_bars(raw_items)
        completed = _completed_bars(raw_items)
        if len(completed) < 80:
            missing_intervals.append(interval)
            continue
        frames[interval] = completed
        current_by_interval[interval] = _last_close(raw_items) or completed[-1]["close"]
        stale = stale or bool(cached.get("stale"))

    # 5m is optional only for classic midline monitoring. Direct MODEL and
    # classic short-term routes both require it; MODEL also needs the full
    # 5m model window, rather than the shorter classic timing minimum.
    cached_5m = _realtime_kline_snapshot(position, "5m")
    raw_5m = cached_5m.get("items") if isinstance(cached_5m, dict) else []
    observed_5m = _valid_kline_bars(raw_5m)
    completed_5m = _completed_bars(raw_5m)
    required_5m_bars = int(MODEL_INPUT_WINDOWS[MODEL_REFERENCE_INTERVAL]) if model_mode else MIN_SHORT_TERM_READING_BARS
    if len(completed_5m) >= required_5m_bars:
        frames["5m"] = completed_5m
        current_by_interval["5m"] = _last_close(raw_5m) or completed_5m[-1]["close"]
        stale = stale or bool(cached_5m.get("stale"))
    elif short_term_mode or model_mode:
        missing_intervals.append("5m")
    if missing_intervals:
        waiting = "、".join(missing_intervals)
        raise RuntimeError(f"实时 K 线正在初始化（{waiting}），稍后会由后台继续更新计划")
    current_interval = "5m" if (short_term_mode or model_mode) and current_by_interval.get("5m") else "15m"
    current = current_by_interval.get(current_interval) or current_by_interval.get("1h") or current_by_interval.get("4h") or 0.0
    if current <= 0:
        raise RuntimeError("实时 K 线当前价格无效")
    frames["_observed15m"] = observed_15m
    frames["_observed5m"] = observed_5m
    return frames, current, stale


def _bootstrap_position_kline_cache(position: dict[str, Any]) -> None:
    """Seed missing execution-plan history once when a plan is saved.

    The snapshot worker also performs this work for plans restored after a
    restart. Keeping this small synchronous path makes a newly saved plan
    immediately usable while the REST snapshot worker handles later refreshes.
    """

    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:
        try:
            import binance_snapshot_worker as snapshot_worker
        except ImportError:
            return

    network = position.get("network")
    market_type = position.get("marketMode")
    symbol = position.get("symbol")
    loader = get_spot_klines if market_type == "SPOT" else get_futures_klines
    settings = _strategy_settings_for_position(position)
    short_term_mode = _strategy_mode(settings) == STRATEGY_MODE_SHORT_TERM
    model_mode = _strategy_engine(settings) == "MODEL"
    missing = []
    for interval in ("4h", "1h", "15m"):
        cached = snapshot_worker.get_cached_kline_snapshot(network, market_type, symbol, interval)
        completed = _completed_bars(cached.get("items") if isinstance(cached, dict) else [])
        if len(completed) < 80:
            missing.append(interval)
    if short_term_mode or model_mode:
        cached_5m = snapshot_worker.get_cached_kline_snapshot(network, market_type, symbol, "5m")
        completed_5m = _completed_bars(cached_5m.get("items") if isinstance(cached_5m, dict) else [])
        required_5m_bars = int(MODEL_INPUT_WINDOWS[MODEL_REFERENCE_INTERVAL]) if model_mode else MIN_SHORT_TERM_READING_BARS
        if len(completed_5m) < required_5m_bars:
            missing.append("5m")
    if not missing:
        return

    with ThreadPoolExecutor(max_workers=len(missing), thread_name_prefix="binance-plan-bootstrap") as executor:
        requests = {
            executor.submit(loader, network, symbol, interval, KLINE_LIMIT, with_meta=True): interval
            for interval in missing
        }
        for future in as_completed(requests):
            interval = requests[future]
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise BinanceApiError("交易所返回的 K 线格式无效")
                snapshot_worker.cache_kline_snapshot(
                    network,
                    market_type,
                    symbol,
                    interval,
                    result,
                )
            except Exception:
                # The background bootstrap task will retry later. Saving the
                # plan itself must remain possible during a transient outage.
                logger.warning(
                    "Binance initial K-line bootstrap failed for %s %s",
                    symbol,
                    interval,
                    exc_info=True,
                )


def _realtime_kline_snapshot(position: dict[str, Any], interval: str) -> dict[str, Any]:
    """Read the backend-owned REST snapshot cache without making a request."""

    try:
        from .binance_snapshot_worker import get_cached_kline_snapshot
    except ImportError:
        try:
            from binance_snapshot_worker import get_cached_kline_snapshot
        except ImportError:
            return {}
    try:
        return get_cached_kline_snapshot(
            position.get("network"),
            position.get("marketMode"),
            position.get("symbol"),
            interval,
        )
    except Exception:
        return {}


def _notify_snapshot_change() -> None:
    try:
        from .binance_snapshot_worker import notify_snapshot_update
    except ImportError:
        try:
            from binance_snapshot_worker import notify_snapshot_update
        except ImportError:
            return
    try:
        notify_snapshot_update()
    except Exception:
        logger.debug("Binance execution-plan snapshot notification failed", exc_info=True)


def _last_close(items: object) -> float:
    if not isinstance(items, list):
        return 0.0
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        try:
            close = float(item.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            return close
    return 0.0


def _valid_kline_bars(items: object) -> list[dict[str, float]]:
    """Normalize raw K-lines without discarding the still-open candle."""

    if not isinstance(items, list):
        return []
    bars: list[dict[str, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            open_time = float(item.get("openTime") or 0)
            close_time = float(item.get("closeTime") or 0)
            bar = {
                "openTime": open_time,
                "closeTime": close_time,
                "open": float(item.get("open") or 0),
                "high": float(item.get("high") or 0),
                "low": float(item.get("low") or 0),
                "close": float(item.get("close") or 0),
                "volume": float(item.get("volume") or 0),
            }
        except (TypeError, ValueError):
            continue
        if (
            not all(math.isfinite(value) for value in bar.values())
            or open_time <= 0
            or close_time <= open_time
            or bar["high"] < bar["low"]
            or bar["low"] <= 0
            or bar["close"] <= 0
        ):
            continue
        bars.append(bar)
    return bars


def _timestamp_ms(value: object) -> int | None:
    """Parse the timestamp formats used by live positions and K-lines."""

    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None and math.isfinite(numeric) and numeric > 0:
        # Accept second timestamps for compatibility with manually supplied
        # positions, while Binance K-lines and stored openedAt use milliseconds.
        if numeric < 100_000_000_000:
            numeric *= 1000
        return int(numeric)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _position_opened_at_ms(position: dict[str, Any]) -> int | None:
    for key in ("createdAt", "created_at", "openedAt", "opened_at"):
        timestamp = _timestamp_ms(position.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _format_timestamp_iso(timestamp_ms: int | None) -> str | None:
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _holding_period_favorable_extreme(
    position: dict[str, Any],
    bars: list[dict[str, float]],
    observed_bars: list[dict[str, float]],
    *,
    cost: float,
    is_long: bool,
    fallback_price: float,
) -> tuple[float, int | None]:
    """Return the persisted and K-line-confirmed favorable extreme.

    A position timestamp is required before raw bars can be attributed to the
    position.  Without one, using the whole cache could import pre-entry
    prices, so malformed/manual positions fall back to their current observed
    price instead.
    """

    dynamic_snapshot = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    stored = _as_optional_number(dynamic_snapshot.get("favorableExtreme"))
    stored_at = _timestamp_ms(dynamic_snapshot.get("favorableExtremeAt"))
    if stored is None:
        stored = _as_optional_number(position.get("favorableExtreme"))
    if stored is None:
        stored = _as_optional_number(position.get("peakPrice"))

    opened_at = _position_opened_at_ms(position)
    if opened_at is None:
        if stored is not None:
            return (max(cost, stored) if is_long else min(cost, stored)), stored_at
        return fallback_price if fallback_price > 0 else cost, None

    extreme = max(cost, stored) if is_long and stored is not None else min(cost, stored) if stored is not None else cost
    now_ms = int(time.time() * 1000)
    # The mark price is the only live point that is newer than the cached
    # candles. Include it before scanning history so a fresh favorable move
    # can activate/tighten the trail even while the current 5m candle is open.
    if fallback_price > 0 and (
        (is_long and fallback_price > extreme)
        or (not is_long and fallback_price < extreme)
    ):
        extreme = fallback_price
        stored_at = now_ms
    for bar in sorted(
        [*bars, *observed_bars],
        key=lambda item: _timestamp_ms(item.get("openTime")) or 0,
    ):
        bar_opened_at = _timestamp_ms(bar.get("openTime"))
        if bar_opened_at is None or bar_opened_at < opened_at:
            continue
        candidate = bar["high"] if is_long else bar["low"]
        if (is_long and candidate > extreme) or (not is_long and candidate < extreme):
            extreme = candidate
            observed_at = _timestamp_ms(bar.get("closeTime")) or bar_opened_at
            stored_at = min(observed_at, now_ms)
    return extreme, stored_at


def _build_plan(
    position: dict[str, Any],
    frames: dict[str, list[dict[str, float]]],
    current_price: float,
    stale: bool,
    account_snapshot: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    side = position["side"]
    is_long = side == "LONG"
    mark_price = _as_optional_number(actual.get("markPrice")) if isinstance(actual, dict) else None
    latest_price = _as_optional_number(actual.get("lastPrice")) if isinstance(actual, dict) else None
    # ``current_price`` is the cached K-line close when no live account price
    # is available. For futures, it becomes the mark-price reference used by
    # PnL, R, moving-stop calculation and stop state.
    current_price = mark_price or _as_optional_number(current_price) or latest_price
    if current_price is None or current_price <= 0:
        raise ValueError("计划计算缺少有效当前价格")
    latest_price = latest_price or current_price
    cost = float(position["costPrice"])
    quantity = float(position["quantity"])
    source_plan = position.get("planSnapshot") if isinstance(position.get("planSnapshot"), dict) else {}
    strategy_settings = _strategy_settings_for_plan(source_plan)
    strategy_mode = _strategy_mode(strategy_settings)
    model_branch = _model_branch(strategy_settings)
    model_prediction = None
    model_strategy = {
        "engine": _strategy_engine(strategy_settings),
        "branch": model_branch,
        "active": False,
        "accepted": False,
    }
    if _strategy_engine(strategy_settings) == "MODEL":
        try:
            from .binance_ml import predict_binance_futures_frames
        except ImportError:  # pragma: no cover - direct module compatibility
            from binance_ml import predict_binance_futures_frames
        model_input_plan = deepcopy(source_plan)
        model_input_plan.setdefault("symbol", position.get("symbol"))
        model_input_plan.setdefault("direction", side)
        model_input_plan.setdefault("entry", {"trigger": cost, "zoneLow": cost, "zoneHigh": cost})
        model_input_plan.setdefault(
            "stopLoss",
            source_plan.get("stopLoss")
            or (cost - max(cost * 0.01, 1e-8) if is_long else cost + max(cost * 0.01, 1e-8)),
        )
        try:
            model_prediction = predict_binance_futures_frames(
                {interval: list(frames[interval]) for interval in MODEL_ANALYSIS_INTERVALS if isinstance(frames.get(interval), list)},
                network=str(position.get("network") or "mainnet"),
                plan=model_input_plan,
                branch=model_branch,
                allow_rejected_research_model=True,
                model_run_id=_model_run_id(strategy_settings),
            )
        except Exception:
            logger.debug("Binance model management prediction unavailable for %s", position.get("symbol"), exc_info=True)
            model_prediction = None
        if (
            isinstance(model_prediction, dict)
            and str(model_prediction.get("direction") or "").upper() == side
            and str(model_prediction.get("modelReason") or "").upper() != "MODEL_INVALID"
        ):
            # Refresh targets and trailing parameters from the selected direct
            # model while retaining the position's immutable initial stop.
            source_plan = {
                **source_plan,
                "entry": deepcopy(model_prediction.get("entry") or source_plan.get("entry") or {}),
                "takeProfits": deepcopy(model_prediction.get("takeProfits") or []),
                "trailingStop": deepcopy(model_prediction.get("trailingStop") or {}),
                "timeCost": deepcopy(model_prediction.get("timeCost") or {}),
                "modelRunId": model_prediction.get("modelRunId"),
                "modelBranch": model_prediction.get("modelBranch"),
                "modelGenerated": True,
            }
    short_term_mode = strategy_mode == STRATEGY_MODE_SHORT_TERM
    model_mode = _strategy_engine(strategy_settings) == "MODEL"
    model_trailing = model_prediction.get("trailingStop") if isinstance(model_prediction, dict) else {}
    model_initial = model_trailing.get("INITIAL") if isinstance(model_trailing, dict) and isinstance(model_trailing.get("INITIAL"), dict) else {}
    model_breakeven = model_trailing.get("BREAKEVEN") if isinstance(model_trailing, dict) and isinstance(model_trailing.get("BREAKEVEN"), dict) else {}
    model_structure = model_trailing.get("STRUCTURE_TRAILING") if isinstance(model_trailing, dict) and isinstance(model_trailing.get("STRUCTURE_TRAILING"), dict) else {}
    model_atr = model_trailing.get("ATR_TRAILING") if isinstance(model_trailing, dict) and isinstance(model_trailing.get("ATR_TRAILING"), dict) else {}
    trailing_atr_multiplier = _as_optional_number(model_atr.get("multiplier")) or strategy_settings["trailingAtrMultiplier"]
    structure_stop_atr_multiplier = _as_optional_number(model_structure.get("bufferAtr")) if isinstance(model_structure, dict) else None
    structure_stop_atr_multiplier = structure_stop_atr_multiplier if structure_stop_atr_multiplier is not None else strategy_settings["structureStopAtrMultiplier"]
    breakeven_buffer_atr_multiplier = _as_optional_number(model_breakeven.get("bufferAtr")) if isinstance(model_breakeven, dict) else None
    breakeven_buffer_atr_multiplier = breakeven_buffer_atr_multiplier if breakeven_buffer_atr_multiplier is not None else strategy_settings["breakevenBufferAtrMultiplier"]
    moving_stop_activation_r = _as_optional_number(model_breakeven.get("triggerR")) or _as_optional_number(model_initial.get("activationR")) or strategy_settings["movingStopActivationR"]
    four_hour = _read_market(frames["4h"], "4h")
    one_hour = _read_market(frames["1h"], "1h")
    fifteen = _read_market(frames["15m"], "15m")
    bars = frames["15m"]
    bars_5m = frames.get("5m") if isinstance(frames.get("5m"), list) else None
    five_minute = (
        _read_market(bars_5m, "5m")
        if bars_5m is not None and len(bars_5m) >= MIN_SHORT_TERM_READING_BARS
        else None
    )
    # A direct MODEL plan is anchored to 5m just like SHORT_TERM execution.
    # Keep the 15m stream available for macro context, but never let a model
    # refresh silently fall back to 15m volatility or structure when 5m data
    # is present.
    execution_on_5m = (short_term_mode or model_mode) and five_minute is not None
    execution_bars = bars_5m if execution_on_5m else bars
    execution_reading = five_minute if execution_on_5m else fifteen
    model_atr_period = max(2, min(96, int(_as_optional_number(model_atr.get("period")) or 14)))
    model_structure_lookback = max(1, min(96, int(_as_optional_number(model_structure.get("lookbackBars")) or TRAILING_LOOKBACK)))
    if model_mode:
        atr_values = _atr(execution_bars, model_atr_period)
        atr = max(float(atr_values[-1]) if atr_values else 0.0, current_price * 0.0002)
    else:
        atr = max(execution_reading.atr, current_price * 0.0004)
    management_atr = max(
        execution_reading.atr,
        current_price * (0.0002 if short_term_mode or model_mode else 0.0004),
    )
    dynamic_snapshot = position.get("dynamicPlan") if isinstance(position.get("dynamicPlan"), dict) else {}
    source_stop = _as_optional_number(source_plan.get("stopLoss"))
    source_entry = source_plan.get("entry") if isinstance(source_plan.get("entry"), dict) else {}
    source_trigger = _as_optional_number(source_entry.get("trigger"))
    source_zone_low = _as_optional_number(source_entry.get("zoneLow"))
    source_zone_high = _as_optional_number(source_entry.get("zoneHigh"))
    if model_mode:
        structure_window = execution_bars[-model_structure_lookback:]
        structure_edge = (
            min(item["low"] for item in structure_window)
            if is_long
            else max(item["high"] for item in structure_window)
        )
        level_basis = {
            "strategy": "MODEL_DIRECT",
            "initialStop": {"structureEdge": structure_edge, "source": "MODEL_STRUCTURE_TRAILING"},
            "lookbackBars": model_structure_lookback,
        }
    else:
        level_basis = _initial_level_basis(
            bars,
            direction=side,
            trigger=source_trigger or cost,
            atr=atr,
            strategy=strategy_settings["levelStrategy"],
            platform_allowed=True,
        )
    structure_edge = float(level_basis["initialStop"]["structureEdge"])
    structure_stop = structure_edge - atr * structure_stop_atr_multiplier if is_long else structure_edge + atr * structure_stop_atr_multiplier
    management_structure_stop = (
        structure_edge - management_atr * structure_stop_atr_multiplier
        if is_long
        else structure_edge + management_atr * structure_stop_atr_multiplier
    )
    if not _stop_is_outside_zone(source_stop, source_trigger, source_zone_low, source_zone_high, is_long):
        source_stop = None
    stored_initial_stop = _as_optional_number(dynamic_snapshot.get("initialStop"))
    initial_stop = source_stop or stored_initial_stop or structure_stop
    if source_trigger and source_zone_low and source_zone_high and not _stop_is_outside_zone(initial_stop, source_trigger, source_zone_low, source_zone_high, is_long):
        initial_stop = _force_stop_outside_zone(
            initial_stop,
            source_zone_low,
            source_zone_high,
            is_long,
            atr,
            buffer_multiplier=structure_stop_atr_multiplier,
            zone_buffer_multiplier=strategy_settings["triggerZoneStopBufferAtrMultiplier"],
        )
    # The entry plan fixes the original structural stop. A newer structure can
    # only become a trailing candidate after the position has earned that
    # management transition; it must not silently rewrite the initial stop.
    risk = max(abs(cost - initial_stop), atr * 0.35)
    # The midline route keeps 4h as its environment.  Short-term monitoring
    # follows the same 1h -> 15m -> 5m route as target discovery: 1h decides
    # trend/range/transition while 4h only vetoes a confirmed opposite trend.
    environment_reading = one_hour if short_term_mode else four_hour
    mode = (
        "TREND"
        if environment_reading.state in {"BULL_TREND", "BEAR_TREND"}
        else "RANGE"
        if environment_reading.state == "RANGE"
        else "REBOUND"
    )
    current_location_in_range = (current_price - environment_reading.range_low) / max(environment_reading.range_high - environment_reading.range_low, atr)
    range_edge_fraction = strategy_settings["rangeEdgeFraction"]
    at_range_edge = (
        current_location_in_range <= range_edge_fraction
        if is_long
        else current_location_in_range >= 1 - range_edge_fraction
    )
    range_fade = mode == "RANGE" and at_range_edge
    calculated_target_one, calculated_target_two, calculated_target_one_source, calculated_target_two_source = _target_levels(
        direction=side,
        trigger=cost,
        risk=risk,
        mode=mode,
        range_fade=range_fade,
        four_hour=environment_reading,
        one_hour=one_hour,
        fifteen=fifteen,
        target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    calculated_target_one, calculated_target_two, calculated_target_one_source, calculated_target_two_source, target_basis = _apply_platform_targets(
        direction=side,
        trigger=cost,
        risk=risk,
        minimum_target_r=(
            strategy_settings["rangeMinimumTargetR"]
            if mode == "RANGE"
            else 1.25
            if mode == "REBOUND"
            else strategy_settings["trendMinimumTargetR"]
        ),
        basis=level_basis,
        fallback_first=calculated_target_one,
        fallback_second=calculated_target_two,
        fallback_first_source=calculated_target_one_source,
        fallback_second_source=calculated_target_two_source,
        atr=atr,
        target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
    )
    target_basis["executionBuffer"] = {
        "atrMultiplier": TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
        "policy": "目标区前侧提前成交，做多下移、做空上移；不要求触及技术位极值。",
    }
    level_basis["targets"] = target_basis
    planned_targets = source_plan.get("takeProfits") if isinstance(source_plan.get("takeProfits"), list) else []
    planned_target_protection, planned_target_one, planned_target_two = _target_prices_by_role(planned_targets)
    planned_target_protection_record = next(
        (
            item
            for item in planned_targets
            if isinstance(item, dict)
            and str(item.get("role") or "").strip().upper() == "PROTECTIVE_TARGET"
        ),
        None,
    )
    planned_target_protection = _planned_target([{"price": planned_target_protection}], 0, cost, is_long)
    planned_target_one = _planned_target([{"price": planned_target_one}], 0, cost, is_long)
    planned_target_two = _planned_target([{"price": planned_target_two}], 0, cost, is_long)
    # A direct MODEL plan owns its complete target ladder.  In particular, an
    # absent PROTECTIVE_TARGET is intentional and must not be replaced by the
    # classic strategy's nearest-structure fallback during the first refresh.
    # Otherwise the persisted plan changes shape after creation and the
    # protection layer can ask for a phantom near-target ratio.
    model_direct_without_protective = (
        _strategy_engine(strategy_settings) == "MODEL"
        and planned_target_protection is None
    )
    stored_target_one = _planned_target([{"price": dynamic_snapshot.get("targetOne")}], 0, cost, is_long)
    stored_target_two = _planned_target([{"price": dynamic_snapshot.get("targetTwo")}], 0, cost, is_long)
    target_one = planned_target_one or stored_target_one or calculated_target_one
    # An explicit empty second target means the selected route has not yet
    # confirmed another platform. Preserve that decision instead of inventing
    # a mechanical 3R target in the execution or real-protection path.
    second_target_is_explicitly_absent = bool(planned_targets) and planned_target_two is None and (
        any(str(item.get("role") or "").strip().upper() == "EXTENSION_TARGET" for item in planned_targets if isinstance(item, dict))
        or len(planned_targets) > 1
    )
    if second_target_is_explicitly_absent:
        target_two = None
    else:
        target_two = planned_target_two or stored_target_two or calculated_target_two
        if target_two is not None and target_one is not None:
            target_two = max(target_two, target_one + risk) if is_long else min(target_two, target_one - risk)

    stored_target_protection = _planned_target(
        [{"price": dynamic_snapshot.get("targetProtection")}],
        0,
        cost,
        is_long,
    )
    target_protection = (
        None
        if model_direct_without_protective
        else planned_target_protection or stored_target_protection
    )
    target_protection_source = (
        str(planned_target_protection_record.get("source") or "")
        if planned_target_protection_record
        else str(dynamic_snapshot.get("targetProtectionSource") or "")
    ) or None
    target_protection_timeframe = (
        str(planned_target_protection_record.get("timeframe") or "")
        if planned_target_protection_record
        else str(dynamic_snapshot.get("targetProtectionTimeframe") or "")
    ) or None
    if model_direct_without_protective:
        target_protection_source = None
        target_protection_timeframe = None
    if target_protection is not None and target_one is not None and (
        (is_long and target_protection >= target_one)
        or (not is_long and target_protection <= target_one)
    ):
        target_protection = None
        target_protection_source = None
        target_protection_timeframe = None
    if target_protection is None and not model_direct_without_protective:
        target_protection, target_protection_source, target_protection_timeframe = _near_term_target(
            direction=side,
            trigger=cost,
            risk=risk,
            mode=mode,
            bars_15m=bars,
            bars_5m=bars_5m,
            five_minute=five_minute,
            main_target=target_one,
            minimum_target_r=strategy_settings["nearTermMinimumTargetR"],
            target_buffer_atr_multiplier=TARGET_EXECUTION_BUFFER_ATR_MULTIPLIER,
        )

    if _strategy_engine(strategy_settings) == "MODEL":
        if isinstance(model_prediction, dict):
            model_strategy = {"engine": "MODEL", "direct": True, "updatePhase": "POSITION_LEVEL_REFRESH"}
            model_strategy["updatePhase"] = "POSITION_LEVEL_REFRESH"
            model_strategy["targetSource"] = "MODEL_DIRECT_PLAN"
        else:
            model_strategy.update({"fallback": "PREDICTION_UNAVAILABLE", "updatePhase": "POSITION_LEVEL_REFRESH"})

    _, _, _, protective_ratio, first_ratio, second_ratio = _plan_protection_targets(source_plan)
    # Without a confirmed extension platform the first structural target is
    # the final fixed target. Its effective cumulative allocation is therefore
    # the configured second boundary, with the remaining slice
    # reserved for the moving-stop runner.
    if target_two is None:
        first_ratio = second_ratio

    previous_protected = _as_optional_number(position.get("protectedStop"))
    previous_moving = _as_optional_number(position.get("movingStop"))
    favorable_extreme, favorable_extreme_at = _holding_period_favorable_extreme(
        position,
        bars,
        [
            *(bars_5m or []),
            *(frames.get("_observed15m") or []),
            *(frames.get("_observed5m") or []),
        ],
        cost=cost,
        is_long=is_long,
        fallback_price=current_price,
    )
    moving_candidate = (
        favorable_extreme - management_atr * trailing_atr_multiplier
        if is_long
        else favorable_extreme + management_atr * trailing_atr_multiplier
    )
    profit = (current_price - cost) if is_long else (cost - current_price)
    favorable_profit = favorable_extreme - cost if is_long else cost - favorable_extreme
    current_r_multiple = profit / risk if risk > 0 else 0.0
    favorable_r_multiple = favorable_profit / risk if risk > 0 else 0.0
    previous_stage = _stop_management_stage(
        dynamic_snapshot.get("stopManagementStage"),
        fallback="ATR_TRAILING" if bool(position.get("movingStopActive")) else "INITIAL",
    )
    protective_target_reached = bool(dynamic_snapshot.get("protectiveTargetReached")) or _target_reached(
        favorable_extreme,
        target_protection,
        is_long,
    )
    first_target_reached = bool(dynamic_snapshot.get("firstTargetReached")) or _target_reached(
        favorable_extreme,
        target_one,
        is_long,
    )
    extension_target_reached = bool(dynamic_snapshot.get("extensionTargetReached")) or _target_reached(
        favorable_extreme,
        target_two,
        is_long,
    )
    trend_continuation_confirmed = _trend_continuation_confirmed(
        execution_bars,
        execution_reading,
        one_hour,
        is_long,
    )
    management_stage = _advance_stop_management_stage(
        previous_stage,
        earned_one_r=favorable_r_multiple >= moving_stop_activation_r,
        trend_continuation_confirmed=trend_continuation_confirmed,
        first_target_reached=first_target_reached,
    )
    moving_active = management_stage != "INITIAL"
    breakeven_stop = (
        cost + management_atr * breakeven_buffer_atr_multiplier
        if is_long
        else cost - management_atr * breakeven_buffer_atr_multiplier
    )
    moving_stop = previous_moving
    if moving_active:
        # Once 1R is earned, the favorable-extreme ATR candidate is primary.
        # The breakeven buffer is only used while that candidate is still on
        # the losing side of the entry.
        moving_stop = _moving_stop_after_activation(
            moving_stop,
            moving_candidate,
            breakeven_stop,
            entry_price=cost,
            is_long=is_long,
        )
    technical_stop = initial_stop
    if STOP_MANAGEMENT_STAGE_ORDER[management_stage] >= STOP_MANAGEMENT_STAGE_ORDER["STRUCTURE_TRAILING"]:
        technical_stop = _tighten(initial_stop, management_structure_stop, is_long)

    management_stop = technical_stop
    management_source = "STRUCTURE"
    if moving_active and moving_stop is not None and _stop_is_tighter(moving_stop, technical_stop, is_long):
        management_stop = moving_stop
        management_source = "MOVING"

    position_risk_stop, risk_context = _stored_position_risk_snapshot(source_plan)
    if position_risk_stop is None:
        risk_context = _position_risk_context(
            user_id=None,
            position=position,
            account_snapshot=account_snapshot,
            actual=actual,
            strategy_settings=strategy_settings,
        )
        position_risk_stop = _position_risk_stop(risk_context, is_long)
    # The executable stop is the candidate with the smallest loss at the
    # actual entry and quantity. This includes the immutable account-risk
    # boundary captured when the execution plan started. A wider structural
    # stop must not leave that stricter account-risk boundary as display-only.
    selected_stop, selected_source, _selected_loss = _select_effective_stop(
        [
            (management_source, management_stop),
            ("POSITION_RISK", position_risk_stop),
        ],
        entry_price=cost,
        quantity=quantity,
        is_long=is_long,
    )
    if selected_stop is None:
        selected_stop = management_stop
        selected_source = management_source
    # Once the position has earned the normal management transition, a model
    # predicted low adverse excursion may tighten the live stop. It can never
    # widen a classic/position-risk stop and must remain outside the original
    # trigger zone.
    if model_strategy.get("accepted") and moving_active and isinstance(model_prediction, dict):
        model_side = model_prediction.get(side.lower()) if isinstance(model_prediction.get(side.lower()), dict) else {}
        expected_mae = _as_optional_number(model_side.get("expectedMaeR"))
        if expected_mae is not None and 0 < expected_mae < 1.0 and risk > 0:
            model_mae_r = min(1.0, max(0.5, expected_mae * 1.25))
            model_stop = cost - risk * model_mae_r if is_long else cost + risk * model_mae_r
            zone_valid = _stop_is_outside_zone(
                model_stop,
                source_trigger,
                source_zone_low,
                source_zone_high,
                is_long,
            ) if source_trigger is not None and source_zone_low is not None and source_zone_high is not None else True
            if zone_valid and (selected_stop is None or _stop_is_tighter(model_stop, selected_stop, is_long)):
                selected_stop = model_stop
                selected_source = "MODEL_RISK"
                model_strategy["stopAdjustment"] = {
                    "applied": True,
                    "expectedMaeR": round(expected_mae, 3),
                    "toR": round(model_mae_r, 3),
                    "boundary": "CLASSIC_TRIGGER_ZONE_AND_ONE_WAY_STOP",
                }
        elif "stopAdjustment" not in model_strategy:
            model_strategy["stopAdjustment"] = {"applied": False, "reason": "模型MAE未满足收紧条件"}
    protected_stop = _tighten(previous_protected, selected_stop, is_long)
    active_stop_source = selected_source or "STRUCTURE"
    # Keep the current-plan selection independent from the one-way protected
    # stop persisted for the real order.  Otherwise an old tighter stop makes
    # the UI report a stale candidate source.
    active_stop_loss = _stop_loss_amount(cost, selected_stop, quantity, is_long) if selected_stop else None
    position_risk_loss = _stop_loss_amount(cost, position_risk_stop, quantity, is_long) if position_risk_stop else None
    position_risk_account_total = risk_context.get("accountTotal") if risk_context else None
    position_risk_budget = risk_context.get("riskBudget") if risk_context else None
    position_risk_leverage = risk_context.get("leverage") if risk_context else None
    position_risk_notional = (
        risk_context.get("entryPrice") * risk_context.get("quantity")
        if risk_context
        else None
    )
    position_risk_margin = (
        position_risk_notional / position_risk_leverage
        if position_risk_notional is not None and position_risk_leverage and position_risk_leverage > 0
        else None
    )
    activation_price = _as_optional_number(position.get("movingStopActivationPrice"))
    activation_at = position.get("movingStopActivationAt")
    if moving_active and activation_price is None:
        activation_price = favorable_extreme
        activation_at = _format_timestamp_iso(favorable_extreme_at) or datetime.now(timezone.utc).isoformat(timespec="seconds")

    status = _plan_status(
        current_price,
        protected_stop,
        target_one,
        is_long,
        active_stop_source == "MOVING",
        stop_reference=mark_price,
        target_reference=latest_price,
    )
    leverage = float(position.get("leverage") or 1)
    pnl_percent = (profit / cost * 100 * leverage) if cost else 0
    reasons = []
    missing = []
    expected_bias = "BULL" if is_long else "BEAR"
    four_hour_veto_clear = _four_hour_veto_is_clear(four_hour, expected_bias)
    if short_term_mode:
        if four_hour_veto_clear:
            reasons.append("4h 未出现与短线持仓方向相反的强趋势，仅作为隐藏否决过滤。")
        else:
            missing.append("4h 已形成与短线持仓方向相反的强趋势，进入防守复核。")
        if _direction_context_is_compatible(one_hour, expected_bias):
            reasons.append(f"1h 环境仍可管理{ '做多' if is_long else '做空' }持仓。")
        else:
            missing.append("等待 1h 环境重新回到持仓方向兼容状态。")
    else:
        if four_hour.bias == expected_bias:
            reasons.append(f"4h 背景与{ '做多' if is_long else '做空' }持仓方向一致。")
        else:
            missing.append("4h 背景尚未与持仓方向一致，继续观察。")
        if one_hour.bias == expected_bias:
            reasons.append(f"1h 结构与{ '做多' if is_long else '做空' }方向一致。")
        else:
            missing.append("等待 1h 结构重新与持仓方向一致。")
    if fifteen.bias == expected_bias:
        reasons.append(f"15m 结构仍支持{ '多头' if is_long else '空头' }防守。")
    else:
        missing.append("15m 出现反向偏置，仅作预警；未触及当前有效止损前不自动反向。")
    moving_candidate_profitable = _stop_is_profitable(moving_candidate, cost, is_long)
    activation_label = f"{moving_stop_activation_r:g}R"
    trailing_label = f"{trailing_atr_multiplier:g} ATR"
    if management_stage == "BREAKEVEN":
        if moving_candidate_profitable:
            reasons.append(f"已达到 {activation_label}，持仓有利极点计算出的 {trailing_label} 移动止损已进入盈利区，直接使用该值，不启用保本保护阶段。")
        else:
            reasons.append(f"已达到 {activation_label}，但持仓有利极点计算出的移动止损仍在亏损侧，使用保本保护作为兜底。")
    elif management_stage == "STRUCTURE_TRAILING":
        execution_timeframe = "5m" if short_term_mode else "15m"
        reasons.append(f"已达到 {activation_label} 且 {execution_timeframe} 延续与 1h 背景确认，结构止损开始单向跟随；移动止损优先按持仓有利极点和 {trailing_label} 计算。")
    elif management_stage == "ATR_TRAILING":
        reasons.append(f"第一止盈已触及且趋势延续确认，移动止损按持仓期间有利极值（多头最高价/空头最低价）和 {trailing_label} 单向收紧。")
    else:
        missing.append(f"持仓期间有利极值尚未达到 {activation_label}，维持初始结构止损。")
    if moving_active and not trend_continuation_confirmed and management_stage == "BREAKEVEN":
        execution_timeframe = "5m" if short_term_mode else "15m"
        if moving_candidate_profitable:
            missing.append(f"{execution_timeframe}/1h趋势延续尚未确认，暂不提前收紧结构止损；盈利侧移动止损仍按持仓有利极点保护。")
        else:
            missing.append(f"移动止损候选仍在亏损侧，{execution_timeframe}/1h趋势延续尚未确认，继续使用保本保护。")
    if stale:
        missing.append("行情来自最近成功缓存，恢复实时数据后重新确认。")
    opposite_timeframes = [
        timeframe
        for timeframe, reading in (("4h", four_hour), ("1h", one_hour), ("15m", fifteen))
        if reading.bias not in {expected_bias, "NEUTRAL"}
    ]
    bias_conflict = bool(opposite_timeframes)
    management_note = (
        f"{', '.join(opposite_timeframes)} 出现与持仓方向相反的偏置；按纪律仅作反向预警，必须同时确认强反向冲击、主趋势线或极点突破及后续跟随，不能把执行中的{ '做多' if is_long else '做空' }计划改成反向仓位。"
        if bias_conflict
        else f"执行中的{ '做多' if is_long else '做空' }计划方向保持不变；仅在止损、第一目标或已确认管理事件发生时更新状态。"
    )
    cancellation = [
        f"已收盘 K 线确认{ '跌破' if is_long else '突破' }当前有效止损 { _format_price(protected_stop) }，执行退出纪律。",
        "若 1h 或 4h 同时完成强反向冲击、结构突破和后续跟随，进入减仓/退出复核，不自动创建反向仓位。",
        "目标位仅作计划参考，不以预测替代止损纪律。",
    ]
    timeframe_payload = {
        "4h": _timeframe_payload(four_hour),
        "1h": _timeframe_payload(one_hour),
        "15m": _timeframe_payload(fifteen),
    }
    if five_minute is not None:
        timeframe_payload["5m"] = _timeframe_payload(
            five_minute,
            extra=[
                "短线模式：5m 只负责执行信号与短线结构确认，不能单独改变 1h 方向。"
                if short_term_mode
                else "中线模式：5m 仅用于入场时机参考，不覆盖 1h/4h 背景。",
            ],
        )
    execution_targets = deepcopy(planned_targets)
    if execution_targets:
        target_count = len(execution_targets)
        for index, target in enumerate(execution_targets):
            if not isinstance(target, dict):
                continue
            role = str(target.get("role") or "").strip().upper()
            if not role:
                if target_count >= 3:
                    role = ("PROTECTIVE_TARGET", "FIRST_TARGET", "EXTENSION_TARGET")[index] if index < 3 else ""
                elif target_count == 2:
                    role = ("FIRST_TARGET", "EXTENSION_TARGET")[index]
                elif target_count == 1:
                    role = "FIRST_TARGET"
            cumulative_ratio = {
                "PROTECTIVE_TARGET": protective_ratio,
                "FIRST_TARGET": first_ratio,
                "EXTENSION_TARGET": second_ratio,
            }.get(role)
            if cumulative_ratio is not None:
                target["cumulativeRatio"] = cumulative_ratio
    if not execution_targets:
        if target_protection is not None:
            execution_targets.append(
                {
                    "role": "PROTECTIVE_TARGET",
                    "label": "近端保护目标",
                    "price": _round_price(target_protection),
                    "rMultiple": round(abs(target_protection - cost) / risk, 2) if risk > 0 else None,
                    "source": target_protection_source,
                    "timeframe": target_protection_timeframe,
                    "cumulativeRatio": protective_ratio,
                    "semantics": "当前监控按已确认的5m/15m平台计算，不使用近期单点极值后备。",
                }
            )
    if model_strategy.get("accepted") and target_one is not None:
        model_first = next(
            (
                item
                for item in execution_targets
                if isinstance(item, dict) and str(item.get("role") or "").strip().upper() == "FIRST_TARGET"
            ),
            None,
        )
        if model_first is not None:
            model_first["price"] = _round_price(target_one)
            model_first["rMultiple"] = round(abs(target_one - cost) / risk, 2) if risk > 0 else None
            model_first["source"] = "时序模型预期路径（受经典结构目标边界约束）"
        if target_one is not None:
            execution_targets.append(
                {
                    "role": "FIRST_TARGET",
                    "label": "第一目标",
                    "price": _round_price(target_one),
                    "rMultiple": round(abs(target_one - cost) / risk, 2) if risk > 0 else None,
                    "source": calculated_target_one_source,
                    "timeframe": "5m/15m/1h" if short_term_mode else "15m/1h",
                    "cumulativeRatio": (
                        first_ratio
                    ),
                    "semantics": "当前监控按已确认结构的可达侧并留前侧执行缓冲。",
                }
            )
        if target_two is not None:
            execution_targets.append(
                {
                    "role": "EXTENSION_TARGET",
                    "label": "第二目标",
                    "price": _round_price(target_two),
                    "rMultiple": round(abs(target_two - cost) / risk, 2) if risk > 0 else None,
                    "source": calculated_target_two_source,
                    "timeframe": "1h",
                    "cumulativeRatio": second_ratio,
                    "semantics": "仅在新确认和跟随后启用，不机械外推。",
                }
            )
    has_target_roles = any(
        isinstance(item, dict) and str(item.get("role") or "").strip()
        for item in execution_targets
    )
    if target_protection is not None and not any(
        isinstance(item, dict)
        and str(item.get("role") or "").strip().upper() == "PROTECTIVE_TARGET"
        for item in execution_targets
    ):
        protective_record = {
            "role": "PROTECTIVE_TARGET",
            "label": "近端保护目标",
            "price": _round_price(target_protection),
            "rMultiple": round(abs(target_protection - cost) / risk, 2) if risk > 0 else None,
            "source": target_protection_source or "持仓监控：最近20根5m/15m确认结构或局部边界。",
            "timeframe": target_protection_timeframe or "5m/15m",
            "cumulativeRatio": protective_ratio,
            "semantics": "旧计划缺失近端目标时，按当前持仓成本和最近20根5m/15m结构补算；不是固定收益百分比。",
        }
        if has_target_roles:
            execution_targets.insert(0, protective_record)
        else:
            legacy_roles = ("FIRST_TARGET", "EXTENSION_TARGET")
            role_targets = []
            for index, item in enumerate(execution_targets[:2]):
                if not isinstance(item, dict):
                    continue
                role_item = dict(item)
                role_item.setdefault("role", legacy_roles[index])
                role_targets.append(role_item)
            # Preserve the legacy two-row order for older UI snapshots while
            # adding explicit roles so all consumers can still identify the
            # three levels without relying on indexes.
            execution_targets = [*role_targets, protective_record]
    return {
        "currentPrice": _round_price(current_price),
        "latestPrice": _round_price(latest_price),
        "lastPrice": _round_price(latest_price),
        "markPrice": _round_price(mark_price),
        "unrealizedPnl": _round_money(profit * quantity),
        "unrealizedPnlPercent": round(pnl_percent, 4),
        "riskAmount": _round_price(risk),
        "rMultiple": round(current_r_multiple, 3),
        "favorableRMultiple": round(favorable_r_multiple, 3),
        "initialStop": _round_price(initial_stop),
        "favorableExtreme": _round_price(favorable_extreme),
        "favorableExtremeAt": favorable_extreme_at,
        "levelBasis": level_basis,
        "targetOneSource": calculated_target_one_source if not planned_target_one and not stored_target_one else None,
        "targetTwoSource": calculated_target_two_source if not planned_target_two and not stored_target_two else None,
        "movingStopCandidate": _round_price(moving_candidate),
        "movingStop": _round_price(moving_stop),
        "positionRiskStop": _round_price(position_risk_stop),
        "positionRiskStopLoss": _round_money(position_risk_loss) if position_risk_loss is not None else None,
        "positionRiskLoss": _round_money(position_risk_loss) if position_risk_loss is not None else None,
        "positionRiskAccountTotal": _round_money(position_risk_account_total) if position_risk_account_total is not None else None,
        "positionRiskBudget": _round_money(position_risk_budget) if position_risk_budget is not None else None,
        "positionRiskFraction": risk_context.get("riskFraction") if risk_context else _as_optional_number(source_plan.get("positionRiskFraction")),
        "positionRiskLeverage": position_risk_leverage,
        "positionRiskNotional": _round_money(position_risk_notional) if position_risk_notional is not None else None,
        "positionRiskMargin": _round_money(position_risk_margin) if position_risk_margin is not None else None,
        "movingStopActive": moving_active,
        "movingStopEligible": moving_active,
        "movingStopActivationR": moving_stop_activation_r,
        "stopManagementStage": management_stage,
        "protectiveTargetReached": protective_target_reached,
        "trendContinuationConfirmed": trend_continuation_confirmed,
        "firstTargetReached": first_target_reached,
        "extensionTargetReached": extension_target_reached,
        "activeStopSource": active_stop_source,
        "activeStopLoss": _round_money(active_stop_loss) if active_stop_loss is not None else None,
        "movingStopActivationPrice": _round_price(activation_price),
        "movingStopActivationAt": activation_at,
        "activeStop": _round_price(selected_stop),
        "targetProtection": _round_price(target_protection),
        "targetProtectionSource": target_protection_source,
        "targetProtectionTimeframe": target_protection_timeframe,
        "targetOne": _round_price(target_one),
        "targetTwo": _round_price(target_two),
        "targetPnlSnapshot": deepcopy(source_plan.get("targetPnlSnapshot")) if isinstance(source_plan.get("targetPnlSnapshot"), dict) else None,
        "executionStatus": "EXECUTING",
        "strategyEngine": _strategy_engine(strategy_settings),
        "modelBranch": model_branch,
        "modelPrediction": model_prediction,
        "modelStrategy": model_strategy,
        "strategyMode": strategy_mode,
        "strategyModeLabel": "短线模式" if short_term_mode else "中线模式",
        "timeframeRoles": {
            "4h": "背景方向与强反向否决" if short_term_mode else "主趋势/区间背景",
            "1h": "短线方向环境" if short_term_mode else "方向与结构确认",
            "15m": "位置、回撤与主要结构",
            "5m": "执行信号、触发与短线失败确认" if short_term_mode else "入场时机参考",
        },
        "status": status,
        "statusLabel": {
            "PROTECTING": "移动止损中",
            "PROFIT": "盈利保护",
            "AT_RISK": "触及防守",
            "WATCH": "观察中",
        }[status],
        "timeframes": timeframe_payload,
        "reasons": reasons,
        "missingConditions": missing,
        "cancellationConditions": cancellation,
        "executionPlan": {
            "entry": source_plan.get("entry"),
            # Keep the original short-timeframe entry context with the
            # execution snapshot so the market chart can render the same
            # trigger zone and 5m reference after a plan is started.
            "entryTiming": source_plan.get("entryTiming"),
            "stopLoss": source_plan.get("stopLoss"),
            "takeProfits": execution_targets,
            "protectiveTakeProfitRatio": protective_ratio,
            "firstTakeProfitRatio": first_ratio,
            "secondTakeProfitRatio": second_ratio,
            "strategyMode": strategy_mode,
            "strategySettings": strategy_settings,
            "reasons": source_plan.get("reasons") or [],
            "missingConditions": source_plan.get("missingConditions") or [],
            "cancellationConditions": source_plan.get("cancellationConditions") or [],
        },
        "stale": stale,
        "updatedAt": int(time.time() * 1000),
        "state": {
            "protectedStop": protected_stop,
            "movingStop": moving_stop,
            "movingStopActive": moving_active,
            "favorableExtreme": favorable_extreme,
            "favorableExtremeAt": favorable_extreme_at,
            "stopManagementStage": management_stage,
            "protectiveTargetReached": protective_target_reached,
            "firstTargetReached": first_target_reached,
            "extensionTargetReached": extension_target_reached,
            "trendContinuationConfirmed": trend_continuation_confirmed,
            "activeStopSource": active_stop_source,
            "movingStopActivationPrice": activation_price,
            "movingStopActivationAt": activation_at,
        },
        "direction": side,
        "biasConflict": bias_conflict,
        "biasConflictTimeframes": opposite_timeframes,
        "directionChangeAllowed": False,
        "managementAction": "DEFEND_AND_REVIEW" if bias_conflict else "HOLD_AND_MANAGE",
        "managementNote": management_note,
        "automatedOrder": False,
    }


def _state_changed(position: dict[str, Any], state: dict[str, Any]) -> bool:
    before_values = {
        "protectedStop": position.get("protectedStop"),
        "movingStop": position.get("movingStop"),
        "movingStopActivationPrice": position.get("movingStopActivationPrice"),
    }
    for key, before_value in before_values.items():
        before = _as_optional_number(before_value)
        after = _as_optional_number(state.get(key))
        if before is None and after is not None:
            return True
        if before is not None and after is not None and abs(before - after) > max(abs(after) * 1e-9, 1e-9):
            return True
    if bool(position.get("movingStopActive")) != bool(state.get("movingStopActive")):
        return True
    return (position.get("movingStopActivationAt") or None) != (state.get("movingStopActivationAt") or None)


def _tighten(previous: float | None, candidate: float | None, is_long: bool) -> float | None:
    if candidate is None or not math.isfinite(candidate) or candidate <= 0:
        return previous
    if previous is None:
        return candidate
    return max(previous, candidate) if is_long else min(previous, candidate)


def _stop_is_profitable(stop_price: float | None, entry_price: float, is_long: bool) -> bool:
    if stop_price is None or not math.isfinite(stop_price) or stop_price <= 0:
        return False
    return stop_price > entry_price if is_long else stop_price < entry_price


def _moving_stop_after_activation(
    previous: float | None,
    moving_candidate: float | None,
    breakeven_stop: float | None,
    *,
    entry_price: float,
    is_long: bool,
) -> float | None:
    """Use the favorable-extreme trail when it protects profit.

    Breakeven is only a fallback while the calculated ATR trail remains on
    the losing side of the entry. Both paths stay monotonic through
    ``_tighten`` so a later ATR change cannot loosen an existing stop.
    """

    if _stop_is_profitable(moving_candidate, entry_price, is_long):
        return _tighten(previous, moving_candidate, is_long)
    return _tighten(previous, breakeven_stop, is_long)


def _stop_outcome_amount(entry_price: float, stop_price: float, quantity: float, is_long: bool) -> float:
    """Return signed PnL at the stop: losses are positive, protected profit negative."""

    signed_distance = entry_price - stop_price if is_long else stop_price - entry_price
    return signed_distance * quantity


def _stop_management_stage(value: object, *, fallback: str = "INITIAL") -> str:
    normalized = str(value or "").strip().upper()
    if normalized in STOP_MANAGEMENT_STAGE_ORDER:
        return normalized
    return fallback if fallback in STOP_MANAGEMENT_STAGE_ORDER else "INITIAL"


def _advance_stop_management_stage(
    previous_stage: str,
    *,
    earned_one_r: bool,
    trend_continuation_confirmed: bool,
    first_target_reached: bool,
) -> str:
    """Advance the discipline stop state without ever relaxing it."""

    stage = _stop_management_stage(previous_stage)
    if earned_one_r or STOP_MANAGEMENT_STAGE_ORDER[stage] >= STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"]:
        stage = "BREAKEVEN"
    if (
        STOP_MANAGEMENT_STAGE_ORDER[stage] >= STOP_MANAGEMENT_STAGE_ORDER["BREAKEVEN"]
        and trend_continuation_confirmed
    ):
        stage = "STRUCTURE_TRAILING"
    if (
        STOP_MANAGEMENT_STAGE_ORDER[stage] >= STOP_MANAGEMENT_STAGE_ORDER["STRUCTURE_TRAILING"]
        and first_target_reached
        and trend_continuation_confirmed
    ):
        stage = "ATR_TRAILING"
    return stage


def _trend_continuation_confirmed(
    bars: list[dict[str, float]],
    fifteen: Any,
    one_hour: Any,
    is_long: bool,
) -> bool:
    """Require completed 15m continuation and no opposing 1h structure.

    This deliberately does not treat one counter-trend candle as a trend
    change. The same gate is used before structure and ATR trailing advance.
    """

    if len(bars) < 5:
        return False
    expected_bias = "BULL" if is_long else "BEAR"
    opposite_bias = "BEAR" if is_long else "BULL"
    if getattr(fifteen, "bias", None) != expected_bias or getattr(one_hour, "bias", None) == opposite_bias:
        return False
    recent = bars[-3:]
    directional_bars = 0
    for bar in recent:
        span = max(bar["high"] - bar["low"], 1e-12)
        if is_long:
            closes_near_extreme = (bar["close"] - bar["low"]) / span >= 0.58
            directional = bar["close"] > bar["open"] and closes_near_extreme
        else:
            closes_near_extreme = (bar["high"] - bar["close"]) / span >= 0.58
            directional = bar["close"] < bar["open"] and closes_near_extreme
        directional_bars += int(directional)
    continuation_break = (
        bars[-1]["close"] > max(bar["high"] for bar in bars[-5:-1])
        if is_long
        else bars[-1]["close"] < min(bar["low"] for bar in bars[-5:-1])
    )
    return directional_bars >= 2 or continuation_break


def _target_reached(current_price: float, target: float | None, is_long: bool) -> bool:
    target_value = _as_optional_number(target)
    if target_value is None:
        return False
    return current_price >= target_value if is_long else current_price <= target_value


def _stop_is_tighter(candidate: float | None, reference: float | None, is_long: bool) -> bool:
    candidate_value = _as_optional_number(candidate)
    reference_value = _as_optional_number(reference)
    if candidate_value is None or reference_value is None:
        return False
    tolerance = max(abs(reference_value) * 1e-9, 1e-9)
    return candidate_value > reference_value + tolerance if is_long else candidate_value < reference_value - tolerance


def _planned_target(targets: object, index: int, cost: float, is_long: bool) -> float | None:
    if not isinstance(targets, list) or index >= len(targets) or not isinstance(targets[index], dict):
        return None
    price = _as_optional_number(targets[index].get("price"))
    if price is None:
        return None
    if is_long and price <= cost:
        return None
    if not is_long and price >= cost:
        return None
    return price


def _plan_status(
    current: float,
    active_stop: float,
    target_one: float | None,
    is_long: bool,
    moving_active: bool,
    *,
    stop_reference: float | None = None,
    target_reference: float | None = None,
) -> str:
    # A stop is a risk decision and follows mark price; target progress is
    # based on the actual traded/latest price used by take-profit orders.
    stop_price_reference = _as_optional_number(stop_reference) or current
    target_price_reference = _as_optional_number(target_reference) or current
    stop_hit = stop_price_reference <= active_stop if is_long else stop_price_reference >= active_stop
    target_reached = target_one is not None and (
        target_price_reference >= target_one if is_long else target_price_reference <= target_one
    )
    if stop_hit:
        return "AT_RISK"
    if target_reached or moving_active:
        return "PROFIT" if target_reached else "PROTECTING"
    return "WATCH"


def _as_optional_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _round_price(value: float | None) -> float | None:
    number = _as_optional_number(value)
    if number is None:
        return None
    return round(number, _price_decimal_places(number))


def _format_price(value: float | None) -> str:
    rounded = _round_price(value)
    if rounded is None:
        return "--"
    return f"{rounded:.{_price_decimal_places(rounded)}f}".rstrip("0").rstrip(".")


def _price_decimal_places(value: float) -> int:
    """Use four useful digits for sub-unit prices and up to four decimals otherwise."""

    absolute = abs(float(value))
    if absolute >= 1:
        return 4
    return max(0, 4 - math.floor(math.log10(absolute)) - 1)


def _round_money(value: float) -> float:
    return round(float(value), 8)
