"""Completed-daily-bar price-action plans for the discipline trading workspace."""

from __future__ import annotations

from typing import Any

from .price_action import MIN_ANALYSIS_BARS, analyze_price_action, normalize_bars
from .price_action_contract import SOURCE_POLICY_ID, SOURCE_POLICY_VERSION, build_bar_facts, update_dynamic_stop
from .a_share_strategy import attach_a_share_strategy_plan


DEFAULT_RULES = {
    "accountValue": 100_000.0,
    "maxPositionPct": 0.20,
    "riskPerTradePct": 0.01,
    "hardStopPct": 0.08,
    "initialStopAtr": 2.0,
    "structureStopBufferAtr": 0.2,
    "movingStopLookback": 20,
    "movingStopAtr": 2.5,
    "movingStopBreakevenBufferAtr": 0.05,
    "minimumRewardRisk": 1.5,
    # Partial-reduction sizing is an execution-profile value.  The PDFs define
    # when a strong counter-impulse permits reducing exposure, but do not fix a
    # universal percentage for every account.
    "reductionFraction": 0.50,
    "takeProfitAtrFallback": 1.5,
    "lotSize": 100,
    "maxDailyLossPct": 0.02,
    "maxConcentrationPct": 0.20,
    "slippageBps": 12,
    "feeBps": 10,
    "tPlusOne": True,
}


_UPSIDE_TARGET_ROLES = {
    "RESISTANCE",
    "RESISTANCE_MAGNET",
    "RANGE_MIDDLE_MAGNET",
    "UPSIDE_MAGNET",
}


def evaluate_discipline_strategy(
    history: list[dict[str, Any]],
    account_value: float | int | None = None,
    position: dict[str, Any] | None = None,
    index_history: list[dict[str, Any]] | None = None,
    available_cash: float | int | None = None,
) -> dict[str, Any]:
    """Create a next-session plan without placing, scheduling, or simulating an order."""

    bars = normalize_bars(history)
    account = _positive_number(account_value, DEFAULT_RULES["accountValue"])
    rules = {
        **DEFAULT_RULES,
        "accountValue": account,
        "availableCash": _non_negative_number(available_cash, account),
    }
    existing = _normalize_position(position)
    if len(bars) < MIN_ANALYSIS_BARS:
        return attach_a_share_strategy_plan(
            _insufficient_history_plan(bars, rules, existing),
            bars,
            scope="DISCIPLINE",
        )

    stock_result = analyze_price_action(history, timeframe="1d")
    stock_pa = stock_result["priceAction"]
    index_result = analyze_price_action(index_history or [], timeframe="1d")
    market = _market_context(index_result["priceAction"])
    latest = bars[-1]
    close = float(latest["close"])
    metrics = dict(stock_pa.get("metrics") or {})
    metrics["latestClose"] = _round_price(close)
    plan_context = _plan_context(stock_pa)

    if existing:
        return attach_a_share_strategy_plan(_position_plan(
            bars=bars,
            latest=latest,
            close=close,
            stock_pa=stock_pa,
            metrics=metrics,
            market=market,
            position=existing,
            rules=rules,
            plan_context=plan_context,
        ), bars, scope="DISCIPLINE")
    return attach_a_share_strategy_plan(_entry_plan(
        latest=latest,
        close=close,
        stock_pa=stock_pa,
        metrics=metrics,
        market=market,
        rules=rules,
        plan_context=plan_context,
    ), bars, scope="DISCIPLINE")


def _entry_plan(
    *,
    latest: dict[str, Any],
    close: float,
    stock_pa: dict[str, Any],
    metrics: dict[str, Any],
    market: dict[str, Any],
    rules: dict[str, Any],
    plan_context: dict[str, Any],
) -> dict[str, Any]:
    environment = stock_pa["environment"]
    signal = _latest_signal(stock_pa, "BUY")
    blocked: list[str] = []
    checks = [
        _check("完整日K", environment["state"] != "DATA_INSUFFICIENT", "仅使用最近一根已完成日K。"),
        _check("市场环境", market["allowNewPosition"], market["detail"]),
        _check("位置与结构", bool(signal) and _eligible_long_setup(signal), "需是确认的顺势回撤、突破回踩、区间下沿反转或失败下破。"),
        _check("确认与复核", bool(signal) and not signal.get("reviewRequired"), "必须已有确认，不能以候选或需复核形态开仓。"),
        _check("不过度延伸", abs(float(metrics.get("extensionAtr") or 0)) <= 4.0 and not _has_wedge_warning(stock_pa), "不在远离20EMA、第三次推动或高潮候选处追价。"),
        _check("量能质量", (stock_pa.get("volumeTurnover") or {}).get("state") not in {"CLIMAX_CANDIDATE"}, "量能仅作结构证据；高潮量和异常单柱需要复核。"),
        _check("涨跌停与流动性", abs(_number(latest.get("pctChange"))) < 9.8 and float(latest.get("volume") or 0) > 0, "涨跌停或零成交量时不生成新仓订单。"),
    ]
    if not signal:
        blocked.append("没有已确认的完整做多价格行为计划")
    if environment["state"] not in {"BULL_TREND", "RANGE", "TRANSITION"}:
        blocked.append("当前日线不是可做多的趋势或区间环境")

    if signal:
        entry = _number(signal.get("entryLimit"))
        stop = _number(signal.get("protectiveStop") or signal.get("invalidationPrice"))
        target = _number(signal.get("firstTarget"))
        structure_distance = (entry - stop) / entry if entry > 0 else 1.0
        reward_risk = (target - entry) / max(entry - stop, 0.001) if target > entry > stop else 0.0
        checks.extend(
            [
                _check("结构失效价", entry > stop > 0, "止损必须位于信号、回踩或结构失效的一侧。"),
                _check("硬风险边界", 0 < structure_distance <= rules["hardStopPct"], f"结构止损距离 {structure_distance * 100:.2f}%，硬上限 {rules['hardStopPct'] * 100:.2f}%。"),
                _check("磁铁风险回报", reward_risk >= rules["minimumRewardRisk"], f"第一磁铁风险回报 {reward_risk:.2f}，最低要求 {rules['minimumRewardRisk']:.2f}。"),
            ]
        )
        sizing = _position_size(entry, stop, rules, market["positionCapMultiplier"])
        checks.append(_check("现金与整手", sizing["shares"] > 0, "数量同时受单笔风险、20%仓位、现金和100股整手限制。"))
        if structure_distance > rules["hardStopPct"]:
            blocked.append("结构止损超过硬风险边界，不能人为收紧止损")
        if reward_risk < rules["minimumRewardRisk"]:
            blocked.append("第一磁铁无法覆盖最低风险回报")
        if sizing["shares"] <= 0:
            blocked.append("账户风险预算或可用现金不足以买入一手")
    else:
        entry = stop = target = reward_risk = 0.0
        sizing = {"shares": 0, "estimatedValue": 0.0, "riskAmount": 0.0}

    for check in checks:
        if not check["passed"]:
            blocked.append(check["name"])
    blocked = _unique(blocked)
    if not blocked:
        levels = _levels_from_signal(signal, close, rules)
        return _base_plan(
            action="BUY",
            action_label="条件计划买入",
            tone="positive",
            latest=latest,
            metrics=metrics,
            market=market,
            price_action=plan_context,
            checks=checks,
            reasons=list(signal["reasons"]),
            blocked_reasons=[],
            position=None,
            order={
                "when": f"下一交易日仅在触发 {entry:.2f} 后，成交价不高于 {entry:.2f} 时执行。",
                "priceRule": f"计划入场上限 {entry:.2f}",
                "shares": sizing["shares"],
                "estimatedValue": sizing["estimatedValue"],
                "stopPrice": stop,
                "riskAmount": sizing["riskAmount"],
                "actionHint": "不追高，不补仓，不把成本价当作技术支撑。",
            },
            levels=levels,
            rules=rules,
        )

    levels = _levels_from_signal(signal, close, rules) if signal else _empty_levels(close, rules)
    reasons = list(stock_pa["assessment"].get("reasons") or [])
    return _base_plan(
        action="WAIT",
        action_label="等待条件完整",
        tone="neutral",
        latest=latest,
        metrics=metrics,
        market=market,
        price_action=plan_context,
        checks=checks,
        reasons=reasons or ["没有满足环境、位置、结构、确认和风险计划的完整新仓条件。"],
        blocked_reasons=blocked,
        position=None,
        order={
            "when": "等待下一根完整日K后重新评估。",
            "priceRule": "没有有效买入订单",
            "shares": 0,
            "estimatedValue": 0,
            "stopPrice": levels.get("activeDefense"),
            "riskAmount": 0,
            "actionHint": "条件不完整时不交易。",
        },
        levels=levels,
        rules=rules,
    )


