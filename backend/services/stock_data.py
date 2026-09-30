from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as datetime_time, timedelta
from threading import Lock

import pandas as pd

try:
    from . import database as db
    from .eastmoney_client import (
        CORE_F10_REPORTS,
        F10_REPORTS,
        extract_f10_rows,
        fetch_all_f10_reports,
        fetch_f10_report,
    )
    from .market_data_util import (
        MarketDataError,
        normalize_symbol,
        secid,
        secucode,
    )
    from . import market_data_util as market_data
except ImportError:
    import services.database as db
    from services.eastmoney_client import (
        CORE_F10_REPORTS,
        F10_REPORTS,
        extract_f10_rows,
        fetch_all_f10_reports,
        fetch_f10_report,
    )
    from services.market_data_util import (
        MarketDataError,
        normalize_symbol,
        secid,
        secucode,
    )
    import services.market_data_util as market_data


DAILY_PERIOD = "101"
DAILY_ADJUST = "1"
INDEX_ADJUST = "0"
INTRADAY_SNAPSHOT_MAX_AGE = timedelta(minutes=2)
MORNING_SESSION_START = datetime_time(9, 15)
MORNING_SESSION_END = datetime_time(11, 30)
AFTERNOON_SESSION_START = datetime_time(13, 0)
CLOSE_SNAPSHOT_TIME = datetime_time(15, 5)
GROWTH_BOARD_PREFIXES = ("300", "301", "302", "688", "689")

_F10_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_F10_RUNNING: set[str] = set()
_F10_LOCK = Lock()
_INDEX_QUOTE_LOCK = Lock()
_INDEX_QUOTES_LAST_REFRESH: datetime | None = None
INDEX_QUOTES_REFRESH_INTERVAL = timedelta(seconds=45)
SECURITY_INDEX_REFRESH_INTERVAL = timedelta(hours=18)
SECURITY_INDEX_SOURCE_METADATA_KEY = "security_index_source"
SECURITY_INDEX_SOURCE = "non-tushare-v3"
_SECURITY_INDEX_LOCK = Lock()
_SECURITY_INDEX_ITEMS: tuple[dict, ...] = ()
_SECURITY_INDEX_BY_SYMBOL: dict[str, dict] = {}
_SECURITY_INDEX_DATABASE_PATH: str | None = None
_SECURITY_INDEX_EXPIRES_AT: datetime | None = None
VALUATION_FIELDS = (
    "latestPrice",
    "pctChange",
    "turnoverRate",
    "peDynamic",
    "peTtm",
    "pb",
    "totalMarketCap",
    "circulatingMarketCap",
)


FALLBACK_STOCKS = [
    {"代码": "000001", "名称": "平安银行"},
    {"代码": "000002", "名称": "万科A"},
    {"代码": "000063", "名称": "中兴通讯"},
    {"代码": "000333", "名称": "美的集团"},
    {"代码": "000651", "名称": "格力电器"},
    {"代码": "000858", "名称": "五粮液"},
    {"代码": "002230", "名称": "科大讯飞"},
    {"代码": "002415", "名称": "海康威视"},
    {"代码": "300059", "名称": "东方财富"},
    {"代码": "300750", "名称": "宁德时代"},
    {"代码": "600000", "名称": "浦发银行"},
    {"代码": "600036", "名称": "招商银行"},
    {"代码": "600519", "名称": "贵州茅台"},
    {"代码": "600887", "名称": "伊利股份"},
    {"代码": "601318", "名称": "中国平安"},
    {"代码": "601398", "名称": "工商银行"},
    {"代码": "601857", "名称": "中国石油"},
    {"代码": "601988", "名称": "中国银行"},
]


def init_storage() -> None:
    db.init_db()


def search_stocks(keyword: str, limit: int = 20) -> list[dict]:
    word = keyword.strip().lower()
    if not word:
        return []
    normalized_word = re.sub(r"\s+", "", word)
    return [
        item
        for item in _security_index_items()
        if normalized_word in str(item.get("symbol") or "").lower()
        or word in str(item.get("name") or "").lower()
    ][: max(1, int(limit or 20))]


def _is_usable_security_name(value: object, symbol: str) -> bool:
    name = str(value or "").strip()
    normalized_symbol = _normalize_symbol(symbol)
    return bool(name) and name not in {normalized_symbol, "代码直查", "--", "-"} and not name.isdigit()


def resolve_stock_name(symbol: str, *candidates: object) -> str:
    """Return a displayable name without persisting symbols as names."""

    normalized = _normalize_symbol(symbol)
    for candidate in candidates:
        value = candidate.get("name") if isinstance(candidate, dict) else candidate
        if _is_usable_security_name(value, normalized):
            return str(value).strip()

    security = db.get_security(normalized)
    if security and _is_usable_security_name(security.get("name"), normalized):
        return str(security["name"]).strip()

    fallback = next((item.get("名称") for item in FALLBACK_STOCKS if str(item.get("代码") or "") == normalized), "")
    return str(fallback or "").strip()


def list_stocks() -> list[dict]:
    try:
        items = _security_index_items()
        if items:
            return items
    except Exception:
        pass
    return _stock_rows_to_items(pd.DataFrame(FALLBACK_STOCKS))


def list_price_limited_non_st_stocks(min_price: float, max_price: float) -> list[dict]:
    if max_price <= 0 or min_price > max_price:
        return []
    try:
        _ensure_securities()
        items = db.price_limited_non_st(min_price, max_price)
        if items:
            return items
    except Exception:
        pass
    try:
        return market_data.list_price_limited_stocks(
            min_price,
            max_price,
            tencent_securities=db.list_securities(),
        )
    except Exception:
        return []


def get_market_today(include_growth_boards: bool = False, refresh_indices: bool = False) -> dict:
    quotes = db.list_daily_quotes()
    snapshot = _market_snapshot_metadata()
    valid = [item for item in quotes if _safe_float(item.get("latestPrice")) is not None]
    ranking_quotes = valid if include_growth_boards else [
        item for item in valid if not _is_growth_or_star_board(item.get("symbol"))
    ]
    ranking_quotes = [
        item for item in ranking_quotes if not market_data.is_beijing_board_symbol(item.get("symbol"))
    ]
    pct_values = [_safe_float(item.get("pctChange")) for item in valid]
    pct_values = [value for value in pct_values if value is not None]
    total_amount = sum(_safe_float(item.get("amount")) or 0 for item in valid)
    up_count = sum(1 for value in pct_values if value > 0)
    down_count = sum(1 for value in pct_values if value < 0)
    flat_count = max(0, len(valid) - up_count - down_count)
    avg_pct = round(sum(pct_values) / len(pct_values), 3) if pct_values else 0

    return {
        "quoteMinute": snapshot["quoteMinute"],
        "updatedAt": snapshot["updatedAt"],
        # The home screen reports the local snapshot only. It must never start
        # an all-market refresh as a side effect of opening or refreshing it.
        "requiredQuoteMinute": snapshot["requiredQuoteMinute"],
        "session": snapshot.get("session"),
        "tradeDate": snapshot.get("tradeDate"),
        "complete": snapshot["complete"],
        "stale": snapshot["stale"],
        "source": "cache",
        "count": len(valid),
        "upCount": up_count,
        "downCount": down_count,
        "flatCount": flat_count,
        "limitUpCount": sum(1 for value in pct_values if value >= 9.9),
        "limitDownCount": sum(1 for value in pct_values if value <= -9.9),
        "totalAmount": round(total_amount, 2),
        "prevTotalAmount": _safe_float(db.get_app_metadata("market_prev_total_amount")),
        "prevTradeDate": db.get_app_metadata("market_prev_total_date"),
        "avgPctChange": avg_pct,
        "heat": _market_heat(up_count, down_count, avg_pct),
        "rankingIncludesGrowthBoards": include_growth_boards,
        "topGainers": _top_quotes(ranking_quotes, "pctChange", reverse=True),
        "topLosers": _top_quotes(ranking_quotes, "pctChange", reverse=False),
        "topAmounts": _top_quotes(ranking_quotes, "amount", reverse=True),
        "topTurnover": _top_quotes(ranking_quotes, "turnoverRate", reverse=True),
        "indices": get_home_index_quotes(force=refresh_indices, cache_only=not refresh_indices),
    }


