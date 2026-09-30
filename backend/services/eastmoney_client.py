from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
UT = "fa5fd1943c7b386f172d6893dbfba10b"
TENCENT_BATCH_SIZE = 300
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


F10_REPORTS = [
    "RPT_F10_BASIC_ORGINFO",
    "RPT_F10_ORG_BASICINFO",
    "RPT_HSF9_BASIC_ORGINFO",
    "RPT_F10_ORGINFO_MANAINTRO",
    "RPT_F10_ORGINFO_SALARY",
    "RPT_ORG_COURSECHANGE",
    "RPT_PCF10_ORG_ISSUEINFO",
    "RPT_F10_FINANCE_MAINFINADATA",
    "RPT_F10_QTR_MAINFINADATA",
    "RPT_PCF10_FINANCEMAINFINADATA",
    "RPT_F10_FINANCE_GBALANCE",
    "RPT_F10_FINANCE_GINCOME",
    "RPT_F10_FINANCE_GCASHFLOW",
    "RPT_F10_FINANCE_GCASHFLOWQC",
    "RPT_F10_FINANCE_GINCOMEQC",
    "RPT_F10_FINANCE_SBALANCE",
    "RPT_F10_FINANCE_SINCOME",
    "RPT_F10_FINANCE_SCASHFLOW",
    "RPT_F10_FINANCE_SCASHFLOWQC",
    "RPT_F10_FINANCE_SINCOMEQC",
    "RPT_F10_FINANCE_IBALANCE",
    "RPT_F10_FINANCE_IINCOME",
    "RPT_F10_FINANCE_ICASHFLOW",
    "RPT_F10_FINANCE_ICASHFLOWQC",
    "RPT_F10_FINANCE_IINCOMEQC",
    "RPT_F10_FINANCE_BBALANCE",
    "RPT_F10_FINANCE_BINCOME",
    "RPT_F10_FINANCE_BCASHFLOW",
    "RPT_F10_FINANCE_BINCOMEQC",
    "RPT_F10_FINANCE_DUPONT",
    "RPT_F10_FINANCE_GRATIO",
    "RPT_PCF10_ITEMCHG_EXPLAIN",
    "RPT_PCF10_ORIG_REPORT",
    "RPT_F10_FN_MAINOP",
    "RPT_F10_OP_BUSINESSANALYSIS",
    "RPT_HSF9_OP_VIOLATION",
    "RPT_F10_EH_HOLDERS",
    "RPT_F10_EH_HOLDERSDATE",
    "RPT_F10_EH_FREEHOLDERS",
    "RPT_F10_EH_FREEHOLDERSDATE",
    "RPT_F10_EH_HOLDERNUM",
    "RPT_F10_FREE_TOTALHOLDNUM",
    "RPT_F10_EH_RELATION",
    "RPT_F10_EH_EQUITY",
    "RPT_F10_SHAREHOLDER_CHANGE",
    "RPT_F10_MAIN_ORGHOLDDETAILS",
    "RPT_MAIN_ORGHOLDDETAIL",
    "RPT_F10_PUBLIC_OP_HOLDINGORG",
    "RPT_MUTUAL_STOCK_HOLDRANKN_NEW",
    "RPT_NORTH_ORG_HOLDDETAIL_NEW",
    "RPT_F10_TRADE_EXCHANGEHOLD",
    "RPT_F10_DIVIDEND_MAIN",
    "RPT_F10_DIVIDENDNEW_PROFILE",
    "RPT_F10_DIVIDENDNEW_LITY",
    "RPT_F10_DIVIDEND_3YEAR",
    "RPT_F10_DIVIDEND_ALLCOMPRE",
    "RPT_F10_DIVIDEND_ALLOTMENT",
    "RPT_F10_DIVIDEND_BOND",
    "RPT_F10_DIVIDEND_COMPRE",
    "RPT_F10_DIVIDEND_CURVE",
    "RPT_F10_DIVIDEND_EFFECT",
    "RPT_F10_DIVIDEND_HISTOGRAM",
    "RPT_F10_DIVIDEND_PRESHARES",
    "RPT_F10_DIVIDEND_SEO",
    "RPT_PCF10_DIVIDENDNEW_RANK",
    "RPT_F10_CAPITAL_ITEM",
    "RPT_F10_CAPITAL_RAISE",
    "RPT_ORG_RECAPITALIZE",
    "RPT_F10_CORETHEME_BOARDTYPE",
    "RPT_F10_CORETHEME_CONTENT",
    "RPT_F10_RELATE_GN",
    "RPT_F10_RELATE_RANK",
    "RPT_F10_INDUSTRY_COMPARED",
    "RPT_PCF10_INDUSTRY_CVALUE",
    "RPT_PCF10_INDUSTRY_DBFX",
    "RPT_PCF10_INDUSTRY_GROWTH",
    "RPT_PCF10_INDUSTRY_MARKET",
    "RPT_PCF10_MARKETPER",
    "RPT_STOCKVALUATIONTANTILE",
    "RPT_DMSK_NEWINDICATOR",
    "RPT_CUSTOM_DMSK_TREND",
    "RPT_STOCK_PK_RANK",
    "RPT_HSF10_RES_ORGRATING",
    "RPT_HSF10_RES_ORGPREDICT",
    "RPT_HSF10_RES_PREDICTDETAIL",
    "RPT_HSF10_RESPREDICT_STATISTICS",
    "RPT_HSF10_RESPREDICT_COUNTSTATISTICS",
    "RPT_BILLBOARD_DAILYDETAILS",
    "RPT_DATA_BLOCKTRADE",
    "RPT_MARGIN_STATISTICS_STOCKS",
    "RPT_STOCK_MARGINTRENDEXPLAIN",
    "RPT_OPERATEDEPT_TRADE",
    "RPT_F10_REMIND_RELATIONSHIP",
    "RPT_F10_ORGRES_GUARANTEE",
    "RPT_LITIGATION_ARBITRATION_BSINFO",
    "RPT_EXECUTIVE_HOLD_DETAILS",
    "RPT_F10_PUBLIC_COMPANYTPYE",
]

