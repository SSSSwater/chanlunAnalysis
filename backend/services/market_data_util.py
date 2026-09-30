"""Unified market-data access with provider chains suited to each consumer."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import time as datetime_time
from typing import Any, Callable

import pandas as pd

try:
    from .eastmoney_client import (
        get_intraday_kline as _eastmoney_intraday_kline,
        get_kline as _eastmoney_kline,
        get_stock_snapshot as _eastmoney_snapshot,
        list_a_stocks as _eastmoney_stocks,
        list_tencent_a_stocks as _tencent_stocks,
        get_tencent_kline as _tencent_kline_provider,
        normalize_symbol,
        secid,
        secucode,
    )
    from .price_action_contract import session_for_timestamp
    from .tushare_client import (
        get_tushare_kline,
        get_tushare_snapshot,
        is_configured as tushare_is_configured,
        list_tushare_daily_quotes,
        list_tushare_securities,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from services.eastmoney_client import (
        get_intraday_kline as _eastmoney_intraday_kline,
        get_kline as _eastmoney_kline,
        get_stock_snapshot as _eastmoney_snapshot,
        list_a_stocks as _eastmoney_stocks,
        list_tencent_a_stocks as _tencent_stocks,
        get_tencent_kline as _tencent_kline_provider,
        normalize_symbol,
        secid,
        secucode,
    )
    from services.price_action_contract import session_for_timestamp
    from services.tushare_client import (
        get_tushare_kline,
        get_tushare_snapshot,
        is_configured as tushare_is_configured,
        list_tushare_daily_quotes,
        list_tushare_securities,
    )


DAILY_PERIOD = "101"
STOCK_ADJUST = "1"
INDEX_ADJUST = "0"
CODE_COL = "代码"
NAME_COL = "名称"
LATEST_PRICE_COL = "最新价"
BEIJING_BOARD_PREFIXES = ("920",)


_akshare_module = None


def _load_akshare():
    """Load AkShare only when its fallback provider is actually selected.

    AkShare imports a large catalog of optional providers at module import
    time, including a native curl/OpenSSL extension. The backend's normal
    Tushare/Eastmoney/Tencent paths do not need it, so importing it eagerly
    needlessly increases startup cost and native-crash surface.
    """

    global _akshare_module
    if _akshare_module is None:
        import akshare

        _akshare_module = akshare
    return _akshare_module


class _LazyAkShare:
    def __getattr__(self, name: str):
        return getattr(_load_akshare(), name)


ak = _LazyAkShare()


@dataclass(frozen=True)
class MarketDataResult:
    """Result metadata shared by all provider-backed market-data methods."""

    items: list[dict]
    source: str
    errors: tuple[str, ...] = ()
    filtered_items: list[dict] | None = None

    @property
    def selected_items(self) -> list[dict]:
        return self.filtered_items if self.filtered_items is not None else self.items


class MarketDataError(RuntimeError):
    """Raised after every configured provider fails or returns unusable data."""

    def __init__(self, context: str, errors: list[str]):
        self.errors = tuple(errors)
        detail = "；".join(self.errors) or "无可用数据源"
        super().__init__(f"{context}：{detail}")


def list_securities(limit: int = 5000) -> list[dict]:
    """Return a security catalog using Tushare, Eastmoney, then AkShare."""

    providers: list[tuple[str, str, Callable[[], list[dict]]]] = []
    if tushare_is_configured():
        providers.append(("tushare", "Tushare", lambda: list_tushare_securities(limit=limit)))
    providers.extend(
        (
            ("eastmoney", "东方财富", lambda: _eastmoney_stocks(page_size=limit)),
            ("akshare", "AkShare", lambda: _akshare_security_items()),
        )
    )
    return _first_list("证券列表获取失败", providers).items


def list_non_tushare_securities(limit: int = 6000) -> MarketDataResult:
    """Return the searchable A-share catalog without using Tushare.

    Stock names shown in the UI must come from the public quote/catalog
    providers, because the Tushare security master can lag renames and has
    occasionally returned symbols in its display-name field.
    """

    providers: list[tuple[str, str, Callable[[], list[dict]]]] = [
        ("eastmoney", "东方财富", lambda: _eastmoney_stocks(page_size=limit)),
        ("akshare", "AkShare", lambda: _akshare_security_items()),
    ]
    return _first_list("证券列表获取失败", providers)


def list_market_quotes(
    *,
    page_size: int = 5000,
    minimum_count: int = 0,
    tencent_securities: list[dict] | None = None,
) -> MarketDataResult:
    """Fetch a full-market quote snapshot in a single, shared provider chain."""

    providers: list[tuple[str, str, Callable[[], list[dict]]]] = []
    if tushare_is_configured():
        providers.append(
            ("tushare", "Tushare", lambda: list_tushare_daily_quotes(page_size=page_size))
        )
    providers.append(("eastmoney", "东方财富", lambda: _eastmoney_stocks(page_size=page_size)))
    if tencent_securities:
        providers.append(
            (
                "tencent",
                "腾讯",
                lambda: _tencent_stocks(tencent_securities),
            )
        )
    providers.append(("akshare", "AkShare", lambda: _akshare_market_items()))
    return _first_list("全市场行情获取失败", providers, minimum_count=minimum_count)


def list_non_tushare_market_quotes(
    *,
    page_size: int = 6000,
    minimum_count: int = 0,
    tencent_securities: list[dict] | None = None,
) -> MarketDataResult:
    """Fetch the home-page market snapshot from non-Tushare providers only."""

    providers: list[tuple[str, str, Callable[[], list[dict]]]] = [
        ("eastmoney", "东方财富", lambda: _eastmoney_stocks(page_size=page_size)),
    ]
    if tencent_securities:
        providers.append(
            (
                "tencent",
                "腾讯",
                lambda: _tencent_stocks(tencent_securities),
            )
        )
    providers.append(("akshare", "AkShare", lambda: _akshare_market_items()))
    return _first_list("全市场行情获取失败", providers, minimum_count=minimum_count)


def list_price_limited_stocks(
    min_price: float,
    max_price: float,
    *,
    tencent_securities: list[dict] | None = None,
) -> list[dict]:
    """Return non-ST stocks in a price range from the unified quote snapshot."""

    if max_price <= 0 or min_price > max_price:
        return []
    try:
        items = list_market_quotes(page_size=6000, tencent_securities=tencent_securities).items
    except MarketDataError:
        return []

    result: list[dict] = []
    for item in items:
        symbol = normalize_symbol(item.get("symbol"))
        name = str(item.get("name") or "").strip()
        price = _safe_float(item.get("latestPrice"))
        if (
            not re.fullmatch(r"\d{6}", symbol or "")
            or not _is_supported_stock_symbol(symbol)
            or "ST" in name.upper()
            or price is None
            or price <= 0
            or not min_price <= price <= max_price
        ):
            continue
        result.append(
            {
                "symbol": symbol,
                "name": name or symbol,
                "label": f"{symbol} - {name or symbol}",
                "latestPrice": round(price, 3),
            }
        )
    return result


def get_snapshot(symbol: str) -> dict:
    """Fetch one stock snapshot using Tushare first and provider fallbacks."""

    normalized = normalize_symbol(symbol)
    providers: list[tuple[str, str, Callable[[], dict]]] = []
    if tushare_is_configured():
        providers.append(("tushare", "Tushare", lambda: get_tushare_snapshot(normalized)))
    providers.extend(
        (
            ("eastmoney", "东方财富", lambda: _eastmoney_snapshot(normalized)),
            ("tencent", "腾讯", lambda: _tencent_snapshot(normalized)),
            ("akshare", "AkShare", lambda: _akshare_snapshot(normalized)),
        )
    )
    errors: list[str] = []
    for source, label, fetcher in providers:
        try:
            item = fetcher()
            if isinstance(item, dict) and item:
                return item
            errors.append(f"{label}无返回")
        except Exception as exc:
            errors.append(f"{label}：{exc}")
    raise MarketDataError(f"股票快照获取失败：{normalized}", errors)


def get_daily_kline(
    symbol: str,
    start_date: str = "",
    end_date: str = "",
    *,
    limit: int = 260,
    adjust: str = STOCK_ADJUST,
    is_index: bool = False,
    minimum_bars: int = 0,
    require_turnover: bool = False,
) -> MarketDataResult:
    """Fetch daily bars through the shared Tushare-first provider chain."""

    return _get_kline(
        symbol,
        period=DAILY_PERIOD,
        adjust=adjust,
        limit=limit,
        is_index=is_index,
        start_date=start_date,
        end_date=end_date,
        minimum_bars=minimum_bars,
        require_turnover=require_turnover,
    )


def get_intraday_kline(
    symbol: str,
    period: str,
    start_date: str = "",
    end_date: str = "",
    *,
    limit: int = 220,
    adjust: str = STOCK_ADJUST,
    is_index: bool = False,
) -> MarketDataResult:
    """Fetch minute bars through the shared Tushare-first provider chain."""

    return _get_kline(
        symbol,
        period=period,
        adjust=adjust,
        limit=limit,
        is_index=is_index,
        start_date=start_date,
        end_date=end_date,
    )


def _get_kline(
    symbol: str,
    *,
    period: str,
    adjust: str,
    limit: int,
    is_index: bool,
    start_date: str,
    end_date: str,
    minimum_bars: int = 0,
    require_turnover: bool = False,
) -> MarketDataResult:
    normalized = normalize_symbol(symbol)
    providers: list[tuple[str, str, Callable[[], list[dict]]]] = []
    if tushare_is_configured():
        providers.append(
            (
                "tushare",
                "Tushare",
                lambda: get_tushare_kline(
                    normalized,
                    period=period,
                    adjust=adjust,
                    limit=limit,
                    is_index=is_index,
                    start_date=start_date,
                    end_date=end_date,
                ),
            )
        )

    # Keep the existing fast Tencent stock path while centralizing the
    # provider order for every caller of this utility.
    if str(period) == DAILY_PERIOD and not is_index:
        providers.append(
            (
                "tencent",
                "腾讯",
                lambda: _tencent_kline(normalized, period=period, adjust=adjust, limit=limit, is_index=False),
            )
        )
    providers.append(
        (
            "eastmoney",
            "东方财富",
            lambda: (
                _eastmoney_kline(normalized, period=period, adjust=adjust, limit=limit, is_index=is_index)
                if str(period) == DAILY_PERIOD
                else _eastmoney_intraday_kline(normalized, period=period, limit=limit, is_index=is_index)
            ),
        )
    )
    if str(period) == DAILY_PERIOD and is_index:
        providers.append(
            (
                "tencent",
                "腾讯",
                lambda: _tencent_kline(normalized, period=period, adjust=adjust, limit=limit, is_index=True),
            )
        )
    providers.append(
        (
            "akshare",
            "AkShare",
            lambda: _akshare_kline(
                normalized,
                period=period,
                start_date=start_date,
                end_date=end_date,
                is_index=is_index,
            ),
        )
    )
    return _first_history(
        f"{'指数' if is_index else '股票'}{('分钟' if str(period) != DAILY_PERIOD else '日')}K数据获取失败",
        providers,
        start_date=start_date,
        end_date=end_date,
        minimum_bars=minimum_bars,
        require_turnover=require_turnover,
    )


def _first_list(
    context: str,
    providers: list[tuple[str, str, Callable[[], list[dict]]]],
    *,
    minimum_count: int = 0,
) -> MarketDataResult:
    errors: list[str] = []
    for source, label, fetcher in providers:
        try:
            items = fetcher()
            if not items:
                errors.append(f"{label}无返回")
                continue
            if len(items) < max(0, int(minimum_count or 0)):
                errors.append(f"{label}数据量不足：{len(items)}/{minimum_count}")
                continue
            return MarketDataResult(items=items, source=source, errors=tuple(errors))
        except Exception as exc:
            errors.append(f"{label}：{exc}")
    raise MarketDataError(context, errors)


def _first_history(
    context: str,
    providers: list[tuple[str, str, Callable[[], list[dict]]]],
    *,
    start_date: str,
    end_date: str,
    minimum_bars: int = 0,
    require_turnover: bool = False,
) -> MarketDataResult:
    errors: list[str] = []
    required = max(0, int(minimum_bars or 0))
    for source, label, fetcher in providers:
        try:
            items = fetcher()
            filtered = _filter_items_by_date(items, start_date, end_date)
            if filtered and len(filtered) >= required:
                if require_turnover and any(
                    _turnover_value(item) is None
                    for item in filtered
                ):
                    errors.append(f"{label}K线缺少历史换手率")
                    continue
                return MarketDataResult(
                    items=items,
                    source=source,
                    errors=tuple(errors),
                    filtered_items=filtered,
                )
            errors.append(f"{label}K线数量不足")
        except Exception as exc:
            errors.append(f"{label}：{exc}")
    raise MarketDataError(context, errors)


def _tencent_kline(symbol: str, **kwargs: object) -> list[dict]:
    return _tencent_kline_provider(symbol, **kwargs)


def _tencent_snapshot(symbol: str) -> dict:
    items = _tencent_stocks([{"symbol": symbol, "name": symbol}])
    if not items:
        raise RuntimeError("腾讯快照无返回")
    return items[0]


def _akshare_security_items() -> list[dict]:
    items = _akshare_market_items()
    if not items:
        raise RuntimeError("AkShare 股票列表无返回")
    return items


def _akshare_market_items() -> list[dict]:
    return _ak_spot_to_items(_stock_spot_full_ak())


def _akshare_snapshot(symbol: str) -> dict:
    for item in _akshare_market_items():
        if item.get("symbol") == symbol:
            return item
    raise RuntimeError(f"AkShare 快照无返回：{symbol}")


def _akshare_kline(
    symbol: str,
    *,
    period: str,
    start_date: str,
    end_date: str,
    is_index: bool,
) -> list[dict]:
    if str(period) == DAILY_PERIOD:
        frame = _fetch_index_history_ak(symbol, start_date, end_date) if is_index else _fetch_history_ak(symbol, start_date, end_date)
    else:
        frame = (
            _fetch_index_intraday_history_ak(symbol, period, start_date, end_date)
            if is_index
            else _fetch_intraday_history_ak(symbol, period, start_date, end_date)
        )
    return _history_to_items(frame)


def _stock_spot_full_ak() -> pd.DataFrame:
    fetchers = [ak.stock_zh_a_spot_em, ak.stock_zh_a_spot]
    for fetcher in fetchers:
        try:
            frame = fetcher().dropna(subset=[CODE_COL, NAME_COL]).drop_duplicates(CODE_COL)
            if LATEST_PRICE_COL in frame.columns:
                return frame
        except Exception:
            continue
    frame = ak.stock_info_a_code_name()
    return frame.rename(columns={"code": CODE_COL, "name": NAME_COL})[[CODE_COL, NAME_COL]].dropna().drop_duplicates(CODE_COL)


def _ak_spot_to_items(frame: pd.DataFrame) -> list[dict]:
    if frame.empty or CODE_COL not in frame.columns or NAME_COL not in frame.columns:
        return []
    items: list[dict] = []
    for _, row in frame.iterrows():
        symbol = normalize_symbol(row.get(CODE_COL))
        name = str(row.get(NAME_COL) or "").strip()
        if not re.fullmatch(r"\d{6}", symbol or "") or not name or not _is_supported_stock_symbol(symbol):
            continue
        items.append(
            {
                "symbol": symbol,
                "name": name,
                "market": secid(symbol).split(".")[0],
                "secid": secid(symbol),
                "secucode": secucode(symbol),
                "isSt": "ST" in name.upper(),
                "latestPrice": _safe_float(row.get(LATEST_PRICE_COL)),
                "pctChange": _safe_float(row.get("涨跌幅")),
                "priceChange": _safe_float(row.get("涨跌额")),
                "turnoverRate": _safe_float(row.get("换手率")),
                "peDynamic": _safe_float(row.get("市盈率-动态")),
                "pb": _safe_float(row.get("市净率")),
                "totalMarketCap": _safe_float(row.get("总市值")),
                "source": "akshare",
                "provider": "akshare",
            }
        )
    return items


def _fetch_history_ak(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_a_hist_tx(symbol=_market_symbol(symbol), start_date=start_date, end_date=end_date, adjust="qfq", timeout=8),
        lambda: ak.stock_zh_a_daily(symbol=_market_symbol(symbol), start_date=start_date, end_date=end_date, adjust="qfq"),
        lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq", timeout=4),
    ]
    for fetcher in fetchers:
        frame, error = _fetch_with_retries(fetcher)
        if error:
            errors.append(error)
        if frame is not None and not frame.empty:
            return frame
    raise RuntimeError("；".join(errors[-2:]) or "数据源无返回")


def _fetch_intraday_history_ak(symbol: str, period: str, start_date: str, end_date: str) -> pd.DataFrame:
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_a_minute(symbol=_market_symbol(symbol), period=period, adjust="qfq"),
        lambda: ak.stock_zh_a_hist_min_em(symbol=symbol, period=period, start_date=_minute_datetime(start_date), end_date=_minute_datetime(end_date, end_of_day=True), adjust="qfq"),
    ]
    for fetcher in fetchers:
        frame, error = _fetch_with_retries(fetcher, attempts=1)
        if error:
            errors.append(error)
        if frame is not None and not frame.empty:
            return frame
    raise RuntimeError("；".join(errors[-2:]) or "数据源无返回")


def _fetch_index_history_ak(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    normalized = normalize_symbol(symbol)
    market_symbol = f"sh{normalized}" if normalized.startswith(("0", "5", "6")) else f"sz{normalized}"
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_index_daily_tx(symbol=market_symbol, start_date=start_date, end_date=end_date),
        lambda: ak.stock_zh_index_daily(symbol=market_symbol),
        lambda: ak.index_zh_a_hist(symbol=normalized, period="daily", start_date=start_date, end_date=end_date),
    ]
    for fetcher in fetchers:
        frame, error = _fetch_with_retries(fetcher)
        if error:
            errors.append(error)
        if frame is not None and not frame.empty:
            return frame
    raise RuntimeError("；".join(errors[-2:]) or "数据源无返回")


def _fetch_index_intraday_history_ak(symbol: str, period: str, start_date: str, end_date: str) -> pd.DataFrame:
    normalized = normalize_symbol(symbol)
    market_symbol = f"sh{normalized}" if normalized.startswith(("0", "5", "6")) else f"sz{normalized}"
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_a_minute(symbol=market_symbol, period=period, adjust=""),
        lambda: ak.index_zh_a_hist_min_em(symbol=normalized, period=period, start_date=_minute_datetime(start_date), end_date=_minute_datetime(end_date, end_of_day=True)),
    ]
    for fetcher in fetchers:
        frame, error = _fetch_with_retries(fetcher, attempts=1)
        if error:
            errors.append(error)
        if frame is not None and not frame.empty:
            return frame
    raise RuntimeError("；".join(errors[-2:]) or "数据源无返回")


def _history_to_items(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    normalized = _normalize_history_columns(frame)
    normalized["_date_filter"] = pd.to_datetime(normalized["date"])
    normalized = normalized.sort_values("_date_filter").reset_index(drop=True)
    fields = ["date", "open", "high", "low", "close", "volume", "amount", "pct_change", "turnover_rate"]
    items: list[dict] = []
    for _, row in normalized[fields].iterrows():
        items.append(
            {
                "date": str(row["date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]) if pd.notna(row["amount"]) else 0.0,
                "pctChange": float(row["pct_change"]) if pd.notna(row["pct_change"]) else 0.0,
                "turnoverRate": float(row["turnover_rate"]) if pd.notna(row["turnover_rate"]) else None,
                "amountUnit": "CNY",
                "volumeUnit": "shares",
                "session": session_for_timestamp(str(row["date"]), timeframe="1d" if " " not in str(row["date"]) else "15m"),
                "source": "akshare",
                "provider": "akshare",
                "completed": True,
                "timestampQuality": "VALID",
                "adjustmentVersion": "provider-default",
            }
        )
    return items


def _normalize_history_columns(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.rename(
        columns={
            "日期": "date",
            "时间": "date",
            "day": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_change",
            "换手率": "turnover_rate",
        }
    ).copy()
    normalized = normalized.rename(columns={"datetime": "date", "pctChange": "pct_change", "turnoverRate": "turnover_rate"})
    numeric_fields = ["open", "high", "low", "close", "volume", "amount", "pct_change", "turnover_rate"]
    if "volume" not in normalized.columns and "amount" in normalized.columns:
        normalized["volume"] = normalized["amount"]
    if "amount" not in normalized.columns:
        normalized["amount"] = 0
    if "turnover_rate" not in normalized.columns:
        normalized["turnover_rate"] = None
    for field in numeric_fields:
        if field in normalized.columns:
            normalized[field] = pd.to_numeric(normalized[field], errors="coerce")
    if "pct_change" not in normalized.columns or normalized["pct_change"].isna().all():
        normalized["pct_change"] = normalized["close"].pct_change().fillna(0) * 100
    else:
        normalized["pct_change"] = normalized["pct_change"].fillna(0)
    date_values = pd.to_datetime(normalized["date"])
    has_intraday_time = (date_values.dt.time != datetime_time(0, 0)).any()
    normalized["date"] = date_values.dt.strftime("%Y-%m-%d %H:%M" if has_intraday_time else "%Y-%m-%d")
    return normalized.dropna(subset=["date", "open", "high", "low", "close"])


def _filter_items_by_date(items: list[dict], start_date: str, end_date: str) -> list[dict]:
    if not start_date and not end_date:
        return items
    start_key = _date_filter_key(start_date) if start_date else ""
    end_key = _date_filter_key(end_date, end=True) if end_date else "9999-99-99 99:99"
    return [item for item in items if start_key <= str(item.get("date", "")) <= end_key]


def _date_filter_key(value: str, end: bool = False) -> str:
    text = str(value)
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}{' 23:59' if end else ''}"
    return text


def _market_symbol(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{normalized}"


def _is_supported_stock_symbol(symbol: str) -> bool:
    return str(symbol).startswith(("000", "001", "002", "003", "300", "301", "302", "600", "601", "603", "605", "688", "689"))


def is_beijing_board_symbol(symbol: object) -> bool:
    """Return whether a symbol belongs to the Beijing board excluded from rankings."""

    normalized = normalize_symbol(symbol)
    return bool(re.fullmatch(r"\d{6}", normalized or "") and normalized.startswith(BEIJING_BOARD_PREFIXES))


def _fetch_with_retries(fetcher: Callable[[], pd.DataFrame], attempts: int = 2) -> tuple[pd.DataFrame | None, str | None]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return fetcher(), None
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.8 * (attempt + 1))
    return None, str(last_error) if last_error else "未知错误"


def _safe_float(value: object) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        number = float(value)
        return None if pd.isna(number) else number
    except (TypeError, ValueError):
        return None


def _turnover_value(item: dict) -> float | None:
    for field in ("turnoverRate", "turnover_rate", "providerTurnover"):
        value = _safe_float(item.get(field))
        if value is not None:
            return value
    return None
