from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Literal


Direction = Literal["up", "down"]
FractalType = Literal["top", "bottom"]
SignalType = Literal[
    "first_buy",
    "first_sell",
    "second_buy",
    "second_sell",
    "third_buy",
    "third_sell",
]


@dataclass
class Bar:
    index: int
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MergedBar:
    index: int
    start_index: int
    end_index: int
    start_date: str
    end_date: str
    high: float
    low: float
    open: float
    close: float
    volume: float = 0.0
    raw_indexes: list[int] = field(default_factory=list)


@dataclass
class Fractal:
    index: int
    raw_index: int
    date: str
    confirm_index: int
    confirm_date: str
    type: FractalType
    price: float


@dataclass
class Stroke:
    start_index: int
    end_index: int
    start_date: str
    end_date: str
    confirm_index: int
    confirm_date: str
    start_price: float
    end_price: float
    direction: Direction
    high: float
    low: float
    strength: float
    macd_area: float
    macd_peak: float


@dataclass
class Center:
    id: int
    start_index: int
    end_index: int
    start_date: str
    end_date: str
    high: float
    low: float
    zg: float
    zd: float
    stroke_start: int
    stroke_end: int
    status: str


def analyze_chanlun(raw_bars: list[dict]) -> dict:
    bars = [_to_bar(item, index) for index, item in enumerate(raw_bars)]
    macd = calculate_macd(bars)
    merged = merge_inclusion(bars)
    fractals = detect_fractals(merged)
    strokes = build_strokes(fractals, merged, macd)
    centers = detect_centers(strokes, bars)
    signals = detect_trade_points(strokes, bars)
    signals = apply_signal_backtest(signals, bars)
    signal_rows = build_signal_rows(signals, bars)
    future_signals = detect_future_trade_points(strokes, centers, bars)

    return {
        "rawKlines": raw_bars,
        "mergedKlines": [_serialize_merged(item) for item in merged],
        "fractals": [_serialize_fractal(item) for item in fractals],
        "strokes": [_serialize_stroke(item) for item in strokes],
        "centers": [_serialize_center(item) for item in centers],
        "signals": signals,
        "signalRows": signal_rows,
        "futureSignals": future_signals,
        "summary": build_summary(raw_bars, merged, fractals, strokes, signals, centers, future_signals),
    }


def _to_bar(item: dict, index: int) -> Bar:
    return Bar(
        index=index,
        date=item["date"],
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=float(item.get("volume", 0)),
    )


def calculate_macd(bars: list[Bar], fast: int = 12, slow: int = 26, signal: int = 9) -> list[dict]:
    ema_fast: float | None = None
    ema_slow: float | None = None
    dea = 0.0
    macd_items: list[dict] = []
    fast_alpha = 2 / (fast + 1)
    slow_alpha = 2 / (slow + 1)
    signal_alpha = 2 / (signal + 1)

    for bar in bars:
        close = bar.close
        ema_fast = close if ema_fast is None else ema_fast * (1 - fast_alpha) + close * fast_alpha
        ema_slow = close if ema_slow is None else ema_slow * (1 - slow_alpha) + close * slow_alpha
        dif = ema_fast - ema_slow
        dea = dea * (1 - signal_alpha) + dif * signal_alpha
        hist = (dif - dea) * 2
        macd_items.append({"dif": dif, "dea": dea, "hist": hist})

    return macd_items


def _is_inclusion(a: MergedBar, b: Bar | MergedBar) -> bool:
    return (a.high >= b.high and a.low <= b.low) or (a.high <= b.high and a.low >= b.low)


def _trend_of_last(items: list[MergedBar]) -> Direction:
    if len(items) < 2:
        return "up"
    prev, cur = items[-2], items[-1]
    if cur.high > prev.high and cur.low > prev.low:
        return "up"
    if cur.high < prev.high and cur.low < prev.low:
        return "down"
    return "up" if cur.close >= prev.close else "down"


