"""Structured price-action contracts used by daily, intraday and discipline flows.

The legacy analyser still supplies a few display-oriented fields.  This module is
the canonical, serialisable contract around those fields: facts are measured from
completed bars, structures carry confirmation timestamps, and plans remain
conditional until every hard risk gate passes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from hashlib import sha1
from math import isfinite
import os
import re
from statistics import median
from typing import Any, Iterable


PROFILE_ID = "al-brooks-candlestick-price-action"
PROFILE_VERSION = "2026.08.25"
SOURCE_POLICY_ID = "four-books-authoritative-discipline"
SOURCE_POLICY_VERSION = "2026.08.23"

# The PDFs are the trading-rule source.  Thresholds below remain engineering
# profile values and must never be presented as quotations from a book.
SOURCE_POLICY: dict[str, Any] = {
    "policyId": SOURCE_POLICY_ID,
    "policyVersion": SOURCE_POLICY_VERSION,
    "authority": "PDF_PRIMARY",
    "books": [
        {"id": "candlestick", "file": "日本蜡烛图交易技术分析.pdf", "pages": "p15-338"},
        {"id": "trend", "file": "Al Brooks 价格行为交易趋势篇.pdf", "pages": "p70-405"},
        {"id": "range", "file": "Al Brooks 价格行为交易区间篇.pdf", "pages": "p18-436"},
        {"id": "reversal", "file": "Al Brooks 价格行为交易反转篇.pdf", "pages": "p37-420"},
    ],
    "notesAreSecondary": True,
    "marketAdapter": {
        "defaultMarket": "A_SHARE_CASH",
        "aShareShortSellingAllowed": False,
        "usShortSellingMayDiffer": True,
        "executionExceptionOnly": "市场执行适配只处理做空权限、整手、T+1、涨跌停、现金和流动性，不改写价格行为纪律。",
    },
}


DEFAULT_PRICE_ACTION_PROFILE: dict[str, Any] = {
    "profileId": PROFILE_ID,
    "profileVersion": PROFILE_VERSION,
    "atrPeriod": 14,
    "emaPeriod": 20,
    "swingRadius": 2,
    "minimumBars": 35,
    "structureLookback": {"1d": 20, "30m": 48, "15m": 64, "5m": 96},
    "thresholds": {
        "trendBarBodyAtr": 0.55,
        "dojiBodyRatio": 0.18,
        "smallBarAtr": 0.55,
        "longTailRatio": 0.45,
        "overlapRange": 0.46,
        "rangeFlatSlopeAtr": 1.4,
        "rangeBoundaryToleranceAtr": 0.25,
        "rangeBoundaryTolerancePct": 0.08,
        "rangeOverlapSpan": 0.34,
        "rangeBalanceMoveAtr": 4.0,
        "rangeScoreFloor": 0.40,
        "rangeScoreSpan": 0.50,
        "trendSlopeAtr": 0.18,
        "trendMoveAtr": 1.5,
        "strongTrendMoveAtr": 4.0,
        "rangeMiddlePct": 0.25,
        "levelToleranceAtr": 0.18,
        "entryBufferAtr": 0.25,
        "stopBufferAtr": 0.20,
        "minimumRoomR": 1.5,
        "relativeVolumeConfirm": 1.10,
        "relativeVolumeClimax": 2.50,
        "relativeVolumeDryUp": 0.65,
        "minimumVolumeSamples": 5,
        "openingRangeBars": 3,
        "firstHourMinutes": 60,
        "microChannelBars": 4,
        "reversalFollowThroughBars": 2,
        "triangleMinimumBars": 6,
        "tightRangeAtr": 1.35,
        "headShoulderToleranceAtr": 0.85,
        "sessionCloseBufferMinutes": 30,
    },
    "risk": {
        "riskPerTradePct": 0.01,
        "maxPositionPct": 0.20,
        "hardStopPct": 0.08,
        "lotSize": 100,
        "tickSize": 0.01,
        "slippageBps": 12,
        "feeBps": 10,
        "maxDailyLossPct": 0.02,
        "tPlusOne": True,
    },
    "sessions": {
        "regular": (("09:30", "11:30"), ("13:00", "15:00")),
        "premarket": (("09:00", "09:30"),),
        "overnight": (("15:00", "09:00"),),
    },
    "volumePolicy": {
        "unknownRequiresReview": True,
        "extremeVolumeBlocks": False,
        "excludePremarketFromBaseline": True,
    },
}


BLOCK_REASONS = {
    "DATA_INSUFFICIENT",
    "INCOMPLETE_BAR",
    "SESSION_INCOMPLETE",
    "UNKNOWN_STOP",
    "UNKNOWN_TARGET",
    "INSUFFICIENT_ROOM",
    "RISK_LIMIT",
    "CASH_LIMIT",
    "CONCENTRATION_LIMIT",
    "LOT_SIZE",
    "T_PLUS_ONE",
    "PRICE_LIMIT",
    "ILLIQUID",
    "DAILY_LOSS_LOCK",
    "CONTEXT_CHANGED",
    "DUPLICATE_SIGNAL",
    "MAGNET_CONSUMED",
    "SESSION_EXPIRED",
    "REVIEW_REQUIRED",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: str
    priority: int
    description: str
    hard: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ruleId": self.rule_id, "severity": self.severity, "priority": self.priority, "description": self.description, "hard": self.hard}


RULE_CATALOG = tuple(
    Rule(rule_id, "HARD", 100, rule_id.replace("_", " "), True)
    for rule_id in sorted(BLOCK_REASONS)
) + (
    Rule("VOLUME_CONFIRMATION", "SOFT", 20, "量能只作为结构质量证据", False),
    Rule("PATTERN_SCORE", "SOFT", 10, "形态评分不能绕过硬门槛", False),
    Rule("MANUAL_REVIEW", "REVIEW", 50, "主观质量判断需要人工复核", False),
)


def _merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def get_price_action_profile(profile: dict[str, Any] | None = None, timeframe: str | None = None) -> dict[str, Any]:
    """Return a detached, versioned profile; callers may safely mutate it."""

    result = _merge(DEFAULT_PRICE_ACTION_PROFILE, profile)
    result["timeframe"] = timeframe
    return result


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and isfinite(float(value)) else None


def _stamp(value: Any, fallback: str = "") -> str:
    return str(value or fallback)


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(item or "") for item in parts)
    return f"{prefix}_{sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _parse_clock(value: str) -> time | None:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        return None


def session_for_timestamp(value: Any, *, timeframe: str = "1d") -> str:
    """Classify a timestamp without assuming a provider-specific date format."""

    if timeframe == "1d":
        return "regular"
    raw = str(value or "").replace("T", " ")
    if " " not in raw:
        return "regular"
    clock = raw.rsplit(" ", 1)[-1][:5]
    parsed = _parse_clock(clock)
    if not parsed:
        return "unknown"
    if time(9, 30) <= parsed <= time(11, 30) or time(13, 0) <= parsed <= time(15, 0):
        return "regular"
    if time(9, 0) <= parsed < time(9, 30):
        return "premarket"
    return "overnight"


@dataclass
class BarFacts:
    bar_id: str
    index: int
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None
    float_shares: float | None
    provider_turnover: float | None
    computed_turnover: float | None
    session: str
    source: str
    adjustment: str
    completed: bool
    quality_flags: list[str] = field(default_factory=list)
    true_range: float = 0.0
    atr: float | None = None
    ema: float | None = None
    body: float = 0.0
    range: float = 0.0
    upper_wick: float = 0.0
    lower_wick: float = 0.0
    body_ratio: float = 0.0
    close_position: float = 0.5
    direction: str = "NEUTRAL"
    gap: float = 0.0
    overlap_ratio: float | None = None
    pattern_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"barId": result.pop("bar_id"), "floatShares": result.pop("float_shares"), "providerTurnover": result.pop("provider_turnover"), "computedTurnover": result.pop("computed_turnover"), "qualityFlags": result.pop("quality_flags"), "trueRange": result.pop("true_range"), "closePosition": result.pop("close_position"), "bodyRatio": result.pop("body_ratio"), "upperWick": result.pop("upper_wick"), "lowerWick": result.pop("lower_wick"), "overlapRatio": result.pop("overlap_ratio"), "patternLabels": result.pop("pattern_labels")})
        result["turnoverRate"] = result.get("providerTurnover") if result.get("providerTurnover") is not None else result.get("computedTurnover")
        return result


@dataclass
class VolumeTurnoverContext:
    available: bool
    volume: float | None
    amount: float | None
    relative_volume: float | None
    amount_percentile: float | None
    volume_acceleration: float | None
    turnover_rate: float | None
    turnover_source: str
    turnover_quality: str
    baseline: float | None
    baseline_window: int
    baseline_samples: int
    price_volume_alignment: str
    state: str
    quality_flags: list[str] = field(default_factory=list)
    session_policy: str = "regular-only baseline"
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"relativeVolume": result.pop("relative_volume"), "amountPercentile": result.pop("amount_percentile"), "volumeAcceleration": result.pop("volume_acceleration"), "turnoverRate": result.pop("turnover_rate"), "turnoverSource": result.pop("turnover_source"), "turnoverQuality": result.pop("turnover_quality"), "baselineWindow": result.pop("baseline_window"), "baselineSamples": result.pop("baseline_samples"), "priceVolumeAlignment": result.pop("price_volume_alignment"), "qualityFlags": result.pop("quality_flags"), "sessionPolicy": result.pop("session_policy"), "asOf": result.pop("as_of")})
        return result


@dataclass
class EnvironmentState:
    state: str
    label: str
    score: float
    confidence: float
    evidence: list[str]
    conflicts: list[str]
    confirmation_time: str | None
    review_required: bool = False
    position: str | None = None
    range_zone: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"confirmationTime": result.pop("confirmation_time"), "reviewRequired": result.pop("review_required"), "rangeZone": result.pop("range_zone")})
        return result


@dataclass
class Structure:
    structure_id: str
    kind: str
    direction: str
    status: str
    observed_at: str
    confirmed_at: str | None
    triggered_at: str | None = None
    failed_at: str | None = None
    expired_at: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
    invalidation_price: float | None = None
    source_pages: list[str] = field(default_factory=list)
    state_transition: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"structureId": result.pop("structure_id"), "observedAt": result.pop("observed_at"), "confirmedAt": result.pop("confirmed_at"), "triggeredAt": result.pop("triggered_at"), "failedAt": result.pop("failed_at"), "expiredAt": result.pop("expired_at"), "evidenceIds": result.pop("evidence_ids"), "invalidationPrice": result.pop("invalidation_price"), "sourcePages": result.pop("source_pages"), "stateTransition": result.pop("state_transition")})
        return result


@dataclass
class MagnetZone:
    magnet_id: str
    role: str
    low: float
    high: float
    source: str
    owner_timeframe: str
    confirmed_at: str | None
    structural: bool = True
    distance_atr: float | None = None
    distance_r: float | None = None
    consumed: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"magnetId": result.pop("magnet_id"), "ownerTimeframe": result.pop("owner_timeframe"), "confirmedAt": result.pop("confirmed_at"), "distanceAtr": result.pop("distance_atr"), "distanceR": result.pop("distance_r")})
        return result


@dataclass
class Setup:
    setup_id: str
    kind: str
    direction: str
    status: str
    observed_at: str
    confirmed_at: str | None
    trigger_price: float | None
    invalidation_price: float | None
    location: str
    evidence_ids: list[str]
    evidence: list[str]
    missing_conditions: list[str]
    alternative_scenario: str
    review_required: bool
    source_pages: list[str]
    state_transition: list[dict[str, Any]] = field(default_factory=list)
    volume_evidence: dict[str, Any] = field(default_factory=dict)
    market_mode: str = "REBOUND"
    market_mode_label: str = "反弹"
    family: str = "REBOUND_REVERSAL"
    lifecycle: str = "WATCH"
    next_condition: str = "等待完整K线确认"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"setupId": result.pop("setup_id"), "observedAt": result.pop("observed_at"), "confirmedAt": result.pop("confirmed_at"), "triggerPrice": result.pop("trigger_price"), "invalidationPrice": result.pop("invalidation_price"), "evidenceIds": result.pop("evidence_ids"), "missingConditions": result.pop("missing_conditions"), "alternativeScenario": result.pop("alternative_scenario"), "reviewRequired": result.pop("review_required"), "sourcePages": result.pop("source_pages"), "stateTransition": result.pop("state_transition"), "volumeEvidence": result.pop("volume_evidence"), "marketMode": result.pop("market_mode"), "marketModeLabel": result.pop("market_mode_label"), "nextCondition": result.pop("next_condition")})
        return result


@dataclass
class TradePlan:
    plan_id: str
    setup_id: str
    direction: str
    mode: str
    status: str
    trigger_price: float | None
    entry_zone: dict[str, float] | None
    entry_order_type: str
    entry_limit: float | None
    structural_reference: float | None
    structural_invalidation: float | None
    initial_stop: float | None
    active_stop: float | None
    stop_state: str
    first_target: dict[str, Any] | None
    extension_target: dict[str, Any] | None
    cancellation_conditions: list[str]
    room_r: float | None
    risk_status: str
    blocked_reasons: list[str]
    reason_clusters: list[list[str]]
    observed_at: str
    confirmed_at: str | None
    triggered_at: str | None = None
    failed_at: str | None = None
    expired_at: str | None = None
    session_expiry: str | None = None
    profile_id: str = PROFILE_ID
    profile_version: str = PROFILE_VERSION
    source_pages: list[str] = field(default_factory=list)
    stop_events: list[dict[str, Any]] = field(default_factory=list)
    alternative_scenario: str = ""
    # A-share cash accounts never open short positions. Direction remains a
    # market-direction signal; these fields describe execution semantics.
    execution_action: str = "OBSERVE"
    execution_constraint: str = "NO_NEW_SHORT"
    position_required: bool = False
    short_selling_allowed: bool = False
    executable: bool = False
    target_semantics: str = "OBSERVATION_ONLY"
    market_mode: str = "REBOUND"
    market_mode_label: str = "反弹"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"planId": result.pop("plan_id"), "setupId": result.pop("setup_id"), "triggerPrice": result.pop("trigger_price"), "entryZone": result.pop("entry_zone"), "entryOrderType": result.pop("entry_order_type"), "entryLimit": result.pop("entry_limit"), "structuralReference": result.pop("structural_reference"), "structuralInvalidation": result.pop("structural_invalidation"), "initialStop": result.pop("initial_stop"), "activeStop": result.pop("active_stop"), "stopState": result.pop("stop_state"), "firstTarget": result.pop("first_target"), "extensionTarget": result.pop("extension_target"), "cancellationConditions": result.pop("cancellation_conditions"), "roomR": result.pop("room_r"), "riskStatus": result.pop("risk_status"), "blockedReasons": result.pop("blocked_reasons"), "reasonClusters": result.pop("reason_clusters"), "observedAt": result.pop("observed_at"), "confirmedAt": result.pop("confirmed_at"), "triggeredAt": result.pop("triggered_at"), "failedAt": result.pop("failed_at"), "expiredAt": result.pop("expired_at"), "sessionExpiry": result.pop("session_expiry"), "profileId": result.pop("profile_id"), "profileVersion": result.pop("profile_version"), "sourcePages": result.pop("source_pages"), "stopEvents": result.pop("stop_events"), "alternativeScenario": result.pop("alternative_scenario"), "executionAction": result.pop("execution_action"), "executionConstraint": result.pop("execution_constraint"), "positionRequired": result.pop("position_required"), "shortSellingAllowed": result.pop("short_selling_allowed"), "executable": result.pop("executable"), "targetSemantics": result.pop("target_semantics"), "marketMode": result.pop("market_mode"), "marketModeLabel": result.pop("market_mode_label")})
        return result


@dataclass
class SignalRecord:
    signal_id: str
    setup_id: str
    plan_id: str | None
    direction: str
    status: str
    observed_at: str
    confirmed_at: str | None
    triggered_at: str | None
    failed_at: str | None
    evidence_ids: list[str]
    review_required: bool

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update({"signalId": result.pop("signal_id"), "setupId": result.pop("setup_id"), "planId": result.pop("plan_id"), "observedAt": result.pop("observed_at"), "confirmedAt": result.pop("confirmed_at"), "triggeredAt": result.pop("triggered_at"), "failedAt": result.pop("failed_at"), "evidenceIds": result.pop("evidence_ids"), "reviewRequired": result.pop("review_required")})
        return result


def _valid_ohlc(item: dict[str, Any]) -> bool:
    values = [_safe_float(item.get(key), -1) for key in ("open", "high", "low", "close")]
    return min(values) > 0 and values[1] >= max(values[0], values[2], values[3]) and values[2] <= min(values[0], values[1], values[3])


def build_bar_facts(raw_bars: Iterable[dict[str, Any]], timeframe: str = "1d", profile: dict[str, Any] | None = None) -> list[BarFacts]:
    cfg = get_price_action_profile(profile, timeframe)
    bars = [item for item in raw_bars if isinstance(item, dict) and _valid_ohlc(item)]
    bars.sort(key=lambda item: str(item.get("date") or item.get("datetime") or ""))
    atr_period = int(cfg["atrPeriod"])
    ema_period = int(cfg["emaPeriod"])
    alpha = 2 / (ema_period + 1)
    ema: float | None = None
    atr: float | None = None
    output: list[BarFacts] = []
    previous_close = None
    for index, item in enumerate(bars):
        timestamp = _stamp(item.get("date") or item.get("datetime"))
        open_price = _safe_float(item.get("open"))
        high = _safe_float(item.get("high"))
        low = _safe_float(item.get("low"))
        close = _safe_float(item.get("close"))
        volume_value = item.get("volume")
        volume = _safe_float(volume_value) if volume_value not in (None, "") else None
        amount_value = item.get("amount")
        amount = _safe_float(amount_value) if amount_value not in (None, "") else None
        float_value = item.get("floatShares", item.get("float_shares"))
        float_shares = _safe_float(float_value) if float_value not in (None, "") else None
        provider_value = item.get("turnoverRate", item.get("turnover_rate"))
        provider_turnover = _safe_float(provider_value) if provider_value not in (None, "") else None
        computed_turnover = amount / float_shares if amount is not None and amount > 0 and float_shares and float_shares > 0 else None
        true_range = max(high - low, abs(high - (previous_close or close)), abs(low - (previous_close or close)))
        atr = true_range if atr is None else ((atr * (atr_period - 1)) + true_range) / atr_period
        ema = close if ema is None else ema + alpha * (close - ema)
        span = max(high - low, 1e-9)
        body = abs(close - open_price)
        close_position = (close - low) / span
        upper = high - max(open_price, close)
        lower = min(open_price, close) - low
        flags: list[str] = []
        raw_quality = item.get("qualityFlags", item.get("quality", []))
        if isinstance(raw_quality, str):
            raw_quality = [raw_quality]
        if isinstance(raw_quality, (list, tuple, set)):
            flags.extend(str(flag) for flag in raw_quality if flag)
        if str(item.get("timestampQuality") or "VALID").upper() not in {"VALID", ""}:
            flags.append("STALE_OR_INVALID_TIMESTAMP")
        if volume is None or volume <= 0:
            flags.append("ZERO_OR_MISSING_VOLUME")
        if amount is not None and volume is not None and amount > 0 and volume > 0:
            unit_price = amount / volume
            if unit_price < close * 0.05 or unit_price > close * 100:
                flags.append("AMOUNT_VOLUME_UNIT_MISMATCH")
        if provider_turnover is not None and not 0 <= provider_turnover <= 100:
            flags.append("TURNOVER_UNIT_INVALID")
            provider_turnover = None
        direction = "BULL" if close > open_price else "BEAR" if close < open_price else "NEUTRAL"
        labels: list[str] = []
        thresholds = cfg["thresholds"]
        if body / span <= thresholds["dojiBodyRatio"]:
            labels.append("DOJI")
        if body / max(atr or span, 1e-9) >= thresholds["trendBarBodyAtr"] and close_position >= 0.65:
            labels.append("BULL_TREND_BAR" if direction == "BULL" else "BEAR_TREND_BAR")
        if body / max(atr or span, 1e-9) <= thresholds["smallBarAtr"]:
            labels.append("SMALL_BAR")
        if lower / span >= thresholds["longTailRatio"] and close_position >= 0.55:
            labels.append("BULL_REVERSAL_TAIL")
        if upper / span >= thresholds["longTailRatio"] and close_position <= 0.45:
            labels.append("BEAR_REVERSAL_TAIL")
        fact = BarFacts(
            bar_id=_stable_id("bar", timestamp, index), index=index, timestamp=timestamp,
            open=open_price, high=high, low=low, close=close, volume=volume, amount=amount,
            float_shares=float_shares, provider_turnover=provider_turnover,
            computed_turnover=computed_turnover, session=session_for_timestamp(timestamp, timeframe=timeframe),
            source=_stamp(item.get("source") or item.get("provider"), "unknown"),
            adjustment=_stamp(item.get("adjustment") or item.get("adjust"), "unknown"),
            completed=bool(item.get("completed", item.get("isCompleted", True))), quality_flags=flags,
            true_range=true_range, atr=atr, ema=ema, body=body, range=span,
            upper_wick=max(upper, 0.0), lower_wick=max(lower, 0.0), body_ratio=body / span,
            close_position=max(0.0, min(1.0, close_position)), direction=direction,
            gap=(open_price - previous_close) if previous_close is not None else 0.0,
            pattern_labels=labels,
        )
        if output:
            previous = output[-1]
            intersection = max(0.0, min(previous.high, high) - max(previous.low, low))
            fact.overlap_ratio = intersection / max(min(previous.range, span), 1e-9)
            if high <= previous.high and low >= previous.low:
                fact.pattern_labels.append("INSIDE_BAR")
            if high >= previous.high and low <= previous.low:
                fact.pattern_labels.append("OUTSIDE_BAR")
            if close > open_price and close >= previous.open and open_price <= previous.close:
                fact.pattern_labels.append("BULL_ENGULFING")
            if close < open_price and close <= previous.open and open_price >= previous.close:
                fact.pattern_labels.append("BEAR_ENGULFING")
            if previous.direction != "NEUTRAL" and direction != "NEUTRAL" and previous.direction != direction:
                fact.pattern_labels.append("OPPOSITE_TWO_BAR_COMBINATION")
            if fact.pattern_labels and "INSIDE_BAR" in fact.pattern_labels and "INSIDE_BAR" in previous.pattern_labels:
                fact.pattern_labels.append("II")
            if "OUTSIDE_BAR" in fact.pattern_labels and "INSIDE_BAR" in previous.pattern_labels:
                fact.pattern_labels.append("IOI")
        if "DOJI" in fact.pattern_labels and (fact.atr or 0) > 0 and fact.range >= (fact.atr or fact.range) * 1.2:
            fact.pattern_labels.append("LARGE_DOJI_MICRO_RANGE")
        if abs(fact.gap) >= max(fact.atr or 0, fact.range) * 0.35:
            fact.pattern_labels.append("GAP_UP" if fact.gap > 0 else "GAP_DOWN")
        output.append(fact)
        previous_close = close
    return output


def build_volume_turnover_context(facts: list[BarFacts], profile: dict[str, Any] | None = None, timeframe: str = "1d") -> dict[str, Any]:
    cfg = get_price_action_profile(profile, timeframe)
    if not facts:
        return VolumeTurnoverContext(False, None, None, None, None, None, None, "UNKNOWN", "UNKNOWN", None, 0, 0, "UNKNOWN", "UNKNOWN", ["NO_BARS"], as_of="").to_dict()
    latest = facts[-1]
    comparable = [fact for fact in facts[:-1] if fact.volume is not None and fact.volume > 0 and (not cfg["volumePolicy"]["excludePremarketFromBaseline"] or fact.session == latest.session)]
    window = min(len(comparable), 20)
    baseline_values = [fact.volume for fact in comparable[-window:]]
    baseline = median(baseline_values) if baseline_values else None
    relative = latest.volume / baseline if latest.volume and baseline else None
    prior = median([fact.volume for fact in comparable[-min(5, len(comparable)):]]) if comparable else None
    acceleration = latest.volume / prior if latest.volume and prior else None
    amount_values = [fact.amount for fact in facts[:-1] if fact.amount and fact.amount > 0]
    amount_percentile = None
    if latest.amount and amount_values:
        amount_percentile = sum(value <= latest.amount for value in amount_values) / len(amount_values)
    provider = latest.provider_turnover
    computed = latest.computed_turnover
    turnover = provider if provider is not None else computed
    turnover_source = "PROVIDER" if provider is not None else "COMPUTED" if computed is not None else "UNKNOWN"
    turnover_quality = "VALID" if turnover is not None else "UNKNOWN"
    flags = list(latest.quality_flags)
    if latest.float_shares is None and provider is None:
        flags.append("FLOAT_SHARES_MISSING")
    if latest.session == "premarket":
        flags.append("PREMARKET_BAR")
    if relative is None:
        state = "UNKNOWN"
    elif relative >= cfg["thresholds"]["relativeVolumeClimax"] and latest.body / max(latest.atr or latest.range, 1e-9) >= 1.5 and latest.close_position < 0.65:
        state = "CLIMAX_CANDIDATE"
    elif relative >= cfg["thresholds"]["relativeVolumeConfirm"]:
        state = "EXPANSION"
    elif relative <= cfg["thresholds"]["relativeVolumeDryUp"]:
        state = "DRY_UP"
    else:
        state = "NORMAL"
    if relative is not None and relative >= 5:
        flags.append("UNUSUAL_SINGLE_BAR_VOLUME")
    if latest.volume is None or latest.volume <= 0:
        flags.append("LOW_LIQUIDITY")
    alignment = "UNKNOWN"
    if relative is not None and len(facts) >= 2:
        price_up = latest.close > facts[-2].close
        volume_up = latest.volume is not None and facts[-2].volume is not None and latest.volume > facts[-2].volume
        if state == "CLIMAX_CANDIDATE":
            alignment = "CLIMAX_CANDIDATE"
        elif price_up == volume_up:
            alignment = "CONFIRMING"
        else:
            alignment = "DIVERGING"
    return VolumeTurnoverContext(
        available=latest.volume is not None and latest.volume > 0,
        volume=latest.volume, amount=latest.amount, relative_volume=relative,
        amount_percentile=amount_percentile, volume_acceleration=acceleration,
        turnover_rate=turnover, turnover_source=turnover_source, turnover_quality=turnover_quality,
        baseline=baseline, baseline_window=window, baseline_samples=len(baseline_values),
        price_volume_alignment=alignment, state=state, quality_flags=sorted(set(flags)),
        as_of=latest.timestamp,
    ).to_dict()


def _environment_from_facts(facts: list[BarFacts], volume: dict[str, Any], profile: dict[str, Any]) -> EnvironmentState:
    if len(facts) < int(profile["minimumBars"]):
        return EnvironmentState("DATA_INSUFFICIENT", "数据不足", 0.0, 0.0, [f"需要至少 {profile['minimumBars']} 根完整K线，当前 {len(facts)} 根"], ["MINIMUM_BARS"], facts[-1].timestamp if facts else None, False)
    latest = facts[-1]
    recent = facts[-min(12, len(facts)):]
    slope = (latest.ema - recent[0].ema) / max(latest.atr or latest.range, 1e-9) if latest.ema is not None and recent[0].ema is not None else 0.0
    move = (latest.close - recent[0].close) / max(latest.atr or latest.range, 1e-9)
    overlap_values = [fact.overlap_ratio for fact in recent[1:] if fact.overlap_ratio is not None]
    overlap = sum(overlap_values) / len(overlap_values) if overlap_values else 0.0
    high = max(fact.high for fact in facts[-20:])
    low = min(fact.low for fact in facts[-20:])
    width = max(high - low, latest.atr or latest.range)
    position = max(0.0, min(1.0, (latest.close - low) / width))
    thresholds = profile["thresholds"]
    evidence = [f"20EMA斜率 {slope:.2f} ATR，近12根移动 {move:.2f} ATR", f"重叠度 {overlap:.2f}"]
    conflicts: list[str] = []
    if overlap >= thresholds["overlapRange"] and abs(slope) < thresholds["rangeFlatSlopeAtr"]:
        state, label = "RANGE", "交易区间"
        score, range_evidence = _range_environment_score(facts, recent, slope, move, overlap, thresholds)
        evidence.extend(range_evidence)
        evidence.append(f"区间边界 {low:.4f}/{high:.4f}")
    elif latest.close >= latest.ema and slope >= thresholds["trendSlopeAtr"] and move >= thresholds["trendMoveAtr"]:
        state, label, score = "BULL_TREND", "多头趋势", min(0.98, 0.55 + min(move / 10, 0.35))
    elif latest.close <= latest.ema and slope <= -thresholds["trendSlopeAtr"] and move <= -thresholds["trendMoveAtr"]:
        state, label, score = "BEAR_TREND", "下行趋势", min(0.98, 0.55 + min(abs(move) / 10, 0.35))
    else:
        state, label, score = "TRANSITION", "过渡/方向不清", 0.4
        conflicts.append("趋势和区间证据未形成一致方向")
    confidence = min(0.99, max(0.0, score * (0.9 if volume.get("state") == "UNKNOWN" else 1.0)))
    return EnvironmentState(state, label, round(score, 4), round(confidence, 4), evidence, conflicts, latest.timestamp, state == "TRANSITION", "UPPER" if position > 0.75 else "LOWER" if position < 0.25 else "MIDDLE", {"low": low, "high": high, "mid": (low + high) / 2, "width": width, "position": position} if state == "RANGE" else None)


def _range_environment_score(
    facts: list[BarFacts],
    recent: list[BarFacts],
    slope: float,
    move: float,
    overlap: float,
    thresholds: dict[str, Any],
) -> tuple[float, list[str]]:
    """Score the strength of balance evidence; it is not a directional signal."""

    def clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    atr = max(recent[-1].atr or recent[-1].range, 1e-9)
    flat_slope_atr = max(float(thresholds["rangeFlatSlopeAtr"]), 1e-9)
    overlap_span = max(float(thresholds["rangeOverlapSpan"]), 1e-9)
    overlap_score = clamp((overlap - float(thresholds["overlapRange"])) / overlap_span)
    flatness_score = 1.0 - clamp(abs(slope) / flat_slope_atr)
    direction_changes = sum(previous.direction != current.direction for previous, current in zip(recent, recent[1:]))
    alternation_score = direction_changes / max(len(recent) - 1, 1)

    boundary_window = facts[-20:]
    range_high = max(fact.high for fact in boundary_window)
    range_low = min(fact.low for fact in boundary_window)
    boundary_tolerance = max(
        atr * float(thresholds["rangeBoundaryToleranceAtr"]),
        (range_high - range_low) * float(thresholds["rangeBoundaryTolerancePct"]),
    )
    upper_touches = sum(fact.high >= range_high - boundary_tolerance for fact in recent)
    lower_touches = sum(fact.low <= range_low + boundary_tolerance for fact in recent)
    boundary_score = 0.5 * min(upper_touches / 2.0, 1.0) + 0.5 * min(lower_touches / 2.0, 1.0)
    directional_balance = 1.0 - clamp(abs(move) / max(float(thresholds["rangeBalanceMoveAtr"]), 1e-9))

    quality = (
        0.30 * overlap_score
        + 0.25 * flatness_score
        + 0.20 * alternation_score
        + 0.15 * boundary_score
        + 0.10 * directional_balance
    )
    score = float(thresholds["rangeScoreFloor"]) + float(thresholds["rangeScoreSpan"]) * quality
    score = max(0.0, min(0.99, score))
    evidence = [
        f"区间评分依据：重叠证据 {overlap_score:.2f}，EMA平坦度 {flatness_score:.2f}，方向交替 {alternation_score:.2f}。",
        f"上下边界测试 {upper_touches}/{lower_touches}，单向移动压力 {abs(move):.2f} ATR，区间评分 {score * 100:.1f}。",
    ]
    return score, evidence


def _confirmed_swings(facts: list[BarFacts], radius: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index in range(radius, len(facts) - radius):
        window = facts[index - radius:index + radius + 1]
        current = facts[index]
        if current.high == max(item.high for item in window) and sum(item.high == current.high for item in window) == 1:
            result.append({"kind": "SWING_HIGH", "index": index, "price": current.high, "observedAt": current.timestamp, "confirmedAt": facts[index + radius].timestamp})
        if current.low == min(item.low for item in window) and sum(item.low == current.low for item in window) == 1:
            result.append({"kind": "SWING_LOW", "index": index, "price": current.low, "observedAt": current.timestamp, "confirmedAt": facts[index + radius].timestamp})
    return result


def build_legs(facts: list[BarFacts], swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build chronological legs from confirmed pivots; no unconfirmed pivot is used."""

    ordered = sorted(swings, key=lambda item: int(item.get("index", -1)))
    legs: list[dict[str, Any]] = []
    for number, (start, end) in enumerate(zip(ordered, ordered[1:]), start=1):
        if start.get("kind") == end.get("kind"):
            continue
        direction = "UP" if start.get("kind") == "SWING_LOW" else "DOWN"
        start_index = int(start.get("index", 0))
        end_index = int(end.get("index", start_index))
        legs.append({
            "legId": _stable_id("leg", start.get("observedAt"), end.get("observedAt")),
            "number": number,
            "direction": direction,
            "startPrice": _round(_safe_float(start.get("price"))),
            "endPrice": _round(_safe_float(end.get("price"))),
            "startAt": start.get("observedAt"),
            "endAt": end.get("observedAt"),
            "confirmedAt": end.get("confirmedAt"),
            "bars": max(0, end_index - start_index),
            "twoLegCount": number in {2, 4},
        })
    return legs


