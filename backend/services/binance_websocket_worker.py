from __future__ import annotations

"""Long-lived Binance WebSocket transport for the process-local snapshots.

The page still polls ``/api/binance/snapshot`` once per second.  This module
owns the exchange connections behind that cache, so a browser refresh never
creates another Binance handshake.  There are deliberately two independent
transport families:

* one public market line per network, containing only the symbols currently
  monitored by WebSocket-enabled users;
* one authenticated account line per user, used for periodic ``account.status``
  snapshots.

REST remains the bootstrap/fallback transport.  A failed socket therefore
marks the cache stale without replacing the last successful payload.
"""

import logging
import time
from copy import deepcopy
from threading import Event, RLock, Thread

try:
    from . import database as db
    from .binance_websocket_transport import (
        WebSocketReceiveTimeout as _WebSocketReceiveTimeout,
        close_socket as _close_socket,
        open_websocket as _open_websocket,
        recv_json_with_timeout as _recv_json_with_timeout,
        socket_is_closed as _socket_is_closed,
        websocket_error_message as _websocket_error_message,
    )
    from .binance_websocket_test import (
        _account_status_params,
        _best_effort_sync_account_clock,
        _normalize_account_status,
        _websocket_error_details,
    )
    from .binance_account_worker import (
        cache_binance_websocket_account_snapshot,
        mark_binance_account_websocket_error,
    )
    from .binance_futures_client import _realized_pnl_by_position
except ImportError:  # pragma: no cover - direct module execution compatibility
    import database as db
    from binance_websocket_transport import (
        WebSocketReceiveTimeout as _WebSocketReceiveTimeout,
        close_socket as _close_socket,
        open_websocket as _open_websocket,
        recv_json_with_timeout as _recv_json_with_timeout,
        socket_is_closed as _socket_is_closed,
        websocket_error_message as _websocket_error_message,
    )
    from binance_websocket_test import (
        _account_status_params,
        _best_effort_sync_account_clock,
        _normalize_account_status,
        _websocket_error_details,
    )
    from binance_account_worker import (
        cache_binance_websocket_account_snapshot,
        mark_binance_account_websocket_error,
    )
    from binance_futures_client import _realized_pnl_by_position


LOGGER = logging.getLogger(__name__)

WEBSOCKET_MODE = "WEBSOCKET"
REST_MODE = "REST"
MARKET_RECONCILE_INTERVAL_SECONDS = 2.0
ACCOUNT_RECONCILE_INTERVAL_SECONDS = 2.0
ACCOUNT_REQUEST_INTERVAL_SECONDS = 15.0
WEBSOCKET_CONNECT_TIMEOUT_SECONDS = 10.0
WEBSOCKET_MAX_BACKOFF_SECONDS = 30.0
WEBSOCKET_RECV_POLL_SECONDS = 1.0
WEBSOCKET_IDLE_RECONNECT_SECONDS = 30.0
ACCOUNT_RESPONSE_TIMEOUT_SECONDS = 20.0
MARKET_HEALTH_MAX_AGE_SECONDS = 15.0
ACCOUNT_HEALTH_MAX_AGE_SECONDS = 45.0