def merge_inclusion(bars: list[Bar]) -> list[MergedBar]:
    merged: list[MergedBar] = []
    for bar in bars:
        current = MergedBar(
            index=len(merged),
            start_index=bar.index,
            end_index=bar.index,
            start_date=bar.date,
            end_date=bar.date,
            high=bar.high,
            low=bar.low,
            open=bar.open,
            close=bar.close,
            volume=bar.volume,
            raw_indexes=[bar.index],
        )

        if not merged:
            merged.append(current)
            continue

        last = merged[-1]
        if _is_inclusion(last, current):
            direction = _trend_of_last(merged)
            if direction == "up":
                last.high = max(last.high, current.high)
                last.low = max(last.low, current.low)
            else:
                last.high = min(last.high, current.high)
                last.low = min(last.low, current.low)
            last.end_index = current.end_index
            last.end_date = current.end_date
            last.close = current.close
            last.volume += current.volume
            last.raw_indexes.extend(current.raw_indexes)
        else:
            current.index = len(merged)
            merged.append(current)

    for index, item in enumerate(merged):
        item.index = index
    return merged


def detect_fractals(merged: list[MergedBar]) -> list[Fractal]:
    fractals: list[Fractal] = []
    for i in range(1, len(merged) - 1):
        prev_bar, bar, next_bar = merged[i - 1], merged[i], merged[i + 1]
        if bar.high > prev_bar.high and bar.high > next_bar.high and bar.low > prev_bar.low and bar.low > next_bar.low:
            fractals.append(
                Fractal(
                    index=i,
                    raw_index=bar.end_index,
                    date=bar.end_date,
                    confirm_index=next_bar.end_index,
                    confirm_date=next_bar.end_date,
                    type="top",
                    price=bar.high,
                )
            )
        elif bar.low < prev_bar.low and bar.low < next_bar.low and bar.high < prev_bar.high and bar.high < next_bar.high:
            fractals.append(
                Fractal(
                    index=i,
                    raw_index=bar.end_index,
                    date=bar.end_date,
                    confirm_index=next_bar.end_index,
                    confirm_date=next_bar.end_date,
                    type="bottom",
                    price=bar.low,
                )
            )

    return _filter_fractals(fractals)


def _filter_fractals(fractals: list[Fractal], min_distance: int = 4) -> list[Fractal]:
    selected: list[Fractal] = []
    for fractal in fractals:
        if not selected:
            selected.append(fractal)
            continue

        last = selected[-1]
        if fractal.type == last.type:
            better_top = fractal.type == "top" and fractal.price >= last.price
            better_bottom = fractal.type == "bottom" and fractal.price <= last.price
            if better_top or better_bottom:
                selected[-1] = fractal
            continue

        if fractal.index - last.index < min_distance:
            stronger = (
                fractal.type == "top"
                and last.type == "bottom"
                and fractal.price - last.price > abs(last.price) * 0.015
            ) or (
                fractal.type == "bottom"
                and last.type == "top"
                and last.price - fractal.price > abs(last.price) * 0.015
            )
            if stronger:
                selected.append(fractal)
            continue

        selected.append(fractal)

    return selected


def build_strokes(fractals: list[Fractal], merged: list[MergedBar], macd: list[dict]) -> list[Stroke]:
    strokes: list[Stroke] = []
    for start, end in zip(fractals, fractals[1:]):
        if start.type == end.type:
            continue
        direction: Direction = "up" if start.type == "bottom" and end.type == "top" else "down"
        span = merged[start.index : end.index + 1]
        high = max(item.high for item in span)
        low = min(item.low for item in span)
        strength = abs(end.price - start.price) / max(abs(start.price), 0.01)
        macd_area, macd_peak = _stroke_macd_strength(start.raw_index, end.raw_index, direction, macd)
        strokes.append(
            Stroke(
                start_index=start.raw_index,
                end_index=end.raw_index,
                start_date=start.date,
                end_date=end.date,
                confirm_index=end.confirm_index,
                confirm_date=end.confirm_date,
                start_price=start.price,
                end_price=end.price,
                direction=direction,
                high=high,
                low=low,
                strength=round(strength, 4),
                macd_area=macd_area,
                macd_peak=macd_peak,
            )
        )

    return strokes