def get_stock_quotes(symbols: list[str]) -> dict:
    normalized_symbols = [_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol)]
    if not normalized_symbols:
        return {"quoteMinute": None, "updatedAt": None, "stale": False, "items": []}
    meta = ensure_daily_quotes()
    return {
        "quoteMinute": meta.get("quoteMinute"),
        "updatedAt": meta.get("updatedAt"),
        "stale": bool(meta.get("stale")),
        "source": meta.get("source"),
        "items": db.list_daily_quotes(normalized_symbols),
    }


def get_portfolio(user_id: int) -> dict:
    positions = db.list_portfolio_positions(user_id)
    account = db.get_portfolio_account(user_id)
    snapshot = _market_snapshot_metadata()
    symbols = [item["symbol"] for item in positions]
    quote_map = {item["symbol"]: item for item in db.list_daily_quotes(symbols)} if symbols else {}
    for symbol in symbols:
        cached = quote_map.get(symbol) or {}
        if _safe_float(cached.get("latestPrice")) is not None:
            continue
        holding_snapshot = _get_snapshot_with_cache(symbol)
        if holding_snapshot:
            quote_map[symbol] = holding_snapshot
    items = [_portfolio_with_quote(position, quote_map.get(position["symbol"])) for position in positions]
    total_market_value = sum(item.get("marketValue") or 0 for item in items)
    total_cost_value = sum(item.get("costValue") or 0 for item in items)
    floating_profit = total_market_value - total_cost_value
    cash_balance = float(account.get("cashBalance") or 0)
    return {
        "quoteMinute": snapshot["quoteMinute"],
        "updatedAt": snapshot["updatedAt"],
        "requiredQuoteMinute": snapshot["requiredQuoteMinute"],
        "stale": snapshot["stale"],
        "source": "cache",
        "marketData": snapshot,
        "items": items,
        "account": {
            **account,
            "totalMarketValue": round(total_market_value, 2),
            "totalAccountValue": round(total_market_value + cash_balance, 2),
        },
        "summary": {
            "positionCount": len(items),
            "totalMarketValue": round(total_market_value, 2),
            "totalCostValue": round(total_cost_value, 2),
            "floatingProfit": round(floating_profit, 2),
            "floatingProfitRate": round((floating_profit / total_cost_value) * 100, 3) if total_cost_value > 0 else 0,
            "cashBalance": round(cash_balance, 2),
            "totalAccountValue": round(total_market_value + cash_balance, 2),
        },
    }


def get_watchlist(user_id: int) -> dict:
    watchlist = db.list_watchlist_items(user_id)
    snapshot = _market_snapshot_metadata()
    symbols = [item["symbol"] for item in watchlist]
    quote_map = {item["symbol"]: item for item in db.list_daily_quotes(symbols)} if symbols else {}
    for symbol in symbols:
        cached = quote_map.get(symbol) or {}
        if _safe_float(cached.get("latestPrice")) is not None:
            continue
        item_snapshot = _get_snapshot_with_cache(symbol)
        if item_snapshot:
            quote_map[symbol] = item_snapshot
    return {
        "quoteMinute": snapshot["quoteMinute"],
        "updatedAt": snapshot["updatedAt"],
        "requiredQuoteMinute": snapshot["requiredQuoteMinute"],
        "stale": snapshot["stale"],
        "source": "cache",
        "marketData": snapshot,
        "items": [_watchlist_with_quote(item, quote_map.get(item["symbol"])) for item in watchlist],
    }


def save_portfolio_position(user_id: int, symbol: str, cost_price: float, shares: int, note: str = "", name: str = "") -> dict:
    normalized = _normalize_symbol(symbol)
    if not re.fullmatch(r"\d{6}", normalized or ""):
        raise ValueError("请输入有效股票代码")
    cost = float(cost_price)
    share_count = int(shares)
    if cost <= 0:
        raise ValueError("成本价必须大于 0")
    if share_count <= 0:
        raise ValueError("股数必须大于 0")

    quote = (db.list_daily_quotes([normalized]) or [None])[0]
    if not quote or _safe_float(quote.get("latestPrice")) is None:
        quote = _get_snapshot_with_cache(normalized)
    position_name = resolve_stock_name(normalized, name, quote)
    saved = db.upsert_portfolio_position(
        user_id,
        {
            "symbol": normalized,
            "name": position_name,
            "costPrice": round(cost, 4),
            "shares": share_count,
            "note": str(note or "").strip(),
        }
    )
    quote = (db.list_daily_quotes([normalized]) or [None])[0]
    if not quote or _safe_float(quote.get("latestPrice")) is None:
        quote = _get_snapshot_with_cache(normalized)
    return _portfolio_with_quote(saved, quote)


def remove_portfolio_position(user_id: int, symbol: str) -> bool:
    return db.delete_portfolio_position(user_id, _normalize_symbol(symbol))


def save_watchlist_item(user_id: int, symbol: str, note: str = "", name: str = "") -> dict:
    normalized = _normalize_symbol(symbol)
    if not re.fullmatch(r"\d{6}", normalized or ""):
        raise ValueError("请输入有效股票代码")
    quote = (db.list_daily_quotes([normalized]) or [None])[0]
    if not quote or _safe_float(quote.get("latestPrice")) is None:
        quote = _get_snapshot_with_cache(normalized)
    watch_name = resolve_stock_name(normalized, name, quote)
    saved = db.upsert_watchlist_item(
        user_id,
        {
            "symbol": normalized,
            "name": watch_name,
            "note": str(note or "").strip(),
        }
    )
    quote = (db.list_daily_quotes([normalized]) or [None])[0]
    if not quote or _safe_float(quote.get("latestPrice")) is None:
        quote = _get_snapshot_with_cache(normalized)
    return _watchlist_with_quote(saved, quote)


def remove_watchlist_item(user_id: int, symbol: str) -> bool:
    return db.delete_watchlist_item(user_id, _normalize_symbol(symbol))


def set_portfolio_cash(user_id: int, cash_balance: float) -> dict:
    return db.set_portfolio_cash(user_id, cash_balance)


