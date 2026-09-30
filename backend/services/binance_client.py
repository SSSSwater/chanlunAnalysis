from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
import re
import shutil
import ssl
import subprocess
from http.client import IncompleteRead, RemoteDisconnected
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

MAINNET_BASE_URL = "https://api.binance.com"
TESTNET_BASE_URL = "https://testnet.binance.vision"
SUPPORTED_NETWORKS = {"mainnet": MAINNET_BASE_URL, "testnet": TESTNET_BASE_URL}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
INTERVAL_PATTERN = re.compile(r"^(1s|1m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d|3d|1w|1M)$")
GET_NETWORK_RETRY_DELAYS = (0.35, 1.0)
TRANSIENT_HTTP_STATUS_CODES = frozenset({500, 502, 503, 504})
NETWORK_REQUEST_TIMEOUT_SECONDS = 10
SIGNED_RECV_WINDOW_MS = 10_000
SERVER_TIME_SYNC_MAX_AGE_SECONDS = 30
MARKET_CACHE_TTL_SECONDS = 180
CONNECTION_ERROR_MESSAGE = "无法连接交易所，请检查网络或接口地址"
_market_cache: dict[tuple[object, ...], tuple[float, object]] = {}
_market_cache_lock = Lock()
_server_time_offset_ms = 0
_server_time_last_sync_monotonic = 0.0
_server_time_lock = Lock()
_server_time_sync_lock = Lock()
# Binance REST and WebSocket use the local proxy independently. WebSocket
# transport owns its proxy options in binance_websocket_transport.py.
REST_PROXY_URL = os.environ.get("BINANCE_REST_PROXY", "http://127.0.0.1:10808")
_direct_opener = build_opener(ProxyHandler({"http": REST_PROXY_URL, "https": REST_PROXY_URL}))


def _direct_environment() -> dict[str, str]:
    """Return a child-process environment using only the Binance REST proxy."""

    environment = os.environ.copy()
    for name in tuple(environment):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy", "binance_proxy"}:
            environment.pop(name, None)
    environment["HTTP_PROXY"] = REST_PROXY_URL
    environment["HTTPS_PROXY"] = REST_PROXY_URL
    return environment


class BinanceApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502, exchange_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.exchange_code = exchange_code
        self.is_transport_error = False
        self.is_transient = False
        self.endpoint = None
        self.method = None


def _request_can_retry(method: str, endpoint: str) -> bool:
    """Return whether repeating this request cannot create a duplicate order."""

    request_method = str(method or "GET").upper()
    if request_method in {"GET", "DELETE"}:
        return True
    # Setting leverage is idempotent for a symbol. Other POST requests need a
    # client id and are recovered by their operation-specific caller.
    return request_method == "POST" and str(endpoint or "") == "/fapi/v1/leverage"


def _has_idempotency_key(params: dict[str, object]) -> bool:
    return any(str(params.get(name) or "").strip() for name in ("newClientOrderId", "clientAlgoId"))


def _annotate_request_error(error: BinanceApiError, *, endpoint: str, method: str, transient: bool = False, transport: bool = False) -> BinanceApiError:
    error.endpoint = endpoint
    error.method = method
    error.is_transient = bool(transient)
    error.is_transport_error = bool(transport)
    return error


def _market_cache_get(key: tuple[object, ...]):
    with _market_cache_lock:
        cached = _market_cache.get(key)
        if cached is None:
            return None
        stored_at, payload = cached
        if time.monotonic() - stored_at > MARKET_CACHE_TTL_SECONDS:
            _market_cache.pop(key, None)
            return None
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, list):
            return [dict(item) if isinstance(item, dict) else item for item in payload]
        return payload


def _market_cache_set(key: tuple[object, ...], payload: object) -> None:
    if isinstance(payload, dict):
        value = dict(payload)
    elif isinstance(payload, list):
        value = [dict(item) if isinstance(item, dict) else item for item in payload]
    else:
        value = payload
    with _market_cache_lock:
        _market_cache[key] = (time.monotonic(), value)


def _network_url(network: str) -> str:
    value = str(network or "").strip().lower()
    if value not in SUPPORTED_NETWORKS:
        raise ValueError("网络必须选择主网或测试网")
    return SUPPORTED_NETWORKS[value]