def _source_pages(kind: str) -> list[str]:
    pages = {"candlestick": "日本蜡烛图交易技术分析.pdf:p15-120", "trend": "Al Brooks 价格行为交易趋势篇.pdf:p70-405", "range": "Al Brooks 价格行为交易区间篇.pdf:p18-436", "reversal": "Al Brooks 价格行为交易反转篇.pdf:p37-420"}
    return [pages.get(kind, pages["candlestick"])]


MODE_LABELS = {"TREND": "趋势", "RANGE": "区间", "REBOUND": "反弹"}

TREND_PATTERN_TYPES = {
    "TREND_PULLBACK_H1", "TREND_PULLBACK_H2", "TREND_PULLBACK_L1", "TREND_PULLBACK_L2", "TREND_FIRST_PULLBACK", "TREND_TWO_LEG_PULLBACK",
    "BULL_FLAG", "BEAR_FLAG", "BREAKOUT_UP", "BREAKOUT_DOWN", "BREAKOUT_RETEST_UP",
    "BREAKOUT_RETEST_DOWN", "MICRO_CHANNEL_BREAK", "SPIKE_CHANNEL", "WIDE_CHANNEL",
    "STEP_CHANNEL", "EMA_GAP_CONTEXT", "TREND_CONTINUATION",
}
RANGE_PATTERN_TYPES = {
    "RANGE_LOWER_REVERSAL", "RANGE_UPPER_REVERSAL", "RANGE_MIDDLE", "RANGE_EDGE_FADE_UP",
    "RANGE_EDGE_FADE_DOWN", "TIGHT_RANGE_IRON_WIRE", "TRIANGLE_COMPRESSION",
    "TRIANGLE_EXPANSION", "ABC_RANGE_LEGS", "RANGE_BREAKOUT_PENDING",
    "RANGE_BREAKOUT_CONFIRMED", "RANGE_BREAKOUT_RETEST", "RANGE_BREAKOUT_CONTINUATION",
    "FAILED_BULLISH_BREAKOUT", "FAILED_BEARISH_BREAKOUT", "BREAKOUT_FAILURE_OF_FAILURE",
}
REBOUND_PATTERN_TYPES = {
    "DOUBLE_BOTTOM", "DOUBLE_TOP", "WEDGE_THIRD_PUSH", "CLIMAX_REVERSAL_CANDIDATE",
    "CLIMAX_SPIKE_REVERSAL", "V_REVERSAL_WARNING", "HEAD_SHOULDERS_REVERSAL",
    "EXPANSION_REVERSAL", "FINAL_FLAG_OR_INSIDE_BREAK", "MAJOR_BEARISH_REVERSAL",
    "MAJOR_BULLISH_REVERSAL", "REVERSAL_FAILURE", "REVERSAL_FAILURE_OF_FAILURE",
}