CORE_F10_REPORTS = [
    "RPT_F10_BASIC_ORGINFO",
    "RPT_F10_FINANCE_MAINFINADATA",
    "RPT_F10_EH_HOLDERNUM",
    "RPT_F10_CORETHEME_BOARDTYPE",
    "RPT_F10_OP_BUSINESSANALYSIS",
]


def normalize_symbol(symbol: str) -> str:
    digits = re.sub(r"\D", "", str(symbol or ""))
    return digits[-6:] if len(digits) >= 6 else str(symbol or "").strip()


def market_code(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    return "1" if normalized.startswith(("5", "6", "9")) else "0"


def secid(symbol: str, is_index: bool = False) -> str:
    normalized = normalize_symbol(symbol)
    if is_index and normalized.startswith(("0", "5", "6")):
        return f"1.{normalized}"
    return f"{market_code(normalized)}.{normalized}"


def secucode(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    suffix = "SH" if market_code(normalized) == "1" else "SZ"
    return f"{normalized}.{suffix}"


def list_a_stocks(page_size: int = 5000) -> list[dict]:
    requested_count = max(1, int(page_size or 5000))
    provider_page_size = min(100, requested_count)
    base_params = {
        "fs": "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80",
        "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f8,f9,f20,f21,f23",
        "fid": "f3",
        "po": 1,
        "np": 1,
        "fltt": 1,
        "invt": 2,
        "ut": UT,
    }

    def fetch_page(page_number: int) -> dict:
        return _get_json(
            [
                "https://push2.eastmoney.com/api/qt/clist/get",
                "https://push2delay.eastmoney.com/api/qt/clist/get",
            ],
            params={**base_params, "pn": page_number, "pz": provider_page_size},
            timeout=5,
            # The quote-list endpoint accepts the regular client. Avoid the
            # browser-fingerprint retry matrix for every catalog page.
            browser_like=False,
            allow_browser_fallback=False,
            referer="https://quote.eastmoney.com/center/gridlist.html",
        )

    first_page = fetch_page(1)
    first_payload = (first_page.get("data") or {}) if isinstance(first_page, dict) else {}
    try:
        provider_total = int(first_payload.get("total") or 0)
    except (TypeError, ValueError):
        provider_total = 0
    page_count = max(1, (min(requested_count, provider_total or requested_count) + provider_page_size - 1) // provider_page_size)
    pages = [first_page]
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=min(4, page_count - 1)) as executor:
            pages.extend(executor.map(fetch_page, range(2, page_count + 1)))

    items: list[dict] = []
    seen_symbols: set[str] = set()
    for page in pages:
        rows = (((page.get("data") or {}).get("diff") or []) if isinstance(page, dict) else [])
        for row in rows:
            symbol = normalize_symbol(row.get("f12"))
            name = _text(row.get("f14"))
            if not re.fullmatch(r"\d{6}", symbol or "") or not name or symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            items.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": str(row.get("f13") or market_code(symbol)),
                    "secid": f"{row.get('f13') or market_code(symbol)}.{symbol}",
                    "secucode": secucode(symbol),
                    "isSt": "ST" in name.upper(),
                    "latestPrice": _scaled(row.get("f2"), 100),
                    "pctChange": _scaled(row.get("f3"), 100),
                    "priceChange": _scaled(row.get("f4"), 100),
                    "volume": _num(row.get("f5")),
                    "amount": _num(row.get("f6")),
                    "turnoverRate": _scaled(row.get("f8"), 100),
                    "peDynamic": _scaled(row.get("f9"), 100),
                    "pb": _scaled(row.get("f23"), 100),
                    "totalMarketCap": _num(row.get("f20")),
                }
            )
            if len(items) >= requested_count:
                return items
    return items


