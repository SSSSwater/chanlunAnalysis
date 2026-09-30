from __future__ import annotations

from statistics import fmean
from typing import Any

from .price_action_contract import (
    BarFacts,
    EnvironmentState,
    MagnetZone,
    Setup,
    SignalRecord,
    Structure,
    TradePlan,
    VolumeTurnoverContext,
    DEFAULT_PRICE_ACTION_PROFILE,
    BLOCK_REASONS,
    build_bar_facts,
    build_contract,
    build_session_context,
    build_trade_plan,
    build_volume_turnover_context,
    get_price_action_profile,
    market_mode_for_setup,
    session_for_timestamp,
    update_dynamic_stop,
)

# Re-export the canonical contract types from this long-standing service module;
# callers that already import price_action do not need a migration flag day.
PRICE_ACTION_PROFILE = DEFAULT_PRICE_ACTION_PROFILE


EMA_PERIOD = 20
ATR_PERIOD = 14
MIN_ANALYSIS_BARS = 35
SWING_RADIUS = 2
LEVEL_ZONE_ATR = 0.18
STOP_BUFFER_ATR = 0.2
ENTRY_BUFFER_ATR = 0.25
VOLUME_CONFIRM_RATIO = 1.1
STRUCTURE_LOOKBACKS = {
    "1d": 20,
    "30m": 48,
    "15m": 64,
    "5m": 96,
}

SETUP_DISPLAY_LABELS = {
    "TREND_PULLBACK_H1": "趋势回撤 H1",
    "TREND_PULLBACK_H2": "趋势回撤 H2",
    "BREAKOUT_RETEST_UP": "突破回踩再启动",
    "BREAKOUT_UP": "向上突破",
    "RANGE_LOWER_REVERSAL": "区间下沿反转",
    "RANGE_UPPER_REVERSAL": "区间上沿反转",
    "RANGE_MIDDLE": "区间中部观察",
    "DOUBLE_BOTTOM": "双底",
    "DOUBLE_TOP": "双顶",
    "FAILED_BULLISH_BREAKOUT": "向上突破失败",
    "FAILED_BEARISH_BREAKOUT": "向下突破失败",
    "BEAR_FLAG": "熊旗",
    "WEDGE_THIRD_PUSH": "楔形第三次推动",
    "MAJOR_BEARISH_REVERSAL": "主要向下反转",
    "MAJOR_BULLISH_REVERSAL": "主要向上反转",
}


def analyze_price_action(raw_bars: list[dict[str, Any]], timeframe: str = "1d") -> dict[str, Any]:
    """Return a deterministic, completed-bar price-action reading.

    The service deliberately limits itself to measurable evidence. Pattern quality,
    trader intent, and exhaustion remain visible as review-required conditions.
    """

    bars = normalize_bars(raw_bars)
    if len(bars) < MIN_ANALYSIS_BARS:
        return _insufficient_result(bars, raw_bars, timeframe)

    ema_values = _ema([bar["close"] for bar in bars], EMA_PERIOD)
    atr_values = _atr(bars, ATR_PERIOD)
    enriched = _enrich_raw_bars(bars, ema_values, atr_values)
    metrics = _metrics(bars, ema_values, atr_values)
    structure_bars = bars[-_structure_lookback(timeframe) :]
    volume = _volume_context(bars, structure_bars)
    metrics.update(
        {
            "volumeAvailable": volume["available"],
            "volumeBaseline": volume["baseline"],
            "volumeRecentVsPrior": volume["recentVsPrior"],
        }
    )
    swings = _detect_swings(structure_bars)
    range_info = _range_info(structure_bars, metrics["atr14"])
    environment = _classify_environment(bars, ema_values, metrics, range_info, volume)
    levels = _build_levels(bars, structure_bars, ema_values, metrics, swings, range_info, environment)
    setups = _detect_setups(bars, structure_bars, ema_values, metrics, range_info, environment, levels, swings, volume)
    setups = _apply_volume_quality(setups, volume)
    signals = _build_signals(bars, metrics, levels, setups, environment)
    future_plans = _build_future_plans(bars, metrics, levels, setups, signals, volume)
    assessment = _build_assessment(environment, setups, signals)

    price_action = {
        "version": "price-action-v1",
        "timeframe": timeframe,
        "environment": environment,
        "metrics": metrics,
        "structure": {
            "lookbackBars": len(structure_bars),
            "timeframe": timeframe,
            "label": f"最近{len(structure_bars)}根{_timeframe_label(timeframe)}完整K线",
            "method": "重叠度、摆动高低点、突破/回踩、反转K线、量能质量与ATR风险边界",
        },
        "volume": volume,
        "levels": levels,
        "setups": setups,
        "signals": signals,
        "futurePlans": future_plans,
        "assessment": assessment,
    }
    result = {
        "rawKlines": enriched,
        "priceAction": price_action,
        "summary": _build_summary(bars, price_action),
    }
    return build_contract(result, raw_bars, timeframe=timeframe)