def execute_portfolio_trade(
    user_id: int,
    action: str,
    symbol: str,
    price: float,
    shares: int,
    fee: float | None = None,
    note: str = "",
    name: str = "",
    discipline_override: bool = False,
) -> dict:
    normalized = _normalize_symbol(symbol)
    if not re.fullmatch(r"\d{6}", normalized or ""):
        raise ValueError("请输入有效股票代码")
    quote = (db.list_daily_quotes([normalized]) or [None])[0]
    if not quote or _safe_float(quote.get("latestPrice")) is None:
        quote = _get_snapshot_with_cache(normalized)
    position_name = resolve_stock_name(normalized, name, quote)
    return db.execute_portfolio_trade(
        user_id,
        {
            "action": action,
            "symbol": normalized,
            "name": position_name,
            "price": price,
            "shares": shares,
            "fee": fee,
            "note": note,
            "disciplineOverride": discipline_override is True,
        }
    )


def list_portfolio_trades(user_id: int, limit: int = 30) -> list[dict]:
    return db.list_portfolio_trades(user_id, limit)


def ensure_daily_quotes(force: bool = False, now: datetime | None = None) -> dict:
    db.init_db()
    now = now or datetime.now()
    current_minute = _current_quote_session_key(now)
    status = db.daily_quote_cache_status()
    snapshot = _market_snapshot_metadata(status=status, now=now)
    expected_count = snapshot["expectedCount"]
    cache_is_complete = snapshot["complete"]
    if not force and not snapshot["stale"]:
        return {**status, "stale": False, "source": "cache"}

    try:
        result = market_data.list_non_tushare_market_quotes(
            page_size=6000,
            minimum_count=int(expected_count * 0.7),
            tencent_securities=db.list_securities(),
        )
        source = result.source
        items = _normalize_market_quote_names(result.items)
    except MarketDataError as data_error:
        status = db.daily_quote_cache_status()
        if status.get("count", 0) >= int(expected_count * 0.7):
            return {
                **status,
                "stale": True,
                "source": "cache",
                "error": "；".join(data_error.errors),
            }
        raise RuntimeError(str(data_error)) from data_error

    # 快照换交易日时，把旧快照的全市场成交额存为“上一交易日”基准，供首页差额展示
    previous_status = db.daily_quote_cache_status()
    previous_minute = previous_status.get("quoteMinute")
    previous_day = _snapshot_quote_date(previous_minute) if previous_minute else None
    next_day = _snapshot_quote_date(current_minute)
    if previous_day and next_day and previous_day != next_day:
        previous_total = sum(_safe_float(item.get("amount")) or 0 for item in db.list_daily_quotes())
        if previous_total > 0:
            db.set_app_metadata("market_prev_total_amount", str(round(previous_total, 2)))
            db.set_app_metadata("market_prev_total_date", previous_day.isoformat())

    db.replace_daily_quotes(items, current_minute)
    if source in {"eastmoney", "tencent", "akshare"}:
        db.upsert_securities(items)
    status = db.daily_quote_cache_status()
    if status.get("count", 0) < int(expected_count * 0.7):
        raise RuntimeError(f"全市场行情写入不完整：{status.get('count', 0)}/{expected_count}")
    return {**status, "stale": False, "source": source}


def synchronize_market_data(force: bool = False, now: datetime | None = None) -> dict:
    """Synchronize the full-market snapshot and retain a usable cache on provider failure."""

    db.init_db()
    now = now or datetime.now()
    before = _market_snapshot_metadata(now=now)
    if not force and not before["stale"]:
        return {**before, "refreshed": False, "source": "cache", "error": None}

    result = ensure_daily_quotes(force=force, now=now)
    after = _market_snapshot_metadata(now=now)
    return {
        **after,
        # A forced provider check that falls back to the cache is not proof
        # that the snapshot remains current, even if its coarse session key
        # still matches the current date.
        "stale": bool(after["stale"] or result.get("stale")),
        "refreshed": result.get("source") in {"tushare", "eastmoney", "tencent", "akshare"},
        "source": result.get("source", "cache"),
        "error": result.get("error"),
    }


def _home_index_items() -> list[tuple[str, str]]:
    return [
        ("000001", "上证指数"),
        ("399001", "深成指"),
        ("399006", "创业板指"),
        ("000300", "沪深300"),
        ("000905", "中证500"),
        ("000852", "中证1000"),
        ("000688", "科创50"),
    ]


def _refresh_home_index_quote(item: tuple[str, str]) -> dict:
    symbol, name = item
    latest = {}
    try:
        result = market_data.get_daily_kline(
            symbol,
            limit=3,
            adjust=INDEX_ADJUST,
            is_index=True,
        )
        if result.items:
            tagged_items = _tag_history_items(result.items, result.source)
            db.upsert_klines(symbol, DAILY_PERIOD, INDEX_ADJUST, tagged_items)
            latest = tagged_items[-1]
    except MarketDataError:
        pass
    if not latest:
        history = get_cached_index_history(symbol=symbol)
        latest = history[-1] if history else {}
    return {
        "symbol": symbol,
        "name": name,
        "latestPrice": latest.get("close"),
        "pctChange": latest.get("pctChange"),
        "amount": latest.get("amount"),
        "date": latest.get("date"),
    }


def _cached_home_index_quotes(items: list[tuple[str, str]] | None = None) -> list[dict]:
    quotes: list[dict] = []
    for symbol, name in items or _home_index_items():
        try:
            history = get_cached_index_history(symbol=symbol)
            latest = history[-1] if history else {}
            quotes.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "latestPrice": latest.get("close"),
                    "pctChange": latest.get("pctChange"),
                    "amount": latest.get("amount"),
                    "date": latest.get("date"),
                }
            )
        except Exception:
            quotes.append({"symbol": symbol, "name": name, "latestPrice": None, "pctChange": None})
    return quotes


def get_home_index_quotes(force: bool = False, cache_only: bool = False) -> list[dict]:
    global _INDEX_QUOTES_LAST_REFRESH

    items = _home_index_items()
    if cache_only:
        return _cached_home_index_quotes(items)

    now = datetime.now()
    with _INDEX_QUOTE_LOCK:
        should_refresh = force or _INDEX_QUOTES_LAST_REFRESH is None or now - _INDEX_QUOTES_LAST_REFRESH >= INDEX_QUOTES_REFRESH_INTERVAL
        if should_refresh:
            # Claim the refresh window before network calls so concurrent home
            # requests do not start seven duplicate provider requests.
            _INDEX_QUOTES_LAST_REFRESH = now

    items = _home_index_items()
    if should_refresh:
        with ThreadPoolExecutor(max_workers=4) as executor:
            return list(executor.map(_refresh_home_index_quote, items))

    return _cached_home_index_quotes(items)


def get_stock_history(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    minimum_bars: int = 0,
) -> list[dict]:
    """Return daily bars, refreshing a short cache when a caller needs more history."""

    normalized = _normalize_symbol(symbol)
    cached = db.query_klines(normalized, DAILY_PERIOD, DAILY_ADJUST, start_date, end_date, max_age=timedelta(hours=18))
    required_bars = max(0, int(minimum_bars or 0))
    if cached and len(cached) >= required_bars and not _history_needs_turnover_refresh(cached):
        return cached
    limit = _estimate_limit(start_date, end_date)
    try:
        result = market_data.get_daily_kline(
            normalized,
            start_date,
            end_date,
            limit=limit,
            adjust=DAILY_ADJUST,
            is_index=False,
            minimum_bars=required_bars,
            require_turnover=True,
        )
        tagged_items = _tag_history_items(result.items, result.source)
        db.upsert_klines(normalized, DAILY_PERIOD, DAILY_ADJUST, tagged_items)
        selected_ids = {id(item) for item in result.selected_items}
        return [
            tagged_items[index]
            for index, item in enumerate(result.items)
            if id(item) in selected_ids
        ]
    except MarketDataError as data_error:
        raise RuntimeError("日K数据源连续失败：" + "；".join(data_error.errors[-3:])) from data_error