def _stroke_macd_strength(start_index: int, end_index: int, direction: Direction, macd: list[dict]) -> tuple[float, float]:
    if not macd:
        return 0.0, 0.0
    left = max(0, min(start_index, end_index))
    right = min(len(macd) - 1, max(start_index, end_index))
    values = [item["hist"] for item in macd[left : right + 1]]
    if direction == "up":
        relevant = [value for value in values if value > 0]
    else:
        relevant = [abs(value) for value in values if value < 0]
    if not relevant:
        relevant = [abs(value) for value in values]
    area = round(sum(relevant), 4)
    peak = round(max(relevant) if relevant else 0.0, 4)
    return area, peak


def detect_centers(strokes: list[Stroke], bars: list[Bar]) -> list[Center]:
    centers: list[Center] = []
    i = 0
    while i <= len(strokes) - 3:
        window = strokes[i : i + 3]
        low = max(stroke.low for stroke in window)
        high = min(stroke.high for stroke in window)
        if low >= high:
            i += 1
            continue

        start_stroke = i
        end_stroke = i + 2
        for j in range(i + 3, len(strokes)):
            next_low = max(low, strokes[j].low)
            next_high = min(high, strokes[j].high)
            if next_low < next_high:
                low = next_low
                high = next_high
                end_stroke = j
            else:
                break

        start_index = strokes[start_stroke].start_index
        end_index = strokes[end_stroke].confirm_index
        latest_close = bars[-1].close
        status = "active" if low <= latest_close <= high else "left_up" if latest_close > high else "left_down"
        centers.append(
            Center(
                id=len(centers) + 1,
                start_index=start_index,
                end_index=end_index,
                start_date=strokes[start_stroke].start_date,
                end_date=strokes[end_stroke].confirm_date,
                high=high,
                low=low,
                zg=high,
                zd=low,
                stroke_start=start_stroke,
                stroke_end=end_stroke,
                status=status,
            )
        )
        i = max(end_stroke, i + 1)

    return centers


def detect_trade_points(strokes: list[Stroke], bars: list[Bar]) -> list[dict]:
    signals: list[dict] = []
    if len(strokes) < 3:
        return signals

    recent_volume = _volume_lookup(bars)
    for i in range(2, len(strokes)):
        s1, s2, s3 = strokes[i - 2], strokes[i - 1], strokes[i]
        volume_ratio = _volume_ratio(recent_volume, s3.confirm_index)
        macd_diverged = _is_macd_diverged(s1, s3)

        if s1.direction == "down" and s2.direction == "up" and s3.direction == "down":
            price_not_weaker = s3.end_price >= s1.end_price * 0.985
            if price_not_weaker and macd_diverged:
                signals.append(
                    _signal(
                        "first_buy",
                        s3,
                        "下跌笔确认后未有效跌破前低，且 MACD 柱面积/峰值收缩，疑似一类买点",
                        confidence=0.78,
                        volume_ratio=volume_ratio,
                        bars=bars,
                    )
                )
            if s3.end_price > s1.end_price and s3.end_price > min(s1.end_price, s2.start_price):
                signals.append(
                    _signal(
                        "second_buy",
                        s3,
                        "上涨后的回调笔已确认，回调未破前低，疑似二类买点",
                        confidence=0.68,
                        volume_ratio=volume_ratio,
                        bars=bars,
                    )
                )

        if s1.direction == "up" and s2.direction == "down" and s3.direction == "up":
            price_not_stronger = s3.end_price <= s1.end_price * 1.015
            if price_not_stronger and macd_diverged:
                signals.append(
                    _signal(
                        "first_sell",
                        s3,
                        "上涨笔确认后未有效突破前高，且 MACD 柱面积/峰值收缩，疑似一类卖点",
                        confidence=0.78,
                        volume_ratio=volume_ratio,
                        bars=bars,
                    )
                )
            if s3.end_price < s1.end_price and s3.end_price < max(s1.end_price, s2.start_price):
                signals.append(
                    _signal(
                        "second_sell",
                        s3,
                        "下跌后的反抽笔已确认，反抽未过前高，疑似二类卖点",
                        confidence=0.68,
                        volume_ratio=volume_ratio,
                        bars=bars,
                    )
                )

    for i in range(4, len(strokes)):
        window = strokes[i - 4 : i]
        current = strokes[i]
        center_high = min(item.high for item in window)
        center_low = max(item.low for item in window)
        if center_low >= center_high:
            continue

        volume_ratio = _volume_ratio(recent_volume, current.confirm_index)
        left_stroke = strokes[i - 1]
        if left_stroke.direction == "up" and left_stroke.end_price > center_high and current.direction == "down" and current.end_price > center_high:
            signals.append(
                _signal(
                    "third_buy",
                    current,
                    "离开已确认中枢后回踩笔确认，低点不回中枢，疑似三类买点",
                    confidence=0.72,
                    volume_ratio=volume_ratio,
                    bars=bars,
                )
            )
        elif left_stroke.direction == "down" and left_stroke.end_price < center_low and current.direction == "up" and current.end_price < center_low:
            signals.append(
                _signal(
                    "third_sell",
                    current,
                    "离开已确认中枢后反抽笔确认，高点不回中枢，疑似三类卖点",
                    confidence=0.72,
                    volume_ratio=volume_ratio,
                    bars=bars,
                )
            )

    return _dedupe_signals(signals)