def normalize_bars(raw_bars: list[dict[str, Any]]) -> list[dict[str, float | str]]:
    """Normalize and sort usable OHLCV bars without altering caller-owned data."""

    normalized: list[dict[str, float | str]] = []
    for item in raw_bars:
        try:
            open_price = float(item.get("open"))
            high = float(item.get("high"))
            low = float(item.get("low"))
            close = float(item.get("close"))
            volume = float(item.get("volume") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if min(open_price, high, low, close) <= 0 or high < low:
            continue
        normalized.append(
            {
                "date": str(item.get("date") or ""),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": max(volume, 0.0),
                "amount": item.get("amount"),
                "pctChange": item.get("pctChange", item.get("pct_change")),
                "priceChange": item.get("priceChange", item.get("price_change")),
                "floatShares": item.get("floatShares", item.get("float_shares")),
                "turnoverRate": item.get("turnoverRate", item.get("turnover_rate")),
                "source": item.get("source", item.get("provider", "unknown")),
                "provider": item.get("provider"),
                "adjustment": item.get("adjustment", item.get("adjust", "unknown")),
                "session": item.get("session"),
                "completed": item.get("completed", item.get("isCompleted", True)),
                "quality": item.get("quality", item.get("qualityFlags", [])),
            }
        )
    return sorted(normalized, key=lambda item: str(item["date"]))


def _structure_lookback(timeframe: str) -> int:
    return STRUCTURE_LOOKBACKS.get(str(timeframe), 48 if str(timeframe).endswith("m") else 20)


def _timeframe_label(timeframe: str) -> str:
    return "日" if timeframe == "1d" else f"{str(timeframe).removesuffix('m')}分钟"


def _setup_display_label(setup_type: str) -> str:
    return SETUP_DISPLAY_LABELS.get(str(setup_type), "该结构")


def _insufficient_result(bars: list[dict[str, float | str]], raw_bars: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    latest_date = str(bars[-1]["date"]) if bars else "--"
    raw_klines = [dict(item) for item in raw_bars]
    assessment = {
        "action": "WAIT",
        "label": "数据不足，等待",
        "reasons": [f"{timeframe} 需要至少 {MIN_ANALYSIS_BARS} 根完整K线，当前可用 {len(bars)} 根。"],
        "blockedReasons": ["无法确认趋势、区间和结构关键位"],
        "reviewRequired": False,
    }
    price_action = {
        "version": "price-action-v1",
        "timeframe": timeframe,
        "environment": {
            "state": "DATA_INSUFFICIENT",
            "label": "数据不足",
            "strength": "UNKNOWN",
            "evidence": assessment["reasons"],
            "reviewRequired": False,
        },
        "metrics": {},
        "structure": {
            "lookbackBars": len(bars),
            "timeframe": timeframe,
            "label": f"最近{len(bars)}根{_timeframe_label(timeframe)}完整K线",
            "method": "数据不足，暂不判断区间或点位",
        },
        "levels": [],
        "setups": [],
        "signals": [],
        "futurePlans": [],
        "assessment": assessment,
    }
    result = {
        "rawKlines": raw_klines,
        "priceAction": price_action,
        "summary": {
            "latestDate": latest_date,
            "latestClose": _round(float(bars[-1]["close"])) if bars else None,
            "environment": "DATA_INSUFFICIENT",
            "action": "WAIT",
            "signalCount": 0,
            "setupCount": 0,
        },
    }
    return build_contract(result, raw_bars, timeframe=timeframe)


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result: list[float] = []
    current: float | None = None
    for value in values:
        current = value if current is None else current + alpha * (value - current)
        result.append(current)
    return result


def _atr(bars: list[dict[str, float | str]], period: int) -> list[float]:
    values: list[float] = []
    current: float | None = None
    for index, bar in enumerate(bars):
        high = float(bar["high"])
        low = float(bar["low"])
        previous_close = float(bars[index - 1]["close"]) if index else float(bar["close"])
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        current = true_range if current is None else ((current * (period - 1)) + true_range) / period
        values.append(max(current, max(float(bar["close"]) * 0.001, 0.0001)))
    return values


def _enrich_raw_bars(
    bars: list[dict[str, float | str]], ema_values: list[float], atr_values: list[float]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        item = dict(bar)
        item["ema20"] = _round(ema_values[index])
        item["atr14"] = _round(atr_values[index])
        enriched.append(item)
    return enriched


def _metrics(bars: list[dict[str, float | str]], ema_values: list[float], atr_values: list[float]) -> dict[str, float]:
    recent = bars[-12:]
    overlaps = []
    for previous, current in zip(recent, recent[1:]):
        previous_high, previous_low = float(previous["high"]), float(previous["low"])
        current_high, current_low = float(current["high"]), float(current["low"])
        intersection = max(0.0, min(previous_high, current_high) - max(previous_low, current_low))
        denominator = max(min(previous_high - previous_low, current_high - current_low), 0.0001)
        overlaps.append(intersection / denominator)
    volume_values = [float(bar["volume"]) for bar in bars[-21:-1] if float(bar["volume"]) > 0]
    latest_volume = float(bars[-1]["volume"])
    volume_ratio = latest_volume / fmean(volume_values) if volume_values and latest_volume else 0.0
    atr = atr_values[-1]
    ema_slope = (ema_values[-1] - ema_values[max(0, len(ema_values) - 6)]) / max(atr, 0.0001)
    extension = (float(bars[-1]["close"]) - ema_values[-1]) / max(atr, 0.0001)
    return {
        "ema20": _round(ema_values[-1]),
        "emaSlope": _round(ema_slope),
        "atr14": _round(atr),
        "overlapRatio": _round(fmean(overlaps) if overlaps else 0.0),
        "volumeRatio": _round(volume_ratio),
        "extensionAtr": _round(extension),
        "latestClose": _round(float(bars[-1]["close"])),
        "recentMoveAtr": _round((float(bars[-1]["close"]) - float(bars[-12]["close"])) / max(atr, 0.0001)),
    }


def _volume_context(
    bars: list[dict[str, float | str]], structure_bars: list[dict[str, float | str]]
) -> dict[str, Any]:
    """Describe participation without treating volume as a directional signal."""

    prior_values = [float(bar["volume"]) for bar in bars[-21:-1] if float(bar["volume"]) > 0]
    recent_values = [float(bar["volume"]) for bar in structure_bars[-3:] if float(bar["volume"]) > 0]
    earlier_values = [float(bar["volume"]) for bar in structure_bars[-8:-3] if float(bar["volume"]) > 0]
    if len(prior_values) < 3:
        return {
            "available": False,
            "baseline": 0.0,
            "latestRatio": 0.0,
            "recentVsPrior": 0.0,
            "state": "UNAVAILABLE",
            "label": "量能不可用",
            "evidence": ["缺少足够的有效成交量，量能只作为待复核项。"],
        }

    baseline = fmean(prior_values)
    latest_ratio = float(bars[-1]["volume"]) / baseline if float(bars[-1]["volume"]) > 0 else 0.0
    recent_average = fmean(recent_values) if recent_values else 0.0
    earlier_average = fmean(earlier_values) if earlier_values else baseline
    recent_vs_prior = recent_average / earlier_average if earlier_average > 0 else 0.0
    if latest_ratio >= VOLUME_CONFIRM_RATIO:
        state, label = "EXPANSION", "放量参与"
    elif recent_vs_prior <= 0.85:
        state, label = "CONTRACTION", "回撤缩量"
    else:
        state, label = "NORMAL", "量能中性"
    return {
        "available": True,
        "baseline": _round(baseline),
        "latestRatio": _round(latest_ratio),
        "recentVsPrior": _round(recent_vs_prior),
        "state": state,
        "label": label,
        "evidence": [
            f"最新成交量为前20根均量 {latest_ratio:.2f} 倍，近3根相对前5根为 {recent_vs_prior:.2f} 倍。",
            "量能只辅助判断突破、反转和旗形质量，不单独生成方向。",
        ],
    }


def _detect_swings(bars: list[dict[str, float | str]]) -> list[dict[str, Any]]:
    swings: list[dict[str, Any]] = []
    for index in range(SWING_RADIUS, len(bars) - SWING_RADIUS):
        window = bars[index - SWING_RADIUS : index + SWING_RADIUS + 1]
        high = float(bars[index]["high"])
        low = float(bars[index]["low"])
        highs = [float(item["high"]) for item in window]
        lows = [float(item["low"]) for item in window]
        if high == max(highs) and highs.count(high) == 1:
            swings.append({"kind": "SWING_HIGH", "index": index, "date": bars[index]["date"], "price": high})
        if low == min(lows) and lows.count(low) == 1:
            swings.append({"kind": "SWING_LOW", "index": index, "date": bars[index]["date"], "price": low})
    return swings


def _range_info(bars: list[dict[str, float | str]], atr: float) -> dict[str, float | bool]:
    window = bars
    high = max(float(item["high"]) for item in window)
    low = min(float(item["low"]) for item in window)
    close = float(bars[-1]["close"])
    overlap = _range_overlap(window)
    width = max(high - low, atr)
    ema_slope_proxy = (float(window[-1]["close"]) - float(window[0]["close"])) / max(atr, 0.0001)
    is_range = overlap >= 0.46 and abs(ema_slope_proxy) <= 7.0
    return {
        "high": high,
        "low": low,
        "mid": (high + low) / 2,
        "width": width,
        "overlap": overlap,
        "isRange": is_range,
        "position": (close - low) / width,
    }


def _range_overlap(bars: list[dict[str, float | str]]) -> float:
    ratios: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        high = min(float(previous["high"]), float(current["high"]))
        low = max(float(previous["low"]), float(current["low"]))
        denominator = max(
            min(float(previous["high"]) - float(previous["low"]), float(current["high"]) - float(current["low"])),
            0.0001,
        )
        ratios.append(max(0.0, high - low) / denominator)
    return fmean(ratios) if ratios else 0.0


def _classify_environment(
    bars: list[dict[str, float | str]],
    ema_values: list[float],
    metrics: dict[str, float],
    range_info: dict[str, float | bool],
    volume: dict[str, Any],
) -> dict[str, Any]:
    close = float(bars[-1]["close"])
    ema = ema_values[-1]
    move = metrics["recentMoveAtr"]
    slope = metrics["emaSlope"]
    overlap = metrics["overlapRatio"]
    evidence = [f"收盘价 {close:.2f}，20EMA {ema:.2f}，EMA斜率 {slope:.2f} ATR。", *volume["evidence"][:1]]
    review_required = False

    if bool(range_info["isRange"]) and abs(slope) < 1.4:
        state, label, strength = "RANGE", "交易区间", "WEAK"
        evidence.append(f"近20根K线重叠度 {overlap:.2f}，上下边界 {float(range_info['low']):.2f}/{float(range_info['high']):.2f}。")
    elif close >= ema and slope >= 0.18 and move >= 1.5:
        state = "BULL_TREND"
        strength = "STRONG" if move >= 4.0 and overlap < 0.62 else "MODERATE"
        label = "多头趋势"
        evidence.append(f"近12根K线向上移动 {move:.2f} ATR，价格位于20EMA上方。")
    elif close <= ema and slope <= -0.18 and move <= -1.5:
        state = "BEAR_TREND"
        strength = "STRONG" if move <= -4.0 and overlap < 0.62 else "MODERATE"
        label = "下行趋势"
        evidence.append(f"近12根K线向下移动 {abs(move):.2f} ATR，价格位于20EMA下方。")
    else:
        state, label, strength = "TRANSITION", "过渡/方向不清", "WEAK"
        review_required = True
        evidence.append("趋势、区间或转折的数值证据不充分，需要人工复核。")

    return {
        "state": state,
        "label": label,
        "strength": strength,
        "evidence": evidence,
        "volume": volume,
        "reviewRequired": review_required,
    }


def _build_levels(
    bars: list[dict[str, float | str]],
    structure_bars: list[dict[str, float | str]],
    ema_values: list[float],
    metrics: dict[str, float],
    swings: list[dict[str, Any]],
    range_info: dict[str, float | bool],
    environment: dict[str, Any],
) -> list[dict[str, Any]]:
    close = float(bars[-1]["close"])
    atr = metrics["atr14"]
    zone = max(atr * LEVEL_ZONE_ATR, close * 0.001)
    levels: list[dict[str, Any]] = []

    levels.append(
        _level(
            kind="EMA20",
            role="SUPPORT" if close >= ema_values[-1] else "RESISTANCE",
            price=ema_values[-1],
            source="20周期EMA",
            close=close,
            atr=atr,
            zone=zone,
        )
    )
    recent_highs = [item for item in swings if item["kind"] == "SWING_HIGH"][-3:]
    recent_lows = [item for item in swings if item["kind"] == "SWING_LOW"][-3:]
    for swing in recent_highs:
        levels.append(_level("SWING_HIGH", "RESISTANCE_MAGNET", float(swing["price"]), f"确认摆动高点 {swing['date']}", close, atr, zone))
    for swing in recent_lows:
        levels.append(_level("SWING_LOW", "SUPPORT_MAGNET", float(swing["price"]), f"确认摆动低点 {swing['date']}", close, atr, zone))

    if bool(range_info["isRange"]):
        range_low, range_high, range_mid = float(range_info["low"]), float(range_info["high"]), float(range_info["mid"])
        levels.extend(
            [
                _level("RANGE_LOW", "SUPPORT_MAGNET", range_low, "近20根K线区间下沿", close, atr, zone),
                _level("RANGE_MID", "RANGE_MIDDLE_MAGNET", range_mid, "近20根K线区间中轴", close, atr, zone),
                _level("RANGE_HIGH", "RESISTANCE_MAGNET", range_high, "近20根K线区间上沿", close, atr, zone),
            ]
        )

    previous = bars[-2]
    latest = bars[-1]
    gap = float(latest["low"]) - float(previous["high"])
    if abs(gap) >= atr * 0.7:
        edge = float(previous["high"]) if gap > 0 else float(previous["low"])
        role = "SUPPORT_MAGNET" if gap > 0 else "RESISTANCE_MAGNET"
        levels.append(_level("GAP_EDGE", role, edge, "最近跳空缺口边缘", close, atr, zone))

    prior_window = structure_bars[:-2] or structure_bars
    prior_high = max(float(item["high"]) for item in prior_window)
    prior_low = min(float(item["low"]) for item in prior_window)
    prior_width = max(prior_high - prior_low, atr)
    if close > prior_high:
        levels.append(_level("MEASURED_MOVE_UP", "UPSIDE_MAGNET", prior_high + prior_width, "前20根K线区间测量目标", close, atr, zone))
    elif close < prior_low:
        levels.append(_level("MEASURED_MOVE_DOWN", "DOWNSIDE_MAGNET", prior_low - prior_width, "前20根K线区间测量目标", close, atr, zone))

    return _dedupe_levels(levels)


def _level(kind: str, role: str, price: float, source: str, close: float, atr: float, zone: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "role": role,
        "price": _round(price),
        "zoneLow": _round(price - zone),
        "zoneHigh": _round(price + zone),
        "source": source,
        "distanceAtr": _round((price - close) / max(atr, 0.0001)),
    }


def _dedupe_levels(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for level in levels:
        if any(level["kind"] == item["kind"] and abs(level["price"] - item["price"]) < 0.001 for item in selected):
            continue
        selected.append(level)
    return sorted(selected, key=lambda item: (abs(float(item["distanceAtr"])), item["kind"]))


def _detect_setups(
    bars: list[dict[str, float | str]],
    structure_bars: list[dict[str, float | str]],
    ema_values: list[float],
    metrics: dict[str, float],
    range_info: dict[str, float | bool],
    environment: dict[str, Any],
    levels: list[dict[str, Any]],
    swings: list[dict[str, Any]],
    volume: dict[str, Any],
) -> list[dict[str, Any]]:
    latest = bars[-1]
    previous = bars[-2]
    atr = metrics["atr14"]
    close = float(latest["close"])
    setups: list[dict[str, Any]] = []
    prior_window = structure_bars[:-2] or structure_bars
    prior_high = max(float(item["high"]) for item in prior_window)
    prior_low = min(float(item["low"]) for item in prior_window)
    bull_bar = _bullish_bar(latest)
    bear_bar = _bearish_bar(latest)
    upper_close = _close_location(latest) >= 0.62
    lower_close = _close_location(latest) <= 0.38

    recent_breakout_bars = structure_bars[-4:-1] or structure_bars
    breakout_seen = max(float(item["high"]) for item in recent_breakout_bars) > prior_high + atr * 0.08
    failed_bull = breakout_seen and close < prior_high - atr * 0.08 and lower_close
    failed_bear = min(float(item["low"]) for item in recent_breakout_bars) < prior_low - atr * 0.08 and close > prior_low + atr * 0.08 and upper_close
    if failed_bull:
        failure_high = max(float(item["high"]) for item in bars[-4:])
        setups.append(
            _setup(
                "FAILED_BULLISH_BREAKOUT",
                "SELL",
                "CONFIRMED",
                "突破后回落到原阻力位内",
                ["此前出现向上突破", "收盘回到突破位下方", "弱收盘确认多头突破失败"],
                [],
                False,
                trigger=min(float(latest["low"]), float(previous["low"])),
                invalidation=failure_high + atr * STOP_BUFFER_ATR,
                structure_low=float(latest["low"]),
            )
        )
    elif failed_bear:
        failure_low = min(float(item["low"]) for item in bars[-4:])
        setups.append(
            _setup(
                "FAILED_BEARISH_BREAKOUT",
                "BUY",
                "CONFIRMED",
                "突破后重新站回原支撑位",
                    ["此前出现向下突破", "收盘重新回到支撑位上方", "强收盘确认向下突破失败"],
                [],
                False,
                trigger=max(float(latest["high"]), float(previous["high"])),
                invalidation=failure_low - atr * STOP_BUFFER_ATR,
                structure_low=failure_low,
            )
        )

    last_two_above = float(previous["close"]) > prior_high and close > prior_high
    retest_holds = float(latest["low"]) <= prior_high + atr * 0.7 and close > prior_high and bull_bar
    if last_two_above and retest_holds:
        setups.append(
            _setup(
                "BREAKOUT_RETEST_UP",
                "BUY",
                "CONFIRMED",
                "前高突破位回踩",
                ["两根K线收于前高之上", "回踩未跌回原区间", "最新K线向上收盘"],
                [],
                False,
                trigger=max(float(latest["high"]), prior_high),
                invalidation=min(float(latest["low"]), prior_high) - atr * STOP_BUFFER_ATR,
                structure_low=min(float(latest["low"]), prior_high),
            )
        )
    elif close > prior_high + atr * 0.1:
        setups.append(
            _setup(
                "BREAKOUT_UP",
                "BUY",
                "PENDING",
                "前高/区间上沿突破",
                ["收盘突破前高或区间上沿"],
                ["等待后续K线守住突破位或出现合格回踩"],
                False,
                trigger=float(latest["high"]),
                invalidation=prior_high - atr * STOP_BUFFER_ATR,
                structure_low=prior_high,
            )
        )

    if environment["state"] == "BULL_TREND" and not failed_bull:
        recent_lows = [float(item["low"]) for item in structure_bars[-7:]]
        near_ema = min(recent_lows) <= ema_values[-1] + atr * 0.8
        countertrend_bars = sum(1 for item in structure_bars[-6:-1] if float(item["close"]) < float(item["open"]))
        attempt = "H2" if countertrend_bars >= 2 else "H1"
        pullback_low = min(recent_lows)
        confirmed = near_ema and bull_bar and close > float(previous["high"])
        if near_ema:
            setups.append(
                _setup(
                    f"TREND_PULLBACK_{attempt}",
                    "BUY",
                    "CONFIRMED" if confirmed else "PENDING",
                    "20EMA及近期回撤低点附近",
                    ["多头趋势环境", "回撤接近20EMA", f"{attempt} 顺势尝试"],
                    [] if confirmed else ["等待顺势信号K突破或后续跟随"],
                    False,
                    trigger=max(float(latest["high"]), float(previous["high"])),
                    invalidation=pullback_low - atr * STOP_BUFFER_ATR,
                    structure_low=pullback_low,
                )
            )

    if environment["state"] == "RANGE":
        range_low, range_high, width = float(range_info["low"]), float(range_info["high"]), float(range_info["width"])
        at_lower_edge = close <= range_low + width * 0.32 or float(latest["low"]) <= range_low + atr * 0.35
        at_upper_edge = close >= range_high - width * 0.32 or float(latest["high"]) >= range_high - atr * 0.35
        if at_lower_edge:
            confirmed = bull_bar and close > float(previous["high"])
            setups.append(
                _setup(
                    "RANGE_LOWER_REVERSAL",
                    "BUY",
                    "CONFIRMED" if confirmed else "PENDING",
                    "交易区间下沿",
                    ["交易区间环境", "价格处于区间下沿", "出现向上反转尝试"],
                    [] if confirmed else ["等待反转K线突破或后续跟随"],
                    False,
                    trigger=max(float(latest["high"]), float(previous["high"])),
                    invalidation=range_low - atr * STOP_BUFFER_ATR,
                    structure_low=range_low,
                )
            )
        elif at_upper_edge:
            confirmed = bear_bar and close < float(previous["low"])
            setups.append(
                _setup(
                    "RANGE_UPPER_REVERSAL",
                    "SELL",
                    "CONFIRMED" if confirmed else "PENDING",
                    "交易区间上沿",
                    ["交易区间环境", "价格处于区间上沿", "出现向下反转尝试"],
                    [] if confirmed else ["等待反转K线跌破或后续跟随"],
                    False,
                    trigger=min(float(latest["low"]), float(previous["low"])),
                    invalidation=range_high + atr * STOP_BUFFER_ATR,
                    structure_low=range_high,
                )
            )
        else:
            setups.append(
                _setup(
                    "RANGE_MIDDLE",
                    "NONE",
                    "WATCH",
                    "交易区间中部",
                    ["价格没有处于可定义风险的区间边缘"],
                    ["等待区间边缘失败突破或已确认突破回踩"],
                    False,
                )
            )

    double_setup = _detect_double_test(swings, structure_bars, bars, atr, environment)
    if double_setup:
        setups.append(double_setup)
    bear_flag_setup = _detect_bear_flag(structure_bars, bars, atr, environment)
    if bear_flag_setup:
        setups.append(bear_flag_setup)
    wedge_setup = _detect_wedge(swings, structure_bars, bars, atr, environment)
    if wedge_setup:
        setups.append(wedge_setup)
    reversal = _detect_major_reversal(bars, ema_values, atr, environment, failed_bull, failed_bear)
    if reversal:
        setups.append(reversal)
    return _dedupe_setups(setups)


def _detect_double_test(
    swings: list[dict[str, Any]],
    structure_bars: list[dict[str, float | str]],
    bars: list[dict[str, float | str]],
    atr: float,
    environment: dict[str, Any],
) -> dict[str, Any] | None:
    lows = [item for item in swings if item["kind"] == "SWING_LOW"][-2:]
    highs = [item for item in swings if item["kind"] == "SWING_HIGH"][-2:]
    latest = bars[-1]
    previous = bars[-2]
    if len(lows) == 2 and abs(float(lows[-1]["price"]) - float(lows[-2]["price"])) <= atr * 0.75:
        neckline = max(
            float(item["high"])
            for item in structure_bars[lows[-2]["index"] : lows[-1]["index"] + 1]
        )
        confirmed = _bullish_bar(latest) and float(latest["close"]) > neckline
        return _setup(
            "DOUBLE_BOTTOM",
            "BUY",
            "CONFIRMED" if confirmed else "PENDING",
            "两次相近低点测试",
            ["两个确认摆动低点的差异小于0.75 ATR", "最新价格处于第二次测试之后"],
            [] if confirmed else [f"等待收盘突破双底颈线 {neckline:.2f} 并有向上跟随"],
            environment["state"] == "BEAR_TREND",
            trigger=neckline,
            invalidation=min(float(lows[-1]["price"]), float(lows[-2]["price"])) - atr * STOP_BUFFER_ATR,
            structure_low=min(float(lows[-1]["price"]), float(lows[-2]["price"])),
        )
    if len(highs) == 2 and abs(float(highs[-1]["price"]) - float(highs[-2]["price"])) <= atr * 0.75:
        neckline = min(
            float(item["low"])
            for item in structure_bars[highs[-2]["index"] : highs[-1]["index"] + 1]
        )
        confirmed = _bearish_bar(latest) and float(latest["close"]) < neckline
        return _setup(
            "DOUBLE_TOP",
            "SELL",
            "CONFIRMED" if confirmed else "PENDING",
            "两次相近高点测试",
            ["两个确认摆动高点的差异小于0.75 ATR", "最新价格处于第二次测试之后"],
            [] if confirmed else [f"等待收盘跌破双顶颈线 {neckline:.2f} 并有向下跟随"],
            environment["state"] == "BULL_TREND",
            trigger=neckline,
            invalidation=max(float(highs[-1]["price"]), float(highs[-2]["price"])) + atr * STOP_BUFFER_ATR,
            structure_low=max(float(highs[-1]["price"]), float(highs[-2]["price"])),
        )
    return None


def _detect_wedge(
    swings: list[dict[str, Any]],
    structure_bars: list[dict[str, float | str]],
    bars: list[dict[str, float | str]],
    atr: float,
    environment: dict[str, Any],
) -> dict[str, Any] | None:
    if environment["state"] not in {"BULL_TREND", "BEAR_TREND"}:
        return None
    kind = "SWING_HIGH" if environment["state"] == "BULL_TREND" else "SWING_LOW"
    points = [item for item in swings if item["kind"] == kind][-3:]
    if len(points) < 3:
        return None
    prices = [float(item["price"]) for item in points]
    advancing = prices[0] < prices[1] < prices[2] if kind == "SWING_HIGH" else prices[0] > prices[1] > prices[2]
    if not advancing:
        return None
    direction = "SELL" if kind == "SWING_HIGH" else "BUY"
    latest = bars[-1]
    previous = bars[-2]
    segment = structure_bars[points[-2]["index"] : points[-1]["index"] + 1]
    if kind == "SWING_HIGH":
        trigger = min(float(item["low"]) for item in segment)
        confirmed = _bearish_bar(latest) and float(latest["close"]) < trigger
        invalidation = float(points[-1]["price"]) + atr * STOP_BUFFER_ATR
        condition = f"等待收盘跌破第三次推动回撤低点 {trigger:.2f} 并有跟随"
        structure_price = trigger
    else:
        trigger = max(float(item["high"]) for item in segment)
        confirmed = _bullish_bar(latest) and float(latest["close"]) > trigger
        invalidation = float(points[-1]["price"]) - atr * STOP_BUFFER_ATR
        condition = f"等待收盘突破第三次推动回撤高点 {trigger:.2f} 并有跟随"
        structure_price = trigger
    return _setup(
        "WEDGE_THIRD_PUSH",
        direction,
        "CONFIRMED" if confirmed else "WATCH",
        "趋势第三次推动附近",
        ["已确认至少三次同向摆动推进", "第三次推动的追价风险升高"],
        [] if confirmed else [condition, "不能仅凭第三次推动反转"],
        not confirmed,
        trigger=trigger,
        invalidation=invalidation,
        structure_low=structure_price,
    )


def _detect_bear_flag(
    structure_bars: list[dict[str, float | str]],
    bars: list[dict[str, float | str]],
    atr: float,
    environment: dict[str, Any],
) -> dict[str, Any] | None:
    """Identify a compact pullback after a downside impulse as a future exit anchor.

    A bear flag is not an automatic short instruction for A-share cash accounts.  It
    becomes a SELL-oriented plan only after a completed bar breaks the flag floor;
    in an otherwise bullish environment it remains review-required defensive context.
    """

    if len(structure_bars) < 8 or atr <= 0:
        return None

    impulse_bars = structure_bars[-8:-3]
    flag_bars = structure_bars[-3:-1]
    latest = bars[-1]
    impulse_high = max(float(item["high"]) for item in impulse_bars)
    impulse_low = min(float(item["low"]) for item in impulse_bars)
    impulse_drop = impulse_high - impulse_low
    minimum_impulse = max(atr * 2.0, impulse_high * 0.018)
    bearish_impulse_bars = sum(1 for item in impulse_bars if _bearish_bar(item))
    if impulse_drop < minimum_impulse or bearish_impulse_bars < 2:
        return None

    flag_high = max(float(item["high"]) for item in flag_bars)
    flag_low = min(float(item["low"]) for item in flag_bars)
    flag_retrace = (flag_high - impulse_low) / impulse_drop if impulse_drop else 1.0
    flag_width = flag_high - flag_low
    if flag_retrace > 0.7 or flag_high >= impulse_high - atr * 0.12:
        return None
    if flag_width > max(atr * 2.4, impulse_drop * 0.7):
        return None

    close = float(latest["close"])
    confirmed = _bearish_bar(latest) and close < flag_low - atr * 0.05
    still_below_impulse = close < impulse_high - atr * 0.12
    if not confirmed and not still_below_impulse:
        return None

    requires_review = environment["state"] == "BULL_TREND"
    return _setup(
        "BEAR_FLAG",
        "SELL",
        "CONFIRMED" if confirmed and not requires_review else "PENDING",
        "下跌推动后的回抽旗面",
        [
            f"前5根K线形成约 {impulse_drop / atr:.1f} ATR 的下跌推动",
            "旗面回抽没有收复推动起点",
            f"旗面回撤约为推动幅度的 {flag_retrace * 100:.0f}%",
        ],
        [] if confirmed and not requires_review else [f"等待收盘跌破熊旗旗面下沿 {flag_low:.2f} 并有向下跟随"],
        requires_review,
        trigger=flag_low,
        invalidation=flag_high + atr * STOP_BUFFER_ATR,
        structure_low=flag_low,
    )


def _detect_major_reversal(
    bars: list[dict[str, float | str]],
    ema_values: list[float],
    atr: float,
    environment: dict[str, Any],
    failed_bull: bool,
    failed_bear: bool,
) -> dict[str, Any] | None:
    latest, previous = bars[-1], bars[-2]
    prior_ema_rising = ema_values[-3] > ema_values[-8]
    prior_ema_falling = ema_values[-3] < ema_values[-8]
    bearish_break = _bearish_bar(latest) and float(latest["close"]) < ema_values[-1] and float(previous["close"]) >= ema_values[-2]
    bullish_break = _bullish_bar(latest) and float(latest["close"]) > ema_values[-1] and float(previous["close"]) <= ema_values[-2]
    if failed_bull or (prior_ema_rising and bearish_break):
        confirmed = failed_bull
        return _setup(
            "MAJOR_BEARISH_REVERSAL",
            "SELL",
            "CONFIRMED" if confirmed else "NEEDS_REVIEW",
            "趋势线/EMA破坏后的反转候选",
            ["此前20EMA处于上行", "出现强向下收盘并回到EMA下方"],
            [] if confirmed else ["需要第二次测试失败或反向跟随，单根反向K不足以确认反转"],
            not confirmed,
            trigger=min(float(latest["low"]), float(previous["low"])),
            invalidation=max(float(latest["high"]), float(previous["high"])) + atr * STOP_BUFFER_ATR,
            structure_low=float(latest["low"]),
        )
    if failed_bear or (prior_ema_falling and bullish_break):
        confirmed = failed_bear
        return _setup(
            "MAJOR_BULLISH_REVERSAL",
            "BUY",
            "CONFIRMED" if confirmed else "NEEDS_REVIEW",
            "趋势线/EMA破坏后的反转候选",
            ["此前20EMA处于下行", "出现强向上收盘并回到EMA上方"],
            [] if confirmed else ["需要第二次测试成功或向上跟随，单根反向K不足以确认反转"],
            not confirmed,
            trigger=max(float(latest["high"]), float(previous["high"])),
            invalidation=min(float(latest["low"]), float(previous["low"])) - atr * STOP_BUFFER_ATR,
            structure_low=float(latest["low"]),
        )
    return None


def _setup(
    setup_type: str,
    direction: str,
    status: str,
    location: str,
    evidence: list[str],
    missing_conditions: list[str],
    review_required: bool,
    *,
    trigger: float | None = None,
    invalidation: float | None = None,
    structure_low: float | None = None,
) -> dict[str, Any]:
    return {
        "type": setup_type,
        "direction": direction,
        "status": status,
        "location": location,
        "evidence": evidence,
        "missingConditions": missing_conditions,
        "reviewRequired": review_required,
        "triggerPrice": _round(trigger) if trigger else None,
        "invalidationPrice": _round(invalidation) if invalidation else None,
        "structurePrice": _round(structure_low) if structure_low else None,
    }


def _apply_volume_quality(setups: list[dict[str, Any]], volume: dict[str, Any]) -> list[dict[str, Any]]:
    """Use volume as structural quality evidence, never as a standalone direction."""

    confirmation_types = {
        "BREAKOUT_UP",
        "BREAKOUT_RETEST_UP",
        "FAILED_BULLISH_BREAKOUT",
        "FAILED_BEARISH_BREAKOUT",
        "BEAR_FLAG",
        "MAJOR_BEARISH_REVERSAL",
        "MAJOR_BULLISH_REVERSAL",
    }
    for setup in setups:
        needs_volume_confirmation = setup["type"] in confirmation_types
        setup["volume"] = {
            "role": "确认结构质量" if needs_volume_confirmation else "辅助结构判断",
            "available": volume["available"],
            "state": volume["state"],
            "label": volume["label"],
            "latestRatio": volume["latestRatio"],
            "recentVsPrior": volume["recentVsPrior"],
            "evidence": list(volume["evidence"]),
            "qualified": bool(volume["available"] and volume["latestRatio"] >= VOLUME_CONFIRM_RATIO),
        }
        if not needs_volume_confirmation:
            continue
        if not volume["available"]:
            if setup["status"] == "CONFIRMED":
                setup["status"] = "PENDING"
            setup["reviewRequired"] = True
            setup["missingConditions"].append("量能不可用，等待成交量恢复后复核结构质量")
            continue
        if volume["latestRatio"] < VOLUME_CONFIRM_RATIO:
            if setup["status"] == "CONFIRMED":
                setup["status"] = "PENDING"
            setup["reviewRequired"] = True
            setup["missingConditions"].append(
                f"等待突破/反转日成交量达到前20根均量 {VOLUME_CONFIRM_RATIO:.2f} 倍附近"
            )
    return setups


def _future_anchor_meta(setup: dict[str, Any], anchor_price: float | None) -> dict[str, Any]:
    labels = {
        "DOUBLE_TOP": ("双顶颈线确认", "CONFIRMATION"),
        "DOUBLE_BOTTOM": ("双底颈线确认", "CONFIRMATION"),
        "WEDGE_THIRD_PUSH": ("第三次推动反向突破", "CONFIRMATION"),
        "BREAKOUT_UP": ("突破位站稳/回踩", "CONFIRMATION"),
        "BREAKOUT_RETEST_UP": ("突破回踩再启动", "EXECUTION"),
        "FAILED_BULLISH_BREAKOUT": ("上破失败后的向下跟随", "EXECUTION"),
        "FAILED_BEARISH_BREAKOUT": ("下破失败后的向上跟随", "EXECUTION"),
        "BEAR_FLAG": ("熊旗下沿确认", "CONFIRMATION"),
        "RANGE_LOWER_REVERSAL": ("区间下沿反转触发", "CONFIRMATION"),
        "RANGE_UPPER_REVERSAL": ("区间上沿反转触发", "CONFIRMATION"),
        "RANGE_MIDDLE": ("下一次区间边缘测试", "OBSERVATION"),
        "TREND_PULLBACK_H1": ("趋势回撤 H1 触发", "EXECUTION"),
        "TREND_PULLBACK_H2": ("趋势回撤 H2 触发", "EXECUTION"),
        "MAJOR_BEARISH_REVERSAL": ("主要向下反转确认", "CONFIRMATION"),
        "MAJOR_BULLISH_REVERSAL": ("主要向上反转确认", "CONFIRMATION"),
    }
    label, anchor_type = labels.get(setup["type"], ("结构确认锚点", "CONFIRMATION"))
    condition = (setup.get("missingConditions") or ["达到锚点后等待收盘和后续跟随"])[0]
    if setup["status"] == "CONFIRMED":
        condition = "价格条件已经满足；后续以失效价、第一磁铁和取消条件管理"
    return {
        "anchorType": anchor_type,
        "anchorLabel": label,
        "anchorPrice": _round(anchor_price) if anchor_price else None,
        "anchorCondition": condition,
    }


def _build_future_plans(
    bars: list[dict[str, float | str]],
    metrics: dict[str, float],
    levels: list[dict[str, Any]],
    setups: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    volume: dict[str, Any],
) -> list[dict[str, Any]]:
    """Turn every candidate setup into an explicit future price anchor plan."""

    signal_by_type = {item["type"]: item for item in signals}
    close = float(bars[-1]["close"])
    plans: list[dict[str, Any]] = []
    for setup in setups:
        signal = signal_by_type.get(setup["type"])
        direction = setup["direction"]
        market_mode, market_mode_label = market_mode_for_setup(setup.get("type"))
        anchor_price = setup.get("triggerPrice")
        if setup["type"] == "RANGE_MIDDLE" and not anchor_price:
            candidates = [
                item for item in levels if item["kind"] in {"RANGE_LOW", "RANGE_HIGH"} and float(item["price"]) > 0
            ]
            if candidates:
                anchor_price = min(candidates, key=lambda item: abs(float(item["price"]) - close))["price"]
        entry_limit = None
        first_target = None
        extension_target = None
        if signal:
            entry_limit = signal.get("entryLimit")
            first_target = signal.get("firstTarget")
            extension_target = signal.get("extensionTarget")
        elif direction == "BUY" and anchor_price and setup.get("invalidationPrice"):
            entry_limit = anchor_price + metrics["atr14"] * ENTRY_BUFFER_ATR
            first_target = _find_target(levels, entry_limit, direction, metrics["atr14"], setup)
            extension_target = _find_extension_target(levels, entry_limit, first_target, metrics["atr14"], market_mode)
        if direction == "NONE" and anchor_price:
            first_target = anchor_price
        anchor = _future_anchor_meta(setup, float(anchor_price) if anchor_price else None)
        invalidation = signal.get("invalidationPrice") if signal else setup.get("invalidationPrice")
        cancellation = signal.get("cancellationConditions") if signal else [
            f"若价格在确认前失守结构失效价 {float(invalidation):.2f}，取消本计划。" if invalidation else "若结构边界失效，取消本计划。",
            f"若{_setup_display_label(setup['type'])}没有后续跟随，回到等待状态。",
        ]
        plans.append(
            {
                "type": setup["type"],
                "direction": direction,
                "executionAction": "BUY_LONG" if direction == "BUY" else "SELL_EXISTING_LONG",
                "executionConstraint": "CASH_LONG_ONLY" if direction == "BUY" else "EXISTING_LONG_ONLY",
                "positionRequired": direction == "SELL",
                "shortSellingAllowed": False,
                "executable": direction == "BUY",
                "targetSemantics": "LONG_PROFIT_TARGET" if direction == "BUY" else "DOWNSIDE_REFERENCE_ONLY",
                "marketMode": market_mode,
                "marketModeLabel": market_mode_label,
                "status": signal["status"] if signal else setup["status"],
                "reviewRequired": bool(signal.get("reviewRequired")) if signal else setup["reviewRequired"],
                "date": str(bars[-1]["date"]),
                "index": len(bars) - 1,
                "location": setup["location"],
                "evidence": list(setup["evidence"]),
                "missingConditions": list(setup["missingConditions"]),
                "reasons": list(signal.get("reasons") or setup["evidence"]) if signal else list(setup["evidence"]),
                **anchor,
                "triggerPrice": _round(anchor_price) if anchor_price else None,
                "entryLimit": _round(entry_limit) if entry_limit else None,
                "invalidationPrice": _round(invalidation) if invalidation else None,
                "protectiveStop": _round(signal.get("protectiveStop")) if signal and signal.get("protectiveStop") else _round(invalidation) if invalidation else None,
                "firstTarget": _round(first_target) if first_target else None,
                "extensionTarget": _round(extension_target) if extension_target else None,
                "cancellationConditions": cancellation,
                "volume": setup.get("volume") or volume,
            }
        )
    priority = {"CONFIRMED": 0, "PENDING": 1, "NEEDS_REVIEW": 2, "WATCH": 3}
    return sorted(plans, key=lambda item: (priority.get(item["status"], 4), item["type"]))


def _dedupe_setups(setups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for setup in setups:
        if any(item["type"] == setup["type"] for item in selected):
            continue
        selected.append(setup)
    return selected


def _build_signals(
    bars: list[dict[str, float | str]],
    metrics: dict[str, float],
    levels: list[dict[str, Any]],
    setups: list[dict[str, Any]],
    environment: dict[str, Any],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    latest = bars[-1]
    for setup in setups:
        if setup["status"] != "CONFIRMED" or setup["reviewRequired"] or setup["direction"] not in {"BUY", "SELL"}:
            continue
        trigger = setup.get("triggerPrice")
        invalidation = setup.get("invalidationPrice")
        if not trigger or not invalidation:
            continue
        direction = setup["direction"]
        market_mode, market_mode_label = market_mode_for_setup(setup.get("type"))
        if direction == "SELL":
            entry_limit = first_target = extension = None
        else:
            entry_limit = trigger + metrics["atr14"] * ENTRY_BUFFER_ATR
            first_target = _find_target(levels, entry_limit, direction, metrics["atr14"], setup)
            if first_target is None:
                continue
            extension = _find_extension_target(levels, entry_limit, first_target, metrics["atr14"], market_mode)
        reasons = [environment["label"], *setup["evidence"][:2]]
        cancellation = _cancellation_conditions(direction, entry_limit, invalidation, setup)
        signals.append(
            {
                "type": setup["type"],
                "direction": direction,
                "executionAction": "BUY_LONG" if direction == "BUY" else "SELL_EXISTING_LONG",
                "executionConstraint": "CASH_LONG_ONLY" if direction == "BUY" else "EXISTING_LONG_ONLY",
                "positionRequired": direction == "SELL",
                "shortSellingAllowed": False,
                "executable": direction == "BUY",
                "targetSemantics": "LONG_PROFIT_TARGET" if direction == "BUY" else "DOWNSIDE_REFERENCE_ONLY",
                "marketMode": market_mode,
                "marketModeLabel": market_mode_label,
                "status": "CONFIRMED",
                "date": str(latest["date"]),
                "index": len(bars) - 1,
                "triggerPrice": _round(trigger),
                "entryLimit": _round(entry_limit) if entry_limit else None,
                "invalidationPrice": _round(invalidation),
                "protectiveStop": _round(invalidation) if direction == "BUY" else None,
                "firstTarget": _round(first_target) if first_target else None,
                "extensionTarget": _round(extension) if extension else None,
                "cancellationConditions": cancellation,
                "reasons": reasons,
                "reviewRequired": False,
            }
        )
    return signals


def _find_target(
    levels: list[dict[str, Any]], entry: float, direction: str, atr: float, setup: dict[str, Any]
) -> float | None:
    if direction == "BUY":
        candidates = [float(item["price"]) for item in levels if float(item["price"]) > entry + atr * 0.15]
        fallback = entry + max(atr * 2.2, abs(entry - float(setup["invalidationPrice"])) * 2)
        return min(candidates) if candidates else fallback
    return None


def _find_extension_target(
    levels: list[dict[str, Any]], entry: float, first_target: float | None, atr: float, market_mode: str
) -> float | None:
    """Return only a confirmed next magnet in trend continuation mode."""

    if market_mode != "TREND" or not first_target:
        return None
    candidates = [
        float(item["price"])
        for item in levels
        if float(item.get("price") or 0) > first_target + atr * 0.15
        and ("RESISTANCE" in str(item.get("role") or "") or "UPSIDE" in str(item.get("role") or ""))
    ]
    return min(candidates) if candidates else None


def _cancellation_conditions(direction: str, entry_limit: float, invalidation: float, setup: dict[str, Any]) -> list[str]:
    setup_label = _setup_display_label(setup["type"])
    if direction == "BUY":
        return [
            f"若开盘跳空高于计划上限 {entry_limit:.2f}，取消追价。",
            f"若触发前收盘跌破结构失效价 {invalidation:.2f}，取消做多计划。",
            f"若{setup_label}缺少后续跟随，回到等待状态。",
        ]
    return [
        f"若价格重新站上失效价 {invalidation:.2f}，取消防守/卖出判断。",
        f"若{setup_label}没有后续向下跟随，回到等待状态。",
    ]


def _build_assessment(environment: dict[str, Any], setups: list[dict[str, Any]], signals: list[dict[str, Any]]) -> dict[str, Any]:
    sell_signal = next((item for item in signals if item["direction"] == "SELL"), None)
    buy_signal = next((item for item in signals if item["direction"] == "BUY"), None)
    if sell_signal:
        return {
            "action": "SELL",
            "label": "卖出条件确认",
            "executionConstraint": "EXISTING_LONG_ONLY",
            "positionRequired": True,
            "shortSellingAllowed": False,
            "reasons": sell_signal["reasons"],
            "blockedReasons": [],
            "reviewRequired": False,
        }
    if buy_signal:
        return {
            "action": "BUY",
            "label": "条件买入计划",
            "reasons": buy_signal["reasons"],
            "blockedReasons": [],
            "reviewRequired": False,
        }
    blocked = [condition for setup in setups for condition in setup["missingConditions"]]
    if environment["reviewRequired"]:
        blocked.append("当前环境方向不清，需要人工复核。")
    return {
        "action": "WAIT",
        "label": "等待确认",
        "reasons": environment["evidence"],
        "blockedReasons": blocked or ["没有同时满足位置、结构和确认条件的完整计划。"],
        "reviewRequired": environment["reviewRequired"] or any(item["reviewRequired"] for item in setups),
    }


def _build_summary(bars: list[dict[str, float | str]], price_action: dict[str, Any]) -> dict[str, Any]:
    levels = price_action["levels"]
    close = float(bars[-1]["close"])
    supports = [item for item in levels if float(item["price"]) <= close and "SUPPORT" in item["role"]]
    resistances = [item for item in levels if float(item["price"]) >= close and "RESISTANCE" in item["role"]]
    return {
        "latestDate": str(bars[-1]["date"]),
        "latestClose": _round(close),
        "environment": price_action["environment"]["state"],
        "environmentLabel": price_action["environment"]["label"],
        "action": price_action["assessment"]["action"],
        "signalCount": len(price_action["signals"]),
        "setupCount": len(price_action["setups"]),
        "nearestSupport": supports[0]["price"] if supports else None,
        "nearestResistance": resistances[0]["price"] if resistances else None,
    }


def _bullish_bar(bar: dict[str, float | str]) -> bool:
    return float(bar["close"]) > float(bar["open"]) and _close_location(bar) >= 0.58


def _bearish_bar(bar: dict[str, float | str]) -> bool:
    return float(bar["close"]) < float(bar["open"]) and _close_location(bar) <= 0.42


def _close_location(bar: dict[str, float | str]) -> float:
    high, low = float(bar["high"]), float(bar["low"])
    return (float(bar["close"]) - low) / max(high - low, 0.0001)


def _round(value: float) -> float:
    return round(float(value), 3)
