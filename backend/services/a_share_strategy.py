"""A-share adapter for the Binance-style disciplined strategy contract.

The Binance workspace and A-share workspace deliberately share the same plan
vocabulary: complete conditions, a trigger zone, structural protection,
targets, cancellation rules and timeframe evidence.  They do *not* share
execution assumptions.  This module keeps A shares cash-long-only, carries
T+1 and price-limit constraints forward, and leaves every action as a
next-session plan rather than an order.
"""

from __future__ import annotations

from typing import Any


STRATEGY_ID = "a-share-disciplined-price-action-v1"
STRATEGY_VERSION = "2026-09-binance-plan-parity-v2"
SOFT_TRIAL_CONDITIONS = frozenset({"relativeVolume"})
HARD_BLOCK_CONDITIONS = frozenset({
    "weeklyContext",
    "dailyDirection",
    "completedBar",
    "triggerPending",
    "structuralStop",
    "targetSpace",
    "aShareExecution",
})


def attach_a_share_strategy_plan(
    result: dict[str, Any],
    raw_bars: list[dict[str, Any]] | None = None,
    *,
    scope: str = "STOCK",
) -> dict[str, Any]:
    """Attach the normalized plan to a stock/index/discipline result in place."""

    if not isinstance(result, dict):
        return result
    price_action = result.get("priceAction") if isinstance(result.get("priceAction"), dict) else {}
    plan = build_a_share_strategy_plan(
        price_action,
        raw_bars or [],
        scope=scope,
        discipline_plan=result if scope.upper() == "DISCIPLINE" else None,
    )
    result["strategyPlan"] = plan
    if price_action is not None:
        price_action["strategyPlan"] = plan
    return result


