from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from .services.chanlun import analyze_chanlun
    from .services.intraday import PERIODS, analyze_intraday
    from .services.stock_data import (
        get_index_intraday_history,
        get_index_history,
        get_stock_intraday_history,
        get_stock_history,
        list_price_limited_non_st_stocks,
        list_stocks,
        search_stocks,
    )
except ImportError:
    from services.chanlun import analyze_chanlun
    from services.intraday import PERIODS, analyze_intraday
    from services.stock_data import (
        get_index_intraday_history,
        get_index_history,
        get_stock_intraday_history,
        get_stock_history,
        list_price_limited_non_st_stocks,
        list_stocks,
        search_stocks,
    )


app = Flask(__name__)
CORS(app)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/stocks/search")
def stock_search():
    keyword = request.args.get("keyword", "").strip()
    if not keyword:
        return jsonify({"items": []})

    try:
        return jsonify({"items": search_stocks(keyword)})
    except Exception as exc:
        return jsonify({"message": f"股票搜索失败：{exc}"}), 500


@app.get("/api/stocks")
def stock_list():
    try:
        return jsonify({"items": list_stocks()})
    except Exception as exc:
        return jsonify({"message": f"股票列表加载失败：{exc}"}), 500


@app.get("/api/stocks/find-good")
def find_good_stock():
    min_price = _float_arg("minPrice", 0.0)
    max_price = _float_arg("maxPrice", 30.0)
    max_attempts = max(1, min(_int_arg("maxAttempts", 50), 80))
    threshold = max(0.001, min(_float_arg("threshold", 0.05), 0.2))

    if min_price < 0 or max_price <= 0 or min_price > max_price:
        return jsonify({"message": "请输入有效的股价区间"}), 400

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=185)
    candidates = list_price_limited_non_st_stocks(min_price, max_price)
    if not candidates:
        return jsonify({"message": "未找到符合股价区间的非 ST 股票"}), 404

    random.shuffle(candidates)
    selected_candidates = candidates[:max_attempts]
    errors: list[str] = []

    executor = ThreadPoolExecutor(max_workers=min(8, len(selected_candidates)))
    futures = [
        executor.submit(
            _analyze_find_candidate,
            candidate,
            start_date.strftime("%Y%m%d"),
            end_date.strftime("%Y%m%d"),
            threshold,
        )
        for candidate in selected_candidates
    ]
    try:
        for attempts, future in enumerate(as_completed(futures), start=1):
            candidate, result, matched_signal, error = future.result()
            if error:
                errors.append(error)
                continue
            if result and matched_signal:
                result["symbol"] = candidate["symbol"]
                result["name"] = candidate["name"]
                result["dateRange"] = {
                    "start": result["rawKlines"][0]["date"],
                    "end": result["rawKlines"][-1]["date"],
                }
                result["findMeta"] = {
                    "attempts": attempts,
                    "candidateCount": len(candidates),
                    "minPrice": min_price,
                    "maxPrice": max_price,
                    "threshold": threshold,
                    "latestPrice": candidate.get("latestPrice"),
                    "matchedSignal": matched_signal,
                }
                executor.shutdown(wait=False, cancel_futures=True)
                return jsonify(result)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return jsonify(
        {
            "message": f"已随机分析 {len(selected_candidates)} 只股票，未找到最新价接近未来买点的标的",
            "attempts": len(selected_candidates),
            "candidateCount": len(candidates),
            "errors": errors[-5:],
        }
    ), 404


@app.get("/api/analyze")
def analyze():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"message": "缺少股票代码"}), 400

    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=370)

    try:
        history = get_stock_history(
            symbol=symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if not history:
            return jsonify({"message": "未获取到近一年日 K 数据"}), 404

        result = analyze_chanlun(history)
        result["symbol"] = symbol
        result["dateRange"] = {
            "start": history[0]["date"],
            "end": history[-1]["date"],
        }
        result["intraday"] = {"periods": {}, "summary": {"periodCount": 0, "signalCount": 0}, "errors": {}}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"message": f"分析失败：{exc}"}), 500