PUBLIC_WEBSOCKET_URLS = {
    "mainnet": {
        "SPOT": "wss://stream.binance.com:9443/ws",
        "FUTURES": "wss://fstream.binance.com:443/ws",
    },
    "testnet": {
        "SPOT": "wss://testnet.binance.vision/ws",
        "FUTURES": "wss://stream.binancefuture.com:443/ws",
    },
}
ACCOUNT_WEBSOCKET_URLS = {
    "mainnet": "wss://ws-fapi.binance.com/ws-fapi/v1",
    "testnet": "wss://testnet.binancefuture.com/ws-fapi/v1",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_network(value: object) -> str:
    network = str(value or "mainnet").strip().lower()
    return network if network in PUBLIC_WEBSOCKET_URLS else "mainnet"


def _safe_market_type(value: object) -> str:
    market_type = str(value or "FUTURES").strip().upper()
    return market_type if market_type in {"SPOT", "FUTURES"} else "FUTURES"


def _safe_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _stream_symbol(symbol: object) -> str:
    return _safe_symbol(symbol).lower()


def _stream_name(symbol: object, suffix: str) -> str:
    return f"{_stream_symbol(symbol)}@{suffix}"


def _unwrap_event(payload: object) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    stream = payload.get("stream")
    data = payload.get("data")
    if isinstance(data, dict):
        return data, str(stream or "").strip().lower() or None
    return payload, str(stream or "").strip().lower() or None


def _ticker_payload(data: dict) -> dict | None:
    event = str(data.get("e") or "").strip()
    symbol = _safe_symbol(data.get("s"))
    if not symbol:
        return None
    if event == "markPriceUpdate":
        return {
            "symbol": symbol,
            "markPrice": data.get("p"),
            "eventTime": data.get("E") or _now_ms(),
            "closeTime": data.get("E") or _now_ms(),
        }
    if event not in {"24hrTicker", "ticker"} and data.get("c") is None:
        return None
    return {
        "symbol": symbol,
        "lastPrice": data.get("c"),
        "priceChange": data.get("p"),
        "priceChangePercent": data.get("P"),
        "highPrice": data.get("h"),
        "lowPrice": data.get("l"),
        "volume": data.get("v"),
        "quoteVolume": data.get("q"),
        "closeTime": data.get("C") or data.get("E") or _now_ms(),
        "eventTime": data.get("E") or _now_ms(),
    }


def _kline_payload(data: dict) -> tuple[str, str, dict] | None:
    raw = data.get("k")
    if not isinstance(raw, dict):
        return None
    symbol = _safe_symbol(raw.get("s") or data.get("s"))
    interval = str(raw.get("i") or "").strip()
    if not symbol or not interval:
        return None
    return symbol, interval, {
        "openTime": raw.get("t"),
        "open": raw.get("o"),
        "high": raw.get("h"),
        "low": raw.get("l"),
        "close": raw.get("c"),
        "volume": raw.get("v", 0),
        "closeTime": raw.get("T"),
        "eventTime": data.get("E") or _now_ms(),
        "closed": bool(raw.get("x")),
    }


def _cache_market_event(market_type: str, data: dict, stream: str | None = None) -> None:
    """Forward a normalized event to the snapshot worker without a cycle import."""

    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover
        import binance_snapshot_worker as snapshot_worker
    event = str(data.get("e") or "").strip()
    if event == "kline" or isinstance(data.get("k"), dict) or "@kline_" in str(stream or ""):
        parsed = _kline_payload(data)
        if not parsed:
            return
        symbol, interval, item = parsed
        snapshot_worker.cache_live_kline("mainnet", market_type, symbol, interval, item)
        return
    ticker = _ticker_payload(data)
    if ticker:
        snapshot_worker.cache_live_ticker("mainnet", market_type, ticker["symbol"], ticker)


def _cache_market_event_for_network(network: str, market_type: str, data: dict, stream: str | None = None) -> None:
    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover
        import binance_snapshot_worker as snapshot_worker
    event = str(data.get("e") or "").strip()
    if event == "kline" or isinstance(data.get("k"), dict) or "@kline_" in str(stream or ""):
        parsed = _kline_payload(data)
        if not parsed:
            return
        symbol, interval, item = parsed
        snapshot_worker.cache_live_kline(network, market_type, symbol, interval, item)
        return
    ticker = _ticker_payload(data)
    if ticker:
        snapshot_worker.cache_live_ticker(network, market_type, ticker["symbol"], ticker)


class _MarketLine:
    def __init__(self, network: str, market_type: str = "FUTURES"):
        self.network = _safe_network(network)
        self.market_type = _safe_market_type(market_type)
        self.lock = RLock()
        self.stop_event = Event()
        self.wake_event = Event()
        self.thread: Thread | None = None
        self.socket = None
        self.desired: dict[str, tuple[str, str, str, str]] = {}
        self.active_streams: set[str] = set()
        self.connected = False
        self.status = "IDLE"
        self.error = ""
        self.handshake_count = 0
        self.last_handshake_at = None
        self.last_connected_at = None
        self.last_message_at = None
        # ACK frames prove that the subscription request was accepted, but
        # they do not prove that a particular stream is delivering data.
        # Keep liveness per stream so REST fallback can be decided per target.
        self.last_data_at: dict[str, int] = {}
        self.reconnect_count = 0
        self._request_id = 100

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = Thread(
                target=self._run,
                name=f"binance-market-websocket-{self.network}-{self.market_type.lower()}",
                daemon=True,
            )
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            self.wake_event.set()
            socket = self.socket
            self.socket = None
            self.active_streams.clear()
            self.last_data_at.clear()
            self.connected = False
            self.status = "STOPPED"
        _close_socket(socket)

    def set_desired(self, desired: dict[str, tuple[str, str, str, str]]) -> None:
        normalized = dict(desired or {})
        with self.lock:
            if normalized == self.desired:
                return
            self.desired = normalized
            # The reader thread applies SUBSCRIBE/UNSUBSCRIBE on the existing
            # socket.  Changing the monitored set is not a transport failure,
            # so it must not cause another TLS/WebSocket handshake.
        self.wake_event.set()

    def _snapshot(self) -> tuple[dict[str, tuple[str, str, str, str]], object]:
        with self.lock:
            return dict(self.desired), self.socket

    def _set_error(self, error: object) -> None:
        with self.lock:
            socket = self.socket
            self.socket = None
            self.active_streams.clear()
            self.reconnect_count += 1
            self.error = _websocket_error_message(error, "行情 WebSocket 暂时不可用")
            self.status = "ERROR"
            self.connected = False
            self.last_data_at.clear()
        _close_socket(socket)

    def _connect(self):
        endpoint = PUBLIC_WEBSOCKET_URLS[self.network][self.market_type]
        connection = _open_websocket(
            endpoint,
            timeout=WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
        )
        with self.lock:
            self.handshake_count += 1
            self.last_handshake_at = _now_ms()
            self.last_connected_at = self.last_handshake_at
            self.last_message_at = self.last_handshake_at
            self.error = ""
            self.status = "CONNECTED"
            self.connected = True
            self.socket = connection
            self.active_streams.clear()
            self.last_data_at.clear()
        return connection

    def _sync_subscriptions(
        self,
        socket: object,
        desired: dict[str, tuple[str, str, str, str]],
    ) -> None:
        """Apply a target-set change on the current public socket."""

        desired_streams = set(desired)
        with self.lock:
            if self.socket is not socket:
                return
            active_streams = set(self.active_streams)
        removed = sorted(active_streams - desired_streams)
        added = sorted(desired_streams - active_streams)
        if removed:
            with self.lock:
                request_id = self._request_id
                self._request_id += 1
            socket.send_json({"method": "UNSUBSCRIBE", "params": removed, "id": request_id})
        if added:
            with self.lock:
                request_id = self._request_id
                self._request_id += 1
            socket.send_json({"method": "SUBSCRIBE", "params": added, "id": request_id})
        with self.lock:
            if self.socket is socket:
                self.active_streams = desired_streams
                self.last_data_at = {
                    stream: timestamp
                    for stream, timestamp in self.last_data_at.items()
                    if stream in desired_streams
                }

    def _socket_is_idle(self) -> bool:
        with self.lock:
            last_message = self.last_message_at
            connected = self.connected and self.socket is not None
        if not connected or last_message is None:
            return True
        try:
            return _now_ms() - int(last_message) > WEBSOCKET_IDLE_RECONNECT_SECONDS * 1000
        except (TypeError, ValueError):
            return True

    def _handle(self, payload: object, stream_map: dict[str, tuple[str, str, str, str]]) -> None:
        data, envelope_stream = _unwrap_event(payload)
        if not isinstance(data, dict):
            return
        with self.lock:
            self.last_message_at = _now_ms()
        # Subscription acknowledgements have no market event and are simply
        # consumed.  Error ACKs are recorded but do not poison old snapshots.
        if data.get("result") is not None or (data.get("id") is not None and data.get("e") is None and data.get("s") is None):
            if data.get("code") is not None or isinstance(data.get("error"), dict):
                message, _ = _websocket_error_details(data)
                self._set_error(message)
            return
        event = str(data.get("e") or "").strip()
        symbol = _safe_symbol(data.get("s") or (data.get("k") or {}).get("s"))
        if event == "kline" or isinstance(data.get("k"), dict):
            interval = str((data.get("k") or {}).get("i") or "").strip()
            target_stream = _stream_name(symbol, f"kline_{interval}") if symbol and interval else envelope_stream
        elif event == "markPriceUpdate":
            target_stream = _stream_name(symbol, "markPrice@1s") if symbol else envelope_stream
        else:
            target_stream = _stream_name(symbol, "ticker") if symbol else envelope_stream
        if target_stream and target_stream not in stream_map:
            # A direct endpoint may omit the envelope stream; use the only
            # matching target when the symbol is unambiguous.
            matches = [name for name in stream_map if name.startswith(f"{_stream_symbol(symbol)}@")]
            if len(matches) == 1:
                target_stream = matches[0]
        target = stream_map.get(target_stream) if target_stream else None
        if not target:
            return
        with self.lock:
            if self.socket is not None and target_stream:
                self.last_data_at[target_stream] = _now_ms()
        _cache_market_event_for_network(self.network, target[1], data, target_stream)

    def _run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            desired, socket = self._snapshot()
            if not desired:
                with self.lock:
                    self.status = "IDLE"
                    self.connected = False
                    socket = self.socket
                    self.socket = None
                _close_socket(socket)
                self.wake_event.wait(1.0)
                self.wake_event.clear()
                continue
            with self.lock:
                socket_connected = self.connected
            if socket is None or _socket_is_closed(socket) or not socket_connected:
                with self.lock:
                    self.status = "CONNECTING"
                self.connected = False
                try:
                    socket = self._connect()
                    backoff = 1.0
                except Exception as exc:
                    self._set_error(str(exc) or "行情 WebSocket 握手失败")
                    self.wake_event.wait(backoff)
                    self.wake_event.clear()
                    backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)
                    continue
            try:
                desired_now, current_socket = self._snapshot()
                if current_socket is not socket:
                    continue
                self._sync_subscriptions(socket, desired_now)
                self.wake_event.clear()
                payload = _recv_json_with_timeout(socket, WEBSOCKET_RECV_POLL_SECONDS)
                desired_now, current_socket = self._snapshot()
                if current_socket is not socket or desired_now != desired:
                    continue
                self._handle(payload, desired_now)
                # An error acknowledgement is handled by _handle and closes
                # the socket.  Apply the same backoff as a read/connect error
                # before the next loop can create another handshake.
                with self.lock:
                    transport_failed = self.socket is not socket or not self.connected
                if transport_failed:
                    if not self.stop_event.is_set():
                        self.wake_event.wait(backoff)
                        self.wake_event.clear()
                        backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)
                    continue
                backoff = 1.0
            except _WebSocketReceiveTimeout:
                # A normal public stream is chatty.  A prolonged silent line
                # is treated as a disconnect so the next loop performs one
                # fresh handshake; short polling timeouts also let queued
                # subscription changes apply without reconnecting.
                if not self._socket_is_idle():
                    continue
                with self.lock:
                    if self.socket is socket:
                        self.socket = None
                    self.active_streams.clear()
                    self.last_data_at.clear()
                    self.connected = False
                    self.status = "ERROR"
                    self.error = "行情 WebSocket 长时间无数据"
                    self.reconnect_count += 1
                _close_socket(socket)
                if not self.stop_event.is_set():
                    self.wake_event.wait(backoff)
                    self.wake_event.clear()
                    backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)
            except Exception as exc:
                with self.lock:
                    if self.socket is socket:
                        self.socket = None
                    self.active_streams.clear()
                    self.last_data_at.clear()
                    self.connected = False
                    self.status = "ERROR"
                    self.error = _websocket_error_message(exc, "行情 WebSocket 已断开")
                    self.reconnect_count += 1
                _close_socket(socket)
                if not self.stop_event.is_set():
                    self.wake_event.wait(backoff)
                    self.wake_event.clear()
                    backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)

    def status_payload(self, subscriptions: int | None = None) -> dict:
        with self.lock:
            return {
                "mode": WEBSOCKET_MODE,
                "status": self.status,
                "connected": bool(self.connected and self.socket is not None),
                "subscriptions": len(self.desired) if subscriptions is None else int(subscriptions),
                "handshakeCount": self.handshake_count,
                "reconnectCount": self.reconnect_count,
                "lastHandshakeAt": self.last_handshake_at,
                "lastConnectedAt": self.last_connected_at,
                "lastMessageAt": self.last_message_at,
                "lastDataAt": max(self.last_data_at.values(), default=None),
                "error": self.error or None,
            }