def _history_needs_turnover_refresh(history: list[dict]) -> bool:
    """Refresh any stock daily cache that lacks historical turnover data."""

    return any(
        _turnover_value(bar) is None
        for bar in history
    )


def _tag_history_items(items: list[dict], source: str) -> list[dict]:
    """Keep the selected provider visible after bars are persisted locally."""

    provider = str(source or "unknown").strip().lower() or "unknown"
    return [
        {
            **item,
            "source": item.get("source") or provider,
            "provider": item.get("provider") or provider,
        }
        for item in items
    ]


def _turnover_value(item: dict) -> float | None:
    for field in ("turnoverRate", "turnover_rate", "providerTurnover"):
        value = _safe_float(item.get(field))
        if value is not None:
            return value
    return None


def get_cached_stock_history(symbol: str, start_date: str = "") -> list[dict]:
    """Return stored completed daily bars without a network fallback."""

    normalized = _normalize_symbol(symbol)
    return _latest_continuous_daily_cache(normalized, DAILY_ADJUST, start_date)


def get_stock_intraday_history(symbol: str, period: str, start_date: str, end_date: str) -> list[dict]:
    normalized = _normalize_symbol(symbol)
    adjust = DAILY_ADJUST
    cached = db.query_klines(normalized, period, adjust, start_date, end_date, max_age=timedelta(minutes=20))
    if cached:
        return cached
    try:
        result = market_data.get_intraday_kline(
            normalized,
            period,
            start_date,
            end_date,
            limit=220,
            adjust=adjust,
            is_index=False,
        )
        tagged_items = _tag_history_items(result.items, result.source)
        db.upsert_klines(normalized, period, adjust, tagged_items)
        selected_ids = {id(item) for item in result.selected_items}
        return [
            tagged_items[index]
            for index, item in enumerate(result.items)
            if id(item) in selected_ids
        ]
    except MarketDataError as data_error:
        raise RuntimeError("分钟数据源连续失败：" + "；".join(data_error.errors)) from data_error


def get_index_history(symbol: str = "000001", start_date: str = "", end_date: str = "") -> list[dict]:
    normalized = _normalize_symbol(symbol) or "000001"
    cached = db.query_klines(normalized, DAILY_PERIOD, INDEX_ADJUST, start_date, end_date, max_age=timedelta(hours=18))
    if cached:
        return cached

    try:
        result = market_data.get_daily_kline(
            normalized,
            start_date,
            end_date,
            limit=_estimate_limit(start_date, end_date),
            adjust=INDEX_ADJUST,
            is_index=True,
        )
        tagged_items = _tag_history_items(result.items, result.source)
        db.upsert_klines(normalized, DAILY_PERIOD, INDEX_ADJUST, tagged_items)
        selected_ids = {id(item) for item in result.selected_items}
        return [
            tagged_items[index]
            for index, item in enumerate(result.items)
            if id(item) in selected_ids
        ]
    except MarketDataError as data_error:
        # Keep stale local bars as the final safety net for analysis screens.
        stale = db.query_klines(normalized, DAILY_PERIOD, INDEX_ADJUST, start_date, end_date, max_age=None)
        if stale:
            return stale
        raise RuntimeError("指数数据源连续失败：" + "；".join(data_error.errors)) from data_error


def get_cached_index_history(symbol: str = "000001", start_date: str = "") -> list[dict]:
    """Return stored index daily bars without refreshing market data."""

    normalized = _normalize_symbol(symbol) or "000001"
    return _latest_continuous_daily_cache(normalized, INDEX_ADJUST, start_date)


def get_index_intraday_history(symbol: str = "000001", period: str = "15", start_date: str = "", end_date: str = "") -> list[dict]:
    normalized = _normalize_symbol(symbol) or "000001"
    cached = db.query_klines(normalized, period, INDEX_ADJUST, start_date, end_date, max_age=timedelta(minutes=20))
    if cached:
        return cached
    try:
        result = market_data.get_intraday_kline(
            normalized,
            period,
            start_date,
            end_date,
            limit=220,
            adjust=INDEX_ADJUST,
            is_index=True,
        )
        tagged_items = _tag_history_items(result.items, result.source)
        db.upsert_klines(normalized, period, INDEX_ADJUST, tagged_items)
        selected_ids = {id(item) for item in result.selected_items}
        return [
            tagged_items[index]
            for index, item in enumerate(result.items)
            if id(item) in selected_ids
        ]
    except MarketDataError as data_error:
        raise RuntimeError("指数分钟数据源连续失败：" + "；".join(data_error.errors)) from data_error


def get_stock_fundamentals(symbol: str, refresh_core: bool = False, start_background: bool = True) -> dict:
    normalized = _normalize_symbol(symbol)
    db.init_db()
    cached = None if refresh_core else db.get_fundamental_summary(normalized, max_age=timedelta(days=1))
    if cached:
        # Rebuild the compact summary from raw F10 rows so fixes to field
        # mappings also repair summaries that were cached before the fix.
        status = db.get_f10_status(normalized)
        if start_background:
            status = ensure_f10_background_sync(normalized)
        summary = build_fundamental_summary(normalized, valuation=_resolve_valuation(normalized, cached.get("valuation")))
        summary["f10Status"] = status
        return db.save_fundamental_summary(normalized, summary)

    snapshot = _resolve_valuation(normalized)
    saved_rows = 0
    failures: list[dict] = []
    for report in CORE_F10_REPORTS:
        try:
            payload = fetch_f10_report(normalized, report, page_size=10, sort_columns=_sort_columns(report), sort_types=_sort_types(report))
            rows = extract_f10_rows(payload)
            saved_rows += db.upsert_f10_report_rows(normalized, report, rows)
        except Exception as exc:
            failures.append({"reportName": report, "error": str(exc)})

    summary = build_fundamental_summary(normalized, valuation=snapshot)
    status = db.get_f10_status(normalized)
    if failures:
        status["coreFailures"] = failures
    summary["f10Status"] = status
    saved = db.save_fundamental_summary(normalized, summary)
    if start_background:
        ensure_f10_background_sync(normalized)
    return saved


def get_stock_fundamentals_quick(symbol: str, start_background: bool = True) -> dict:
    normalized = _normalize_symbol(symbol)
    cached = db.get_fundamental_summary(normalized, max_age=timedelta(days=1))
    if cached:
        status = db.get_f10_status(normalized)
        if start_background:
            status = ensure_f10_background_sync(normalized)
        summary = build_fundamental_summary(normalized, valuation=_resolve_valuation(normalized, cached.get("valuation")))
        summary["f10Status"] = status
        return db.save_fundamental_summary(normalized, summary)
    status = ensure_f10_background_sync(normalized) if start_background else db.get_f10_status(normalized)
    summary = build_fundamental_summary(normalized, valuation=_resolve_valuation(normalized))
    summary["f10Status"] = status
    return db.save_fundamental_summary(normalized, summary)


def get_f10_status(symbol: str) -> dict:
    return db.get_f10_status(_normalize_symbol(symbol))


