"""Tushare data provider adapter.

The application stores volumes in shares and amounts in CNY. Tushare returns
volume in hands and amount in thousand CNY, so this module normalizes those
units before returning the shared application contract.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

import pandas as pd
from dotenv import load_dotenv

try:
    import tushare as ts
except ImportError:  # pragma: no cover - exercised when the optional package is absent
    ts = None

try:
    from .price_action_contract import session_for_timestamp
except ImportError:  # pragma: no cover - direct module execution compatibility
    from services.price_action_contract import session_for_timestamp


TUSHARE_TOKEN_ENV = "TUSHARE_TOKEN"
TUSHARE_HTTP_URL_ENV = "TUSHARE_HTTP_URL"
DEFAULT_TUSHARE_HTTP_URL = "https://jiaoch.top/"
TUSHARE_PROVIDER = "tushare"

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env", override=False)

_PRO_LOCK = RLock()
_PRO: Any | None = None
_PRO_CACHE_KEY: tuple[str, str] | None = None


def is_configured() -> bool:
    """Return whether the provider has a token without exposing its value."""

    return bool(os.environ.get(TUSHARE_TOKEN_ENV, "").strip())


def get_pro() -> Any:
    """Create and cache a Tushare client using the configured mirror endpoint."""

    if ts is None:
        raise RuntimeError("Tushare 未安装，请先执行 pip install -r backend\\requirements.txt")

    token = os.environ.get(TUSHARE_TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"未配置 {TUSHARE_TOKEN_ENV}，Tushare 数据源不可用")

    http_url = os.environ.get(TUSHARE_HTTP_URL_ENV, DEFAULT_TUSHARE_HTTP_URL).strip()
    http_url = (http_url or DEFAULT_TUSHARE_HTTP_URL).rstrip("/") + "/"
    cache_key = (token, http_url)
    global _PRO, _PRO_CACHE_KEY
    with _PRO_LOCK:
        if _PRO is None or _PRO_CACHE_KEY != cache_key:
            pro = ts.pro_api(token)
            # Tushare's DataApi keeps the endpoint in this private attribute.
            # The custom mirror documented for this release relies on it.
            pro._DataApi__http_url = http_url
            _PRO = pro
            _PRO_CACHE_KEY = cache_key
        return _PRO


def reset_client() -> None:
    """Reset the cached client for tests or runtime configuration changes."""

    global _PRO, _PRO_CACHE_KEY
    with _PRO_LOCK:
        _PRO = None
        _PRO_CACHE_KEY = None


def normalize_symbol(symbol: object) -> str:
    text = str(symbol or "").strip().upper()
    digits = re.sub(r"\D", "", text)
    return digits[-6:] if len(digits) >= 6 else digits


def to_ts_code(symbol: object, *, is_index: bool = False) -> str:
    normalized = normalize_symbol(symbol)
    if not re.fullmatch(r"\d{6}", normalized or ""):
        raise ValueError(f"无效证券代码：{symbol}")
    if is_index:
        suffix = "SZ" if normalized.startswith("399") else "SH"
    else:
        suffix = "SH" if normalized.startswith(("5", "6", "9")) else "SZ"
    return f"{normalized}.{suffix}"


def list_tushare_securities(limit: int = 5000) -> list[dict]:
    """Return the listed A-share security catalog from ``stock_basic``."""

    pro = get_pro()
    kwargs = {
        "exchange": "",
        "list_status": "L",
        "fields": "ts_code,symbol,name,market,exchange,list_date",
        "limit": max(1, min(int(limit or 5000), 5000)),
    }
    frame = _call_frame(pro, "stock_basic", **kwargs)
    if frame.empty:
        raise RuntimeError("Tushare 股票基础信息无返回")

    items: list[dict] = []
    for row in frame.to_dict(orient="records"):
        symbol = normalize_symbol(row.get("ts_code") or row.get("symbol"))
        name = _security_name(row.get("name"), symbol)
        if not re.fullmatch(r"\d{6}", symbol or "") or not name or not _is_a_share(symbol):
            continue
        suffix = _suffix_for_symbol(symbol)
        market = "1" if suffix == "SH" else "0"
        items.append(
            {
                "symbol": symbol,
                "name": name,
                "market": market,
                "secid": f"{market}.{symbol}",
                "secucode": f"{symbol}.{suffix}",
                "isSt": "ST" in name.upper(),
                "latestPrice": None,
                "pctChange": None,
                "turnoverRate": None,
                "peDynamic": None,
                "pb": None,
                "totalMarketCap": None,
            }
        )
    if not items:
        raise RuntimeError("Tushare 股票基础信息无有效 A 股")
    return items


def list_tushare_daily_quotes(page_size: int = 5000) -> list[dict]:
    """Return the latest available full-market daily quote snapshot."""

    pro = get_pro()
    trade_date = _latest_trade_date(pro)
    daily = _call_frame(
        pro,
        "daily",
        trade_date=trade_date,
        fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        limit=max(1, min(int(page_size or 5000), 5000)),
    )
    if daily.empty:
        raise RuntimeError(f"Tushare 日行情无返回：{trade_date}")

    basic = _optional_frame(
        pro,
        "daily_basic",
        trade_date=trade_date,
        fields="ts_code,turnover_rate,pe,pe_ttm,pb,total_mv,circ_mv",
        limit=5000,
    )
    names = _optional_frame(
        pro,
        "stock_basic",
        exchange="",
        list_status="L",
        fields="ts_code,symbol,name",
        limit=5000,
    )
    frame = _merge_frames(daily, basic, names)
    items = _daily_frame_to_items(frame, is_index=False)
    if not items:
        raise RuntimeError(f"Tushare 日行情无有效 A 股：{trade_date}")
    return items


def get_tushare_snapshot(symbol: str) -> dict:
    """Return the latest available daily snapshot for one stock."""

    normalized = normalize_symbol(symbol)
    ts_code = to_ts_code(normalized)
    pro = get_pro()
    trade_date = _latest_trade_date(pro)
    daily = _call_frame(
        pro,
        "daily",
        ts_code=ts_code,
        trade_date=trade_date,
        fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        limit=2,
    )
    if daily.empty:
        raise RuntimeError(f"Tushare 股票快照无返回：{normalized}")
    basic = _optional_frame(
        pro,
        "daily_basic",
        ts_code=ts_code,
        trade_date=trade_date,
        fields="ts_code,turnover_rate,pe,pe_ttm,pb,total_mv,circ_mv",
        limit=2,
    )
    names = _optional_frame(
        pro,
        "stock_basic",
        ts_code=ts_code,
        fields="ts_code,symbol,name",
        limit=2,
    )
    items = _daily_frame_to_items(_merge_frames(daily, basic, names), is_index=False)
    if not items:
        raise RuntimeError(f"Tushare 股票快照无有效数据：{normalized}")
    return items[-1]


def get_tushare_kline(
    symbol: str,
    period: str = "101",
    adjust: str = "1",
    limit: int = 260,
    *,
    is_index: bool = False,
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """Fetch daily or intraday bars through the documented ``pro_bar`` API."""

    period_key = str(period)
    frequencies = {"101": "D", "D": "D", "1d": "D", "30": "30min", "15": "15min", "5": "5min"}
    frequency = frequencies.get(period_key)
    if not frequency:
        raise RuntimeError(f"Tushare 不支持的K线周期：{period}")

    pro = get_pro()
    kwargs: dict[str, object] = {
        "api": pro,
        "ts_code": to_ts_code(symbol, is_index=is_index),
        "freq": frequency,
        "asset": "I" if is_index else "E",
        "limit": max(1, min(int(limit or 260), 2000)),
    }
    if not is_index:
        if str(adjust) == "1":
            kwargs["adj"] = "qfq"
        elif str(adjust) == "2":
            kwargs["adj"] = "hfq"
    if start_date:
        kwargs["start_date"] = _compact_date(start_date)
    if end_date:
        kwargs["end_date"] = _compact_date(end_date)

    if ts is None:  # keep static type checkers and defensive callers explicit
        raise RuntimeError("Tushare 未安装")
    frame = ts.pro_bar(**kwargs)
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise RuntimeError("Tushare K线无返回")
    items = _bar_frame_to_items(frame, is_index=is_index, adjust=adjust)
    if not items:
        raise RuntimeError("Tushare K线无有效数据")
    if not is_index and frequency == "D":
        _enrich_daily_turnover(
            items,
            pro,
            ts_code=str(kwargs["ts_code"]),
            start_date=str(kwargs.get("start_date") or items[0].get("date") or ""),
            end_date=str(kwargs.get("end_date") or items[-1].get("date") or ""),
        )
    return items


def _latest_trade_date(pro: Any, as_of: date | None = None) -> str:
    today = as_of or datetime.now().date()
    today_text = today.strftime("%Y%m%d")
    start = today - timedelta(days=30)
    calendar_dates: list[str] = []
    try:
        calendar = _call_frame(
            pro,
            "trade_cal",
            exchange="",
            start_date=start.strftime("%Y%m%d"),
            end_date=today.strftime("%Y%m%d"),
            is_open=1,
            fields="cal_date,is_open",
            limit=40,
        )
        dates = [_compact_date(value) for value in calendar.get("cal_date", [])]
        calendar_dates = sorted({value for value in dates if value and value <= today_text}, reverse=True)
    except Exception:
        pass

    # An open calendar date can still have no daily rows while the market is
    # open or while the provider is publishing the completed day's data. Probe
    # the rows themselves before accepting the calendar date.
    recent_dates = [
        (today - timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(0, 31)
    ]
    candidates = list(dict.fromkeys([*calendar_dates, *recent_dates]))
    for candidate_text in candidates:
        try:
            frame = _call_frame(
                pro,
                "daily",
                trade_date=candidate_text,
                fields="ts_code,trade_date",
                limit=1,
            )
            if not frame.empty:
                return candidate_text
        except Exception:
            continue
    return calendar_dates[0] if calendar_dates else today_text


def _call_frame(pro: Any, method: str, **kwargs: object) -> pd.DataFrame:
    result = getattr(pro, method)(**kwargs)
    if result is None:
        return pd.DataFrame()
    if not isinstance(result, pd.DataFrame):
        raise RuntimeError(f"Tushare {method} 返回格式错误")
    return result.copy()


def _optional_frame(pro: Any, method: str, **kwargs: object) -> pd.DataFrame:
    try:
        return _call_frame(pro, method, **kwargs)
    except Exception:
        return pd.DataFrame()


def _merge_frames(base: pd.DataFrame, *others: pd.DataFrame) -> pd.DataFrame:
    result = base.copy()
    for other in others:
        if other.empty or "ts_code" not in other.columns:
            continue
        columns = [column for column in other.columns if column != "ts_code" and column not in result.columns]
        if columns:
            result = result.merge(other[["ts_code", *columns]], on="ts_code", how="left")
    return result


def _daily_frame_to_items(frame: pd.DataFrame, *, is_index: bool) -> list[dict]:
    if frame.empty:
        return []
    return _quote_frame_to_items(frame, is_index=is_index)


def _quote_frame_to_items(frame: pd.DataFrame, *, is_index: bool) -> list[dict]:
    items: list[dict] = []
    rows = frame.sort_values("trade_date" if "trade_date" in frame.columns else "ts_code").to_dict(orient="records")
    for row in rows:
        symbol = normalize_symbol(row.get("ts_code") or row.get("symbol"))
        close = _number(row.get("close"))
        if not re.fullmatch(r"\d{6}", symbol or "") or close is None:
            continue
        suffix = _suffix_for_symbol(symbol, is_index=is_index, ts_code=row.get("ts_code"))
        market = "1" if suffix == "SH" else "0"
        name = _security_name(row.get("name"), symbol)
        pre_close = _number(row.get("pre_close"))
        price_change = _number(row.get("change"))
        if price_change is None and pre_close is not None:
            price_change = close - pre_close
        pct_change = _number(row.get("pct_chg"))
        if pct_change is None and pre_close:
            pct_change = price_change / pre_close * 100 if price_change is not None else None
        items.append(
            {
                "symbol": symbol,
                "name": name,
                "market": market,
                "secid": f"{market}.{symbol}",
                "secucode": f"{symbol}.{suffix}",
                "isSt": "ST" in name.upper(),
                "latestPrice": close,
                "priceChange": price_change,
                "pctChange": pct_change,
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "previousClose": pre_close,
                "volume": _tushare_volume(row.get("vol")),
                "amount": _tushare_amount(row.get("amount")),
                "turnoverRate": _number(row.get("turnover_rate")),
                "peDynamic": _number(row.get("pe")),
                "peTtm": _number(row.get("pe_ttm")),
                "pb": _number(row.get("pb")),
                "totalMarketCap": _tushare_market_value(row.get("total_mv")),
                "circulatingMarketCap": _tushare_market_value(row.get("circ_mv")),
                "quoteDate": _format_timestamp(row.get("trade_date"), daily=True),
                "source": TUSHARE_PROVIDER,
                "provider": TUSHARE_PROVIDER,
            }
        )
    return items


def _security_name(value: object, fallback: str) -> str:
    """Return a display name without leaking missing-value sentinels."""

    if value is None:
        return fallback
    try:
        if bool(pd.isna(value)):
            return fallback
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return fallback if not text or text.lower() in {"nan", "nat", "none"} else text


def _bar_frame_to_items(frame: pd.DataFrame, *, is_index: bool, adjust: str) -> list[dict]:
    normalized = frame.rename(
        columns={
            "trade_time": "bar_time",
            "datetime": "bar_time",
            "date": "trade_date",
            "vol": "volume_raw",
            "pct_chg": "pct_change",
            "pre_close": "previous_close",
        }
    ).copy()
    date_column = "bar_time" if "bar_time" in normalized.columns else "trade_date"
    if date_column not in normalized.columns:
        raise RuntimeError("Tushare K线缺少时间字段")
    normalized["_bar_datetime"] = normalized[date_column].map(_parse_timestamp)
    normalized = normalized.dropna(subset=["_bar_datetime"]).sort_values("_bar_datetime")
    items: list[dict] = []
    previous_close: float | None = None
    adjustment = "qfq" if str(adjust) == "1" and not is_index else "hfq" if str(adjust) == "2" and not is_index else "raw"
    for row in normalized.to_dict(orient="records"):
        parsed = row.get("_bar_datetime")
        close = _number(row.get("close"))
        open_price = _number(row.get("open"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        if parsed is None or None in (close, open_price, high, low):
            continue
        previous = _number(row.get("previous_close")) or previous_close
        price_change = _number(row.get("change"))
        if price_change is None and previous is not None:
            price_change = close - previous
        pct_change = _number(row.get("pct_change"))
        if pct_change is None and previous:
            pct_change = price_change / previous * 100 if price_change is not None else None
        symbol = normalize_symbol(row.get("ts_code"))
        suffix = _suffix_for_symbol(symbol, is_index=is_index, ts_code=row.get("ts_code"))
        market = "1" if suffix == "SH" else "0"
        date_text = parsed.strftime("%Y-%m-%d %H:%M" if parsed.time() != datetime.min.time() else "%Y-%m-%d")
        items.append(
            {
                "date": date_text,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": _tushare_volume(row.get("volume_raw")),
                "amount": _tushare_amount(row.get("amount")),
                "pctChange": pct_change,
                "priceChange": price_change,
                "turnoverRate": _number(row.get("turnover_rate")),
                "amountUnit": "CNY",
                "volumeUnit": "shares",
                "session": session_for_timestamp(date_text, timeframe="1d" if " " not in date_text else "15m"),
                "source": TUSHARE_PROVIDER,
                "provider": TUSHARE_PROVIDER,
                "completed": True,
                "timestampQuality": "VALID",
                "adjustmentVersion": adjustment,
                "market": market,
                "secid": f"{market}.{symbol}" if symbol else None,
                "secucode": f"{symbol}.{suffix}" if symbol else None,
            }
        )
        previous_close = close
    return items


def _enrich_daily_turnover(
    items: list[dict],
    pro: Any,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> None:
    """Add historical daily turnover rates when the bar endpoint omits them."""

    if not items:
        return
    basic = pd.DataFrame()
    start_key = _compact_date(start_date)
    end_key = _compact_date(end_date)
    for attempt in range(2):
        try:
            basic = _call_frame(
                pro,
                "daily_basic",
                ts_code=ts_code,
                start_date=start_key,
                end_date=end_key,
                fields="ts_code,trade_date,turnover_rate",
                limit=2000,
            )
            if not basic.empty:
                break
        except Exception:
            basic = pd.DataFrame()
        if attempt == 0:
            time.sleep(0.8)
    if basic.empty:
        return
    turnover_by_date = {
        _format_timestamp(row.get("trade_date"), daily=True): _number(row.get("turnover_rate"))
        for row in basic.to_dict(orient="records")
    }
    for item in items:
        if _number(item.get("turnoverRate")) is None:
            item["turnoverRate"] = turnover_by_date.get(str(item.get("date") or "")[:10])


def _suffix_for_symbol(symbol: str, *, is_index: bool = False, ts_code: object = None) -> str:
    text = str(ts_code or "").upper()
    if text.endswith(".SH"):
        return "SH"
    if text.endswith(".SZ"):
        return "SZ"
    if is_index:
        return "SZ" if symbol.startswith("399") else "SH"
    return "SH" if symbol.startswith(("5", "6", "9")) else "SZ"


def _is_a_share(symbol: str) -> bool:
    return symbol.startswith(("000", "001", "002", "003", "300", "301", "302", "600", "601", "603", "605", "688", "689"))


def _compact_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return digits[:8]
    try:
        return pd.to_datetime(value).strftime("%Y%m%d")
    except Exception:
        return ""


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d")
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def _format_timestamp(value: object, *, daily: bool = False) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return str(value or "")
    return parsed.strftime("%Y-%m-%d" if daily else "%Y-%m-%d %H:%M")


def _number(value: object) -> float | None:
    try:
        if value is None or value == "" or (isinstance(value, str) and value.strip() in {"-", "--"}):
            return None
        number = float(value)
        return None if pd.isna(number) else number
    except (TypeError, ValueError):
        return None


def _tushare_volume(value: object) -> float:
    number = _number(value)
    return number * 100 if number is not None else 0.0


def _tushare_amount(value: object) -> float:
    number = _number(value)
    return number * 1000 if number is not None else 0.0


def _tushare_market_value(value: object) -> float | None:
    number = _number(value)
    return number * 10000 if number is not None else None
