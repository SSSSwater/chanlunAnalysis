from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from threading import Condition, Event, RLock, Thread
from uuid import uuid4

try:
    from .binance_account_worker import (
        get_cached_binance_account_snapshot,
        refresh_binance_accounts_once,
        refresh_requested_binance_accounts_once,
    )
    from .binance_client import BinanceApiError, get_klines as get_spot_klines, get_spot_markets
    from .binance_copy_trading import (
        cleanup_binance_smart_money_follows_without_real_positions,
        get_cached_binance_copy_trading_history,
        project_binance_copy_trading_follows,
        refresh_binance_copy_trading_histories_once,
    )
    from .binance_futures_client import get_futures_klines, get_futures_markets
    from .binance_simulated_portfolio import (
        get_cached_binance_simulated_portfolio,
        refresh_binance_simulated_positions_once,
    )
    from . import database as db
except ImportError:
    from binance_account_worker import get_cached_binance_account_snapshot, refresh_binance_accounts_once, refresh_requested_binance_accounts_once
    from binance_client import BinanceApiError, get_klines as get_spot_klines, get_spot_markets
    from binance_copy_trading import cleanup_binance_smart_money_follows_without_real_positions, get_cached_binance_copy_trading_history, project_binance_copy_trading_follows, refresh_binance_copy_trading_histories_once
    from binance_futures_client import get_futures_klines, get_futures_markets
    from binance_simulated_portfolio import get_cached_binance_simulated_portfolio, refresh_binance_simulated_positions_once
    import database as db


SNAPSHOT_REFRESH_INTERVAL_SECONDS = 5
PUBLIC_RECONCILIATION_INTERVAL_SECONDS = 300
PUBLIC_RETRY_INTERVAL_SECONDS = 30
ACCOUNT_RECONCILIATION_INTERVAL_SECONDS = 15
ACCOUNT_REQUEST_REFRESH_INTERVAL_SECONDS = 1
MONITOR_BOOTSTRAP_INTERVAL_SECONDS = 1
MONITOR_FALLBACK_INTERVAL_SECONDS = 5
MONITOR_HISTORY_INTERVAL_SECONDS = 30
COPY_TRADING_REFRESH_INTERVAL_SECONDS = 15
LIVE_KLINE_FALLBACK_AFTER_SECONDS = 8
KLINE_BOOTSTRAP_RETRY_SECONDS = 30
MARKET_LIMIT = 100
KLINE_LIMIT = 180
SUPPORTED_NETWORKS = {"mainnet", "testnet"}
SUPPORTED_MARKET_TYPES = {"SPOT", "FUTURES"}
SUPPORTED_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}
# Keep target validation consistent with the futures client. Binance has
# listed Unicode-letter symbols, which must be allowed through to the chart
# cache and WebSocket subscription instead of failing after selection.
SYMBOL_PATTERN = re.compile(r"^[^\W_]{5,20}$", re.UNICODE)
POLL_CLIENT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
POLL_TARGET_TTL_SECONDS = 30

logger = logging.getLogger(__name__)
_snapshot_lock = RLock()
_snapshot_condition = Condition(_snapshot_lock)
_market_snapshots: dict[tuple[str, str], dict] = {}
_public_market_next_due: dict[str, float] = {}
_kline_snapshots: dict[tuple[str, str, str, str], dict] = {}
_live_ticker_snapshots: dict[tuple[str, str, str], dict] = {}
_kline_bootstrap_attempts: dict[tuple[str, str, str, str], float] = {}
_monitor_targets: dict[str, dict] = {}
_poll_monitor_targets: dict[str, str] = {}
_poll_monitor_last_seen: dict[str, float] = {}
_snapshot_version = 0
_worker_state_lock = RLock()
_worker_stop_event: Event | None = None
_worker_thread: Thread | None = None


def _normalized_target(network: object, market_type: object, symbol: object, interval: object) -> dict:
    safe_network = str(network or "mainnet").strip().lower()
    safe_market_type = str(market_type or "SPOT").strip().upper()
    safe_symbol = str(symbol or "").strip().upper()
    safe_interval = str(interval or "1h").strip()
    if safe_network not in SUPPORTED_NETWORKS:
        raise ValueError("网络必须选择主网或测试网")
    if safe_market_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError("行情类型必须选择现货或合约")
    if not SYMBOL_PATTERN.fullmatch(safe_symbol):
        raise ValueError("交易对格式无效")
    if safe_interval not in SUPPORTED_INTERVALS:
        raise ValueError("K 线周期无效")
    return {
        "network": safe_network,
        "marketType": safe_market_type,
        "symbol": safe_symbol,
        "interval": safe_interval,
    }


