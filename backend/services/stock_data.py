from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Callable

import akshare as ak
import pandas as pd


CODE_COL = "\u4ee3\u7801"
NAME_COL = "\u540d\u79f0"
LATEST_PRICE_COL = "\u6700\u65b0\u4ef7"


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


def _normalize_symbol(symbol: str) -> str:
    digits = re.sub(r"\D", "", symbol)
    return digits[-6:] if len(digits) >= 6 else symbol.strip()


def _market_symbol(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{normalized}"


@lru_cache(maxsize=1)
def _stock_spot_full() -> pd.DataFrame:
    fetchers = [ak.stock_zh_a_spot_em, ak.stock_zh_a_spot]
    for fetcher in fetchers:
        try:
            df = fetcher().dropna(subset=[CODE_COL, NAME_COL]).drop_duplicates(CODE_COL)
            if LATEST_PRICE_COL in df.columns:
                return df
        except Exception:
            continue

    df = ak.stock_info_a_code_name()
    return (
        df.rename(columns={"code": CODE_COL, "name": NAME_COL})[[CODE_COL, NAME_COL]]
        .dropna()
        .drop_duplicates(CODE_COL)
    )


@lru_cache(maxsize=1)
def _stock_spot() -> pd.DataFrame:
    try:
        df = _stock_spot_full()
        return df[[CODE_COL, NAME_COL]].dropna().drop_duplicates(CODE_COL)
    except Exception:
        df = ak.stock_info_a_code_name()
        return df.rename(columns={"code": CODE_COL, "name": NAME_COL})[[CODE_COL, NAME_COL]].dropna().drop_duplicates(CODE_COL)


def search_stocks(keyword: str, limit: int = 20) -> list[dict]:
    word = keyword.strip().lower()
    if not word:
        return []

    stocks = list_stocks()
    df = pd.DataFrame(
        [{"代码": item["symbol"], "名称": item["name"]} for item in stocks]
    )
    if df.empty:
        df = pd.DataFrame(FALLBACK_STOCKS)

    code_match = df["代码"].astype(str).str.contains(word, case=False, na=False, regex=False)
    name_match = df["名称"].astype(str).str.lower().str.contains(word, na=False, regex=False)
    matched = df[code_match | name_match].head(limit)

    if matched.empty and re.fullmatch(r"\d{1,6}", word):
        matched = pd.DataFrame([{"代码": word.zfill(6), "名称": "代码直查"}])

    return _stock_rows_to_items(matched)


def list_stocks() -> list[dict]:
    try:
        df = _stock_spot()
    except Exception:
        df = pd.DataFrame(FALLBACK_STOCKS)

    return _stock_rows_to_items(df)


def list_price_limited_non_st_stocks(min_price: float, max_price: float) -> list[dict]:
    if max_price <= 0 or min_price > max_price:
        return []

    try:
        df = _stock_spot_full().copy()
    except Exception:
        return []

    if LATEST_PRICE_COL not in df.columns:
        return []

    df[LATEST_PRICE_COL] = pd.to_numeric(df[LATEST_PRICE_COL], errors="coerce")
    code = df[CODE_COL].astype(str).map(_normalize_symbol)
    name = df[NAME_COL].astype(str)
    mask = (
        code.str.fullmatch(r"\d{6}", na=False)
        & code.map(_is_supported_stock_symbol)
        & ~name.str.upper().str.contains("ST", na=False)
        & df[LATEST_PRICE_COL].notna()
        & (df[LATEST_PRICE_COL] > 0)
        & (df[LATEST_PRICE_COL] >= min_price)
        & (df[LATEST_PRICE_COL] <= max_price)
    )
    matched = df[mask].copy()
    return [
        {
            "symbol": _normalize_symbol(str(row[CODE_COL])),
            "name": str(row[NAME_COL]),
            "label": f'{_normalize_symbol(str(row[CODE_COL]))} - {row[NAME_COL]}',
            "latestPrice": round(float(row[LATEST_PRICE_COL]), 3),
        }
        for _, row in matched.iterrows()
    ]


def _is_supported_stock_symbol(symbol: str) -> bool:
    return str(symbol).startswith(
        (
            "000",
            "001",
            "002",
            "003",
            "300",
            "301",
            "600",
            "601",
            "603",
            "605",
            "688",
        )
    )


def _stock_rows_to_items(df: pd.DataFrame) -> list[dict]:
    return [
        {
            "symbol": str(row["代码"]),
            "name": str(row["名称"]),
            "label": f'{row["代码"]} - {row["名称"]}',
        }
        for _, row in df.iterrows()
    ]


def get_stock_history(symbol: str, start_date: str, end_date: str) -> list[dict]:
    normalized = _normalize_symbol(symbol)
    df = _fetch_history(normalized, start_date, end_date)
    return _history_to_items(df)


def get_index_history(symbol: str = "000001", start_date: str = "", end_date: str = "") -> list[dict]:
    normalized = _normalize_symbol(symbol) or "000001"
    df = _fetch_index_history(normalized, start_date, end_date)
    return _history_to_items(df, start_date=start_date, end_date=end_date)


def _history_to_items(df: pd.DataFrame, start_date: str = "", end_date: str = "") -> list[dict]:
    if df.empty:
        return []

    df = _normalize_history_columns(df)
    if start_date:
        start_text = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        df = df[df["date"] >= start_text]
    if end_date:
        end_text = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        df = df[df["date"] <= end_text]
    df = df.sort_values("date").reset_index(drop=True)

    fields = ["date", "open", "high", "low", "close", "volume", "amount", "pct_change", "turnover_rate"]
    items: list[dict] = []
    for _, row in df[fields].iterrows():
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
            }
        )

    return items


