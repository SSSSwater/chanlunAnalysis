from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from http.client import IncompleteRead, RemoteDisconnected
from threading import Lock, RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

try:
    from . import binance_futures_websocket_api
    from .binance_client import (
        BinanceApiError,
        CONNECTION_ERROR_MESSAGE,
        GET_NETWORK_RETRY_DELAYS,
        TRANSIENT_HTTP_STATUS_CODES,
        _curl_request_json,
        _direct_opener,
        _annotate_request_error,
        _has_idempotency_key,
        _request_can_retry,
        _is_tls_transport_error,
    )
except ImportError:
    import binance_futures_websocket_api
    from binance_client import (
        BinanceApiError,
        CONNECTION_ERROR_MESSAGE,
        GET_NETWORK_RETRY_DELAYS,
        TRANSIENT_HTTP_STATUS_CODES,
        _curl_request_json,
        _direct_opener,
        _annotate_request_error,
        _has_idempotency_key,
        _request_can_retry,
        _is_tls_transport_error,
    )


FUTURES_MAINNET_BASE_URL = "https://fapi.binance.com"
FUTURES_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
FUTURES_NETWORKS = {"mainnet": FUTURES_MAINNET_BASE_URL, "testnet": FUTURES_TESTNET_BASE_URL}
# Binance has returned a small number of valid futures symbols containing
# Unicode letters. Keep the length and alphanumeric constraints, but do not
# reject a symbol solely because it is outside ASCII; urlencode() safely
# encodes it in the request query.
SYMBOL_PATTERN = re.compile(r"^[^\W_]{5,20}$", re.UNICODE)
INTERVAL_PATTERN = re.compile(r"^(1m|5m|15m|30m|1h|2h|4h|6h|8h|12h|1d|3d|1w|1M)$")
PUBLIC_CACHE_TTL_SECONDS = 30
# Target discovery consumes completed candles only. Keeping those immutable
# bars for a short extra window avoids a second full multi-timeframe download
# after a slow scan while the live quote catalog continues to use the tighter
# public-data TTL above.
SCAN_KLINE_CACHE_TTL_SECONDS = 90
FUTURES_MARKET_LIMIT = 100
MAX_FUTURES_LEVERAGE = 20
NETWORK_REQUEST_TIMEOUT_SECONDS = 10
SIGNED_RECV_WINDOW_MS = 10_000
SERVER_TIME_SYNC_MAX_AGE_SECONDS = 30

_cache: dict[tuple[object, ...], tuple[float, object]] = {}
_cache_lock = Lock()
_protection_update_lock = RLock()
ORDER_RECOVERY_RETRY_DELAYS = (0.25, 0.75)
# The configured local proxy completes TLS reliably through the system curl
# transport, while Python's urllib can spend its full handshake timeout before
# falling back. These are public, idempotent reads used by target discovery;
# send them through curl first so one stalled TLS handshake cannot multiply
# across every symbol and timeframe in a market scan.
REST_CURL_FIRST_PUBLIC_GET_ENDPOINTS = frozenset({
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/klines",
})
REST_CURL_FIRST_POST_ENDPOINTS = frozenset({"/fapi/v1/leverage", "/fapi/v1/order"})
_server_time_offset_ms = 0
_server_time_last_sync_monotonic = 0.0
_server_time_lock = Lock()
_server_time_sync_lock = Lock()
_use_websocket_api = ContextVar("binance_futures_use_websocket_api", default=False)
# Binance Futures WebSocket API supports order operations but does not expose
# REST exchangeInfo. Keep only rules that have been explicitly verified for
# a locally supported WebSocket-only contract; do not guess precision.
_WEBSOCKET_LOCAL_CONTRACT_RULES = {
    "BTCUSDT": {
        "PRICE_FILTER": (Decimal("0.1"), Decimal("0.1"), None),
        "LOT_SIZE": (Decimal("0.001"), Decimal("0.001"), Decimal("1000")),
        "MARKET_LOT_SIZE": (Decimal("0.001"), Decimal("0.001"), Decimal("1000")),
    },
}
_websocket_inferred_rules: dict[tuple[str, str], dict[str, tuple[Decimal, Decimal | None, Decimal | None]]] = {}
# Compatibility hook for focused transport tests. REST uses the explicitly
# configured local proxy instead of urllib's ambient environment settings.
urlopen = _direct_opener.open


class BinanceEntryOrderError(BinanceApiError):
    """An error raised while preparing or submitting the real entry order."""

    def __init__(
        self,
        message: str,
        status_code: int = 502,
        exchange_code: int | None = None,
        *,
        entry_order_submitted: bool = True,
    ):
        super().__init__(message, status_code=status_code, exchange_code=exchange_code)
        # A leverage/configuration failure happens before the entry request;
        # the caller can safely remove the pending local monitor in that case.
        self.entry_order_submitted = bool(entry_order_submitted)


@contextmanager
def websocket_api_requests(enabled: bool):
    token = _use_websocket_api.set(bool(enabled))
    try:
        yield
    finally:
        _use_websocket_api.reset(token)


def _network_url(network: str) -> str:
    value = str(network or "").strip().lower()
    if value not in FUTURES_NETWORKS:
        raise ValueError("网络必须选择主网或测试网")
    return FUTURES_NETWORKS[value]


def _symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError("合约交易对格式无效")
    return value


def _interval(interval: str) -> str:
    value = str(interval or "").strip()
    if not INTERVAL_PATTERN.fullmatch(value):
        raise ValueError("合约 K 线周期无效")
    return value


def _copy_payload(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: _copy_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_copy_payload(item) for item in payload]
    return payload


def _cache_get(key: tuple[object, ...], *, max_age_seconds: float = PUBLIC_CACHE_TTL_SECONDS) -> object | None:
    with _cache_lock:
        cached = _cache.get(key)
        if cached is None:
            return None
        stored_at, payload = cached
        if time.monotonic() - stored_at > max(0.0, float(max_age_seconds)):
            _cache.pop(key, None)
            return None
        return _copy_payload(payload)


def _cache_set(key: tuple[object, ...], payload: object) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), _copy_payload(payload))


def _read_json(response) -> object:
    try:
        return json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


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
        f"{base_url}/fapi/v1/time",
        headers={"Accept": "application/json", "User-Agent": "chanlun-analysis/binance-futures"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=NETWORK_REQUEST_TIMEOUT_SECONDS) as response:
            payload = _read_json(response)
    except (TimeoutError, URLError, IncompleteRead, RemoteDisconnected) as exc:
        if not _is_tls_transport_error(exc):
            raise
        payload = _curl_request_json(
            f"{base_url}/fapi/v1/time",
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "chanlun-analysis/binance-futures"},
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
    if _use_websocket_api.get():
        return binance_futures_websocket_api.request(
            network, endpoint, params, method=method, api_key=api_key, api_secret=api_secret, signed=signed
        )
    base_url = _network_url(network)
    query_params = dict(params or {})
    refresh_timestamp_on_attempt = "timestamp" not in query_params
    headers = {"Accept": "application/json", "User-Agent": "chanlun-analysis/binance-futures"}
    if signed:
        key, secret = _credentials(api_key, api_secret)
        query_params.setdefault("recvWindow", SIGNED_RECV_WINDOW_MS)
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
    has_idempotency_key = _has_idempotency_key(query_params)
    time_sync_attempted = False
    max_attempts = len(retry_delays) + 1 + (1 if signed else 0)
    for attempt in range(max_attempts):
        request_query = dict(query_params)
        if signed and refresh_timestamp_on_attempt:
            request_query["timestamp"] = _signed_timestamp()
        if signed:
            query = urlencode(request_query)
            signature = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
        else:
            query = urlencode(request_query)
        url = f"{base_url}{endpoint}"
        if query:
            url = f"{url}?{query}"
        request = Request(url, headers=headers, method=request_method)
        try:
            # The workstation proxy occasionally stalls or terminates
            # Python's TLS handshake while the REST curl transport remains
            # usable. Use curl first for public scan reads and the two real
            # entry steps; keep urllib as a fallback when curl itself fails.
            curl_first = (
                (request_method == "POST" and endpoint in REST_CURL_FIRST_POST_ENDPOINTS)
                or (request_method == "GET" and not signed and endpoint in REST_CURL_FIRST_PUBLIC_GET_ENDPOINTS)
            )
            if curl_first:
                try:
                    return _curl_request_json(url, method=request_method, headers=headers)
                except BinanceApiError as curl_error:
                    if not (curl_error.status_code >= 500 or curl_error.is_transport_error or curl_error.is_transient):
                        raise
                except (TimeoutError, URLError, OSError):
                    pass
            with urlopen(request, timeout=NETWORK_REQUEST_TIMEOUT_SECONDS) as response:
                return _read_json(response)
        except HTTPError as exc:
            error = _exchange_error(_read_json(exc), "交易所请求失败")
            # Keep Binance's throttling response distinguishable from ordinary
            # request errors so history jobs can stop immediately on a limit.
            error.status_code = 429 if exc.code == 429 else (400 if 400 <= exc.code < 500 else 502)
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
                # A 5xx response may arrive after Binance accepted the order.
                # Reuse the same client id through the alternate transport so
                # the operation can reconcile instead of creating a duplicate.
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


def _cached_read(key: tuple[object, ...], loader, *, max_age_seconds: float = PUBLIC_CACHE_TTL_SECONDS):
    # The short public-data TTL is a freshness budget, not only an outage
    # fallback. Re-reading a successful K-line or market catalogue within it
    # sends the entire target scan back through the local proxy for no new
    # information and makes a repeated click unnecessarily slow.
    cached = _cache_get(key, max_age_seconds=max_age_seconds)
    if cached is not None:
        return cached, False
    try:
        payload = loader()
        _cache_set(key, payload)
        return payload, False
    except BinanceApiError as exc:
        if str(exc) != CONNECTION_ERROR_MESSAGE:
            raise
        cached = _cache_get(key, max_age_seconds=max_age_seconds)
        if cached is None:
            raise
        return cached, True


def _exchange_info(network: str) -> tuple[dict, bool]:
    if _use_websocket_api.get():
        symbols = []
        for symbol, rules in _WEBSOCKET_LOCAL_CONTRACT_RULES.items():
            filters = []
            for filter_type, (step, minimum, maximum) in rules.items():
                if filter_type == "PRICE_FILTER":
                    filters.append({
                        "filterType": filter_type,
                        "tickSize": str(step),
                        "minPrice": str(minimum) if minimum else "0",
                        "maxPrice": str(maximum) if maximum else "0",
                    })
                else:
                    filters.append({
                        "filterType": filter_type,
                        "stepSize": str(step),
                        "minQty": str(minimum) if minimum else "0",
                        "maxQty": str(maximum) if maximum else "0",
                    })
            symbols.append({"symbol": symbol, "filters": filters})
        return {"symbols": symbols}, True
    payload, stale = _cached_read(
        ("exchange-info", str(network or "").lower()),
        lambda: _request(network, "/fapi/v1/exchangeInfo"),
    )
    if not isinstance(payload, dict):
        raise BinanceApiError("交易所返回的合约信息格式无效")
    return payload, stale


def _decimal_observation_step(values: list[object]) -> Decimal | None:
    """Return a conservative decimal grid inferred from observed values."""

    decimals = []
    for value in values:
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if decimal > 0 and decimal.is_finite():
            decimals.append(decimal)
    if not decimals:
        return None
    exponent = min(item.as_tuple().exponent for item in decimals)
    scale = Decimal(10) ** -exponent
    units = [int((item * scale).to_integral_value()) for item in decimals]
    divisor = 0
    for unit in units:
        divisor = math.gcd(divisor, abs(unit))
    if divisor <= 0:
        return None
    return Decimal(divisor) / scale


def _websocket_inferred_contract_rules(network: str, symbol: str) -> dict[str, tuple[Decimal, Decimal | None, Decimal | None]] | None:
    key = (str(network or "mainnet").lower(), str(symbol or "").upper())
    cached = _websocket_inferred_rules.get(key)
    if cached:
        return cached
    try:
        from . import database as db
    except ImportError:  # pragma: no cover
        import database as db
    rows = []
    for interval in ("1m", "5m", "15m"):
        try:
            rows = db.list_latest_binance_futures_history_klines(key[0], key[1], interval, 240)
        except Exception:
            rows = []
        if rows:
            break
    if not rows:
        return None
    price_step = _decimal_observation_step([
        item.get(field) for item in rows for field in ("open", "high", "low", "close")
    ])
    quantity_step = _decimal_observation_step([item.get("volume") for item in rows])
    if price_step is None or quantity_step is None:
        return None
    rules = {
        "PRICE_FILTER": (price_step, None, None),
        "LOT_SIZE": (quantity_step, None, None),
        "MARKET_LOT_SIZE": (quantity_step, None, None),
    }
    _websocket_inferred_rules[key] = rules
    return rules


def _ticker_24h(network: str) -> tuple[list[dict], bool]:
    payload, stale = _cached_read(
        ("ticker-24h", str(network or "").lower()),
        lambda: _request(network, "/fapi/v1/ticker/24hr"),
    )
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的合约行情格式无效")
    return [item for item in payload if isinstance(item, dict)], stale


def _premium_index(network: str) -> tuple[list[dict], bool]:
    payload, stale = _cached_read(
        ("premium-index", str(network or "").lower()),
        lambda: _request(network, "/fapi/v1/premiumIndex"),
    )
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的标记价格格式无效")
    return [item for item in payload if isinstance(item, dict)], stale