def list_tencent_a_stocks(securities: list[dict], batch_size: int = TENCENT_BATCH_SIZE) -> list[dict]:
    """Fetch the current quote fields for a known A-share universe from Tencent."""

    universe = []
    seen: set[str] = set()
    for security in securities:
        symbol = normalize_symbol(security.get("symbol"))
        if not re.fullmatch(r"\d{6}", symbol or "") or symbol in seen:
            continue
        if not symbol.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")):
            continue
        seen.add(symbol)
        universe.append({"symbol": symbol, "name": _text(security.get("name"))})

    items: list[dict] = []
    for start in range(0, len(universe), max(1, batch_size)):
        batch = universe[start : start + max(1, batch_size)]
        query = ",".join(f"{'sh' if item['symbol'].startswith(('5', '6', '9')) else 'sz'}{item['symbol']}" for item in batch)
        response = _request_json_text(
            f"https://qt.gtimg.cn/q={query}",
            None,
            10,
            browser_like=True,
            referer="https://gu.qq.com/",
        )
        items.extend(_parse_tencent_quotes(response.content, batch))
        time.sleep(0.04)
    return items


def get_stock_snapshot(symbol: str) -> dict:
    fields = "f57,f58,f43,f59,f169,f170,f46,f44,f45,f60,f47,f48,f50,f168,f116,f117,f162,f167,f164,f92,f71,f85,f84,f86,f108,f152"
    data = _get_json(
        [
            "https://push2.eastmoney.com/api/qt/stock/get",
            "https://push2delay.eastmoney.com/api/qt/stock/get",
        ],
        params={"secid": secid(symbol), "ut": UT, "fltt": 1, "invt": 2, "fields": fields},
    )
    row = (data.get("data") or {}) if isinstance(data, dict) else {}
    if not row:
        raise RuntimeError("东方财富快照无返回")
    scale = 10 ** int(row.get("f59") or 2)
    return {
        "symbol": normalize_symbol(row.get("f57") or symbol),
        "name": _text(row.get("f58")),
        "latestPrice": _scaled(row.get("f43"), scale),
        "priceChange": _scaled(row.get("f169"), scale),
        "pctChange": _scaled(row.get("f170"), 100),
        "open": _scaled(row.get("f46"), scale),
        "high": _scaled(row.get("f44"), scale),
        "low": _scaled(row.get("f45"), scale),
        "previousClose": _scaled(row.get("f60"), scale),
        "volume": _num(row.get("f47")),
        "amount": _num(row.get("f48")),
        "volumeRatio": _num(row.get("f50")),
        "turnoverRate": _scaled(row.get("f168"), 100),
        "totalMarketCap": _num(row.get("f116")),
        "circulatingMarketCap": _num(row.get("f117")),
        "peDynamic": _scaled(row.get("f162"), 100),
        "peStatic": _scaled(row.get("f167"), 100),
        "peTtm": _scaled(row.get("f164"), 100),
        # Unlike the PE fields, f108 and f92 are already returned as decimal
        # values by the stock snapshot endpoint.
        "pb": _num(row.get("f108")),
        "navPerShare": _num(row.get("f92")),
    }


def get_kline(symbol: str, period: str = "101", adjust: str = "1", limit: int = 260, is_index: bool = False) -> list[dict]:
    params = {
        "secid": secid(symbol, is_index=is_index),
        "ut": UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": period,
        "fqt": adjust,
        "end": "20500101",
        "lmt": limit,
    }
    data = _get_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params=params,
        timeout=10,
        browser_like=True,
        referer=f"https://quote.eastmoney.com/{'sh' if secid(symbol, is_index=is_index).startswith('1.') else 'sz'}{normalize_symbol(symbol)}.html",
    )
    rows = ((data.get("data") or {}).get("klines") or []) if isinstance(data, dict) else []
    items = [_parse_kline_row(row) for row in rows]
    if not items:
        raise RuntimeError("东方财富K线无返回")
    return items