class _AccountLine:
    def __init__(self, credentials: dict):
        self.user_id = int(credentials["userId"])
        self.network = _safe_network(credentials.get("network"))
        self.api_key = str(credentials.get("apiKey") or "")
        self.api_secret = str(credentials.get("apiSecret") or "")
        self.profile = deepcopy(credentials.get("profile") or {})
        self.lock = RLock()
        self.stop_event = Event()
        self.wake_event = Event()
        self.thread: Thread | None = None
        self.socket = None
        self.connected = False
        self.status = "IDLE"
        self.error = ""
        self.handshake_count = 0
        self.reconnect_count = 0
        self.last_handshake_at = None
        self.last_connected_at = None
        self.last_message_at = None
        self.last_data_at = None
        self._request_id = 1

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = Thread(
                target=self._run,
                name=f"binance-account-websocket-{self.user_id}",
                daemon=True,
            )
            self.thread.start()

    def update_credentials(self, credentials: dict) -> None:
        network = _safe_network(credentials.get("network"))
        api_key = str(credentials.get("apiKey") or "")
        api_secret = str(credentials.get("apiSecret") or "")
        profile = deepcopy(credentials.get("profile") or self.profile)
        changed = (network, api_key, api_secret) != (self.network, self.api_key, self.api_secret)
        with self.lock:
            self.profile = profile
            if not changed:
                return
            self.network = network
            self.api_key = api_key
            self.api_secret = api_secret
            socket = self.socket
            self.socket = None
            self.last_data_at = None
            self.connected = False
            self.status = "RECONNECTING"
            self.reconnect_count += 1
        _close_socket(socket)
        self.wake_event.set()

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            self.wake_event.set()
            socket = self.socket
            self.socket = None
            self.connected = False
            self.status = "STOPPED"
        _close_socket(socket)

    def request_refresh(self) -> None:
        self.wake_event.set()

    def _connection_metadata(self) -> dict:
        with self.lock:
            return {
                "mode": WEBSOCKET_MODE,
                "status": self.status,
                "connected": bool(self.connected and self.socket is not None),
                "handshakeCount": self.handshake_count,
                "reconnectCount": self.reconnect_count,
                "lastHandshakeAt": self.last_handshake_at,
                "lastConnectedAt": self.last_connected_at,
                "lastMessageAt": self.last_message_at,
                "lastDataAt": self.last_data_at,
                "error": self.error or None,
            }

    def _mark_error(self, error: object) -> None:
        message = _websocket_error_message(error, "账户 WebSocket 暂时不可用")
        with self.lock:
            self.error = message
            self.status = "ERROR"
            self.connected = False
        try:
            mark_binance_account_websocket_error(
                self.user_id,
                message,
                self.profile,
                connection=self._connection_metadata(),
            )
        except Exception:
            LOGGER.debug("Binance account WebSocket error cache update failed", exc_info=True)

    def _connect(self):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("账户 API 配置不完整")
        connection = _open_websocket(
            ACCOUNT_WEBSOCKET_URLS[self.network],
            timeout=WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
        )
        with self.lock:
            self.handshake_count += 1
            self.last_handshake_at = _now_ms()
            self.last_connected_at = self.last_handshake_at
            self.last_message_at = self.last_handshake_at
            self.error = ""
            self.status = "AUTHENTICATING"
            self.connected = True
            self.socket = connection
            self.last_data_at = None
        return connection

    def _request_account_status(self, socket) -> None:
        with self.lock:
            api_key = self.api_key
            api_secret = self.api_secret
            network = self.network
        _best_effort_sync_account_clock(network)
        for attempt in range(2):
            with self.lock:
                request_id = self._request_id
                self._request_id += 1
            socket.send_json(
                {
                    "id": request_id,
                    "method": "account.status",
                    "params": _account_status_params(api_key, api_secret),
                }
            )
            while True:
                candidate = _recv_json_with_timeout(socket, ACCOUNT_RESPONSE_TIMEOUT_SECONDS)
                with self.lock:
                    self.last_message_at = _now_ms()
                if not isinstance(candidate, dict):
                    raise RuntimeError("账户 WebSocket 响应格式无效")
                if candidate.get("id") != request_id:
                    # The account API currently has no unsolicited event in
                    # this line, but tolerate one without losing the request.
                    continue
                message, exchange_code = _websocket_error_details(candidate)
                try:
                    response_status = int(candidate.get("status"))
                except (TypeError, ValueError):
                    response_status = None
                if response_status != 200:
                    if exchange_code == -1021 and attempt == 0:
                        _best_effort_sync_account_clock(network, force=True)
                        break
                    raise RuntimeError(message or "账户 WebSocket 查询失败")
                result = candidate.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("账户 WebSocket 数据格式无效")
                account_fields, assets, positions = _normalize_account_status(result)
                try:
                    realized_by_position, failed_symbols = _realized_pnl_by_position(
                        self.network,
                        self.api_key,
                        self.api_secret,
                        positions,
                    )
                    for position in positions:
                        symbol = str(position.get("symbol") or "").strip().upper()
                        position_side = str(position.get("positionSide") or "BOTH").strip().upper()
                        position["realizedPnl"] = (
                            None
                            if symbol in failed_symbols
                            else round(realized_by_position.get((symbol, position_side), 0.0), 8)
                        )
                except Exception:
                    for position in positions:
                        position["realizedPnl"] = None
                futures_account = {
                    **account_fields,
                    "available": True,
                    "snapshotAvailable": True,
                    "assets": assets,
                    "positions": positions,
                    "accountType": result.get("accountType"),
                    "openOrderCount": 0,
                    "openAlgoOrderCount": 0,
                }
                with self.lock:
                    self.last_data_at = _now_ms()
                    self.status = "CONNECTED"
                    self.error = ""
                cache_binance_websocket_account_snapshot(
                    self.user_id,
                    futures_account,
                    self.profile,
                    connection=self._connection_metadata(),
                )
                return

    def _run(self) -> None:
        backoff = 1.0
        next_request = 0.0
        while not self.stop_event.is_set():
            with self.lock:
                socket = self.socket
            if socket is None or _socket_is_closed(socket):
                with self.lock:
                    self.status = "CONNECTING"
                    self.connected = False
                try:
                    socket = self._connect()
                    backoff = 1.0
                    next_request = 0.0
                except Exception as exc:
                    self._mark_error(str(exc) or "账户 WebSocket 握手失败")
                    self.wake_event.wait(backoff)
                    self.wake_event.clear()
                    backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)
                    continue
            now = time.monotonic()
            if now < next_request:
                self.wake_event.wait(min(next_request - now, 1.0))
                self.wake_event.clear()
                continue
            try:
                self._request_account_status(socket)
                next_request = time.monotonic() + ACCOUNT_REQUEST_INTERVAL_SECONDS
                backoff = 1.0
                woke = self.wake_event.wait(min(ACCOUNT_REQUEST_INTERVAL_SECONDS, 1.0))
                self.wake_event.clear()
                if woke:
                    next_request = 0.0
            except Exception as exc:
                with self.lock:
                    if self.socket is socket:
                        self.socket = None
                    self.connected = False
                    self.status = "ERROR"
                    self.error = _websocket_error_message(exc, "账户 WebSocket 已断开")
                    self.reconnect_count += 1
                _close_socket(socket)
                self._mark_error(str(exc) or "账户 WebSocket 已断开")
                if not self.stop_event.is_set():
                    self.wake_event.wait(backoff)
                    self.wake_event.clear()
                    backoff = min(WEBSOCKET_MAX_BACKOFF_SECONDS, backoff * 2)

    def status_payload(self) -> dict:
        with self.lock:
            return {
                "mode": WEBSOCKET_MODE,
                "status": self.status,
                "connected": bool(self.connected and self.socket is not None),
                "handshakeCount": self.handshake_count,
                "reconnectCount": self.reconnect_count,
                "lastHandshakeAt": self.last_handshake_at,
                "lastConnectedAt": self.last_connected_at,
                "lastMessageAt": self.last_message_at,
                "lastDataAt": self.last_data_at,
                "error": self.error or None,
            }


