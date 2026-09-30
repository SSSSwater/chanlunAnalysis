from __future__ import annotations

"""Small transport adapter shared by Binance WebSocket workers and probes.

The long-lived and diagnostic WebSocket paths use ``websocket-client`` so
they use the system Python TLS stack.  Compatibility hooks for the old curl
fallback remain patchable in tests, but the production process does not load
curl_cffi's native Windows implementation.
"""

import json
import os
from select import select
import time
from urllib.parse import urlparse

try:
    import websocket as websocket_client
except ImportError:  # pragma: no cover - deployment dependency guard
    websocket_client = None

# Do not import curl_cffi in the long-lived backend process. These names are
# retained as compatibility hooks for the existing transport tests.
curl_requests = None
CurlECode = CurlInfo = CurlWsFlag = None

# The current host cannot reach Binance directly.  Keep WebSocket on the same
# local proxy as REST, while allowing deployments to override the endpoint.
WEBSOCKET_PROXY_URL = os.environ.get("BINANCE_WS_PROXY", "http://127.0.0.1:10808").strip()

class WebSocketReceiveTimeout(TimeoutError):
    """Raised when a synchronous WebSocket has no frame before a deadline."""


def websocket_error_message(exc: object, fallback: str = "WebSocket 连接失败") -> str:
    """Hide noisy TLS/proxy implementation details from the API payload."""

    message = str(exc or "").strip()
    text = message.lower()
    if "timeout" in text or "timed out" in text:
        return "WebSocket 连接超时"
    if "proxy" in text or "tunnel" in text:
        return "WebSocket 代理连接失败"
    if any(
        marker in text
        for marker in (
            "ssl",
            "tls",
            "handshake",
            "certificate",
            "invalid library",
            "unexpected_eof",
        )
    ):
        return "WebSocket TLS 或握手失败"
    return message or fallback


class _WebSocketClientAdapter:
    """Expose the JSON-oriented interface used by the existing workers."""

    def __init__(self, socket):
        self._socket = socket

    @property
    def closed(self) -> bool:
        return not bool(
            getattr(self._socket, "connected", False)
            and getattr(self._socket, "sock", None) is not None
        )

    def connect(self, endpoint: str, **options):
        self._socket.connect(endpoint, **options)

    def send_json(self, payload: object) -> None:
        self._socket.send(json.dumps(payload, separators=(",", ":")))

    def recv_json(self) -> object:
        while True:
            payload = self._socket.recv()
            # websocket-client returns an empty string for control frames that
            # it handled internally.  Keep waiting for the next data frame.
            if payload in ("", b""):
                continue
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            try:
                return json.loads(payload)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("WebSocket 返回的 JSON 格式无效") from exc

    def settimeout(self, timeout: float) -> None:
        self._socket.settimeout(timeout)

    def close(self) -> None:
        self._socket.close()

    def terminate(self) -> None:
        self.close()


def _proxy_options() -> dict[str, object]:
    parsed = urlparse(WEBSOCKET_PROXY_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("WebSocket 代理地址无效")
    try:
        port = int(parsed.port or 80)
    except ValueError as exc:
        raise RuntimeError("WebSocket 代理端口无效") from exc
    return {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": port,
        "proxy_type": "http",
        # Explicitly clear websocket-client's bypass list so Binance does not
        # accidentally go direct because of inherited NO_PROXY settings.
        "http_no_proxy": [],
    }


def websocket_proxy_enabled() -> bool:
    return bool(WEBSOCKET_PROXY_URL)


def open_websocket(endpoint: str, *, timeout: float):
    """Open one WebSocket through the configured local proxy."""

    options = {"timeout": timeout, **_proxy_options()}
    if websocket_client is not None and hasattr(websocket_client, "WebSocket"):
        connection = _WebSocketClientAdapter(websocket_client.WebSocket())
        try:
            connection.connect(endpoint, **options)
        except Exception:
            connection.close()
            raise
        return connection
    if curl_requests is not None and hasattr(curl_requests, "WebSocket"):
        connection = curl_requests.WebSocket()
        try:
            connection.connect(
                endpoint,
                timeout=timeout,
                impersonate="edge",
                proxies={"http": WEBSOCKET_PROXY_URL, "https": WEBSOCKET_PROXY_URL},
            )
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            raise
        return connection
    raise RuntimeError("WebSocket 组件不可用")


def socket_is_closed(socket: object) -> bool:
    if socket is None:
        return True
    try:
        closed = getattr(socket, "closed", None)
        if closed is not None:
            return bool(closed)
        connected = getattr(socket, "connected", None)
        if connected is not None:
            return not bool(connected)
    except Exception:
        return True
    return False


def close_socket(socket: object) -> None:
    if socket is None:
        return
    try:
        socket.close()
    except Exception:
        try:
            socket.terminate()
        except Exception:
            pass


def recv_json_with_timeout(socket: object, timeout: float) -> object:
    """Receive one JSON frame without allowing a dead socket to block forever."""

    if isinstance(socket, _WebSocketClientAdapter):
        raw_socket = socket._socket
        try:
            raw_socket.settimeout(max(0.05, float(timeout)))
            return socket.recv_json()
        except Exception as exc:
            if websocket_client is not None and isinstance(
                exc, websocket_client.WebSocketTimeoutException
            ):
                raise WebSocketReceiveTimeout("WebSocket 接收超时") from exc
            raise

    # The synchronous curl-cffi API does not expose a timeout argument for
    # recv_json.  Its low-level frame API does, so use select around it.
    if (
        CurlInfo is None
        or CurlWsFlag is None
        or not hasattr(socket, "curl")
        or not hasattr(socket, "recv_fragment")
    ):
        return socket.recv_json()

    sock_fd = socket.curl.getinfo(CurlInfo.ACTIVESOCKET)
    if not isinstance(sock_fd, int) or sock_fd < 0:
        raise RuntimeError("WebSocket 没有有效的活动连接")
    deadline = time.monotonic() + max(0.05, float(timeout))
    chunks: list[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebSocketReceiveTimeout("WebSocket 接收超时")
        readable, _, _ = select([sock_fd], [], [], min(remaining, 0.5))
        if not readable:
            continue
        try:
            chunk, frame = socket.recv_fragment()
        except Exception as exc:
            if CurlECode is not None and getattr(exc, "code", None) == CurlECode.AGAIN:
                continue
            raise
        chunks.append(chunk)
        flags = getattr(frame, "flags", 0)
        if not getattr(frame, "bytesleft", 0) and not (flags & CurlWsFlag.CONT):
            break

    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("WebSocket 返回的 JSON 格式无效") from exc


__all__ = [
    "WebSocketReceiveTimeout",
    "close_socket",
    "curl_requests",
    "open_websocket",
    "recv_json_with_timeout",
    "socket_is_closed",
    "websocket_error_message",
    "websocket_proxy_enabled",
    "websocket_client",
]