def _fetch_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=10,
        ),
        lambda: ak.stock_zh_a_hist_tx(
            symbol=_market_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
            timeout=10,
        ),
        lambda: ak.stock_zh_a_daily(
            symbol=_market_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        ),
    ]

    for fetcher in fetchers:
        df, error = _fetch_with_retries(fetcher)
        if error:
            errors.append(error)
        if df is not None and not df.empty:
            return df

    raise RuntimeError("；".join(errors[-2:]) or "数据源无返回")


def _fetch_index_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    market_symbol = f"sh{_normalize_symbol(symbol)}"
    errors: list[str] = []
    fetchers = [
        lambda: ak.stock_zh_index_daily_tx(
            symbol=market_symbol,
            start_date=start_date,
            end_date=end_date,
        ),
        lambda: ak.stock_zh_index_daily(symbol=market_symbol),
        lambda: ak.index_zh_a_hist(
            symbol=_normalize_symbol(symbol),
            period="daily",
            start_date=start_date,
            end_date=end_date,
        ),
    ]

    for fetcher in fetchers:
        df, error = _fetch_with_retries(fetcher)
        if error:
            errors.append(error)
        if df is not None and not df.empty:
            return df

    raise RuntimeError("指数数据源连续失败：" + ("；".join(errors[-2:]) or "数据源无返回"))


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


def _normalize_history_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.rename(
        columns={
            "日期": "date",
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

    if "volume" not in normalized.columns and "amount" in normalized.columns:
        normalized["volume"] = normalized["amount"]
    if "amount" not in normalized.columns:
        normalized["amount"] = 0
    if "pct_change" not in normalized.columns:
        normalized["pct_change"] = normalized["close"].pct_change().fillna(0) * 100
    if "turnover_rate" not in normalized.columns:
        normalized["turnover_rate"] = None

    normalized["date"] = pd.to_datetime(normalized["date"]).dt.strftime("%Y-%m-%d")
    numeric_fields = ["open", "high", "low", "close", "volume", "amount", "pct_change", "turnover_rate"]
    for field in numeric_fields:
        normalized[field] = pd.to_numeric(normalized[field], errors="coerce")

    return normalized.dropna(subset=["date", "open", "high", "low", "close"])
