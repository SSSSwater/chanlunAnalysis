from __future__ import annotations

from .price_action import analyze_price_action
from .price_action_contract import validate_session_continuity


PERIODS = ("30", "15", "5")


def analyze_intraday(period_bars: dict[str, list[dict]]) -> dict:
    """Apply the same price-action contract to every configured intraday period."""

    periods: dict[str, dict] = {}
    for period, bars in period_bars.items():
        periods[period] = analyze_intraday_period(period, bars)

    valid = [item for item in periods.values() if item.get("rawKlines")]
    all_signals = [signal for item in valid for signal in item.get("priceAction", {}).get("signals", [])]
    all_setups = [setup for item in valid for setup in item.get("priceAction", {}).get("setups", [])]
    latest_signal = sorted(all_signals, key=lambda item: (str(item.get("date") or ""), int(item.get("index") or -1)))[-1] if all_signals else None
    buy_count = sum(1 for item in all_signals if item.get("direction") == "BUY")
    sell_count = sum(1 for item in all_signals if item.get("direction") == "SELL")
    bias = "等待"
    if buy_count > sell_count:
        bias = "偏多"
    elif sell_count > buy_count:
        bias = "偏下行（卖出管理观察）"

    cross_period = _cross_period_route(periods)
    return {
        "periods": periods,
        "summary": {
            "bias": bias,
            "periodCount": len(valid),
            "latestSignal": latest_signal,
            "actionableSetupCount": sum(1 for item in all_setups if item.get("status") == "CONFIRMED" and not item.get("reviewRequired")),
            "reviewRequiredSetupCount": sum(1 for item in all_setups if item.get("reviewRequired")),
            "signalCount": len(all_signals),
            "crossPeriodContext": "各周期均使用环境、位置、结构、确认和失效条件；周期结论不替代日线持仓计划。",
            "dayType": _day_type(periods),
            "openingContext": _opening_summary(periods),
            "sessionQuality": {period: (item.get("priceAction", {}).get("sessionQuality") or {}) for period, item in periods.items()},
            "crossPeriodOwnership": cross_period,
        },
    }


def analyze_intraday_period(period: str, bars: list[dict]) -> dict:
    result = analyze_price_action(bars, timeframe=f"{period}m")
    raw_klines = result.get("rawKlines") or []
    result["period"] = period
    result["dateRange"] = {
        "start": raw_klines[0].get("date", "") if raw_klines else "",
        "end": raw_klines[-1].get("date", "") if raw_klines else "",
    }
    pa = result.setdefault("priceAction", {})
    pa["timeWindow"] = (pa.get("sessionContext") or {}).get("timeWindow", "UNKNOWN")
    pa["sessionExpiry"] = "SESSION_CLOSE" if period in {"5", "15", "30"} else None
    pa["intradayDiscipline"] = [
        "仅使用已经收盘的分钟K；缺少首根/前三根K时不确认开盘结构。",
        "分钟计划在当日收盘前失效，不自动转成隔日计划。",
        "11:30、午盘重开和收盘前30-60分钟重新检查滑点、流动性和高周期磁力位。",
    ]
    return result


def _day_type(periods: dict[str, dict]) -> str:
    session_patterns = [
        pattern
        for item in periods.values()
        for pattern in item.get("priceAction", {}).get("sessionPatterns", [])
    ]
    confirmed = {str(pattern.get("type") or "") for pattern in session_patterns if pattern.get("status") == "CONFIRMED"}
    if "TREND_DAY" in confirmed:
        return "TREND_DAY"
    if "TREND_FROM_RANGE" in confirmed:
        return "TREND_FROM_RANGE"
    if "OPENING_REVERSAL" in confirmed:
        return "OPENING_REVERSAL"
    if "OPENING_TREND" in confirmed:
        return "OPENING_TREND"
    states = [str(item.get("priceAction", {}).get("environment", {}).get("state") or "") for item in periods.values()]
    if all(state == "RANGE" for state in states if state):
        return "RANGE_DAY_CANDIDATE"
    return "OPENING_OR_TRANSITION" if _opening_summary(periods).get("status") == "CONFIRMED" else "UNKNOWN"


def _opening_summary(periods: dict[str, dict]) -> dict:
    contexts = [item.get("priceAction", {}).get("openingContext") or {} for item in periods.values()]
    valid = [item for item in contexts if item.get("status") == "CONFIRMED"]
    if not valid:
        return {"status": "DATA_INSUFFICIENT", "reason": "缺少完整开盘区间或首根K"}
    first = valid[0]
    return {
        "status": "CONFIRMED",
        "openingRangeHigh": first.get("openingRangeHigh"),
        "openingRangeLow": first.get("openingRangeLow"),
        "gap": first.get("gap"),
        "dayType": first.get("dayType", "OPENING_RANGE"),
        "sessionPatterns": [
            pattern
            for item in periods.values()
            for pattern in item.get("priceAction", {}).get("sessionPatterns", [])
        ],
    }


def _cross_period_route(periods: dict[str, dict]) -> list[dict]:
    """Keep high-period ownership explicit when minute signals conflict."""

    order = {"30": 30, "15": 15, "5": 5}
    routes: list[dict] = []
    ranked = sorted(periods.items(), key=lambda item: order.get(item[0], 999))
    for period, item in ranked:
        environment = item.get("priceAction", {}).get("environment", {})
        state = environment.get("state")
        for signal in item.get("priceAction", {}).get("signals", []):
            owner = period
            disposition = "ALLOW_REVIEW"
            for higher_period, higher in ranked:
                if order.get(higher_period, 999) >= order.get(period, 999):
                    continue
                higher_state = higher.get("priceAction", {}).get("environment", {}).get("state")
                if higher_state in {"BULL_TREND", "BEAR_TREND"}:
                    expected = "BUY" if higher_state == "BULL_TREND" else "SELL"
                    if signal.get("direction") != expected:
                        owner = higher_period
                        disposition = "SCALP_OR_NEEDS_REVIEW"
                        break
            routes.append({"signalId": signal.get("signalId") or signal.get("type"), "sourcePeriod": period, "ownerPeriod": owner, "disposition": disposition, "executionConstraint": "CASH_LONG_ONLY" if signal.get("direction") == "BUY" else "EXISTING_LONG_ONLY" if signal.get("direction") == "SELL" else "NO_NEW_SHORT", "positionRequired": signal.get("direction") == "SELL", "shortSellingAllowed": False, "reason": "高周期强趋势优先；低周期反向信号不得升级为隔日计划。" if owner != period else "同向或无高周期冲突；卖出信号按持仓卖出/减仓纪律处理。" if signal.get("direction") == "SELL" else "同向或无高周期冲突。"})
    return routes