def _symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError("交易对格式无效")
    return value


def _interval(interval: str) -> str:
    value = str(interval or "1h").strip()
    if not INTERVAL_PATTERN.fullmatch(value):
        raise ValueError("K 线周期无效")
    return value


def _credentials(api_key: str, api_secret: str) -> tuple[str, str]:
    key = str(api_key or "").strip()
    secret = str(api_secret or "").strip()
    if not key or not secret:
        raise ValueError("请填写 API Key 和 Secret Key")
    if len(key) > 256 or len(secret) > 256:
        raise ValueError("API 凭据长度无效")
    return key, secret


def _api_key(api_key: str) -> str:
    key = str(api_key or "").strip()
    if not key or len(key) > 256:
        raise ValueError("API Key 长度无效")
    return key


def _as_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _read_json(response) -> object:
    try:
        return json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _curl_request_json(url: str, *, method: str, headers: dict[str, str]) -> object:
    """Use the system curl executable for the isolated TLS/proxy fallback.

    urllib remains the normal REST transport. Keeping this fallback in a
    separate process avoids loading a second native libcurl/OpenSSL stack into
    the long-lived Python service.
    """

    return _system_curl_request_json(url, method=method, headers=headers)


def _system_curl_request_json(url: str, *, method: str, headers: dict[str, str]) -> object:
    """Use the installed curl executable without exposing credentials to a shell."""

    executable = shutil.which("curl.exe") or shutil.which("curl")
    if not executable:
        raise URLError("curl executable is unavailable")
    marker = b"\n__CHANLUN_HTTP_STATUS__"
    command = [
        executable,
        "--silent",
        "--show-error",
        "--compressed",
        "--request",
        method.upper(),
        "--url",
        url,
        "--connect-timeout",
        "5",
        "--max-time",
        str(NETWORK_REQUEST_TIMEOUT_SECONDS),
        "--proxy",
        REST_PROXY_URL,
        "--write-out",
        "\n__CHANLUN_HTTP_STATUS__%{http_code}",
    ]
    for name, value in headers.items():
        command.extend(("--header", f"{name}: {value}"))
    if method.upper() == "GET":
        command.extend(("--retry", "2", "--retry-delay", "0.2", "--retry-max-time", "8", "--retry-all-errors"))
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=NETWORK_REQUEST_TIMEOUT_SECONDS + (3 if method.upper() == "GET" else 1),
            check=False,
            creationflags=creation_flags,
            env=_direct_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise URLError(exc) from exc
    output = completed.stdout or b""
    status_position = output.rfind(marker)
    if status_position < 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise URLError(detail or f"curl exited with status {completed.returncode}")
    body = output[:status_position]
    status_bytes = output[status_position + len(marker):].strip()
    try:
        status_code = int(status_bytes)
    except ValueError as exc:
        raise URLError("curl returned an invalid HTTP status") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if completed.returncode != 0 and status_code == 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise URLError(detail or f"curl exited with status {completed.returncode}")
    if status_code >= 400:
        error = _exchange_error(payload, "交易所请求失败")
        error.status_code = 429 if status_code == 429 else (400 if status_code < 500 else 502)
        raise error
    return payload