def is_minute_timeframe(timeframe: str | None) -> bool:
    return bool(re.fullmatch(r"[1-9]\d*m", str(timeframe or "").strip().lower()))


def _pattern_family(mode: str) -> str:
    return {"TREND": "TREND_CONTINUATION", "RANGE": "RANGE_STRUCTURE", "REBOUND": "REBOUND_REVERSAL"}.get(mode, "REBOUND_REVERSAL")


def market_mode_for_setup(setup_type: str | None, environment_state: str | None = None) -> tuple[str, str]:
    """Return the explicit price-action mode, not an execution-period label.

    The explicit pattern mapping is authoritative.  When a caller only has an
    observation type, the current environment selects its mode; this is not a
    catch-all rebound label and keeps `SCALP`/`SWING` separate from behaviour.
    """

    kind = str(setup_type or "")
    if kind in TREND_PATTERN_TYPES:
        return "TREND", MODE_LABELS["TREND"]
    if kind in RANGE_PATTERN_TYPES:
        return "RANGE", MODE_LABELS["RANGE"]
    if kind in REBOUND_PATTERN_TYPES:
        return "REBOUND", MODE_LABELS["REBOUND"]
    if environment_state == "RANGE":
        return "RANGE", MODE_LABELS["RANGE"]
    if environment_state in {"BULL_TREND", "BEAR_TREND"}:
        return "TREND", MODE_LABELS["TREND"]
    return "REBOUND", MODE_LABELS["REBOUND"]