def sync_f10_reports(symbol: str, reports: list[str] | None = None) -> dict:
    normalized = _normalize_symbol(symbol)
    target_reports = reports or F10_REPORTS
    started_at = db.now_iso()
    failures: list[dict] = []
    completed = 0
    db.set_f10_status(normalized, "running", len(target_reports), 0, [], started_at=started_at)
    for report in target_reports:
        try:
            payload = fetch_f10_report(normalized, report, page_size=20, sort_columns=_sort_columns(report), sort_types=_sort_types(report))
            rows = extract_f10_rows(payload)
            db.upsert_f10_report_rows(normalized, report, rows)
            completed += 1
        except Exception as exc:
            failures.append({"reportName": report, "error": str(exc)})
        db.set_f10_status(normalized, "running", len(target_reports), completed, failures, started_at=started_at)

    status = "completed" if not failures else "partial"
    finished_at = db.now_iso()
    result = db.set_f10_status(normalized, status, len(target_reports), completed, failures, started_at=started_at, finished_at=finished_at)
    summary = build_fundamental_summary(normalized, valuation=_resolve_valuation(normalized, db.get_snapshot(normalized)))
    summary["f10Status"] = result
    db.save_fundamental_summary(normalized, summary)
    return result


def ensure_f10_background_sync(symbol: str) -> dict:
    normalized = _normalize_symbol(symbol)
    status = db.get_f10_status(normalized)
    if status.get("status") == "running":
        return status
    if status.get("status") in {"completed", "partial"} and db.is_fresh(status.get("updatedAt"), timedelta(days=7)):
        return status
    with _F10_LOCK:
        if normalized in _F10_RUNNING:
            status["status"] = "running"
            return status
        _F10_RUNNING.add(normalized)

    def _run() -> None:
        try:
            sync_f10_reports(normalized)
        finally:
            with _F10_LOCK:
                _F10_RUNNING.discard(normalized)

    _F10_EXECUTOR.submit(_run)
    return db.set_f10_status(
        normalized,
        "running",
        len(F10_REPORTS),
        int(status.get("completedReports") or 0),
        status.get("failures") or [],
        started_at=status.get("startedAt") or db.now_iso(),
    )


def build_fundamental_summary(symbol: str, valuation: dict | None = None) -> dict:
    normalized = _normalize_symbol(symbol)
    valuation = _merge_valuation_sources(valuation, db.get_snapshot(normalized))
    profile_rows = _f10_rows_any(
        normalized,
        ("RPT_F10_BASIC_ORGINFO", "RPT_F10_ORG_BASICINFO", "RPT_HSF9_BASIC_ORGINFO"),
        limit=3,
    )
    finance_rows = _first_f10_rows(
        normalized,
        ("RPT_F10_FINANCE_MAINFINADATA", "RPT_F10_QTR_MAINFINADATA", "RPT_PCF10_FINANCEMAINFINADATA"),
        limit=8,
    )
    finance_rows = _enrich_finance_rows(normalized, finance_rows)
    holder_rows = _first_f10_rows(
        normalized,
        ("RPT_F10_EH_HOLDERS", "RPT_F10_EH_FREEHOLDERS", "RPT_F10_EH_HOLDERSDATE"),
        limit=10,
    )
    theme_rows = _merge_f10_rows_by_identity(
        _f10_rows_any(
            normalized,
            ("RPT_F10_CORETHEME_BOARDTYPE", "RPT_F10_RELATE_GN", "RPT_F10_RELATE_RANK"),
            limit=40,
        ),
        ("BOARD_CODE", "BK_CODE", "BOARD_NAME", "SECURITY_NAME", "CONCEPT_NAME"),
    )
    business_rows = _first_f10_rows(normalized, ("RPT_F10_OP_BUSINESSANALYSIS",), limit=3)
    operation_rows = _f10_rows_any(normalized, ("RPT_F10_FN_MAINOP",), limit=12)
    management_rows = _f10_rows_any(normalized, ("RPT_F10_ORGINFO_MANAINTRO",), limit=12)
    dividend_rows = _f10_rows_any(normalized, ("RPT_F10_DIVIDEND_MAIN",), limit=8)

    profile = _extract_profile(_merge_f10_rows(profile_rows))
    themes = [_extract_theme(row) for row in theme_rows]
    themes = [item for item in themes if item.get("name")]
    finance = [_extract_finance(row) for row in finance_rows]
    holder = [_extract_holder(row) for row in holder_rows]
    business = _extract_business(business_rows[0] if business_rows else {})
    business["mainOperations"] = [_extract_main_operation(row) for row in operation_rows]
    business["management"] = [_extract_management(row) for row in management_rows]
    business["dividends"] = [_extract_dividend(row) for row in dividend_rows]
    return {
        "symbol": normalized,
        "profile": profile,
        "valuation": {
            "latestPrice": valuation.get("latestPrice"),
            "pctChange": valuation.get("pctChange"),
            "turnoverRate": valuation.get("turnoverRate"),
            "peDynamic": valuation.get("peDynamic"),
            "peTtm": valuation.get("peTtm"),
            "pb": valuation.get("pb"),
            "totalMarketCap": valuation.get("totalMarketCap"),
            "circulatingMarketCap": valuation.get("circulatingMarketCap"),
        },
        "finance": finance,
        "holder": holder,
        "themes": themes,
        "business": business,
        "f10Status": db.get_f10_status(normalized),
    }


def _f10_rows_any(symbol: str, reports: tuple[str, ...], limit: int = 20) -> list[dict]:
    rows: list[dict] = []
    for report in reports:
        remaining = max(limit - len(rows), 0)
        if not remaining:
            break
        rows.extend(db.get_f10_report_rows(symbol, report, limit=remaining))
    return rows[:limit]


def _first_f10_rows(symbol: str, reports: tuple[str, ...], limit: int = 20) -> list[dict]:
    for report in reports:
        rows = db.get_f10_report_rows(symbol, report, limit=limit)
        if rows:
            return rows
    return []


def _merge_f10_rows(rows: list[dict]) -> dict:
    merged: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if _pick(merged, key) is None and value not in (None, "", "-", "--"):
                merged[key] = value
    return merged