def _is_macd_diverged(previous: Stroke, current: Stroke) -> bool:
    if previous.direction != current.direction:
        return False
    if previous.macd_area <= 0 and previous.macd_peak <= 0:
        return current.strength < previous.strength * 0.85
    area_weaker = current.macd_area <= previous.macd_area * 0.82
    peak_weaker = current.macd_peak <= previous.macd_peak * 0.88 if previous.macd_peak > 0 else True
    price_strength_weaker = current.strength <= previous.strength * 0.95
    return (area_weaker or peak_weaker) and price_strength_weaker


def detect_future_trade_points(strokes: list[Stroke], centers: list[Center], bars: list[Bar]) -> list[dict]:
    if not centers or not bars:
        return []

    latest_bar = bars[-1]
    latest_close = latest_bar.close
    latest_index = latest_bar.index
    latest_date = latest_bar.date
    volume_ratio = _volume_ratio(_volume_lookup(bars), latest_index)
    center = centers[-1]
    width = max(center.high - center.low, latest_close * 0.01, 0.01)
    near_margin = width * 0.25
    signals: list[dict] = []

    if latest_close > center.high:
        signals.append(
            _future_signal(
                "future_third_buy",
                "buy",
                latest_index,
                latest_date,
                center.high,
                center.id,
                "基于已确认中枢：价格在中枢上沿之上，后续回踩不跌回上沿可作为潜在三买锚点",
                confidence=0.66,
                volume_ratio=volume_ratio,
            )
        )
        signals.append(
            _future_signal(
                "future_stop_loss",
                "sell",
                latest_index,
                latest_date,
                center.low,
                center.id,
                "基于已确认中枢：若回落跌破中枢下沿，向上离开结构转弱，可作为风险锚点",
                confidence=0.62,
                volume_ratio=volume_ratio,
            )
        )
    elif latest_close < center.low:
        signals.append(
            _future_signal(
                "future_third_sell",
                "sell",
                latest_index,
                latest_date,
                center.low,
                center.id,
                "基于已确认中枢：价格在中枢下沿之下，后续反抽不回下沿可作为潜在三卖锚点",
                confidence=0.66,
                volume_ratio=volume_ratio,
            )
        )
        signals.append(
            _future_signal(
                "future_reclaim_buy",
                "buy",
                latest_index,
                latest_date,
                center.high,
                center.id,
                "基于已确认中枢：若重新站回中枢上沿，说明下跌离开失败，可作为转强观察锚点",
                confidence=0.62,
                volume_ratio=volume_ratio,
            )
        )
    else:
        buy_confidence = 0.68 if latest_close <= center.low + near_margin else 0.6
        sell_confidence = 0.68 if latest_close >= center.high - near_margin else 0.6
        signals.append(
            _future_signal(
                "future_center_buy",
                "buy",
                latest_index,
                latest_date,
                center.low,
                center.id,
                "基于已确认中枢：价格仍在中枢内，中枢下沿可作为低吸观察锚点",
                confidence=buy_confidence,
                volume_ratio=volume_ratio,
            )
        )
        signals.append(
            _future_signal(
                "future_center_sell",
                "sell",
                latest_index,
                latest_date,
                center.high,
                center.id,
                "基于已确认中枢：价格仍在中枢内，中枢上沿可作为减仓观察锚点",
                confidence=sell_confidence,
                volume_ratio=volume_ratio,
            )
        )

    return signals