def _safe_poll_client_key(client_key: object) -> str:
    value = str(client_key or "").strip()
    return value if POLL_CLIENT_KEY_PATTERN.fullmatch(value) else "anonymous"


def _prune_poll_monitor_targets_locked(now: float | None = None) -> bool:
    current_time = time.monotonic() if now is None else now
    expired = [
        key
        for key, last_seen in _poll_monitor_last_seen.items()
        if current_time - last_seen > POLL_TARGET_TTL_SECONDS
    ]
    for key in expired:
        subscription_id = _poll_monitor_targets.pop(key, None)
        _poll_monitor_last_seen.pop(key, None)
        if subscription_id:
            _monitor_targets.pop(subscription_id, None)
    return bool(expired)


def upsert_poll_monitor_target(
    client_key: object,
    network: object,
    market_type: object,
    symbol: object,
    interval: object,
    owner_user_id: int | None = None,
) -> str:
    """Keep one short-lived monitor target for a frontend polling client."""

    safe_client_key = _safe_poll_client_key(client_key)
    if not str(symbol or "").strip():
        safe_network = str(network or "mainnet").strip().lower()
        safe_market_type = str(market_type or "SPOT").strip().upper()
        safe_interval = str(interval or "1h").strip()
        if safe_network not in SUPPORTED_NETWORKS:
            raise ValueError("网络必须选择主网或测试网")
        if safe_market_type not in SUPPORTED_MARKET_TYPES:
            raise ValueError("行情类型必须选择现货或合约")
        if safe_interval not in SUPPORTED_INTERVALS:
            raise ValueError("K 线周期无效")
        with _snapshot_condition:
            _prune_poll_monitor_targets_locked()
            subscription_id = _poll_monitor_targets.pop(safe_client_key, None)
            _poll_monitor_last_seen.pop(safe_client_key, None)
            if subscription_id:
                _monitor_targets.pop(subscription_id, None)
        return ""

    target = _normalized_target(network, market_type, symbol, interval)
    with _snapshot_condition:
        now = time.monotonic()
        _prune_poll_monitor_targets_locked(now)
        subscription_id = _poll_monitor_targets.get(safe_client_key)
        if not subscription_id:
            subscription_id = uuid4().hex
            _poll_monitor_targets[safe_client_key] = subscription_id
        _poll_monitor_last_seen[safe_client_key] = now
        target["userId"] = int(owner_user_id) if owner_user_id is not None else None
        _monitor_targets[subscription_id] = target
    # Historical chart data is always bootstrapped by the REST refresh worker.
    # Do not seed a WebSocket target from the local backtest corpus here: that
    # table may intentionally end at an old date (for example 1 September),
    # which would make a newly selected interval look current only because a
    # live candle was appended to stale history.  The connection mode still
    # controls the live ticker/kline transport; it must not change the source
    # of historical chart bars.
    return subscription_id


def get_execution_monitor_targets(user_ids: object = None) -> list[tuple[str, str, str, str]]:
    """Return execution-plan K-line targets for the selected WebSocket users."""

    if user_ids is None:
        allowed_users = None
    else:
        try:
            allowed_users = {int(value) for value in user_ids}
        except (TypeError, ValueError):
            allowed_users = set()

    with _snapshot_lock:
        targets = [
            (target["network"], target["marketType"], target["symbol"], target["interval"])
            for target in _monitor_targets.values()
        ]
    try:
        executing_positions = db.list_binance_simulated_positions_for_refresh()
    except Exception:
        logger.debug("Binance executing-plan targets are unavailable", exc_info=True)
        executing_positions = []
    for _user_id, position in executing_positions:
        if allowed_users is not None and int(_user_id) not in allowed_users:
            continue
        network = str(position.get("network") or "mainnet").lower()
        market_type = str(position.get("marketMode") or "FUTURES").upper()
        symbol = str(position.get("symbol") or "").upper()
        if network not in SUPPORTED_NETWORKS or market_type not in SUPPORTED_MARKET_TYPES or not SYMBOL_PATTERN.fullmatch(symbol):
            continue
        targets.extend((network, market_type, symbol, interval) for interval in ("5m", "15m", "1h", "4h"))
    return sorted(set(targets))


def get_market_stream_targets(user_ids: object = None) -> list[tuple[str, str, str, str]]:
    """Return only the currently displayed chart targets for WebSocket users.

    Execution plans are added separately by the WebSocket worker through
    ``get_execution_monitor_targets`` so chart subscriptions remain scoped to
    what the browser is currently displaying.
    """

    if user_ids is None:
        allowed_users = None
    else:
        try:
            allowed_users = {int(value) for value in user_ids}
        except (TypeError, ValueError):
            allowed_users = set()
    with _snapshot_condition:
        _prune_poll_monitor_targets_locked()
        targets = []
        for target in _monitor_targets.values():
            owner_id = target.get("userId")
            if allowed_users is not None and owner_id not in allowed_users:
                continue
            targets.append(
                (
                    target["network"],
                    target["marketType"],
                    target["symbol"],
                    target["interval"],
                )
            )
    return sorted(set(targets))