def _merge_f10_rows_by_identity(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        identity = next((_pick(row, key) for key in keys if _pick(row, key) is not None), None)
        identity = str(identity or "").strip()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _report_date_key(row: dict) -> str | None:
    return _clean_date(_pick(row, "REPORT_DATE", "END_DATE"))


def _enrich_finance_rows(symbol: str, rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    support_reports = (
        "RPT_F10_FINANCE_GINCOME",
        "RPT_F10_FINANCE_GCASHFLOW",
        "RPT_F10_FINANCE_GBALANCE",
        "RPT_F10_FINANCE_GRATIO",
    )
    support: dict[str, list[dict]] = {}
    for report in support_reports:
        for row in db.get_f10_report_rows(symbol, report, limit=20):
            date_key = _report_date_key(row)
            if date_key:
                support.setdefault(date_key, []).append(row)
    enriched: list[dict] = []
    for row in rows:
        date_key = _report_date_key(row)
        enriched.append(_merge_f10_rows([row, *support.get(date_key or "", [])]))
    return enriched


def _previous_weekday(day):
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _market_snapshot_session(now: datetime) -> dict:
    """Describe the market phase that determines quote-cache freshness."""

    if now.weekday() >= 5:
        trade_day = _previous_weekday(now.date())
        return {"phase": "closed", "tradeDay": trade_day, "requiredQuoteMinute": f"{trade_day:%Y-%m-%d} closed"}

    current_time = now.time()
    if current_time < MORNING_SESSION_START:
        trade_day = _previous_weekday(now.date())
        return {"phase": "pre_open", "tradeDay": trade_day, "requiredQuoteMinute": f"{trade_day:%Y-%m-%d} closed"}
    if current_time < MORNING_SESSION_END:
        return {"phase": "morning", "tradeDay": now.date(), "requiredQuoteMinute": now.strftime("%Y-%m-%d %H:%M")}
    if current_time < AFTERNOON_SESSION_START:
        return {"phase": "lunch_break", "tradeDay": now.date(), "requiredQuoteMinute": f"{now:%Y-%m-%d} intraday"}
    if current_time < CLOSE_SNAPSHOT_TIME:
        return {"phase": "afternoon", "tradeDay": now.date(), "requiredQuoteMinute": now.strftime("%Y-%m-%d %H:%M")}
    return {"phase": "after_close", "tradeDay": now.date(), "requiredQuoteMinute": f"{now:%Y-%m-%d} closed"}


def _current_quote_session_key(now: datetime | None = None) -> str:
    return _market_snapshot_session(now or datetime.now())["requiredQuoteMinute"]


def _snapshot_quote_date(value: str | None):
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _snapshot_quote_time(value: str | None) -> datetime | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$", str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _snapshot_updated_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _market_snapshot_metadata(
    *,
    status: dict | None = None,
    required_quote_minute: str | None = None,
    now: datetime | None = None,
) -> dict:
    status = status or db.daily_quote_cache_status()
    now = now or datetime.now()
    session = _market_snapshot_session(now)
    required = required_quote_minute or session["requiredQuoteMinute"]
    expected_count = max(len(db.list_securities()), 1000)
    count = int(status.get("count") or 0)
    complete = count >= int(expected_count * 0.7)
    quote_minute = status.get("quoteMinute") or None
    updated_at = status.get("updatedAt") or None
    quote_day = _snapshot_quote_date(quote_minute)
    quote_time = _snapshot_quote_time(quote_minute)
    updated_time = _snapshot_updated_time(updated_at)
    trade_day = session["tradeDay"]
    is_trade_day_snapshot = quote_day == trade_day
    is_closed_snapshot = str(quote_minute or "").endswith(" closed")
    close_threshold = datetime.combine(trade_day, CLOSE_SNAPSHOT_TIME)

    current = False
    if complete and updated_time and is_trade_day_snapshot:
        if session["phase"] in {"morning", "afternoon"} and quote_time:
            age = now - quote_time
            current = timedelta(0) <= age <= INTRADAY_SNAPSHOT_MAX_AGE
        elif session["phase"] == "lunch_break" and quote_time:
            current = quote_time.time() >= MORNING_SESSION_START
        elif session["phase"] == "after_close":
            current = is_closed_snapshot and updated_time >= close_threshold
        elif session["phase"] in {"pre_open", "closed"}:
            current = is_closed_snapshot
    return {
        "quoteMinute": quote_minute,
        "updatedAt": updated_at,
        "requiredQuoteMinute": required,
        "session": session["phase"],
        "tradeDate": trade_day.isoformat(),
        "maxAgeSeconds": int(INTRADAY_SNAPSHOT_MAX_AGE.total_seconds()) if session["phase"] in {"morning", "afternoon"} else None,
        "count": count,
        "expectedCount": expected_count,
        "complete": complete,
        "stale": not current,
    }


def _market_heat(up_count: int, down_count: int, avg_pct: float) -> str:
    total = up_count + down_count
    if total <= 0:
        return "暂无行情"
    up_ratio = up_count / total
    if avg_pct >= 1 or up_ratio >= 0.68:
        return "偏强"
    if avg_pct <= -1 or up_ratio <= 0.32:
        return "偏弱"
    return "震荡"


def _is_growth_or_star_board(symbol: object) -> bool:
    normalized = _normalize_symbol(symbol)
    return bool(re.fullmatch(r"\d{6}", normalized or "") and normalized.startswith(GROWTH_BOARD_PREFIXES))


def _top_quotes(items: list[dict], key: str, reverse: bool, limit: int = 10) -> list[dict]:
    filtered = [item for item in items if _safe_float(item.get(key)) is not None]
    return [
        {
            "symbol": item.get("symbol"),
            "name": resolve_stock_name(str(item.get("symbol") or ""), item),
            "latestPrice": item.get("latestPrice"),
            "pctChange": item.get("pctChange"),
            "priceChange": item.get("priceChange"),
            "volume": item.get("volume"),
            "amount": item.get("amount"),
            "turnoverRate": item.get("turnoverRate"),
        }
        for item in sorted(filtered, key=lambda quote: _safe_float(quote.get(key)) or 0, reverse=reverse)[:limit]
    ]


def _portfolio_with_quote(position: dict, quote: dict | None) -> dict:
    quote = quote or {}
    shares = int(position.get("shares") or 0)
    cost_price = float(position.get("costPrice") or 0)
    latest_price = _safe_float(quote.get("latestPrice"))
    market_value = (latest_price or 0) * shares
    cost_value = cost_price * shares
    profit = market_value - cost_value
    name = resolve_stock_name(position.get("symbol") or "", quote, position)
    return {
        **position,
        "name": name,
        "label": f'{position.get("symbol")} - {name}' if name else position.get("symbol"),
        "latestPrice": latest_price,
        "pctChange": quote.get("pctChange"),
        "priceChange": quote.get("priceChange"),
        "turnoverRate": quote.get("turnoverRate"),
        "amount": quote.get("amount"),
        "quoteMinute": quote.get("quoteMinute"),
        "marketValue": round(market_value, 2),
        "costValue": round(cost_value, 2),
        "floatingProfit": round(profit, 2),
        "floatingProfitRate": round((profit / cost_value) * 100, 3) if cost_value > 0 else 0,
    }


def _watchlist_with_quote(item: dict, quote: dict | None) -> dict:
    quote = quote or {}
    name = resolve_stock_name(item.get("symbol") or "", quote, item)
    return {
        **item,
        "name": name,
        "label": f'{item.get("symbol")} - {name}' if name else item.get("symbol"),
        "latestPrice": _safe_float(quote.get("latestPrice")),
        "pctChange": _safe_float(quote.get("pctChange")),
        "priceChange": _safe_float(quote.get("priceChange")),
        "turnoverRate": _safe_float(quote.get("turnoverRate")),
        "amount": _safe_float(quote.get("amount")),
        "quoteMinute": quote.get("quoteMinute"),
    }


def _security_index_items() -> list[dict]:
    _ensure_securities()
    with _SECURITY_INDEX_LOCK:
        return [dict(item) for item in _SECURITY_INDEX_ITEMS]


def _normalize_market_quote_names(items: list[dict]) -> list[dict]:
    """Prefer a validated catalog name before storing market quote rows."""

    catalog_names = {
        _normalize_symbol(item.get("symbol") or ""): str(item.get("name") or "").strip()
        for item in db.list_securities()
        if _is_usable_security_name(item.get("name"), str(item.get("symbol") or ""))
    }
    normalized_items: list[dict] = []
    for item in items:
        quote = dict(item)
        symbol = _normalize_symbol(quote.get("symbol") or "")
        catalog_name = catalog_names.get(symbol, "")
        quote_name = quote.get("name")
        if catalog_name:
            quote["name"] = catalog_name
        elif not _is_usable_security_name(quote_name, symbol):
            quote["name"] = ""
        normalized_items.append(quote)
    return normalized_items


def _set_security_index(items: list[dict], database_path: str) -> None:
    global _SECURITY_INDEX_ITEMS, _SECURITY_INDEX_BY_SYMBOL, _SECURITY_INDEX_DATABASE_PATH, _SECURITY_INDEX_EXPIRES_AT

    normalized: list[dict] = []
    by_symbol: dict[str, dict] = {}
    for item in items:
        symbol = _normalize_symbol(item.get("symbol") or "")
        name = str(item.get("name") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol or "") or not _is_usable_security_name(name, symbol):
            continue
        indexed = {**item, "symbol": symbol, "name": name, "label": f"{symbol} - {name}"}
        normalized.append(indexed)
        by_symbol[symbol] = indexed

    _SECURITY_INDEX_ITEMS = tuple(normalized)
    _SECURITY_INDEX_BY_SYMBOL = by_symbol
    _SECURITY_INDEX_DATABASE_PATH = database_path
    _SECURITY_INDEX_EXPIRES_AT = datetime.now() + SECURITY_INDEX_REFRESH_INTERVAL


def _ensure_securities() -> None:
    """Build one in-memory searchable catalog and refresh it infrequently."""

    db.init_db()
    database_path = str(db.db_path().resolve())
    now = datetime.now()
    with _SECURITY_INDEX_LOCK:
        if (
            _SECURITY_INDEX_ITEMS
            and _SECURITY_INDEX_DATABASE_PATH == database_path
            and _SECURITY_INDEX_EXPIRES_AT is not None
            and _SECURITY_INDEX_EXPIRES_AT > now
        ):
            return

        cached_items = db.list_securities()
        source = db.get_app_metadata(SECURITY_INDEX_SOURCE_METADATA_KEY)
        if (
            cached_items
            and source == SECURITY_INDEX_SOURCE
            and db.securities_are_fresh(SECURITY_INDEX_REFRESH_INTERVAL)
        ):
            _set_security_index(cached_items, database_path)
            return

        try:
            result = market_data.list_non_tushare_securities()
            items = result.items
            db.upsert_securities(items)
            db.set_app_metadata(SECURITY_INDEX_SOURCE_METADATA_KEY, SECURITY_INDEX_SOURCE)
            _set_security_index(items, database_path)
            return
        except Exception:
            if cached_items:
                _set_security_index(cached_items, database_path)


def _get_snapshot_with_cache(symbol: str) -> dict:
    snapshot = db.get_snapshot(symbol, max_age=timedelta(minutes=5))
    if snapshot:
        return snapshot
    try:
        snapshot = market_data.get_snapshot(symbol)
        db.upsert_snapshot(snapshot)
        _upsert_security_from_snapshot(snapshot)
        return snapshot
    except MarketDataError:
        return db.get_snapshot(symbol) or {}


def _has_valuation_value(value: object) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, float) and value != value:
        return False
    return True