def apply_signal_backtest(signals: list[dict], bars: list[Bar]) -> list[dict]:
    if not signals or not bars:
        return signals

    enriched = [dict(signal) for signal in signals]
    by_index = {bar.index: bar for bar in bars}
    latest_close = bars[-1].close
    holding_signal: dict | None = None
    holding_price = 0.0

    for signal in enriched:
        signal["backtestReturn"] = None
        signal["backtestStatus"] = "wait"
        bar = by_index.get(signal["index"])
        trade_price = float(signal.get("price") or (bar.close if bar else 0))
        if trade_price <= 0:
            continue

        if signal["direction"] == "buy":
            if holding_signal is None:
                holding_signal = signal
                holding_price = trade_price
                signal["backtestStatus"] = "open"
            else:
                signal["backtestStatus"] = "ignored_holding"
        elif signal["direction"] == "sell":
            if holding_signal is not None and holding_price > 0:
                pct_return = (trade_price - holding_price) / holding_price * 100
                holding_signal["backtestReturn"] = round(pct_return, 2)
                holding_signal["backtestStatus"] = "closed"
                holding_signal["exitDate"] = signal["date"]
                holding_signal["exitPrice"] = round(trade_price, 3)
                signal["backtestReturn"] = round(pct_return, 2)
                signal["backtestStatus"] = "close"
                signal["entryDate"] = holding_signal["date"]
                signal["entryPrice"] = round(holding_price, 3)
                holding_signal = None
                holding_price = 0.0
            else:
                signal["backtestStatus"] = "ignored_empty"

    if holding_signal is not None and holding_price > 0:
        floating_return = (latest_close - holding_price) / holding_price * 100
        holding_signal["backtestReturn"] = round(floating_return, 2)
        holding_signal["backtestStatus"] = "floating"
        holding_signal["exitDate"] = bars[-1].date
        holding_signal["exitPrice"] = round(latest_close, 3)

    return enriched


def build_signal_rows(signals: list[dict], bars: list[Bar]) -> list[dict]:
    rows: dict[tuple[str, str, float], dict] = {}
    for signal in signals:
        key = (signal["date"], signal["direction"], round(float(signal["price"]), 3))
        if key not in rows:
            rows[key] = {
                "date": signal["date"],
                "index": signal["index"],
                "direction": signal["direction"],
                "price": signal["price"],
                "types": [],
                "type": signal["type"],
                "confidence": signal["confidence"],
                "volumeRatio": signal.get("volumeRatio"),
                "reasonParts": [],
                "reason": "",
                "future": False,
            }
        row = rows[key]
        row["types"].append(signal["type"])
        row["confidence"] = max(float(row["confidence"]), float(signal["confidence"]))
        if row.get("volumeRatio") is None:
            row["volumeRatio"] = signal.get("volumeRatio")
        if signal["reason"] not in row["reasonParts"]:
            row["reasonParts"].append(signal["reason"])

    ordered = sorted(rows.values(), key=lambda item: (item["index"], item["direction"]))
    for row in ordered:
        row["type"] = "+".join(row["types"])
        row["reason"] = "；".join(row["reasonParts"])
        row.pop("reasonParts", None)
        row["backtestSegmentReturn"] = 0.0
        row["backtestTotalReturn"] = 0.0
        row["backtestStatus"] = "wait"

    apply_row_backtest(ordered, bars)
    return ordered