def get_execution_market_price_targets(user_ids: object = None) -> list[tuple[str, str, str]]:
    """Return active position symbols that need one live public price stream.

    Dynamic plan analysis continues to read the process-local REST K-line
    cache.  A futures position only needs mark price for live protection
    context; a legacy spot position uses ticker because spot has no mark
    price stream.
    """

    if user_ids is None:
        allowed_users = None
    else:
        try:
            allowed_users = {int(value) for value in user_ids}
        except (TypeError, ValueError):
            allowed_users = set()
    try:
        executing_positions = db.list_binance_simulated_positions_for_refresh()
    except Exception:
        logger.debug("Binance execution price targets are unavailable", exc_info=True)
        executing_positions = []
    targets = []
    for user_id, position in executing_positions:
        try:
            normalized_user_id = int(user_id)
        except (TypeError, ValueError):
            continue
        if allowed_users is not None and normalized_user_id not in allowed_users:
            continue
        network = str(position.get("network") or "mainnet").lower()
        market_type = str(position.get("marketMode") or "FUTURES").upper()
        symbol = str(position.get("symbol") or "").upper()
        if network not in SUPPORTED_NETWORKS or market_type not in SUPPORTED_MARKET_TYPES or not SYMBOL_PATTERN.fullmatch(symbol):
            continue
        targets.append((network, market_type, symbol))
    return sorted(set(targets))


def get_account_position_market_price_targets(user_ids: object) -> list[tuple[str, str, str]]:
    """Return each real futures position that needs exact live quote streams."""

    try:
        allowed_users = {int(value) for value in user_ids}
    except (TypeError, ValueError):
        return []
    targets = []
    for user_id in allowed_users:
        snapshot = get_cached_binance_account_snapshot(user_id) or {}
        credentials = snapshot.get("credentials") if isinstance(snapshot, dict) else {}
        network = str((credentials or {}).get("network") or "mainnet").lower()
        account = snapshot.get("account") if isinstance(snapshot, dict) else {}
        futures = account.get("futures") if isinstance(account, dict) else {}
        for position in futures.get("positions") if isinstance(futures, dict) else []:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("symbol") or "").upper()
            if network in SUPPORTED_NETWORKS and SYMBOL_PATTERN.fullmatch(symbol):
                targets.append((network, "FUTURES", symbol))
    return sorted(set(targets))


def _copy(value: object) -> object:
    return deepcopy(value)


def _as_number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_market_snapshot(network: str, market_type: str, value: dict | None = None, *, error: str | None = None) -> None:
    key = (network, market_type)
    with _snapshot_condition:
        previous = _market_snapshots.get(key) or {}
        if value is not None:
            snapshot = {
                "items": list(value.get("items") or []),
                "limit": int(value.get("limit") or MARKET_LIMIT),
                "stale": bool(value.get("stale")),
                "updatedAt": value.get("updatedAt") or int(time.time() * 1000),
                "error": None,
            }
        else:
            snapshot = {
                "items": list(previous.get("items") or []),
                "limit": int(previous.get("limit") or MARKET_LIMIT),
                "stale": True,
                "updatedAt": previous.get("updatedAt"),
                "error": str(error or "行情暂时不可用"),
            }
        _market_snapshots[key] = snapshot


def _set_kline_snapshot(key: tuple[str, str, str, str], value: dict | None = None, *, error: str | None = None) -> None:
    with _snapshot_condition:
        previous = _kline_snapshots.get(key) or {}
        if value is not None:
            snapshot = {
                "items": list(value.get("items") or []),
                "stale": bool(value.get("stale")),
                "updatedAt": int(time.time() * 1000),
                "error": None,
            }
        else:
            snapshot = {
                "items": list(previous.get("items") or []),
                "stale": True,
                "updatedAt": previous.get("updatedAt"),
                "error": str(error or "K 线暂时不可用"),
            }
        _kline_snapshots[key] = snapshot
        if value is not None and len(snapshot["items"]) >= 80:
            _kline_bootstrap_attempts.pop(key, None)