def get_tencent_kline(symbol: str, period: str = "101", adjust: str = "1", limit: int = 260, is_index: bool = False) -> list[dict]:
    """Fetch daily bars through Tencent's direct quote endpoint.

    The endpoint is used as the fast stock-history path when Eastmoney is
    unreachable. It intentionally returns the same normalized bar contract as
    ``get_kline`` so the analysis engine does not depend on the provider.
    """
    if str(period) != "101":
        raise RuntimeError("腾讯K线仅支持日线")

    normalized = normalize_symbol(symbol)
    if not normalized:
        raise RuntimeError("腾讯K线缺少股票代码")
    market = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    if is_index:
        market = "sh" if normalized.startswith(("0", "5", "6", "9")) else "sz"
    suffix = "qfq" if str(adjust) == "1" else "hfq" if str(adjust) == "2" else ""
    safe_limit = max(1, min(int(limit or 260), 2000))
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        TENCENT_KLINE_URL,
        params={"param": f"{market}{normalized},day,,,{safe_limit},{suffix}"},
        headers=_request_headers("https://gu.qq.com/"),
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    quote = ((payload.get("data") or {}).get(f"{market}{normalized}") or {}) if isinstance(payload, dict) else {}
    rows = quote.get(f"{suffix}day") or quote.get("day") or []
    if not rows:
        raise RuntimeError("腾讯K线无返回")

    items: list[dict] = []
    previous_close: float | None = None
    for row in rows[-safe_limit:]:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        date_text = str(row[0] or "").strip()
        open_price = _num(row[1])
        close_price = _num(row[2])
        high_price = _num(row[3])
        low_price = _num(row[4])
        volume = _num(row[5]) or 0.0
        if not date_text or None in (open_price, close_price, high_price, low_price):
            continue
        price_change = close_price - previous_close if previous_close else None
        pct_change = (price_change / previous_close * 100) if price_change is not None and previous_close else None
        items.append(
            {
                "date": date_text,
                "open": open_price,
                "close": close_price,
                "high": high_price,
                "low": low_price,
                "volume": volume,
                "amount": 0.0,
                "pctChange": pct_change,
                "priceChange": price_change,
                "turnoverRate": None,
                "amountUnit": "CNY",
                "volumeUnit": "shares",
                "session": "CLOSED",
                "source": "tencent",
                "provider": "tencent",
                "completed": True,
                "timestampQuality": "VALID",
                "adjustmentVersion": suffix or "raw",
            }
        )
        previous_close = close_price
    if not items:
        raise RuntimeError("腾讯K线无有效数据")
    return items


def get_intraday_kline(symbol: str, period: str, limit: int = 180, is_index: bool = False) -> list[dict]:
    return get_kline(symbol=symbol, period=period, adjust="1" if not is_index else "0", limit=limit, is_index=is_index)


def fetch_f10_report(
    symbol: str,
    report_name: str,
    *,
    page_size: int = 10,
    page_number: int = 1,
    sort_columns: str = "",
    sort_types: str = "",
) -> dict:
    params: dict[str, Any] = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": f'(SECUCODE="{secucode(symbol)}")',
        "source": "HSF10",
        "client": "PC",
        "pageSize": page_size,
        "pageNumber": page_number,
    }
    if sort_columns:
        params["sortColumns"] = sort_columns
    if sort_types:
        params["sortTypes"] = sort_types
    return _get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get", params=params)


def fetch_all_f10_reports(symbol: str, reports: list[str] | None = None, page_size: int = 20) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for report_name in reports or F10_REPORTS:
        sort_columns, sort_types = _report_sort(report_name)
        try:
            results[report_name] = fetch_f10_report(
                symbol,
                report_name,
                page_size=page_size,
                sort_columns=sort_columns,
                sort_types=sort_types,
            )
            time.sleep(0.08)
        except Exception as exc:
            results[report_name] = {"error": str(exc), "result": {"data": []}}
    return results


def extract_f10_rows(payload: dict) -> list[dict]:
    result = payload.get("result") if isinstance(payload, dict) else None
    data = (result or {}).get("data")
    return data if isinstance(data, list) else []


def _report_sort(report_name: str) -> tuple[str, str]:
    if "HOLDER" in report_name or "HOLD" in report_name:
        return "END_DATE,HOLDER_RANK", "-1,1"
    if "DIVIDEND" in report_name:
        return "NOTICE_DATE", "-1"
    if "FINANCE" in report_name or "MAINFINADATA" in report_name or "MAINOP" in report_name:
        return "REPORT_DATE", "-1"
    return "", ""