def _merge_valuation_sources(*sources: dict | None) -> dict:
    """Fill valuation fields without letting a partial cache hide newer data."""

    merged: dict = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for field in VALUATION_FIELDS:
            value = source.get(field)
            if field not in merged or not _has_valuation_value(merged.get(field)):
                if _has_valuation_value(value):
                    merged[field] = value
    return merged


def _resolve_valuation(symbol: str, *preferred_sources: dict | None) -> dict:
    """Resolve PE/PB and quote fields from the best available local/provider data."""

    normalized = _normalize_symbol(symbol)
    preferred = _merge_valuation_sources(*preferred_sources)
    snapshot = _get_snapshot_with_cache(normalized) if len(preferred) < len(VALUATION_FIELDS) else {}
    daily_quote = (db.list_daily_quotes([normalized]) or [None])[0]
    security = (db.search_securities(normalized, limit=1) or [None])[0]
    return _merge_valuation_sources(snapshot, preferred, daily_quote, security)


def _upsert_security_from_snapshot(snapshot: dict) -> None:
    if not snapshot.get("symbol") or not _is_usable_security_name(snapshot.get("name"), str(snapshot.get("symbol") or "")):
        return
    symbol = snapshot["symbol"]
    db.upsert_securities(
        [
            {
                "symbol": symbol,
                "name": snapshot.get("name"),
                "market": secid(symbol).split(".")[0],
                "secid": secid(symbol),
                "secucode": secucode(symbol),
                "isSt": "ST" in str(snapshot.get("name") or "").upper(),
                "latestPrice": snapshot.get("latestPrice"),
                "pctChange": snapshot.get("pctChange"),
                "turnoverRate": snapshot.get("turnoverRate"),
                "peDynamic": snapshot.get("peDynamic"),
                "pb": snapshot.get("pb"),
                "totalMarketCap": snapshot.get("totalMarketCap"),
            }
        ]
    )


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


def _stock_rows_to_items(df: pd.DataFrame) -> list[dict]:
    return [{"symbol": str(row["代码"]), "name": str(row["名称"]), "label": f'{row["代码"]} - {row["名称"]}'} for _, row in df.iterrows()]




def _latest_continuous_daily_cache(symbol: str, adjust: str, start_date: str = "") -> list[dict]:
    """Use the newest uninterrupted local daily-bar segment for EOD decisions."""

    items = db.list_cached_klines(symbol, DAILY_PERIOD, adjust)
    segment_start = 0
    for index in range(1, len(items)):
        previous = str(items[index - 1].get("date") or "")[:10]
        current = str(items[index].get("date") or "")[:10]
        try:
            gap = (datetime.fromisoformat(current) - datetime.fromisoformat(previous)).days
        except ValueError:
            gap = 999
        if gap > 10:
            segment_start = index

    latest_segment = items[segment_start:]
    if not start_date:
        return latest_segment
    start_key = _date_filter_key(start_date)
    return [item for item in latest_segment if str(item.get("date") or "") >= start_key]


def _date_filter_key(value: str, end: bool = False) -> str:
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}{' 23:59' if end else ''}"
    return text


def _estimate_limit(start_date: str, end_date: str) -> int:
    try:
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        return max(120, min(int((end - start).days * 1.7), 1200))
    except Exception:
        return 370