def _is_tls_transport_error(exc: BaseException) -> bool:
    """Recognize TLS handshake failures wrapped by urllib's URLError."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        reason = getattr(current, "reason", None)
        cause = getattr(current, "__cause__", None)
        current = reason if isinstance(reason, BaseException) else cause if isinstance(cause, BaseException) else None
    text = repr(exc).lower()
    return "ssl" in text and any(marker in text for marker in ("eof", "handshake", "tls"))


def _exchange_error(payload: object, fallback: str) -> BinanceApiError:
    if isinstance(payload, dict):
        message = str(payload.get("msg") or payload.get("message") or "").strip()
        code = payload.get("code")
        try:
            exchange_code = int(code) if code is not None else None
        except (TypeError, ValueError):
            exchange_code = None
        if message:
            return BinanceApiError(message, exchange_code=exchange_code)
    return BinanceApiError(fallback)


def _signed_timestamp() -> int:
    with _server_time_lock:
        offset = _server_time_offset_ms
    return int(time.time() * 1000) + offset


def _sync_server_time(base_url: str) -> int:
    global _server_time_offset_ms, _server_time_last_sync_monotonic
    started_at = time.monotonic()
    request = Request(
        f"{base_url}/api/v3/time",
        headers={"Accept": "application/json", "User-Agent": "chanlun-analysis/binance"},
        method="GET",
    )
    try:
        with _direct_opener.open(request, timeout=NETWORK_REQUEST_TIMEOUT_SECONDS) as response:
            payload = _read_json(response)
    except (TimeoutError, URLError, IncompleteRead, RemoteDisconnected) as exc:
        if not _is_tls_transport_error(exc):
            raise
        payload = _curl_request_json(
            f"{base_url}/api/v3/time",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "chanlun-analysis/binance"},
        )
    if not isinstance(payload, dict) or payload.get("serverTime") is None:
        raise BinanceApiError("交易所服务器时间格式无效")
    try:
        server_time = int(payload["serverTime"])
    except (TypeError, ValueError) as exc:
        raise BinanceApiError("交易所服务器时间格式无效") from exc
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    local_time = int(time.time() * 1000) - max(0, elapsed_ms // 2)
    offset = server_time - local_time
    with _server_time_lock:
        _server_time_offset_ms = offset
        _server_time_last_sync_monotonic = time.monotonic()
    return offset


def _sync_server_time_if_stale(base_url: str) -> None:
    """Refresh the signed-request clock before the cached offset can drift."""

    with _server_time_sync_lock:
        with _server_time_lock:
            last_sync = _server_time_last_sync_monotonic
        if last_sync and time.monotonic() - last_sync < SERVER_TIME_SYNC_MAX_AGE_SECONDS:
            return
        _sync_server_time(base_url)


def _request(
    network: str,
    endpoint: str,
    params: dict[str, object] | None = None,
    *,
    method: str = "GET",
    api_key: str = "",
    api_secret: str = "",
    signed: bool = False,
) -> object:
    base_url = _network_url(network)
    query: dict[str, object] = dict(params or {})
    refresh_timestamp_on_attempt = "timestamp" not in query
    headers = {"Accept": "application/json", "User-Agent": "chanlun-analysis/binance"}
    if signed:
        key, secret = _credentials(api_key, api_secret)
        query.setdefault("recvWindow", SIGNED_RECV_WINDOW_MS)
        headers["X-MBX-APIKEY"] = key
        try:
            _sync_server_time_if_stale(base_url)
        except Exception:
            # A failed proactive time read must not turn an otherwise valid
            # signed request into an outage. -1021 still triggers a forced
            # resync and one fresh retry below.
            pass
    elif api_key:
        headers["X-MBX-APIKEY"] = _api_key(api_key)
    request_method = method.upper()
    retry_delays = GET_NETWORK_RETRY_DELAYS if _request_can_retry(request_method, endpoint) else ()
    has_idempotency_key = _has_idempotency_key(query)
    time_sync_attempted = False
    max_attempts = len(retry_delays) + 1 + (1 if signed else 0)
    for attempt in range(max_attempts):
        request_query = dict(query)
        if signed and refresh_timestamp_on_attempt:
            request_query["timestamp"] = _signed_timestamp()
        if signed:
            query_string = urlencode(request_query)
            signature = hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
            query_string = f"{query_string}&signature={signature}"
        else:
            query_string = urlencode(request_query)
        url = f"{base_url}{endpoint}"
        if query_string:
            url = f"{url}?{query_string}"
        request = Request(url, headers=headers, method=request_method)
        try:
            with _direct_opener.open(request, timeout=NETWORK_REQUEST_TIMEOUT_SECONDS) as response:
                return _read_json(response)
        except HTTPError as exc:
            payload = _read_json(exc)
            error = _exchange_error(payload, "交易所请求失败")
            error.status_code = 400 if 400 <= exc.code < 500 else 502
            _annotate_request_error(
                error,
                endpoint=endpoint,
                method=request_method,
                transient=exc.code in TRANSIENT_HTTP_STATUS_CODES,
            )
            if signed and error.exchange_code == -1021 and not time_sync_attempted:
                time_sync_attempted = True
                try:
                    _sync_server_time(base_url)
                    refresh_timestamp_on_attempt = True
                    continue
                except Exception:
                    pass
            if error.is_transient and (
                request_method in {"GET", "DELETE"}
                or has_idempotency_key
                or _request_can_retry(request_method, endpoint)
            ):
                # A 5xx response may have been produced after Binance accepted
                # the request. Repeating GET/DELETE or a client-id request
                # through the second transport is safe.
                try:
                    return _curl_request_json(url, method=request_method, headers=headers)
                except BinanceApiError as fallback_error:
                    _annotate_request_error(
                        fallback_error,
                        endpoint=endpoint,
                        method=request_method,
                        transient=fallback_error.status_code >= 500,
                    )
                except (TimeoutError, URLError, OSError):
                    pass
            if error.is_transient and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise error from exc
        except BinanceApiError as exc:
            if signed and exc.exchange_code == -1021 and not time_sync_attempted:
                time_sync_attempted = True
                try:
                    _sync_server_time(base_url)
                    refresh_timestamp_on_attempt = True
                    continue
                except Exception:
                    pass
            raise
        except (TimeoutError, URLError, IncompleteRead, RemoteDisconnected) as exc:
            if (
                _is_tls_transport_error(exc)
                or has_idempotency_key
                or request_method == "DELETE"
                or attempt >= len(retry_delays)
            ):
                try:
                    return _curl_request_json(url, method=request_method, headers=headers)
                except BinanceApiError as curl_error:
                    _annotate_request_error(
                        curl_error,
                        endpoint=endpoint,
                        method=request_method,
                        transient=curl_error.status_code >= 500,
                        transport=True,
                    )
                    if signed and curl_error.exchange_code == -1021 and not time_sync_attempted:
                        time_sync_attempted = True
                        try:
                            _sync_server_time(base_url)
                            refresh_timestamp_on_attempt = True
                            continue
                        except Exception:
                            pass
                    raise
                except (TimeoutError, URLError, OSError):
                    pass
            if attempt >= len(retry_delays):
                raise _annotate_request_error(
                    BinanceApiError(CONNECTION_ERROR_MESSAGE),
                    endpoint=endpoint,
                    method=request_method,
                    transient=True,
                    transport=True,
                ) from exc
            time.sleep(retry_delays[attempt])


def get_ticker(network: str, symbol: str) -> dict:
    safe_symbol = _symbol(symbol)
    cache_key = ("ticker", str(network or "").lower(), safe_symbol)
    try:
        payload = _request(network, "/api/v3/ticker/24hr", {"symbol": safe_symbol})
        if not isinstance(payload, dict):
            raise BinanceApiError("交易所返回的行情格式无效")
        result = {
            "symbol": payload.get("symbol"),
            "lastPrice": payload.get("lastPrice"),
            "priceChange": payload.get("priceChange"),
            "priceChangePercent": payload.get("priceChangePercent"),
            "highPrice": payload.get("highPrice"),
            "lowPrice": payload.get("lowPrice"),
            "volume": payload.get("volume"),
            "quoteVolume": payload.get("quoteVolume"),
            "closeTime": payload.get("closeTime"),
            "stale": False,
        }
        _market_cache_set(cache_key, result)
        return result
    except BinanceApiError as exc:
        if str(exc) == CONNECTION_ERROR_MESSAGE:
            cached = _market_cache_get(cache_key)
            if cached is not None:
                cached["stale"] = True
                return cached
        raise


def _cached_public_request(key: tuple[object, ...], loader):
    try:
        payload = loader()
        _market_cache_set(key, payload)
        return payload, False
    except BinanceApiError as exc:
        if str(exc) != CONNECTION_ERROR_MESSAGE:
            raise
        cached = _market_cache_get(key)
        if cached is None:
            raise
        return cached, True


def get_spot_markets(network: str = "mainnet", limit: int = 100) -> dict:
    """Return the highest-turnover public USDT spot pairs."""

    network_key = str(network or "").lower()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-spot-market") as executor:
        exchange_info_future = executor.submit(
            _cached_public_request,
            ("spot-exchange-info", network_key),
            lambda: _request(network, "/api/v3/exchangeInfo"),
        )
        tickers_future = executor.submit(
            _cached_public_request,
            ("spot-ticker-24h", network_key),
            lambda: _request(network, "/api/v3/ticker/24hr"),
        )
        exchange_info, info_stale = exchange_info_future.result()
        tickers, tickers_stale = tickers_future.result()
    if not isinstance(exchange_info, dict):
        raise BinanceApiError("交易所返回的现货信息格式无效")
    if isinstance(tickers, dict):
        tickers = [tickers]
    if not isinstance(tickers, list):
        raise BinanceApiError("交易所返回的现货行情格式无效")

    tickers_by_symbol = {str(item.get("symbol") or ""): item for item in tickers if isinstance(item, dict)}
    items = []
    for market in exchange_info.get("symbols") or []:
        if not isinstance(market, dict):
            continue
        symbol = str(market.get("symbol") or "")
        if (
            market.get("status") != "TRADING"
            or market.get("quoteAsset") != "USDT"
            or market.get("isSpotTradingAllowed") is False
            or not symbol
        ):
            continue
        ticker = tickers_by_symbol.get(symbol)
        if not ticker:
            continue
        items.append(
            {
                "symbol": symbol,
                "contractType": "SPOT",
                "status": "TRADING",
                "baseAsset": market.get("baseAsset"),
                "lastPrice": ticker.get("lastPrice"),
                "priceChange": ticker.get("priceChange"),
                "priceChangePercent": ticker.get("priceChangePercent"),
                "highPrice": ticker.get("highPrice"),
                "lowPrice": ticker.get("lowPrice"),
                "volume": ticker.get("volume"),
                "quoteVolume": ticker.get("quoteVolume"),
                "closeTime": ticker.get("closeTime"),
                "markPrice": None,
                "fundingRate": None,
            }
        )
    items.sort(key=lambda item: _as_number(item.get("quoteVolume")), reverse=True)
    safe_limit = min(max(int(limit or 100), 1), 100)
    items = items[:safe_limit]
    return {
        "items": items,
        "limit": safe_limit,
        "stale": bool(info_stale or tickers_stale),
        "updatedAt": int(time.time() * 1000),
    }


def get_klines(
    network: str,
    symbol: str,
    interval: str = "1h",
    limit: int = 72,
    *,
    with_meta: bool = False,
) -> list[dict] | dict:
    safe_symbol = _symbol(symbol)
    safe_interval = _interval(interval)
    safe_limit = min(max(int(limit or 72), 10), 500)
    cache_key = ("klines", str(network or "").lower(), safe_symbol, safe_interval, safe_limit)
    try:
        payload = _request(
            network,
            "/api/v3/klines",
            {"symbol": safe_symbol, "interval": safe_interval, "limit": safe_limit},
        )
        if not isinstance(payload, list):
            raise BinanceApiError("交易所返回的 K 线格式无效")
        items = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            items.append(
                {
                    "openTime": row[0],
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                    "closeTime": row[6],
                }
            )
        _market_cache_set(cache_key, items)
        return {"items": items, "stale": False} if with_meta else items
    except BinanceApiError as exc:
        if str(exc) == CONNECTION_ERROR_MESSAGE:
            cached = _market_cache_get(cache_key)
            if cached is not None:
                return {"items": cached, "stale": True} if with_meta else cached
        raise


def get_account(network: str, api_key: str, api_secret: str) -> dict:
    payload = _request(network, "/api/v3/account", api_key=api_key, api_secret=api_secret, signed=True)
    if not isinstance(payload, dict):
        raise BinanceApiError("交易所返回的账户格式无效")
    balances = []
    for item in payload.get("balances") or []:
        if not isinstance(item, dict):
            continue
        try:
            free = float(item.get("free") or 0)
            locked = float(item.get("locked") or 0)
        except (TypeError, ValueError):
            continue
        if free == 0 and locked == 0:
            continue
        balances.append({"asset": item.get("asset"), "free": free, "locked": locked})
    balances.sort(key=lambda item: (item["asset"] != "USDT", item["asset"]))
    return {
        "accountType": payload.get("accountType"),
        "canTrade": bool(payload.get("canTrade")),
        "canWithdraw": bool(payload.get("canWithdraw")),
        "canDeposit": bool(payload.get("canDeposit")),
        "updateTime": payload.get("updateTime"),
        "balances": balances,
    }