def _position_plan(
    *,
    bars: list[dict[str, float | str]],
    latest: dict[str, Any],
    close: float,
    stock_pa: dict[str, Any],
    metrics: dict[str, Any],
    market: dict[str, Any],
    position: dict[str, Any],
    rules: dict[str, Any],
    plan_context: dict[str, Any],
) -> dict[str, Any]:
    sell_signal = _latest_signal(stock_pa, "SELL")
    long_signal = _latest_signal(stock_pa, "BUY")
    sell_plan = _latest_future_plan(stock_pa, "SELL")
    long_plan = _latest_future_plan(stock_pa, "BUY")
    confirmed_sell_plan = _latest_future_plan(stock_pa, "SELL", confirmed_only=True)
    confirmed_buy_plan = _latest_future_plan(stock_pa, "BUY", confirmed_only=True)
    # A pending bearish pattern is a future observation anchor, not an active
    # defense instruction. It must not erase a confirmed long plan or make the
    # holding card look as if its entry point disappeared.
    defense_plan = sell_signal or confirmed_sell_plan
    upside_plan = long_signal or confirmed_buy_plan or long_plan
    defense_observation = None if defense_plan else sell_plan
    cost_price = position["costPrice"]
    cost_stop = cost_price * (1 - rules["hardStopPct"])
    atr = _number(metrics.get("atr14"))
    structural_invalidation = _number((long_signal or {}).get("invalidationPrice"))
    structural_reference = _nearest_support(stock_pa.get("levels") or [], close)
    pending_invalidation = _number((long_plan or {}).get("invalidationPrice"))
    structure_boundary = (
        structural_invalidation
        or pending_invalidation
        or structural_reference
    )
    initial_stop, initial_stop_source, initial_stop_reason = _initial_position_stop(
        bars=bars,
        close=close,
        cost_price=cost_price,
        cost_stop=cost_stop,
        atr=atr,
        structural_invalidation=structural_invalidation,
        structural_plan=long_plan,
        structural_reference=structural_reference,
        rules=rules,
    )
    moving_state = _moving_stop_state(
        bars=bars,
        position=position,
        cost_price=cost_price,
        initial_stop=initial_stop,
        atr=atr,
        close=close,
        rules=rules,
    )
    recent_high = moving_state["recentHigh"]
    moving_candidate = moving_state["movingCandidate"]
    moving_stop = moving_state["movingStop"]
    contract_candidates = [
        item for item in stock_pa.get("tradePlans", [])
        # The contract's SELL direction is an existing-long defense
        # observation, never a short-position trailing-stop plan. Dynamic
        # contract stop management therefore only consumes long plans here;
        # live holding defense is calculated by _position_levels below.
        if item.get("direction") == "BUY" and item.get("setupId") and item.get("executionConstraint") != "EXISTING_LONG_ONLY"
    ]
    contract_plan = contract_candidates[0] if contract_candidates else None
    contract_events: list[dict[str, Any]] = []
    if contract_plan:
        managed_contract_plan = update_dynamic_stop(contract_plan, build_bar_facts(bars, timeframe="1d"), position)
        contract_events = list(managed_contract_plan.get("stopEvents") or [])
        plan_context["activeTradePlan"] = managed_contract_plan
        plan_context["dynamicStop"] = {
            "state": managed_contract_plan.get("stopState", "initial"),
            "initial": managed_contract_plan.get("initialStop"),
            "active": managed_contract_plan.get("activeStop"),
            "events": contract_events,
        }
    # The technical stop is structural first. The cost stop remains a separate
    # account-risk emergency boundary and is checked independently below; it
    # must not silently move a valid structure stop closer to the entry price.
    defense_candidates = [initial_stop]
    if moving_stop is not None:
        defense_candidates.append(moving_stop)
    active_defense = max(defense_candidates)
    risk_budget_exceeded = active_defense < cost_stop - max(cost_price * 0.0005, 0.001)
    levels = _position_levels(
        defense_plan=defense_plan,
        upside_plan=upside_plan,
        close=close,
        cost_price=cost_price,
        structural_invalidation=structural_invalidation,
        structural_reference=structural_reference,
        active_defense=active_defense,
        hard_stop=initial_stop,
        cost_stop=cost_stop,
        moving_stop=moving_stop,
        recent_high=recent_high,
        rules=rules,
        moving_stop_active=moving_state["active"],
        moving_stop_activation_price=moving_state["activationPrice"],
        moving_stop_activation_date=moving_state["activationDate"],
        moving_stop_reason=moving_state["reason"],
        moving_stop_candidate=moving_candidate,
        moving_stop_stage=moving_state["stage"],
        moving_stop_peak=moving_state["peakPrice"],
        moving_stop_peak_date=moving_state["peakDate"],
        structure_boundary=structure_boundary,
        initial_stop_source=initial_stop_source,
        initial_stop_reason=initial_stop_reason,
        risk_budget_exceeded=risk_budget_exceeded,
        defense_observation=defense_observation,
        price_action_levels=stock_pa.get("levels") or [],
        atr=atr,
    )
    levels["cancellationConditions"] = list((defense_plan or {}).get("cancellationConditions") or [
        f"若后续完整日K跌破当前防守线 {active_defense:.2f}，取消持有判断并重新评估卖出。"
    ])
    # This quantity is an explicit next-session contingency, not an order. It
    # lets the plan state what to do at the first upside magnet even when the
    # latest completed bar has not reached it yet.
    levels["takeProfitReductionShares"] = _reduction_shares(
        position=position,
        active_defense=active_defense,
        rules=rules,
    )
    levels["dynamicStopState"] = plan_context.get("dynamicStop") or {"state": "initial", "events": []}
    levels["stopMoveEvents"] = contract_events
    levels["movingStopAtr"] = rules["movingStopAtr"]
    levels["movingStopActivationR"] = 1.0
    levels["movingStopBreakevenBufferAtr"] = rules["movingStopBreakevenBufferAtr"]
    unrealized_pct = ((close - position["costPrice"]) / position["costPrice"]) * 100
    enriched_position = {**position, "unrealizedPct": round(unrealized_pct, 2)}
    hard_exit = close <= cost_stop
    initial_exit = close <= initial_stop
    moving_exit = bool(moving_stop) and close <= moving_stop
    structural_exit = bool(structural_invalidation) and close <= structural_invalidation
    confirmed_defense = bool(sell_signal or confirmed_sell_plan) or market["status"] == "defensive"
    target_touch_evidence = _target_touch_evidence(
        close=close,
        upside_plan=upside_plan,
        long_signal=long_signal,
    )
    reduction_evidence = target_touch_evidence or _confirmed_reduction_evidence(
        bars=bars,
        metrics=metrics,
        sell_signal=sell_signal,
        confirmed_sell_plan=confirmed_sell_plan,
        market=market,
        active_defense=active_defense,
    )
    moving_check_detail = (
        f"移动止损 {moving_stop:.2f}；当前收盘 {close:.2f}。"
        if moving_stop is not None
        else moving_state["reason"]
    )
    common_checks = [
        _check("账户风险退出线", not hard_exit, f"账户风险退出线 {cost_stop:.2f}；当前收盘 {close:.2f}。"),
        _check("结构初始止损", not initial_exit, f"结构初始止损 {initial_stop:.2f}；当前收盘 {close:.2f}。"),
        _check("移动止损", not moving_exit, moving_check_detail),
        _check(
            "账户风险距离",
            True,
            (
                f"结构止损 {active_defense:.2f} 低于成本风险上限 {cost_stop:.2f}；"
                "不把止损上移到结构内部，已有仓位应通过减仓控制风险。"
                if risk_budget_exceeded
                else f"当前结构止损距离未超过成本风险上限 {cost_stop:.2f}。"
            ),
        ),
        _check(
            "结构防守",
            not structural_exit,
            f"结构失效位 {structural_invalidation:.2f}；结构参考位不替代当前有效止损。"
            if structural_invalidation
            else "没有可执行的已确认做多结构失效位，仅使用成本风险线和移动止损。",
        ),
        _check("市场暴露", market["status"] != "defensive", market["detail"]),
    ]

    if hard_exit or initial_exit or moving_exit or structural_exit:
        if hard_exit:
            reason = (
                f"当前收盘已触及账户风险退出线 {cost_stop:.2f}；"
                f"结构技术防守位为 {active_defense:.2f}，不把技术止损上移到结构内部。"
            )
        elif moving_exit:
            reason = f"当前收盘已跌破移动止损 {moving_stop:.2f}，保护已形成的价格空间。"
        elif structural_exit:
            reason = f"当前收盘已跌破已确认做多结构失效位 {structural_invalidation:.2f}。"
        else:
            reason = f"当前收盘已触及初始止损 {initial_stop:.2f}。"
        return _base_plan(
            action="SELL",
            action_label="计划卖出",
            tone="negative",
            latest=latest,
            metrics=metrics,
            market=market,
            price_action=plan_context,
            checks=common_checks,
            reasons=[reason, "保护账户优先于预测；不等待盘中反弹确认。"],
            blocked_reasons=[],
            position=enriched_position,
            order={
                "when": "下一交易日按可成交价格执行；跳空和涨跌停可能使计划价无法成交。",
                "priceRule": (
                    f"日线收盘触及账户风险退出线 {cost_stop:.2f}"
                    if hard_exit
                    else f"日线收盘失守技术防守线 {active_defense:.2f}"
                ),
                "shares": position["shares"],
                "estimatedValue": _round_money(position["shares"] * close),
                "stopPrice": cost_stop if hard_exit else active_defense,
                "riskAmount": _round_money(max(0, position["costPrice"] - close) * position["shares"]),
                "actionHint": "卖出后不因成本价或盘中反弹立即追回。",
            },
            levels=levels,
            rules=rules,
        )

    if reduction_evidence:
        reduce_shares = _reduction_shares(
            position=position,
            active_defense=active_defense,
            rules=rules,
        )
        full_exit = reduce_shares >= position["shares"]
        levels.update(
            {
                "reductionShares": 0 if full_exit else reduce_shares,
                "reductionTriggerPrice": _round_price(
                    reduction_evidence.get("triggerPrice") or active_defense
                ),
                "reductionReason": reduction_evidence["reason"],
                "reductionStatus": "FULL_EXIT" if full_exit else "CONFIRMED",
            }
        )
        plan_context["reductionEvidence"] = reduction_evidence
        return _base_plan(
            action="SELL" if full_exit else "REDUCE",
            action_label="计划卖出" if full_exit else "部分减仓",
            tone="negative" if full_exit else "warning",
            latest=latest,
            metrics=metrics,
            market=market,
            price_action=plan_context,
            checks=common_checks,
            reasons=[
                reduction_evidence["reason"],
                (
                    f"当前持仓仅剩一手或不可再分的余量；下一交易日按确认事件卖出全部 {position['shares']} 股。"
                    if full_exit
                    else f"下一交易日按确认事件减仓 {reduce_shares} 股；剩余仓位继续使用有效防守线 {active_defense:.2f}。"
                ),
            ],
            blocked_reasons=[],
            position=enriched_position,
            order={
                "when": "下一交易日按可成交价格执行卖出；若跳空、涨跌停或流动性不足，按实际可成交数量复核。",
                "priceRule": f"确认强反向结构/指数防守后，当前有效防守线 {active_defense:.2f} 仍未被触及",
                "shares": position["shares"] if full_exit else reduce_shares,
                "estimatedValue": _round_money((position["shares"] if full_exit else reduce_shares) * close),
                "stopPrice": active_defense,
                "riskAmount": _round_money(max(0, position["costPrice"] - active_defense) * (position["shares"] if full_exit else reduce_shares)),
                "actionHint": "卖出后不因盘中反弹追回。" if full_exit else "部分减仓后不因盘中反弹追回；若收盘继续跌破有效防守线，再按止损纪律卖出剩余仓位。",
            },
            levels=levels,
            rules=rules,
        )

    if confirmed_defense:
        reasons = list((defense_plan or {}).get("reasons") or [])
        if market["status"] == "defensive":
            reasons.append("指数价格行为进入防守环境，限制个股风险暴露。")
        reasons.insert(0, f"下一交易日条件：完整日K收盘跌破有效防守线 {active_defense:.2f} 并出现后续跟随时，再执行卖出。")
        return _base_plan(
            action="HOLD",
            action_label="防守观察",
            tone="warning",
            latest=latest,
            metrics=metrics,
            market=market,
            price_action=plan_context,
            checks=common_checks,
            reasons=reasons or ["已出现确认防守条件，保留部分仓位等待下一次完整日K复核。"],
            blocked_reasons=[],
            position=enriched_position,
            order={
                "when": f"下一交易日若完整日K收盘跌破有效防守线 {active_defense:.2f} 并出现后续跟随，卖出全部现有持仓。",
                "priceRule": f"等待收盘跌破有效防守线 {active_defense:.2f}",
                "shares": 0,
                "estimatedValue": 0,
                "stopPrice": active_defense,
                "riskAmount": 0,
                "actionHint": "形态预警不等于止损触发；未跌破有效防守线前保持观察。",
            },
            levels=levels,
            rules=rules,
        )

    warnings = []
    if stock_pa["assessment"].get("reviewRequired"):
        warnings.append("当前形态含需人工复核部分，暂停新增仓位。")
    if risk_budget_exceeded:
        warnings.append("当前结构止损距离超过账户风险退出线；不收紧技术止损，新增风险应暂停并优先减仓。")
    return _base_plan(
        action="HOLD",
        action_label="持有观察",
        tone="positive",
        latest=latest,
        metrics=metrics,
        market=market,
        price_action=plan_context,
        checks=common_checks,
        reasons=["持仓长线结构和防守线尚未失效。", *warnings, "成本价用于成本风险线和加仓门槛，不是技术支撑或继续持有理由。"],
        blocked_reasons=[],
        position=enriched_position,
        order={
            "when": "不新增订单，下一交易日收盘后重新评估。",
            "priceRule": f"持仓防守线 {active_defense:.2f}",
            "shares": 0,
            "estimatedValue": 0,
            "stopPrice": active_defense,
            "riskAmount": _round_money(max(0, position["costPrice"] - active_defense) * position["shares"]),
            "actionHint": "不因浮盈、浮亏或均摊成本临时修改计划。",
        },
        levels=levels,
        rules=rules,
    )


