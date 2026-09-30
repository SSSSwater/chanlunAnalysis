from __future__ import annotations

from statistics import mean


def detect_golden_pillar_watch(
    history: list[dict],
    *,
    support_tolerance: float = 0.01,
) -> dict | None:
    """Detect a day-3/day-4 golden pillar watch setup using only available bars."""
    if len(history) < 28:
        return None

    bars = history
    matches: list[dict] = []
    latest_index = len(bars) - 1
    for day_number in (4, 3):
        pillar_index = latest_index - day_number + 1
        if pillar_index < 20:
            continue

        pillar = bars[pillar_index]
        previous_volumes = [_to_float(item.get("volume")) for item in bars[pillar_index - 20 : pillar_index]]
        avg_volume = mean([value for value in previous_volumes if value > 0] or [0])
        meta = _match_setup(
            bars=bars,
            pillar_index=pillar_index,
            day_number=day_number,
            avg_volume=avg_volume,
            support_tolerance=support_tolerance,
        )
        if meta:
            matches.append(meta)

    if not matches:
        return None

    return sorted(matches, key=lambda item: item["score"], reverse=True)[0]


def _match_setup(
    *,
    bars: list[dict],
    pillar_index: int,
    day_number: int,
    avg_volume: float,
    support_tolerance: float,
) -> dict | None:
    pillar = bars[pillar_index]
    pillar_open = _to_float(pillar.get("open"))
    pillar_close = _to_float(pillar.get("close"))
    pillar_high = _to_float(pillar.get("high"))
    pillar_volume = _to_float(pillar.get("volume"))
    if pillar_open <= 0 or pillar_close <= 0 or pillar_volume <= 0:
        return None
    if pillar_close <= pillar_open:
        return None

    volume_ratio = pillar_volume / avg_volume if avg_volume > 0 else 0
    if volume_ratio < 1.35 or volume_ratio > 3.8:
        return None

    support_price = pillar_close
    observe_bars = bars[pillar_index + 1 :]
    if len(observe_bars) != day_number - 1:
        return None

    reasons: list[str] = [
        f"第1天 {pillar.get('date')} 为放量阳线，量能约为前20日均量 {volume_ratio:.2f} 倍，未达到过度天量阈值",
    ]
    shrink_ratios: list[float] = []
    close_margins: list[float] = []
    low_margins: list[float] = []

    for offset, bar in enumerate(observe_bars, start=2):
        close = _to_float(bar.get("close"))
        open_price = _to_float(bar.get("open"))
        low = _to_float(bar.get("low"))
        volume = _to_float(bar.get("volume"))
        prev_close = _to_float(bars[pillar_index + offset - 2].get("close"))
        is_red_or_fake_red = close >= open_price or close >= prev_close
        if not is_red_or_fake_red:
            return None
        if close < support_price:
            return None
        if low < support_price * (1 - support_tolerance):
            return None
        if volume <= 0 or volume >= pillar_volume:
            return None

        shrink_ratios.append(volume / pillar_volume)
        close_margins.append((close - support_price) / support_price)
        low_margins.append((low - support_price) / support_price)
        reasons.append(
            f"第{offset}天 {bar.get('date')} 收盘 {close:.2f} 守在第一天收盘横线 {support_price:.2f} 上方，成交量缩至第1天的 {volume / pillar_volume:.2f}"
        )

    latest = bars[-1]
    latest_close = _to_float(latest.get("close"))
    latest_low = _to_float(latest.get("low"))
    min_low_margin = min(low_margins or [0])
    min_close_margin = min(close_margins or [0])
    avg_shrink_ratio = mean(shrink_ratios or [1])
    day_score = 18 if day_number == 4 else 10
    score = (
        day_score
        + min(volume_ratio, 3.0) * 8
        + max(0.0, 1 - avg_shrink_ratio) * 20
        + max(0.0, min_close_margin) * 120
        + max(0.0, min_low_margin + support_tolerance) * 80
    )
    status = "第4天机会观察" if day_number == 4 else "第3天预观察"
    reasons.append(
        f"当前处于{status}，若后续不跌破 {support_price:.2f} 横线，可作为待观察价位"
    )

    return {
        "dayNumber": day_number,
        "status": status,
        "pillarDate": pillar.get("date"),
        "latestDate": latest.get("date"),
        "pillarClose": round(pillar_close, 3),
        "pillarHigh": round(pillar_high, 3),
        "pillarVolume": round(pillar_volume, 2),
        "latestClose": round(latest_close, 3),
        "latestLow": round(latest_low, 3),
        "supportPrice": round(support_price, 3),
        "supportTolerancePct": round(support_tolerance * 100, 2),
        "volumeRatio": round(volume_ratio, 2),
        "avgShrinkRatio": round(avg_shrink_ratio, 2),
        "minCloseMarginPct": round(min_close_margin * 100, 2),
        "minLowMarginPct": round(min_low_margin * 100, 2),
        "score": round(score, 2),
        "reason": "；".join(reasons),
    }


def _to_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
