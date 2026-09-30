"""Capture Binance Smart Money request headers from a local Chrome tab.

The browser owns the authenticated session, so the backend must not try to
reconstruct it from ``document.cookie``.  Chrome DevTools Protocol exposes the
actual request headers, including HttpOnly cookies, when a local browser is
started with remote debugging enabled.
"""

from __future__ import annotations

import json
import os
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SMART_MONEY_URL = "https://www.binance.com/zh-CN/smart-money/my-subscriptions"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"


class BinanceBrowserAutomationError(RuntimeError):
    """Raised when the local browser cannot provide the requested headers."""


def _cdp_url() -> str:
    return str(os.environ.get("BINANCE_BROWSER_CDP_URL") or DEFAULT_CDP_URL).strip().rstrip("/")


def _request_json(path: str, *, method: str = "GET", timeout: float = 3) -> object:
    request = Request(f"{_cdp_url()}{path}", method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise BinanceBrowserAutomationError(
            "无法连接本机 Chrome 自动化接口，请确认 Chrome 已开启远程调试（CDP）"
        ) from exc


def _list_pages() -> list[dict]:
    payload = _request_json("/json/list")
    return [page for page in payload if isinstance(page, dict) and page.get("type") == "page"] if isinstance(payload, list) else []


def _find_page() -> dict | None:
    for page in _list_pages():
        url = str(page.get("url") or "")
        if url.startswith(SMART_MONEY_URL) and page.get("webSocketDebuggerUrl"):
            return page
    return None


def _open_page() -> dict:
    encoded_url = quote(SMART_MONEY_URL, safe=":/")
    try:
        payload = _request_json(f"/json/new?{encoded_url}", method="PUT", timeout=5)
    except BinanceBrowserAutomationError:
        payload = _request_json(f"/json/new?{encoded_url}", method="GET", timeout=5)
    if not isinstance(payload, dict) or not payload.get("webSocketDebuggerUrl"):
        raise BinanceBrowserAutomationError("Chrome 自动化接口未返回新标签页")
    return payload


def _find_or_open_page() -> dict:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        page = _find_page()
        if page:
            return page
        sleep(0.2)
    return _open_page()


def _normalized_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {
        str(name).lower(): str(value).strip()
        for name, value in headers.items()
        if str(name).strip() and value not in (None, "")
    }


def _header_capture(headers: dict[str, str]) -> dict[str, str] | None:
    cookie = headers.get("cookie", "")
    csrf_token = headers.get("csrftoken", "")
    if not cookie or not csrf_token:
        return None
    return {"cookie": cookie, "csrfToken": csrf_token}


def capture_smart_money_auth(*, timeout_seconds: float = 20) -> dict[str, str]:
    """Open/find the Smart Money tab and capture Cookie + csrftoken headers."""

    try:
        import websocket
    except ImportError as exc:  # pragma: no cover - requirements install guard
        raise BinanceBrowserAutomationError("后端缺少 websocket-client，无法连接 Chrome 自动化接口") from exc

    page = _find_or_open_page()
    try:
        socket = websocket.create_connection(str(page["webSocketDebuggerUrl"]), timeout=1)
    except Exception as exc:
        raise BinanceBrowserAutomationError("无法连接 Binance 标签页的自动化通道") from exc

    next_command_id = 0
    request_urls: dict[str, str] = {}
    request_headers: dict[str, dict[str, str]] = {}

    def send(method: str, params: dict | None = None) -> None:
        nonlocal next_command_id
        next_command_id += 1
        socket.send(json.dumps({"id": next_command_id, "method": method, "params": params or {}}))

    try:
        send("Network.enable", {"maxTotalBufferSize": 10 * 1024 * 1024, "maxResourceBufferSize": 1 * 1024 * 1024})
        send("Page.enable")
        send("Network.setCacheDisabled", {"cacheDisabled": True})
        send("Page.reload", {"ignoreCache": True})
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            try:
                message = json.loads(socket.recv())
            except Exception:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                request_id = str(params.get("requestId") or "")
                request_urls[request_id] = str(request.get("url") or "")
                request_headers.setdefault(request_id, {}).update(_normalized_headers(request.get("headers")))
            elif method == "Network.requestWillBeSentExtraInfo":
                request_id = str(params.get("requestId") or "")
                request_headers.setdefault(request_id, {}).update(_normalized_headers(params.get("headers")))
            else:
                continue

            request_id = str(params.get("requestId") or "")
            if not request_urls.get(request_id, "").startswith("https://www.binance.com/"):
                continue
            result = _header_capture(request_headers.get(request_id, {}))
            if result:
                return result
    finally:
        socket.close()

    raise BinanceBrowserAutomationError(
        "未捕获到同时包含 Cookie 和 csrftoken 的 Binance 请求，请先在该浏览器中完成登录后重试"
    )