@app.get("/api/index/analyze")
def analyze_index():
    symbol = request.args.get("symbol", "000001").strip() or "000001"
    name = request.args.get("name", "上证指数").strip() or "上证指数"
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=370)

    try:
        history = get_index_history(
            symbol=symbol,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if not history:
            return jsonify({"message": "未获取到近一年指数日 K 数据"}), 404

        result = analyze_chanlun(history)
        result["symbol"] = symbol
        result["name"] = name
        result["dateRange"] = {
            "start": history[0]["date"],
            "end": history[-1]["date"],
        }
        result["intraday"] = {"periods": {}, "summary": {"periodCount": 0, "signalCount": 0}, "errors": {}}
        return jsonify(result)
    except Exception as exc:
        return jsonify({"message": f"指数分析失败：{exc}"}), 500


@app.get("/api/intraday/analyze")
def analyze_intraday_stock():
    symbol = request.args.get("symbol", "").strip()
    period = request.args.get("period", "").strip()
    if not symbol:
        return jsonify({"message": "??????"}), 400
    try:
        return jsonify(_build_intraday_analysis(symbol, is_index=False, periods=[period] if period else None))
    except Exception as exc:
        return jsonify({"message": f"???????{exc}"}), 500


@app.get("/api/index/intraday/analyze")
def analyze_intraday_index():
    symbol = request.args.get("symbol", "000001").strip() or "000001"
    period = request.args.get("period", "").strip()
    try:
        return jsonify(_build_intraday_analysis(symbol, is_index=True, periods=[period] if period else None))
    except Exception as exc:
        return jsonify({"message": f"?????????{exc}"}), 500


def _match_future_buy(result: dict, threshold: float, current_price: float | None = None) -> dict | None:
    try:
        latest_price = float(current_price or 0)
    except (TypeError, ValueError):
        latest_price = 0
    if latest_price <= 0:
        latest_price = float(result.get("summary", {}).get("latestClose") or 0)
    if latest_price <= 0:
        return None

    buy_signals = [
        signal
        for signal in result.get("futureSignals", [])
        if signal.get("direction") == "buy" and float(signal.get("price") or 0) > 0
    ]
    sell_signals = [
        signal
        for signal in result.get("futureSignals", [])
        if signal.get("direction") == "sell" and float(signal.get("price") or 0) > 0
    ]
    if not buy_signals:
        return None

    matches = []
    for signal in buy_signals:
        anchor_price = float(signal["price"])
        distance = abs(latest_price - anchor_price) / anchor_price
        if distance <= threshold:
            nearest_above_sell = _nearest_above_sell(latest_price, anchor_price, sell_signals)
            if nearest_above_sell and nearest_above_sell["distance"] <= threshold:
                continue

            item = dict(signal)
            item["currentPrice"] = round(latest_price, 3)
            item["distancePct"] = round(distance * 100, 2)
            if nearest_above_sell:
                item["nearestSellPrice"] = nearest_above_sell["price"]
                item["nearestSellDistancePct"] = round(nearest_above_sell["distance"] * 100, 2)
            matches.append(item)

    if not matches:
        return None
    return sorted(matches, key=lambda item: item["distancePct"])[0]


def _build_intraday_analysis(symbol: str, is_index: bool, periods: list[str] | None = None) -> dict:
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=12)
    period_bars: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    target_periods = [period for period in (periods or list(PERIODS)) if period in PERIODS]

    for period in target_periods:
        try:
            fetcher = get_index_intraday_history if is_index else get_stock_intraday_history
            bars = fetcher(
                symbol=symbol,
                period=period,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            if bars:
                period_bars[period] = bars[-180:]
        except Exception as exc:
            errors[period] = str(exc)

    result = analyze_intraday(period_bars)
    result["errors"] = errors
    return result


def _nearest_above_sell(latest_price: float, buy_price: float, sell_signals: list[dict]) -> dict | None:
    above_sells = []
    for signal in sell_signals:
        sell_price = float(signal.get("price") or 0)
        if sell_price <= buy_price:
            continue
        above_sells.append(
            {
                "price": sell_price,
                "distance": abs(latest_price - sell_price) / sell_price,
            }
        )
    if not above_sells:
        return None
    return sorted(above_sells, key=lambda item: item["distance"])[0]


def _analyze_find_candidate(
    candidate: dict,
    start_date: str,
    end_date: str,
    threshold: float,
) -> tuple[dict, dict | None, dict | None, str | None]:
    symbol = candidate["symbol"]
    try:
        history = get_stock_history(symbol=symbol, start_date=start_date, end_date=end_date)
        if not history:
            return candidate, None, None, None
        result = analyze_chanlun(history)
        matched_signal = _match_future_buy(result, threshold, candidate.get("latestPrice"))
        return candidate, result, matched_signal, None
    except Exception as exc:
        return candidate, None, None, f"{symbol}: {exc}"


def _float_arg(name: str, default: float) -> float:
    try:
        return float(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
