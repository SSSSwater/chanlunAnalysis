"""Fetch Binance Smart Money operation history and positions.

The endpoint is a Binance web endpoint rather than a signed REST API endpoint.
Use a browser cookie only when the endpoint requires it; never commit it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import time
from http.client import IncompleteRead, RemoteDisconnected
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4


HISTORY_ENDPOINT = "https://www.binance.com/bapi/asset/v1/private/future/smart-money/profile/query-order-history"
POSITIONS_ENDPOINT = "https://www.binance.com/bapi/asset/v1/private/future/smart-money/profile/query-positions"
PROFILE_ENDPOINT = "https://www.binance.com/bapi/asset/v1/friendly/future/smart-money/profile"
SUBSCRIPTION_LIST_ENDPOINT = "https://www.binance.com/bapi/futures/v1/private/future/smart-money/subscription/list"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)
DEFAULT_LOOKBACK_DAYS = 30
# Keep a small buffer for the endpoint watermark without hiding the latest hour.
DEFAULT_END_LAG_MS = 5 * 60 * 1000
DEFAULT_TIMEOUT_SECONDS = 15
NETWORK_RETRY_DELAYS = (0.4, 1.0, 2.0)
MAX_PAGE_SIZE = 10


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _timestamp(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be milliseconds") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("timestamp must not be negative")
    return parsed


def _request_payload(
    top_trader_id: str,
    page_size: int,
    start_time: int | None,
    end_time: int | None,
    lookback_days: int,
) -> dict[str, Any]:
    effective_end = (
        int(time.time() * 1000) - DEFAULT_END_LAG_MS
        if end_time is None
        else end_time
    )
    effective_start = (
        effective_end - lookback_days * 24 * 60 * 60 * 1000
        if start_time is None
        else start_time
    )
    if effective_start > effective_end:
        raise ValueError("start-time must not be later than end-time")
    return {
        "topTraderId": top_trader_id,
        "marketType": "UM",
        "startTime": effective_start,
        "endTime": effective_end,
        "rows": page_size,
        "page": 1,
    }


def _cookie_from_environment() -> str:
    return ""


def _capture_headers_from_file(path: str) -> dict[str, str]:
    """Read only request headers from a pasted browser capture, never log them."""

    names = {
        "cookie",
        "csrftoken",
        "device-info",
        "fvideo-id",
        "fvideo-token",
        "x-passthrough-token",
        "referer",
    }
    all_header_names = names | {
        "accept", "accept-encoding", "accept-language", "authorization", "content-type",
        "origin", "user-agent", "x-trace-id", "x-ui-request-trace", "clienttype", "lang",
    }
    headers: dict[str, str] = {}
    pending_name: str | None = None
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        first, separator, remainder = raw.partition(" ")
        first = first.strip().lower().rstrip(":")
        if pending_name:
            if first in all_header_names:
                if pending_name in names:
                    headers[pending_name] = ""
                pending_name = None
            else:
                headers[pending_name] = raw
                pending_name = None
                continue
        if first in names:
            if separator and remainder.strip():
                headers[first] = remainder.strip()
            else:
                pending_name = first
            continue
        if ":" in raw:
            name, value = raw.split(":", 1)
        else:
            name, separator, value = raw.partition(" ")
            if not separator:
                continue
        name = name.strip().lower().rstrip(":")
        if name in names:
            headers[name] = value.strip()
    if pending_name in names:
        headers[pending_name] = ""
    return headers


def _configured_header(environment_name: str, capture_name: str, auth: dict[str, str] | None = None) -> str:
    if auth is not None:
        auth_keys = {
            "csrftoken": "csrfToken",
            "device-info": "deviceInfo",
            "fvideo-id": "fvideoId",
            "fvideo-token": "fvideoToken",
            "x-passthrough-token": "xPassthroughToken",
        }
        return str(auth.get(capture_name) or auth.get(auth_keys.get(capture_name, "")) or "").strip()
    return ""


def _headers(
    cookie: str,
    *,
    referer: str,
    user_agent: str,
    auth: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": "https://www.binance.com",
        "Referer": referer,
        "User-Agent": user_agent,
        "clienttype": "web",
        "lang": "zh-CN",
        "bnc-location": "CN",
        "bnc-time-zone": "Asia/Shanghai",
        "bnc-uuid": os.environ.get("BINANCE_BNC_UUID", str(uuid4())),
        "x-trace-id": str(uuid4()),
        "x-ui-request-trace": str(uuid4()),
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if cookie:
        headers["Cookie"] = cookie
    csrf_token = _configured_header("BINANCE_CSRF_TOKEN", "csrftoken", auth)
    if csrf_token:
        headers["csrftoken"] = csrf_token
    for environment_name, header_name in (
        ("BINANCE_DEVICE_INFO", "device-info"),
        ("BINANCE_FVIDEO_ID", "fvideo-id"),
        ("BINANCE_FVIDEO_TOKEN", "fvideo-token"),
        ("BINANCE_X_PASSTHROUGH_TOKEN", "x-passthrough-token"),
    ):
        value = _configured_header(environment_name, header_name, auth)
        if value:
            headers[header_name] = value
    return headers


def _is_retryable_network_error(exc: BaseException) -> bool:
    """Identify transient proxy/TLS disconnects without hiding API errors."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (ssl.SSLError, TimeoutError, IncompleteRead, RemoteDisconnected, ConnectionError)):
            return True
        reason = getattr(current, "reason", None)
        cause = getattr(current, "__cause__", None)
        current = reason if isinstance(reason, BaseException) else cause if isinstance(cause, BaseException) else None
    text = str(exc).lower()
    return any(marker in text for marker in (
        "unexpected_eof",
        "eof occurred",
        "connection reset",
        "remote end closed",
        "timed out",
        "temporarily unavailable",
    ))