def cache_live_ticker(
    network: object,
    market_type: object,
    symbol: object,
    value: dict,
) -> None:
    """Merge one public WebSocket ticker into the process-local snapshot."""

    target = _normalized_target(network, market_type, symbol, "1m")
    if not isinstance(value, dict):
        return
    key = (target["network"], target["marketType"], target["symbol"])
    event_time = value.get("eventTime") or value.get("closeTime") or int(time.time() * 1000)
    try:
        event_time = int(event_time)
    except (TypeError, ValueError):
        event_time = int(time.time() * 1000)
    live = {
        key_name: value[key_name]
        for key_name in (
            "symbol",
            "lastPrice",
            "priceChange",
            "priceChangePercent",
            "highPrice",
            "lowPrice",
            "volume",
            "quoteVolume",
            "closeTime",
            "markPrice",
            "eventTime",
        )
        if key_name in value and value[key_name] is not None
    }
    live["symbol"] = target["symbol"]
    live["updatedAt"] = event_time
    live["stale"] = False
    with _snapshot_condition:
        previous = _live_ticker_snapshots.get(key) or {}
        merged = {**previous, **live}
        _live_ticker_snapshots[key] = merged
        market_key = (target["network"], target["marketType"])
        market = _market_snapshots.get(market_key)
        if market:
            items = []
            found = False
            for item in market.get("items") or []:
                if isinstance(item, dict) and str(item.get("symbol") or "").upper() == target["symbol"]:
                    items.append({**item, **merged})
                    found = True
                else:
                    items.append(item)
            if found:
                market["items"] = items
                market["updatedAt"] = event_time


def get_cached_live_ticker(
    network: object,
    market_type: object,
    symbol: object,
) -> dict:
    """Return the most recent public ticker without contacting Binance."""

    target = _normalized_target(network, market_type, symbol, "1m")
    key = (target["network"], target["marketType"], target["symbol"])
    with _snapshot_lock:
        return _copy(
            _live_ticker_snapshots.get(key)
            or {"symbol": target["symbol"], "stale": True, "updatedAt": None}
        )


def cache_live_kline(
    network: object,
    market_type: object,
    symbol: object,
    interval: object,
    item: dict,
) -> None:
    """Upsert the currently streaming candle without discarding REST history."""

    target = _normalized_target(network, market_type, symbol, interval)
    if not isinstance(item, dict):
        return
    try:
        open_time = int(item.get("openTime"))
    except (TypeError, ValueError):
        return
    if open_time <= 0:
        return
    normalized = {
        "openTime": open_time,
        "open": item.get("open"),
        "high": item.get("high"),
        "low": item.get("low"),
        "close": item.get("close"),
        "volume": item.get("volume", 0),
        "closeTime": item.get("closeTime"),
    }
    key = (target["network"], target["marketType"], target["symbol"], target["interval"])
    event_time = item.get("eventTime") or item.get("closeTime") or int(time.time() * 1000)
    try:
        event_time = int(event_time)
    except (TypeError, ValueError):
        event_time = int(time.time() * 1000)
    with _snapshot_condition:
        previous = _kline_snapshots.get(key) or {}
        by_open_time = {
            int(candidate.get("openTime")): candidate
            for candidate in previous.get("items") or []
            if isinstance(candidate, dict) and _safe_int(candidate.get("openTime")) is not None
        }
        by_open_time[open_time] = normalized
        items = [by_open_time[index] for index in sorted(by_open_time)][-KLINE_LIMIT:]
        _kline_snapshots[key] = {
            "items": items,
            "stale": False,
            "updatedAt": event_time,
            "error": None,
        }
        if len(items) >= 80:
            _kline_bootstrap_attempts.pop(key, None)


def cache_kline_snapshot(
    network: object,
    market_type: object,
    symbol: object,
    interval: object,
    value: dict,
) -> None:
    """Store a one-time REST bootstrap in the process cache."""

    target = _normalized_target(network, market_type, symbol, interval)
    if not isinstance(value, dict):
        raise ValueError("K 线快照格式无效")
    _set_kline_snapshot(
        (target["network"], target["marketType"], target["symbol"], target["interval"]),
        value,
    )


def cache_local_futures_history_snapshot(network: object, symbol: object, interval: object) -> dict:
    """Seed a WebSocket chart from local history without contacting Binance."""

    target = _normalized_target(network, "FUTURES", symbol, interval)
    items = db.list_latest_binance_futures_history_klines(
        target["network"], target["symbol"], target["interval"], KLINE_LIMIT
    )
    if not items:
        raise ValueError(f"{target['symbol']} {target['interval']} 没有可用本地 K 线历史")
    payload = {
        "items": items,
        "stale": False,
        "source": "LOCAL_HISTORY_WEBSOCKET",
    }
    _set_kline_snapshot(
        (target["network"], target["marketType"], target["symbol"], target["interval"]),
        payload,
    )
    return payload


def get_cached_kline_snapshot(network: object, market_type: object, symbol: object, interval: object) -> dict:
    target = _normalized_target(network, market_type, symbol, interval)
    key = (target["network"], target["marketType"], target["symbol"], target["interval"])
    with _snapshot_lock:
        return _copy(
            _kline_snapshots.get(key)
            or {"items": [], "stale": False, "updatedAt": None, "error": None}
        )