_manager_lock = RLock()
_manager_stop_event: Event | None = None
_manager_wake_event = Event()
_manager_thread: Thread | None = None
_market_lines: dict[tuple[str, str], _MarketLine] = {}
_account_lines: dict[int, _AccountLine] = {}


def _configured_websocket_credentials() -> dict[int, dict]:
    try:
        raw_items = list(db.list_user_binance_credentials_for_refresh())
    except Exception:
        LOGGER.debug("Binance WebSocket credential discovery failed", exc_info=True)
        return {}
    try:
        modes = db.list_user_binance_connection_modes()
    except Exception:
        modes = {}
    result = {}
    for item in raw_items:
        if not isinstance(item, dict) or item.get("userId") is None:
            continue
        try:
            user_id = int(item["userId"])
        except (TypeError, ValueError):
            continue
        mode = str(item.get("connectionMode") or modes.get(user_id, REST_MODE)).strip().upper()
        if mode != WEBSOCKET_MODE:
            continue
        profile = {
            "configured": True,
            "network": _safe_network(item.get("network")),
            "apiKeyMasked": "",
            "updatedAt": None,
            "requiresReconfiguration": False,
            "connectionMode": WEBSOCKET_MODE,
        }
        try:
            stored_profile = db.get_user_binance_credentials(user_id, include_secrets=False)
            if isinstance(stored_profile, dict):
                profile.update(
                    {
                        "configured": bool(stored_profile.get("configured", True)),
                        "apiKeyMasked": str(stored_profile.get("apiKeyMasked") or ""),
                        "updatedAt": stored_profile.get("updatedAt"),
                        "requiresReconfiguration": bool(stored_profile.get("requiresReconfiguration")),
                        "connectionMode": WEBSOCKET_MODE,
                    }
                )
        except Exception:
            pass
        if not profile["configured"]:
            continue
        result[user_id] = {
            **item,
            "userId": user_id,
            "network": _safe_network(item.get("network")),
            "profile": profile,
        }
    return result