def _pattern(
    kind: str,
    direction: str,
    status: str,
    *,
    location: str,
    evidence: list[str],
    missing: list[str] | None = None,
    trigger: float | None = None,
    invalidation: float | None = None,
    structure: float | None = None,
    source_kind: str,
    environment_state: str | None = None,
    lifecycle: str | None = None,
    review_required: bool = False,
    next_condition: str | None = None,
    observed_at: str | None = None,
    confirmed_at: str | None = None,
    state_transition: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mode, label = market_mode_for_setup(kind, environment_state)
    missing = list(missing or [])
    lifecycle = lifecycle or status
    return {
        "type": kind,
        "direction": direction,
        "status": status,
        "lifecycle": lifecycle,
        "marketMode": mode,
        "marketModeLabel": label,
        "family": _pattern_family(mode),
        "location": location,
        "evidence": evidence,
        "missingConditions": missing,
        "nextCondition": next_condition or (missing[0] if missing else "等待下一根完整K线确认"),
        "triggerPrice": _round(trigger),
        "invalidationPrice": _round(invalidation),
        "structurePrice": _round(structure),
        "reviewRequired": review_required or status in {"WATCH", "NEEDS_REVIEW"},
        "sourcePages": _source_pages(source_kind),
        "observedAt": observed_at,
        "confirmedAt": confirmed_at,
        "stateTransition": state_transition or [],
    }


def _last_directional_run(facts: list[BarFacts], direction: str) -> int:
    count = 0
    for fact in reversed(facts):
        if fact.direction == direction:
            count += 1
        else:
            break
    return count


def _is_strong_bar(fact: BarFacts, direction: str) -> bool:
    return fact.direction == direction and fact.body_ratio >= 0.50 and ((fact.close_position >= 0.65) if direction == "BULL" else (fact.close_position <= 0.35))


def _trend_patterns(facts: list[BarFacts], environment: EnvironmentState, profile: dict[str, Any]) -> list[dict[str, Any]]:
    if environment.state not in {"BULL_TREND", "BEAR_TREND"} or len(facts) < 7:
        return []
    latest, previous = facts[-1], facts[-2]
    bullish = environment.state == "BULL_TREND"
    trend_direction, counter_direction = ("BULL", "BEAR") if bullish else ("BEAR", "BULL")
    action = "BUY" if bullish else "SELL"
    atr = max(latest.atr or latest.range, 1e-9)
    patterns: list[dict[str, Any]] = []
    recent = facts[-8:]
    counter_bars = [fact for fact in recent[-5:-1] if fact.direction == counter_direction]
    pullback_low = min(fact.low for fact in recent[-6:])
    pullback_high = max(fact.high for fact in recent[-6:])
    trigger = max(latest.high, previous.high) if bullish else min(latest.low, previous.low)
    invalidation = pullback_low - atr * profile["thresholds"]["stopBufferAtr"] if bullish else pullback_high + atr * profile["thresholds"]["stopBufferAtr"]
    breakout = latest.close > previous.high if bullish else latest.close < previous.low
    if counter_bars:
        attempts = 2 if len(counter_bars) >= 2 else 1
        confirmed = _is_strong_bar(latest, trend_direction) and breakout
        classic_entry = f"TREND_PULLBACK_{'H' if bullish else 'L'}{attempts}"
        patterns.append(_pattern(
            classic_entry, action,
            "CONFIRMED" if confirmed else "PENDING", location="趋势回撤/均线或前波段附近",
            evidence=[f"{MODE_LABELS['TREND']}环境仍有效", f"回撤包含{attempts}次逆势尝试", "当前价格重新测试顺势触发位"],
            missing=[] if confirmed else ["等待顺势信号K越过回撤触发位并出现后续跟随"], trigger=trigger,
            invalidation=invalidation, structure=pullback_low if bullish else pullback_high, source_kind="trend",
            environment_state=environment.state, lifecycle="CONFIRMED" if confirmed else "PENDING",
            next_condition="完整K线顺势突破触发位且不立即回到回撤内部" if not confirmed else "触发后继续检查跟随与第一磁铁空间",
            observed_at=latest.timestamp, confirmed_at=latest.timestamp if confirmed else None,
        ))
        patterns.append(_pattern(
            "TREND_TWO_LEG_PULLBACK" if attempts == 2 else "TREND_FIRST_PULLBACK", action,
            "CONFIRMED" if confirmed else "PENDING", location="趋势回撤/均线或前波段附近",
            evidence=["第一/两腿回撤的通用结构记录", f"对应 {'H' if bullish else 'L'}{attempts} 顺势尝试"],
            missing=[] if confirmed else ["等待顺势信号K越过回撤触发位并出现后续跟随"], trigger=trigger,
            invalidation=invalidation, structure=pullback_low if bullish else pullback_high, source_kind="trend",
            environment_state=environment.state, lifecycle="CONFIRMED" if confirmed else "PENDING",
            next_condition="完整K线顺势突破触发位且不立即回到回撤内部" if not confirmed else "触发后继续检查跟随与第一磁铁空间",
            observed_at=latest.timestamp, confirmed_at=latest.timestamp if confirmed else None,
        ))
    run = _last_directional_run(recent, trend_direction)
    if run >= 3:
        first_range = max(fact.range for fact in recent[:3])
        kind = "SPIKE_CHANNEL" if first_range >= atr * 1.25 else "WIDE_CHANNEL" if max(fact.range for fact in recent) >= atr * 1.45 else "STEP_CHANNEL"
        patterns.append(_pattern(kind, action, "WATCH", location="趋势通道末端/通道内", evidence=[f"最近连续 {run} 根{('多头' if bullish else '空头')}K", "通道延续但末端不追价"], missing=["等待第一次回撤、通道边界突破回测或微型通道反向突破"], trigger=trigger, invalidation=invalidation, source_kind="trend", environment_state=environment.state, lifecycle="WATCH", next_condition="不要追随末端K线；等待回撤或回测后确认", observed_at=latest.timestamp))
    micro_window = recent[-int(profile["thresholds"]["microChannelBars"]):]
    if len(micro_window) >= 4 and all(fact.direction == trend_direction for fact in micro_window[:-1]):
        break_trigger = min(fact.low for fact in micro_window[:-1]) if bullish else max(fact.high for fact in micro_window[:-1])
        micro_break = latest.close < break_trigger if bullish else latest.close > break_trigger
        patterns.append(_pattern("MICRO_CHANNEL_BREAK", "SELL" if bullish else "BUY", "PENDING" if micro_break else "WATCH", location="微型通道边界", evidence=["至少三根K线构成同向微型通道", "通道突破是回调或反转的早期证据"], missing=["需要主趋势线/关键回撤位突破及后续跟随，单次刺穿不够"], trigger=break_trigger, invalidation=max(fact.high for fact in micro_window) + atr * 0.2 if bullish else min(fact.low for fact in micro_window) - atr * 0.2, source_kind="trend", environment_state=environment.state, lifecycle="PENDING" if micro_break else "WATCH", review_required=True, next_condition="等待收盘穿过关键回撤位且出现后续跟随", observed_at=latest.timestamp))
    ema_gap = abs(latest.close - (latest.ema or latest.close)) / atr
    if ema_gap >= 1.25:
        patterns.append(_pattern("EMA_GAP_CONTEXT", action, "WATCH", location="价格远离20EMA", evidence=[f"收盘距20EMA约 {ema_gap:.2f} ATR", "远离均线时追价风险上升"], missing=["等待回撤、旗形或新的二次入场"], trigger=None, invalidation=None, source_kind="trend", environment_state=environment.state, lifecycle="WATCH", next_condition="回撤到可定义结构后再评估顺势入场", observed_at=latest.timestamp))
    impulse, flag = recent[:4], recent[-3:-1]
    if sum(fact.direction == trend_direction for fact in impulse) >= 3 and sum(fact.direction == counter_direction for fact in flag) >= 1:
        flag_high, flag_low = max(fact.high for fact in flag), min(fact.low for fact in flag)
        flag_confirmed = latest.close > flag_high if bullish else latest.close < flag_low
        patterns.append(_pattern("BULL_FLAG" if bullish else "BEAR_FLAG", action, "CONFIRMED" if flag_confirmed else "PENDING", location="趋势推动后的旗形回撤", evidence=["前段存在同向推动", "旗形回撤未破坏推动起点", "顺势突破旗形边界后才确认"], missing=[] if flag_confirmed else ["等待顺势收盘突破旗形边界并有跟随"], trigger=flag_high if bullish else flag_low, invalidation=flag_low - atr * 0.2 if bullish else flag_high + atr * 0.2, structure=flag_low if bullish else flag_high, source_kind="trend", environment_state=environment.state, lifecycle="CONFIRMED" if flag_confirmed else "PENDING", next_condition="旗形突破后观察是否守住突破边界", observed_at=latest.timestamp, confirmed_at=latest.timestamp if flag_confirmed else None))
    breakout_edge = max(fact.high for fact in recent[:-2]) if bullish else min(fact.low for fact in recent[:-2])
    retest_holds = latest.low <= breakout_edge + atr * 0.25 and latest.close > breakout_edge if bullish else latest.high >= breakout_edge - atr * 0.25 and latest.close < breakout_edge
    if retest_holds:
        retest_confirmed = _is_strong_bar(latest, trend_direction)
        patterns.append(_pattern("BREAKOUT_RETEST_UP" if bullish else "BREAKOUT_RETEST_DOWN", action, "CONFIRMED" if retest_confirmed else "PENDING", location="趋势突破回测", evidence=["价格曾穿过前方结构边界", "回测未有效回到原结构内部"], missing=[] if retest_confirmed else ["等待回测后顺势K收盘确认"], trigger=latest.high if bullish else latest.low, invalidation=breakout_edge - atr * 0.2 if bullish else breakout_edge + atr * 0.2, structure=breakout_edge, source_kind="trend", environment_state=environment.state, lifecycle="CONFIRMED" if retest_confirmed else "PENDING", next_condition="回测后收盘保持在突破方向一侧", observed_at=latest.timestamp, confirmed_at=latest.timestamp if retest_confirmed else None))
    return patterns


def _range_patterns(facts: list[BarFacts], environment: EnvironmentState, profile: dict[str, Any]) -> list[dict[str, Any]]:
    if environment.state != "RANGE" or not environment.range_zone or len(facts) < 6:
        return []
    latest, previous = facts[-1], facts[-2]
    zone = environment.range_zone
    low, high, mid, width = (_safe_float(zone.get(key)) for key in ("low", "high", "mid", "width"))
    atr = max(latest.atr or latest.range, 1e-9)
    tolerance = atr * profile["thresholds"]["levelToleranceAtr"]
    patterns: list[dict[str, Any]] = []
    lower = latest.close <= low + width * 0.30 or latest.low <= low + tolerance
    upper = latest.close >= high - width * 0.30 or latest.high >= high - tolerance
    bull_confirm = _is_strong_bar(latest, "BULL") and latest.close > previous.high
    bear_confirm = _is_strong_bar(latest, "BEAR") and latest.close < previous.low
    if lower:
        patterns.append(_pattern("RANGE_EDGE_FADE_UP", "BUY", "CONFIRMED" if bull_confirm else "PENDING", location="区间下沿", evidence=["区间边界已定义", "价格测试下沿区域", "下沿反转必须有突破信号K"], missing=[] if bull_confirm else ["等待向上信号K突破并有后续跟随"], trigger=max(latest.high, previous.high), invalidation=low - tolerance, structure=low, source_kind="range", environment_state=environment.state, lifecycle="CONFIRMED" if bull_confirm else "PENDING", next_condition="完整K线突破反转信号高点且不重新跌破下沿", observed_at=latest.timestamp, confirmed_at=latest.timestamp if bull_confirm else None))
    elif upper:
        patterns.append(_pattern("RANGE_EDGE_FADE_DOWN", "SELL", "CONFIRMED" if bear_confirm else "PENDING", location="区间上沿", evidence=["区间边界已定义", "价格测试上沿区域", "上沿反转必须有跌破信号K"], missing=[] if bear_confirm else ["等待向下信号K跌破并有后续跟随"], trigger=min(latest.low, previous.low), invalidation=high + tolerance, structure=high, source_kind="range", environment_state=environment.state, lifecycle="CONFIRMED" if bear_confirm else "PENDING", next_condition="完整K线跌破反转信号低点且不重新站回上沿", observed_at=latest.timestamp, confirmed_at=latest.timestamp if bear_confirm else None))
    else:
        patterns.append(_pattern("RANGE_MIDDLE", "NONE", "WATCH", location="区间中部", evidence=["价格位于已定义区间中线附近", "中部缺少明确风险边界"], missing=["等待区间边缘反转，或等待突破-回测确认"], source_kind="range", environment_state=environment.state, lifecycle="WATCH", next_condition="先到边缘或形成已确认突破回测，再考虑交易", observed_at=latest.timestamp))
    recent = facts[-6:]
    tight_span = max(fact.high for fact in recent) - min(fact.low for fact in recent)
    if tight_span <= atr * profile["thresholds"]["tightRangeAtr"]:
        patterns.append(_pattern("TIGHT_RANGE_IRON_WIRE", "NONE", "WATCH", location="紧密区间", evidence=[f"最近{len(recent)}根K线总波幅约 {tight_span / atr:.2f} ATR", "K线重叠高，方向优势低"], missing=["等待有效突破、跟随和回测"], source_kind="range", environment_state=environment.state, lifecycle="WATCH", next_condition="不要在铁丝网中部追单；等待突破后回测", observed_at=latest.timestamp))
    highs, lows = [fact.high for fact in recent], [fact.low for fact in recent]
    contracting = highs[-1] < highs[0] and lows[-1] > lows[0]
    expanding = highs[-1] > highs[0] and lows[-1] < lows[0]
    if contracting or expanding:
        kind = "TRIANGLE_COMPRESSION" if contracting else "TRIANGLE_EXPANSION"
        patterns.append(_pattern(kind, "NONE", "WATCH", location="区间三角形", evidence=["高低点" + ("收敛" if contracting else "扩张"), "仍在区间内部，方向需由突破后确认"], missing=["等待突破、后续跟随及失败/回测判定"], source_kind="range", environment_state=environment.state, lifecycle="WATCH", next_condition="突破后先判定是否守住边界，再决定延续或失败", observed_at=latest.timestamp))
    alternating = sum(1 for left, right in zip(recent, recent[1:]) if left.direction != right.direction and left.direction != "NEUTRAL" and right.direction != "NEUTRAL")
    if alternating >= 3:
        patterns.append(_pattern("ABC_RANGE_LEGS", "NONE", "WATCH", location="区间腿数/ABC", evidence=["短腿方向交替", "腿数仅用作背景和目标评估"], missing=["需要边缘位置或有效突破确认"], source_kind="range", environment_state=environment.state, lifecycle="WATCH", next_condition="不因单一ABC计数入场，先确认区间位置", observed_at=latest.timestamp))
    prior_high = max(fact.high for fact in facts[-12:-2])
    prior_low = min(fact.low for fact in facts[-12:-2])
    upward = latest.close > prior_high + tolerance
    downward = latest.close < prior_low - tolerance
    previous_upward = previous.close > prior_high + tolerance
    previous_downward = previous.close < prior_low - tolerance
    if upward or downward:
        direction = "BUY" if upward else "SELL"
        boundary = prior_high if upward else prior_low
        direction_name = "BULL" if direction == "BUY" else "BEAR"
        strong_break = _is_strong_bar(latest, direction_name)
        continuation = (direction == "BUY" and previous_upward) or (direction == "SELL" and previous_downward)
        status = "CONFIRMED" if strong_break or continuation else "PENDING"
        kind = "RANGE_BREAKOUT_CONTINUATION" if continuation else "RANGE_BREAKOUT_CONFIRMED" if status == "CONFIRMED" else "RANGE_BREAKOUT_PENDING"
        patterns.append(_pattern(kind, direction, status, location="区间突破边界", evidence=["价格收盘穿过近期区间边界", "突破生命周期以守住、回测或回到区间判定"], missing=[] if status == "CONFIRMED" else ["等待同向收盘跟随或突破后回测守住边界"], trigger=boundary, invalidation=boundary - tolerance if direction == "BUY" else boundary + tolerance, structure=boundary, source_kind="range", environment_state=environment.state, lifecycle=status, next_condition="突破后等待守住边界；回到区间并出现反向跟随时按失败结构复核", observed_at=latest.timestamp, confirmed_at=latest.timestamp if status == "CONFIRMED" else None))
    failed_up = previous_upward and latest.close < prior_high - tolerance and _is_strong_bar(latest, "BEAR")
    failed_down = previous_downward and latest.close > prior_low + tolerance and _is_strong_bar(latest, "BULL")
    if failed_up or failed_down:
        failed_boundary = prior_high if failed_up else prior_low
        failed_kind = "FAILED_BULLISH_BREAKOUT" if failed_up else "FAILED_BEARISH_BREAKOUT"
        failed_direction = "SELL" if failed_up else "BUY"
        patterns.append(_pattern(failed_kind, failed_direction, "FAILED", location="突破回到区间", evidence=["前一根完整K线曾收在区间外", "当前强反向K线收回原区间内部"], missing=[], trigger=failed_boundary, invalidation=failed_boundary + tolerance if failed_up else failed_boundary - tolerance, structure=failed_boundary, source_kind="range", environment_state=environment.state, lifecycle="FAILED", next_condition="失败突破只记录为反向背景；仍等待区间边缘/反转链的独立确认", observed_at=latest.timestamp, confirmed_at=latest.timestamp))
    retest_up = previous_upward and latest.low <= prior_high + tolerance and latest.close > prior_high
    retest_down = previous_downward and latest.high >= prior_low - tolerance and latest.close < prior_low
    if retest_up or retest_down:
        direction, boundary = ("BUY", prior_high) if retest_up else ("SELL", prior_low)
        confirmed = latest.close > previous.close if retest_up else latest.close < previous.close
        patterns.append(_pattern("RANGE_BREAKOUT_RETEST", direction, "CONFIRMED" if confirmed else "PENDING", location="突破边界回测", evidence=["前一根K线保持在区间外", "当前K线测试原区间边界"], missing=[] if confirmed else ["等待回测后同向收盘跟随"], trigger=boundary, invalidation=boundary - tolerance if retest_up else boundary + tolerance, structure=boundary, source_kind="range", environment_state=environment.state, lifecycle="CONFIRMED" if confirmed else "PENDING", next_condition="回测守住边界才允许按突破延续处理", observed_at=latest.timestamp, confirmed_at=latest.timestamp if confirmed else None))
    before_previous = facts[-3]
    failed_failure = (
        before_previous.close > prior_high + tolerance and previous.close < prior_high - tolerance and latest.close > prior_high + tolerance
    ) or (
        before_previous.close < prior_low - tolerance and previous.close > prior_low + tolerance and latest.close < prior_low - tolerance
    )
    if failed_failure:
        direction = "BUY" if latest.close > prior_low else "SELL"
        boundary = prior_low if direction == "BUY" else prior_high
        patterns.append(_pattern("BREAKOUT_FAILURE_OF_FAILURE", direction, "CONFIRMED" if _is_strong_bar(latest, "BULL" if direction == "BUY" else "BEAR") else "PENDING", location="失败突破再次失败", evidence=["前一次突破已未能延续", "价格重新穿过失败方向的区间边界"], missing=["需要同向收盘跟随，不能只凭一次回到边界外"], trigger=boundary, invalidation=boundary - tolerance if direction == "BUY" else boundary + tolerance, structure=boundary, source_kind="range", environment_state=environment.state, lifecycle="CONFIRMED" if _is_strong_bar(latest, "BULL" if direction == "BUY" else "BEAR") else "PENDING", next_condition="等待第二次失败后的顺势确认", observed_at=latest.timestamp))
    return patterns


def _reversal_chain(facts: list[BarFacts], environment: EnvironmentState, profile: dict[str, Any], swings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if environment.state not in {"BULL_TREND", "BEAR_TREND", "TRANSITION"} or len(facts) < 8:
        return []
    latest, previous = facts[-1], facts[-2]
    trend_up = environment.state != "BEAR_TREND"
    primary_direction, counter_direction = ("BULL", "BEAR") if trend_up else ("BEAR", "BULL")
    reversal_action = "SELL" if trend_up else "BUY"
    atr = max(latest.atr or latest.range, 1e-9)
    recent = facts[-10:]
    extension = abs(latest.close - (latest.ema or latest.close)) / atr >= 1.6 or sum(fact.direction == primary_direction for fact in recent[:-2]) >= 6
    climax = any(fact.range >= atr * 1.8 and fact.direction == primary_direction for fact in recent[-4:])
    counter_impulse = _is_strong_bar(latest, counter_direction) or (_is_strong_bar(previous, counter_direction) and latest.direction == counter_direction)
    main_break = latest.close < (latest.ema or latest.close) if trend_up else latest.close > (latest.ema or latest.close)
    prior_extreme = max(fact.high for fact in recent[:-2]) if trend_up else min(fact.low for fact in recent[:-2])
    extreme_tested = latest.high >= prior_extreme - atr * 0.35 if trend_up else latest.low <= prior_extreme + atr * 0.35
    signal = latest.close < previous.low if trend_up else latest.close > previous.high
    follow = len(recent) >= 3 and (latest.close < previous.close < recent[-3].close if trend_up else latest.close > previous.close > recent[-3].close)
    states = ["TREND_ACTIVE"]
    if extension or climax: states.append("EXTENDED_OR_CLIMAX")
    if counter_impulse: states.append("COUNTER_IMPULSE")
    if main_break: states.append("MAIN_TRENDLINE_BROKEN")
    if extreme_tested: states.append("EXTREME_TESTED")
    if signal: states.append("SIGNAL")
    if extension and counter_impulse and main_break and extreme_tested and signal and follow: states.append("CONFIRMED")
    confirmed = states[-1] == "CONFIRMED"
    kind = "MAJOR_BEARISH_REVERSAL" if trend_up else "MAJOR_BULLISH_REVERSAL"
    missing = [] if confirmed else ["反转需同时出现趋势受损、极点测试、强反向冲击、信号突破与后续跟随"]
    transition = [{"from": states[index], "to": states[index + 1], "at": latest.timestamp} for index in range(len(states) - 1)]
    patterns = [_pattern(kind, reversal_action, "CONFIRMED" if confirmed else "NEEDS_REVIEW", location="趋势极端/反转链", evidence=[f"反转链已到 {states[-1]}", " > ".join(states), "单根反向K线不单独改变主趋势"], missing=missing, trigger=min(latest.low, previous.low) if trend_up else max(latest.high, previous.high), invalidation=max(latest.high, previous.high) + atr * 0.2 if trend_up else min(latest.low, previous.low) - atr * 0.2, structure=prior_extreme, source_kind="reversal", environment_state="TRANSITION", lifecycle="CONFIRMED" if confirmed else "NEEDS_REVIEW", review_required=not confirmed, next_condition="补齐反转链缺失步骤后才形成反转计划", observed_at=latest.timestamp, confirmed_at=latest.timestamp if confirmed else None, state_transition=transition)]
    if climax:
        patterns.append(_pattern("CLIMAX_SPIKE_REVERSAL", reversal_action, "NEEDS_REVIEW", location="高潮/尖峰末端", evidence=["近期出现相对ATR显著扩张的趋势K", "高潮只提高反转警觉，不等于反转确认"], missing=["等待趋势线/关键回撤位突破、二次测试和后续跟随"], trigger=min(latest.low, previous.low) if trend_up else max(latest.high, previous.high), invalidation=prior_extreme + atr * 0.2 if trend_up else prior_extreme - atr * 0.2, source_kind="reversal", environment_state="TRANSITION", lifecycle="NEEDS_REVIEW", review_required=True, next_condition="高潮后先等第二次测试或主趋势破坏", observed_at=latest.timestamp))
    if latest.range >= atr * 1.5 and ((trend_up and latest.close < latest.open) or (not trend_up and latest.close > latest.open)):
        patterns.append(_pattern("V_REVERSAL_WARNING", reversal_action, "WATCH", location="快速反向波动", evidence=["出现大幅反向K线", "V形快速转折需避免追价"], missing=["等待回测、关键位突破和后续跟随"], source_kind="reversal", environment_state="TRANSITION", lifecycle="WATCH", review_required=True, next_condition="不以单根V形K线反转；等待结构确认", observed_at=latest.timestamp))
    if "INSIDE_BAR" in latest.pattern_labels and (extension or climax):
        break_direction = "SELL" if trend_up else "BUY"
        patterns.append(_pattern("FINAL_FLAG_OR_INSIDE_BREAK", break_direction, "WATCH", location="延伸趋势末端的旗形/内包", evidence=["主趋势已延伸或出现高潮", "末端出现内包/小旗形压缩"], missing=["等待突破旗形反方向、主趋势线破坏和后续跟随"], trigger=latest.low if trend_up else latest.high, invalidation=latest.high + atr * 0.2 if trend_up else latest.low - atr * 0.2, structure=latest.low if trend_up else latest.high, source_kind="reversal", environment_state="TRANSITION", lifecycle="WATCH", review_required=True, next_condition="最终旗形必须先失败再有反向确认，不能预先猜顶底", observed_at=latest.timestamp))
    expanding = max(fact.high for fact in recent[-5:]) - min(fact.low for fact in recent[-5:]) > (max(fact.high for fact in recent[:5]) - min(fact.low for fact in recent[:5])) * 1.35
    if expanding and (extension or climax):
        patterns.append(_pattern("EXPANSION_REVERSAL", reversal_action, "NEEDS_REVIEW", location="扩张三角形/波动扩张", evidence=["最近波动范围相对前段明显扩张", "扩张末端提高双向失败和反转可能"], missing=["等待关键摆动突破、回测失败和后续跟随"], source_kind="reversal", environment_state="TRANSITION", lifecycle="NEEDS_REVIEW", review_required=True, next_condition="用突破后的跟随确定方向，而不是预测扩张末端", observed_at=latest.timestamp))
    if counter_impulse and not main_break:
        patterns.append(_pattern("REVERSAL_FAILURE", "BUY" if trend_up else "SELL", "WATCH", location="反转尝试失败", evidence=["出现反向冲击但主趋势关键位未被破坏", "原趋势仍可能恢复"], missing=["等待价格重新站回/跌回主趋势关键位并有跟随"], source_kind="reversal", environment_state=environment.state, lifecycle="WATCH", next_condition="反向尝试失败后只按主趋势恢复确认处理", observed_at=latest.timestamp))
    if main_break and not counter_impulse:
        patterns.append(_pattern("REVERSAL_FAILURE_OF_FAILURE", reversal_action, "PENDING", location="原趋势恢复测试", evidence=["趋势线/均线被测试", "反向冲击没有保持，需复核原趋势是否恢复"], missing=["等待价格重回原趋势方向并有后续跟随"], source_kind="reversal", environment_state="TRANSITION", lifecycle="PENDING", next_condition="确认失败的反转是否再次失败，避免在过渡区间预判", observed_at=latest.timestamp))
    highs = [swing for swing in swings if swing.get("kind") == "SWING_HIGH"][-3:]
    lows = [swing for swing in swings if swing.get("kind") == "SWING_LOW"][-3:]
    points = highs if trend_up else lows
    if len(points) == 3:
        prices = [_safe_float(point.get("price")) for point in points]
        shoulders_close = abs(prices[0] - prices[2]) <= atr * profile["thresholds"]["headShoulderToleranceAtr"]
        head_higher = prices[1] > max(prices[0], prices[2]) if trend_up else prices[1] < min(prices[0], prices[2])
        if shoulders_close and head_higher:
            neckline = min(fact.low for fact in facts[int(points[0]["index"]):int(points[2]["index"]) + 1]) if trend_up else max(fact.high for fact in facts[int(points[0]["index"]):int(points[2]["index"]) + 1])
            neck_break = latest.close < neckline if trend_up else latest.close > neckline
            patterns.append(_pattern("HEAD_SHOULDERS_REVERSAL", reversal_action, "CONFIRMED" if neck_break and follow else "PENDING", location="三段摆动与颈线", evidence=["两侧肩部在ATR容差内", "中间摆动为头部", "颈线必须收盘突破并有跟随"], missing=[] if neck_break and follow else ["等待收盘突破颈线并出现后续跟随"], trigger=neckline, invalidation=prices[2] + atr * 0.2 if trend_up else prices[2] - atr * 0.2, structure=neckline, source_kind="reversal", environment_state="TRANSITION", lifecycle="CONFIRMED" if neck_break and follow else "PENDING", next_condition="颈线突破后不立即回到肩部结构内", observed_at=latest.timestamp, confirmed_at=latest.timestamp if neck_break and follow else None))
    pair_points = (highs[-2:] if trend_up else lows[-2:])
    if len(pair_points) == 2 and int(pair_points[-1].get("index", -99)) >= len(facts) - 8:
        first_point, second_point = pair_points
        first_price, second_price = _safe_float(first_point.get("price")), _safe_float(second_point.get("price"))
        equal_extremes = abs(first_price - second_price) <= atr * profile["thresholds"]["headShoulderToleranceAtr"]
        left, right = sorted((int(first_point["index"]), int(second_point["index"])))
        if equal_extremes and right > left:
            neckline = min(fact.low for fact in facts[left:right + 1]) if trend_up else max(fact.high for fact in facts[left:right + 1])
            neck_break = latest.close < neckline if trend_up else latest.close > neckline
            double_confirmed = confirmed and neck_break
            double_kind = "DOUBLE_TOP" if trend_up else "DOUBLE_BOTTOM"
            patterns.append(_pattern(
                double_kind,
                reversal_action,
                "CONFIRMED" if double_confirmed else "NEEDS_REVIEW",
                location="两次相近极点与颈线",
                evidence=["两次极点处于ATR容差内", "双顶/双底本身只说明测试，必须结合完整反转链", "颈线突破与后续跟随才改变主趋势"],
                missing=[] if double_confirmed else ["等待趋势受损、颈线收盘突破与后续跟随；不能把双顶/双底单独当作反转"],
                trigger=neckline,
                invalidation=second_price + atr * 0.2 if trend_up else second_price - atr * 0.2,
                structure=neckline,
                source_kind="reversal",
                environment_state="TRANSITION",
                lifecycle="CONFIRMED" if double_confirmed else "NEEDS_REVIEW",
                review_required=not double_confirmed,
                next_condition="颈线突破后仍需完整反转链确认；强趋势中的双底/双顶可以只是延续旗形",
                observed_at=latest.timestamp,
                confirmed_at=latest.timestamp if double_confirmed else None,
                state_transition=transition,
            ))
    wedge_points = points if len(points) == 3 else []
    if wedge_points and int(wedge_points[-1].get("index", -99)) >= len(facts) - 8:
        wedge_prices = [_safe_float(point.get("price")) for point in wedge_points]
        third_push = wedge_prices[0] <= wedge_prices[1] <= wedge_prices[2] if trend_up else wedge_prices[0] >= wedge_prices[1] >= wedge_prices[2]
        if third_push:
            left, right = int(wedge_points[0]["index"]), int(wedge_points[-1]["index"])
            wedge_neckline = min(fact.low for fact in facts[left:right + 1]) if trend_up else max(fact.high for fact in facts[left:right + 1])
            wedge_break = latest.close < wedge_neckline if trend_up else latest.close > wedge_neckline
            wedge_confirmed = confirmed and wedge_break
            patterns.append(_pattern(
                "WEDGE_THIRD_PUSH",
                reversal_action,
                "CONFIRMED" if wedge_confirmed else "NEEDS_REVIEW",
                location="三次推动/楔形末端",
                evidence=["已识别同向三次摆动推动", "第三推动只提高衰竭警觉", "仍要求趋势受损、关键位突破和跟随"],
                missing=[] if wedge_confirmed else ["等待楔形边界/颈线突破、极点测试和反向跟随"],
                trigger=wedge_neckline,
                invalidation=wedge_prices[-1] + atr * 0.2 if trend_up else wedge_prices[-1] - atr * 0.2,
                structure=wedge_neckline,
                source_kind="reversal",
                environment_state="TRANSITION",
                lifecycle="CONFIRMED" if wedge_confirmed else "NEEDS_REVIEW",
                review_required=not wedge_confirmed,
                next_condition="第三推动后不预判反转；仅在完整反转链和颈线突破同时成立后处理",
                observed_at=latest.timestamp,
                confirmed_at=latest.timestamp if wedge_confirmed else None,
                state_transition=transition,
            ))
    return patterns


def detect_pattern_families(facts: list[BarFacts], environment: EnvironmentState, profile: dict[str, Any] | None = None, swings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Detect codeable trend/range/reversal candidates from complete bars only."""

    if len(facts) < 6:
        return []
    cfg = get_price_action_profile(profile)
    swings = swings or _confirmed_swings(facts, int(cfg["swingRadius"]))
    candidates = [*_trend_patterns(facts, environment, cfg), *_range_patterns(facts, environment, cfg), *_reversal_chain(facts, environment, cfg, swings)]
    ranked = {"CONFIRMED": 6, "FAILED": 5, "PENDING": 4, "NEEDS_REVIEW": 3, "WATCH": 2, "EXPIRED": 1}
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (str(candidate.get("type")), str(candidate.get("direction")), str(candidate.get("marketMode")))
        current = selected.get(key)
        if current is None or ranked.get(str(candidate.get("status")), 0) > ranked.get(str(current.get("status")), 0):
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: (-ranked.get(str(item.get("status")), 0), str(item.get("type"))))


def _state_transition(status: str, observed: str, confirmed: str | None) -> list[dict[str, Any]]:
    transitions = [{"from": "OBSERVED", "to": "PENDING", "at": observed}]
    if confirmed:
        transitions.append({"from": "PENDING", "to": status, "at": confirmed})
    return transitions


def enrich_setup(setup: dict[str, Any], facts: list[BarFacts], volume: dict[str, Any], timeframe: str, profile: dict[str, Any]) -> dict[str, Any]:
    observed = facts[-1].timestamp if facts else ""
    status = str(setup.get("status") or "WATCH")
    market_mode, market_mode_label = market_mode_for_setup(setup.get("type") or setup.get("kind"), setup.get("environmentState"))
    market_mode = str(setup.get("marketMode") or market_mode)
    market_mode_label = str(setup.get("marketModeLabel") or MODE_LABELS.get(market_mode) or market_mode_label)
    observed = str(setup.get("observedAt") or observed)
    confirmed = str(setup.get("confirmedAt") or observed) if status == "CONFIRMED" else None
    kind = str(setup.get("type") or setup.get("kind") or "UNKNOWN")
    evidence = list(setup.get("evidence") or [])
    evidence_ids = [_stable_id("evidence", kind, text) for text in evidence]
    source_kind = "reversal" if "REVERSAL" in kind or "DOUBLE" in kind or "WEDGE" in kind else "range" if "RANGE" in kind or "BREAKOUT" in kind or "FLAG" in kind else "trend"
    result = dict(setup)
    result.update(Setup(
        setup_id=_stable_id("setup", kind, observed, timeframe), kind=kind,
        direction=str(setup.get("direction") or "NONE"), status=status, observed_at=observed,
        confirmed_at=confirmed, trigger_price=setup.get("triggerPrice"), invalidation_price=setup.get("invalidationPrice"),
        location=str(setup.get("location") or "UNKNOWN"), evidence_ids=evidence_ids, evidence=evidence,
        missing_conditions=list(setup.get("missingConditions") or []),
        alternative_scenario=str(setup.get("alternativeScenario") or "若价格重新回到结构内部，则优先按延续/失败突破场景复核。"),
        review_required=bool(setup.get("reviewRequired")), source_pages=list(setup.get("sourcePages") or _source_pages(source_kind)),
        state_transition=list(setup.get("stateTransition") or _state_transition(status, observed, confirmed)), volume_evidence=dict(setup.get("volume") or volume),
        market_mode=market_mode, market_mode_label=market_mode_label,
        family=str(setup.get("family") or _pattern_family(market_mode)), lifecycle=str(setup.get("lifecycle") or status),
        next_condition=str(setup.get("nextCondition") or (setup.get("missingConditions") or ["等待完整K线确认"])[0]),
    ).to_dict())
    result["family"] = str(setup.get("family") or _pattern_family(market_mode))
    result["lifecycle"] = str(setup.get("lifecycle") or status)
    result["structurePrice"] = setup.get("structurePrice")
    return result


def _magnet_zones(levels: Iterable[dict[str, Any]], timeframe: str, latest: BarFacts, atr: float) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    for level in levels:
        price = _safe_float(level.get("price"))
        if price <= 0:
            continue
        low = _safe_float(level.get("zoneLow"), price - atr * 0.18)
        high = _safe_float(level.get("zoneHigh"), price + atr * 0.18)
        role = str(level.get("role") or "MAGNET")
        zones.append(MagnetZone(_stable_id("magnet", level.get("kind"), price, timeframe), role, low, high, str(level.get("source") or level.get("kind") or "price-action"), timeframe, level.get("confirmedAt") or latest.timestamp, role not in {"EMA20", "RANGE_MIDDLE_MAGNET"}, (price - latest.close) / max(atr, 1e-9)).to_dict())
    return zones


def build_trade_plan(setup: dict[str, Any], levels: list[dict[str, Any]], latest: BarFacts, environment: dict[str, Any], volume: dict[str, Any], timeframe: str = "1d", profile: dict[str, Any] | None = None, account: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = get_price_action_profile(profile, timeframe)
    thresholds = cfg["thresholds"]
    direction = str(setup.get("direction") or "NONE")
    market_mode, market_mode_label = market_mode_for_setup(setup.get("type") or setup.get("kind"), environment.get("state"))
    market_mode = str(setup.get("marketMode") or market_mode)
    market_mode_label = str(setup.get("marketModeLabel") or MODE_LABELS.get(market_mode) or market_mode_label)
    trigger = _safe_float(setup.get("triggerPrice")) or None
    invalidation = _safe_float(setup.get("invalidationPrice")) or None
    status = str(setup.get("status") or "WATCH")
    atr = latest.atr or latest.range
    buffer = atr * thresholds["stopBufferAtr"]
    entry_buffer = atr * thresholds["entryBufferAtr"]
    execution_action = "BUY_LONG" if direction == "BUY" else "SELL_EXISTING_LONG" if direction == "SELL" else "OBSERVE"
    execution_constraint = "CASH_LONG_ONLY" if direction == "BUY" else "EXISTING_LONG_ONLY" if direction == "SELL" else "NO_NEW_SHORT"
    position_required = direction == "SELL"
    short_selling_allowed = False
    target_semantics = "LONG_PROFIT_TARGET" if direction == "BUY" else "DOWNSIDE_REFERENCE_ONLY" if direction == "SELL" else "OBSERVATION_ONLY"
    if direction == "BUY":
        entry = trigger + entry_buffer if trigger else None
        stop = invalidation - buffer if invalidation else None
        candidates = [level for level in levels if _safe_float(level.get("price")) > (entry or latest.close) and ("RESISTANCE" in str(level.get("role") or "") or "UPSIDE" in str(level.get("role") or ""))]
        first = min(candidates, key=lambda item: _safe_float(item.get("price")), default=None)
        target_price = _safe_float(first.get("price")) if first else None
        # The extension is a second confirmed structure/magnet, not a fixed
        # percentage projection. It is available only for continuation mode.
        extension_candidates = [
            level for level in levels
            if target_price
            and _safe_float(level.get("price")) > target_price + atr * 0.15
            and ("RESISTANCE" in str(level.get("role") or "") or "UPSIDE" in str(level.get("role") or ""))
        ]
        extension = min(extension_candidates, key=lambda item: _safe_float(item.get("price")), default=None) if market_mode == "TREND" and status == "CONFIRMED" else None
    elif direction == "SELL":
        # Downward patterns are sell/trim observations for an existing long.
        # They must never become a short entry, short stop, target, or R plan.
        entry = stop = target_price = None
        first = None
        extension = None
    else:
        entry = stop = target_price = None
        first = extension = None
    room_r = None
    if entry and stop and target_price and direction == "BUY" and entry > stop:
        risk = abs(entry - stop)
        room_r = abs(target_price - entry) / max(risk, 1e-9)
    blocked: list[str] = []
    if status not in {"CONFIRMED", "SCALP"}:
        # WATCH/PENDING means the setup is observable but not confirmed yet;
        # reserve DATA_INSUFFICIENT for genuinely missing bars or fields.
        blocked.append("REVIEW_REQUIRED" if status in {"NEEDS_REVIEW", "WATCH", "PENDING", "FAILED"} or setup.get("reviewRequired") else "DATA_INSUFFICIENT")
    if direction == "SELL":
        blocked.extend(["EXISTING_LONG_REQUIRED", "SHORT_SELLING_DISABLED"])
    if not latest.completed:
        blocked.append("INCOMPLETE_BAR")
    if not trigger:
        blocked.append("UNKNOWN_TRIGGER")
    if direction == "BUY" and (not invalidation or not stop):
        blocked.append("UNKNOWN_STOP")
    if direction == "BUY" and not target_price:
        blocked.append("UNKNOWN_TARGET")
    if room_r is not None and room_r < thresholds["minimumRoomR"]:
        blocked.append("INSUFFICIENT_ROOM")
    account = account or {}
    entry_value = entry * _safe_float(account.get("lotSize"), cfg["risk"]["lotSize"]) if entry else 0
    if entry and account.get("availableCash") is not None and entry_value > _safe_float(account.get("availableCash")):
        blocked.append("CASH_LIMIT")
    if entry and stop and account.get("accountValue") and abs(entry - stop) / entry > cfg["risk"]["hardStopPct"]:
        blocked.append("RISK_LIMIT")
    plan_status = "CONFIRMED" if not blocked and status == "CONFIRMED" else "BLOCKED" if blocked else "WAIT"
    executable = direction == "BUY" and plan_status == "CONFIRMED"
    plan_id = _stable_id("plan", setup.get("setupId") or setup.get("type"), latest.timestamp, timeframe)
    cancellation = ["下一根K线未收盘或交易会话过期", "价格重新进入结构内部或触发结构失效位", "高周期上下文改变或目标磁力位已消耗", "出现涨跌停、低流动性、每日风险锁或重复信号"]
    if direction == "SELL":
        cancellation = ["下一根K线未收盘或交易会话过期", "价格重新站回卖出确认失效位", "高周期上下文改变或下行确认失败", "卖出条件未满足时保持原持仓纪律"]
    return TradePlan(
        plan_id=plan_id, setup_id=str(setup.get("setupId") or _stable_id("setup", setup.get("type"), latest.timestamp, timeframe)), direction=direction,
        mode="SCALP" if timeframe.endswith("m") else "SWING", status=plan_status, trigger_price=_round(trigger),
        entry_zone={"low": _round(trigger or entry), "high": _round(entry or trigger)} if entry else None,
        entry_order_type="STOP_ENTRY" if direction == "BUY" and status == "CONFIRMED" else "SELL_CONFIRMATION" if direction == "SELL" else "MARKET_REVIEW",
        entry_limit=_round(entry), structural_reference=_round(_safe_float(setup.get("structurePrice")) or trigger), structural_invalidation=_round(invalidation), initial_stop=_round(stop), active_stop=_round(stop), stop_state="not_applicable" if direction == "SELL" else "initial" if stop else "manual_review",
        first_target={"price": _round(target_price), "zoneLow": _round(_safe_float(first.get("zoneLow"), target_price) if first else target_price), "zoneHigh": _round(_safe_float(first.get("zoneHigh"), target_price) if first else target_price), "source": str(first.get("source") if first else "unavailable"), "role": "range_opposite_edge" if market_mode == "RANGE" else "first_magnet", "semantics": "可能测试/停顿区；到达后按计划分批止盈，不保证成交", "isGuaranteed": False} if target_price else None,
        extension_target={"price": _round(_safe_float(extension.get("price"))), "source": str(extension.get("source") or "趋势延续后的下一结构磁铁"), "role": "trend_extension", "semantics": "仅在第一目标后出现新的趋势确认时启用", "isGuaranteed": False} if extension else None,
        cancellation_conditions=cancellation, room_r=_round(room_r), risk_status="NOT_APPLICABLE" if direction == "SELL" else "BLOCKED" if blocked else "PASS", blocked_reasons=sorted(set(blocked)), reason_clusters=[list(environment.get("evidence") or [])[:2], list(setup.get("evidence") or [])[:2]], observed_at=latest.timestamp, confirmed_at=latest.timestamp if status == "CONFIRMED" else None, session_expiry=latest.timestamp if timeframe.endswith("m") else None, source_pages=list(setup.get("sourcePages") or _source_pages("candlestick")), alternative_scenario="下行结构用于卖出确认，价格重新回到结构内部则回到观察。" if direction == "SELL" else "若触发后没有跟随，按失败突破或回到区间处理。",
        execution_action=execution_action, execution_constraint=execution_constraint, position_required=position_required, short_selling_allowed=short_selling_allowed, executable=executable, target_semantics=target_semantics, market_mode=market_mode, market_mode_label=market_mode_label,
    ).to_dict()


def update_dynamic_stop(plan: dict[str, Any], facts: list[BarFacts], position: dict[str, Any] | None = None, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply monotonic stop management to a plan using completed bars only."""

    if not plan or not facts:
        return plan
    result = dict(plan)
    direction = str(result.get("direction") or "NONE")
    if direction == "SELL":
        # A-share cash accounts do not carry short positions. A SELL contract
        # is only an existing-long defense observation, so short trailing-stop
        # math must never run here.
        result.update({"stopState": "not_applicable", "dynamicStopAllowed": False, "updatedAt": facts[-1].timestamp})
        return result
    entry = _safe_float(result.get("entryLimit") or (position or {}).get("entryPrice"))
    initial = _safe_float(result.get("initialStop"))
    if not entry or not initial:
        result["stopState"] = "manual_review"
        return result
    latest = facts[-1]
    risk = abs(entry - initial)
    favourable = (latest.close - entry) if direction == "BUY" else (entry - latest.close)
    events = list(result.get("stopEvents") or [])
    active = _safe_float(result.get("activeStop"), initial)
    state = str(result.get("stopState") or "initial")
    if favourable >= risk:
        candidate = entry if direction == "BUY" else entry
        if (direction == "BUY" and candidate > active) or (direction == "SELL" and candidate < active):
            events.append({"oldPrice": active, "newPrice": candidate, "event": "breakeven", "timestamp": latest.timestamp, "rationale": "已确认达到+1R"})
            active, state = candidate, "breakeven"
    if len(facts) >= 3:
        if direction == "BUY":
            trail = min(facts[-2].low, facts[-3].low)
            if trail > active and favourable >= risk * 1.25:
                events.append({"oldPrice": active, "newPrice": trail, "event": "structure_trailing", "timestamp": latest.timestamp, "rationale": "新确认更高低点后结构跟踪"})
                active, state = trail, "structure_trailing"
        elif direction == "SELL":
            trail = max(facts[-2].high, facts[-3].high)
            if trail < active and favourable >= risk * 1.25:
                events.append({"oldPrice": active, "newPrice": trail, "event": "structure_trailing", "timestamp": latest.timestamp, "rationale": "新确认更低高点后结构跟踪"})
                active, state = trail, "structure_trailing"
    result.update({"activeStop": _round(active), "stopState": state, "stopEvents": events, "updatedAt": latest.timestamp})
    return result


def _dedupe_setup_records(setups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one record per structure/mode/direction without losing evidence."""

    rank = {"CONFIRMED": 6, "FAILED": 5, "PENDING": 4, "NEEDS_REVIEW": 3, "WATCH": 2, "EXPIRED": 1}
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for setup in setups:
        key = (str(setup.get("type") or setup.get("kind")), str(setup.get("direction") or "NONE"), str(setup.get("marketMode") or ""))
        previous = selected.get(key)
        if previous is None or rank.get(str(setup.get("status")), 0) > rank.get(str(previous.get("status")), 0):
            selected[key] = setup
    return sorted(selected.values(), key=lambda item: (-rank.get(str(item.get("status")), 0), str(item.get("type") or "")))


def _market_mode_state(environment: EnvironmentState, setups: Iterable[dict[str, Any]]) -> dict[str, Any]:
    confirmed_reversal = next((item for item in setups if item.get("marketMode") == "REBOUND" and item.get("status") == "CONFIRMED"), None)
    if confirmed_reversal:
        mode = "REBOUND"
        reason = f"{confirmed_reversal.get('type')} 的反转链已确认。"
    elif environment.state == "RANGE":
        mode = "RANGE"
        reason = "区间边界和高重叠证据仍有效。"
    elif environment.state in {"BULL_TREND", "BEAR_TREND"}:
        mode = "TREND"
        reason = "主趋势环境仍有效；反向候选未完成确认链。"
    else:
        mode = "REBOUND"
        reason = "当前处于过渡/反弹观察，等待确认链决定新方向。"
    return {"mode": mode, "label": MODE_LABELS[mode], "reason": reason, "environmentState": environment.state, "confirmedSetupId": confirmed_reversal.get("setupId") if confirmed_reversal else None}


def _promotable_setup(setup: dict[str, Any]) -> bool:
    return (
        str(setup.get("status")) == "CONFIRMED"
        and str(setup.get("direction")) in {"BUY", "SELL"}
        and not bool(setup.get("reviewRequired"))
        and _safe_float(setup.get("triggerPrice")) > 0
        and _safe_float(setup.get("invalidationPrice")) > 0
    )


def build_contract(result: dict[str, Any], raw_bars: list[dict[str, Any]], timeframe: str = "1d", profile: dict[str, Any] | None = None, account: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add the structured contract while retaining the existing display contract."""

    cfg = get_price_action_profile(profile, timeframe)
    facts = build_bar_facts(raw_bars, timeframe=timeframe, profile=cfg)
    complete_facts = [fact for fact in facts if fact.completed]
    analysis_facts = complete_facts
    volume = build_volume_turnover_context(analysis_facts, profile=cfg, timeframe=timeframe)
    environment = _environment_from_facts(analysis_facts, volume, cfg)
    pa = result.setdefault("priceAction", {})
    legacy_environment = dict(pa.get("environment") or {})
    environment_dict = environment.to_dict()
    environment_dict.update({key: value for key, value in legacy_environment.items() if key not in environment_dict})
    pa["environment"] = environment_dict
    pa["profile"] = {"profileId": cfg["profileId"], "profileVersion": cfg["profileVersion"], "timeframe": timeframe, "thresholds": cfg["thresholds"], "risk": cfg["risk"], "rules": [rule.to_dict() for rule in RULE_CATALOG]}
    pa["profileId"] = cfg["profileId"]
    pa["profileVersion"] = cfg["profileVersion"]
    pa["sourcePolicy"] = SOURCE_POLICY
    pa["barFacts"] = [fact.to_dict() for fact in facts]
    pa["volume"] = {**(pa.get("volume") or {}), **volume}
    if result.get("rawKlines") and facts:
        fact_by_timestamp = {fact.timestamp: fact.to_dict() for fact in facts}
        enriched_raw = []
        for item in result["rawKlines"]:
            merged = dict(item)
            fact = fact_by_timestamp.get(str(item.get("date") or item.get("datetime") or ""))
            if fact:
                for key in ("barId", "session", "completed", "qualityFlags", "body", "range", "upperWick", "lowerWick", "bodyRatio", "closePosition", "direction", "gap", "overlapRatio", "patternLabels", "floatShares", "providerTurnover", "computedTurnover", "turnoverRate"):
                    if key in fact:
                        merged[key] = fact[key]
            enriched_raw.append(merged)
        result["rawKlines"] = enriched_raw
    pa["volumeTurnover"] = volume
    has_incomplete_latest = bool(facts and not facts[-1].completed)
    pa["uncertainty"] = {"dataQuality": volume.get("qualityFlags") or [], "unknowns": ["turnoverRate"] if volume.get("turnoverQuality") == "UNKNOWN" else [], "reviewRequired": bool(environment.review_required or volume.get("state") == "UNKNOWN" or has_incomplete_latest), "incompleteLatestBar": has_incomplete_latest}
    pa["sessionQuality"] = validate_session_continuity(facts, timeframe)
    swings = _confirmed_swings(analysis_facts, int(cfg["swingRadius"]))
    legs = build_legs(analysis_facts, swings)
    structures = [Structure(_stable_id("structure", swing["kind"], swing["observedAt"], timeframe), swing["kind"], "SELL" if swing["kind"] == "SWING_HIGH" else "BUY", "CONFIRMED", swing["observedAt"], swing["confirmedAt"], evidence_ids=[_stable_id("evidence", swing["kind"], swing["observedAt"])], source_pages=_source_pages("candlestick"), state_transition=_state_transition("CONFIRMED", swing["observedAt"], swing["confirmedAt"])).to_dict() for swing in swings]
    if latest := (analysis_facts[-1] if analysis_facts else None):
        if environment.state in {"BULL_TREND", "BEAR_TREND"}:
            structures.append(Structure(_stable_id("structure", environment.state, timeframe), "MAIN_TREND", "BUY" if environment.state == "BULL_TREND" else "SELL", "CONFIRMED", facts[0].timestamp, environment.confirmation_time, evidence_ids=[_stable_id("evidence", "EMA", environment.state)], source_pages=_source_pages("trend"), state_transition=_state_transition("CONFIRMED", facts[0].timestamp, environment.confirmation_time)).to_dict())
        elif environment.state == "RANGE" and environment.range_zone:
            structures.append(Structure(_stable_id("structure", "TRADING_RANGE", timeframe), "TRADING_RANGE", "NONE", "CONFIRMED", facts[0].timestamp, environment.confirmation_time, evidence_ids=[_stable_id("evidence", "RANGE", timeframe)], source_pages=_source_pages("range"), state_transition=_state_transition("CONFIRMED", facts[0].timestamp, environment.confirmation_time)).to_dict())
    legacy_setups = [] if has_incomplete_latest else list(pa.get("setups") or [])
    pattern_families = detect_pattern_families(analysis_facts, environment, cfg, swings)
    enriched_setups = [enrich_setup(setup, analysis_facts, volume, timeframe, cfg) for setup in [*legacy_setups, *pattern_families] if analysis_facts]
    pa["setups"] = _dedupe_setup_records(enriched_setups)
    pa["structures"] = structures
    pa["swings"] = swings
    pa["legs"] = legs
    pa["twoLegCount"] = len([leg for leg in legs if leg.get("twoLegCount")])
    pa["patternFamilies"] = pattern_families
    pa["marketMode"] = _market_mode_state(environment, pa["setups"])
    latest = analysis_facts[-1] if analysis_facts else None
    for level in pa.get("levels") or []:
        level.setdefault("levelId", _stable_id("level", level.get("kind"), level.get("price"), timeframe))
        level.setdefault("ownerTimeframe", timeframe)
        level.setdefault("confirmedAt", latest.timestamp if latest else None)
        level.setdefault("structural", level.get("kind") not in {"EMA20"})
        level.setdefault("lifecycle", {"observedAt": latest.timestamp if latest else None, "confirmedAt": latest.timestamp if latest else None, "failedAt": None, "expiredAt": None})
    pa["magnetZones"] = _magnet_zones(pa.get("levels") or [], timeframe, latest, latest.atr if latest else 1.0)
    plans = []
    signals = []
    if latest:
        for setup in pa["setups"]:
            plan = build_trade_plan(setup, pa.get("levels") or [], latest, environment_dict, volume, timeframe=timeframe, profile=cfg, account=account) if _promotable_setup(setup) else None
            if plan:
                plans.append(plan)
            if setup.get("direction") in {"BUY", "SELL"}:
                signal_status = "CONFIRMED" if plan and plan.get("status") == "CONFIRMED" else str(setup.get("status") or "WATCH")
                signal_record = SignalRecord(_stable_id("signal", setup.get("setupId"), latest.timestamp), setup.get("setupId"), plan.get("planId") if plan else None, setup.get("direction", "NONE"), signal_status, setup.get("observedAt", latest.timestamp), setup.get("confirmedAt") if signal_status == "CONFIRMED" else None, None, None, setup.get("evidenceIds", []), bool(setup.get("reviewRequired") or signal_status != "CONFIRMED")).to_dict()
                signal_record.update({
                    "executionAction": plan.get("executionAction") if plan else "OBSERVE",
                    "executionConstraint": plan.get("executionConstraint") if plan else "NO_ORDER_UNCONFIRMED",
                    "positionRequired": plan.get("positionRequired", False) if plan else False,
                    "shortSellingAllowed": False,
                    "marketMode": setup.get("marketMode"),
                    "lifecycle": setup.get("lifecycle") or setup.get("status"),
                })
                signals.append(signal_record)
    pa["tradePlans"] = plans
    pa["signalRecords"] = signals
    actionable_signals = [signal for signal in signals if signal.get("status") == "CONFIRMED" and not signal.get("reviewRequired")]
    pa["discipline"] = {"state": "BLOCKED" if any(plan.get("status") == "BLOCKED" for plan in plans) and not actionable_signals else "ARMED" if actionable_signals else "WAIT", "hardGates": sorted({reason for plan in plans for reason in plan.get("blockedReasons", [])}), "events": [], "automatedOrder": False}
    pa["rollout"] = {"mode": str(os.environ.get("PRICE_ACTION_CONTRACT_MODE") or "shadow"), "legacyCompatibility": True, "automatedOrder": False, "rollback": "设置 PRICE_ACTION_CONTRACT_MODE=legacy 后仍保留旧字段读取路径。"}
    pa["sessionContext"] = build_session_context(analysis_facts, timeframe, cfg, environment=environment)
    pa["openingContext"] = pa["sessionContext"].get("openingContext", {})
    pa["sessionPatterns"] = list(pa["sessionContext"].get("sessionPatterns") or [])
    intraday_plans: list[dict[str, Any]] = []
    if latest and is_minute_timeframe(timeframe):
        for session_pattern in pa["sessionPatterns"]:
            session_setup = enrich_setup(session_pattern, analysis_facts, volume, timeframe, cfg)
            if not _promotable_setup(session_setup):
                continue
            plan = build_trade_plan(session_setup, pa.get("levels") or [], latest, environment_dict, volume, timeframe=timeframe, profile=cfg, account=account)
            plan.update({
                "sessionExpiry": session_pattern.get("sessionExpiry") or "SESSION_CLOSE",
                "intendedUse": "INTRADAY_ENTRY_OR_T_MANAGEMENT",
                "nextCondition": session_pattern.get("nextCondition"),
                "sourcePages": session_pattern.get("sourcePages") or plan.get("sourcePages"),
            })
            intraday_plans.append(plan)
    pa["intradayTradePlans"] = intraday_plans
    result["contractVersion"] = "price-action-contract-v1"
    return result


def build_session_context(
    facts: list[BarFacts],
    timeframe: str,
    profile: dict[str, Any] | None = None,
    *,
    environment: EnvironmentState | None = None,
) -> dict[str, Any]:
    """Build opening/session structures exclusively from complete minute bars."""

    cfg = get_price_action_profile(profile, timeframe)
    if not is_minute_timeframe(timeframe):
        return {
            "applicable": False,
            "session": "NOT_APPLICABLE",
            "complete": True,
            "reason": "NON_MINUTE_TIMEFRAME",
            "timeWindow": "NOT_APPLICABLE",
            "openingContext": {"status": "NOT_APPLICABLE"},
            "sessionPatterns": [],
        }
    if not facts:
        return {"applicable": True, "session": "unknown", "complete": False, "reason": "DATA_INSUFFICIENT", "timeWindow": "UNKNOWN", "openingContext": {"status": "DATA_INSUFFICIENT", "reason": "没有完整分钟K"}, "sessionPatterns": []}
    dates = [fact.timestamp[:10] for fact in facts if fact.timestamp]
    latest_date = dates[-1] if dates else ""
    today = [fact for fact in facts if fact.timestamp.startswith(latest_date)]
    regular = [fact for fact in today if fact.session == "regular"]
    opening_count = int(cfg["thresholds"]["openingRangeBars"])
    opening = regular[:opening_count]
    opening_high = max((fact.high for fact in opening), default=None)
    opening_low = min((fact.low for fact in opening), default=None)
    minutes = max(int(str(timeframe).rstrip("m")), 1)
    first_hour_count = max(opening_count, int(cfg["thresholds"]["firstHourMinutes"] / minutes))
    first_hour = regular[:first_hour_count]
    previous_dates = sorted({fact.timestamp[:10] for fact in facts if fact.timestamp and fact.timestamp[:10] < latest_date})
    previous_day = previous_dates[-1] if previous_dates else None
    previous_regular = [fact for fact in facts if previous_day and fact.timestamp.startswith(previous_day) and fact.session == "regular"]
    time_window = "REGULAR"
    parsed = None
    if regular:
        clock = regular[-1].timestamp.replace("T", " ").rsplit(" ", 1)[-1][:5]
        parsed = _parse_clock(clock)
        if parsed:
            if parsed < time(10, 30):
                time_window = "OPENING_FIRST_HOUR"
            elif time(11, 0) <= parsed <= time(11, 30):
                time_window = "PRE_LUNCH_1130"
            elif time(13, 0) <= parsed <= time(13, 30):
                time_window = "LUNCH_REOPEN"
            elif parsed >= time(14, 0):
                time_window = "CLOSE_30_60"
    patterns = _session_patterns(
        regular,
        opening,
        first_hour,
        previous_regular,
        environment,
        timeframe,
        parsed,
        cfg,
    )
    return {
        "applicable": True,
        "session": regular[-1].session if regular else (today[-1].session if today else "unknown"),
        "date": latest_date,
        "barCount": len(today),
        "regularBarCount": len(regular),
        "complete": bool(regular and len(opening) >= opening_count),
        "missingOpeningBars": max(0, opening_count - len(opening)),
        "timeWindow": time_window,
        "riskWindows": ["OPEN", "FIRST_HOUR", "PRE_LUNCH_1130", "LUNCH_REOPEN", "CLOSE_30_60"],
        "sessionPatterns": patterns,
        "openingContext": {
            "status": "CONFIRMED" if len(opening) >= opening_count else "DATA_INSUFFICIENT",
            "openingRangeHigh": _round(opening_high),
            "openingRangeLow": _round(opening_low),
            "firstBar": opening[0].timestamp if opening else None,
            "firstThreeBars": [fact.timestamp for fact in opening],
            "firstHourHigh": _round(max((fact.high for fact in first_hour), default=0) or None),
            "firstHourLow": _round(min((fact.low for fact in first_hour), default=0) or None),
            "priorSessionHigh": _round(max((fact.high for fact in previous_regular), default=0) or None),
            "priorSessionLow": _round(min((fact.low for fact in previous_regular), default=0) or None),
            "gap": _round(opening[0].gap if opening else 0),
            "dayType": next((item["type"] for item in patterns if item.get("status") == "CONFIRMED"), "OPENING_RANGE" if opening else "UNKNOWN"),
        },
    }


def _session_pattern(
    kind: str,
    status: str,
    *,
    direction: str = "NONE",
    evidence: list[str],
    missing: list[str] | None = None,
    trigger: float | None = None,
    invalidation: float | None = None,
    expiry: str = "SESSION_CLOSE",
    observed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "type": kind,
        "family": "INTRADAY_SESSION",
        "marketMode": "TREND" if kind in {"OPENING_TREND", "TREND_FROM_RANGE", "TREND_DAY", "TREND_RESUMPTION"} else "REBOUND",
        "marketModeLabel": MODE_LABELS["TREND"] if kind in {"OPENING_TREND", "TREND_FROM_RANGE", "TREND_DAY", "TREND_RESUMPTION"} else MODE_LABELS["REBOUND"],
        "direction": direction,
        "status": status,
        "lifecycle": status,
        "evidence": evidence,
        "missingConditions": list(missing or []),
        "nextCondition": (missing or ["按会话结束前的完整K线复核"])[0],
        "triggerPrice": _round(trigger),
        "invalidationPrice": _round(invalidation),
        "sessionExpiry": expiry,
        "observedAt": observed_at,
        "confirmedAt": observed_at if status == "CONFIRMED" else None,
        "sourcePages": _source_pages("trend" if "TREND" in kind else "reversal" if "REVERSAL" in kind else "candlestick"),
    }


def _session_patterns(
    regular: list[BarFacts],
    opening: list[BarFacts],
    first_hour: list[BarFacts],
    previous_regular: list[BarFacts],
    environment: EnvironmentState | None,
    timeframe: str,
    clock: time | None,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    if len(opening) < int(profile["thresholds"]["openingRangeBars"]):
        return [_session_pattern("OPENING_DATA_INSUFFICIENT", "DATA_INSUFFICIENT", evidence=["首根/前三根完整分钟K不足"], missing=["补足开盘区间后才判断开盘趋势或开盘反转"], observed_at=regular[-1].timestamp if regular else None)]
    latest = regular[-1]
    atr = max(latest.atr or latest.range, 1e-9)
    opening_high, opening_low = max(fact.high for fact in opening), min(fact.low for fact in opening)
    direction_count = sum(1 if fact.direction == "BULL" else -1 if fact.direction == "BEAR" else 0 for fact in opening)
    opening_direction = "BUY" if direction_count >= 2 else "SELL" if direction_count <= -2 else "NONE"
    patterns: list[dict[str, Any]] = []
    after_open = regular[len(opening):]
    strong_open = opening_direction != "NONE" and sum(fact.range for fact in opening) >= atr * 1.25
    if strong_open:
        hold_side = latest.close > opening_high if opening_direction == "BUY" else latest.close < opening_low
        first_pullback = any(fact.direction == ("BEAR" if opening_direction == "BUY" else "BULL") for fact in after_open)
        status = "CONFIRMED" if hold_side and first_pullback else "WATCH"
        patterns.append(_session_pattern("OPENING_TREND", status, direction=opening_direction, evidence=["开盘区间内至少两根同向K线", "开盘波幅相对ATR具有方向性"], missing=[] if status == "CONFIRMED" else ["等待开盘区间突破后的第一次回撤/回测及后续跟随"], trigger=opening_high if opening_direction == "BUY" else opening_low, invalidation=opening_low if opening_direction == "BUY" else opening_high, observed_at=latest.timestamp))
    prior_close = previous_regular[-1].close if previous_regular else None
    gap_direction = "BUY" if prior_close and opening[0].open > prior_close else "SELL" if prior_close and opening[0].open < prior_close else "NONE"
    reversal = gap_direction != "NONE" and opening_direction != "NONE" and gap_direction != opening_direction
    if reversal:
        reversal_confirmed = latest.close > opening_high if opening_direction == "BUY" else latest.close < opening_low
        patterns.append(_session_pattern("OPENING_REVERSAL", "CONFIRMED" if reversal_confirmed else "PENDING", direction=opening_direction, evidence=["开盘缺口方向与开盘推进方向相反", "开盘反转仍需突破开盘区间并有跟随"], missing=[] if reversal_confirmed else ["等待完整K线突破开盘区间并保持"], trigger=opening_high if opening_direction == "BUY" else opening_low, invalidation=opening_low if opening_direction == "BUY" else opening_high, observed_at=latest.timestamp))
    if after_open:
        up_break = latest.close > opening_high + atr * 0.05
        down_break = latest.close < opening_low - atr * 0.05
        if up_break or down_break:
            direction = "BUY" if up_break else "SELL"
            followed = latest.close > after_open[-2].close if up_break and len(after_open) > 1 else latest.close < after_open[-2].close if len(after_open) > 1 else False
            patterns.append(_session_pattern("TREND_FROM_RANGE", "CONFIRMED" if followed else "PENDING", direction=direction, evidence=["价格离开开盘区间", "趋势从区间启动需后续同向收盘"], missing=[] if followed else ["等待突破后至少一根同向完整K线或回测守住开盘边界"], trigger=opening_high if up_break else opening_low, invalidation=opening_low if up_break else opening_high, observed_at=latest.timestamp))
    if len(first_hour) >= max(3, int(profile["thresholds"]["firstHourMinutes"] / max(int(timeframe.rstrip("m")), 1))):
        first_high, first_low = max(fact.high for fact in first_hour), min(fact.low for fact in first_hour)
        close_near_extreme = latest.close >= first_high - atr * 0.25 or latest.close <= first_low + atr * 0.25
        if close_near_extreme and environment and environment.state in {"BULL_TREND", "BEAR_TREND"}:
            direction = "BUY" if environment.state == "BULL_TREND" else "SELL"
            patterns.append(_session_pattern("TREND_DAY", "WATCH", direction=direction, evidence=["第一小时保持方向并靠近其极值", "多周期环境仍为同向趋势"], missing=["等待回撤、回测或二次入场，不追第一小时末端"], trigger=first_high if direction == "BUY" else first_low, invalidation=first_low if direction == "BUY" else first_high, observed_at=latest.timestamp))
    if clock and clock >= time(13, 0) and after_open:
        morning = [fact for fact in regular if (_parse_clock(fact.timestamp.replace("T", " ").rsplit(" ", 1)[-1][:5]) or time(23, 59)) < time(11, 31)]
        morning_high = max((fact.high for fact in morning), default=None)
        morning_low = min((fact.low for fact in morning), default=None)
        direction = "BUY" if morning_high and latest.close > morning_high else "SELL" if morning_low and latest.close < morning_low else "NONE"
        if direction != "NONE" and morning_high is not None and morning_low is not None:
            patterns.append(_session_pattern("TREND_RESUMPTION", "PENDING", direction=direction, evidence=["午盘重开后重新测试上午区间外侧", "重开后需要成交量、流动性和高周期磁力位复核"], missing=["等待午后同向收盘跟随，不把午盘第一根K线当追价信号"], trigger=morning_high if direction == "BUY" else morning_low, invalidation=morning_low if direction == "BUY" else morning_high, observed_at=latest.timestamp))
    if clock and clock >= time(14, 30):
        patterns.append(_session_pattern("CLOSE_WINDOW_REVIEW", "WATCH", evidence=["进入收盘前30分钟窗口", "需重查高周期磁力位、流动性和会话剩余时间"], missing=["不在会话末端把未确认的分钟候选升级成隔夜计划"], observed_at=latest.timestamp))
    return patterns


def validate_session_continuity(facts: list[BarFacts], timeframe: str) -> dict[str, Any]:
    """Validate completed bars and expected intraday spacing without inventing bars."""

    if not facts:
        return {"complete": False, "state": "DATA_INSUFFICIENT", "missingBars": 0, "gaps": [], "reason": "NO_BARS"}
    incomplete = [fact.timestamp for fact in facts if not fact.completed]
    gaps: list[dict[str, Any]] = []
    if is_minute_timeframe(timeframe):
        try:
            interval = int(timeframe[:-1]) * 60
            stamps = [datetime.fromisoformat(fact.timestamp.replace("Z", "+00:00")) for fact in facts if " " in fact.timestamp or "T" in fact.timestamp]
            for previous, current in zip(stamps, stamps[1:]):
                delta = (current - previous).total_seconds()
                if delta > interval * 2.5 and previous.date() == current.date():
                    gaps.append({"from": previous.isoformat(), "to": current.isoformat(), "seconds": delta, "expected": interval})
        except (TypeError, ValueError):
            gaps.append({"reason": "TIMESTAMP_PARSE_ERROR"})
    state = "DATA_INSUFFICIENT" if incomplete or gaps else "VALID"
    return {"complete": state == "VALID", "state": state, "missingBars": len(incomplete) + len(gaps), "incompleteBars": incomplete, "gaps": gaps, "regularSessions": sorted({fact.timestamp[:10] for fact in facts if fact.session == "regular"})}