def refresh_public_markets_once(*, force: bool = True) -> None:
    """Refresh the futures market list and legacy spot lists when requested.

    A healthy result is refreshed every five minutes.  A failed or stale
    result is retried sooner so one transient exchange/TLS failure cannot
    leave the page showing a connection error for the whole five-minute
    window.
    """

    with _snapshot_lock:
        has_legacy_spot_target = any(
            str(target.get("marketType") or "").upper() == "SPOT"
            for target in _monitor_targets.values()
        )
    requests = {"FUTURES": get_futures_markets}
    if has_legacy_spot_target:
        requests["SPOT"] = get_spot_markets
    now = time.monotonic()
    if not force:
        with _snapshot_lock:
            requests = {
                market_type: loader
                for market_type, loader in requests.items()
                if now >= _public_market_next_due.get(market_type, 0.0)
            }
    if not requests:
        return
    with ThreadPoolExecutor(max_workers=len(requests), thread_name_prefix="binance-market-snapshot") as executor:
        futures = {
            executor.submit(loader, "mainnet", MARKET_LIMIT): market_type
            for market_type, loader in requests.items()
        }
        for future in as_completed(futures):
            market_type = futures[future]
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise BinanceApiError("交易所返回的行情格式无效")
                _set_market_snapshot("mainnet", market_type, result)
                with _snapshot_lock:
                    _public_market_next_due[market_type] = time.monotonic() + (
                        PUBLIC_RECONCILIATION_INTERVAL_SECONDS
                        if not bool(result.get("stale"))
                        else PUBLIC_RETRY_INTERVAL_SECONDS
                    )
            except Exception as exc:
                _set_market_snapshot("mainnet", market_type, error=str(exc))
                with _snapshot_lock:
                    _public_market_next_due[market_type] = time.monotonic() + PUBLIC_RETRY_INTERVAL_SECONDS
                logger.warning("Binance %s market snapshot refresh failed", market_type.lower(), exc_info=True)


def _kline_needs_rest_fallback(key: tuple[str, str, str, str], now_ms: int) -> bool:
    snapshot = _kline_snapshots.get(key) or {}
    if not snapshot.get("items") or snapshot.get("stale"):
        return True
    try:
        updated_at = int(snapshot.get("updatedAt") or 0)
    except (TypeError, ValueError):
        return True
    return updated_at <= 0 or now_ms - updated_at >= LIVE_KLINE_FALLBACK_AFTER_SECONDS * 1000


def _websocket_target_is_healthy(key: tuple[str, str, str, str]) -> bool:
    """Ask the optional transport worker whether REST can stay idle."""

    try:
        from .binance_websocket_worker import is_market_target_healthy
    except ImportError:
        try:
            from binance_websocket_worker import is_market_target_healthy
        except ImportError:
            return False
    try:
        return bool(is_market_target_healthy(key))
    except Exception:
        return False


def refresh_monitored_klines_once(
    *,
    force: bool = True,
    include_execution_targets: bool = False,
    only_stale: bool = False,
    execution_only: bool = False,
    interactive_only: bool = False,
) -> None:
    """Refresh selected and execution-plan K-lines through REST."""

    with _snapshot_lock:
        targets = list(_monitor_targets.values())
    unique_targets = set()
    if not execution_only:
        for target in targets:
            unique_targets.add((
                target["network"], target["marketType"], target["symbol"], target["interval"]
            ))
    if include_execution_targets or execution_only:
        execution_targets = set(get_execution_monitor_targets())
        unique_targets.update(execution_targets)
    if interactive_only:
        interactive_targets = {
            (target["network"], target["marketType"], target["symbol"], target["interval"])
            for target in targets
        }
        unique_targets.intersection_update(interactive_targets)
    # Historical bars are an explicit REST responsibility for every
    # connection mode.  A WebSocket may merge a fresh in-progress candle into
    # this cache, but it must never suppress a REST refresh: otherwise a
    # target seeded from an old local corpus can remain stuck before the
    # current date.  Keep this path independent of the live transport so
    # switching between 5m/15m/1h/4h/1d always requests that interval's latest
    # bars from Binance.
    if only_stale:
        now_ms = int(time.time() * 1000)
        with _snapshot_lock:
            unique_targets = {
                key for key in unique_targets
                if _kline_needs_rest_fallback(key, now_ms)
            }
    if not force:
        now = time.monotonic()
        with _snapshot_lock:
            bootstrap_targets = set()
            for key in unique_targets:
                if len((_kline_snapshots.get(key) or {}).get("items") or []) >= 80:
                    continue
                last_attempt = _kline_bootstrap_attempts.get(key, 0.0)
                if now - last_attempt < KLINE_BOOTSTRAP_RETRY_SECONDS:
                    continue
                _kline_bootstrap_attempts[key] = now
                bootstrap_targets.add(key)
            unique_targets = bootstrap_targets
    if not unique_targets:
        return

    with ThreadPoolExecutor(max_workers=min(8, len(unique_targets)), thread_name_prefix="binance-kline-snapshot") as executor:
        futures = {}
        for network, market_type, symbol, interval in unique_targets:
            loader = get_spot_klines if market_type == "SPOT" else get_futures_klines
            # Execution monitoring only needs enough recent bars for structure
            # and trailing-stop checks. The model route needs its larger 5m
            # input window; selected frontend charts keep the full chart limit.
            request_limit = KLINE_LIMIT
            if execution_only:
                request_limit = 320 if interval == "5m" else 192
            futures[executor.submit(loader, network, symbol, interval, request_limit, with_meta=True)] = (
                network,
                market_type,
                symbol,
                interval,
            )
        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise BinanceApiError("交易所返回的 K 线格式无效")
                _set_kline_snapshot(key, result)
            except Exception as exc:
                _set_kline_snapshot(key, error=str(exc))
                logger.warning("Binance monitored K-line refresh failed for %s", key[2], exc_info=True)