def _confirmed_reduction_evidence(
    *,
    bars: list[dict[str, float | str]],
    metrics: dict[str, Any],
    sell_signal: dict[str, Any] | None,
    confirmed_sell_plan: dict[str, Any] | None,
    market: dict[str, Any],
    active_defense: float,
) -> dict[str, Any] | None:
    """Return only a documented strong counter-event, never a plain warning.

    A single ordinary bearish bar remains a defense observation.  Reduction is
    reserved for a confirmed failed bullish breakout, upper-range failure,
    major bearish reversal, or a confirmed defensive index environment, and it
    is only considered while the completed close remains above the live stop.
    """

    close = _number(bars[-1].get("close")) if bars else 0.0
    if not close or close <= active_defense:
        return None
    if market.get("status") == "defensive":
        return {
            "type": "DEFENSIVE_INDEX_ENVIRONMENT",
            "reason": "指数价格行为已进入防守环境；按四本资料的风险优先纪律先降低已有仓位暴露。",
            "triggerPrice": close,
            "source": "指数环境确认",
        }

    candidate = sell_signal or confirmed_sell_plan
    if not candidate:
        return None
    if candidate.get("status") != "CONFIRMED" or candidate.get("reviewRequired"):
        return None
    setup_type = str(candidate.get("type") or candidate.get("setupType") or "")
    if setup_type not in {
        "FAILED_BULLISH_BREAKOUT",
        "RANGE_UPPER_REVERSAL",
        "MAJOR_BEARISH_REVERSAL",
        "BEAR_FLAG",
    }:
        return None
    latest = bars[-1]
    atr = max(_number(metrics.get("atr14")), close * 0.001)
    body = abs(_number(latest.get("close")) - _number(latest.get("open")))
    span = max(_number(latest.get("high")) - _number(latest.get("low")), 0.0001)
    close_location = (_number(latest.get("close")) - _number(latest.get("low"))) / span
    strong_counter_bar = (
        _number(latest.get("close")) < _number(latest.get("open"))
        and body / atr >= 0.55
        and close_location <= 0.42
    )
    previous = bars[-2] if len(bars) >= 2 else None
    previous_span = max(_number(previous.get("high")) - _number(previous.get("low")), 0.0001) if previous else 0.0001
    previous_body = abs(_number(previous.get("close")) - _number(previous.get("open"))) if previous else 0.0
    previous_close_location = (
        (_number(previous.get("close")) - _number(previous.get("low"))) / previous_span
        if previous else 1.0
    )
    follow_through = bool(
        previous
        and _number(previous.get("close")) < _number(previous.get("open"))
        and previous_body / atr >= 0.25
        and previous_close_location <= 0.55
    )
    if not strong_counter_bar or not follow_through:
        return None
    return {
        "type": setup_type,
        "reason": f"已确认{_setup_label(setup_type)}，出现强向下冲击并收盘靠近低位；按持仓纪律先部分减仓。",
        "triggerPrice": candidate.get("triggerPrice") or candidate.get("anchorPrice"),
        "source": "个股反向结构确认",
    }


