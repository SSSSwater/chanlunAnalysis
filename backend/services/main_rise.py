"""Strict main-rise candidate policy built on the shared price-action result."""

from __future__ import annotations

from typing import Any

from .price_action import MIN_ANALYSIS_BARS, VOLUME_CONFIRM_RATIO


MAIN_RISE_POLICY_VERSION = "1.2.0"
MAIN_RISE_MAX_ATTEMPTS = 30
MAIN_RISE_MIN_RISK_REWARD = 1.5

# These are launch/restart structures from the trend and implementation notes.
# Generic reversal, range-middle, climax and extension-only records are excluded.
MAIN_RISE_SETUP_TYPES = frozenset(
    {
        "BREAKOUT_RETEST_UP",
        "RANGE_BREAKOUT_CONFIRMED",
        "RANGE_BREAKOUT_RETEST",
        "TREND_PULLBACK_H1",
        "TREND_PULLBACK_H2",
        "TREND_FIRST_PULLBACK",
        "TREND_TWO_LEG_PULLBACK",
        "BULL_FLAG",
        "MICRO_CHANNEL_BREAK",
    }
)

VOLUME_REQUIRED_SETUP_TYPES = frozenset(
    {
        "BREAKOUT_RETEST_UP",
        "RANGE_BREAKOUT_CONFIRMED",
        "RANGE_BREAKOUT_RETEST",
        "BULL_FLAG",
        "MICRO_CHANNEL_BREAK",
    }
)