def _fetch_json(
    endpoint: str,
    params: dict[str, Any],
    *,
    cookie: str,
    proxy: str,
    referer: str,
    timeout: float,
    auth: dict[str, str] | None = None,
    method: str = "GET",
    json_payload: dict[str, Any] | None = None,
) -> Any:
    body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8") if json_payload is not None else None
    request_url = f"{endpoint}?{urlencode(params)}" if params else endpoint
    last_error: BaseException | None = None
    for attempt in range(len(NETWORK_RETRY_DELAYS) + 1):
        request = Request(
            request_url,
            data=body,
            # Rebuild trace headers and the opener for each attempt. This
            # avoids reusing a proxy connection that already emitted EOF.
            headers=_headers(cookie, referer=referer, user_agent=DEFAULT_USER_AGENT, auth=auth),
            method=method,
        )
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else build_opener()
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            break
        except HTTPError as exc:
            if 500 <= exc.code < 600 and attempt < len(NETWORK_RETRY_DELAYS):
                try:
                    exc.read()
                except Exception:
                    pass
                time.sleep(NETWORK_RETRY_DELAYS[attempt])
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance returned HTTP {exc.code}: {detail[:500]}") from exc
        except (URLError, TimeoutError, IncompleteRead, RemoteDisconnected, OSError) as exc:
            last_error = exc
            if _is_retryable_network_error(exc) and attempt < len(NETWORK_RETRY_DELAYS):
                time.sleep(NETWORK_RETRY_DELAYS[attempt])
                continue
            reason = getattr(exc, "reason", exc)
            raise RuntimeError(f"Binance request failed: {reason}") from exc
    else:  # pragma: no cover - the loop either returns or raises
        raise RuntimeError(f"Binance request failed: {last_error}") from last_error
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Binance returned a non-JSON response") from exc


def fetch_history(
    payload: dict[str, Any],
    *,
    cookie: str = "",
    proxy: str = "",
    referer: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    auth: dict[str, str] | None = None,
) -> Any:
    return _fetch_json(
        HISTORY_ENDPOINT,
        payload,
        cookie=cookie,
        proxy=proxy,
        referer=referer,
        timeout=timeout,
        auth=auth,
    )


def fetch_positions(
    top_trader_id: str,
    *,
    page: int = 1,
    rows: int = 20,
    cookie: str = "",
    proxy: str = "",
    referer: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    auth: dict[str, str] | None = None,
) -> Any:
    return _fetch_json(
        POSITIONS_ENDPOINT,
        {"topTraderId": top_trader_id, "marketType": "UM", "page": page, "rows": rows},
        cookie=cookie,
        proxy=proxy,
        referer=referer,
        timeout=timeout,
        auth=auth,
    )


def fetch_profile(
    top_trader_id: str,
    *,
    cookie: str = "",
    proxy: str = "",
    referer: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    auth: dict[str, str] | None = None,
) -> Any:
    return _fetch_json(
        PROFILE_ENDPOINT,
        {"topTraderId": top_trader_id},
        cookie=cookie,
        proxy=proxy,
        referer=referer,
        timeout=timeout,
        auth=auth,
    )


def fetch_smart_money_subscriptions(
    *,
    page: int = 1,
    rows: int = 10,
    only_show_sharing_position: bool = False,
    cookie: str = "",
    proxy: str = "",
    referer: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    auth: dict[str, str] | None = None,
) -> Any:
    return _fetch_json(
        SUBSCRIPTION_LIST_ENDPOINT,
        {},
        cookie=cookie,
        proxy=proxy,
        referer=referer,
        timeout=timeout,
        auth=auth,
        method="POST",
        json_payload={
            "page": page,
            "rows": rows,
            "onlyShowSharingPosition": only_show_sharing_position,
        },
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Binance Smart Money operation history.")
    parser.add_argument("--top-trader-id", required=True, help="Binance Smart Money top trader ID")
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=10,
        help=f"Number of latest records requested (default: 10, max: {MAX_PAGE_SIZE})",
    )
    parser.add_argument("--start-time", type=_timestamp, help="Start timestamp in milliseconds")
    parser.add_argument("--end-time", type=_timestamp, help="End timestamp in milliseconds")
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback days when --start-time is omitted (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--cookie-file",
        help="Read browser Cookie header from this local file",
    )
    parser.add_argument("--proxy", default=os.environ.get("BINANCE_REST_PROXY", ""), help="HTTP proxy URL")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit > MAX_PAGE_SIZE:
        parser.error(f"--limit must be <= {MAX_PAGE_SIZE}")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    capture_auth = _capture_headers_from_file(args.cookie_file) if args.cookie_file else None
    cookie = _cookie_from_environment()
    if args.cookie_file:
        cookie = capture_auth.get("cookie", "")
    payload = _request_payload(
        args.top_trader_id,
        args.limit,
        args.start_time,
        args.end_time,
        args.days,
    )
    referer = f"https://www.binance.com/zh-CN/smart-money/profile/{args.top_trader_id}"
    try:
        result = fetch_history(
            payload,
            cookie=cookie,
            proxy=args.proxy,
            referer=referer,
            timeout=args.timeout,
            auth=capture_auth,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