def _market_snapshot(network: str, market_type: str) -> dict:
    with _snapshot_lock:
        return _copy(_market_snapshots.get((network, market_type)) or {
            "items": [],
            "limit": MARKET_LIMIT,
            "stale": False,
            "updatedAt": None,
            "error": None,
        })


def _selected_snapshot(target: dict) -> dict:
    market = _market_snapshot(target["network"], target["marketType"])
    ticker = next((item for item in market.get("items", []) if item.get("symbol") == target["symbol"]), None)
    with _snapshot_lock:
        live_ticker = _copy(
            _live_ticker_snapshots.get(
                (target["network"], target["marketType"], target["symbol"])
            )
        )
    if live_ticker:
        ticker = {**(ticker or {}), **live_ticker}
    key = (target["network"], target["marketType"], target["symbol"], target["interval"])
    with _snapshot_lock:
        kline = _copy(_kline_snapshots.get(key) or {
            "items": [],
            "stale": False,
            "updatedAt": None,
            "error": None,
        })
    return {
        "marketType": target["marketType"],
        "symbol": target["symbol"],
        "interval": target["interval"],
        "ticker": ticker,
        "klines": kline.get("items") or [],
        "stale": bool(kline.get("stale")),
        "updatedAt": kline.get("updatedAt"),
        "error": kline.get("error"),
    }


def _empty_selected_snapshot(market_type: str, interval: str) -> dict:
    return {
        "marketType": market_type,
        "symbol": "",
        "interval": interval,
        "ticker": None,
        "klines": [],
        "stale": False,
        "updatedAt": None,
        "error": None,
    }


def _connection_status(user_id: int | None) -> dict:
    if user_id is None:
        return {
            "mode": "REST",
            "account": {"status": "REST"},
            "market": {"status": "REST", "subscriptions": 0},
        }
    try:
        from .binance_websocket_worker import get_binance_connection_status
    except ImportError:
        try:
            from binance_websocket_worker import get_binance_connection_status
        except ImportError:
            return {"mode": "REST", "account": {"status": "REST"}, "market": {"status": "REST", "subscriptions": 0}}
    try:
        return get_binance_connection_status(user_id)
    except Exception:
        logger.debug("Binance connection status is unavailable", exc_info=True)
        return {"mode": "REST", "account": {"status": "UNKNOWN"}, "market": {"status": "UNKNOWN", "subscriptions": 0}}


