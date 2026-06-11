from __future__ import annotations


PERIODS = ("30", "15", "5")


def analyze_intraday(period_bars: dict[str, list[dict]]) -> dict:
    periods = {}
    for period, bars in period_bars.items():
        periods[period] = analyze_intraday_period(period, bars)

    valid = [item for item in periods.values() if item.get("rawKlines")]
    latest_signal = _select_latest_signal(valid)
    bias_score = sum(_period_bias(item) for item in valid)
    bias = "震荡"
    if bias_score >= 2:
        bias = "偏多"
    elif bias_score <= -2:
        bias = "偏空"

    return {
        "periods": periods,
        "summary": {
            "bias": bias,
            "periodCount": len(valid),
            "latestSignal": latest_signal,
            "signalCount": sum(len(item.get("signals", [])) for item in valid),
        },
    }


def analyze_intraday_period(period: str, bars: list[dict]) -> dict:
    enriched = _with_indicators(bars)
    signals = _detect_intraday_signals(period, enriched)
    return {
        "period": period,
        "rawKlines": enriched,
        "signals": signals,
        "signalRows": signals,
        "summary": {
            "latestClose": enriched[-1]["close"] if enriched else None,
            "latestRsi": enriched[-1].get("rsi14") if enriched else None,
            "latestMacd": enriched[-1].get("macdHist") if enriched else None,
            "signalCount": len(signals),
            "latestSignal": signals[-1] if signals else None,
        },
        "dateRange": {
            "start": enriched[0]["date"] if enriched else "",
            "end": enriched[-1]["date"] if enriched else "",
        },
    }


def _with_indicators(bars: list[dict]) -> list[dict]:
    if not bars:
        return []

    closes = [float(item["close"]) for item in bars]
    highs = [float(item["high"]) for item in bars]
    lows = [float(item["low"]) for item in bars]
    rsi = _rsi(closes, 14)
    ema5 = _ema(closes, 5)
    ema20 = _ema(closes, 20)
    dif, dea, hist = _macd(closes)

    result = []
    for index, item in enumerate(bars):
        prev_close = closes[index - 1] if index > 0 else float(item.get("open") or closes[index])
        pct_change = (closes[index] - prev_close) / prev_close * 100 if prev_close else 0
        result.append(
            {
                **item,
                "index": index,
                "high": highs[index],
                "low": lows[index],
                "close": closes[index],
                "pctChange": round(float(item.get("pctChange", pct_change) or pct_change), 2),
                "rsi14": _round_or_none(rsi[index]),
                "ema5": _round_or_none(ema5[index]),
                "ema20": _round_or_none(ema20[index]),
                "macdDif": _round_or_none(dif[index]),
                "macdDea": _round_or_none(dea[index]),
                "macdHist": _round_or_none(hist[index]),
            }
        )
    return result


def _detect_intraday_signals(period: str, bars: list[dict]) -> list[dict]:
    if len(bars) < 25:
        return []

    signals = []
    recent = range(max(1, len(bars) - 24), len(bars))
    for index in recent:
        prev = bars[index - 1]
        current = bars[index]
        rsi = current.get("rsi14")
        prev_rsi = prev.get("rsi14")
        hist = current.get("macdHist")
        prev_hist = prev.get("macdHist")
        ema5 = current.get("ema5")
        ema20 = current.get("ema20")
        prev_ema5 = prev.get("ema5")
        prev_ema20 = prev.get("ema20")

        if any(value is None for value in [rsi, prev_rsi, hist, prev_hist, ema5, ema20, prev_ema5, prev_ema20]):
            continue

        if rsi <= 35 and rsi > prev_rsi and hist > prev_hist:
            signals.append(
                _intraday_signal(
                    "intraday_rebound_buy",
                    "buy",
                    period,
                    current,
                    "RSI 低位回升，MACD 绿柱收敛，短线有反弹条件",
                    0.66,
                )
            )
        if prev_ema5 <= prev_ema20 and ema5 > ema20 and rsi >= 48 and hist > 0:
            signals.append(
                _intraday_signal(
                    "intraday_trend_buy",
                    "buy",
                    period,
                    current,
                    "EMA5 上穿 EMA20，RSI 回到强弱线之上，短线转强",
                    0.7,
                )
            )
        if rsi >= 68 and rsi < prev_rsi and hist < prev_hist:
            signals.append(
                _intraday_signal(
                    "intraday_pullback_sell",
                    "sell",
                    period,
                    current,
                    "RSI 高位回落，MACD 红柱收敛，短线有回落风险",
                    0.66,
                )
            )
        if prev_ema5 >= prev_ema20 and ema5 < ema20 and rsi <= 52 and hist < 0:
            signals.append(
                _intraday_signal(
                    "intraday_trend_sell",
                    "sell",
                    period,
                    current,
                    "EMA5 下穿 EMA20，RSI 跌回强弱线下方，短线转弱",
                    0.7,
                )
            )

    return _dedupe_intraday_signals(signals)


def _intraday_signal(signal_type: str, direction: str, period: str, bar: dict, reason: str, confidence: float) -> dict:
    return {
        "type": signal_type,
        "direction": direction,
        "date": bar["date"],
        "index": bar["index"],
        "price": round(float(bar["close"]), 3),
        "confidence": confidence,
        "reason": f"{period} 分钟：{reason}；RSI {bar.get('rsi14')}，MACD柱 {bar.get('macdHist')}",
        "future": False,
        "period": period,
        "rsi14": bar.get("rsi14"),
        "macdHist": bar.get("macdHist"),
        "backtestSegmentReturn": 0,
        "backtestTotalReturn": 0,
    }


def _dedupe_intraday_signals(signals: list[dict]) -> list[dict]:
    unique = {}
    for signal in signals:
        key = (signal["type"], signal["index"])
        if key not in unique or signal["confidence"] > unique[key]["confidence"]:
            unique[key] = signal
    return sorted(unique.values(), key=lambda item: item["index"])


def _select_latest_signal(items: list[dict]) -> dict | None:
    signals = []
    for item in items:
        signals.extend(item.get("signals", []))
    if not signals:
        return None
    return sorted(signals, key=lambda item: item["date"])[-1]


def _period_bias(item: dict) -> int:
    latest = item.get("summary", {}).get("latestSignal")
    if not latest:
        return 0
    return 1 if latest.get("direction") == "buy" else -1


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = []
    current = values[0]
    for value in values:
        current = value if not result else current * (1 - alpha) + value * alpha
        result.append(current)
    return result


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = []
    losses = []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
        if index < period:
            continue
        window_gains = gains[index - period : index]
        window_losses = losses[index - period : index]
        avg_gain = sum(window_gains) / period
        avg_loss = sum(window_losses) / period
        if avg_loss == 0:
            result[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[index] = 100 - 100 / (1 + rs)
    return result


def _macd(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)
    dif = [fast - slow for fast, slow in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, hist


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)