def _extract_profile(row: dict) -> dict:
    return {
        "companyName": _pick(row, "ORG_NAME", "SECURITY_NAME_ABBR", "SECURITY_NAME"),
        "industry": _pick(row, "INDUSTRY", "CSRC_INDUSTRY", "CSRC_INDUSTRY_NAME", "EM2016"),
        "area": _pick(row, "PROVINCE", "REGION", "REGIONBK"),
        "listDate": _clean_date(_pick(row, "LISTING_DATE", "LIST_DATE")),
        "foundDate": _clean_date(_pick(row, "FOUND_DATE", "ESTABLISH_DATE")),
        "chairman": _pick(row, "CHAIRMAN", "PRESIDENT", "LEGAL_PERSON"),
        "secretary": _pick(row, "SECRETARY"),
        "mainBusiness": _pick(row, "BUSINESS_SCOPE", "MAIN_BUSINESS", "ORG_PROFILE", "ORG_PROFIE"),
        "website": _pick(row, "ORG_WEB", "WEB_SITE"),
        "address": _pick(row, "ADDRESS", "REG_ADDRESS"),
        "registeredCapital": _num(_pick(row, "REG_CAPITAL")),
        "employeeCount": _num(_pick(row, "EMP_NUM", "EMPLOYEE_NUM", "TOTAL_NUM", "TATOLNUMBER")),
        "actualHolder": _pick(row, "ACTUAL_HOLDER", "REAL_CONTROLER", "CONTROL_HOLDER"),
        "boardName": _pick(row, "BOARD_NAME_LEVEL", "BOARD_NAME_3LEVEL", "BOARD_NAME_2LEVEL", "BOARD_NAME_1LEVEL"),
    }


def _extract_finance(row: dict) -> dict:
    return {
        "reportDate": _clean_date(_pick(row, "REPORT_DATE")),
        "eps": _num(_pick(row, "BASIC_EPS", "EPSJB")),
        "bps": _num(_pick(row, "BPS")),
        "roe": _num(_pick(row, "ROEJQ", "ROE_WEIGHT", "JROE", "ROE_DILUTED")),
        "roeYoy": _num(_pick(row, "ROEJQTZ", "ROE_WEIGHT_YOY")),
        "netProfit": _num(_pick(row, "PARENTNETPROFIT", "NETPROFIT", "PARENT_NETPROFIT")),
        "netProfitYoy": _num(_pick(row, "PARENTNETPROFITTZ", "NETPROFIT_YOY", "PARENT_NETPROFIT_YOY")),
        "revenue": _num(_pick(row, "TOTALOPERATEREVE", "TOTAL_OPERATE_INCOME", "OPERATE_INCOME")),
        "revenueYoy": _num(_pick(row, "TOTALOPERATEREVETZ", "TOTAL_OPERATE_INCOME_YOY", "OPERATE_INCOME_YOY")),
        "grossMargin": _num(_pick(row, "GROSS_PROFIT_RATIO", "XSMLL")),
        "netMargin": _num(_pick(row, "NET_PROFIT_RATIO", "XSJLL")),
        "debtRatio": _num(_pick(row, "ASSET_LIAB_RATIO", "ZCFZL")),
        "operatingCashFlow": _num(_pick(row, "NETCASH_OPERATE", "OPERATE_NETCASH")),
        "cashFlowPerShare": _num(_pick(row, "MGJYXJJE", "PER_NETCASH")),
        "totalAssets": _num(_pick(row, "TOTAL_ASSETS", "TOTAL_ASSETS_PK")),
        "totalEquity": _num(_pick(row, "TOTAL_EQUITY", "TOTAL_EQUITY_PK")),
    }


def _extract_holder(row: dict) -> dict:
    return {
        "endDate": _clean_date(_pick(row, "END_DATE")),
        "rank": _num(_pick(row, "HOLDER_RANK")),
        "name": _pick(row, "HOLDER_NAME", "HOLDER_NAME_ABBR", "HOLD_NUM_ABBR"),
        "shares": _num(_pick(row, "HOLD_NUM", "HOLDER_NUM", "HOLDER_TOTAL_NUM", "TOTAL_NUM")),
        "ratio": _num(_pick(row, "HOLD_NUM_RATIO", "HOLD_RATIO", "FREE_HOLDNUM_RATIO")),
        "change": _pick(row, "HOLD_NUM_CHANGE", "HOLD_CHANGE", "HOLDER_STATE_NEW"),
        "changeRate": _num(_pick(row, "CHANGE_RATIO", "NEW_CHANGE_RATIO", "HOLDER_NUM_CHANGE_RATE")),
    }


def _extract_theme(row: dict) -> dict:
    return {
        "code": _pick(row, "BOARD_CODE", "BK_CODE"),
        "name": _pick(row, "BOARD_NAME", "SECURITY_NAME", "CONCEPT_NAME"),
        "type": _pick(row, "BOARD_TYPE", "BOARD_TYPE_NAME"),
    }


def _extract_business(row: dict) -> dict:
    return {
        "reportDate": _clean_date(_pick(row, "REPORT_DATE")),
        "summary": _pick(row, "BUSINESS_REVIEW", "BUSINESS_SCOPE", "MAIN_BUSINESS"),
        "futureExpect": _pick(row, "FUTURE_EXPECT"),
    }


def _extract_main_operation(row: dict) -> dict:
    return {
        "reportDate": _clean_date(_pick(row, "REPORT_DATE")),
        "name": _pick(row, "ITEM_NAME", "PRODUCT_NAME"),
        "income": _num(_pick(row, "MAIN_BUSINESS_INCOME", "TOTAL_BUSINESS_INCOME")),
        "incomeRatio": _num(_pick(row, "MBI_RATIO", "TOTAL_MBI_RATIO")),
        "grossMargin": _num(_pick(row, "GROSS_RPOFIT_RATIO", "GROSS_PROFIT_RATIO")),
        "profit": _num(_pick(row, "MAIN_BUSINESS_RPOFIT", "TOTAL_BUSINESS_RPOFIT")),
        "profitRatio": _num(_pick(row, "MBR_RATIO", "TOTAL_MBR_RATIO")),
    }


def _extract_management(row: dict) -> dict:
    return {
        "name": _pick(row, "PERSON_NAME", "NAME"),
        "position": _pick(row, "POSITION", "POSITION_NAME"),
        "incumbentDate": _clean_date(_pick(row, "INCUMBENT_DATE", "REPORT_DATE")),
        "education": _pick(row, "HIGH_DEGREE"),
        "age": _num(_pick(row, "AGE")),
        "resume": _pick(row, "RESUME"),
        "shares": _num(_pick(row, "HOLD_NUM", "IND_HOLD_NUM")),
        "salary": _num(_pick(row, "SALARY")),
    }


def _extract_dividend(row: dict) -> dict:
    return {
        "noticeDate": _clean_date(_pick(row, "NOTICE_DATE")),
        "reportDate": _clean_date(_pick(row, "REPORT_DATE")),
        "plan": _pick(row, "IMPL_PLAN_PROFILE", "NEW_PROFILE", "IMPL_PLAN_NEWPROFILE"),
        "progress": _pick(row, "ASSIGN_PROGRESS"),
        "totalDividend": _num(_pick(row, "TOTAL_DIVIDEND", "TOTAL_DIVIDEND_A")),
    }


def _sort_columns(report: str) -> str:
    if "HOLDER" in report or "HOLD" in report:
        return "END_DATE,HOLDER_RANK"
    if "DIVIDEND" in report:
        return "NOTICE_DATE"
    if "FINANCE" in report or "MAINFINADATA" in report or "MAINOP" in report or "BUSINESS" in report:
        return "REPORT_DATE"
    return ""


def _sort_types(report: str) -> str:
    if "HOLDER" in report or "HOLD" in report:
        return "-1,1"
    if _sort_columns(report):
        return "-1"
    return ""


def _pick(row: dict, *keys: str):
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-", "--"):
            return value
    return None


def _num(value: object) -> float | None:
    return _safe_float(value)


def _safe_float(value: object) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_date(value: object) -> str | None:
    if value in (None, "", "-", "--"):
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text


init_storage()