def _contract_quantity_rules(contract: dict) -> dict[str, object]:
    filters = [item for item in contract.get("filters") or [] if isinstance(item, dict)]
    lot_size = next((item for item in filters if item.get("filterType") == "LOT_SIZE"), {})
    market_lot_size = next(
        (item for item in filters if item.get("filterType") == "MARKET_LOT_SIZE"),
        lot_size,
    )
    try:
        market_step = Decimal(str(market_lot_size.get("stepSize") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        market_step = Decimal("0")
    if market_step <= 0:
        market_lot_size = lot_size
    return {
        "quantityStep": lot_size.get("stepSize"),
        "minQuantity": lot_size.get("minQty"),
        "maxQuantity": lot_size.get("maxQty"),
        "marketQuantityStep": market_lot_size.get("stepSize"),
        "marketMinQuantity": market_lot_size.get("minQty"),
        "marketMaxQuantity": market_lot_size.get("maxQty"),
    }


def _futures_market_items(network: str) -> tuple[list[dict], bool]:
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="binance-futures-market") as executor:
        exchange_info_future = executor.submit(_exchange_info, network)
        tickers_future = executor.submit(_ticker_24h, network)
        premiums_future = executor.submit(_premium_index, network)
        exchange_info, info_stale = exchange_info_future.result()
        tickers, tickers_stale = tickers_future.result()
        premiums, premiums_stale = premiums_future.result()
    tickers_by_symbol = {str(item.get("symbol") or ""): item for item in tickers}
    premiums_by_symbol = {str(item.get("symbol") or ""): item for item in premiums}
    items = []
    for contract in exchange_info.get("symbols") or []:
        if not isinstance(contract, dict):
            continue
        symbol = str(contract.get("symbol") or "")
        if (
            contract.get("contractType") != "PERPETUAL"
            or contract.get("quoteAsset") != "USDT"
            or contract.get("status") != "TRADING"
            or not symbol
        ):
            continue
        ticker = tickers_by_symbol.get(symbol)
        if not ticker:
            continue
        premium = premiums_by_symbol.get(symbol, {})
        items.append(
            {
                "symbol": symbol,
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": contract.get("baseAsset"),
                "lastPrice": ticker.get("lastPrice"),
                "priceChange": ticker.get("priceChange"),
                "priceChangePercent": ticker.get("priceChangePercent"),
                "highPrice": ticker.get("highPrice"),
                "lowPrice": ticker.get("lowPrice"),
                "volume": ticker.get("volume"),
                "quoteVolume": ticker.get("quoteVolume"),
                "closeTime": ticker.get("closeTime"),
                "markPrice": premium.get("markPrice"),
                "indexPrice": premium.get("indexPrice"),
                "fundingRate": premium.get("lastFundingRate"),
                "nextFundingTime": premium.get("nextFundingTime"),
                **_contract_quantity_rules(contract),
                "onboardDate": contract.get("onboardDate"),
                "deliveryDate": contract.get("deliveryDate"),
            }
        )
    items.sort(key=lambda item: _as_number(item.get("quoteVolume")), reverse=True)
    return items, bool(info_stale or tickers_stale or premiums_stale)


def get_futures_markets(network: str = "mainnet", limit: int = FUTURES_MARKET_LIMIT) -> dict:
    """Return the highest-turnover public USDT perpetual quotes."""

    items, stale = _futures_market_items(network)
    safe_limit = min(max(int(limit or FUTURES_MARKET_LIMIT), 1), FUTURES_MARKET_LIMIT)
    items = items[:safe_limit]
    return {
        "items": items,
        "limit": safe_limit,
        "stale": stale,
        "updatedAt": int(time.time() * 1000),
    }


def get_all_futures_markets(network: str = "mainnet") -> dict:
    """Return every currently trading USDT perpetual used by the local backtest corpus."""

    items, stale = _futures_market_items(network)
    return {
        "items": items,
        "limit": len(items),
        "stale": stale,
        "updatedAt": int(time.time() * 1000),
    }


def get_futures_klines(network: str, symbol: str, interval: str, limit: int = 180, *, with_meta: bool = False) -> list[dict] | dict:
    safe_symbol = _symbol(symbol)
    safe_interval = _interval(interval)
    safe_limit = min(max(int(limit or 180), 80), 500)
    cache_key = (
        "futures-klines",
        "websocket" if _use_websocket_api.get() else "rest",
        str(network or "").lower(),
        safe_symbol,
        safe_interval,
        safe_limit,
    )

    def load():
        payload = _request(network, "/fapi/v1/klines", {"symbol": safe_symbol, "interval": safe_interval, "limit": safe_limit})
        if not isinstance(payload, list):
            raise BinanceApiError("交易所返回的合约 K 线格式无效")
        result = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            result.append(
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
        return result

    items, stale = _cached_read(cache_key, load, max_age_seconds=SCAN_KLINE_CACHE_TTL_SECONDS)
    if not isinstance(items, list):
        raise BinanceApiError("交易所返回的合约 K 线格式无效")
    return {"items": items, "stale": stale} if with_meta else items




def get_futures_historical_klines(
    network: str,
    symbol: str,
    interval: str,
    start_time: int,
    end_time: int,
    *,
    limit: int = 1500,
    with_meta: bool = False,
) -> list[dict] | dict:
    """Load one bounded historical futures K-line page for local replay only."""

    safe_symbol = _symbol(symbol)
    safe_interval = _interval(interval)
    try:
        safe_start = int(start_time)
        safe_end = int(end_time)
    except (TypeError, ValueError) as exc:
        raise ValueError("历史 K 线时间范围无效") from exc
    if safe_start <= 0 or safe_end <= safe_start:
        raise ValueError("历史 K 线时间范围无效")
    safe_limit = min(max(int(limit or 1500), 1), 1500)
    cache_key = (
        "futures-historical-klines",
        str(network or "").lower(),
        safe_symbol,
        safe_interval,
        safe_start,
        safe_end,
        safe_limit,
    )

    def load():
        payload = _request(
            network,
            "/fapi/v1/klines",
            {
                "symbol": safe_symbol,
                "interval": safe_interval,
                "startTime": safe_start,
                "endTime": safe_end,
                "limit": safe_limit,
            },
        )
        if not isinstance(payload, list):
            raise BinanceApiError("交易所返回的历史合约 K 线格式无效")
        result = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 7:
                continue
            result.append(
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
        return result

    items, stale = _cached_read(cache_key, load)
    if not isinstance(items, list):
        raise BinanceApiError("交易所返回的历史合约 K 线格式无效")
    return {"items": items, "stale": stale} if with_meta else items


_PROTECTION_ORDER_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT", "LIMIT"}


def _decorate_futures_protection_position(
    position: dict,
    orders: list[dict],
    *,
    protection_state: str = "CONFIRMED",
    checked_at: int | None = None,
) -> dict:
    """Attach the exchange's current protection orders to one live position."""

    decorated = dict(position)
    matching_orders = _matching_protection_orders(
        decorated,
        orders,
    )
    is_long = decorated.get("side") == "LONG"
    decorated["protectionOrders"] = matching_orders
    decorated["positionStopLoss"] = _protection_price(
        matching_orders,
        "STOP",
        close_position=True,
        is_long=is_long,
    )
    decorated["positionTakeProfit"] = _protection_price(
        matching_orders,
        "TAKE_PROFIT",
        close_position=True,
        is_long=is_long,
    )
    decorated["partialStopLoss"] = _protection_price(
        matching_orders,
        "STOP",
        close_position=False,
        is_long=is_long,
    )
    take_profit_levels = _partial_take_profit_levels(decorated)
    decorated["partialTakeProfitLevels"] = take_profit_levels
    first_take_profit_level = take_profit_levels[0] if take_profit_levels else None
    decorated["partialTakeProfit"] = first_take_profit_level["price"] if first_take_profit_level else None
    partial_quantity = first_take_profit_level["quantity"] if first_take_profit_level else None
    decorated["partialQuantity"] = partial_quantity
    decorated["partialQuantityRatio"] = (
        round(partial_quantity / decorated["quantity"] * 100, 2)
        if partial_quantity and decorated.get("quantity")
        else None
    )
    # Keep the old single-level aliases for consumers that have not migrated.
    decorated["stopLoss"] = decorated["positionStopLoss"]
    decorated["takeProfit"] = decorated["positionTakeProfit"]
    decorated["protectionState"] = str(protection_state or "UNKNOWN").strip().upper()
    decorated["protectionCheckedAt"] = checked_at or int(time.time() * 1000)
    return decorated


def get_futures_account(network: str, api_key: str, api_secret: str) -> dict:
    payload = _request(
        network,
        "/fapi/v2/account",
        api_key=api_key,
        api_secret=api_secret,
        signed=True,
    )
    if not isinstance(payload, dict):
        raise BinanceApiError("交易所返回的合约账户格式无效")

    raw_positions = payload.get("positions") or []
    position_risk_error = None
    protection_error = None
    algo_orders = []
    standard_orders = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="binance-futures-account") as executor:
        risk_future = executor.submit(_get_position_risk, network, api_key, api_secret)
        algo_future = executor.submit(_get_open_algo_orders, network, api_key, api_secret)
        standard_future = executor.submit(_get_open_futures_orders, network, api_key, api_secret, None)
        ticker_future = executor.submit(_ticker_24h, network)
        try:
            risk_positions = risk_future.result()
            if risk_positions:
                # positionRisk has the freshest mark/entry/liquidation data,
                # while /account carries unrealizedProfit and the margin
                # basis used for ROE.  Replacing the account positions with
                # positionRisk silently turns ROE into zero or an unstable
                # notional/leverage estimate.  Merge only fields owned by the
                # risk endpoint and retain the account's PnL/margin fields.
                risk_by_key = {
                    (
                        str(item.get("symbol") or "").upper(),
                        str(item.get("positionSide") or "BOTH").upper(),
                    ): item
                    for item in risk_positions
                    if isinstance(item, dict)
                }
                merged_positions = []
                for account_position in raw_positions:
                    if not isinstance(account_position, dict):
                        continue
                    key = (
                        str(account_position.get("symbol") or "").upper(),
                        str(account_position.get("positionSide") or "BOTH").upper(),
                    )
                    risk_position = risk_by_key.get(key)
                    if not risk_position:
                        merged_positions.append(account_position)
                        continue
                    merged = dict(account_position)
                    for field in (
                        "positionAmt",
                        "entryPrice",
                        "markPrice",
                        "liquidationPrice",
                        "leverage",
                        "maxNotionalValue",
                        "marginType",
                        "isolatedMargin",
                        "positionSide",
                        "notional",
                        "updateTime",
                    ):
                        value = risk_position.get(field)
                        if value is not None and value != "":
                            merged[field] = value
                    merged_positions.append(merged)
                account_keys = {
                    (
                        str(item.get("symbol") or "").upper(),
                        str(item.get("positionSide") or "BOTH").upper(),
                    )
                    for item in merged_positions
                    if isinstance(item, dict)
                }
                merged_positions.extend(
                    item
                    for item in risk_positions
                    if isinstance(item, dict)
                    and (
                        str(item.get("symbol") or "").upper(),
                        str(item.get("positionSide") or "BOTH").upper(),
                    ) not in account_keys
                )
                raw_positions = merged_positions
        except BinanceApiError as exc:
            position_risk_error = str(exc)
        try:
            algo_orders = algo_future.result()
        except Exception as exc:
            protection_error = str(exc)
        try:
            standard_orders = standard_future.result()
        except Exception as exc:
            if protection_error:
                protection_error = f"{protection_error}；普通委托：{exc}"
            else:
                protection_error = f"普通委托：{exc}"
        try:
            tickers, _ticker_stale = ticker_future.result()
        except Exception:
            # The account snapshot remains useful when the optional public
            # ticker read is unavailable.  Mark price and account PnL still
            # come from the authenticated endpoints.
            tickers = []

    latest_prices = {
        str(item.get("symbol") or "").upper(): item.get("lastPrice")
        for item in tickers
        if isinstance(item, dict) and item.get("lastPrice") not in (None, "")
    }
    if latest_prices:
        raw_positions = [
            {
                **raw_position,
                "lastPrice": latest_prices.get(
                    str(raw_position.get("symbol") or "").upper(),
                    raw_position.get("lastPrice"),
                ),
            }
            if isinstance(raw_position, dict)
            else raw_position
            for raw_position in raw_positions
        ]

    protection_state = "CONFIRMED" if not protection_error else "UNKNOWN"
    protection_checked_at = int(time.time() * 1000)
    position_quantity_rules: dict[str, dict[str, object]] = {}
    try:
        exchange_info, _ = _exchange_info(network)
        position_quantity_rules = {
            str(contract.get("symbol") or "").upper(): _contract_quantity_rules(contract)
            for contract in exchange_info.get("symbols") or []
            if isinstance(contract, dict) and contract.get("symbol")
        }
    except Exception:
        # Quantity rules are useful for the UI, but must not make an account
        # snapshot unavailable when the public exchange-info request is down.
        position_quantity_rules = {}
    positions = []
    for raw_position in raw_positions:
        position = _futures_position_item(
            raw_position,
            position_quantity_rules.get(str(raw_position.get("symbol") or "").upper())
            if isinstance(raw_position, dict)
            else None,
        )
        if not position:
            continue
        positions.append(
            _decorate_futures_protection_position(
                position,
                [*algo_orders, *standard_orders],
                protection_state=protection_state,
                checked_at=protection_checked_at,
            )
        )

    realized_pnl_by_position, realized_pnl_failed_symbols = _realized_pnl_by_position(
        network,
        api_key,
        api_secret,
        positions,
    )
    for position in positions:
        symbol = str(position.get("symbol") or "").strip().upper()
        position_side = str(position.get("positionSide") or "BOTH").strip().upper()
        position["realizedPnl"] = (
            None
            if symbol in realized_pnl_failed_symbols
            else round(realized_pnl_by_position.get((symbol, position_side), 0.0), 8)
        )

    assets = []
    for raw_asset in payload.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        wallet_balance = _as_number(raw_asset.get("walletBalance"))
        unrealized_profit = _as_number(raw_asset.get("unrealizedProfit"))
        margin_balance = _as_number(raw_asset.get("marginBalance"))
        available_balance = _as_number(raw_asset.get("availableBalance"))
        if abs(wallet_balance) <= 0 and abs(unrealized_profit) <= 0 and abs(margin_balance) <= 0:
            continue
        assets.append(
            {
                "asset": raw_asset.get("asset"),
                "walletBalance": wallet_balance,
                "unrealizedProfit": unrealized_profit,
                "marginBalance": margin_balance,
                "availableBalance": available_balance,
                "initialMargin": _as_number(raw_asset.get("initialMargin")),
                "maintMargin": _as_number(raw_asset.get("maintMargin")),
                "maxWithdrawAmount": _as_number(raw_asset.get("maxWithdrawAmount")),
            }
        )
    assets.sort(key=lambda item: (item["asset"] != "USDT", item["asset"] or ""))
    positions.sort(key=lambda item: item["notional"], reverse=True)
    return {
        "available": True,
        "accountType": payload.get("accountType"),
        "canTrade": bool(payload.get("canTrade")),
        "canDeposit": bool(payload.get("canDeposit")),
        "canWithdraw": bool(payload.get("canWithdraw")),
        "updateTime": payload.get("updateTime"),
        "assets": assets,
        "positions": positions,
        "totalWalletBalance": _as_number(payload.get("totalWalletBalance")),
        "totalUnrealizedProfit": _as_number(payload.get("totalUnrealizedProfit")),
        "totalMarginBalance": _as_number(payload.get("totalMarginBalance")),
        "availableBalance": _as_number(payload.get("availableBalance")),
        "openOrderCount": len(algo_orders) + len(standard_orders),
        "openStandardOrderCount": len(standard_orders),
        "openAlgoOrderCount": len(algo_orders),
        "positionRiskError": position_risk_error,
        "protectionError": protection_error,
        "protectionState": protection_state,
        "protectionCheckedAt": protection_checked_at,
    }


def get_futures_protection_state(
    network: str,
    api_key: str,
    api_secret: str,
    positions: list[dict] | None,
) -> dict:
    """Read open exit orders for a cached account position set.

    Binance's Futures WebSocket API does not expose an ``openAlgoOrders``
    status method. Read both order families over the proxied REST endpoints,
    regardless of the caller-selected account transport.
    """

    def read(loader, *args):
        # ContextVars do not propagate into ThreadPoolExecutor workers. Make
        # the REST-only contract explicit for every worker thread.
        with websocket_api_requests(False):
            return loader(*args)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-protection-read") as executor:
        algo_future = executor.submit(read, _get_open_algo_orders, network, api_key, api_secret)
        standard_future = executor.submit(read, _get_open_futures_orders, network, api_key, api_secret, None)
        algo_orders = algo_future.result()
        standard_orders = standard_future.result()

    checked_at = int(time.time() * 1000)
    all_orders = [*algo_orders, *standard_orders]
    decorated_positions = [
        _decorate_futures_protection_position(
            dict(position),
            all_orders,
            protection_state="CONFIRMED",
            checked_at=checked_at,
        )
        for position in positions or []
        if isinstance(position, dict)
    ]
    return {
        "positions": decorated_positions,
        "openOrderCount": len(all_orders),
        "openStandardOrderCount": len(standard_orders),
        "openAlgoOrderCount": len(algo_orders),
        "protectionState": "CONFIRMED",
        "protectionCheckedAt": checked_at,
    }


def _conditional_order_params(
    symbol: str,
    side: str,
    position_side: str,
    order_type: str,
    trigger_price: Decimal,
) -> dict[str, object]:
    is_take_profit = "TAKE_PROFIT" in str(order_type or "").upper()
    return {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": order_type,
        "triggerPrice": _format_order_price(trigger_price),
        # Risk exits use the fair mark price. Profit exits use the actual
        # traded (contract) price so a wick through a target is not missed
        # merely because mark price lags behind it.
        "workingType": "CONTRACT_PRICE" if is_take_profit else "MARK_PRICE",
        "priceProtect": "false" if is_take_profit else "true",
    }


def _quantity_take_profit_order_type() -> str:
    """Use a quantity-based latest-price conditional exit for every target."""

    return "TAKE_PROFIT_MARKET"


def _cancel_futures_protection_orders(
    network: str,
    api_key: str,
    api_secret: str,
    symbol: str,
    orders: list[dict],
) -> list[object]:
    cancelled_order_ids = []
    for order in orders:
        is_algo = str(order.get("orderSource") or "").upper() == "ALGO" or order.get("algoId") is not None
        order_id = (order.get("algoId") or order.get("orderId")) if is_algo else order.get("orderId")
        if order_id is None:
            continue
        endpoint = "/fapi/v1/algoOrder" if is_algo else "/fapi/v1/order"
        id_params = {"algoId": order_id} if is_algo else {"orderId": order_id}
        try:
            _request(
                network,
                endpoint,
                {"symbol": symbol, **id_params},
                method="DELETE",
                api_key=api_key,
                api_secret=api_secret,
                signed=True,
            )
        except BinanceApiError as exc:
            # A lost response after a successful cancel is commonly followed
            # by Binance -2011 on the retry. The desired final state is already
            # reached, so do not report that as a failed protection update.
            if exc.exchange_code != -2011:
                raise
        cancelled_order_ids.append(order_id)
    return cancelled_order_ids


def _open_protection_orders(
    network: str,
    api_key: str,
    api_secret: str,
    symbol: str,
) -> list[dict]:
    """Read both order families without making legacy accounts brittle.

    Binance exposes conditional exits through ``openAlgoOrders`` and normal
    LIMIT exits through ``openOrders``.  A temporary failure (or an older test
    adapter that only knows one endpoint) must not prevent a new protective
    set from being installed; the replacement code will still reconcile the
    family it can read and the next account refresh will pick up the other.
    """

    orders: list[dict] = []
    try:
        orders.extend(_get_open_algo_orders(network, api_key, api_secret))
    except Exception:
        pass
    try:
        orders.extend(_get_open_futures_orders(network, api_key, api_secret, symbol))
    except Exception:
        pass
    return orders


def _serialize_protection_update(func):
    """Prevent an automatic plan sync and a manual edit from interleaving."""

    @wraps(func)
    def wrapped(*args, **kwargs):
        with _protection_update_lock:
            return func(*args, **kwargs)

    return wrapped


def _place_futures_protection_orders(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    close_side: str,
    position_side: str,
    order_specs: list[tuple[str, str, Decimal, Decimal | None]],
) -> list[dict]:
    """Create a complete replacement set before touching the current set.

    Protection orders belong to the live position.  A replacement therefore
    must not start by deleting the currently active exits: one rejected POST
    would otherwise leave the position without its take-profit or stop-loss.
    If creation fails, remove only orders created by this attempt and let the
    previous set remain active.
    """

    placed_orders: list[dict] = []
    try:
        for role, order_type, trigger_price, quantity in order_specs:
            is_quantity_take_profit = "TAKE_PROFIT" in str(order_type or "").upper()
            if is_quantity_take_profit and quantity is None:
                raise ValueError("数量型止盈必须指定平仓数量")
            client_order_id = f"cl_{uuid.uuid4().hex[:24]}"
            params = _conditional_order_params(symbol, close_side, position_side, order_type, trigger_price)
            params["clientAlgoId"] = client_order_id
            endpoint = "/fapi/v1/algoOrder"
            if quantity is None:
                # Only the position-level stop may close the full remaining
                # position. Profit targets always carry an explicit quantity.
                params["closePosition"] = "true"
            else:
                params["quantity"] = _format_order_quantity(quantity)
                if position_side == "BOTH":
                    params["reduceOnly"] = "true"
            response = None
            try:
                response = _request(
                    network,
                    endpoint,
                    params,
                    method="POST",
                    api_key=api_key,
                    api_secret=api_secret,
                    signed=True,
                )
            except BinanceApiError as exc:
                if not (exc.is_transport_error or exc.status_code >= 500 or exc.is_transient):
                    raise
                response = _recover_open_algo_order(
                    network,
                    api_key,
                    api_secret,
                    symbol=symbol,
                    client_algo_id=client_order_id,
                )
                if response is None:
                    raise
            if (
                not isinstance(response, dict)
                or (response.get("algoId") is None and response.get("orderId") is None)
            ):
                recovered = _recover_open_algo_order(
                    network,
                    api_key,
                    api_secret,
                    symbol=symbol,
                    client_algo_id=client_order_id,
                )
                if recovered is not None:
                    response = recovered
            algo_id = response.get("algoId") if isinstance(response, dict) else None
            order_id = response.get("orderId") if isinstance(response, dict) else None
            if algo_id is None and order_id is None:
                raise BinanceApiError("交易所未返回保护单编号")
            placed_orders.append(
                {
                    "role": role,
                    "algoId": algo_id,
                    "orderId": order_id or algo_id,
                    "clientAlgoId": response.get("clientAlgoId") or client_order_id,
                    "clientOrderId": None,
                    "type": order_type,
                    "orderSource": "ALGO",
                    "price": None,
                    "triggerPrice": float(trigger_price),
                    "stopPrice": float(trigger_price),
                    "quantity": float(quantity) if quantity is not None else None,
                    "closePosition": quantity is None,
                }
            )
    except Exception:
        if placed_orders:
            try:
                _cancel_futures_protection_orders(network, api_key, api_secret, symbol, placed_orders)
            except Exception:
                # The original protection set is still present.  Keep the
                # original exception as the actionable failure for the user.
                pass
        raise
    return placed_orders


def _recover_open_algo_order(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    client_algo_id: str,
) -> dict | None:
    """Find an Algo order accepted before its HTTP response was lost."""

    for attempt in range(len(ORDER_RECOVERY_RETRY_DELAYS) + 1):
        try:
            orders = _get_open_algo_orders(network, api_key, api_secret)
        except Exception:
            orders = []
        for order in orders:
            if str(order.get("clientAlgoId") or "").strip() == client_algo_id and str(order.get("symbol") or "").upper() == symbol:
                return order
        if attempt < len(ORDER_RECOVERY_RETRY_DELAYS):
            time.sleep(ORDER_RECOVERY_RETRY_DELAYS[attempt])
    return None


@_serialize_protection_update
def update_futures_partial_protection(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    stop_loss: float | None,
    take_profit: float | None,
    quantity_ratio: float = 50,
) -> dict:
    """Replace quantity-based conditional exits for a percentage of a position."""

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    try:
        ratio = float(quantity_ratio)
    except (TypeError, ValueError):
        raise ValueError("止盈止损比例必须在 1% 到 100% 之间") from None
    if not math.isfinite(ratio) or ratio <= 0 or ratio > 100:
        raise ValueError("止盈止损比例必须在 1% 到 100% 之间")
    if stop_loss is not None and (not isinstance(stop_loss, (int, float)) or not math.isfinite(stop_loss) or stop_loss <= 0):
        raise ValueError("止损价必须是正数")
    if take_profit is not None and (not isinstance(take_profit, (int, float)) or not math.isfinite(take_profit) or take_profit <= 0):
        raise ValueError("止盈价必须是正数")

    current = _find_futures_position(
        _get_position_risk_with_latest_price(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")
    if stop_loss is None and take_profit is None:
        rounded_stop_loss = rounded_take_profit = None
        partial_quantity = None
    else:
        stop_reference_price, stop_reference_label = _protection_reference_price(position, is_stop=True)
        target_reference_price, target_reference_label = _protection_reference_price(position, is_stop=False)
        is_long = position["side"] == "LONG"
        if stop_loss is not None and ((is_long and stop_loss >= stop_reference_price) or (not is_long and stop_loss <= stop_reference_price)):
            raise ValueError(f"止损价必须位于当前{stop_reference_label}的防守侧")
        if take_profit is not None and ((is_long and take_profit <= target_reference_price) or (not is_long and take_profit >= target_reference_price)):
            raise ValueError(f"止盈价必须位于当前{target_reference_label}的盈利侧")
        tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
        stop_reference_decimal = Decimal(str(stop_reference_price)) if stop_reference_price > 0 else None
        target_reference_decimal = Decimal(str(target_reference_price)) if target_reference_price > 0 else None
        rounded_stop_loss = None
        rounded_take_profit = None
        if stop_loss is not None:
            rounded_stop_loss = _round_price_to_tick(stop_loss, tick_size, ROUND_DOWN if is_long else ROUND_UP)
            _validate_protection_price(rounded_stop_loss, stop_reference_decimal, is_long, is_stop=True, min_price=min_price, max_price=max_price, reference_label=stop_reference_label)
        if take_profit is not None:
            rounded_take_profit = _round_price_to_tick(take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
            _validate_protection_price(rounded_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
        # Both quantity-based exits are conditional market orders. Use the
        # market quantity filter for the stop and latest-price target alike.
        quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(
            network,
            safe_symbol,
            market=True,
        )
        partial_quantity = _round_quantity_to_step(
            position["quantity"] * ratio / 100,
            quantity_step,
            min_quantity,
            max_quantity,
            too_small_message="当前持仓数量不足以按交易所精度设置比例止盈止损",
        )

    actual_position_side = position["positionSide"]
    close_side = "SELL" if position["side"] == "LONG" else "BUY"
    existing_orders = [
        order
        for order in _open_protection_orders(network, api_key, api_secret, safe_symbol)
        if _is_matching_protection_order(order, safe_symbol, actual_position_side, close_side)
        and not _is_close_position_order(order)
    ]
    order_specs = [
        ("PARTIAL_STOP_LOSS", "STOP_MARKET", rounded_stop_loss),
        ("PARTIAL_TAKE_PROFIT", _quantity_take_profit_order_type(), rounded_take_profit),
    ]
    replacement_specs = [
        (role, order_type, trigger_price, partial_quantity)
        for role, order_type, trigger_price in order_specs
        if trigger_price is not None
    ]
    placed_orders = _place_futures_protection_orders(
        network,
        api_key,
        api_secret,
        symbol=safe_symbol,
        close_side=close_side,
        position_side=actual_position_side,
        order_specs=replacement_specs,
    )
    cancelled_order_ids = _cancel_futures_protection_orders(network, api_key, api_secret, safe_symbol, existing_orders)
    return {
        "scope": "PARTIAL",
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "quantityRatio": ratio,
        "quantity": float(partial_quantity) if partial_quantity is not None else None,
        "stopLoss": float(rounded_stop_loss) if rounded_stop_loss is not None else None,
        "takeProfit": float(rounded_take_profit) if rounded_take_profit is not None else None,
        "cancelledOrderIds": cancelled_order_ids,
        "orders": placed_orders,
        "markPrice": position["markPrice"],
    }


@_serialize_protection_update
def update_futures_position_protection(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    stop_loss: float | None,
    take_profit: float | None,
) -> dict:
    """Replace the full-position stop and an optional quantity-based target.

    The stop remains a position-level conditional exit because it must close
    whatever quantity is still open. A take-profit passed to this legacy
    endpoint is converted to a quantity-bearing latest-price conditional
    order; Binance's ``closePosition`` flag is never used for profit targets.
    """

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    if stop_loss is not None and (not isinstance(stop_loss, (int, float)) or stop_loss <= 0):
        raise ValueError("止损价必须是正数")
    if take_profit is not None and (not isinstance(take_profit, (int, float)) or take_profit <= 0):
        raise ValueError("止盈价必须是正数")

    current = _find_futures_position(
        _get_position_risk_with_latest_price(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")
    stop_reference_price, stop_reference_label = _protection_reference_price(position, is_stop=True)
    target_reference_price, target_reference_label = _protection_reference_price(position, is_stop=False)
    is_long = position["side"] == "LONG"
    if stop_loss is not None and ((is_long and stop_loss >= stop_reference_price) or (not is_long and stop_loss <= stop_reference_price)):
        raise ValueError(f"止损价必须位于当前{stop_reference_label}的防守侧")
    if take_profit is not None and ((is_long and take_profit <= target_reference_price) or (not is_long and take_profit >= target_reference_price)):
        raise ValueError(f"止盈价必须位于当前{target_reference_label}的盈利侧")

    rounded_stop_loss = None
    rounded_take_profit = None
    if stop_loss is not None or take_profit is not None:
        tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
        stop_reference_decimal = Decimal(str(stop_reference_price)) if stop_reference_price > 0 else None
        target_reference_decimal = Decimal(str(target_reference_price)) if target_reference_price > 0 else None
        if stop_loss is not None:
            rounded_stop_loss = _round_price_to_tick(stop_loss, tick_size, ROUND_DOWN if is_long else ROUND_UP)
            _validate_protection_price(
                rounded_stop_loss,
                stop_reference_decimal,
                is_long,
                is_stop=True,
                min_price=min_price,
                max_price=max_price,
                reference_label=stop_reference_label,
            )
        if take_profit is not None:
            rounded_take_profit = _round_price_to_tick(take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
            _validate_protection_price(
                rounded_take_profit,
                target_reference_decimal,
                is_long,
                is_stop=False,
                min_price=min_price,
                max_price=max_price,
                reference_label=target_reference_label,
            )

    actual_position_side = position["positionSide"]
    close_side = "SELL" if is_long else "BUY"
    existing_orders = [
        order
        for order in _open_protection_orders(network, api_key, api_secret, safe_symbol)
        if _is_matching_protection_order(order, safe_symbol, actual_position_side, close_side)
        and (
            (
                _is_close_position_order(order, position["quantity"])
                and "STOP" in str(order.get("type") or "").upper()
            )
            or (
                take_profit is not None
                and (
                    "TAKE_PROFIT" in str(order.get("type") or "").upper()
                    or str(order.get("type") or "").upper() == "LIMIT"
                )
            )
        )
    ]
    full_exit_quantity = None
    if rounded_take_profit is not None:
        try:
            # This is still a quantity-based target even when it equals the
            # whole position. It must not be represented by closePosition.
            quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(
                network,
                safe_symbol,
                market=True,
            )
            full_exit_quantity = _round_quantity_to_step(
                position["quantity"],
                quantity_step,
                min_quantity,
                max_quantity,
                too_small_message="当前持仓数量不足以设置数量型止盈",
            )
        except BinanceApiError:
            # A manual editor may be used while exchangeInfo is temporarily
            # unavailable. Keep the validated position quantity; Binance will
            # apply the symbol precision check on the actual order request.
            full_exit_quantity = Decimal(str(position["quantity"]))
    replacement_specs = [
        ("POSITION_STOP_LOSS", "STOP_MARKET", rounded_stop_loss, None),
        ("POSITION_QUANTITY_TAKE_PROFIT", _quantity_take_profit_order_type(), rounded_take_profit, full_exit_quantity),
    ]
    placed_orders = _place_futures_protection_orders(
        network,
        api_key,
        api_secret,
        symbol=safe_symbol,
        close_side=close_side,
        position_side=actual_position_side,
        order_specs=[spec for spec in replacement_specs if spec[2] is not None],
    )
    cancelled_order_ids = _cancel_futures_protection_orders(network, api_key, api_secret, safe_symbol, existing_orders)
    return {
        "scope": "POSITION",
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "stopLoss": float(rounded_stop_loss) if rounded_stop_loss is not None else None,
        "takeProfit": float(rounded_take_profit) if rounded_take_profit is not None else None,
        "takeProfitQuantity": float(full_exit_quantity) if full_exit_quantity is not None else None,
        "takeProfitOrderType": _quantity_take_profit_order_type() if rounded_take_profit is not None else None,
        "positionTakeProfit": None,
        "cancelledOrderIds": cancelled_order_ids,
        "orders": placed_orders,
        "markPrice": position["markPrice"],
    }


@_serialize_protection_update
def update_futures_position_stop_loss(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    stop_loss: float,
) -> dict:
    """Update only the full-position stop without touching any take-profits."""

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    if not isinstance(stop_loss, (int, float)) or not math.isfinite(stop_loss) or stop_loss <= 0:
        raise ValueError("止损价必须是正数")

    current = _find_futures_position(
        _get_position_risk(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")
    reference_price = position["markPrice"] or position["entryPrice"]
    is_long = position["side"] == "LONG"
    if (is_long and stop_loss >= reference_price) or (not is_long and stop_loss <= reference_price):
        raise ValueError("止损价必须位于当前标记价的防守侧")

    tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
    rounded_stop_loss = _round_price_to_tick(stop_loss, tick_size, ROUND_DOWN if is_long else ROUND_UP)
    reference_decimal = Decimal(str(reference_price)) if reference_price > 0 else None
    _validate_protection_price(
        rounded_stop_loss,
        reference_decimal,
        is_long,
        is_stop=True,
        min_price=min_price,
        max_price=max_price,
        reference_label="标记价",
    )
    actual_position_side = position["positionSide"]
    close_side = "SELL" if is_long else "BUY"
    existing_orders = [
        order
        for order in _open_protection_orders(network, api_key, api_secret, safe_symbol)
        if _is_matching_protection_order(order, safe_symbol, actual_position_side, close_side)
        and _is_close_position_order(order)
        and "STOP" in str(order.get("type") or "").upper()
    ]
    existing_stop_prices = {
        Decimal(str(order.get("stopPrice")))
        for order in existing_orders
        if _as_number(order.get("stopPrice")) > 0
    }
    if len(existing_orders) == 1 and rounded_stop_loss in existing_stop_prices:
        return {
            "scope": "POSITION_STOP",
            "symbol": safe_symbol,
            "positionSide": actual_position_side,
            "side": position["side"],
            "stopLoss": float(rounded_stop_loss),
            "unchanged": True,
            "cancelledOrderIds": [],
            "orders": [],
            "markPrice": position["markPrice"],
        }
    cancelled_order_ids = _cancel_futures_protection_orders(network, api_key, api_secret, safe_symbol, existing_orders)
    try:
        # Binance permits only one closePosition stop/target for a position
        # direction. Unlike quantity-based take-profits, the previous stop
        # must therefore be cancelled before its replacement is submitted.
        placed_orders = _place_futures_protection_orders(
            network,
            api_key,
            api_secret,
            symbol=safe_symbol,
            close_side=close_side,
            position_side=actual_position_side,
            order_specs=[("POSITION_STOP_LOSS", "STOP_MARKET", rounded_stop_loss, None)],
        )
    except Exception:
        # Keep the exposure window as short as possible. If the replacement
        # is rejected, make one best-effort attempt to restore the last stop;
        # fixed quantity take-profits are never touched by this operation.
        restore_specs = []
        for order in existing_orders:
            previous_stop = _as_number(order.get("stopPrice"))
            if previous_stop > 0:
                restore_specs.append(("RESTORE_POSITION_STOP_LOSS", "STOP_MARKET", Decimal(str(previous_stop)), None))
        if restore_specs:
            try:
                _place_futures_protection_orders(
                    network,
                    api_key,
                    api_secret,
                    symbol=safe_symbol,
                    close_side=close_side,
                    position_side=actual_position_side,
                    order_specs=restore_specs,
                )
            except Exception:
                pass
        raise
    return {
        "scope": "POSITION_STOP",
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "stopLoss": float(rounded_stop_loss),
        "cancelledOrderIds": cancelled_order_ids,
        "orders": placed_orders,
        "markPrice": position["markPrice"],
    }


@_serialize_protection_update
def close_futures_position_market(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    quantity_ratio: float = 100,
) -> dict:
    """Close the current futures position immediately with a market order.

    Binance rejects a STOP_MARKET trigger that has already crossed the mark
    price. This is the corresponding terminal protection action. One-way
    positions use ``reduceOnly``; hedge-mode positions are identified by
    ``positionSide`` and must not include that flag.
    """

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    try:
        requested_ratio = Decimal(str(quantity_ratio))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("平仓比例必须在 1% 到 100% 之间") from exc
    if not requested_ratio.is_finite() or requested_ratio <= 0 or requested_ratio > 100:
        raise ValueError("平仓比例必须在 1% 到 100% 之间")

    current = _find_futures_position(
        _get_position_risk(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")

    quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(
        network,
        safe_symbol,
        market=True,
    )
    position_quantity = Decimal(str(position["quantity"]))
    quantity = _quantity_at_ratio(
        position_quantity,
        requested_ratio,
        quantity_step,
        min_quantity,
        max_quantity,
        too_small_message="当前平仓比例对应的数量不足以按交易所最小单位提交",
    )
    actual_position_side = position["positionSide"]
    close_side = "SELL" if position["side"] == "LONG" else "BUY"
    client_order_id = f"cl_{uuid.uuid4().hex[:24]}"
    params: dict[str, object] = {
        "symbol": safe_symbol,
        "side": close_side,
        "positionSide": actual_position_side,
        "type": "MARKET",
        "quantity": _format_order_quantity(quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": client_order_id,
    }
    if actual_position_side == "BOTH":
        params["reduceOnly"] = "true"

    try:
        response = _request(
            network,
            "/fapi/v1/order",
            params,
            method="POST",
            api_key=api_key,
            api_secret=api_secret,
            signed=True,
        )
    except BinanceApiError as exc:
        if not (exc.is_transport_error or exc.status_code >= 500 or exc.is_transient):
            raise
        response = _recover_futures_entry_order(
            network,
            api_key,
            api_secret,
            symbol=safe_symbol,
            client_order_id=client_order_id,
        )
        if response is None:
            raise

    if not isinstance(response, dict) or not (
        response.get("orderId") or response.get("clientOrderId")
    ):
        response = _recover_futures_entry_order(
            network,
            api_key,
            api_secret,
            symbol=safe_symbol,
            client_order_id=client_order_id,
        )
    if not isinstance(response, dict) or not (
        response.get("orderId") or response.get("clientOrderId")
    ):
        raise BinanceApiError("交易所未返回市价平仓单编号")

    return {
        "scope": "POSITION_MARKET_CLOSE",
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "closeSide": close_side,
        "requestedQuantityRatio": float(requested_ratio),
        "positionQuantity": float(position_quantity),
        "quantity": float(quantity),
        "effectiveQuantityRatio": float(quantity / position_quantity * 100) if position_quantity > 0 else None,
        "orderId": response.get("orderId"),
        "clientOrderId": response.get("clientOrderId") or client_order_id,
        "status": response.get("status"),
        "order": response,
    }


@_serialize_protection_update
def apply_futures_plan_protection(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    stop_loss: float,
    first_take_profit: float,
    protective_take_profit: float | None = None,
    extension_take_profit: float | None = None,
    protective_take_profit_ratio: float = 25,
    first_take_profit_ratio: float = 50,
    second_take_profit_ratio: float = 75,
) -> dict:
    """Apply a plan with a full stop and up to three incremental targets."""

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    requested_prices = (stop_loss, first_take_profit) + tuple(
        value for value in (protective_take_profit, extension_take_profit) if value is not None
    )
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in requested_prices):
        raise ValueError("计划保护价必须是正数")
    if protective_take_profit is not None and (
        not isinstance(protective_take_profit, (int, float))
        or not math.isfinite(protective_take_profit)
        or protective_take_profit <= 0
    ):
        raise ValueError("近端保护目标必须是正数")
    if extension_take_profit is not None and (
        not isinstance(extension_take_profit, (int, float))
        or not math.isfinite(extension_take_profit)
        or extension_take_profit <= 0
    ):
        raise ValueError("扩展目标必须是正数")
    if protective_take_profit is not None:
        protective_ratio, target_ratio, extension_ratio = _plan_take_profit_ladder_ratios(
            protective_take_profit_ratio,
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )
    else:
        protective_ratio = None
        target_ratio, extension_ratio = _plan_take_profit_ratios(
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )

    current = _find_futures_position(
        _get_position_risk_with_latest_price(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")
    stop_reference_price, stop_reference_label = _protection_reference_price(position, is_stop=True)
    target_reference_price, target_reference_label = _protection_reference_price(position, is_stop=False)
    is_long = position["side"] == "LONG"
    if (
        (is_long and (stop_loss >= stop_reference_price or first_take_profit <= target_reference_price))
        or (not is_long and (stop_loss <= stop_reference_price or first_take_profit >= target_reference_price))
    ):
        raise ValueError(f"计划保护价必须分别位于当前{stop_reference_label}的防守侧和当前{target_reference_label}的盈利侧")
    if protective_take_profit is not None and (
        (is_long and (protective_take_profit <= target_reference_price or protective_take_profit >= first_take_profit))
        or (not is_long and (protective_take_profit >= target_reference_price or protective_take_profit <= first_take_profit))
    ):
        raise ValueError(f"近端保护目标必须位于当前{target_reference_label}与第一目标之间")
    if extension_take_profit is not None and (
        (is_long and extension_take_profit <= first_take_profit)
        or (not is_long and extension_take_profit >= first_take_profit)
    ):
        raise ValueError("扩展目标必须位于第一目标的盈利方向")

    tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
    stop_reference_decimal = Decimal(str(stop_reference_price)) if stop_reference_price > 0 else None
    target_reference_decimal = Decimal(str(target_reference_price)) if target_reference_price > 0 else None
    rounded_stop_loss = _round_price_to_tick(stop_loss, tick_size, ROUND_DOWN if is_long else ROUND_UP)
    rounded_protective_take_profit = (
        _round_price_to_tick(protective_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if protective_take_profit is not None
        else None
    )
    rounded_first_take_profit = _round_price_to_tick(first_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
    rounded_extension_take_profit = (
        _round_price_to_tick(extension_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if extension_take_profit is not None
        else None
    )
    _validate_protection_price(rounded_stop_loss, stop_reference_decimal, is_long, is_stop=True, min_price=min_price, max_price=max_price, reference_label=stop_reference_label)
    if rounded_protective_take_profit is not None:
        _validate_protection_price(rounded_protective_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    _validate_protection_price(rounded_first_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    if rounded_extension_take_profit is not None:
        _validate_protection_price(rounded_extension_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    if rounded_protective_take_profit is not None and (
        (is_long and rounded_protective_take_profit >= rounded_first_take_profit)
        or (not is_long and rounded_protective_take_profit <= rounded_first_take_profit)
    ):
        raise ValueError("近端保护目标必须位于第一目标的盈利方向之前")
    if rounded_extension_take_profit is not None and (
        (is_long and rounded_extension_take_profit <= rounded_first_take_profit)
        or (not is_long and rounded_extension_take_profit >= rounded_first_take_profit)
    ):
        raise ValueError("扩展目标必须位于第一目标的盈利方向")

    # Every plan take-profit is a quantity-based latest-price conditional
    # order after the position is open; use MARKET_LOT_SIZE for all exits.
    quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(network, safe_symbol, market=True)
    protective_target_quantity = None
    if rounded_protective_take_profit is not None:
        protective_target_quantity, first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_ladder_quantities(
            position["quantity"],
            protective_ratio,
            target_ratio,
            extension_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            has_extension=rounded_extension_take_profit is not None,
            too_small_message="当前持仓数量不足以拆分三档计划止盈",
        )
    elif rounded_extension_take_profit is None:
        first_target_quantity, target_split_fallback = _first_plan_take_profit_quantity(
            position["quantity"],
            target_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            too_small_message="当前持仓数量不足以按第一目标分批止盈",
        )
        extension_target_quantity = None
    else:
        first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_quantities(
            position["quantity"],
            target_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            second_target_ratio=extension_ratio,
            too_small_message="当前持仓数量不足以拆分两档计划止盈",
        )
    actual_position_side = position["positionSide"]
    close_side = "SELL" if is_long else "BUY"
    existing_orders = [
        order
        for order in _open_protection_orders(network, api_key, api_secret, safe_symbol)
        if _is_matching_protection_order(order, safe_symbol, actual_position_side, close_side)
    ]

    # Target ratios are cumulative portions of the original position; order
    # quantities are the increments between those cumulative boundaries.
    order_specs = [
        ("STOP_LOSS", "STOP_MARKET", rounded_stop_loss, None),
    ]
    if protective_target_quantity is not None and rounded_protective_take_profit is not None:
        order_specs.append(("PROTECTIVE_TARGET", _quantity_take_profit_order_type(), rounded_protective_take_profit, protective_target_quantity))
    order_specs.append(("FIRST_TARGET", _quantity_take_profit_order_type(), rounded_first_take_profit, first_target_quantity))
    if extension_target_quantity is not None and rounded_extension_take_profit is not None:
        order_specs.append(("EXTENSION_TARGET", _quantity_take_profit_order_type(), rounded_extension_take_profit, extension_target_quantity))
    placed_orders = _place_futures_protection_orders(
        network,
        api_key,
        api_secret,
        symbol=safe_symbol,
        close_side=close_side,
        position_side=actual_position_side,
        order_specs=order_specs,
    )
    cancelled_order_ids = _cancel_futures_protection_orders(network, api_key, api_secret, safe_symbol, existing_orders)
    target_quantity_summary = _target_quantity_summary(
        protective_target_quantity,
        first_target_quantity,
        extension_target_quantity,
    )
    return {
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "stopLoss": float(rounded_stop_loss),
        "protectiveTakeProfit": float(rounded_protective_take_profit) if protective_target_quantity is not None and rounded_protective_take_profit is not None else None,
        "firstTakeProfit": float(rounded_first_take_profit),
        "extensionTakeProfit": float(rounded_extension_take_profit) if extension_target_quantity is not None and rounded_extension_take_profit is not None else None,
        "positionTakeProfit": None,
        "partialTakeProfit": float(rounded_first_take_profit),
        "protectiveTakeProfitRatio": protective_ratio,
        "firstTakeProfitRatio": target_ratio,
        "secondTakeProfitRatio": extension_ratio,
        "protectiveTargetQuantity": float(protective_target_quantity) if protective_target_quantity is not None else None,
        "firstTargetQuantity": float(first_target_quantity),
        "extensionTargetQuantity": float(extension_target_quantity) if extension_target_quantity is not None else None,
        **target_quantity_summary,
        "takeProfitOrderType": _quantity_take_profit_order_type(),
        "takeProfitWorkingType": "CONTRACT_PRICE",
        "fallback": target_split_fallback,
        "runnerManagedByMovingStop": (
            rounded_extension_take_profit is None
            and target_split_fallback not in {"FIRST_TARGET_FULL_QUANTITY", "EXTENSION_TARGET_HALF_POSITION"}
        ),
        "fallbackReason": _target_split_fallback_reason(
            target_split_fallback,
            subject="当前持仓",
            protective_ratio=protective_ratio,
            first_ratio=target_ratio,
            second_ratio=extension_ratio,
            extension_requested=rounded_extension_take_profit is not None,
        ),
        "positionQuantity": position["quantity"],
        "cancelledOrderIds": cancelled_order_ids,
        "orders": placed_orders,
        "markPrice": position["markPrice"],
    }


@_serialize_protection_update
def apply_futures_plan_take_profits(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    position_side: str,
    first_take_profit: float,
    protective_take_profit: float | None = None,
    extension_take_profit: float | None = None,
    protective_take_profit_ratio: float = 25,
    first_take_profit_ratio: float = 50,
    second_take_profit_ratio: float = 75,
) -> dict:
    """Add missing plan targets without touching a live position stop."""

    safe_symbol = _symbol(symbol)
    requested_position_side = str(position_side or "BOTH").strip().upper()
    if requested_position_side not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    requested_prices = (first_take_profit,) + tuple(
        value for value in (protective_take_profit, extension_take_profit) if value is not None
    )
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in requested_prices):
        raise ValueError("计划止盈价必须是正数")
    if protective_take_profit is not None and (
        not isinstance(protective_take_profit, (int, float))
        or not math.isfinite(protective_take_profit)
        or protective_take_profit <= 0
    ):
        raise ValueError("近端保护目标必须是正数")
    if extension_take_profit is not None and (
        not isinstance(extension_take_profit, (int, float))
        or not math.isfinite(extension_take_profit)
        or extension_take_profit <= 0
    ):
        raise ValueError("扩展目标必须是正数")
    if protective_take_profit is not None:
        protective_ratio, target_ratio, extension_ratio = _plan_take_profit_ladder_ratios(
            protective_take_profit_ratio,
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )
    else:
        protective_ratio = None
        target_ratio, extension_ratio = _plan_take_profit_ratios(
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )

    current = _find_futures_position(
        _get_position_risk_with_latest_price(network, api_key, api_secret),
        safe_symbol,
        requested_position_side,
    )
    if not current:
        raise ValueError("未找到该交易对的当前合约持仓")
    position = _futures_position_item(current)
    if not position:
        raise ValueError("当前合约持仓数量无效")
    target_reference_price, target_reference_label = _protection_reference_price(position, is_stop=False)
    is_long = position["side"] == "LONG"
    if (
        (is_long and first_take_profit <= target_reference_price)
        or (not is_long and first_take_profit >= target_reference_price)
    ):
        raise ValueError(f"计划止盈价必须位于当前{target_reference_label}的盈利侧")
    if protective_take_profit is not None and (
        (is_long and (protective_take_profit <= target_reference_price or protective_take_profit >= first_take_profit))
        or (not is_long and (protective_take_profit >= target_reference_price or protective_take_profit <= first_take_profit))
    ):
        raise ValueError(f"近端保护目标必须位于当前{target_reference_label}与第一目标之间")
    if extension_take_profit is not None and (
        (is_long and extension_take_profit <= first_take_profit)
        or (not is_long and extension_take_profit >= first_take_profit)
    ):
        raise ValueError("扩展目标必须位于第一目标的盈利方向")

    tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
    target_reference_decimal = Decimal(str(target_reference_price)) if target_reference_price > 0 else None
    rounded_protective_take_profit = (
        _round_price_to_tick(protective_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if protective_take_profit is not None
        else None
    )
    rounded_first_take_profit = _round_price_to_tick(first_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
    rounded_extension_take_profit = (
        _round_price_to_tick(extension_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if extension_take_profit is not None
        else None
    )
    if rounded_protective_take_profit is not None:
        _validate_protection_price(rounded_protective_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    _validate_protection_price(rounded_first_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    if rounded_extension_take_profit is not None:
        _validate_protection_price(rounded_extension_take_profit, target_reference_decimal, is_long, is_stop=False, min_price=min_price, max_price=max_price, reference_label=target_reference_label)
    if rounded_protective_take_profit is not None and (
        (is_long and rounded_protective_take_profit >= rounded_first_take_profit)
        or (not is_long and rounded_protective_take_profit <= rounded_first_take_profit)
    ):
        raise ValueError("近端保护目标必须位于第一目标的盈利方向之前")
    if rounded_extension_take_profit is not None and (
        (is_long and rounded_extension_take_profit <= rounded_first_take_profit)
        or (not is_long and rounded_extension_take_profit >= rounded_first_take_profit)
    ):
        raise ValueError("扩展目标必须位于第一目标的盈利方向")

    # Every plan take-profit is a quantity-based latest-price conditional
    # order after the position is open; use MARKET_LOT_SIZE for all exits.
    quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(network, safe_symbol, market=True)
    protective_target_quantity = None
    if rounded_protective_take_profit is not None:
        protective_target_quantity, first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_ladder_quantities(
            position["quantity"],
            protective_ratio,
            target_ratio,
            extension_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            has_extension=rounded_extension_take_profit is not None,
            too_small_message="当前持仓数量不足以拆分三档计划止盈",
        )
    elif rounded_extension_take_profit is None:
        first_target_quantity, target_split_fallback = _first_plan_take_profit_quantity(
            position["quantity"],
            target_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            too_small_message="当前持仓数量不足以按第一目标分批止盈",
        )
        extension_target_quantity = None
    else:
        first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_quantities(
            position["quantity"],
            target_ratio,
            quantity_step,
            min_quantity,
            max_quantity,
            second_target_ratio=extension_ratio,
            too_small_message="当前持仓数量不足以拆分两档计划止盈",
        )
    actual_position_side = position["positionSide"]
    close_side = "SELL" if is_long else "BUY"
    existing_orders = [
        order
        for order in _open_protection_orders(network, api_key, api_secret, safe_symbol)
        if _is_matching_protection_order(order, safe_symbol, actual_position_side, close_side)
        and (
            "TAKE_PROFIT" in str(order.get("type") or "").upper()
            or str(order.get("type") or "").upper() == "LIMIT"
        )
    ]
    desired_specs: list[tuple[str, str, Decimal, Decimal]] = []
    if protective_target_quantity is not None and rounded_protective_take_profit is not None:
        desired_specs.append(("PROTECTIVE_TARGET", _quantity_take_profit_order_type(), rounded_protective_take_profit, protective_target_quantity))
    desired_specs.append(("FIRST_TARGET", _quantity_take_profit_order_type(), rounded_first_take_profit, first_target_quantity))
    if extension_target_quantity is not None and rounded_extension_take_profit is not None:
        desired_specs.append(("EXTENSION_TARGET", _quantity_take_profit_order_type(), rounded_extension_take_profit, extension_target_quantity))

    def target_matches(order: dict, target_price: Decimal, target_quantity: Decimal) -> bool:
        """Only a quantity-based latest-price target satisfies a plan target."""

        if str(order.get("orderSource") or "").upper() != "ALGO":
            return False
        if str(order.get("type") or "").upper() != _quantity_take_profit_order_type():
            return False
        if _is_close_position_order(order):
            return False
        order_price = _as_number(order.get("triggerPrice") or order.get("stopPrice"))
        order_quantity = _as_number(order.get("quantity") or order.get("origQty"))
        if order_price <= 0 or order_quantity <= 0:
            return False
        return (
            Decimal(str(order_price)) == target_price
            and abs(Decimal(str(order_quantity)) - target_quantity) <= max(target_quantity * Decimal("1e-9"), Decimal("1e-12"))
        )

    # Keep exactly one matching quantity-based latest-price target per role.
    # This replaces legacy LIMIT or position-level take-profits and removes
    # stale/wrong-quantity orders.
    retained_order_ids: set[object] = set()
    stale_orders: list[dict] = []
    missing_specs: list[tuple[str, str, Decimal, Decimal]] = []
    for role, order_type, target_price, target_quantity in desired_specs:
        match = next(
            (
                order
                for order in existing_orders
                if (order.get("orderId") or order.get("algoId")) not in retained_order_ids
                and target_matches(order, target_price, target_quantity)
            ),
            None,
        )
        if match is None:
            missing_specs.append((role, order_type, target_price, target_quantity))
        else:
            retained_order_ids.add(match.get("orderId") or match.get("algoId"))
    for order in existing_orders:
        order_id = order.get("orderId") or order.get("algoId")
        if order_id not in retained_order_ids:
            stale_orders.append(order)

    placed_orders = _place_futures_protection_orders(
        network,
        api_key,
        api_secret,
        symbol=safe_symbol,
        close_side=close_side,
        position_side=actual_position_side,
        order_specs=missing_specs,
    ) if missing_specs else []
    cancelled_order_ids = _cancel_futures_protection_orders(
        network,
        api_key,
        api_secret,
        safe_symbol,
        stale_orders,
    ) if stale_orders else []
    target_quantity_summary = _target_quantity_summary(
        protective_target_quantity,
        first_target_quantity,
        extension_target_quantity,
    )
    return {
        "scope": "PLAN_TAKE_PROFITS",
        "symbol": safe_symbol,
        "positionSide": actual_position_side,
        "side": position["side"],
        "protectiveTakeProfit": float(rounded_protective_take_profit) if protective_target_quantity is not None and rounded_protective_take_profit is not None else None,
        "firstTakeProfit": float(rounded_first_take_profit),
        "extensionTakeProfit": float(rounded_extension_take_profit) if extension_target_quantity is not None and rounded_extension_take_profit is not None else None,
        "protectiveTakeProfitRatio": protective_ratio,
        "firstTakeProfitRatio": target_ratio,
        "secondTakeProfitRatio": extension_ratio,
        "protectiveTargetQuantity": float(protective_target_quantity) if protective_target_quantity is not None else None,
        "firstTargetQuantity": float(first_target_quantity),
        "extensionTargetQuantity": float(extension_target_quantity) if extension_target_quantity is not None else None,
        **target_quantity_summary,
        "takeProfitOrderType": _quantity_take_profit_order_type(),
        "takeProfitWorkingType": "CONTRACT_PRICE",
        "fallback": target_split_fallback,
        "runnerManagedByMovingStop": (
            rounded_extension_take_profit is None
            and target_split_fallback not in {"FIRST_TARGET_FULL_QUANTITY", "EXTENSION_TARGET_HALF_POSITION"}
        ),
        "fallbackReason": _target_split_fallback_reason(
            target_split_fallback,
            subject="当前持仓",
            protective_ratio=protective_ratio,
            first_ratio=target_ratio,
            second_ratio=extension_ratio,
            extension_requested=rounded_extension_take_profit is not None,
        ),
        "orders": placed_orders,
        "cancelledOrderIds": cancelled_order_ids,
        "unchanged": not missing_specs and not stale_orders,
        "markPrice": position["markPrice"],
    }


def _resolve_futures_order_position_side(
    network: str,
    api_key: str,
    api_secret: str,
    direction: str,
    requested_position_side: str | None,
) -> str:
    requested = str(requested_position_side or "").strip().upper()
    if requested and requested not in {"BOTH", "LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    # Binance currently rejects ``positionSide.dual`` on its Futures
    # WebSocket API. ``account.status`` is supported and exposes the active
    # position sides, which identifies hedge mode whenever a hedge position
    # exists. With no positions Binance supplies no mode flag, so preserve an
    # explicit side or use the safe one-way default.
    endpoint = "/fapi/v2/account" if _use_websocket_api.get() else "/fapi/v1/positionSide/dual"
    payload = _request(network, endpoint, api_key=api_key, api_secret=api_secret, signed=True)
    if not isinstance(payload, dict):
        raise BinanceApiError("交易所返回的持仓模式格式无效")
    raw_dual = payload.get("dualSidePosition")
    if _use_websocket_api.get() and raw_dual is None:
        positions = payload.get("positions")
        if isinstance(positions, list):
            raw_dual = any(
                str(position.get("positionSide") or "").strip().upper() in {"LONG", "SHORT"}
                for position in positions
                if isinstance(position, dict)
            )
        if raw_dual is None:
            return requested or "BOTH"
    dual_side = raw_dual is True or str(raw_dual or "").strip().lower() == "true"
    if dual_side:
        if requested == "BOTH":
            raise ValueError("双向持仓模式必须指定 LONG 或 SHORT")
        return requested or direction
    if requested in {"LONG", "SHORT"}:
        raise ValueError("单向持仓模式只能使用 BOTH")
    return "BOTH"


def _recover_futures_entry_order(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    client_order_id: str,
) -> dict | None:
    """Find a standard entry accepted before its response was lost."""

    for attempt in range(len(ORDER_RECOVERY_RETRY_DELAYS) + 1):
        try:
            payload = _request(
                network,
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
                api_key=api_key,
                api_secret=api_secret,
                signed=True,
            )
        except BinanceApiError:
            payload = None
        if isinstance(payload, dict) and str(payload.get("clientOrderId") or "").strip() == client_order_id:
            return payload
        if attempt < len(ORDER_RECOVERY_RETRY_DELAYS):
            time.sleep(ORDER_RECOVERY_RETRY_DELAYS[attempt])
    return None


def _set_futures_leverage(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    leverage: int,
) -> object:
    """Set leverage through REST because USDⓈ-M WS API has no such method.

    Binance exposes ``POST /fapi/v1/leverage`` in REST, but the current
    Futures WebSocket API does not expose a corresponding operation (both
    ``leverage.adjust`` and ``leverage.change`` are rejected as invalid
    methods).  Temporarily forcing the existing REST request path keeps the
    selected WebSocket transport for order/position/protection operations while
    routing this one unsupported configuration call through the configured
    local REST proxy.
    """

    with websocket_api_requests(False):
        return _request(
            network,
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": int(leverage)},
            method="POST",
            api_key=api_key,
            api_secret=api_secret,
            signed=True,
        )


@_serialize_protection_update
def place_futures_market_entry_order(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    direction: str,
    quantity: float,
    leverage: float,
    position_side: str | None = None,
) -> dict:
    """Submit and reconcile one futures MARKET entry order."""

    safe_symbol = _symbol(symbol)
    safe_direction = str(direction or "").strip().upper()
    if safe_direction not in {"LONG", "SHORT"}:
        raise ValueError("计划方向必须是 LONG 或 SHORT")
    if not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("市价入场数量必须是正数")
    if not isinstance(leverage, (int, float)) or not math.isfinite(leverage) or not float(leverage).is_integer() or not 1 <= leverage <= MAX_FUTURES_LEVERAGE:
        raise ValueError(f"真实合约杠杆必须是 1 到 {MAX_FUTURES_LEVERAGE} 倍的整数")

    quantity_step, min_quantity, max_quantity = _symbol_quantity_filter(network, safe_symbol, market=True)
    entry_quantity = _round_quantity_to_step(
        quantity,
        quantity_step,
        min_quantity,
        max_quantity,
        too_small_message="市价入场数量不足以按交易所精度下单",
    )
    actual_position_side = _resolve_futures_order_position_side(network, api_key, api_secret, safe_direction, position_side)
    entry_side = "BUY" if safe_direction == "LONG" else "SELL"
    client_order_id = f"cl_{uuid.uuid4().hex[:24]}"
    try:
        _set_futures_leverage(network, api_key, api_secret, symbol=safe_symbol, leverage=int(leverage))
    except BinanceApiError as exc:
        raise BinanceEntryOrderError(
            f"设置入场杠杆失败：{exc}",
            status_code=exc.status_code,
            exchange_code=exc.exchange_code,
            entry_order_submitted=False,
        ) from exc

    try:
        entry_response = _request(
            network,
            "/fapi/v1/order",
            {
                "symbol": safe_symbol,
                "side": entry_side,
                "positionSide": actual_position_side,
                "type": "MARKET",
                "quantity": _format_order_quantity(entry_quantity),
                "newOrderRespType": "RESULT",
                "newClientOrderId": client_order_id,
            },
            method="POST",
            api_key=api_key,
            api_secret=api_secret,
            signed=True,
        )
    except BinanceApiError as exc:
        if exc.is_transport_error or exc.status_code >= 500 or exc.is_transient:
            recovered = _recover_futures_entry_order(
                network,
                api_key,
                api_secret,
                symbol=safe_symbol,
                client_order_id=client_order_id,
            )
            if recovered is not None:
                entry_response = recovered
            else:
                raise BinanceEntryOrderError(
                    f"{exc}；未能确认入场单状态，请稍后在 Binance 委托中核对订单 {client_order_id}",
                    status_code=exc.status_code,
                    exchange_code=exc.exchange_code,
                ) from exc
        else:
            raise BinanceEntryOrderError(
                str(exc) or "交易所拒绝了入场市价单",
                status_code=exc.status_code,
                exchange_code=exc.exchange_code,
            ) from exc

    if not isinstance(entry_response, dict) or not (entry_response.get("orderId") or entry_response.get("clientOrderId")):
        raise BinanceEntryOrderError("交易所未返回有效的入场市价单结果")
    entry_status = str(entry_response.get("status") or "").strip().upper()
    if entry_status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
        raise BinanceEntryOrderError(f"入场市价单未成功，交易所状态：{entry_status}", status_code=400)
    if entry_status and entry_status not in {"NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_NEW"}:
        raise BinanceEntryOrderError(f"入场市价单返回未知状态：{entry_status}")
    avg_price = entry_response.get("avgPrice") or entry_response.get("price")
    try:
        effective_price = float(avg_price) if float(avg_price or 0) > 0 else None
    except (TypeError, ValueError):
        effective_price = None
    return {
        "scope": "MARKET_ENTRY",
        "symbol": safe_symbol,
        "direction": safe_direction,
        "positionSide": actual_position_side,
        "leverage": int(leverage),
        "entryOrder": entry_response,
        "entryStatus": entry_status or "FILLED",
        "entryAccepted": True,
        "entryPrice": effective_price,
        "quantity": float(entry_quantity),
    }


def cancel_futures_order(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    order_id: object = None,
    client_order_id: str | None = None,
) -> dict:
    """Cancel one known futures order without touching sibling orders."""

    params: dict[str, object] = {"symbol": _symbol(symbol)}
    if order_id not in (None, ""):
        params["orderId"] = order_id
    elif client_order_id:
        params["origClientOrderId"] = client_order_id
    else:
        raise ValueError("撤单需要订单编号")
    response = _request(network, "/fapi/v1/order", params, method="DELETE", api_key=api_key, api_secret=api_secret, signed=True)
    if not isinstance(response, dict):
        raise BinanceApiError("交易所撤单响应格式无效")
    return response


def place_futures_limit_plan_order(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    direction: str,
    quantity: float,
    cost_price: float,
    leverage: float,
    stop_loss: float | None = None,
    first_take_profit: float | None = None,
    protective_take_profit: float | None = None,
    extension_take_profit: float | None = None,
    protective_take_profit_ratio: float = 25,
    first_take_profit_ratio: float = 50,
    position_side: str | None = None,
    second_take_profit_ratio: float = 75,
    with_protection: bool = True,
) -> dict:
    """Submit a futures LIMIT entry and defer protection until it is filled.

    Binance conditional orders with ``closePosition`` or ``reduceOnly`` are
    tied to an existing position. A LIMIT entry can be accepted while it is
    still ``NEW``, so protection is installed later by the execution-plan
    refresh worker after the account snapshot contains the filled position.
    """

    safe_symbol = _symbol(symbol)
    safe_direction = str(direction or "").strip().upper()
    if safe_direction not in {"LONG", "SHORT"}:
        raise ValueError("计划方向必须是 LONG 或 SHORT")
    protection_enabled = bool(with_protection)
    if protection_enabled and (stop_loss is None or first_take_profit is None):
        raise ValueError("启用保护时必须提供止损和第一止盈")
    values = (quantity, cost_price, leverage) + ((stop_loss, first_take_profit) if protection_enabled else ())
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("限价计划参数必须是正数")
    if protection_enabled and protective_take_profit is not None and (
        not isinstance(protective_take_profit, (int, float))
        or not math.isfinite(protective_take_profit)
        or protective_take_profit <= 0
    ):
        raise ValueError("近端保护目标必须是正数")
    if protection_enabled and extension_take_profit is not None and (
        not isinstance(extension_take_profit, (int, float))
        or not math.isfinite(extension_take_profit)
        or extension_take_profit <= 0
    ):
        raise ValueError("扩展目标必须是正数")
    if not protection_enabled:
        protective_take_profit = None
        extension_take_profit = None
        protective_ratio = None
        target_ratio = None
        extension_ratio = None
    elif protective_take_profit is not None:
        protective_ratio, target_ratio, extension_ratio = _plan_take_profit_ladder_ratios(
            protective_take_profit_ratio,
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )
    else:
        protective_ratio = None
        target_ratio, extension_ratio = _plan_take_profit_ratios(
            first_take_profit_ratio,
            second_take_profit_ratio,
            has_extension=extension_take_profit is not None,
        )
    if not float(leverage).is_integer() or leverage < 1 or leverage > MAX_FUTURES_LEVERAGE:
        raise ValueError(f"真实合约杠杆必须是 1 到 {MAX_FUTURES_LEVERAGE} 倍的整数")

    is_long = safe_direction == "LONG"
    tick_size, min_price, max_price = _symbol_price_filter(network, safe_symbol)
    rounded_entry_price = _round_price_to_tick(cost_price, tick_size, ROUND_DOWN if is_long else ROUND_UP)
    entry_reference = rounded_entry_price
    rounded_stop_loss = _round_price_to_tick(stop_loss, tick_size, ROUND_DOWN if is_long else ROUND_UP) if protection_enabled else None
    rounded_protective_take_profit = (
        _round_price_to_tick(protective_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if protective_take_profit is not None
        else None
    )
    rounded_first_take_profit = _round_price_to_tick(first_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN) if protection_enabled else None
    rounded_extension_take_profit = (
        _round_price_to_tick(extension_take_profit, tick_size, ROUND_UP if is_long else ROUND_DOWN)
        if extension_take_profit is not None
        else None
    )
    _validate_protection_price(rounded_entry_price, None, is_long, is_stop=False, min_price=min_price, max_price=max_price)
    if protection_enabled:
        _validate_protection_price(rounded_stop_loss, entry_reference, is_long, is_stop=True, min_price=min_price, max_price=max_price)
    if rounded_protective_take_profit is not None:
        _validate_protection_price(rounded_protective_take_profit, entry_reference, is_long, is_stop=False, min_price=min_price, max_price=max_price)
    if protection_enabled:
        _validate_protection_price(rounded_first_take_profit, entry_reference, is_long, is_stop=False, min_price=min_price, max_price=max_price)
    if rounded_extension_take_profit is not None:
        _validate_protection_price(rounded_extension_take_profit, entry_reference, is_long, is_stop=False, min_price=min_price, max_price=max_price)
    if protection_enabled and rounded_protective_take_profit is not None and (
        (is_long and rounded_protective_take_profit >= rounded_first_take_profit)
        or (not is_long and rounded_protective_take_profit <= rounded_first_take_profit)
    ):
        raise ValueError("近端保护目标必须位于第一目标的盈利方向之前")
    if protection_enabled and rounded_extension_take_profit is not None and (
        (is_long and rounded_extension_take_profit <= rounded_first_take_profit)
        or (not is_long and rounded_extension_take_profit >= rounded_first_take_profit)
    ):
        raise ValueError("扩展目标必须位于第一目标的盈利方向")

    entry_step, entry_min_quantity, entry_max_quantity = _symbol_quantity_filter(network, safe_symbol, market=False)
    entry_quantity = _round_quantity_to_step(
        quantity,
        entry_step,
        entry_min_quantity,
        entry_max_quantity,
        too_small_message="限价入场数量不足以按交易所精度下单",
    )
    # Protection targets are quantity-based latest-price conditional orders.
    exit_step, exit_min_quantity, exit_max_quantity = _symbol_quantity_filter(network, safe_symbol, market=True)
    protective_target_quantity = None
    first_target_quantity = None
    extension_target_quantity = None
    target_split_fallback = None
    if not protection_enabled:
        pass
    elif rounded_protective_take_profit is not None:
        protective_target_quantity, first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_ladder_quantities(
            float(entry_quantity),
            protective_ratio,
            target_ratio,
            extension_ratio,
            exit_step,
            exit_min_quantity,
            exit_max_quantity,
            has_extension=rounded_extension_take_profit is not None,
            too_small_message="限价入场数量不足以拆分三档计划止盈",
        )
    elif rounded_extension_take_profit is None:
        first_target_quantity, target_split_fallback = _first_plan_take_profit_quantity(
            float(entry_quantity),
            target_ratio,
            exit_step,
            exit_min_quantity,
            exit_max_quantity,
            too_small_message="限价入场数量不足以按第一目标分批止盈",
        )
        extension_target_quantity = None
    else:
        first_target_quantity, extension_target_quantity, target_split_fallback = _split_plan_take_profit_quantities(
            float(entry_quantity),
            target_ratio,
            exit_step,
            exit_min_quantity,
            exit_max_quantity,
            second_target_ratio=extension_ratio,
            too_small_message="限价入场数量不足以拆分两档计划止盈",
        )
    actual_position_side = _resolve_futures_order_position_side(network, api_key, api_secret, safe_direction, position_side)
    entry_side = "BUY" if is_long else "SELL"
    client_order_id = f"cl_{uuid.uuid4().hex[:24]}"

    try:
        _set_futures_leverage(
            network,
            api_key,
            api_secret,
            symbol=safe_symbol,
            leverage=int(leverage),
        )
    except BinanceApiError as exc:
        raise BinanceEntryOrderError(
            f"设置入场杠杆失败：{exc}",
            status_code=exc.status_code,
            exchange_code=exc.exchange_code,
            entry_order_submitted=False,
        ) from exc

    try:
        entry_response = _request(
            network,
            "/fapi/v1/order",
            {
                "symbol": safe_symbol,
                "side": entry_side,
                "positionSide": actual_position_side,
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": _format_order_quantity(entry_quantity),
                "price": _format_order_price(rounded_entry_price),
                "newOrderRespType": "RESULT",
                "newClientOrderId": client_order_id,
            },
            method="POST",
            api_key=api_key,
            api_secret=api_secret,
            signed=True,
        )
    except BinanceApiError as exc:
        # A rejected entry must never be followed by a conditional order.
        if exc.is_transport_error or exc.status_code >= 500 or exc.is_transient:
            recovered = _recover_futures_entry_order(
                network,
                api_key,
                api_secret,
                symbol=safe_symbol,
                client_order_id=client_order_id,
            )
            if recovered is not None:
                entry_response = recovered
            else:
                raise BinanceEntryOrderError(
                    f"{exc}；未能确认入场单状态，请稍后在 Binance 委托中核对订单 {client_order_id}",
                    status_code=exc.status_code,
                    exchange_code=exc.exchange_code,
                ) from exc
        else:
            raise BinanceEntryOrderError(
                str(exc) or "交易所拒绝了入场限价单",
                status_code=exc.status_code,
                exchange_code=exc.exchange_code,
            ) from exc

    if not isinstance(entry_response, dict) or not (
        entry_response.get("orderId") or entry_response.get("clientOrderId")
    ):
        # A successful HTTP response can still have an empty/truncated body.
        # The client id is the idempotency key, so reconcile it before telling
        # the caller that the entry failed.  This prevents an accepted LIMIT
        # order from being reported as a generic 502 and submitted again.
        recovered = _recover_futures_entry_order(
            network,
            api_key,
            api_secret,
            symbol=safe_symbol,
            client_order_id=client_order_id,
        )
        if recovered is not None:
            entry_response = recovered
        else:
            raise BinanceEntryOrderError("交易所未返回有效的入场限价单结果")
    entry_order_id = entry_response.get("orderId") or entry_response.get("clientOrderId")
    if entry_order_id is None:
        raise BinanceEntryOrderError("交易所未返回入场限价单编号")

    entry_status = str(entry_response.get("status") or "NEW").strip().upper()
    if entry_status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
        raise BinanceEntryOrderError(f"入场限价单未成功，交易所状态：{entry_status}", status_code=400)
    if entry_status not in {"NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_NEW"}:
        raise BinanceEntryOrderError(f"入场限价单返回未知状态：{entry_status}")

    # Protection belongs to the live position, not to the pending entry
    # order. Copy trades intentionally do not configure protection orders.
    protection_status = "NOT_CONFIGURED" if not protection_enabled else "WAITING_ENTRY_FILL"
    protection_message = "跟单未配置止盈止损" if not protection_enabled else "入场限价单尚未成交；成交后由后台按实际持仓数量自动设置保护单"
    if protection_enabled and entry_status in {"PARTIALLY_FILLED", "FILLED"}:
        protection_status = "WAITING_POSITION_CONFIRMATION"
        protection_message = "入场单已部分或全部成交；等待账户确认实际持仓后自动设置保护单"
    target_quantity_summary = _target_quantity_summary(
        protective_target_quantity,
        first_target_quantity,
        extension_target_quantity,
    )
    return {
        "scope": "LIMIT_PLAN",
        "symbol": safe_symbol,
        "direction": safe_direction,
        "positionSide": actual_position_side,
        "leverage": int(leverage),
        "entryOrder": entry_response,
        "entryStatus": entry_status,
        "entryAccepted": True,
        "entryPrice": float(rounded_entry_price),
        "quantity": float(entry_quantity),
        "protectiveTakeProfitRatio": protective_ratio,
        "firstTakeProfitRatio": target_ratio,
        "secondTakeProfitRatio": extension_ratio,
        "protectiveTargetQuantity": float(protective_target_quantity) if protective_target_quantity is not None else None,
        "firstTargetQuantity": float(first_target_quantity) if first_target_quantity is not None else None,
        "extensionTargetQuantity": float(extension_target_quantity) if extension_target_quantity is not None else None,
        **target_quantity_summary,
        "takeProfitOrderType": _quantity_take_profit_order_type() if protection_enabled else None,
        "takeProfitWorkingType": "CONTRACT_PRICE",
        "positionTakeProfit": None,
        "partialTakeProfit": float(rounded_first_take_profit) if rounded_first_take_profit is not None else None,
        "protectiveTakeProfit": float(rounded_protective_take_profit) if protective_target_quantity is not None and rounded_protective_take_profit is not None else None,
        "extensionTakeProfit": float(rounded_extension_take_profit) if extension_target_quantity is not None and rounded_extension_take_profit is not None else None,
        "fallback": target_split_fallback,
        "runnerManagedByMovingStop": False if not protection_enabled else rounded_extension_take_profit is None and target_split_fallback != "FIRST_TARGET_FULL_QUANTITY",
        "fallbackReason": (
            _target_split_fallback_reason(
                target_split_fallback,
                subject="入场数量",
                protective_ratio=protective_ratio,
                first_ratio=target_ratio,
                second_ratio=extension_ratio,
                extension_requested=rounded_extension_take_profit is not None,
            ) if protection_enabled else None
        ),
        "protectionStatus": protection_status,
        "protectionPending": protection_enabled,
        "protectionMessage": protection_message,
        "orders": [],
    }


def _get_position_risk(network: str, api_key: str, api_secret: str) -> list[dict]:
    payload = _request(network, "/fapi/v2/positionRisk", api_key=api_key, api_secret=api_secret, signed=True)
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的合约持仓格式无效")
    return [item for item in payload if isinstance(item, dict)]


def _get_position_risk_with_latest_price(network: str, api_key: str, api_secret: str) -> list[dict]:
    """Add the latest traded price needed by target validation to positionRisk."""

    positions = _get_position_risk(network, api_key, api_secret)
    if not positions:
        return positions
    try:
        tickers, _stale = _ticker_24h(network)
    except Exception:
        return positions
    latest_prices = {
        str(item.get("symbol") or "").upper(): item.get("lastPrice")
        for item in tickers
        if isinstance(item, dict) and item.get("lastPrice") not in (None, "")
    }
    if not latest_prices:
        return positions
    return [
        {
            **position,
            "lastPrice": latest_prices.get(
                str(position.get("symbol") or "").upper(),
                position.get("lastPrice"),
            ),
        }
        for position in positions
    ]


def _get_open_futures_orders(network: str, api_key: str, api_secret: str, symbol: str | None = None) -> list[dict]:
    params = {"symbol": symbol} if symbol else None
    payload = _request(
        network,
        "/fapi/v1/openOrders",
        params,
        api_key=api_key,
        api_secret=api_secret,
        signed=True,
    )
    if isinstance(payload, dict):
        payload = payload.get("orders", payload.get("data", []))
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的合约普通委托格式无效")
    result = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        order_type = str(item.get("type") or item.get("orderType") or "").upper()
        result.append(
            {
                **item,
                "orderId": item.get("orderId"),
                "type": order_type,
                "price": item.get("price") or item.get("stopPrice"),
                "stopPrice": item.get("stopPrice") or (item.get("price") if order_type in {"STOP", "TAKE_PROFIT"} else None),
                "quantity": item.get("origQty") or item.get("quantity"),
                "closePosition": item.get("closePosition", False),
                "orderSource": "STANDARD",
                "status": item.get("status"),
            }
        )
    return result


def _get_open_algo_orders(network: str, api_key: str, api_secret: str) -> list[dict]:
    payload = _request(network, "/fapi/v1/openAlgoOrders", api_key=api_key, api_secret=api_secret, signed=True)
    if isinstance(payload, dict):
        payload = payload.get("orders", payload.get("data", []))
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的 Algo 委托格式无效")
    result = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        order_type = str(item.get("orderType") or item.get("type") or "").upper()
        algo_id = item.get("algoId")
        trigger_price = item.get("triggerPrice")
        if trigger_price is None:
            trigger_price = item.get("stopPrice")
        result.append(
            {
                **item,
                "algoId": algo_id,
                "orderId": item.get("orderId") or algo_id,
                "type": order_type,
                "stopPrice": trigger_price,
                "price": None,
                "orderSource": "ALGO",
                "status": item.get("algoStatus") or item.get("status"),
            }
        )
    return result


def _order_flag(value: object) -> bool:
    return value is True or str(value or "").strip().lower() == "true"


def _order_reduces_one_way_position(order: dict) -> bool:
    return _order_flag(order.get("reduceOnly")) or _order_flag(order.get("closePosition"))


def _matches_futures_order_direction(order: dict, symbol: str, direction: str) -> bool:
    if str(order.get("symbol") or "").upper() != symbol:
        return False
    order_position_side = str(order.get("positionSide") or "BOTH").upper()
    if order_position_side == direction:
        return True
    if order_position_side != "BOTH":
        return False

    order_side = str(order.get("side") or "").upper()
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    if _order_reduces_one_way_position(order):
        return order_side == close_side
    return order_side == entry_side


@_serialize_protection_update
def cancel_futures_direction_orders(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    direction: str,
) -> dict:
    """Cancel every open entry and exit order belonging to one plan direction."""

    safe_symbol = _symbol(symbol)
    safe_direction = str(direction or "").strip().upper()
    if safe_direction not in {"LONG", "SHORT"}:
        raise ValueError("计划方向必须是 LONG 或 SHORT")

    with _protection_update_lock:
        standard_orders = _get_open_futures_orders(network, api_key, api_secret, safe_symbol)
        algo_orders = _get_open_algo_orders(network, api_key, api_secret)
        matching_standard_orders = [
            order
            for order in standard_orders
            if _matches_futures_order_direction(order, safe_symbol, safe_direction)
        ]
        matching_algo_orders = [
            order
            for order in algo_orders
            if _matches_futures_order_direction(order, safe_symbol, safe_direction)
        ]
        cancelled_standard_order_ids: list[object] = []
        cancelled_algo_order_ids: list[object] = []
        failures: list[str] = []

        for order in matching_standard_orders:
            order_id = order.get("orderId")
            if order_id is None:
                failures.append("普通委托缺少 orderId")
                continue
            try:
                _request(
                    network,
                    "/fapi/v1/order",
                    {"symbol": safe_symbol, "orderId": order_id},
                    method="DELETE",
                    api_key=api_key,
                    api_secret=api_secret,
                    signed=True,
                )
                cancelled_standard_order_ids.append(order_id)
            except BinanceApiError as exc:
                if exc.exchange_code == -2011:
                    cancelled_standard_order_ids.append(order_id)
                    continue
                failures.append(f"普通委托 {order_id}: {str(exc) or '取消失败'}")
            except Exception as exc:
                failures.append(f"普通委托 {order_id}: {str(exc) or '取消失败'}")

        for order in matching_algo_orders:
            algo_id = order.get("algoId") or order.get("orderId")
            if algo_id is None:
                failures.append("条件委托缺少 algoId")
                continue
            try:
                _request(
                    network,
                    "/fapi/v1/algoOrder",
                    {"symbol": safe_symbol, "algoId": algo_id},
                    method="DELETE",
                    api_key=api_key,
                    api_secret=api_secret,
                    signed=True,
                )
                cancelled_algo_order_ids.append(algo_id)
            except BinanceApiError as exc:
                if exc.exchange_code == -2011:
                    cancelled_algo_order_ids.append(algo_id)
                    continue
                failures.append(f"条件委托 {algo_id}: {str(exc) or '取消失败'}")
            except Exception as exc:
                failures.append(f"条件委托 {algo_id}: {str(exc) or '取消失败'}")

    cancelled_count = len(cancelled_standard_order_ids) + len(cancelled_algo_order_ids)
    if failures:
        raise BinanceApiError(
            f"同向委托清理未完成（已取消 {cancelled_count} 笔）：{'；'.join(failures)}"
        )
    return {
        "symbol": safe_symbol,
        "direction": safe_direction,
        "matchedStandardOrderCount": len(matching_standard_orders),
        "matchedAlgoOrderCount": len(matching_algo_orders),
        "cancelledStandardOrderIds": cancelled_standard_order_ids,
        "cancelledAlgoOrderIds": cancelled_algo_order_ids,
        "cancelledCount": cancelled_count,
    }


def _futures_position_item(raw_position: object, quantity_rules: dict[str, object] | None = None) -> dict | None:
    if not isinstance(raw_position, dict):
        return None
    position_amount = _as_number(raw_position.get("positionAmt"))
    if abs(position_amount) <= 0:
        return None
    position_side = str(raw_position.get("positionSide") or "BOTH").upper()
    if position_side not in {"BOTH", "LONG", "SHORT"}:
        position_side = "BOTH"
    side = "LONG" if position_side == "LONG" or (position_side == "BOTH" and position_amount > 0) else "SHORT"
    entry_price = _as_number(raw_position.get("entryPrice"))
    mark_price = _as_number(raw_position.get("markPrice"))
    last_price = _as_number(raw_position.get("lastPrice"))
    unrealized_profit = _as_number(raw_position.get("unRealizedProfit") or raw_position.get("unrealizedProfit"))
    notional = abs(_as_number(raw_position.get("notional")))
    leverage = _as_number(raw_position.get("leverage"))
    reported_initial_margin = _as_number(
        raw_position.get("initialMargin")
        or raw_position.get("positionInitialMargin")
    )
    # Use entry notional for the ROE denominator whenever possible.  The
    # account endpoint's reported initialMargin can be mark-price based, so
    # using it directly makes ROE jump even when the position size is fixed.
    initial_margin = (
        abs(position_amount) * entry_price / leverage
        if entry_price > 0 and leverage > 0
        else reported_initial_margin
    )
    if initial_margin <= 0 and leverage > 0:
        initial_margin = notional / leverage
    roe = unrealized_profit / initial_margin * 100 if initial_margin > 0 else 0.0
    item = {
        "symbol": raw_position.get("symbol"),
        "positionSide": position_side,
        "side": side,
        "quantity": abs(position_amount),
        "positionAmt": position_amount,
        "entryPrice": entry_price,
        "lastPrice": last_price,
        "markPrice": mark_price,
        "unrealizedProfit": unrealized_profit,
        "roePercent": roe,
        "notional": notional,
        "leverage": leverage,
        "liquidationPrice": _as_number(raw_position.get("liquidationPrice")),
        "marginType": raw_position.get("marginType"),
        "isolatedMargin": _as_number(raw_position.get("isolatedMargin")),
        "initialMargin": initial_margin,
        "maintMargin": _as_number(raw_position.get("maintMargin")),
        "updateTime": raw_position.get("updateTime"),
    }
    if isinstance(quantity_rules, dict):
        item.update(quantity_rules)
    return item


def get_futures_user_trades(
    network: str,
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    limit: int = 1000,
) -> list[dict]:
    """Read recent user trades used to attribute realized PnL by position side."""

    safe_symbol = str(symbol or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(safe_symbol):
        raise ValueError("合约代码无效")
    safe_limit = max(1, min(int(limit), 1000))
    payload = _request(
        network,
        "/fapi/v1/userTrades",
        params={"symbol": safe_symbol, "limit": safe_limit},
        api_key=api_key,
        api_secret=api_secret,
        signed=True,
    )
    if not isinstance(payload, list):
        raise BinanceApiError("交易所返回的成交记录格式无效")
    return [item for item in payload if isinstance(item, dict)]


def _trade_net_realized_pnl(trade: dict, symbol: str) -> float | None:
    try:
        realized = float(trade.get("realizedPnl"))
    except (TypeError, ValueError):
        return None
    try:
        commission = abs(float(trade.get("commission") or 0))
    except (TypeError, ValueError):
        commission = 0.0
    commission_asset = str(trade.get("commissionAsset") or "").strip().upper()
    quote_asset = next((asset for asset in ("USDT", "USDC", "BUSD") if symbol.endswith(asset)), "")
    if commission and (not commission_asset or commission_asset == quote_asset):
        realized -= commission
    return realized


def _current_position_realized_pnl(trades: list[dict], position: dict) -> float | None:
    """Return net realized PnL for the currently open holding lifecycle only."""

    symbol = str(position.get("symbol") or "").strip().upper()
    position_side = str(position.get("positionSide") or "BOTH").strip().upper()
    if position_side not in {"BOTH", "LONG", "SHORT"}:
        position_side = "BOTH"
    relevant = [
        trade
        for trade in trades
        if str(trade.get("symbol") or symbol).strip().upper() == symbol
        and str(trade.get("positionSide") or "BOTH").strip().upper() == position_side
    ]
    relevant.sort(key=lambda trade: (int(trade.get("time") or 0), int(trade.get("id") or 0)))
    try:
        current_amount = float(position.get("positionAmt"))
    except (TypeError, ValueError):
        current_amount = float(position.get("quantity") or 0)
    target = current_amount if position_side == "BOTH" else abs(current_amount)
    tolerance = max(1e-8, abs(target) * 1e-6)
    state = 0.0
    episode_pnl = None
    for trade in relevant:
        try:
            quantity = abs(float(trade.get("qty") or trade.get("quantity") or 0))
        except (TypeError, ValueError):
            quantity = 0.0
        trade_side = str(trade.get("side") or "").strip().upper()
        if quantity <= 0 or trade_side not in {"BUY", "SELL"}:
            continue
        if position_side == "SHORT":
            delta = quantity if trade_side == "SELL" else -quantity
        else:
            delta = quantity if trade_side == "BUY" else -quantity
        if abs(state) <= 1e-9 and episode_pnl is None and delta * target < -tolerance:
            # The returned history starts inside an older position lifecycle;
            # do not mistake its closing trades for a new opening.
            return None
        before = state
        after = state + delta
        if abs(before) <= 1e-9 and abs(after) > 1e-9:
            episode_pnl = 0.0
        elif before * after < -1e-9:
            # A single order flipped direction. The closing part belongs to
            # the prior lifecycle; start the new lifecycle at the remaining side.
            episode_pnl = 0.0
        net_pnl = _trade_net_realized_pnl(trade, symbol)
        if episode_pnl is not None and net_pnl is not None:
            episode_pnl += net_pnl
        state = after
        if abs(state) <= 1e-9:
            episode_pnl = None

    if episode_pnl is None or abs(state - target) > tolerance:
        return None
    return round(episode_pnl, 8)


def _realized_pnl_by_position(
    network: str,
    api_key: str,
    api_secret: str,
    positions: list[dict],
) -> tuple[dict[tuple[str, str], float], set[str]]:
    """Aggregate recent realized PnL by symbol and positionSide.

    A failed symbol query is returned separately so the account snapshot can
    remain usable without presenting a misleading zero value.
    """

    symbols = sorted({
        str(position.get("symbol") or "").strip().upper()
        for position in positions
        if isinstance(position, dict) and position.get("symbol")
    })
    if not symbols:
        return {}, set()
    realized: dict[tuple[str, str], float] = {}
    failed_symbols: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(4, len(symbols)), thread_name_prefix="binance-realized-pnl") as executor:
        futures = {
            symbol: executor.submit(
                get_futures_user_trades,
                network,
                api_key,
                api_secret,
                symbol=symbol,
            )
            for symbol in symbols
        }
        for symbol, future in futures.items():
            try:
                trades = future.result()
            except Exception:
                failed_symbols.add(symbol)
                continue
            for position in positions:
                if str(position.get("symbol") or "").strip().upper() != symbol:
                    continue
                position_side = str(position.get("positionSide") or "BOTH").strip().upper()
                value = _current_position_realized_pnl(trades, position)
                if value is not None:
                    realized[(symbol, position_side)] = value
    return realized, failed_symbols


def _protection_reference_price(position: dict, *, is_stop: bool) -> tuple[float, str]:
    """Return the price source used to validate a protection trigger.

    Mark price is the risk reference for stops.  Take-profits are validated
    against the latest traded price so the order trigger and the displayed
    target semantics agree.  Older account snapshots may not contain a
    latest price, so they safely fall back to mark price and then entry.
    """

    mark_price = _as_number(position.get("markPrice"))
    entry_price = _as_number(position.get("entryPrice"))
    if is_stop:
        return (mark_price or entry_price, "标记价" if mark_price > 0 else "开仓均价")
    last_price = _as_number(position.get("lastPrice"))
    if last_price > 0:
        return last_price, "最新成交价"
    if mark_price > 0:
        return mark_price, "标记价"
    return entry_price, "开仓均价"


def _find_futures_position(positions: list[dict], symbol: str, position_side: str) -> dict | None:
    exact = [
        item
        for item in positions
        if str(item.get("symbol") or "").upper() == symbol and str(item.get("positionSide") or "BOTH").upper() == position_side
    ]
    if exact:
        return next((item for item in exact if abs(_as_number(item.get("positionAmt"))) > 0), None)
    if position_side == "BOTH":
        return next((item for item in positions if str(item.get("symbol") or "").upper() == symbol and abs(_as_number(item.get("positionAmt"))) > 0), None)
    return None


def _symbol_price_filter(network: str, symbol: str) -> tuple[Decimal, Decimal | None, Decimal | None]:
    exchange_info, _ = _exchange_info(network)
    for contract in exchange_info.get("symbols") or []:
        if not isinstance(contract, dict) or str(contract.get("symbol") or "").upper() != symbol:
            continue
        for item in contract.get("filters") or []:
            if not isinstance(item, dict) or item.get("filterType") != "PRICE_FILTER":
                continue
            try:
                tick_size = Decimal(str(item.get("tickSize") or "0"))
                min_price = Decimal(str(item.get("minPrice"))) if item.get("minPrice") else None
                max_price = Decimal(str(item.get("maxPrice"))) if item.get("maxPrice") else None
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise BinanceApiError("交易所返回的交易对价格精度无效") from exc
            if tick_size > 0:
                return tick_size, min_price, max_price
    if _use_websocket_api.get():
        inferred = _websocket_inferred_contract_rules(network, symbol)
        if inferred and inferred.get("PRICE_FILTER"):
            return inferred["PRICE_FILTER"]
    raise BinanceApiError("交易所未返回该交易对的价格精度")


def _symbol_quantity_filter(
    network: str,
    symbol: str,
    *,
    market: bool = True,
) -> tuple[Decimal, Decimal | None, Decimal | None]:
    exchange_info, _ = _exchange_info(network)
    for contract in exchange_info.get("symbols") or []:
        if not isinstance(contract, dict) or str(contract.get("symbol") or "").upper() != symbol:
            continue
        filters = [item for item in contract.get("filters") or [] if isinstance(item, dict)]
        # Conditional market exits use MARKET_LOT_SIZE; a LIMIT entry uses LOT_SIZE.
        preferred = "MARKET_LOT_SIZE" if market else "LOT_SIZE"
        secondary = "LOT_SIZE" if market else "MARKET_LOT_SIZE"
        ordered_filters = [
            item for item in filters if item.get("filterType") == preferred
        ] + [item for item in filters if item.get("filterType") == secondary]
        for item in ordered_filters:
            try:
                step_size = Decimal(str(item.get("stepSize") or "0"))
                min_quantity = Decimal(str(item.get("minQty"))) if item.get("minQty") else None
                max_quantity = Decimal(str(item.get("maxQty"))) if item.get("maxQty") else None
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise BinanceApiError("交易所返回的交易对数量精度无效") from exc
            if step_size > 0:
                return step_size, min_quantity, max_quantity
    if _use_websocket_api.get():
        inferred = _websocket_inferred_contract_rules(network, symbol)
        if inferred:
            preferred = "MARKET_LOT_SIZE" if market else "LOT_SIZE"
            if inferred.get(preferred):
                return inferred[preferred]
    raise BinanceApiError("交易所未返回该交易对的数量精度")


def _round_price_to_tick(value: float, tick_size: Decimal, rounding: str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
        units = (decimal_value / tick_size).to_integral_value(rounding=rounding)
        rounded = units * tick_size
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("保护价无法按交易所精度处理") from exc
    if rounded <= 0:
        raise ValueError("保护价按交易所精度处理后必须为正数")
    return rounded


def normalize_futures_stop_loss_price(network: str, symbol: str, side: str, stop_loss: float) -> float:
    """Return the exact trigger price Binance will use for a position stop."""

    safe_symbol = _symbol(symbol)
    safe_side = str(side or "").strip().upper()
    if safe_side not in {"LONG", "SHORT"}:
        raise ValueError("持仓方向无效")
    if not isinstance(stop_loss, (int, float)) or not math.isfinite(stop_loss) or stop_loss <= 0:
        raise ValueError("止损价必须是正数")
    tick_size, _, _ = _symbol_price_filter(network, safe_symbol)
    rounding = ROUND_DOWN if safe_side == "LONG" else ROUND_UP
    return float(_round_price_to_tick(stop_loss, tick_size, rounding))


def _round_quantity_to_step(
    value: float | Decimal,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str = "当前持仓数量不足以按交易所精度平仓一半",
) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
        units = (decimal_value / step_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = units * step_size
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("平仓数量无法按交易所精度处理") from exc
    if rounded <= 0 or (min_quantity is not None and rounded < min_quantity):
        raise ValueError(too_small_message)
    if max_quantity is not None and rounded > max_quantity:
        raise ValueError("平仓数量超过交易所允许的最大数量")
    return rounded


def _quantity_at_ratio(
    full_quantity: Decimal,
    ratio: float | Decimal,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str,
) -> Decimal:
    """Round a cumulative position ratio without going through float."""

    rounded = _quantity_boundary_at_ratio(
        full_quantity,
        ratio,
        step_size,
        max_quantity,
    )
    if rounded <= 0 or (min_quantity is not None and rounded < min_quantity):
        raise ValueError(too_small_message)
    return rounded


def _quantity_boundary_at_ratio(
    full_quantity: Decimal,
    ratio: float | Decimal,
    step_size: Decimal,
    max_quantity: Decimal | None,
) -> Decimal:
    """Floor a cumulative ratio while allowing a zero boundary for fallback logic."""

    try:
        ratio_decimal = ratio if isinstance(ratio, Decimal) else Decimal(str(ratio))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("止盈比例无法按交易所精度处理") from exc
    try:
        units = (full_quantity * ratio_decimal / Decimal("100") / step_size).to_integral_value(rounding=ROUND_DOWN)
        rounded = units * step_size
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("止盈比例无法按交易所精度处理") from exc
    if max_quantity is not None and rounded > max_quantity:
        raise ValueError("平仓数量超过交易所允许的最大数量")
    return rounded


def _validate_incremental_quantity(
    quantity: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str,
) -> Decimal:
    """Validate a step-aligned increment produced from two cumulative levels."""

    if quantity <= 0 or (min_quantity is not None and quantity < min_quantity):
        raise ValueError(too_small_message)
    if max_quantity is not None and quantity > max_quantity:
        raise ValueError("平仓数量超过交易所允许的最大数量")
    return quantity


def _split_incremental_quantities(
    full_quantity: Decimal,
    cumulative_ratios: list[float | Decimal],
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str,
) -> list[Decimal]:
    """Convert cumulative exit boundaries into safe, step-aligned legs."""

    previous_cumulative = Decimal("0")
    quantities: list[Decimal] = []
    for ratio in cumulative_ratios:
        cumulative = _quantity_at_ratio(
            full_quantity,
            ratio,
            step_size,
            None,
            max_quantity,
            too_small_message=too_small_message,
        )
        leg = cumulative - previous_cumulative
        quantities.append(
            _validate_incremental_quantity(
                leg,
                min_quantity,
                max_quantity,
                too_small_message=too_small_message,
            )
        )
        previous_cumulative = cumulative
    return quantities


def _extension_fallback_quantity(
    full_quantity: Decimal,
    first_quantity: Decimal,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str,
    reserved_quantity: Decimal | None = None,
) -> tuple[Decimal | None, str | None]:
    """Use the documented half-position fallback only when it is executable."""

    reserved = reserved_quantity if reserved_quantity is not None else first_quantity
    half_quantity = _quantity_boundary_at_ratio(
        full_quantity,
        Decimal("50"),
        step_size,
        max_quantity,
    )
    if (
        _quantity_is_executable(half_quantity, min_quantity, max_quantity)
        and reserved + half_quantity <= full_quantity
    ):
        return half_quantity, "EXTENSION_TARGET_HALF_POSITION"
    remainder = full_quantity - reserved
    if _quantity_is_executable(remainder, min_quantity, max_quantity):
        return remainder, "EXTENSION_TARGET_REMAINDER"
    return None, "EXTENSION_TARGET_UNAVAILABLE"


def _quantity_is_executable(
    quantity: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
) -> bool:
    """Check one already step-aligned order quantity against LOT_SIZE."""

    return (
        quantity > 0
        and (min_quantity is None or quantity >= min_quantity)
        and (max_quantity is None or quantity <= max_quantity)
    )


def _split_plan_take_profit_quantities(
    position_quantity: float,
    first_target_ratio: float,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    second_target_ratio: float = 75,
    too_small_message: str,
) -> tuple[Decimal, Decimal | None, str | None]:
    """Split a plan into two cumulative exits of the original position."""

    first_ratio, second_ratio = _plan_take_profit_ratios(
        first_target_ratio,
        second_target_ratio,
        has_extension=second_target_ratio > first_target_ratio,
    )

    full_quantity = _round_quantity_to_step(
        position_quantity,
        step_size,
        min_quantity,
        max_quantity,
        too_small_message=too_small_message,
    )
    first_boundary = _quantity_boundary_at_ratio(full_quantity, first_ratio, step_size, max_quantity)
    second_boundary = _quantity_boundary_at_ratio(full_quantity, second_ratio, step_size, max_quantity)
    first_quantity = first_boundary
    extension_quantity = second_boundary - first_boundary
    if _quantity_is_executable(first_quantity, min_quantity, max_quantity) and _quantity_is_executable(
        extension_quantity,
        min_quantity,
        max_quantity,
    ):
        return first_quantity, extension_quantity, None
    # The first cumulative boundary is the important one. If it cannot be
    # sent as an order, do not manufacture a tiny first leg; use the whole
    # valid position at the first target instead.
    if not _quantity_is_executable(first_quantity, min_quantity, max_quantity):
        return full_quantity, None, "FIRST_TARGET_FULL_QUANTITY"
    extension_quantity, fallback = _extension_fallback_quantity(
        full_quantity,
        first_quantity,
        step_size,
        min_quantity,
        max_quantity,
        too_small_message=too_small_message,
    )
    return first_quantity, extension_quantity, fallback


def _plan_take_profit_ladder_ratios(
    protective_target_ratio: object,
    first_target_ratio: object,
    second_target_ratio: object,
    *,
    has_extension: bool = True,
) -> tuple[float, float, float]:
    """Validate the cumulative ratios used by the three-target ladder."""

    try:
        protective_ratio = float(protective_target_ratio)
        first_ratio = float(first_target_ratio)
        second_ratio = float(second_target_ratio)
    except (TypeError, ValueError):
        raise ValueError("三档止盈累计比例必须是数字") from None
    if not has_extension and math.isfinite(first_ratio) and math.isfinite(second_ratio) and first_ratio > second_ratio:
        raise ValueError("没有扩展目标时，第一目标累计比例不能高于第二档配置")
    if not has_extension and math.isfinite(first_ratio) and math.isfinite(second_ratio):
        # No extension structure means the first structural target is the
        # final fixed target. Promote its effective cumulative allocation to
        # the configured second boundary, leaving the rest for
        # the moving-stop runner.
        first_ratio = second_ratio
    if (
        not math.isfinite(protective_ratio)
        or not 1 <= protective_ratio < 100
        or not math.isfinite(first_ratio)
        or not protective_ratio < first_ratio < 100
        or not math.isfinite(second_ratio)
        or not (first_ratio < second_ratio < 100 if has_extension else first_ratio <= second_ratio < 100)
    ):
        raise ValueError("三档止盈累计比例必须满足近端保护目标 < 第一目标 <= 第二目标，且第二目标小于 100%；有扩展目标时第一目标必须严格小于第二目标")
    return protective_ratio, first_ratio, second_ratio


def _split_plan_take_profit_ladder_quantities(
    position_quantity: float,
    protective_target_ratio: float,
    first_target_ratio: float,
    second_target_ratio: float,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    has_extension: bool,
    too_small_message: str,
) -> tuple[Decimal | None, Decimal, Decimal | None, str | None]:
    """Split a position into incremental quantities for three target roles."""

    protective_ratio, first_ratio, second_ratio = _plan_take_profit_ladder_ratios(
        protective_target_ratio,
        first_target_ratio,
        second_target_ratio,
        has_extension=has_extension,
    )
    full_quantity = _round_quantity_to_step(
        position_quantity,
        step_size,
        min_quantity,
        max_quantity,
        too_small_message=too_small_message,
    )
    protective_boundary = _quantity_boundary_at_ratio(full_quantity, protective_ratio, step_size, max_quantity)
    first_boundary = _quantity_boundary_at_ratio(full_quantity, first_ratio, step_size, max_quantity)
    extension_boundary = (
        _quantity_boundary_at_ratio(full_quantity, second_ratio, step_size, max_quantity)
        if has_extension
        else None
    )
    protective_quantity = protective_boundary
    first_quantity = first_boundary - protective_boundary
    extension_quantity = (
        extension_boundary - first_boundary
        if extension_boundary is not None
        else None
    )

    protective_ok = _quantity_is_executable(protective_quantity, min_quantity, max_quantity)
    first_ok = _quantity_is_executable(first_quantity, min_quantity, max_quantity)
    extension_ok = extension_quantity is None or _quantity_is_executable(extension_quantity, min_quantity, max_quantity)

    if protective_ok and first_ok and extension_ok:
        return protective_quantity, first_quantity, extension_quantity, None

    # A near-target leg is optional. Collapse it into the first cumulative
    # boundary whenever it is too small, then independently apply the same
    # two-target fallback rules used when no near target exists.
    if not protective_ok or not first_ok:
        collapsed_first = first_boundary
        if not _quantity_is_executable(collapsed_first, min_quantity, max_quantity):
            return None, full_quantity, None, "FIRST_TARGET_FULL_QUANTITY"
        if not has_extension:
            return None, collapsed_first, None, "PROTECTIVE_TARGET_UNAVAILABLE"
        collapsed_extension = extension_boundary - first_boundary
        if _quantity_is_executable(collapsed_extension, min_quantity, max_quantity):
            return None, collapsed_first, collapsed_extension, "PROTECTIVE_TARGET_UNAVAILABLE"
        fallback_quantity, fallback = _extension_fallback_quantity(
            full_quantity,
            collapsed_first,
            step_size,
            min_quantity,
            max_quantity,
            too_small_message=too_small_message,
            reserved_quantity=collapsed_first,
        )
        if fallback_quantity is None:
            return None, collapsed_first, None, "PROTECTIVE_TARGET_UNAVAILABLE"
        if fallback in {"EXTENSION_TARGET_HALF_POSITION", "EXTENSION_TARGET_REMAINDER"}:
            return None, collapsed_first, fallback_quantity, "PROTECTIVE_TARGET_UNAVAILABLE"
        return None, collapsed_first, fallback_quantity, fallback

    # The near and first legs are valid; only the extension increment is too
    # small. Preserve both earlier exits and use the documented half-position
    # fallback (or the remaining executable quantity).
    fallback_quantity, fallback = _extension_fallback_quantity(
        full_quantity,
        first_quantity,
        step_size,
        min_quantity,
        max_quantity,
        too_small_message=too_small_message,
        reserved_quantity=protective_quantity + first_quantity,
    )
    return protective_quantity, first_quantity, fallback_quantity, fallback


def _first_plan_take_profit_quantity(
    position_quantity: float,
    first_target_ratio: float,
    step_size: Decimal,
    min_quantity: Decimal | None,
    max_quantity: Decimal | None,
    *,
    too_small_message: str,
) -> tuple[Decimal, str | None]:
    """Return the first partial exit, retaining a full-exit fallback for tiny positions."""

    full_quantity = _round_quantity_to_step(
        position_quantity,
        step_size,
        min_quantity,
        max_quantity,
        too_small_message=too_small_message,
    )
    try:
        first_quantity = _quantity_at_ratio(
            full_quantity,
            first_target_ratio,
            step_size,
            min_quantity,
            max_quantity,
            too_small_message=too_small_message,
        )
    except ValueError as exc:
        if str(exc) != too_small_message:
            raise
        return full_quantity, "FIRST_TARGET_FULL_QUANTITY"
    return first_quantity, None


def _plan_take_profit_ratios(
    first_target_ratio: object,
    second_target_ratio: object,
    *,
    has_extension: bool = True,
) -> tuple[float, float]:
    try:
        first_ratio = float(first_target_ratio)
        second_ratio = float(second_target_ratio)
    except (TypeError, ValueError):
        raise ValueError("两档止盈累计比例必须是数字") from None
    if not math.isfinite(first_ratio) or not 1 <= first_ratio < 100:
        raise ValueError("第一目标止盈累计比例必须在 1% 到 99% 之间")
    if not math.isfinite(second_ratio) or (not has_extension and first_ratio > second_ratio) or not (first_ratio < second_ratio < 100 if has_extension else second_ratio < 100):
        raise ValueError("第二目标止盈累计比例必须大于或等于第一目标且小于 100%；有扩展目标时必须严格大于第一目标")
    if not has_extension:
        first_ratio = second_ratio
    return first_ratio, second_ratio


def _target_split_fallback_reason(
    fallback: str | None,
    *,
    subject: str,
    protective_ratio: float | None,
    first_ratio: float,
    second_ratio: float,
    extension_requested: bool,
) -> str | None:
    """Explain quantity fallbacks using the ratios that were actually requested."""

    if fallback == "PROTECTIVE_TARGET_UNAVAILABLE":
        return (
            f"{subject}无法按交易所最小数量拆分近端保护目标（本档 {protective_ratio:g}%），"
            f"已跳过近端保护单；第一目标仍按累计 {first_ratio:g}% 挂数量型止盈"
        )
    if fallback == "FIRST_TARGET_FULL_QUANTITY":
        if protective_ratio is not None and extension_requested:
            return f"{subject}无法按交易所最小数量拆分三档止盈，已在第一目标挂数量型全量止盈；近端保护与第二目标未设置"
        if extension_requested:
            return f"{subject}无法按交易所最小数量拆分两档止盈，已在第一目标挂数量型全量止盈；第二目标未设置"
        return f"{subject}不足以按交易所最小数量在第一目标分批止盈，已在第一目标挂数量型全量止盈"
    if fallback == "EXTENSION_TARGET_HALF_POSITION":
        extension_ratio = second_ratio - first_ratio
        return f"{subject}的第二目标本档 {extension_ratio:g}% 数量低于交易所最小单位，已按总仓位 50% 数量止盈"
    if fallback == "EXTENSION_TARGET_REMAINDER":
        extension_ratio = second_ratio - first_ratio
        return f"{subject}的第二目标本档 {extension_ratio:g}% 数量低于交易所最小单位，已按可执行剩余数量止盈"
    if fallback == "EXTENSION_TARGET_UNAVAILABLE":
        return f"{subject}的第二目标数量低于交易所最小单位，暂不挂该档止盈"
    return None


def _target_quantity_summary(
    protective_quantity: Decimal | None,
    first_quantity: Decimal | None,
    extension_quantity: Decimal | None,
) -> dict[str, float | None]:
    """Expose both per-order and cumulative quantities for plan targets.

    The exchange receives incremental quantities.  The UI and audit log also
    need the cumulative amount already assigned to each target; otherwise a
    normal 25% first-target leg after a 25% near-target leg can look like a
    broken 25% order instead of the intended 50% cumulative exit.
    """

    cumulative = Decimal("0")
    result: dict[str, float | None] = {}
    for key, quantity in (
        ("protectiveTarget", protective_quantity),
        ("firstTarget", first_quantity),
        ("extensionTarget", extension_quantity),
    ):
        if quantity is None:
            result[f"{key}Quantity"] = None
            result[f"{key}CumulativeQuantity"] = None
            continue
        cumulative += quantity
        result[f"{key}Quantity"] = float(quantity)
        result[f"{key}CumulativeQuantity"] = float(cumulative)
    return result


def _validate_protection_price(
    price: Decimal,
    reference_price: Decimal | None,
    is_long: bool,
    *,
    is_stop: bool,
    min_price: Decimal | None,
    max_price: Decimal | None,
    reference_label: str = "标记价",
) -> None:
    if min_price is not None and price < min_price or max_price is not None and price > max_price:
        raise ValueError("保护价超出交易所允许的价格范围")
    if reference_price is None:
        return
    if is_stop:
        valid = price < reference_price if is_long else price > reference_price
    else:
        valid = price > reference_price if is_long else price < reference_price
    if not valid:
        raise ValueError(f"保护价按交易所精度处理后不在{reference_label}的正确一侧")


def _matching_protection_orders(position: dict, orders: list[dict]) -> list[dict]:
    close_side = "SELL" if position["side"] == "LONG" else "BUY"
    return [
        {
            "orderId": order.get("orderId"),
            "algoId": order.get("algoId"),
            "orderSource": str(order.get("orderSource") or ("ALGO" if order.get("algoId") is not None else "STANDARD")).upper(),
            "type": str(order.get("type") or "").upper(),
            "side": str(order.get("side") or "").upper(),
            "positionSide": str(order.get("positionSide") or "BOTH").upper(),
            "stopPrice": _as_number(order.get("stopPrice")),
            "price": _as_number(order.get("price")),
            "triggerPrice": _as_number(order.get("triggerPrice")),
            "workingType": order.get("workingType"),
            # Standard futures orders normally expose origQty, while a few
            # adapters expose quantity.  Normalize both so the first target
            # cannot appear as a tiny/empty order merely because the response
            # used Binance's native field name.
            "quantity": _as_number(order.get("quantity") or order.get("origQty")),
            "closePosition": _is_close_position_order(order, position.get("quantity")),
            "status": order.get("status"),
        }
        for order in orders
        if _is_matching_protection_order(order, position["symbol"], position["positionSide"], close_side)
    ]


def _is_matching_protection_order(order: dict, symbol: str, position_side: str, close_side: str) -> bool:
    order_type = str(order.get("type") or "").upper()
    order_position_side = str(order.get("positionSide") or "BOTH").upper()
    order_source = str(order.get("orderSource") or "").upper()
    # In one-way mode our LIMIT exits carry reduceOnly=true. Binance rejects
    # reduceOnly in hedge mode, where positionSide plus the close side is the
    # authoritative direction. Accept hedge-mode LIMIT exits for account
    # reconciliation and replacement of legacy orders.
    if order_type == "LIMIT" and order_source == "STANDARD" and order_position_side == "BOTH" and not (
        _order_flag(order.get("reduceOnly")) or _order_flag(order.get("closePosition"))
    ):
        return False
    return (
        str(order.get("symbol") or "").upper() == symbol
        and order_type in _PROTECTION_ORDER_TYPES
        and str(order.get("side") or "").upper() == close_side
        and order_position_side == position_side
    )


def _is_close_position_order(order: dict, position_quantity: float | Decimal | None = None) -> bool:
    """Identify only an explicit or legacy quantity-less full-position exit.

    A quantity-bearing LIMIT is still a quantity-based take-profit even when
    its quantity happens to equal the current position.  Inferring
    ``closePosition`` from that equality made normal LIMIT targets disappear
    from the partial ladder and reclassified them as position take-profits.
    """

    value = order.get("closePosition")
    if value is True or str(value or "").strip().lower() == "true":
        return True

    if "closePosition" in order:
        return False
    # Older exchange responses omitted closePosition. A quantity-bearing
    # order is partial; an order without quantity is the legacy full exit.
    order_quantity = order.get("quantity")
    if order_quantity in (None, ""):
        order_quantity = order.get("origQty")
    return _as_number(order_quantity) <= 0


def _protection_price(
    orders: list[dict],
    kind: str,
    *,
    close_position: bool | None = None,
    is_long: bool | None = None,
) -> float | None:
    matching = [
        order
        for order in orders
        if (
            kind in str(order.get("type") or "")
            or (kind == "TAKE_PROFIT" and str(order.get("type") or "").upper() == "LIMIT")
        )
        and _as_number(order.get("stopPrice") or order.get("triggerPrice") or order.get("price")) > 0
        and (
            close_position is None
            or (
                # _matching_protection_orders() annotates every account order
                # with the live position quantity. Prefer that normalized
                # result so a full-quantity LIMIT target is recognized as a
                # position-level exit even though Binance cannot set
                # closePosition=true on a normal LIMIT order.
                _order_flag(order.get("closePosition"))
                if "closePosition" in order
                else _is_close_position_order(order)
            )
            == close_position
        )
    ]
    if not matching:
        return None
    prices = [
        _as_number(order.get("stopPrice") or order.get("triggerPrice") or order.get("price"))
        for order in matching
    ]
    prices = [price for price in prices if price > 0]
    if not prices:
        return None
    if kind == "STOP" and is_long is not None:
        # If an old and a replacement stop briefly coexist, the exchange will
        # trigger the one closer to the current price first. Report that
        # effective stop instead of depending on API order-list ordering.
        return max(prices) if is_long else min(prices)
    if kind == "TAKE_PROFIT" and is_long is not None:
        return min(prices) if is_long else max(prices)
    return prices[-1]


def _partial_take_profit_levels(position: dict) -> list[dict]:
    """Expose all quantity-based take-profit orders in execution order."""

    levels = [
        {
            "price": _as_number(order.get("stopPrice") or order.get("triggerPrice") or order.get("price")),
            "quantity": _as_number(order.get("quantity") or order.get("origQty")),
            "orderType": str(order.get("type") or "").upper(),
            "workingType": order.get("workingType") or "CONTRACT_PRICE",
            "priceBasis": "LATEST_PRICE",
        }
        for order in position.get("protectionOrders") or []
        if ("TAKE_PROFIT" in str(order.get("type") or "") or str(order.get("type") or "").upper() == "LIMIT")
        and not _is_close_position_order(order, position.get("quantity"))
        and _as_number(order.get("stopPrice") or order.get("triggerPrice") or order.get("price")) > 0
        and _as_number(order.get("quantity")) > 0
    ]
    is_long = position.get("side") == "LONG"
    levels.sort(key=lambda level: level["price"], reverse=not is_long)
    position_quantity = _as_number(position.get("quantity"))
    cumulative_ratio = 0.0
    for level in levels:
        level["quantityRatio"] = round(level["quantity"] / position_quantity * 100, 2) if position_quantity > 0 else None
        if level["quantityRatio"] is not None:
            cumulative_ratio += level["quantityRatio"]
            level["cumulativeQuantityRatio"] = round(cumulative_ratio, 2)
        else:
            level["cumulativeQuantityRatio"] = None
    return levels


def _format_order_price(value: float | Decimal) -> str:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        return format(decimal_value.normalize(), "f")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("保护价格式无效") from exc


def _format_order_quantity(value: float | Decimal) -> str:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if decimal_value <= 0:
            raise ValueError("平仓数量必须为正数")
        return format(decimal_value.normalize(), "f")
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "平仓数量必须为正数":
            raise
        raise ValueError("平仓数量格式无效") from exc


def _as_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