def evaluate_main_rise_candidate(
    result: dict[str, Any] | None,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Return a match and auditable rejection reasons for one analyzed stock."""

    reasons: list[str] = []
    if not isinstance(result, dict):
        return _rejected("未返回有效的价格行为分析结果")

    price_action = result.get("priceAction") or {}
    raw_klines = result.get("rawKlines") or []
    if len(raw_klines) < MIN_ANALYSIS_BARS:
        reasons.append(f"完整日K不足 {MIN_ANALYSIS_BARS} 根")

    uncertainty = price_action.get("uncertainty") or {}
    if uncertainty.get("incompleteLatestBar") or (
        raw_klines and isinstance(raw_klines[-1], dict) and raw_klines[-1].get("completed") is False
    ):
        reasons.append("最新日K尚未完成")

    environment = price_action.get("environment") or {}
    if environment.get("state") != "BULL_TREND":
        reasons.append(f"当前环境不是多头趋势：{environment.get('label') or environment.get('state') or '未知'}")
    if environment.get("reviewRequired"):
        reasons.append("趋势环境仍需人工复核")

    market_mode = price_action.get("marketMode") or {}
    if market_mode.get("mode") != "TREND":
        reasons.append(f"当前市场模式不是趋势：{market_mode.get('label') or market_mode.get('mode')}")

    volume = price_action.get("volume") or price_action.get("volumeTurnover") or {}
    if volume.get("available") is False or volume.get("state") in {"UNKNOWN", "UNAVAILABLE"}:
        reasons.append("成交量参与证据不可用")

    if reasons:
        return _rejected(*reasons)

    latest_price = _number(current_price) or _number((result.get("summary") or {}).get("latestClose"))
    if latest_price <= 0:
        return _rejected("缺少有效最新价")

    setups_by_type = {
        str(item.get("type") or item.get("kind")): item
        for item in price_action.get("setups") or []
        if isinstance(item, dict)
    }
    plans_by_setup = {
        str(item.get("setupId")): item
        for item in price_action.get("tradePlans") or []
        if isinstance(item, dict) and item.get("setupId")
    }

    candidates: list[dict[str, Any]] = []
    for signal in price_action.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        setup_type = str(signal.get("type") or "")
        if setup_type not in MAIN_RISE_SETUP_TYPES:
            continue
        setup = setups_by_type.get(setup_type)
        plan = plans_by_setup.get(str(signal.get("setupId") or ""))
        evaluation = _evaluate_signal(
            signal=signal,
            setup=setup,
            plan=plan,
            volume=volume,
            latest_price=latest_price,
        )
        if evaluation["match"]:
            candidates.append(evaluation["match"])
        else:
            reasons.extend(evaluation["reasons"])

    if not candidates:
        return _rejected(*(reasons or ["没有确认的主升启动结构"]))

    candidates.sort(key=lambda item: (item["distancePct"], item["setupType"]))
    return {
        "matched": True,
        "match": candidates[0],
        "reasons": [],
        "policyVersion": MAIN_RISE_POLICY_VERSION,
    }


def match_main_rise_candidate(
    result: dict[str, Any] | None,
    current_price: float | None = None,
) -> dict[str, Any] | None:
    """Return the first valid main-rise plan, or None."""

    return evaluate_main_rise_candidate(result, current_price).get("match")


def _evaluate_signal(
    *,
    signal: dict[str, Any],
    setup: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    volume: dict[str, Any],
    latest_price: float,
) -> dict[str, Any]:
    setup_type = str(signal.get("type") or "")
    reasons: list[str] = []
    if not setup:
        reasons.append(f"{setup_type}缺少对应结构记录")
    elif str(setup.get("status")) != "CONFIRMED":
        reasons.append(f"{setup_type}尚未确认")
    elif bool(setup.get("reviewRequired")):
        reasons.append(f"{setup_type}仍需人工复核")
    if setup and setup.get("direction") != "BUY":
        reasons.append(f"{setup_type}结构不是做多方向")
    if setup and setup.get("marketMode") != "TREND":
        reasons.append(f"{setup_type}结构不是趋势模式")

    if not plan:
        reasons.append(f"{setup_type}缺少对应交易计划")
    else:
        if plan.get("status") != "CONFIRMED":
            reasons.append(f"{setup_type}交易计划尚未确认")
        if plan.get("direction") != "BUY":
            reasons.append(f"{setup_type}交易计划不是做多方向")
        if plan.get("reviewRequired"):
            reasons.append(f"{setup_type}交易计划仍需人工复核")
        if plan.get("executable") is not True:
            reasons.append(f"{setup_type}交易计划不可执行")

    if signal.get("direction") != "BUY":
        reasons.append(f"{setup_type}不是做多方向")
    if signal.get("status") != "CONFIRMED":
        reasons.append(f"{setup_type}信号尚未确认")
    if bool(signal.get("reviewRequired")):
        reasons.append(f"{setup_type}信号仍需人工复核")
    if signal.get("marketMode") != "TREND":
        reasons.append(f"{setup_type}不是趋势模式")
    if signal.get("executable") is not True:
        reasons.append(f"{setup_type}没有可执行做多计划")

    setup_volume = (setup or {}).get("volume") or (setup or {}).get("volumeEvidence") or {}
    signal_volume = signal.get("volume") or signal.get("volumeEvidence") or setup_volume or volume
    if signal_volume.get("available") is False or signal_volume.get("state") in {"UNKNOWN", "UNAVAILABLE"}:
        reasons.append(f"{setup_type}缺少有效成交量证据")
    if setup_type in VOLUME_REQUIRED_SETUP_TYPES and not _volume_qualified(signal_volume, volume):
        reasons.append(f"{setup_type}未达到突破所需量能确认")

    trigger = _number(signal.get("triggerPrice")) or _number((plan or {}).get("triggerPrice"))
    entry_limit = _number(signal.get("entryLimit")) or _number((plan or {}).get("entryLimit"))
    invalidation = _number(signal.get("invalidationPrice")) or _number((plan or {}).get("structuralInvalidation"))
    first_target = _target_price(signal.get("firstTarget")) or _target_price((plan or {}).get("firstTarget"))
    cancellation = signal.get("cancellationConditions") or (plan or {}).get("cancellationConditions") or []

    if trigger <= 0:
        reasons.append(f"{setup_type}缺少触发价")
    if entry_limit <= 0:
        reasons.append(f"{setup_type}缺少入场上限")
    if invalidation <= 0:
        reasons.append(f"{setup_type}缺少结构失效价")
    if first_target <= 0:
        reasons.append(f"{setup_type}缺少第一目标")
    if not cancellation:
        reasons.append(f"{setup_type}缺少取消条件")
    if first_target > 0 and entry_limit > 0 and first_target <= entry_limit:
        reasons.append(f"{setup_type}第一目标没有高于入场上限")

    risk_reward = 0.0
    if entry_limit > 0 and invalidation > 0 and first_target > entry_limit:
        risk_reward = (first_target - entry_limit) / max(entry_limit - invalidation, 0.001)
        if risk_reward < MAIN_RISE_MIN_RISK_REWARD:
            reasons.append(f"{setup_type}空间风险比不足 {MAIN_RISE_MIN_RISK_REWARD:.1f}R")

    if entry_limit > 0 and latest_price > entry_limit:
        reasons.append(f"{setup_type}当前价高于入场上限，禁止追价")
    if invalidation > 0 and latest_price < invalidation:
        reasons.append(f"{setup_type}当前价已跌破结构失效价")

    if reasons:
        return _rejected(*reasons)

    distance_pct = abs(latest_price - entry_limit) / entry_limit * 100
    return {
        "matched": True,
        "match": {
            "setupType": setup_type,
            "setupId": signal.get("setupId") or (setup or {}).get("setupId"),
            "planId": signal.get("planId") or (plan or {}).get("planId"),
            "direction": "BUY",
            "status": "CONFIRMED",
            "marketMode": "TREND",
            "currentPrice": round(latest_price, 3),
            "triggerPrice": round(trigger, 3),
            "entryLimit": round(entry_limit, 3),
            "invalidationPrice": round(invalidation, 3),
            "firstTarget": round(first_target, 3),
            "riskReward": round(risk_reward, 2),
            "distancePct": round(distance_pct, 2),
            "reasons": list(signal.get("reasons") or (setup or {}).get("evidence") or []),
            "cancellationConditions": list(cancellation),
            "confirmationTime": signal.get("confirmedAt") or (setup or {}).get("confirmedAt"),
            "volume": signal_volume,
        },
        "reasons": [],
    }


def _volume_qualified(signal_volume: dict[str, Any], fallback: dict[str, Any]) -> bool:
    if signal_volume.get("qualified") is True:
        return True
    latest_ratio = _number(signal_volume.get("latestRatio")) or _number(fallback.get("latestRatio"))
    return bool(signal_volume.get("available", fallback.get("available", False)) and latest_ratio >= VOLUME_CONFIRM_RATIO)


def _rejected(*reasons: str) -> dict[str, Any]:
    unique = list(dict.fromkeys(reason for reason in reasons if reason))
    return {
        "matched": False,
        "match": None,
        "reasons": unique or ["未满足主升筛选条件"],
        "policyVersion": MAIN_RISE_POLICY_VERSION,
    }


def _target_price(value: Any) -> float:
    if isinstance(value, dict):
        return _number(value.get("price"))
    return _number(value)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0