def _project_account_positions_into_monitors(
    account_snapshot: dict | None,
    simulated_snapshot: dict | None,
) -> dict | None:
    """Make the snapshot response use one live source for account and monitors.

    The account WebSocket and plan refresh worker have different cadences.  A
    response assembled between those cycles used to contain a new account
    value beside the previous plan value.  Project the already-read account
    position into the response only; persistence and trailing-stop decisions
    remain owned by the monitor worker.
    """

    if not isinstance(simulated_snapshot, dict) or not isinstance(account_snapshot, dict):
        return simulated_snapshot
    account = account_snapshot.get("account")
    futures = account.get("futures") if isinstance(account, dict) else None
    actual_positions = futures.get("positions") if isinstance(futures, dict) else None
    if not isinstance(actual_positions, list):
        return simulated_snapshot

    def position_key(item: object) -> tuple[str, str]:
        if not isinstance(item, dict):
            return "", ""
        symbol = str(item.get("symbol") or "").strip().upper()
        side = str(item.get("side") or "").strip().upper()
        if side not in {"LONG", "SHORT"}:
            position_side = str(item.get("positionSide") or "").strip().upper()
            if position_side in {"LONG", "SHORT"}:
                side = position_side
            elif position_side == "BOTH":
                try:
                    side = "LONG" if float(item.get("positionAmt") or 0) > 0 else "SHORT"
                except (TypeError, ValueError):
                    side = ""
        return symbol, side

    live_by_key = {
        position_key(item): item
        for item in actual_positions
        if position_key(item)[0] and position_key(item)[1]
    }

    def absolute_quantity(value: object) -> object:
        try:
            return abs(float(value))
        except (TypeError, ValueError):
            return None

    items = simulated_snapshot.get("items")
    if not isinstance(items, list):
        return simulated_snapshot
    projected = deepcopy(simulated_snapshot)
    projected_items = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("executionStatus") or "").upper() == "STOPPED":
            projected_items.append(item)
            continue
        actual = live_by_key.get(position_key(item))
        if not isinstance(actual, dict):
            projected_items.append(item)
            continue
        updated = dict(item)
        mark_price = actual.get("markPrice")
        last_price = actual.get("lastPrice")
        unrealized = actual.get("unrealizedProfit", actual.get("unRealizedProfit"))
        roe = actual.get("roePercent")
        entry_price = actual.get("entryPrice")
        quantity = actual.get("quantity", actual.get("positionAmt"))
        for key, value in (
            ("markPrice", mark_price),
            ("lastPrice", last_price),
            ("currentPrice", mark_price),
            ("latestPrice", last_price if last_price not in (None, "") else mark_price),
            ("unrealizedPnl", unrealized),
            ("unrealizedPnlPercent", roe),
            ("entryPrice", entry_price),
            ("costPrice", entry_price),
            ("quantity", absolute_quantity(quantity)),
        ):
            if value is not None:
                updated[key] = value
        plan = updated.get("plan")
        if isinstance(plan, dict):
            projected_plan = dict(plan)
            for key, value in (
                ("markPrice", mark_price),
                ("lastPrice", last_price),
                ("currentPrice", mark_price),
                ("latestPrice", last_price if last_price not in (None, "") else mark_price),
            ):
                if value is not None:
                    projected_plan[key] = value
            updated["plan"] = projected_plan
        projected_items.append(updated)
    projected["items"] = projected_items
    return projected


def _project_live_prices_into_account(account_snapshot: dict | None) -> dict | None:
    """Overlay public ticker and mark-price streams onto real account positions."""

    if not isinstance(account_snapshot, dict):
        return account_snapshot
    stored_account = account_snapshot.get("account")
    credentials = account_snapshot.get("credentials")
    futures = stored_account.get("futures") if isinstance(stored_account, dict) else None
    positions = futures.get("positions") if isinstance(futures, dict) else None
    if not isinstance(positions, list):
        return account_snapshot
    network = str((credentials or {}).get("network") or "mainnet").lower()
    if network not in SUPPORTED_NETWORKS:
        return account_snapshot
    projected = deepcopy(account_snapshot)
    projected_positions = []
    with _snapshot_lock:
        for position in positions:
            updated = dict(position) if isinstance(position, dict) else position
            if isinstance(updated, dict):
                symbol = str(updated.get("symbol") or "").upper()
                quote = _live_ticker_snapshots.get((network, "FUTURES", symbol)) or {}
                for field in ("lastPrice", "markPrice"):
                    if _as_number(quote.get(field)) > 0:
                        updated[field] = quote[field]
            projected_positions.append(updated)
    projected["account"]["futures"] = {**futures, "positions": projected_positions}
    return projected


def _snapshot_payload(network: str, market_type: str, interval: str, user_id: int | None, target: dict | None = None) -> dict:
    futures = _market_snapshot(network, "FUTURES")
    markets = {"futures": futures}
    snapshot_sources = [futures]
    if market_type == "SPOT":
        spot = _market_snapshot(network, "SPOT")
        markets["spot"] = spot
        snapshot_sources.append(spot)
    account = get_cached_binance_account_snapshot(user_id) if user_id else None
    account = _project_live_prices_into_account(account)
    simulated = get_cached_binance_simulated_portfolio(user_id) if user_id else None
    copy_trading = get_cached_binance_copy_trading_history(user_id)
    if user_id and isinstance(copy_trading, dict):
        cleanup_binance_smart_money_follows_without_real_positions(user_id)
        copy_trading = project_binance_copy_trading_follows(
            copy_trading,
            db.list_user_binance_smart_money_position_follows(user_id),
        )
    simulated = _project_account_positions_into_monitors(account, simulated)
    selected = _selected_snapshot(target) if target else _empty_selected_snapshot(market_type, interval)
    timestamps = [
        value.get("updatedAt")
        for value in (*snapshot_sources, selected, account or {}, simulated or {})
        if value.get("updatedAt")
    ]
    return {
        "status": "READY" if (markets.get(market_type.lower()) or {}).get("items") else "LOADING",
        "updatedAt": max(timestamps) if timestamps else None,
        "markets": markets,
        "selected": selected,
        "account": account,
        "connection": _connection_status(user_id),
        "copyTrading": copy_trading,
        "simulatedPositions": simulated or {"items": [], "updatedAt": None, "stale": False, "error": None},
    }