def _websocket_user_ids(configured_profiles: dict[int, dict]) -> set[int]:
    """Return users that opted into WebSocket market transport.

    Public streams do not need API credentials.  Keep their subscription
    lifecycle independent from the authenticated account line so a user can
    inspect a selected chart while their account keys are being configured.
    """

    user_ids = set(configured_profiles)
    try:
        modes = db.list_user_binance_connection_modes()
    except Exception:
        modes = {}
    for raw_user_id, mode in modes.items():
        if str(mode or REST_MODE).strip().upper() == WEBSOCKET_MODE:
            try:
                user_ids.add(int(raw_user_id))
            except (TypeError, ValueError):
                continue
    return user_ids


def _desired_market_streams(user_ids: set[int]) -> dict[tuple[str, str], dict[str, tuple[str, str, str, str]]]:
    if not user_ids:
        return {}
    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover
        import binance_snapshot_worker as snapshot_worker
    try:
        chart_targets = snapshot_worker.get_market_stream_targets(user_ids)
    except Exception:
        LOGGER.debug("Binance WebSocket target discovery failed", exc_info=True)
        chart_targets = []
    try:
        execution_price_targets = snapshot_worker.get_execution_market_price_targets(user_ids)
    except Exception:
        LOGGER.debug("Binance WebSocket execution price target discovery failed", exc_info=True)
        execution_price_targets = []
    try:
        account_position_price_targets = snapshot_worker.get_account_position_market_price_targets(user_ids)
    except Exception:
        LOGGER.debug("Binance WebSocket account position price target discovery failed", exc_info=True)
        account_position_price_targets = []
    try:
        execution_kline_targets = snapshot_worker.get_execution_monitor_targets(user_ids)
    except Exception:
        LOGGER.debug("Binance WebSocket execution K-line target discovery failed", exc_info=True)
        execution_kline_targets = []
    # Binance uses different public hosts for spot and futures.  A line is
    # therefore keyed by both network and market type; account traffic never
    # shares either of these sockets.
    result: dict[tuple[str, str], dict[str, tuple[str, str, str, str]]] = {}
    for target in chart_targets:
        if not isinstance(target, (tuple, list)) or len(target) != 4:
            continue
        network, market_type, symbol, interval = target
        network = _safe_network(network)
        market_type = _safe_market_type(market_type)
        symbol = _safe_symbol(symbol)
        interval = str(interval or "").strip()
        if not symbol or not interval:
            continue
        line_key = (network, market_type)
        ticker_stream = _stream_name(symbol, "ticker")
        kline_stream = _stream_name(symbol, f"kline_{interval}")
        result.setdefault(line_key, {})[ticker_stream] = (network, market_type, symbol, interval)
        result[line_key][kline_stream] = (network, market_type, symbol, interval)
    for target in execution_price_targets:
        if not isinstance(target, (tuple, list)) or len(target) != 3:
            continue
        network, market_type, symbol = target
        network = _safe_network(network)
        market_type = _safe_market_type(market_type)
        symbol = _safe_symbol(symbol)
        if not symbol:
            continue
        line_key = (network, market_type)
        stream_suffix = "markPrice@1s" if market_type == "FUTURES" else "ticker"
        result.setdefault(line_key, {})[_stream_name(symbol, stream_suffix)] = (
            network,
            market_type,
            symbol,
            "",
        )
    for target in account_position_price_targets:
        if not isinstance(target, (tuple, list)) or len(target) != 3:
            continue
        network, market_type, symbol = target
        network = _safe_network(network)
        market_type = _safe_market_type(market_type)
        symbol = _safe_symbol(symbol)
        if not symbol:
            continue
        line_key = (network, market_type)
        result.setdefault(line_key, {})[_stream_name(symbol, "ticker")] = (network, market_type, symbol, "")
        if market_type == "FUTURES":
            result[line_key][_stream_name(symbol, "markPrice@1s")] = (network, market_type, symbol, "")
    # Keep the in-progress candles for active plans on the same public line as
    # account monitoring. REST still refreshes these snapshots periodically
    # to backfill history and drive trailing-stop recalculation.
    for target in execution_kline_targets:
        if not isinstance(target, (tuple, list)) or len(target) != 4:
            continue
        network, market_type, symbol, interval = target
        network = _safe_network(network)
        market_type = _safe_market_type(market_type)
        symbol = _safe_symbol(symbol)
        interval = str(interval or "").strip()
        if not symbol or not interval:
            continue
        line_key = (network, market_type)
        result.setdefault(line_key, {})[_stream_name(symbol, f"kline_{interval}")] = (
            network,
            market_type,
            symbol,
            interval,
        )
    return result