def apply_row_backtest(rows: list[dict], bars: list[Bar]) -> None:
    if not rows or not bars:
        return

    holding_price = 0.0
    holding_row: dict | None = None
    compounded = 1.0

    for row in rows:
        price = float(row["price"])
        if row["direction"] == "buy":
            if holding_row is None:
                holding_row = row
                holding_price = price
                row["backtestStatus"] = "open"
            else:
                row["backtestStatus"] = "ignored_holding"
        elif row["direction"] == "sell":
            if holding_row is not None and holding_price > 0:
                segment = (price - holding_price) / holding_price
                compounded *= 1 + segment
                row["backtestSegmentReturn"] = round(segment * 100, 2)
                row["backtestTotalReturn"] = round((compounded - 1) * 100, 2)
                row["backtestStatus"] = "close"
                row["entryDate"] = holding_row["date"]
                row["entryPrice"] = round(holding_price, 3)
                holding_row["backtestStatus"] = "closed"
                holding_row = None
                holding_price = 0.0
            else:
                row["backtestStatus"] = "ignored_empty"
                row["backtestSegmentReturn"] = 0.0
                row["backtestTotalReturn"] = round((compounded - 1) * 100, 2)
        row["backtestTotalReturn"] = round((compounded - 1) * 100, 2)


def _signal(
    signal_type: SignalType,
    stroke: Stroke,
    reason: str,
    confidence: float,
    volume_ratio: float,
    bars: list[Bar],
) -> dict:
    direction = "buy" if signal_type.endswith("buy") else "sell"
    volume_text = _volume_reason(direction, volume_ratio)
    confidence = confidence + _volume_confidence_adjust(direction, volume_ratio)
    action_bar = bars[stroke.confirm_index] if 0 <= stroke.confirm_index < len(bars) else None
    action_price = action_bar.close if action_bar else stroke.end_price
    return {
        "type": signal_type,
        "direction": direction,
        "date": stroke.confirm_date,
        "index": stroke.confirm_index,
        "price": round(action_price, 3),
        "confidence": round(min(confidence, 0.92), 2),
        "reason": f"{reason}；该信号在确认日按当日收盘价作为可行动参考价；{volume_text}",
        "volumeRatio": volume_ratio,
        "macdArea": stroke.macd_area,
        "macdPeak": stroke.macd_peak,
    }


def _future_signal(
    signal_type: str,
    direction: str,
    index: int,
    date: str,
    price: float,
    center_id: int,
    reason: str,
    confidence: float,
    volume_ratio: float,
) -> dict:
    volume_text = _volume_reason(direction, volume_ratio)
    confidence = confidence + _volume_confidence_adjust(direction, volume_ratio) * 0.6
    return {
        "type": signal_type,
        "direction": direction,
        "date": date,
        "index": index,
        "price": round(price, 3),
        "confidence": round(min(confidence, 0.9), 2),
        "reason": f"{reason}；{volume_text}",
        "centerId": center_id,
        "future": True,
        "volumeRatio": volume_ratio,
    }


def _volume_reason(direction: str, volume_ratio: float) -> str:
    ratio_text = f"量比{volume_ratio:.2f}"
    if direction == "buy":
        if volume_ratio < 0.8:
            return f"成交量较近 25 日均量明显萎缩（{ratio_text}），下跌动能收敛，对买点有利"
        if volume_ratio <= 1.15:
            return f"成交量接近近 25 日均量（{ratio_text}），量能温和，买点需继续观察承接"
        return f"成交量明显放大（{ratio_text}），可能是恐慌释放或分歧加大，买点确认度打折"

    if volume_ratio > 1.25:
        return f"成交量较近 25 日均量明显放大（{ratio_text}），上涨/反抽分歧加大，对卖点有利"
    if volume_ratio >= 0.85:
        return f"成交量接近近 25 日均量（{ratio_text}），卖点量能确认中性"
    return f"成交量明显萎缩（{ratio_text}），反抽力度不足但杀跌量能也偏弱，卖点需观察后续放量"


