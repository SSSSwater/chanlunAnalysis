from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

try:
    from .binance_client import BinanceApiError
    from . import binance_websocket_transport as websocket_transport
    from .binance_futures_client import (
        FUTURES_NETWORKS,
        _signed_timestamp as _futures_signed_timestamp,
        _sync_server_time as _sync_futures_server_time,
        _sync_server_time_if_stale as _sync_futures_server_time_if_stale,
    )
except ImportError:  # pragma: no cover - direct module execution compatibility
    from binance_client import BinanceApiError
    import binance_websocket_transport as websocket_transport
    from binance_futures_client import (
        FUTURES_NETWORKS,
        _signed_timestamp as _futures_signed_timestamp,
        _sync_server_time as _sync_futures_server_time,
        _sync_server_time_if_stale as _sync_futures_server_time_if_stale,
    )


WEBSOCKET_TEST_TIMEOUT_SECONDS = 8
ACCOUNT_STATUS_RECV_WINDOW_MS = 10_000
FUTURES_MARK_PRICE_STREAM = "btcusdt@aggTrade"
MARKET_STREAM_DATA_FIELDS = [
    "e",
    "E",
    "s",
    "a",
    "p",
    "q",
    "f",
    "l",
    "T",
    "m",
    "M",
]
FUTURES_WEBSOCKET_URLS = {
    "mainnet": "wss://fstream.binance.com:443/ws",
    "testnet": "wss://stream.binancefuture.com:443/ws",
}
FUTURES_ACCOUNT_WEBSOCKET_URLS = {
    "mainnet": "wss://ws-fapi.binance.com/ws-fapi/v1",
    "testnet": "wss://testnet.binancefuture.com/ws-fapi/v1",
}

ACCOUNT_STATUS_DATA_FIELDS = {
    "account": [
        "feeTier",
        "canTrade",
        "canDeposit",
        "canWithdraw",
        "updateTime",
        "multiAssetsMargin",
        "totalInitialMargin",
        "totalMaintMargin",
        "totalWalletBalance",
        "totalUnrealizedProfit",
        "totalMarginBalance",
        "totalPositionInitialMargin",
        "totalOpenOrderInitialMargin",
        "totalCrossWalletBalance",
        "totalCrossUnPnl",
        "availableBalance",
        "maxWithdrawAmount",
    ],
    "assets": [
        "asset",
        "walletBalance",
        "unrealizedProfit",
        "marginBalance",
        "maintMargin",
        "initialMargin",
        "positionInitialMargin",
        "openOrderInitialMargin",
        "crossWalletBalance",
        "crossUnPnl",
        "availableBalance",
        "maxWithdrawAmount",
        "marginAvailable",
        "updateTime",
    ],
    "positions": [
        "symbol",
        "initialMargin",
        "maintMargin",
        "unrealizedProfit",
        "positionInitialMargin",
        "openOrderInitialMargin",
        "leverage",
        "isolated",
        "entryPrice",
        "breakEvenPrice",
        "maxNotional",
        "bidNotional",
        "askNotional",
        "positionSide",
        "positionAmt",
        "notional",
        "isolatedWallet",
        "updateTime",
        "isolatedMargin",
        "adl",
        "markPrice",
        "liquidationPrice",
    ],
}


def _network(network: object) -> str:
    value = str(network or "mainnet").strip().lower()
    if value not in FUTURES_WEBSOCKET_URLS:
        raise ValueError("网络必须选择主网或测试网")
    return value


def _failure_message(exc: BaseException) -> str:
    text = str(exc or "").lower()
    if "timeout" in text or "timed out" in text:
        return f"WebSocket 连接超时（{WEBSOCKET_TEST_TIMEOUT_SECONDS} 秒）"
    if "proxy" in text or "tunnel" in text:
        return "WebSocket 代理连接失败"
    if any(marker in text for marker in ("ssl", "tls", "handshake", "certificate", "invalid library", "unexpected_eof")):
        return "WebSocket TLS 或握手失败"
    return websocket_transport.websocket_error_message(exc)


def _credentials(api_key: object, api_secret: object) -> tuple[str, str]:
    key = str(api_key or "").strip()
    secret = str(api_secret or "").strip()
    if not key or not secret:
        raise ValueError("请先在账户 API 页保存 API Key 和 Secret Key")
    if len(key) > 256 or len(secret) > 256:
        raise ValueError("Binance API 凭据长度无效")
    return key, secret