def _reconcile_once() -> None:
    profiles = _configured_websocket_credentials()
    websocket_users = _websocket_user_ids(profiles)
    with _manager_lock:
        existing_account_ids = set(_account_lines)
    for user_id, credentials in profiles.items():
        with _manager_lock:
            line = _account_lines.get(user_id)
            if line is None:
                line = _AccountLine(credentials)
                _account_lines[user_id] = line
                should_start = True
            else:
                should_start = False
        line.update_credentials(credentials)
        if should_start:
            line.start()
    # Account lines require credentials, while public market lines can serve
    # a user who selected WebSocket before configuring an account.  Do not
    # keep a stale authenticated line merely because the transport preference
    # is still set to WebSocket.
    for user_id in existing_account_ids - set(profiles):
        with _manager_lock:
            line = _account_lines.pop(user_id, None)
        if line:
            line.stop()

    desired_by_line = _desired_market_streams(websocket_users)
    with _manager_lock:
        known_lines = set(_market_lines) | set(desired_by_line)
    for line_key in known_lines:
        network, market_type = line_key
        desired = desired_by_line.get(line_key, {})
        with _manager_lock:
            line = _market_lines.get(line_key)
            if not desired:
                if line:
                    _market_lines.pop(line_key, None)
                else:
                    line = None
            elif line is None:
                line = _MarketLine(network, market_type)
                _market_lines[line_key] = line
                should_start = True
            else:
                should_start = False
        if not desired:
            if line:
                line.stop()
            continue
        line.set_desired(desired)
        if should_start:
            line.start()