def _volume_confidence_adjust(direction: str, volume_ratio: float) -> float:
    if direction == "buy":
        if volume_ratio < 0.8:
            return 0.04
        if volume_ratio > 1.35:
            return -0.03
        return 0.0
    if volume_ratio > 1.25:
        return 0.04
    if volume_ratio < 0.65:
        return -0.02
    return 0.0


def _volume_lookup(bars: list[Bar]) -> dict[int, list[float]]:
    lookup: dict[int, list[float]] = {}
    volumes = [bar.volume for bar in bars]
    for bar in bars:
        start = max(0, bar.index - 5)
        base_start = max(0, bar.index - 25)
        current = volumes[start : bar.index + 1]
        base = volumes[base_start : bar.index + 1]
        lookup[bar.index] = [mean(current) if current else 0, mean(base) if base else 0]
    return lookup


def _volume_ratio(lookup: dict[int, list[float]], index: int) -> float:
    current, base = lookup.get(index, [0, 0])
    if base <= 0:
        return 1.0
    return round(current / base, 2)


def _dedupe_signals(signals: list[dict]) -> list[dict]:
    unique: dict[tuple[str, int], dict] = {}
    for signal in signals:
        key = (signal["type"], signal["index"])
        if key not in unique or signal["confidence"] > unique[key]["confidence"]:
            unique[key] = signal
    return sorted(unique.values(), key=lambda item: item["index"])


def build_summary(
    raw_bars: list[dict],
    merged: list[MergedBar],
    fractals: list[Fractal],
    strokes: list[Stroke],
    signals: list[dict],
    centers: list[Center],
    future_signals: list[dict],
) -> dict:
    latest_close = float(raw_bars[-1]["close"])
    trend = "震荡"
    if len(strokes) >= 2:
        last = strokes[-1]
        prev = strokes[-2]
        if last.direction == "up" and last.end_price > prev.high:
            trend = "向上"
        elif last.direction == "down" and last.end_price < prev.low:
            trend = "向下"

    latest_signal = signals[-1] if signals else None
    return {
        "latestClose": latest_close,
        "trend": trend,
        "rawCount": len(raw_bars),
        "mergedCount": len(merged),
        "fractalCount": len(fractals),
        "strokeCount": len(strokes),
        "centerCount": len(centers),
        "signalCount": len(signals),
        "futureSignalCount": len(future_signals),
        "latestSignal": latest_signal,
        "latestCenter": _serialize_center(centers[-1]) if centers else None,
    }


def _serialize_merged(item: MergedBar) -> dict:
    return {
        "index": item.index,
        "startIndex": item.start_index,
        "endIndex": item.end_index,
        "startDate": item.start_date,
        "endDate": item.end_date,
        "open": round(item.open, 3),
        "high": round(item.high, 3),
        "low": round(item.low, 3),
        "close": round(item.close, 3),
        "volume": round(item.volume, 2),
    }


def _serialize_fractal(item: Fractal) -> dict:
    return {
        "index": item.raw_index,
        "mergedIndex": item.index,
        "date": item.date,
        "type": item.type,
        "price": round(item.price, 3),
    }


def _serialize_stroke(item: Stroke) -> dict:
    return {
        "startIndex": item.start_index,
        "endIndex": item.end_index,
        "startDate": item.start_date,
        "endDate": item.end_date,
        "startPrice": round(item.start_price, 3),
        "endPrice": round(item.end_price, 3),
        "direction": item.direction,
        "high": round(item.high, 3),
        "low": round(item.low, 3),
        "strength": item.strength,
    }


def _serialize_center(item: Center) -> dict:
    return {
        "id": item.id,
        "startIndex": item.start_index,
        "endIndex": item.end_index,
        "startDate": item.start_date,
        "endDate": item.end_date,
        "high": round(item.high, 3),
        "low": round(item.low, 3),
        "zg": round(item.zg, 3),
        "zd": round(item.zd, 3),
        "strokeStart": item.stroke_start,
        "strokeEnd": item.stroke_end,
        "status": item.status,
    }