def _as_number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _first_value(payload: dict, *keys: str) -> object:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _position_roe_percent(raw_position: dict, position_amount: float, unrealized_profit: float) -> float:
    """Calculate ROE when account.status does not provide a derived field."""

    entry_price = _as_number(raw_position.get("entryPrice"))
    leverage = _as_number(raw_position.get("leverage"))
    notional = abs(_as_number(raw_position.get("notional")))
    reported_initial_margin = _as_number(
        _first_value(raw_position, "initialMargin", "positionInitialMargin")
    )
    initial_margin = (
        abs(position_amount) * entry_price / leverage
        if entry_price > 0 and leverage > 0
        else reported_initial_margin
    )
    if initial_margin <= 0 and leverage > 0:
        initial_margin = notional / leverage
    return unrealized_profit / initial_margin * 100 if initial_margin > 0 else 0.0


def _account_status_params(api_key: str, api_secret: str) -> dict[str, object]:
    params: dict[str, object] = {
        "apiKey": api_key,
        "recvWindow": ACCOUNT_STATUS_RECV_WINDOW_MS,
        "timestamp": _futures_signed_timestamp(),
    }
    signature_payload = urlencode(sorted(params.items()))
    params["signature"] = hmac.new(
        api_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return params


def _best_effort_sync_account_clock(network: str, *, force: bool = False) -> None:
    try:
        base_url = FUTURES_NETWORKS[network]
        if force:
            _sync_futures_server_time(base_url)
        else:
            _sync_futures_server_time_if_stale(base_url)
    except Exception:
        # The request still carries the local clock.  A -1021 response below
        # gets one forced synchronization and a fresh signature.
        pass


def _websocket_error_details(payload: object) -> tuple[str, int | None]:
    if not isinstance(payload, dict):
        return "WebSocket 返回格式无效", None
    candidate = payload.get("error")
    if not isinstance(candidate, dict):
        candidate = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    message = str(candidate.get("msg") or candidate.get("message") or "WebSocket 请求失败").strip()
    try:
        code = int(candidate.get("code")) if candidate.get("code") is not None else None
    except (TypeError, ValueError):
        code = None
    return message, code


def _normalize_account_status(result: dict) -> tuple[dict, list[dict], list[dict]]:
    account = {
        "feeTier": result.get("feeTier"),
        "canTrade": _as_bool(result.get("canTrade")),
        "canDeposit": _as_bool(result.get("canDeposit")),
        "canWithdraw": _as_bool(result.get("canWithdraw")),
        "updateTime": result.get("updateTime"),
        "multiAssetsMargin": _as_bool(result.get("multiAssetsMargin")),
    }
    for field in (
        "totalInitialMargin",
        "totalMaintMargin",
        "totalWalletBalance",
        "totalUnrealizedProfit",
        "totalMarginBalance",
        "totalPositionInitialMargin",
        "totalOpenOrderInitialMargin",
        "totalCrossWalletBalance",
        "totalCrossUnPnl",
        "availableBalance",
        "maxWithdrawAmount",
    ):
        account[field] = _as_number(result.get(field))

    assets = []
    for raw_asset in result.get("assets") or []:
        if not isinstance(raw_asset, dict):
            continue
        asset = str(raw_asset.get("asset") or "").strip().upper()
        if not asset:
            continue
        assets.append(
            {
                "asset": asset,
                "walletBalance": _as_number(raw_asset.get("walletBalance")),
                "unrealizedProfit": _as_number(raw_asset.get("unrealizedProfit")),
                "marginBalance": _as_number(raw_asset.get("marginBalance")),
                "maintMargin": _as_number(raw_asset.get("maintMargin")),
                "initialMargin": _as_number(raw_asset.get("initialMargin")),
                "positionInitialMargin": _as_number(raw_asset.get("positionInitialMargin")),
                "openOrderInitialMargin": _as_number(raw_asset.get("openOrderInitialMargin")),
                "crossWalletBalance": _as_number(raw_asset.get("crossWalletBalance")),
                "crossUnPnl": _as_number(raw_asset.get("crossUnPnl")),
                "availableBalance": _as_number(raw_asset.get("availableBalance")),
                "maxWithdrawAmount": _as_number(raw_asset.get("maxWithdrawAmount")),
                "marginAvailable": _as_bool(raw_asset.get("marginAvailable")),
                "updateTime": raw_asset.get("updateTime"),
            }
        )

    positions = []
    raw_positions = [item for item in result.get("positions") or [] if isinstance(item, dict)]
    for raw_position in raw_positions:
        position_amount = _as_number(raw_position.get("positionAmt"))
        if abs(position_amount) <= 0:
            continue
        position_side = str(raw_position.get("positionSide") or "BOTH").strip().upper()
        if position_side not in {"BOTH", "LONG", "SHORT"}:
            position_side = "BOTH"
        side = "LONG" if position_side == "LONG" or (position_side == "BOTH" and position_amount > 0) else "SHORT"
        unrealized_profit = _as_number(_first_value(raw_position, "unrealizedProfit", "unRealizedProfit"))
        positions.append(
            {
                "symbol": str(raw_position.get("symbol") or "").strip().upper(),
                "positionSide": position_side,
                "side": side,
                "positionAmt": position_amount,
                "quantity": abs(position_amount),
                "initialMargin": _as_optional_number(raw_position.get("initialMargin")),
                "maintMargin": _as_optional_number(raw_position.get("maintMargin")),
                "entryPrice": _as_number(raw_position.get("entryPrice")),
                "breakEvenPrice": _as_optional_number(raw_position.get("breakEvenPrice")),
                "unrealizedProfit": unrealized_profit,
                "roePercent": _position_roe_percent(raw_position, position_amount, unrealized_profit),
                "positionInitialMargin": _as_optional_number(raw_position.get("positionInitialMargin")),
                "openOrderInitialMargin": _as_optional_number(raw_position.get("openOrderInitialMargin")),
                "markPrice": _as_optional_number(raw_position.get("markPrice")),
                "liquidationPrice": _as_optional_number(raw_position.get("liquidationPrice")),
                "notional": _as_optional_number(raw_position.get("notional")),
                "leverage": raw_position.get("leverage"),
                "marginType": raw_position.get("marginType") or ("isolated" if _as_bool(raw_position.get("isolated")) else "cross"),
                "isolated": _as_bool(raw_position.get("isolated")),
                "isolatedMargin": _as_optional_number(raw_position.get("isolatedMargin")),
                "isolatedWallet": _as_optional_number(raw_position.get("isolatedWallet")),
                "maxNotional": _as_optional_number(_first_value(raw_position, "maxNotional", "maxNotionalValue")),
                "maxNotionalValue": _as_optional_number(_first_value(raw_position, "maxNotionalValue", "maxNotional")),
                "bidNotional": _as_optional_number(raw_position.get("bidNotional")),
                "askNotional": _as_optional_number(raw_position.get("askNotional")),
                "adl": raw_position.get("adl"),
                "updateTime": raw_position.get("updateTime"),
            }
        )
    return account, assets, positions


def test_binance_futures_websocket(network: object = "mainnet") -> dict[str, object]:
    """Open one public stream, receive its subscription ACK, then close it.

    This is deliberately a diagnostic probe only. It does not use API keys,
    modify snapshots, or register a long-lived subscription for the product.
    """

    safe_network = _network(network)
    endpoint = FUTURES_WEBSOCKET_URLS[safe_network]
    started_at = time.monotonic()
    connection = None
    try:
        connection = websocket_transport.open_websocket(
            endpoint,
            timeout=WEBSOCKET_TEST_TIMEOUT_SECONDS,
        )
        connected_ms = max(0, round((time.monotonic() - started_at) * 1000))
        connection.send_json({"method": "SUBSCRIBE", "params": [FUTURES_MARK_PRICE_STREAM], "id": 1})
        payload = websocket_transport.recv_json_with_timeout(
            connection,
            WEBSOCKET_TEST_TIMEOUT_SECONDS,
        )
        first_message_ms = max(connected_ms, round((time.monotonic() - started_at) * 1000))
        if not isinstance(payload, dict):
            raise BinanceApiError("WebSocket 订阅确认格式无效", status_code=503)
        if payload.get("code") is not None:
            try:
                exchange_code = int(payload.get("code")) if payload.get("code") is not None else None
            except (TypeError, ValueError):
                exchange_code = None
            raise BinanceApiError(str(payload.get("msg") or "WebSocket 订阅未确认"), status_code=503, exchange_code=exchange_code)
        if payload.get("id") != 1 or payload.get("result", object()) is not None:
            raise BinanceApiError("WebSocket 订阅未确认", status_code=503)
        return {
            "ok": True,
            "network": safe_network,
            "market": "FUTURES",
            "stream": FUTURES_MARK_PRICE_STREAM,
            "connectedMs": connected_ms,
            "firstMessageMs": first_message_ms,
            "eventType": "SUBSCRIBE_ACK",
            "subscriptionAck": True,
            "dataFields": list(MARKET_STREAM_DATA_FIELDS),
            "proxyUsed": websocket_transport.websocket_proxy_enabled(),
            "testOnly": True,
        }
    except BinanceApiError:
        raise
    except Exception as exc:
        raise BinanceApiError(_failure_message(exc), status_code=503) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def test_binance_futures_account_websocket(
    network: object = "mainnet",
    api_key: object = "",
    api_secret: object = "",
) -> dict[str, object]:
    """Query the current futures account and positions over WebSocket API.

    This is a single authenticated ``account.status`` request.  It returns a
    redacted summary and closes the socket immediately; it does not create a
    user-data subscription, update the account cache, or change exchange state.
    """

    safe_network = _network(network)
    key, secret = _credentials(api_key, api_secret)
    endpoint = FUTURES_ACCOUNT_WEBSOCKET_URLS[safe_network]
    started_at = time.monotonic()
    connection = None
    response = None
    try:
        connection = websocket_transport.open_websocket(
            endpoint,
            timeout=WEBSOCKET_TEST_TIMEOUT_SECONDS,
        )
        connected_ms = max(0, round((time.monotonic() - started_at) * 1000))
        _best_effort_sync_account_clock(safe_network)

        for attempt in range(2):
            request_id = attempt + 1
            connection.send_json(
                {
                    "id": request_id,
                    "method": "account.status",
                    "params": _account_status_params(key, secret),
                }
            )
            candidate = websocket_transport.recv_json_with_timeout(
                connection,
                WEBSOCKET_TEST_TIMEOUT_SECONDS,
            )
            if not isinstance(candidate, dict):
                raise BinanceApiError("WebSocket 账户响应格式无效", status_code=503)
            if candidate.get("id") != request_id:
                raise BinanceApiError("WebSocket 账户响应编号无效", status_code=503)
            message, exchange_code = _websocket_error_details(candidate)
            if exchange_code == -1021 and attempt == 0:
                _best_effort_sync_account_clock(safe_network, force=True)
                continue
            response = candidate
            break

        if not isinstance(response, dict):
            raise BinanceApiError("WebSocket 账户查询未返回结果", status_code=503)
        try:
            response_status = int(response.get("status"))
        except (TypeError, ValueError):
            response_status = None
        if response_status != 200:
            message, exchange_code = _websocket_error_details(response)
            raise BinanceApiError(message, status_code=503, exchange_code=exchange_code)
        result = response.get("result")
        if not isinstance(result, dict):
            raise BinanceApiError("WebSocket 账户数据格式无效", status_code=503)
        account, assets, positions = _normalize_account_status(result)
        return {
            "ok": True,
            "network": safe_network,
            "market": "FUTURES",
            "accountQuery": "account.status",
            "connectedMs": connected_ms,
            "responseMs": max(connected_ms, round((time.monotonic() - started_at) * 1000)),
            "responseStatus": response_status,
            "account": account,
            "assets": assets,
            "assetCount": len(assets),
            "positions": positions,
            "positionCount": len(positions),
            "rawPositionCount": len([item for item in result.get("positions") or [] if isinstance(item, dict)]),
            "dataFields": {key: list(value) for key, value in ACCOUNT_STATUS_DATA_FIELDS.items()},
            "proxyUsed": websocket_transport.websocket_proxy_enabled(),
            "testOnly": True,
        }
    except BinanceApiError:
        raise
    except Exception as exc:
        raise BinanceApiError(_failure_message(exc), status_code=503) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