def _run_manager(stop_event: Event) -> None:
    while not stop_event.is_set():
        try:
            _reconcile_once()
        except Exception:
            LOGGER.warning("Binance WebSocket connection reconciliation failed", exc_info=True)
        _manager_wake_event.wait(MARKET_RECONCILE_INTERVAL_SECONDS)
        _manager_wake_event.clear()


def start_binance_websocket_worker() -> None:
    global _manager_stop_event, _manager_thread
    with _manager_lock:
        if _manager_thread and _manager_thread.is_alive():
            _manager_wake_event.set()
            return
        _manager_stop_event = Event()
        _manager_thread = Thread(
            target=_run_manager,
            args=(_manager_stop_event,),
            name="binance-websocket-manager",
            daemon=True,
        )
        _manager_thread.start()


def start_configured_binance_websocket_worker() -> None:
    """Start the manager only when an account explicitly selected WebSocket."""

    try:
        modes = db.list_user_binance_connection_modes()
    except Exception:
        LOGGER.warning("Unable to read Binance connection modes at startup", exc_info=True)
        return
    if any(str(mode or REST_MODE).strip().upper() == WEBSOCKET_MODE for mode in modes.values()):
        start_binance_websocket_worker()


def stop_binance_websocket_worker() -> None:
    global _manager_stop_event, _manager_thread
    with _manager_lock:
        if _manager_stop_event:
            _manager_stop_event.set()
        _manager_wake_event.set()
        account_lines = list(_account_lines.values())
        market_lines = list(_market_lines.values())
        _account_lines.clear()
        _market_lines.clear()
        _manager_thread = None
        _manager_stop_event = None
    for line in account_lines:
        line.stop()
    for line in market_lines:
        line.stop()


def set_user_connection_mode(user_id: int, mode: object) -> None:
    normalized = str(mode or REST_MODE).strip().upper()
    _manager_wake_event.set()
    if normalized == WEBSOCKET_MODE:
        # Settings can be changed while the app is served by a WSGI runner
        # that did not execute the production launcher block.  Start the
        # manager lazily so the saved preference takes effect immediately.
        start_binance_websocket_worker()
        return
    with _manager_lock:
        line = _account_lines.pop(int(user_id), None)
    if line:
        line.stop()


def request_binance_websocket_account_refresh(user_id: int) -> bool:
    with _manager_lock:
        line = _account_lines.get(int(user_id))
    if not line:
        _manager_wake_event.set()
        return False
    line.request_refresh()
    return True


def is_account_line_healthy(user_id: int) -> bool:
    """Return whether an authenticated line has a recent account response."""

    with _manager_lock:
        line = _account_lines.get(int(user_id))
    if not line:
        return False
    with line.lock:
        if not line.connected or line.socket is None:
            return False
        last_data = line.last_data_at
    if last_data is None:
        return False
    try:
        return _now_ms() - int(last_data) <= ACCOUNT_HEALTH_MAX_AGE_SECONDS * 1000
    except (TypeError, ValueError):
        return False