def _get_json(
    url: str | list[str],
    params: dict | None = None,
    timeout: int = 6,
    *,
    browser_like: bool = False,
    allow_browser_fallback: bool = True,
    referer: str = "https://quote.eastmoney.com/",
) -> dict:
    last_error: Exception | None = None
    urls = url if isinstance(url, list) else [url]
    for target_url in urls:
        for attempt in range(2):
            try:
                response = _request_json_text(
                    target_url,
                    params,
                    timeout,
                    browser_like=browser_like,
                    allow_browser_fallback=allow_browser_fallback,
                    referer=referer,
                )
                return _parse_json_text(response.text)
            except Exception as exc:
                last_error = exc
                time.sleep(0.25 + attempt * 0.35)
    raise RuntimeError(str(last_error) if last_error else "东方财富请求失败")


def _request_json_text(
    target_url: str,
    params: dict | None,
    timeout: int,
    *,
    browser_like: bool,
    allow_browser_fallback: bool = True,
    referer: str,
):
    headers = _request_headers(referer)
    if browser_like:
        return _request_with_browser_fingerprint(target_url, params, headers, timeout)

    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(target_url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except Exception:
        if not allow_browser_fallback:
            raise
        return _request_with_browser_fingerprint(target_url, params, headers, timeout)


def _request_headers(referer: str) -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
    }


def _request_with_browser_fingerprint(target_url: str, params: dict | None, headers: dict, timeout: int):
    """Retry with browser headers without loading a native HTTP extension.

    The old curl_cffi impersonation path loaded a bundled libcurl/OpenSSL
    module into the long-lived service and was associated with Windows heap
    corruption. Eastmoney still receives the same browser-shaped headers;
    standard requests is sufficient for the fallback and keeps the process
    on one TLS implementation.
    """

    last_error: Exception | None = None
    for _ in range(2):
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.get(target_url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            time.sleep(0.15)
    raise last_error or RuntimeError("Eastmoney request failed")


def _parse_json_text(text: str) -> dict:
    raw = text.strip()
    match = re.match(r"^[^(]+\((.*)\);?$", raw, flags=re.S)
    if match:
        raw = match.group(1)
    return json.loads(raw)


def _parse_tencent_quotes(content: bytes, batch: list[dict]) -> list[dict]:
    text = content.decode("gbk", errors="replace")
    names = {item["symbol"]: item.get("name") or item["symbol"] for item in batch}
    items: list[dict] = []
    for match in re.finditer(r"v_(sh|sz)(\d{6})=\"([^\"]*)\"", text):
        market, symbol, raw = match.groups()
        fields = raw.split("~")
        if len(fields) < 39:
            continue
        price = _num(fields[3])
        if price is None or price <= 0:
            continue
        volume = _num(fields[36]) or _num(fields[6])
        amount = (_num(fields[37]) or 0) * 10000
        items.append(
            {
                "symbol": symbol,
                "name": _text(fields[1]) or names.get(symbol) or symbol,
                "market": market,
                "secid": f"{'1' if market == 'sh' else '0'}.{symbol}",
                "secucode": f"{symbol}.{'SH' if market == 'sh' else 'SZ'}",
                "isSt": "ST" in (_text(fields[1]) or names.get(symbol) or "").upper(),
                "latestPrice": price,
                "pctChange": _num(fields[32]),
                "priceChange": _num(fields[31]),
                "volume": volume,
                "amount": amount,
                "turnoverRate": _num(fields[38]),
                "peDynamic": None,
                "pb": None,
                "totalMarketCap": None,
                "quoteTime": fields[30],
            }
        )
    return items


def _parse_kline_row(row: str) -> dict:
    parts = str(row).split(",")
    parts += [""] * (11 - len(parts))
    return {
        "date": parts[0],
        "open": _num(parts[1]),
        "close": _num(parts[2]),
        "high": _num(parts[3]),
        "low": _num(parts[4]),
        "volume": _num(parts[5]),
        "amount": _num(parts[6]),
        "amplitude": _num(parts[7]),
        "pctChange": _num(parts[8]),
        "priceChange": _num(parts[9]),
        "turnoverRate": _num(parts[10]),
    }


def _num(value: object) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scaled(value: object, scale: int | float) -> float | None:
    number = _num(value)
    if number is None:
        return None
    return round(number / scale, 4)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")