def _target_touch_evidence(
    *,
    close: float,
    upside_plan: dict[str, Any] | None,
    long_signal: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Turn a previously confirmed upper magnet touch into partial profit-taking."""

    for candidate in (upside_plan, long_signal):
        if not candidate or candidate.get("status") not in {None, "CONFIRMED"} or candidate.get("reviewRequired"):
            continue
        target = _number(candidate.get("firstTarget"))
        if target > 0 and close >= target:
            return {
                "type": "TARGET_TOUCH",
                "reason": f"价格已触及已确认做多计划第一磁铁 {target:.2f}；按持仓纪律分批止盈并保留剩余仓位观察。",
                "triggerPrice": target,
                "source": "第一磁铁触及",
            }
    return None


def _setup_label(setup_type: str) -> str:
    return {
        "FAILED_BULLISH_BREAKOUT": "向上突破失败",
        "RANGE_UPPER_REVERSAL": "区间上沿反转",
        "MAJOR_BEARISH_REVERSAL": "主要向下反转",
        "BEAR_FLAG": "熊旗下沿确认",
    }.get(setup_type, "下行结构")


def _reduction_shares(
    *,
    position: dict[str, Any],
    active_defense: float,
    rules: dict[str, Any],
) -> int:
    """Size a partial sell without creating a short position.

    When the structure risk is oversized, reduce enough to approach the
    account's configured risk budget; otherwise use the explicit profile
    fraction.  A partial A-share sell is rounded to a board lot when the
    holding contains at least two lots; a one-lot position must use the full
    exit branch rather than advertising an untradeable fractional reduction.
    """

    shares = max(0, int(position.get("shares") or 0))
    if shares <= 0:
        return 0
    cost = _number(position.get("costPrice"))
    risk_per_share = max(cost - active_defense, 0.0)
    allowed_risk = _number(rules.get("accountValue")) * _number(rules.get("riskPerTradePct"))
    if risk_per_share > 0 and allowed_risk > 0 and shares * risk_per_share > allowed_risk:
        target_shares = int(allowed_risk / risk_per_share)
        reduction = shares - max(target_shares, 0)
    else:
        fraction = _number(rules.get("reductionFraction"))
        if fraction <= 0:
            fraction = 0.5
        reduction = int(round(shares * fraction))
    lot_size = max(1, int(_number(rules.get("lotSize")) or 100))
    if shares < lot_size:
        # A pre-existing odd-lot remainder can be sold as-is.
        return shares
    if shares < lot_size * 2:
        # One board lot cannot be partially reduced in a valid A-share order.
        return shares

    # Keep one tradable lot for REDUCE. Full exits are routed explicitly by
    # the caller when that is the only executable quantity.
    desired = max(lot_size, min(shares - lot_size, reduction))
    rounded = _round_lot(desired, lot_size)
    return max(lot_size, min(shares - lot_size, rounded))


def _market_context(index_pa: dict[str, Any]) -> dict[str, Any]:
    environment = index_pa.get("environment") or {}
    assessment = index_pa.get("assessment") or {}
    state = environment.get("state")
    if state == "DATA_INSUFFICIENT":
        return {"status": "unknown", "label": "指数数据不足", "detail": "指数完整日K不足，保守模式不新增仓位。", "allowNewPosition": False, "positionCapMultiplier": 0.0, "priceAction": environment}
    if assessment.get("action") == "SELL" or state == "BEAR_TREND":
        return {"status": "defensive", "label": "指数防守", "detail": "指数出现下行趋势或确认防守结构，暂停新增风险暴露，优先保护已有仓位。", "allowNewPosition": False, "positionCapMultiplier": 0.0, "priceAction": environment}
    if state == "BULL_TREND":
        return {"status": "approved", "label": "指数允许开仓", "detail": "指数处于可识别多头环境，仍需服从个股价格行为和账户风险。", "allowNewPosition": True, "positionCapMultiplier": 1.0, "priceAction": environment}
    if state == "RANGE":
        return {"status": "cautious", "label": "指数区间谨慎", "detail": "指数处于区间，仅允许边缘确认形态并限制仓位。", "allowNewPosition": True, "positionCapMultiplier": 0.5, "priceAction": environment}
    return {"status": "cautious", "label": "指数过渡", "detail": "指数方向不清，暂不新增仓位。", "allowNewPosition": False, "positionCapMultiplier": 0.0, "priceAction": environment}


def _plan_context(price_action: dict[str, Any]) -> dict[str, Any]:
    future_plans = price_action.get("futurePlans") or []
    setups = price_action.get("setups") or []
    trade_plans = price_action.get("tradePlans") or []
    confirmed_contract = next((item for item in trade_plans if item.get("status") == "CONFIRMED" and not item.get("blockedReasons")), None)
    setup = next((item for item in setups if confirmed_contract and item.get("setupId") == confirmed_contract.get("setupId")), None)
    setup = setup or next((item for item in setups if item.get("status") == "CONFIRMED" and not item.get("reviewRequired")), None)
    setup = setup or next((item for item in future_plans if item.get("status") == "CONFIRMED"), None)
    setup = setup or next((item for item in setups if item.get("direction") in {"BUY", "SELL"}), None)
    setup = setup or next((item for item in future_plans if item.get("direction") in {"BUY", "SELL"}), None)
    return {
        "environment": price_action.get("environment") or {},
        "levels": price_action.get("levels") or [],
        "setups": setups,
        "activeSetup": setup,
        "marketMode": price_action.get("marketMode") or {},
        "futurePlans": future_plans,
        "tradePlans": trade_plans,
        "intradayTradePlans": price_action.get("intradayTradePlans") or [],
        "signalRecords": price_action.get("signalRecords") or [],
        "structures": price_action.get("structures") or [],
        "magnetZones": price_action.get("magnetZones") or [],
        "legs": price_action.get("legs") or [],
        "patternFamilies": price_action.get("patternFamilies") or [],
        "profile": price_action.get("profile") or {},
        "profileVersion": price_action.get("profileVersion"),
        "sessionContext": price_action.get("sessionContext") or {},
        "sessionPatterns": price_action.get("sessionPatterns") or [],
        "openingContext": price_action.get("openingContext") or {},
        "uncertainty": price_action.get("uncertainty") or {},
        "discipline": price_action.get("discipline") or {},
        "assessment": price_action.get("assessment") or {},
        "volumeTurnover": price_action.get("volumeTurnover") or {},
        "volume": price_action.get("volume") or {},
    }


def _latest_signal(price_action: dict[str, Any], direction: str) -> dict[str, Any] | None:
    candidates = [
        item
        for item in price_action.get("signals", [])
        if item.get("direction") == direction and item.get("status") == "CONFIRMED" and not item.get("reviewRequired")
    ]
    return candidates[-1] if candidates else None


def _latest_future_plan(
    price_action: dict[str, Any],
    direction: str,
    *,
    confirmed_only: bool = False,
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in price_action.get("futurePlans", [])
        if item.get("direction") == direction
        and item.get("anchorPrice")
        and (not confirmed_only or (item.get("status") == "CONFIRMED" and not item.get("reviewRequired")))
    ]
    return candidates[0] if candidates else None


def _eligible_long_setup(signal: dict[str, Any]) -> bool:
    return signal.get("type") in {
        "TREND_PULLBACK_H1",
        "TREND_PULLBACK_H2",
        "TREND_PULLBACK_L1",
        "TREND_PULLBACK_L2",
        "TREND_FIRST_PULLBACK",
        "TREND_TWO_LEG_PULLBACK",
        "BULL_FLAG",
        "BREAKOUT_RETEST_UP",
        "RANGE_EDGE_FADE_UP",
        "RANGE_BREAKOUT_RETEST",
        "RANGE_BREAKOUT_CONTINUATION",
        "RANGE_LOWER_REVERSAL",
        "DOUBLE_BOTTOM",
        "FAILED_BEARISH_BREAKOUT",
        "MAJOR_BULLISH_REVERSAL",
    }


def _has_wedge_warning(price_action: dict[str, Any]) -> bool:
    return any(item.get("type") == "WEDGE_THIRD_PUSH" and item.get("direction") == "SELL" for item in price_action.get("setups", []))


def _moving_stop_state(
    *,
    bars: list[dict[str, float | str]],
    position: dict[str, Any],
    cost_price: float,
    initial_stop: float,
    atr: float,
    close: float,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Return the persisted, one-way long trailing stop for a holding.

    This mirrors the Binance holding state machine at the A-share daily-bar
    cadence: +1R enables protection, the favourable extreme less an ATR buffer
    is the primary trail, and a small break-even buffer is the fallback.  The
    stored peak and stop are retained so a rolling lookback or a smaller ATR
    can only tighten future defense, never loosen it.
    """

    risk_per_share = cost_price - initial_stop
    previous_stop = _number(position.get("trailingStop"))
    previous_peak = _number(position.get("trailingPeak"))
    previous_stage = _trailing_stage(
        position.get("trailingStage"),
        fallback="ATR_TRAILING" if bool(position.get("trailingActive")) or previous_stop > 0 else "INITIAL",
    )
    previous_activation_price = _number(position.get("trailingActivationPrice"))
    previous_activation_date = position.get("trailingActivationAt")
    if risk_per_share <= max(cost_price * 0.001, 0.001):
        return {
            "active": False,
            "stage": previous_stage,
            "activationPrice": None,
            "activationDate": None,
            "recentHigh": None,
            "peakPrice": _round_price(previous_peak),
            "peakDate": position.get("trailingPeakAt"),
            "movingCandidate": None,
            "movingStop": _round_price(previous_stop),
            "reason": "当前结构防守位已高于实际入场价，继续按该结构防守；不使用成本线人为改写止损。",
        }

    activation_price = cost_price + risk_per_share
    created_at = position.get("createdAt")
    if created_at:
        holding_bars = _bars_after_entry(bars, created_at)
        activation_bars = [
            item for item in holding_bars
            if float(item.get("close") or 0) >= activation_price
        ]
        has_entry_date = True
    else:
        # Legacy/manual positions may not have an opening timestamp.  In that
        # case only the current close can conservatively prove a favorable move;
        # never use a historical high to infer that the position earned 1R.
        holding_bars = bars
        activation_bars = holding_bars[-1:] if close >= activation_price else []
        has_entry_date = False

    observed_high = _recent_high(holding_bars, rules["movingStopLookback"]) if holding_bars else None
    recent_high = max(previous_peak, observed_high or 0) or None
    active = bool(activation_bars) or previous_stage != "INITIAL" or previous_stop > 0
    activation_date = previous_activation_date or (str(activation_bars[0].get("date") or "") if activation_bars else None)
    effective_activation_price = previous_activation_price or activation_price
    moving_candidate = (
        recent_high - atr * rules["movingStopAtr"]
        if active and recent_high and atr > 0
        else None
    )
    breakeven_stop = cost_price + atr * rules["movingStopBreakevenBufferAtr"] if atr > 0 else cost_price
    preferred_stop = moving_candidate if moving_candidate and moving_candidate > cost_price else breakeven_stop
    moving_stop = max(previous_stop, preferred_stop) if active else None
    candidate_stage = "ATR_TRAILING" if moving_candidate and moving_candidate > cost_price else "BREAKEVEN"
    stage = _trailing_stage(candidate_stage, fallback=previous_stage)
    if _trailing_stage_order(stage) < _trailing_stage_order(previous_stage):
        stage = previous_stage
    if active:
        peak_detail = f"持仓峰值 {recent_high:.2f} 减 {rules['movingStopAtr']:.1f}ATR" if recent_high is not None else "峰值数据暂缺，先保持已有保本保护"
        reason = (
            f"{'建仓后完整日K曾' if has_entry_date else '当前收盘已'}达到成本 + 1R（{effective_activation_price:.2f}），"
            f"移动止损已启用：{peak_detail}；"
            "候选仍低于成本时改用保本缓冲。已启用后只会收紧，不会下调。"
        )
    elif has_entry_date:
        reason = (
            f"建仓后尚未出现完整日K收盘达到成本 + 1R（{activation_price:.2f}），"
            "移动止损暂不启用，继续使用初始风险边界。"
        )
    else:
        reason = (
            f"缺少建仓时间，且当前收盘未达到成本 + 1R（{activation_price:.2f}）；"
            "不以建仓前历史高点推断移动止损。"
        )
    return {
        "active": active,
        "stage": stage,
        "activationPrice": _round_price(effective_activation_price),
        "activationDate": activation_date,
        "recentHigh": _round_price(recent_high or 0),
        "peakPrice": _round_price(recent_high or 0),
        "peakDate": position.get("trailingPeakAt") or _date_of_high(holding_bars, recent_high),
        "movingCandidate": _round_price(moving_candidate or 0),
        "movingStop": _round_price(moving_stop or 0),
        "reason": reason,
    }


_TRAILING_STAGE_ORDER = {
    "INITIAL": 0,
    "BREAKEVEN": 1,
    "ATR_TRAILING": 2,
}


def _trailing_stage(value: Any, *, fallback: str = "INITIAL") -> str:
    stage = str(value or "").strip().upper()
    if stage in _TRAILING_STAGE_ORDER:
        return stage
    return fallback if fallback in _TRAILING_STAGE_ORDER else "INITIAL"


def _trailing_stage_order(value: Any) -> int:
    return _TRAILING_STAGE_ORDER.get(_trailing_stage(value), 0)


def _date_of_high(bars: list[dict[str, float | str]], high: float | None) -> str | None:
    if not high:
        return None
    for item in reversed(bars):
        if abs(float(item.get("high") or 0) - high) <= max(high * 1e-9, 1e-9):
            return str(item.get("date") or "") or None
    return None


def _initial_position_stop(
    *,
    bars: list[dict[str, float | str]],
    close: float,
    cost_price: float,
    cost_stop: float,
    atr: float,
    structural_invalidation: float,
    structural_plan: dict[str, Any] | None,
    structural_reference: float | None,
    rules: dict[str, Any],
) -> tuple[float, str, str]:
    """Choose a long-position stop from the structure, not from the cost line.

    Price-action stops belong beyond the signal/pullback structure. The account
    risk boundary is reported separately and may require reducing an already
    oversized holding; it is not a reason to place the technical stop inside the
    structure. Pending long plans can provide a conservative structure boundary,
    but they are never treated as an executable entry signal.
    """

    candidates: list[tuple[float, str, str]] = []
    if 0 < structural_invalidation < close:
        candidates.append(
            (
                structural_invalidation,
                "已确认结构失效位",
                f"初始止损位于已确认做多结构失效价 {structural_invalidation:.2f} 的另一侧。",
            )
        )

    pending_invalidation = _number((structural_plan or {}).get("invalidationPrice"))
    if (
        not structural_invalidation
        and 0 < pending_invalidation < close
        and (structural_plan or {}).get("status") in {"PENDING", "NEEDS_REVIEW", "WATCH"}
    ):
        candidates.append(
            (
                pending_invalidation,
                "观察结构边界",
                f"当前没有已确认做多信号，先以观察结构边界 {pending_invalidation:.2f} 作为风险参考；确认前不加仓。",
            )
        )

    if structural_reference and structural_reference < close:
        buffer = atr * rules["structureStopBufferAtr"] if atr > 0 else cost_price * 0.005
        support_stop = structural_reference - buffer
        if 0 < support_stop < close:
            candidates.append(
                (
                    support_stop,
                    "近期支撑结构",
                    f"未识别到可执行失效信号，止损放在近期支撑 {structural_reference:.2f} 下方并留出波动缓冲。",
                )
            )

    if candidates:
        stop, source, reason = candidates[0]
        # A valid current structure can sit above the original entry after a
        # profitable advance.  It remains a structural stop, not a cost-based
        # break-even rule, as long as it is still below the latest close.
        return _round_price(stop) or stop, source, reason

    recent_lows = [
        float(item.get("low") or 0)
        for item in bars[-max(5, int(rules["movingStopLookback"])) :]
        if 0 < float(item.get("low") or 0) < close
    ]
    if recent_lows:
        buffer = atr * rules["structureStopBufferAtr"] if atr > 0 else cost_price * 0.005
        recent_stop = min(recent_lows) - buffer
        if recent_stop > 0:
            return (
                _round_price(recent_stop) or recent_stop,
                "近20根日K结构低点",
                f"没有可确认结构失效价，使用近{int(rules['movingStopLookback'])}根完整日K结构低点 {min(recent_lows):.2f} 下方的防守参考。",
            )

    # The account-risk line is deliberately separate.  Raising an ATR-based
    # technical stop to that line would put the stop inside the price
    # structure and recreate the cost-line bug this plan is meant to avoid.
    atr_stop = close - atr * rules["initialStopAtr"] if atr > 0 else 0.0
    fallback = atr_stop if atr_stop > 0 else cost_stop
    return (
        _round_price(fallback) or fallback,
        "ATR波动后备" if atr_stop > 0 else "账户风险后备",
        (
            f"缺少有效结构边界，初始技术防守位按最近完整收盘价下方 {rules['initialStopAtr']:.1f} ATR 放在 {fallback:.2f}；"
            f"账户风险退出线 {cost_stop:.2f} 另行检查。"
            if atr_stop > 0
            else f"缺少有效结构边界，暂以后备账户风险退出线 {fallback:.2f} 管理；补足结构数据后重新评估。"
        ),
    )


def _bars_after_entry(
    bars: list[dict[str, float | str]],
    created_at: Any,
) -> list[dict[str, float | str]]:
    entry_key = _date_key(created_at)
    if not entry_key:
        return []
    return [item for item in bars if _date_key(item.get("date")) > entry_key]


def _date_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(character for character in text if character.isdigit())
    return digits[:8] if len(digits) >= 8 else text[:10]


def _levels_from_signal(signal: dict[str, Any] | None, close: float, rules: dict[str, Any]) -> dict[str, Any]:
    if not signal:
        return _empty_levels(close, rules)
    entry = _number(signal.get("entryLimit"))
    direction = signal.get("direction")
    stop = _number(signal.get("protectiveStop") or signal.get("invalidationPrice"))
    first_target = _number(signal.get("firstTarget"))
    extension_target = _number(signal.get("extensionTarget"))
    market_mode = str(signal.get("marketMode") or "REBOUND")
    first_target_role = "range_opposite_edge" if market_mode == "RANGE" else "first_magnet"
    return {
        "direction": direction,
        "executionAction": "BUY_LONG" if direction == "BUY" else "SELL_EXISTING_LONG" if direction == "SELL" else "OBSERVE",
        "executionConstraint": "CASH_LONG_ONLY" if direction == "BUY" else "EXISTING_LONG_ONLY" if direction == "SELL" else "NO_NEW_SHORT",
        "positionRequired": direction == "SELL",
        "shortSellingAllowed": False,
        "targetSemantics": "LONG_PROFIT_TARGET" if direction == "BUY" else "DOWNSIDE_REFERENCE_ONLY" if direction == "SELL" else "OBSERVATION_ONLY",
        "marketMode": market_mode,
        "marketModeLabel": signal.get("marketModeLabel") or {"TREND": "趋势", "RANGE": "区间", "REBOUND": "反弹"}.get(market_mode, "反弹"),
        "levelContext": "BUY_PLAN" if direction == "BUY" else "SELL_PLAN" if direction == "SELL" else "OBSERVATION",
        "targetDirection": "UP" if direction == "BUY" else "DOWN" if direction == "SELL" else None,
        "triggerPrice": _round_price(_number(signal.get("triggerPrice"))),
        "plannedEntryPrice": _round_price(entry) if direction in {None, "BUY"} else None,
        "plannedEntryCandidate": None,
        "entryPlanStatus": signal.get("status"),
        "entryPlanReason": "新仓计划入场上限；必须同时满足结构确认和账户风险条件。",
        "entryPrice": None,
        "costPrice": None,
        "costStop": None,
        "structuralInvalidation": _round_price(_number(signal.get("invalidationPrice"))),
        "structureBoundary": _round_price(_number(signal.get("invalidationPrice"))),
        "structuralReference": None,
        "protectiveStop": _round_price(stop),
        "activeDefense": _round_price(stop),
        "technicalStop": _round_price(stop),
        "accountRiskExit": None,
        "technicalStopAvailable": bool(stop),
        "initialStop": _round_price(stop),
        "hardStop": None,
        "initialStopSource": None,
        "initialStopReason": None,
        "riskBudgetExceeded": False,
        "movingStop": None,
        "movingStopCandidate": None,
        "movingStopActive": False,
        "movingStopActivationPrice": None,
        "movingStopActivationDate": None,
        "movingStopReason": None,
        "recentHigh20": None,
        "firstTarget": _round_price(first_target),
        "extensionTarget": _round_price(extension_target),
        "firstTargetRole": first_target_role if first_target > 0 else None,
        "firstTargetSemantics": "第一磁铁是可能测试/停顿区；到达后分批止盈，不是收益保证。" if first_target > 0 else None,
        "extensionTargetRole": "trend_extension" if market_mode == "TREND" and extension_target > 0 else None,
        "firstTargetSource": "已确认做多计划第一磁铁" if direction == "BUY" and first_target > 0 else None,
        "firstTargetReason": "做多价格到达第一磁铁后分批止盈；它是可能测试/停顿的位置，不是收益保证。" if direction == "BUY" and first_target > 0 else None,
        "takeProfitStatus": "STRUCTURE" if direction == "BUY" and first_target > 0 else "UNAVAILABLE",
        "downsideTarget": _round_price(first_target) if direction == "SELL" else None,
        "downsideExtensionTarget": _round_price(extension_target) if direction == "SELL" else None,
        "cancellationConditions": signal.get("cancellationConditions") or [],
        "defenseTriggerPrice": None,
        "defenseObservationPrice": None,
        "defenseObservationLabel": None,
        "defenseObservationCondition": None,
        "defenseObservationStatus": None,
        "addPriceRule": None,
        "futureAnchorPrice": _round_price(_number(signal.get("anchorPrice") or signal.get("triggerPrice"))),
        "futureAnchorLabel": signal.get("anchorLabel"),
        "futureAnchorCondition": signal.get("anchorCondition"),
        "futurePlanStatus": signal.get("status"),
        "futurePlanType": signal.get("type"),
        "futurePlanVolume": signal.get("volume") or {},
        "plannedNotice": "触发、止损和磁铁均为计划价格；跳空、涨跌停和流动性可能使其无法按价成交。",
    }


def _position_levels(
    *,
    defense_plan: dict[str, Any] | None,
    upside_plan: dict[str, Any] | None,
    close: float,
    cost_price: float,
    structural_invalidation: float,
    structural_reference: float | None,
    active_defense: float,
    hard_stop: float,
    cost_stop: float,
    moving_stop: float | None,
    recent_high: float | None,
    rules: dict[str, Any],
    moving_stop_active: bool = False,
    moving_stop_activation_price: float | None = None,
    moving_stop_activation_date: str | None = None,
    moving_stop_reason: str | None = None,
    moving_stop_candidate: float | None = None,
    moving_stop_stage: str | None = None,
    moving_stop_peak: float | None = None,
    moving_stop_peak_date: str | None = None,
    structure_boundary: float | None = None,
    initial_stop_source: str | None = None,
    initial_stop_reason: str | None = None,
    risk_budget_exceeded: bool = False,
    defense_observation: dict[str, Any] | None = None,
    price_action_levels: list[dict[str, Any]] | None = None,
    atr: float = 0.0,
) -> dict[str, Any]:
    """Build long-position levels while keeping profit and defense plans separate."""

    source = defense_plan or upside_plan
    levels = _levels_from_signal(source, close, rules)
    # A confirmed defensive structure takes precedence for a long position. A
    # pending bearish observation is kept as metadata and does not erase a
    # valid long-side entry/target candidate.
    take_profit = _position_take_profit_target(
        plan=upside_plan,
        price_action_levels=price_action_levels or [],
        close=close,
        cost_price=cost_price,
        active_defense=active_defense,
        hard_stop=hard_stop,
        atr=atr,
        rules=rules,
    )
    upside_target = take_profit["price"]
    upside_extension = None if defense_plan else _future_upside_target(upside_plan, "extensionTarget", close, active_defense, cost_price)
    if upside_target is None or (upside_extension is not None and upside_extension <= upside_target):
        upside_extension = None
    add_price = None if defense_plan else _future_add_price(upside_plan, cost_price, active_defense)
    entry_candidate = None if defense_plan else _future_entry_candidate(upside_plan, cost_price, active_defense)
    downside_target = _future_downside_target(defense_plan, "firstTarget", active_defense)
    downside_extension = _future_downside_target(defense_plan, "extensionTarget", downside_target) if downside_target is not None else None
    if defense_plan and downside_target is None and active_defense > 0 and atr > 0:
        # This is a holding-management path reference, not a short target.
        # When the bearish setup has no confirmed lower magnet, place the
        # observation levels beyond the live defense line using the larger of
        # current ATR movement and the trigger-to-defense distance.  They are
        # deliberately kept out of the SELL trade contract and its R math.
        trigger = _number(defense_plan.get("triggerPrice"))
        path_step = max(atr * 2.0, abs(trigger - active_defense))
        downside_target = _round_price(active_defense - path_step)
        if downside_target and downside_target > 0:
            downside_extension = _round_price(downside_target - path_step)
    if downside_extension is not None and downside_extension >= downside_target:
        downside_extension = None

    levels.update(
        {
            "direction": "SELL" if defense_plan else "BUY" if upside_plan else None,
            "executionAction": "SELL_EXISTING_LONG" if defense_plan else "BUY_LONG" if upside_plan else "OBSERVE",
            "executionConstraint": "EXISTING_LONG_ONLY" if defense_plan else "CASH_LONG_ONLY" if upside_plan else "NO_NEW_SHORT",
            "positionRequired": bool(defense_plan),
            "shortSellingAllowed": False,
            "targetSemantics": "LONG_PROFIT_TARGET" if upside_target is not None else "DOWNSIDE_REFERENCE_ONLY" if defense_plan else "OBSERVATION_ONLY",
            "levelContext": "LONG_POSITION_DEFENSE" if defense_plan else "LONG_POSITION_MANAGEMENT",
            "targetDirection": "UP" if upside_target is not None else "DOWN" if defense_plan else None,
            "triggerPrice": _round_price(_number((source or {}).get("triggerPrice"))),
            "plannedEntryPrice": add_price,
            "plannedEntryCandidate": entry_candidate,
            "entryPlanStatus": (upside_plan or {}).get("status") if entry_candidate else None,
            "entryPlanReason": (
                "已确认结构且价格高于成本和当前防守线，可作为加仓观察价。"
                if add_price
                else "价格行为结构尚未确认；该价位仅供未来入场观察，不得直接成交。"
                if entry_candidate
                else "暂无高于持仓成本且通过结构确认的未来入场价；持仓实际入场价已单独显示，不能把成本价倒推成新的买点。"
            ),
            "entryPrice": _round_price(cost_price),
            "costPrice": _round_price(cost_price),
            "costStop": _round_price(cost_stop),
            "structuralInvalidation": _round_price(structural_invalidation),
            "structureBoundary": _round_price(structure_boundary or 0),
            "structuralReference": _round_price(structural_reference or 0),
            "protectiveStop": _round_price(active_defense),
            "activeDefense": _round_price(active_defense),
            "technicalStop": _round_price(active_defense),
            "accountRiskExit": _round_price(cost_stop),
            "technicalStopAvailable": True,
            "initialStop": _round_price(hard_stop),
            "hardStop": _round_price(hard_stop),
            "initialStopSource": initial_stop_source,
            "initialStopReason": initial_stop_reason,
            "riskBudgetExceeded": bool(risk_budget_exceeded),
            "movingStop": _round_price(moving_stop or 0),
            "movingStopCandidate": _round_price(moving_stop_candidate or 0),
            "movingStopActive": bool(moving_stop_active),
            "movingStopActivationPrice": _round_price(moving_stop_activation_price or 0),
            "movingStopActivationDate": moving_stop_activation_date,
            "movingStopReason": moving_stop_reason,
            "movingStopStage": _trailing_stage(moving_stop_stage),
            "movingStopPeak": _round_price(moving_stop_peak or recent_high or 0),
            "movingStopPeakDate": moving_stop_peak_date,
            "recentHigh20": _round_price(recent_high or 0),
            "firstTarget": upside_target,
            "extensionTarget": upside_extension,
            "firstTargetRole": "first_magnet",
            "firstTargetSemantics": "持仓上方第一磁铁是可能测试/停顿区；首次触及按计划分批止盈，不承诺成交。",
            "extensionTargetRole": "trend_extension" if upside_extension is not None else None,
            "firstTargetSource": take_profit["source"],
            "firstTargetReason": take_profit["reason"],
            "takeProfitStatus": take_profit["status"],
            "downsideTarget": downside_target,
            "downsideExtensionTarget": downside_extension,
            "defenseTriggerPrice": _round_price(_number((defense_plan or {}).get("triggerPrice"))) if defense_plan else None,
            "defenseObservationPrice": _round_price(_number((defense_observation or {}).get("anchorPrice"))) if defense_observation else None,
            "defenseObservationLabel": (defense_observation or {}).get("anchorLabel") if defense_observation else None,
            "defenseObservationCondition": (defense_observation or {}).get("anchorCondition") if defense_observation else None,
            "defenseObservationStatus": (defense_observation or {}).get("status") if defense_observation else None,
            "addPriceRule": f"加仓观察价必须高于持仓成本 {cost_price:.3f} 和当前有效止损 {active_defense:.3f}。",
        }
    )
    return levels


def _position_take_profit_target(
    *,
    plan: dict[str, Any] | None,
    price_action_levels: list[dict[str, Any]],
    close: float,
    cost_price: float,
    active_defense: float,
    hard_stop: float,
    atr: float,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Return an upper profit-management target for every analyzable holding.

    A defensive SELL plan describes a lower-risk path, but it does not erase the
    long position's upper exit plan.  The target is chosen from a confirmed long
    plan first, then from the nearest upper price-action magnet.  Only when no
    upper magnet exists do we use a documented ATR/risk-reward projection.
    """

    floor = max(close, cost_price)
    tolerance = max(abs(floor) * 0.0005, 0.001)

    if plan and plan.get("status") in {None, "CONFIRMED"} and not plan.get("reviewRequired"):
        planned_target = _number(plan.get("firstTarget"))
        if planned_target > floor + tolerance:
            return {
                "price": _round_price(planned_target),
                "source": "已确认做多计划第一磁铁",
                "status": "STRUCTURE",
                "reason": f"沿用已确认做多计划的第一磁铁 {planned_target:.3f}；到达后分批止盈并重新检查后续跟随。",
            }

    candidates: list[tuple[float, dict[str, Any]]] = []
    dynamic_candidates: list[tuple[float, dict[str, Any]]] = []
    for level in price_action_levels:
        price = _number(level.get("price"))
        role = str(level.get("role") or "")
        kind = str(level.get("kind") or "")
        if price <= floor + tolerance:
            continue
        if role not in _UPSIDE_TARGET_ROLES and "RESISTANCE" not in role and "UPSIDE" not in role:
            continue
        if kind == "EMA20":
            dynamic_candidates.append((price, level))
        else:
            candidates.append((price, level))
    candidates = candidates or dynamic_candidates
    if candidates:
        target, level = min(candidates, key=lambda item: item[0])
        source = str(level.get("source") or level.get("kind") or "上方价格行为磁铁")
        return {
            "price": _round_price(target),
            "source": f"上方磁铁：{source}",
            "status": "STRUCTURE",
            "reason": f"最近高于当前价和持仓成本的上方磁铁为 {target:.3f}；首次触及时观察停顿、拒绝或有效突破。",
        }

    # A complete history can still have no identifiable resistance above the
    # latest close.  Preserve a usable, auditable profit plan instead of
    # silently returning an empty field.  The projection is a management target,
    # not a forecast and does not override a structural defense or hard exit.
    risk_per_share = max(cost_price - min(active_defense, cost_price), 0.0)
    if risk_per_share <= tolerance:
        risk_per_share = max(cost_price - min(hard_stop, cost_price), 0.0)
    if risk_per_share <= tolerance:
        risk_per_share = max(atr * 0.5, cost_price * 0.01, 0.01)
    volatility_move = atr * float(rules["takeProfitAtrFallback"]) if atr > 0 else 0.0
    reward_move = risk_per_share * float(rules["minimumRewardRisk"])
    move = max(volatility_move, reward_move, cost_price * 0.01, 0.01)
    projected = max(floor + move, cost_price + reward_move)
    target = _round_price(projected)
    if target is None or target <= floor:
        target = _round_price(floor + max(move, 0.01))
    return {
        "price": target,
        "source": "ATR/风险回报后备止盈位",
        "status": "RISK_FALLBACK",
        "reason": (
            f"未找到高于当前价和持仓成本的上方磁铁，按 {rules['takeProfitAtrFallback']:.1f} ATR 与"
            f"至少 {rules['minimumRewardRisk']:.1f}R 的较大者设置管理目标；不是收益保证。"
        ),
    }


def _future_upside_target(
    plan: dict[str, Any] | None,
    field: str,
    close: float,
    active_defense: float,
    cost_price: float,
) -> float | None:
    if not plan or plan.get("status") not in {None, "CONFIRMED"} or plan.get("reviewRequired"):
        return None
    # An extension is a continuation-management level, not a universal
    # percentage projection.  Only a confirmed trend plan may expose it;
    # range and rebound plans stop at their first structural magnet.
    if field == "extensionTarget" and str(plan.get("marketMode") or "TREND").upper() != "TREND":
        return None
    target = _number((plan or {}).get(field))
    floor = max(close, active_defense, cost_price)
    rounded = _round_price(target)
    return rounded if rounded is not None and rounded > floor else None


def _future_add_price(plan: dict[str, Any] | None, cost_price: float, active_defense: float) -> float | None:
    if not plan or plan.get("status") not in {None, "CONFIRMED"} or plan.get("reviewRequired"):
        return None
    entry = _number((plan or {}).get("entryLimit"))
    rounded = _round_price(entry)
    return rounded if rounded is not None and rounded > cost_price and rounded > active_defense else None


def _future_entry_candidate(plan: dict[str, Any] | None, cost_price: float, active_defense: float) -> float | None:
    """Return a visible future entry anchor, including pending structures."""

    entry = _number((plan or {}).get("entryLimit"))
    rounded = _round_price(entry)
    return rounded if rounded is not None and rounded > cost_price and rounded > active_defense else None


def _future_downside_target(plan: dict[str, Any] | None, field: str, upper_bound: float) -> float | None:
    if field == "extensionTarget" and str((plan or {}).get("marketMode") or "TREND").upper() != "TREND":
        return None
    target = _number((plan or {}).get(field))
    rounded = _round_price(target)
    return rounded if rounded is not None and rounded > 0 and rounded < upper_bound else None


def _empty_levels(close: float, rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "direction": None,
        "executionAction": "OBSERVE",
        "executionConstraint": "NO_NEW_SHORT",
        "positionRequired": False,
        "shortSellingAllowed": False,
        "targetSemantics": "OBSERVATION_ONLY",
        "levelContext": "NO_PLAN",
        "targetDirection": None,
        "triggerPrice": None,
        "plannedEntryPrice": None,
        "plannedEntryCandidate": None,
        "entryPlanStatus": None,
        "entryPlanReason": "没有完整价格行为计划时，不生成可执行入场价。",
        "entryPrice": None,
        "costPrice": None,
        "costStop": None,
        "structuralInvalidation": None,
        "structureBoundary": None,
        "structuralReference": None,
        "protectiveStop": None,
        "activeDefense": None,
        "technicalStop": None,
        "accountRiskExit": None,
        "technicalStopAvailable": False,
        "initialStop": None,
        "hardStop": _round_price(close * (1 - rules["hardStopPct"])) if close else None,
        "initialStopSource": None,
        "initialStopReason": None,
        "riskBudgetExceeded": False,
        "movingStop": None,
        "movingStopCandidate": None,
        "movingStopActive": False,
        "movingStopActivationPrice": None,
        "movingStopActivationDate": None,
        "movingStopReason": None,
        "recentHigh20": None,
        "firstTarget": None,
        "extensionTarget": None,
        "firstTargetRole": None,
        "firstTargetSemantics": None,
        "extensionTargetRole": None,
        "firstTargetSource": None,
        "firstTargetReason": "没有完整价格行为数据时，无法确认上方磁铁或计算止盈目标。",
        "takeProfitStatus": "UNAVAILABLE",
        "downsideTarget": None,
        "downsideExtensionTarget": None,
        "defenseTriggerPrice": None,
        "defenseObservationPrice": None,
        "defenseObservationLabel": None,
        "defenseObservationCondition": None,
        "defenseObservationStatus": None,
        "addPriceRule": None,
        "cancellationConditions": [],
        "futureAnchorPrice": None,
        "futureAnchorLabel": None,
        "futureAnchorCondition": None,
        "futurePlanStatus": None,
        "futurePlanType": None,
        "futurePlanVolume": {},
        "plannedNotice": "没有完整价格行为计划时，不生成可执行点位。",
    }


def _nearest_support(levels: list[dict[str, Any]], close: float) -> float | None:
    # 20EMA is a dynamic context line, not the opposite side of a completed
    # signal, pullback, or swing.  Using it here made an arbitrary moving
    # average look like a near-cost structural stop.  Initial position risk
    # can only come from an actual completed support structure.
    structural_kinds = {"SWING_LOW", "RANGE_LOW", "GAP_EDGE"}
    prices = [
        float(item.get("price") or 0)
        for item in levels
        if item.get("kind") in structural_kinds
        and "SUPPORT" in str(item.get("role"))
        and 0 < float(item.get("price") or 0) <= close
    ]
    return max(prices) if prices else None


def _recent_high(bars: list[dict[str, float | str]], lookback: int) -> float | None:
    highs = [float(item.get("high") or 0) for item in bars[-max(1, int(lookback)) :] if float(item.get("high") or 0) > 0]
    return max(highs) if highs else None


def _next_session_plan(
    *,
    action: str,
    order: dict[str, Any],
    levels: dict[str, Any],
    position: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    blocked_reasons: list[str],
) -> dict[str, Any]:
    """Expose the next conditional action without pretending it is live execution.

    The discipline workspace is refreshed from completed bars rather than a
    live order router.  This contract therefore describes the next action a
    trader should take *if* the stated price and confirmation conditions occur
    on the next session, including the exact share context used to form it.
    """

    has_position = bool(position and _number(position.get("shares")) > 0)
    shares = int(_number((position or {}).get("shares"))) if has_position else 0
    active_defense = _number(levels.get("activeDefense") or levels.get("technicalStop"))
    first_target = _number(levels.get("firstTarget"))
    target_reduction_shares = int(_number(levels.get("takeProfitReductionShares")))
    blocked = _unique(blocked_reasons)
    unmet = [
        {"name": str(item.get("name") or "条件"), "detail": str(item.get("detail") or "")}
        for item in checks
        if not item.get("passed")
    ]
    base = {
        "window": "NEXT_SESSION",
        "label": "下一交易日条件计划",
        "confirmation": "以到价后的完整日K收盘、后续跟随和可交易性复核为准；不是实时自动下单。",
        "hasPosition": has_position,
        "positionShares": shares,
        "blockedReasons": blocked,
        "unmetRequirements": unmet,
        "branches": [],
    }

    if not has_position:
        entry = _number(levels.get("plannedEntryPrice") or levels.get("triggerPrice"))
        stop = _number(order.get("stopPrice") or levels.get("protectiveStop"))
        target = _number(levels.get("firstTarget"))
        if action == "BUY":
            base["status"] = "ARMED"
            base["headline"] = "下一交易日满足入场触发、收盘确认与风险条件时，按计划建立仓位。"
            base["branches"].append(
                {
                    "id": "ENTRY",
                    "status": "ARMED",
                    "action": "BUY",
                    "label": "开仓",
                    "shares": int(_number(order.get("shares"))),
                    "triggerPrice": _round_price(entry),
                    "condition": order.get("when") or f"价格触发 {entry:.2f} 后收盘确认，且成交价不高于计划上限。",
                    "operation": f"买入 {int(_number(order.get('shares')))} 股；初始止损 {stop:.2f}，第一目标 {target:.2f}。",
                    "cancel": f"若入场前完整日K收盘跌破结构防守 {stop:.2f}，取消本次开仓。" if stop else "止损或结构失效价缺失时，取消开仓。",
                }
            )
        else:
            base["status"] = "WAIT"
            base["headline"] = "下一交易日先观察新的做多结构；条件重新完整后才形成开仓计划。"
            base["branches"].append(
                {
                    "id": "ENTRY_REVIEW",
                    "status": "WAIT",
                    "action": "WAIT",
                    "label": "继续观察",
                    "shares": 0,
                    "triggerPrice": _round_price(entry),
                    "condition": "下一根完整日K出现顺势回撤、突破回踩、区间下沿反转或失败下破，并同时完成收盘、量能和风险复核。",
                    "operation": "只有届时生成有效入场价、结构止损和目标空间后才建立仓位。",
                    "cancel": "在没有完整做多结构前不预设买入数量，也不把下行观察当成卖出动作。",
                }
            )
        return base

    if action == "SELL":
        base["status"] = "TRIGGERED"
        base["headline"] = "本轮退出条件已经确认；下一交易日处理全部现有持仓。"
        base["branches"].append(
            {
                "id": "EXIT_NOW",
                "status": "TRIGGERED",
                "action": "SELL",
                "label": "清仓",
                "shares": shares,
                "triggerPrice": _round_price(_number(order.get("stopPrice") or active_defense)),
                "condition": order.get("priceRule") or "已确认退出条件。",
                "operation": f"卖出全部 {shares} 股；按下一交易日实际可成交价格复核。",
                "cancel": "跳空、涨跌停或流动性不足时记录无法按计划成交的原因，禁止以成本价追回。",
            }
        )
        return base

    if action == "REDUCE":
        reduction_shares = int(_number(order.get("shares")))
        base["status"] = "TRIGGERED"
        base["headline"] = "本轮减仓条件已经确认；下一交易日先降低已有仓位风险。"
        base["branches"].append(
            {
                "id": "REDUCE_NOW",
                "status": "TRIGGERED",
                "action": "REDUCE",
                "label": "部分减仓",
                "shares": reduction_shares,
                "triggerPrice": _round_price(_number(levels.get("reductionTriggerPrice") or active_defense)),
                "condition": order.get("priceRule") or "已确认减仓条件。",
                "operation": f"卖出 {reduction_shares} 股；剩余 {max(0, shares - reduction_shares)} 股继续按有效防守线管理。",
                "cancel": f"若后续完整日K收盘跌破有效防守线 {active_defense:.2f}，卖出剩余仓位。" if active_defense else "后续结构失效时重新评估剩余仓位。",
            }
        )
        return base

    base["status"] = "ARMED"
    base["headline"] = "持仓不因当前价格而改变；下一交易日按止损与止盈两个条件分支执行。"
    if active_defense:
        base["branches"].append(
            {
                "id": "DEFENSE_EXIT",
                "status": "ARMED",
                "action": "SELL",
                "label": "止损清仓",
                "shares": shares,
                "triggerPrice": _round_price(active_defense),
                "condition": f"下一交易日任一完整日K收盘跌破当前有效防守线 {active_defense:.2f}。",
                "operation": f"卖出全部 {shares} 股；不因成本价或盘中反弹放宽防守线。",
                "cancel": "防守线只可随新确认结构上移；不得为了等待反弹下移。",
            }
        )
    if first_target and target_reduction_shares:
        target_is_full_exit = target_reduction_shares >= shares
        base["branches"].append(
            {
                "id": "FIRST_TARGET",
                "status": "ARMED",
                "action": "SELL" if target_is_full_exit else "REDUCE",
                "label": "第一止盈清仓" if target_is_full_exit else "第一止盈",
                "shares": target_reduction_shares,
                "triggerPrice": _round_price(first_target),
                "condition": f"下一交易日完整日K收盘到达或上穿第一止盈 {first_target:.2f}，且没有新的反向失效结构。",
                "operation": (
                    f"卖出全部 {shares} 股；单手持仓不生成无法成交的部分止盈数量。"
                    if target_is_full_exit
                    else f"分批卖出 {target_reduction_shares} 股；剩余 {max(0, shares - target_reduction_shares)} 股继续按有效防守线与趋势跟随管理。"
                ),
                "cancel": "第一止盈不是保证成交价；若先触发防守线，优先执行止损清仓分支。",
            }
        )
    if not base["branches"]:
        base["branches"].append(
            {
                "id": "POSITION_REVIEW",
                "status": "WAIT",
                "action": "WAIT",
                "label": "持仓复核",
                "shares": 0,
                "triggerPrice": None,
                "condition": "下一根完整日K后重新确认防守线与上方磁铁。",
                "operation": "没有有效止损或止盈点位时不擅自交易。",
                "cancel": "补足结构数据前不以成本价替代技术点位。",
            }
        )
    return base


def _level_derivation(levels: dict[str, Any], *, has_position: bool) -> list[dict[str, Any]]:
    """Return one audit record per visible level, with a stable semantic role."""

    rows = [
        ("trigger", "触发/确认点", levels.get("triggerPrice"), "价格越过结构触发位后，仍须以完整K线和后续跟随确认。", "signal.triggerPrice"),
        ("entry", "计划入场上限", levels.get("plannedEntryPrice"), "仅在触发、结构、量能和风险门槛同时通过时使用；不是当前市价指令。", "signal.entryLimit"),
        ("defense", "当前有效防守", levels.get("activeDefense"), "由已确认结构止损和已生效移动止损中更严格的一侧组成；只能按新结构收紧。", "activeDefense"),
        ("invalidation", "结构失效位", levels.get("structuralInvalidation") or levels.get("structureBoundary"), "价格回到结构另一侧时，当前解释被取消，不能用成本价替代。", "signal.invalidationPrice / structureBoundary"),
        ("first_target", "第一目标/磁铁", levels.get("firstTarget"), levels.get("firstTargetReason") or "最近可审计的上方磁铁；它表示可能测试或停顿位置，到达后按计划管理。", levels.get("firstTargetSource") or "price-action magnet"),
        ("extension", "趋势延续目标", levels.get("extensionTarget"), "仅趋势模式在第一目标后出现新的确认和下一层磁铁时启用；区间与反弹不机械外推。", levels.get("extensionTargetSource") or "confirmed trend magnet"),
    ]
    if has_position:
        rows.extend([
            ("cost", "持仓成本", levels.get("costPrice"), "账务成本，用于账户风险和加仓门槛；不是技术支撑。", "portfolio cost basis"),
            ("account_risk", "账户风险线", levels.get("accountRiskExit"), "账户级应急退出线，不改变价格行为结构止损。", "account risk profile"),
        ])
    return [
        {"id": key, "label": label, "price": _round_price(_number(price)), "meaning": meaning, "source": source, "available": _number(price) > 0}
        for key, label, price, meaning, source in rows
    ]


def _decision_hierarchy(
    *,
    price_action: dict[str, Any],
    levels: dict[str, Any],
    action: str,
    has_position: bool,
) -> dict[str, Any]:
    market_mode = dict(price_action.get("marketMode") or {})
    setup = dict(price_action.get("activeSetup") or {})
    mode = str(market_mode.get("mode") or setup.get("marketMode") or "REBOUND")
    label = str(market_mode.get("label") or setup.get("marketModeLabel") or {"TREND": "趋势", "RANGE": "区间", "REBOUND": "反弹"}.get(mode, "反弹"))
    next_condition = str(setup.get("nextCondition") or (setup.get("missingConditions") or ["等待下一根完整K线复核"])[0])
    return {
        "context": "HOLDING" if has_position else "WATCHLIST_ENTRY",
        "marketMode": mode,
        "marketModeLabel": label,
        "patternType": setup.get("type") or setup.get("setupType") or "NO_CONFIRMED_PATTERN",
        "patternFamily": setup.get("family"),
        "lifecycle": setup.get("lifecycle") or setup.get("status") or "WATCH",
        "nextCondition": next_condition,
        "action": action,
        "sourcePages": setup.get("sourcePages") or [],
        "pointDerivation": _level_derivation(levels, has_position=has_position),
    }


def _base_plan(
    *,
    action: str,
    action_label: str,
    tone: str,
    latest: dict[str, Any],
    metrics: dict[str, Any],
    market: dict[str, Any],
    price_action: dict[str, Any],
    checks: list[dict[str, Any]],
    reasons: list[str],
    blocked_reasons: list[str],
    position: dict[str, Any] | None,
    order: dict[str, Any],
    levels: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    position_shares = int(_number((position or {}).get("shares"))) if position else 0
    has_position = position_shares > 0
    plan = {
        "strategy": {
            "id": "price-action-discipline-v2",
            "name": "价格行为纪律交易",
            "dataPolicy": "仅用完整日K生成下一交易日计划；不自动下单。",
            "sourcePolicyId": SOURCE_POLICY_ID,
            "sourcePolicyVersion": SOURCE_POLICY_VERSION,
        },
        "tradeDate": str(latest.get("date") or "--"),
        "action": action,
        "actionLabel": action_label,
        "tone": tone,
        "metrics": metrics,
        "market": market,
        "priceAction": price_action,
        "contractVersion": "price-action-contract-v1",
        "profileVersion": (price_action or {}).get("profileVersion"),
        "tradePlans": (price_action or {}).get("tradePlans") or [],
        "signalRecords": (price_action or {}).get("signalRecords") or [],
        "disciplineState": (price_action or {}).get("discipline") or {"state": "WAIT", "events": [], "automatedOrder": False},
        "checks": checks,
        "reasons": _unique(reasons),
        "blockedReasons": _unique(blocked_reasons),
        "position": position,
        "positionContext": {
            "hasPosition": has_position,
            "shares": position_shares,
            "mode": "POSITION_MANAGEMENT" if has_position else "ENTRY_WATCH",
            "entryAllowed": not has_position,
            "managementAllowed": has_position,
            "allowedActions": ["BUY", "WAIT"] if not has_position else ["HOLD", "REDUCE", "SELL"],
        },
        "decisionHierarchy": _decision_hierarchy(
            price_action=price_action or {},
            levels=levels,
            action=action,
            has_position=has_position,
        ),
        "order": order,
        "levels": levels,
        "riskRules": {
            "accountValue": _round_money(rules["accountValue"]),
            "availableCash": _round_money(rules["availableCash"]),
            "maxPositionPct": round(rules["maxPositionPct"] * 100, 2),
            "riskPerTradePct": round(rules["riskPerTradePct"] * 100, 2),
            "hardStopPct": round(rules["hardStopPct"] * 100, 2),
            "initialStopAtr": rules["initialStopAtr"],
            "structureStopBufferAtr": rules["structureStopBufferAtr"],
            "movingStopLookback": rules["movingStopLookback"],
            "movingStopAtr": rules["movingStopAtr"],
            "movingStopActivationR": 1.0,
            "movingStopBreakevenBufferAtr": rules["movingStopBreakevenBufferAtr"],
            "minimumRewardRisk": rules["minimumRewardRisk"],
            "reductionFraction": rules["reductionFraction"],
            "takeProfitAtrFallback": rules["takeProfitAtrFallback"],
            "lotSize": rules["lotSize"],
            "maxDailyLossPct": round(rules["maxDailyLossPct"] * 100, 2),
            "maxConcentrationPct": round(rules["maxConcentrationPct"] * 100, 2),
            "slippageBps": rules["slippageBps"],
            "feeBps": rules["feeBps"],
            "tPlusOne": rules["tPlusOne"],
        },
        "disciplineRules": [
            "先判定环境、位置、结构和确认；条件缺失时输出等待。",
            "结构失效价不能为了满足仓位而任意收紧；超过硬风险即放弃。",
            "成本价用于成本风险线、加仓门槛和账务，不是技术支撑或继续持有理由。",
            "初始止损优先放在当前信号或结构的另一端；成本风险上限只用于账户风险检查，不把技术止损收紧到结构内部。",
            "建仓后持仓期间的完整日K达到成本 + 1R 后启用移动止损：优先用持仓峰值减2.5ATR，候选仍低于成本时改用保本缓冲；峰值和止损持久化后只会收紧，不会放宽。",
            "持仓始终保留上方第一止盈管理位：优先使用确认计划或最近上方磁铁，没有上方磁铁时使用ATR/风险回报后备；防守目标不覆盖止盈位。",
            "只有确认的强反向冲击、结构突破/失败和后续跟随，或指数进入防守环境，才允许部分减仓；普通反向K只观察。部分减仓比例是账户执行参数，不是书中固定比例。",
            "实际入场价是持仓平均成交成本；计划入场上限只表示未来再入场或加仓，不把两者混为一谈。",
            "计划点位可能受跳空、涨跌停、T+1 和流动性影响，不保证成交。",
        ],
    }
    plan["nextSessionPlan"] = _next_session_plan(
        action=action,
        order=order,
        levels=levels,
        position=position,
        checks=checks,
        blocked_reasons=blocked_reasons,
    )
    return plan


def _insufficient_history_plan(bars: list[dict[str, float | str]], rules: dict[str, Any], position: dict[str, Any] | None) -> dict[str, Any]:
    latest = bars[-1] if bars else {"date": "--", "close": 0.0}
    levels = _empty_levels(_number(latest.get("close")), rules)
    if position:
        cost_price = _number(position.get("costPrice"))
        cost_stop = cost_price * (1 - rules["hardStopPct"]) if cost_price > 0 else None
        levels.update(
            {
                "levelContext": "LONG_POSITION_MANAGEMENT",
                "entryPrice": _round_price(cost_price),
                "costPrice": _round_price(cost_price),
                "costStop": _round_price(cost_stop or 0),
                "protectiveStop": _round_price(cost_stop or 0),
                "activeDefense": _round_price(cost_stop or 0),
                "technicalStop": _round_price(cost_stop or 0),
                "accountRiskExit": _round_price(cost_stop or 0),
                "technicalStopAvailable": False,
                "initialStop": _round_price(cost_stop or 0),
                "hardStop": _round_price(cost_stop or 0),
                "initialStopSource": "数据不足后备",
                "initialStopReason": "日K不足，无法确认信号或结构；仅保留账户风险后备线，不生成技术入场和目标。",
                "entryPlanReason": "日K不足，实际入场价仍来自账户账本；补足数据后再生成未来入场观察价。",
                "firstTargetSource": "数据不足",
                "firstTargetReason": "日K不足，不能负责任地确认上方磁铁或生成止盈目标；补足数据后重新评估。",
                "takeProfitStatus": "DATA_INSUFFICIENT",
            }
        )
    return _base_plan(
        action="WAIT",
        action_label="数据不足，等待",
        tone="neutral",
        latest=latest,
        metrics={},
        market={"status": "unknown", "label": "指数未评估", "detail": "股票日K不足，不能生成纪律计划。", "allowNewPosition": False, "positionCapMultiplier": 0.0, "priceAction": {}},
        price_action={"environment": {"state": "DATA_INSUFFICIENT"}, "levels": [], "activeSetup": None, "assessment": {"action": "WAIT"}},
        checks=[_check("日线数据", False, f"至少需要 {MIN_ANALYSIS_BARS} 根完整日K，当前只有 {len(bars)} 根。")],
        reasons=["数据不足时不推断趋势、形态或点位。"],
        blocked_reasons=["日线数据不足"],
        position=position,
        order={"when": "补足本地完整日K后再评估", "priceRule": "没有有效订单", "shares": 0, "estimatedValue": 0, "stopPrice": None, "riskAmount": 0, "actionHint": "今天不下单。"},
        levels=levels,
        rules=rules,
    )


def _position_size(entry_price: float, stop_price: float, rules: dict[str, Any], market_multiplier: float) -> dict[str, float | int]:
    risk_per_share = max(entry_price - stop_price, entry_price * 0.001)
    risk_budget = rules["accountValue"] * rules["riskPerTradePct"]
    position_cap = rules["accountValue"] * rules["maxPositionPct"] * market_multiplier
    shares = _round_lot(
        min(int(risk_budget // risk_per_share), int(position_cap // entry_price), int(rules["availableCash"] // entry_price)),
        rules["lotSize"],
    )
    return {"shares": shares, "estimatedValue": _round_money(shares * entry_price), "riskAmount": _round_money(shares * risk_per_share)}


def _normalize_position(position: dict[str, Any] | None) -> dict[str, Any] | None:
    if not position:
        return None
    cost = _number(position.get("costPrice"))
    shares = int(_number(position.get("shares")))
    if cost <= 0 or shares <= 0:
        return None
    return {
        "symbol": str(position.get("symbol") or ""),
        "name": str(position.get("name") or ""),
        "entryPrice": _round_price(cost),
        "costPrice": _round_price(cost),
        "shares": shares,
        "createdAt": position.get("createdAt"),
        "updatedAt": position.get("updatedAt"),
        "trailingStop": _round_price(_number(position.get("trailingStop"))),
        "trailingPeak": _round_price(_number(position.get("trailingPeak"))),
        "trailingStage": _trailing_stage(position.get("trailingStage")),
        "trailingActive": bool(position.get("trailingActive")),
        "trailingActivationPrice": _round_price(_number(position.get("trailingActivationPrice"))),
        "trailingActivationAt": position.get("trailingActivationAt"),
        "trailingPeakAt": position.get("trailingPeakAt"),
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _round_lot(shares: float | int, lot_size: int) -> int:
    return max(0, int(shares) // lot_size * lot_size)


def _positive_number(value: float | int | None, fallback: float) -> float:
    number = _number(value)
    return number if number > 0 else fallback


def _non_negative_number(value: float | int | None, fallback: float) -> float:
    return max(0.0, _number(value)) if value is not None else fallback


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _round_price(value: float) -> float | None:
    return round(value, 3) if value > 0 else None


def _round_money(value: float) -> float:
    return round(value, 2)