def is_market_target_healthy(key: tuple[str, str, str, str]) -> bool:
    """Return true only after a subscribed target has a fresh live candle."""

    if not isinstance(key, (tuple, list)) or len(key) != 4:
        return False
    network, market_type, symbol, interval = key
    stream = _stream_name(symbol, f"kline_{interval}")
    with _manager_lock:
        line = _market_lines.get((_safe_network(network), _safe_market_type(market_type)))
    if not line:
        return False
    with line.lock:
        if not line.connected or stream not in line.desired:
            return False
        last_data = line.last_data_at.get(stream)
    if last_data is None or (_now_ms() - int(last_data)) > MARKET_HEALTH_MAX_AGE_SECONDS * 1000:
        return False
    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover
        import binance_snapshot_worker as snapshot_worker
    try:
        cached = snapshot_worker.get_cached_kline_snapshot(network, market_type, symbol, interval)
    except Exception:
        return False
    items = cached.get("items") if isinstance(cached, dict) else []
    if not isinstance(items, list) or len(items) < 80:
        return False
    updated_at = cached.get("updatedAt") if isinstance(cached, dict) else None
    try:
        return _now_ms() - int(updated_at or 0) <= MARKET_HEALTH_MAX_AGE_SECONDS * 1000
    except (TypeError, ValueError):
        return False


def _rest_connection_status() -> dict:
    return {"mode": REST_MODE, "status": REST_MODE, "connected": False, "subscriptions": 0, "error": None}


def get_binance_connection_status(user_id: int) -> dict:
    """Return safe transport status for settings and snapshot responses."""

    try:
        settings = db.get_user_binance_connection_settings(int(user_id))
        mode = str(settings.get("connectionMode") or REST_MODE).strip().upper()
    except Exception:
        mode = REST_MODE
    if mode != WEBSOCKET_MODE:
        return {
            "mode": REST_MODE,
            "connectionMode": REST_MODE,
            "account": _rest_connection_status(),
            "market": _rest_connection_status(),
        }
    with _manager_lock:
        account_line = _account_lines.get(int(user_id))
        market_lines = list(_market_lines.values())
    account_status = account_line.status_payload() if account_line else {
        "mode": WEBSOCKET_MODE,
        "status": "WAITING_CREDENTIALS",
        "connected": False,
        "subscriptions": 0,
        "error": None,
    }
    try:
        from . import binance_snapshot_worker as snapshot_worker
    except ImportError:  # pragma: no cover
        import binance_snapshot_worker as snapshot_worker
    try:
        targets = snapshot_worker.get_market_stream_targets({int(user_id)})
    except Exception:
        targets = []
    try:
        execution_price_targets = snapshot_worker.get_execution_market_price_targets({int(user_id)})
    except Exception:
        execution_price_targets = []
    target_lines = {
        (_safe_network(item[0]), _safe_market_type(item[1]))
        for item in targets
        if isinstance(item, (tuple, list)) and len(item) == 4
    }
    target_lines.update(
        {
            (_safe_network(item[0]), _safe_market_type(item[1]))
            for item in execution_price_targets
            if isinstance(item, (tuple, list)) and len(item) == 3
        }
    )
    relevant_lines = [
        line for line in market_lines
        if (line.network, line.market_type) in target_lines
    ]
    if not relevant_lines:
        market_status = {
            "mode": WEBSOCKET_MODE,
            "status": "IDLE",
            "connected": False,
            "subscriptions": 0,
            "error": None,
        }
    else:
        payloads = [line.status_payload() for line in relevant_lines]
        market_status = {
            "mode": WEBSOCKET_MODE,
            "status": "CONNECTED" if all(item.get("connected") for item in payloads) else next(
                (item.get("status") for item in payloads if item.get("status") == "ERROR"),
                "CONNECTING",
            ),
            "connected": all(bool(item.get("connected")) for item in payloads),
            "subscriptions": sum(int(item.get("subscriptions") or 0) for item in payloads),
            "handshakeCount": sum(int(item.get("handshakeCount") or 0) for item in payloads),
            "reconnectCount": sum(int(item.get("reconnectCount") or 0) for item in payloads),
            "lastHandshakeAt": max((item.get("lastHandshakeAt") or 0 for item in payloads), default=0) or None,
            "lastConnectedAt": max((item.get("lastConnectedAt") or 0 for item in payloads), default=0) or None,
            "lastMessageAt": max((item.get("lastMessageAt") or 0 for item in payloads), default=0) or None,
            "error": next((item.get("error") for item in payloads if item.get("error")), None),
        }
    return {
        "mode": WEBSOCKET_MODE,
        "connectionMode": WEBSOCKET_MODE,
        "account": account_status,
        "market": market_status,
    }


__all__ = [
    "get_binance_connection_status",
    "is_account_line_healthy",
    "is_market_target_healthy",
    "request_binance_websocket_account_refresh",
    "start_configured_binance_websocket_worker",
    "set_user_connection_mode",
    "start_binance_websocket_worker",
    "stop_binance_websocket_worker",
]
