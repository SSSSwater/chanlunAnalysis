from __future__ import annotations

"""Synchronous Binance USD-M WebSocket API adapter.

The adapter deliberately opens one authenticated request/response socket per
operation. This keeps order submission isolated from the long-lived market
subscription worker and makes retries/reconciliation use the existing client
IDs in the futures client.
"""

import hashlib
import hmac
import itertools
import time

try:
    from .binance_client import BinanceApiError
    from . import binance_websocket_transport as websocket_transport
except ImportError:  # pragma: no cover
    from binance_client import BinanceApiError
    import binance_websocket_transport as websocket_transport


TIMEOUT_SECONDS = 10
_request_ids = itertools.count(10_000)
ENDPOINTS = {
    "mainnet": "wss://ws-fapi.binance.com/ws-fapi/v1",
    "testnet": "wss://testnet.binancefuture.com/ws-fapi/v1",
}
METHODS = {
    ("GET", "/fapi/v2/account"): "account.status",
    # The REST position-risk resource is exposed by the Futures WebSocket API
    # as ``account.position`` (and not as a versioned REST path).
    ("GET", "/fapi/v2/positionRisk"): "account.position",
    ("GET", "/fapi/v1/openOrders"): "openOrders.status",
    ("GET", "/fapi/v1/order"): "order.status",
    ("POST", "/fapi/v1/order"): "order.place",
    ("DELETE", "/fapi/v1/order"): "order.cancel",
    ("POST", "/fapi/v1/algoOrder"): "algoOrder.place",
    ("DELETE", "/fapi/v1/algoOrder"): "algoOrder.cancel",
}


def request(network: str, endpoint: str, params: dict[str, object] | None, *, method: str, api_key: str = "", api_secret: str = "", signed: bool = False) -> object:
    safe_network = str(network or "mainnet").strip().lower()
    websocket_endpoint = ENDPOINTS.get(safe_network)
    operation = METHODS.get((str(method or "GET").upper(), endpoint))
    if not websocket_endpoint or not operation:
        raise BinanceApiError(f"WebSocket 模式不支持合约接口 {endpoint}", status_code=503)

    request_params = dict(params or {})
    if signed:
        if not api_key or not api_secret:
            raise ValueError("请填写 API Key 和 Secret Key")
        request_params.setdefault("recvWindow", 10_000)
        request_params.setdefault("timestamp", int(time.time() * 1000))
        request_params["apiKey"] = api_key
        signature_payload = "&".join(f"{key}={request_params[key]}" for key in sorted(request_params))
        request_params["signature"] = hmac.new(api_secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
    elif api_key:
        request_params["apiKey"] = api_key

    request_id = next(_request_ids)
    connection = None
    try:
        connection = websocket_transport.open_websocket(websocket_endpoint, timeout=TIMEOUT_SECONDS)
        connection.send_json({"id": request_id, "method": operation, "params": request_params})
        payload = websocket_transport.recv_json_with_timeout(connection, TIMEOUT_SECONDS)
        if not isinstance(payload, dict) or payload.get("id") != request_id:
            raise BinanceApiError("WebSocket API 响应编号无效", status_code=503)
        status = payload.get("status")
        if status is not None and int(status) >= 400:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
            message = str(error.get("msg") or error.get("message") or "WebSocket API 请求失败")
            code = error.get("code")
            raise BinanceApiError(message, status_code=429 if int(status) == 429 else 502, exchange_code=int(code) if code is not None else None)
        return payload.get("result")
    except BinanceApiError:
        raise
    except Exception as exc:
        error = BinanceApiError(websocket_transport.websocket_error_message(exc), status_code=503)
        error.is_transport_error = True
        raise error from exc
    finally:
        if connection is not None:
            websocket_transport.close_socket(connection)