def build_a_share_strategy_plan(
    price_action: dict[str, Any],
    raw_bars: list[dict[str, Any]],
    *,
    scope: str = "STOCK",
    discipline_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Binance-compatible plan while retaining A-share constraints.

    The price-action engine remains the source of structural detection.  This
    adapter applies the shared execution sequence around that evidence instead
    of creating a second, conflicting pattern detector.
    """

    safe_scope = str(scope or "STOCK").upper()
    discipline_plan = discipline_plan or {}
    environment = dict(price_action.get("environment") or {})
    metrics = dict(price_action.get("metrics") or {})
    volume = dict(price_action.get("volumeTurnover") or price_action.get("volume") or {})
    profile = dict(price_action.get("profile") or {})
    levels = dict(discipline_plan.get("levels") or {})
    position = dict(discipline_plan.get("position") or {})
    has_position = bool(position) or bool((discipline_plan.get("positionContext") or {}).get("hasPosition"))
    action = str(discipline_plan.get("action") or "").upper()
    bars = _clean_bars(raw_bars)
    latest = bars[-1] if bars else {}
    latest_close = _number(latest.get("close")) or _number(metrics.get("latestClose"))
    selected = _select_trade_plan(
        price_action.get("tradePlans") or [],
        action=action,
        has_position=has_position,
    )
    setup = dict(price_action.get("activeSetup") or {}) or _select_setup(price_action.get("setups") or [], selected)
    direction = _direction_for_scope(safe_scope, selected, action, has_position)
    market_mode = str(
        selected.get("marketMode")
        or setup.get("marketMode")
        or price_action.get("marketMode", {}).get("mode")
        or "REBOUND"
    ).upper()
    market_mode_label = str(
        selected.get("marketModeLabel")
        or setup.get("marketModeLabel")
        or {"TREND": "趋势", "RANGE": "区间", "REBOUND": "反弹"}.get(market_mode, "等待判断")
    )
    weekly = _weekly_reading(bars)
    entry = _entry_payload(selected, levels, latest_close, direction=direction)
    stop_loss = _stop_loss(selected, levels, direction=direction)
    targets, risk_reward = _target_payloads(selected, levels, entry["trigger"], stop_loss, market_mode, direction=direction)
    minimum_target_r = _number(profile.get("thresholds", {}).get("minimumRoomR")) or 1.5
    tradeability = _a_share_tradeability(
        latest,
        discipline_plan=discipline_plan,
        direction=direction,
        has_position=has_position,
    )
    timing = _entry_timing(
        direction=direction,
        entry=entry,
        latest_close=latest_close,
        action=action,
        has_position=has_position,
        tradeability=tradeability,
    )
    checks = _condition_checks(
        scope=safe_scope,
        direction=direction,
        environment=environment,
        weekly=weekly,
        selected=selected,
        setup=setup,
        metrics=metrics,
        volume=volume,
        latest=latest,
        entry=entry,
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=risk_reward,
        minimum_target_r=minimum_target_r,
        timing=timing,
        tradeability=tradeability,
        action=action,
        has_position=has_position,
    )
    condition_met = sum(1 for item in checks if item["passed"])
    condition_total = len(checks)
    missing_ids = {item["id"] for item in checks if not item["passed"]}
    hard_blocked = bool(missing_ids.intersection(HARD_BLOCK_CONDITIONS))
    stale = not bool(checks[2]["passed"]) if len(checks) > 2 else True
    terminal_extension = not bool(checks[4]["passed"]) and _terminal_extension(metrics, setup)
    status, trial_eligible = _plan_status(
        scope=safe_scope,
        direction=direction,
        action=action,
        condition_met=condition_met,
        condition_total=condition_total,
        missing_ids=missing_ids,
        hard_blocked=hard_blocked,
        terminal_extension=terminal_extension,
    )
    blocked_reasons = _blocked_reasons(
        missing_ids,
        stale=stale,
        terminal_extension=terminal_extension,
        tradeability=tradeability,
    )
    cancellation = _cancellation_conditions(
        selected=selected,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        scope=safe_scope,
        has_position=has_position,
    )
    reasons = [item["reason"] for item in checks if item["passed"]]
    missing = [item["missing"] for item in checks if not item["passed"]]
    execution_action = _execution_action(safe_scope, direction, action, has_position)
    trailing_stop = _trailing_stop_payload(levels, scope=safe_scope, has_position=has_position)
    level_basis = {
        "selectedStrategy": "PRICE_ACTION_STRUCTURE",
        "resolvedStrategy": "A_SHARE_COMPLETED_DAILY_STRUCTURE",
        "executionReady": status in {"ARMED", "TRIAL"},
        "initialStop": {
            "source": levels.get("initialStopSource") or selected.get("structuralReference") or "已确认日线结构",
            "label": levels.get("initialStopReason") or "止损位于当前日线触发结构的另一侧。",
            "structureEdge": _round(_number(selected.get("structuralInvalidation")) or stop_loss),
            "usesPlatform": False,
            "zone": {"low": entry["zoneLow"], "high": entry["zoneHigh"]} if entry["zoneLow"] else None,
        },
        "targets": {
            "source": targets[0].get("source") if targets else "暂无已确认上方磁铁",
            "minimumTargetR": minimum_target_r,
            "riskReward": _round(risk_reward),
        },
    }
    return {
        "strategyId": STRATEGY_ID,
        "strategyVersion": STRATEGY_VERSION,
        "marketType": "A_SHARE",
        "scope": safe_scope,
        "direction": direction,
        "status": status,
        "strategyMode": "MIDLINE",
        "strategyModeLabel": "日线中线模式",
        "marketMode": market_mode,
        "marketModeLabel": market_mode_label,
        "timeframeRoles": {
            "1w": "背景方向与强反向否决",
            "1d": "日线环境、位置、结构与目标空间",
            "nextSession": "下一交易日触发、T+1 与涨跌停执行约束",
        },
        "timeframes": {
            "1w": weekly,
            "1d": _daily_timeframe(environment, metrics, latest_close, volume),
            "nextSession": {
                "state": timing["state"],
                "bias": "LONG_ONLY",
                "close": _round(latest_close),
                "evidence": [timing["reason"], tradeability["reason"]],
            },
        },
        "entry": entry,
        "entryTiming": timing,
        "entryConfirmation": {
            "mode": "COMPLETED_DAILY_BAR",
            "required": True,
            "state": timing["state"],
            "setup": setup.get("type") or selected.get("setupType") or "WAIT_FOR_ENTRY",
        },
        "stopLoss": _round(stop_loss),
        "trailingStop": trailing_stop,
        "takeProfits": targets,
        "minimumTargetR": _round(minimum_target_r),
        "riskReward": _round(risk_reward),
        "confidence": round(condition_met / condition_total * 100) if condition_total else 0,
        "conditionCompleteness": round(condition_met / condition_total * 100) if condition_total else 0,
        "conditionMet": condition_met,
        "conditionTotal": condition_total,
        "conditionChecks": checks,
        "trialEligible": trial_eligible,
        "trialMissingCondition": next(iter(missing_ids), None) if trial_eligible else None,
        "blockedReasons": blocked_reasons,
        "reasons": reasons,
        "missingConditions": missing,
        "cancellationConditions": cancellation,
        "levelBasis": level_basis,
        "lastPrice": _round(latest_close),
        "latestPrice": _round(latest_close),
        "executionAction": execution_action,
        "executionConstraint": _execution_constraint(execution_action),
        "positionRequired": execution_action == "SELL_EXISTING_LONG",
        "shortSellingAllowed": False,
        "executable": status == "ARMED" and execution_action in {"BUY_LONG", "SELL_EXISTING_LONG"},
        "automatedOrder": False,
        "stale": stale,
        "sourcePages": list(selected.get("sourcePages") or setup.get("sourcePages") or []),
        "sourcePolicy": {"id": "trading-discipline-source-policy", "version": "v2", "localOnly": True},
        "profile": {"id": profile.get("profileId") or STRATEGY_ID, "version": profile.get("profileVersion") or STRATEGY_VERSION},
    }


def _select_trade_plan(plans: list[dict[str, Any]], *, action: str, has_position: bool) -> dict[str, Any]:
    candidates = [dict(item) for item in plans if isinstance(item, dict)]
    preferred_direction = "SELL" if has_position and action in {"SELL", "REDUCE"} else "BUY"
    for status in ("CONFIRMED", "SCALP", "WATCH", "PENDING", "BLOCKED"):
        found = next((item for item in candidates if item.get("direction") == preferred_direction and item.get("status") == status), None)
        if found:
            return found
    return next((item for item in candidates if item.get("direction") == preferred_direction), candidates[0] if candidates else {})


def _select_setup(setups: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    candidates = [dict(item) for item in setups if isinstance(item, dict)]
    setup_id = plan.get("setupId")
    return next((item for item in candidates if setup_id and item.get("setupId") == setup_id), candidates[0] if candidates else {})


def _direction_for_scope(scope: str, plan: dict[str, Any], action: str, has_position: bool) -> str:
    if scope == "INDEX":
        return "EXPOSURE"
    if has_position and action in {"SELL", "REDUCE"}:
        return "EXIT_LONG"
    if has_position:
        return "MANAGE_LONG"
    return "LONG" if plan.get("direction") == "BUY" else "OBSERVE"


def _entry_payload(plan: dict[str, Any], levels: dict[str, Any], latest_close: float, *, direction: str) -> dict[str, Any]:
    if direction not in {"LONG", "MANAGE_LONG"}:
        return {
            "trigger": None,
            "zoneLow": None,
            "zoneHigh": None,
            "type": "WAIT_STRUCTURE",
            "structure": None,
            "entryLimit": None,
            "lastClose": _round(latest_close),
        }
    trigger = _number(plan.get("triggerPrice")) or _number(plan.get("entryLimit")) or _number(levels.get("plannedEntryPrice"))
    entry_limit = _number(plan.get("entryLimit")) or _number(levels.get("plannedEntryPrice")) or trigger
    source_zone = dict(plan.get("entryZone") or {})
    if trigger > 0 or entry_limit > 0:
        zone_low = _number(source_zone.get("low")) or min(value for value in (trigger, entry_limit) if value > 0)
        zone_high = _number(source_zone.get("high")) or max(trigger, entry_limit)
    else:
        zone_low = zone_high = 0.0
    return {
        "trigger": _round(trigger),
        "zoneLow": _round(min(zone_low, zone_high)) if zone_low and zone_high else None,
        "zoneHigh": _round(max(zone_low, zone_high)) if zone_low and zone_high else None,
        "type": "NEXT_SESSION_BREAKOUT" if trigger else "WAIT_STRUCTURE",
        "structure": plan.get("setupType") or plan.get("type"),
        "entryLimit": _round(entry_limit),
        "lastClose": _round(latest_close),
    }


def _stop_loss(plan: dict[str, Any], levels: dict[str, Any], *, direction: str) -> float:
    if direction == "OBSERVE":
        return 0.0
    return _number(levels.get("activeDefense")) or _number(plan.get("activeStop")) or _number(plan.get("initialStop")) or _number(plan.get("structuralInvalidation"))


def _target_payloads(plan: dict[str, Any], levels: dict[str, Any], trigger: float, stop_loss: float, mode: str, *, direction: str) -> tuple[list[dict[str, Any]], float]:
    if direction not in {"LONG", "MANAGE_LONG"}:
        return [], 0.0
    trigger = _number(trigger)
    stop_loss = _number(stop_loss)
    first = _number(levels.get("firstTarget")) or _target_price(plan.get("firstTarget"))
    extension = _number(levels.get("extensionTarget")) or _target_price(plan.get("extensionTarget"))
    risk = trigger - stop_loss if trigger > stop_loss > 0 else 0.0
    risk_reward = (first - trigger) / risk if first > trigger and risk > 0 else _number(plan.get("roomR"))
    targets: list[dict[str, Any]] = []
    if first > 0:
        targets.append({
            "role": "FIRST_TARGET",
            "label": "第一目标",
            "price": _round(first),
            "rMultiple": _round(risk_reward),
            "source": levels.get("firstTargetSource") or _target_source(plan.get("firstTarget")) or "日线上方结构磁铁",
            "timeframe": "1d",
            "cumulativeRatio": 0.75,
            "legRatio": 0.75,
            "semantics": "第一目标是可能测试/停顿区；到达后按纪律分批止盈，不保证成交。",
        })
    if extension > first > 0 and mode == "TREND":
        targets.append({
            "role": "EXTENSION_TARGET",
            "label": "第二目标",
            "price": _round(extension),
            "rMultiple": _round((extension - trigger) / risk) if risk > 0 else None,
            "source": levels.get("extensionTargetSource") or _target_source(plan.get("extensionTarget")) or "日线趋势延伸磁铁",
            "timeframe": "1d",
            "cumulativeRatio": 1.0,
            "legRatio": 0.25,
            "semantics": "仅在第一目标后维持趋势与新的结构确认时继续管理。",
        })
    return targets, risk_reward


def _trailing_stop_payload(levels: dict[str, Any], *, scope: str, has_position: bool) -> dict[str, Any]:
    """Expose the A-share holding trail with the same one-way contract as Binance."""

    applicable = scope == "DISCIPLINE" and has_position
    active = applicable and bool(levels.get("movingStopActive"))
    stage = str(levels.get("movingStopStage") or ("ATR_TRAILING" if active else "INITIAL")).upper()
    state_label = {
        "INITIAL": "等待启用",
        "BREAKEVEN": "保本保护",
        "ATR_TRAILING": "ATR跟踪",
    }.get(stage, "等待启用")
    return {
        "applicable": applicable,
        "active": active,
        "state": stage,
        "stateLabel": state_label,
        "stopPrice": _round(_number(levels.get("movingStop"))),
        "candidatePrice": _round(_number(levels.get("movingStopCandidate"))),
        "peakPrice": _round(_number(levels.get("movingStopPeak") or levels.get("recentHigh20"))),
        "peakAt": levels.get("movingStopPeakDate"),
        "activationPrice": _round(_number(levels.get("movingStopActivationPrice"))),
        "activationAt": levels.get("movingStopActivationDate"),
        "atrMultiplier": _round(_number(levels.get("movingStopAtr"))) or 2.5,
        "activationR": _round(_number(levels.get("movingStopActivationR"))) or 1.0,
        "reason": levels.get("movingStopReason") or "持仓移动止损会在完整日K达到成本 + 1R 后启用。",
        "monotonic": True,
        "execution": "NEXT_SESSION_SELL_ONLY",
    }


def _condition_checks(**context: Any) -> list[dict[str, Any]]:
    scope = context["scope"]
    direction = context["direction"]
    environment = context["environment"]
    weekly = context["weekly"]
    selected = context["selected"]
    setup = context["setup"]
    metrics = context["metrics"]
    volume = context["volume"]
    latest = context["latest"]
    entry = context["entry"]
    stop_loss = context["stop_loss"]
    targets = context["targets"]
    risk_reward = context["risk_reward"]
    minimum_target_r = context["minimum_target_r"]
    timing = context["timing"]
    tradeability = context["tradeability"]
    action = context["action"]
    has_position = context["has_position"]
    environment_state = str(environment.get("state") or "DATA_INSUFFICIENT")
    bullish_weekly = weekly["bias"] != "BEAR"
    daily_ok = environment_state in {"BULL_TREND", "RANGE", "TRANSITION"}
    if direction == "EXIT_LONG":
        daily_ok = True
    if scope == "INDEX":
        daily_ok = environment_state != "DATA_INSUFFICIENT"
    facts = latest or {}
    completed = bool(facts) and facts.get("completed") is not False
    confirmed = bool(selected) and str(selected.get("status") or "") == "CONFIRMED" and not bool(setup.get("reviewRequired") or selected.get("reviewRequired"))
    if setup:
        confirmed = confirmed or (
            str(setup.get("status") or "") == "CONFIRMED"
            and not bool(setup.get("reviewRequired"))
        )
    if direction == "EXIT_LONG":
        confirmed = action in {"SELL", "REDUCE"} or confirmed
    if scope == "INDEX":
        # Index analysis is an exposure gate, not a synthetic stock order.
        # A closed, analysable daily environment is its confirmation event.
        confirmed = environment_state != "DATA_INSUFFICIENT"
    terminal = _terminal_extension(metrics, setup)
    fresh = True if scope == "INDEX" else not terminal
    trigger_pending = timing["state"] in {"WAIT_TRIGGER", "READY", "NEXT_SESSION_SELL", "MANAGING", "EXPOSURE_ONLY"}
    if direction in {"EXIT_LONG", "MANAGE_LONG"}:
        stop_ok = stop_loss > 0
        target_ok = True
    elif scope == "INDEX":
        stop_ok = True
        target_ok = True
    else:
        stop_ok = _number(entry.get("trigger")) > _number(stop_loss) > 0
        target_ok = bool(targets) and risk_reward >= minimum_target_r
    relative_volume = _number(volume.get("relativeVolume")) or _number(metrics.get("volumeRatio"))
    volume_state = str(volume.get("state") or "").upper()
    volume_ok = volume_state not in {"CLIMAX", "CLIMAX_CANDIDATE", "UNKNOWN", "DATA_INSUFFICIENT"} and (relative_volume <= 0 or relative_volume >= 0.5)
    if scope == "INDEX":
        volume_ok = True
    return [
        _check("weeklyContext", "周线背景未出现强反向否决", bullish_weekly, weekly["reason"], "周线已转为空头背景，先降低风险暴露。"),
        _check("dailyDirection", "日线环境与策略方向一致", daily_ok, f"日线环境为 {environment.get('label') or environment_state}。", "日线环境尚不支持新的多头风险暴露。"),
        _check("completedBar", "最近日K已收盘", completed, "只使用最近一根完整日K。", "最近日K未收盘或数据不足，不能确认计划。"),
        _check("signalBar", "日线结构已确认", confirmed, "确认结构及其触发价来自价格行为契约。", "等待完整日K确认结构，候选形态不能直接交易。"),
        _check("freshPullback", "位置新鲜且不在趋势末端追价", fresh, "没有检测到末端延伸警示。", "价格远离20EMA或出现末端推进，等待新的回撤/回测。"),
        _check("triggerPending", "下一交易日仍有可执行触发", trigger_pending, timing["reason"], "当前触发已失效或不存在，重新读取收盘结构后再评估。"),
        _check("structuralStop", "结构止损位于触发区另一侧", stop_ok, "止损来自已确认日线结构或持仓动态防守。", "没有位于结构另一侧的有效止损，不能为了下单而硬设止损。"),
        _check("targetSpace", f"第一目标空间不少于 {minimum_target_r:g}R", target_ok, f"第一目标空间约 {risk_reward:.2f}R。" if risk_reward else "持仓/指数管理不以新仓目标空间为限制。", f"第一目标方向或空间不足 {minimum_target_r:g}R，不追价。"),
        _check("relativeVolume", "量能未出现高潮或缺失", volume_ok, f"相对成交量约 {relative_volume:.2f}x。" if relative_volume else "指数与现有持仓不以相对量能单独否决。", "量能偏弱、高潮或缺失，触发前需要复核。"),
        _check("aShareExecution", "A股执行约束允许本次计划", tradeability["passed"], tradeability["reason"], tradeability["missing"]),
    ]


def _plan_status(*, scope: str, direction: str, action: str, condition_met: int, condition_total: int, missing_ids: set[str], hard_blocked: bool, terminal_extension: bool) -> tuple[str, bool]:
    if scope == "INDEX":
        return ("BLOCKED" if hard_blocked else "WATCH"), False
    if direction == "MANAGE_LONG" and action not in {"SELL", "REDUCE"}:
        return "WATCH", False
    if hard_blocked or terminal_extension:
        return "BLOCKED", False
    if condition_met == condition_total:
        return "ARMED", False
    trial = condition_met == condition_total - 1 and missing_ids.issubset(SOFT_TRIAL_CONDITIONS)
    return ("TRIAL", True) if trial else ("WATCH", False)


def _a_share_tradeability(latest: dict[str, Any], *, discipline_plan: dict[str, Any], direction: str, has_position: bool) -> dict[str, Any]:
    if direction == "EXPOSURE":
        return {"passed": True, "reason": "指数只输出风险暴露建议，不生成交易订单。", "missing": ""}
    if not latest:
        return {"passed": False, "reason": "缺少最近日K。", "missing": "补足日K与成交数据后再生成下一交易日计划。"}
    volume_ok = _number(latest.get("volume")) > 0
    pct_change = abs(_number(latest.get("pctChange")))
    limit_locked = pct_change >= 9.8
    position = dict(discipline_plan.get("position") or {})
    created_date = _date_key(position.get("createdAt"))
    latest_date = _date_key(latest.get("date"))
    t_plus_one_locked = direction == "EXIT_LONG" and bool(created_date and latest_date and created_date == latest_date)
    if has_position and direction == "EXIT_LONG" and t_plus_one_locked:
        return {"passed": False, "reason": "持仓当日买入，T+1 不允许卖出。", "missing": "等待下一交易日后再执行减仓或卖出计划。"}
    if limit_locked or not volume_ok:
        return {"passed": False, "reason": "涨跌停或无成交量时无法假设按计划成交。", "missing": "等待可成交的下一交易日，并在收盘后重新确认结构。"}
    order = dict(discipline_plan.get("order") or {})
    if discipline_plan and direction == "LONG" and int(_number(order.get("shares"))) <= 0:
        return {"passed": False, "reason": "纪律账户的现金、风险预算或100股整手限制尚未通过。", "missing": "补足可买一手的现金和风险预算后再评估。"}
    return {"passed": True, "reason": "现货只做多；计划在下一交易日按T+1、涨跌停、流动性和整手规则执行。", "missing": ""}


def _entry_timing(*, direction: str, entry: dict[str, Any], latest_close: float, action: str, has_position: bool, tradeability: dict[str, Any]) -> dict[str, Any]:
    if direction == "EXPOSURE":
        return {"state": "EXPOSURE_ONLY", "setup": "指数风险暴露", "reason": "指数策略只用于决定仓位暴露，不假装生成可交易买卖单。", "referencePrice": None}
    if direction == "EXIT_LONG":
        return {"state": "NEXT_SESSION_SELL" if tradeability["passed"] else "BLOCKED", "setup": "已有持仓防守", "reason": "卖出/减仓只能在下一交易日，且受T+1与涨跌停影响。", "referencePrice": _round(entry["trigger"])}
    if has_position:
        return {"state": "MANAGING", "setup": "已有持仓管理", "reason": "先按动态防守和第一目标管理，新增仓位需重新满足完整买入条件。", "referencePrice": _round(entry["trigger"])}
    trigger = _number(entry.get("trigger"))
    if trigger <= 0:
        return {"state": "WAIT_STRUCTURE", "setup": "等待结构", "reason": "尚未形成可审计的日线触发价。", "referencePrice": None}
    if latest_close >= trigger:
        return {"state": "REASSESS", "setup": "触发已经过", "reason": "收盘已触及或越过旧触发位，不能追用旧计划；等待新的完整日线结构。", "referencePrice": _round(trigger)}
    return {"state": "WAIT_TRIGGER", "setup": "下一交易日突破确认", "reason": "价格仍在触发价外侧，下一交易日仅在触发后再确认成交与风险。", "referencePrice": _round(trigger)}


def _weekly_reading(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [_number(item.get("close")) for item in bars if _number(item.get("close")) > 0]
    weeks = [closes[index:index + 5] for index in range(0, len(closes), 5)]
    weekly_closes = [group[-1] for group in weeks if group]
    if len(weekly_closes) < 8:
        return {"state": "DATA_INSUFFICIENT", "bias": "NEUTRAL", "close": _round(closes[-1] if closes else 0), "ema20": None, "evidence": ["周线样本不足，只作为中性背景。"], "reason": "周线样本不足，暂不把它作为方向确认。"}
    ema = _ema(weekly_closes, min(20, len(weekly_closes)))
    close = weekly_closes[-1]
    slope = ema[-1] - ema[max(0, len(ema) - min(4, len(ema)))]
    if close > ema[-1] and slope >= 0:
        state, bias = "BULL_TREND", "BULL"
    elif close < ema[-1] and slope <= 0:
        state, bias = "BEAR_TREND", "BEAR"
    else:
        state, bias = "RANGE", "NEUTRAL"
    return {"state": state, "bias": bias, "close": _round(close), "ema20": _round(ema[-1]), "evidence": [f"周线收盘 {close:.2f}，EMA背景 {ema[-1]:.2f}。"], "reason": f"周线为{ {'BULL': '多头', 'BEAR': '空头', 'NEUTRAL': '中性'}[bias] }背景。"}


def _daily_timeframe(environment: dict[str, Any], metrics: dict[str, Any], close: float, volume: dict[str, Any]) -> dict[str, Any]:
    state = str(environment.get("state") or "DATA_INSUFFICIENT")
    bias = "BULL" if state == "BULL_TREND" else "BEAR" if state == "BEAR_TREND" else "NEUTRAL"
    return {"state": state, "bias": bias, "close": _round(close), "ema20": _round(_number(metrics.get("ema20"))), "atr": _round(_number(metrics.get("atr14"))), "evidence": list(environment.get("evidence") or [])[:3] + ([f"相对成交量约 {_number(volume.get('relativeVolume')):.2f}x。"] if _number(volume.get("relativeVolume")) else [])}


def _cancellation_conditions(*, selected: dict[str, Any], direction: str, entry: dict[str, Any], stop_loss: float, scope: str, has_position: bool) -> list[str]:
    existing = [str(item) for item in selected.get("cancellationConditions") or [] if item]
    if existing:
        return existing
    if scope == "INDEX":
        return ["周线或日线转为空头背景时，下调新增风险暴露。", "指数只做仓位环境参考，不替代个股结构。"]
    if direction == "EXIT_LONG" or has_position:
        return [f"下一交易日日线收盘跌破防守位 {_format_price(stop_loss)}，按T+1和可成交约束执行卖出。", "若价格重新站回防守结构内部，重新评估，不在盘中凭成本价反复操作。"]
    return [f"下一交易日日线收盘跌破结构防守 {_format_price(stop_loss)}，取消本次买入。", f"触发后未能守住触发区 {_format_price(entry.get('zoneLow'))} - {_format_price(entry.get('zoneHigh'))}，重新评估。", "周线或日线出现确认的反向结构、涨跌停或流动性失效时，取消计划。"]


def _execution_action(scope: str, direction: str, action: str, has_position: bool) -> str:
    if scope == "INDEX":
        return "EXPOSURE_GUIDANCE"
    if direction == "EXIT_LONG" or (has_position and action in {"SELL", "REDUCE"}):
        return "SELL_EXISTING_LONG"
    return "BUY_LONG" if direction == "LONG" else "OBSERVE"


def _execution_constraint(action: str) -> str:
    return {"BUY_LONG": "CASH_LONG_ONLY", "SELL_EXISTING_LONG": "EXISTING_LONG_ONLY", "EXPOSURE_GUIDANCE": "INDEX_EXPOSURE_ONLY"}.get(action, "NO_NEW_SHORT")


def _blocked_reasons(missing_ids: set[str], *, stale: bool, terminal_extension: bool, tradeability: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if stale:
        reasons.append("data_incomplete")
    if "structuralStop" in missing_ids:
        reasons.append("stop_unknown")
    if "targetSpace" in missing_ids:
        reasons.append("no_room")
    if terminal_extension:
        reasons.append("late_trend_extension")
    if not tradeability["passed"]:
        reasons.append("a_share_execution_constraint")
    return reasons


def _terminal_extension(metrics: dict[str, Any], setup: dict[str, Any]) -> bool:
    return abs(_number(metrics.get("extensionAtr"))) > 4.0 or str(setup.get("type") or "") in {"WEDGE_THIRD_PUSH", "CLIMAX_SPIKE_REVERSAL", "CLIMAX_REVERSAL_CANDIDATE"}


def _check(identifier: str, label: str, passed: bool, reason: str, missing: str) -> dict[str, Any]:
    return {"id": identifier, "label": label, "passed": bool(passed), "reason": reason, "missing": missing}


def _clean_bars(raw_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in raw_bars if isinstance(item, dict) and _number(item.get("close")) > 0]


def _target_price(value: Any) -> float:
    return _number(value.get("price")) if isinstance(value, dict) else _number(value)


def _target_source(value: Any) -> str | None:
    return str(value.get("source") or "") if isinstance(value, dict) else None


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if number == number else 0.0
    except (TypeError, ValueError):
        return 0.0


def _round(value: Any) -> float | None:
    number = _number(value)
    return round(number, 4) if number > 0 else None


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * multiplier + output[-1] * (1 - multiplier))
    return output


def _date_key(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())[:8]


def _format_price(value: Any) -> str:
    rounded = _round(value)
    return f"{rounded:.2f}" if rounded is not None else "--"