def get_snapshot(subscription_id: str, user_id: int | None = None) -> dict:
    with _snapshot_lock:
        target = _copy(_monitor_targets.get(str(subscription_id)))
    if target is None:
        raise ValueError("行情订阅不存在")
    return _snapshot_payload(target["network"], target["marketType"], target["interval"], user_id, target)


def get_snapshot_without_target(network: object = "mainnet", market_type: object = "SPOT", interval: object = "1h", user_id: int | None = None) -> dict:
    safe_network = str(network or "mainnet").strip().lower()
    safe_market_type = str(market_type or "SPOT").strip().upper()
    safe_interval = str(interval or "1h").strip()
    if safe_network not in SUPPORTED_NETWORKS:
        raise ValueError("网络必须选择主网或测试网")
    if safe_market_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError("行情类型必须选择现货或合约")
    if safe_interval not in SUPPORTED_INTERVALS:
        raise ValueError("K 线周期无效")
    return _snapshot_payload(safe_network, safe_market_type, safe_interval, user_id)


def notify_snapshot_update() -> None:
    global _snapshot_version
    with _snapshot_condition:
        _snapshot_version += 1
        _snapshot_condition.notify_all()


def start_binance_snapshot_worker() -> None:
    global _worker_stop_event, _worker_thread
    with _worker_state_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _worker_stop_event = Event()
        _worker_thread = Thread(
            target=_run_snapshot_worker,
            args=(_worker_stop_event,),
            name="binance-snapshot-refresh",
            daemon=True,
        )
        _worker_thread.start()


def stop_binance_snapshot_worker() -> None:
    global _worker_stop_event, _worker_thread
    with _worker_state_lock:
        if _worker_stop_event:
            _worker_stop_event.set()
        _worker_stop_event = None
        _worker_thread = None


def _run_snapshot_worker(stop_event: Event) -> None:
    tasks = (
        (
            "public market reconciliation",
            lambda: refresh_public_markets_once(force=False),
            PUBLIC_RETRY_INTERVAL_SECONDS,
            0,
        ),
        ("account reconciliation", refresh_binance_accounts_once, ACCOUNT_RECONCILIATION_INTERVAL_SECONDS, 0),
        ("requested account reconciliation", refresh_requested_binance_accounts_once, ACCOUNT_REQUEST_REFRESH_INTERVAL_SECONDS, 0),
        ("simulated plans", refresh_binance_simulated_positions_once, SNAPSHOT_REFRESH_INTERVAL_SECONDS, 0),
        ("copy trading history", refresh_binance_copy_trading_histories_once, COPY_TRADING_REFRESH_INTERVAL_SECONDS, 0),
        (
            "interactive monitored K-lines",
            lambda: refresh_monitored_klines_once(force=True, interactive_only=True),
            MONITOR_FALLBACK_INTERVAL_SECONDS,
            0,
        ),
        (
            "execution monitor K-lines",
            lambda: refresh_monitored_klines_once(force=True, include_execution_targets=True, execution_only=True),
            MONITOR_HISTORY_INTERVAL_SECONDS,
            0,
        ),
    )
    workers = []
    for name, loader, interval, initial_delay in tasks:
        worker = Thread(
            target=_run_periodic_snapshot_task,
            args=(stop_event, name, loader, interval, initial_delay),
            name=f"binance-{name.replace(' ', '-').lower()}",
            daemon=True,
        )
        worker.start()
        workers.append(worker)
    try:
        stop_event.wait()
    finally:
        pass


def _run_periodic_snapshot_task(
    stop_event: Event,
    name: str,
    loader,
    interval_seconds: float = SNAPSHOT_REFRESH_INTERVAL_SECONDS,
    initial_delay_seconds: float = 0,
) -> None:
    if initial_delay_seconds and stop_event.wait(initial_delay_seconds):
        return
    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            loader()
        except Exception:
            logger.warning("Binance %s snapshot refresh failed", name, exc_info=True)
        notify_snapshot_update()
        elapsed = time.monotonic() - started_at
        stop_event.wait(max(0.0, interval_seconds - elapsed))
