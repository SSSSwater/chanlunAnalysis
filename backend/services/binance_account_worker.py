from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock

try:
    from . import database as db
    from .binance_client import get_account as get_binance_account
    from .binance_futures_client import get_futures_account, get_futures_protection_state, websocket_api_requests
except ImportError:
    import database as db
    from binance_client import get_account as get_binance_account
    from binance_futures_client import get_futures_account, get_futures_protection_state, websocket_api_requests


ACCOUNT_EVENT_RECONCILIATION_COOLDOWN_SECONDS = 3
# A successful protection mutation is written to the local cache immediately.
# Keep that optimistic value only long enough for the exchange order to become
# visible, then let an authoritative REST read win (including a deletion).
PROTECTION_CACHE_RECONCILIATION_TTL_SECONDS = 10

logger = logging.getLogger(__name__)
_account_snapshot_lock = RLock()
_account_snapshots: dict[int, dict] = {}
_pending_protection_cache_updates: dict[tuple[int, str, str], dict[str, float]] = {}
_account_refresh_request_lock = RLock()
_requested_account_refreshes: set[int] = set()
_forced_account_refreshes: set[int] = set()
_last_account_refresh_request_at: dict[int, float] = {}


def get_binance_account_snapshot(network: str, api_key: str, api_secret: str) -> dict:
    """Read spot and futures account state with REST using one account's keys."""

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="binance-account-read") as executor:
        spot_future = executor.submit(get_binance_account, network, api_key, api_secret)
        futures_future = executor.submit(get_futures_account, network, api_key, api_secret)
        try:
            account = spot_future.result()
            if not isinstance(account, dict):
                raise RuntimeError("现货账户返回格式无效")
            account = dict(account)
            account["spotAvailable"] = True
        except Exception as exc:
            account = {
                "available": False,
                "canTrade": False,
                "balances": [],
                "spotAvailable": False,
                "spotError": str(exc).strip() or "现货账户暂时不可用",
            }
        try:
            account["futures"] = futures_future.result()
            if not isinstance(account["futures"], dict):
                raise RuntimeError("合约账户返回格式无效")
        except Exception as exc:
            account["futures"] = {
                "available": False,
                "canTrade": False,
                "assets": [],
                "positions": [],
                "error": str(exc).strip() or "合约账户暂时不可用",
            }
    return account


def _futures_payload_unavailable(account: object) -> bool:
    if not isinstance(account, dict):
        return True
    futures = account.get("futures")
    return not isinstance(futures, dict) or futures.get("available") is False


def _spot_payload_unavailable(account: object) -> bool:
    return isinstance(account, dict) and account.get("spotAvailable") is False


def _safe_credentials_profile(profile: object) -> dict:
    """Keep only non-sensitive credential metadata in the process cache."""

    if not isinstance(profile, dict):
        return {
            "configured": False,
            "network": "mainnet",
            "apiKeyMasked": "",
            "updatedAt": None,
            "requiresReconfiguration": False,
            "connectionMode": "REST",
        }
    connection_mode = str(profile.get("connectionMode") or profile.get("mode") or "REST").strip().upper()
    if connection_mode not in {"REST", "WEBSOCKET"}:
        connection_mode = "REST"
    return {
        "configured": bool(profile.get("configured")),
        "network": "mainnet",
        "apiKeyMasked": str(profile.get("apiKeyMasked") or ""),
        "updatedAt": profile.get("updatedAt"),
        "requiresReconfiguration": bool(profile.get("requiresReconfiguration")),
        "connectionMode": connection_mode,
    }


def cache_binance_account_snapshot(
    user_id: int,
    account: dict | None,
    credentials_profile: object = None,
    *,
    stale: bool = False,
    error: str | None = None,
    transport: str | None = None,
    connection: dict | None = None,
    protection_reconciled: bool = False,
) -> dict:
    """Store one user's last account result without retaining API secrets.

    ``protection_reconciled`` marks a snapshot whose open protection orders
    were just read from REST. That response is authoritative, including when
    it reports that a stop is absent, so it must not be overlaid with the
    short-lived optimistic cache written after a successful order mutation.
    """

    normalized_user_id = int(user_id)
    with _account_snapshot_lock:
        previous = _account_snapshots.get(normalized_user_id) or {}
        profile = _safe_credentials_profile(
            credentials_profile if credentials_profile is not None else previous.get("credentials")
        )
        if account is not None:
            stored_account = deepcopy(account)
        else:
            stored_account = deepcopy(previous.get("account")) if previous.get("account") is not None else None
        if protection_reconciled:
            for key in list(_pending_protection_cache_updates):
                if key[0] == normalized_user_id:
                    _pending_protection_cache_updates.pop(key, None)
        else:
            _apply_pending_protection_cache_updates_locked(normalized_user_id, stored_account)
        safe_transport = str(transport or previous.get("transport") or "REST").strip().upper()
        if safe_transport not in {"REST", "WEBSOCKET"}:
            safe_transport = "REST"
        stored_connection = deepcopy(connection) if isinstance(connection, dict) else deepcopy(previous.get("connection") or {})
        snapshot = {
            "configured": bool(profile.get("configured") or stored_account is not None),
            "account": stored_account,
            "credentials": profile,
            "updatedAt": int(time.time() * 1000) if account is not None and not stale else previous.get("updatedAt"),
            "checkedAt": int(time.time() * 1000),
            "stale": bool(stale),
            "error": str(error or "") or None,
            "transport": safe_transport,
            "connection": stored_connection,
            "positionPnlHistory": deepcopy(previous.get("positionPnlHistory") or []),
        }
        _account_snapshots[normalized_user_id] = snapshot
        result = deepcopy(snapshot)
    if account is not None and not stale:
        futures = stored_account.get("futures") if isinstance(stored_account, dict) else None
        positions = futures.get("positions") if isinstance(futures, dict) else None
        if isinstance(futures, dict) and isinstance(positions, list):
            try:
                history = db.record_user_binance_futures_position_pnl_history(
                    normalized_user_id,
                    positions,
                    result.get("updatedAt"),
                )
                result["positionPnlHistory"] = history
                with _account_snapshot_lock:
                    cached = _account_snapshots.get(normalized_user_id)
                    if isinstance(cached, dict):
                        cached["positionPnlHistory"] = deepcopy(history)
            except Exception:
                logger.warning("Unable to persist Binance PnL history for user %s", normalized_user_id, exc_info=True)
    return result


def _stop_is_at_least_as_strict(current: object, expected: float, is_long: bool) -> bool:
    try:
        current_value = float(current)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(current_value) or current_value <= 0:
        return False
    tolerance = max(abs(expected) * 1e-9, 1e-9)
    return current_value >= expected - tolerance if is_long else current_value <= expected + tolerance


def _apply_pending_protection_cache_updates_locked(user_id: int, account: object) -> None:
    """Prevent an in-flight REST read from rolling back a successful stop update."""

    if not isinstance(account, dict):
        return
    now = time.time()
    pending_keys = [
        key
        for key, update in _pending_protection_cache_updates.items()
        if key[0] == int(user_id)
        and now - float(update.get("updatedAt") or now) > PROTECTION_CACHE_RECONCILIATION_TTL_SECONDS
    ]
    for key in pending_keys:
        _pending_protection_cache_updates.pop(key, None)

    futures = account.get("futures")
    positions = futures.get("positions") if isinstance(futures, dict) else None
    if not isinstance(positions, list):
        return
    remaining: set[tuple[int, str, str]] = set()
    for key, update in list(_pending_protection_cache_updates.items()):
        pending_user_id, symbol, position_side = key
        if pending_user_id != int(user_id):
            continue
        match = next(
            (
                item
                for item in positions
                if isinstance(item, dict)
                and str(item.get("symbol") or "").strip().upper() == symbol
                and str(item.get("positionSide") or "BOTH").strip().upper() == position_side
            ),
            None,
        )
        if match is None and position_side == "BOTH":
            match = next(
                (
                    item
                    for item in positions
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").strip().upper() == symbol
                ),
                None,
            )
        if match is None:
            # Do not recreate a position that has already disappeared. Keep
            # the update briefly in case this is only a partial account read.
            remaining.add(key)
            continue

        item = dict(match)
        side = str(item.get("side") or "").strip().upper()
        is_long = side != "SHORT" and position_side != "SHORT"
        stop_update = update.get("stopLoss")
        if stop_update is not None:
            current_stop = item.get("positionStopLoss")
            if current_stop is None:
                current_stop = item.get("stopLoss")
            if _stop_is_at_least_as_strict(current_stop, stop_update, is_long):
                update.pop("stopLoss", None)
            else:
                item["positionStopLoss"] = stop_update
                item["stopLoss"] = stop_update
                orders = item.get("protectionOrders")
                if isinstance(orders, list):
                    updated_orders = []
                    for order in orders:
                        if not isinstance(order, dict):
                            updated_orders.append(order)
                            continue
                        updated_order = dict(order)
                        order_type = str(updated_order.get("type") or "").upper()
                        close_position = updated_order.get("closePosition") is True or str(
                            updated_order.get("closePosition") or ""
                        ).strip().lower() == "true"
                        if close_position and "STOP" in order_type and "TAKE_PROFIT" not in order_type:
                            updated_order["stopPrice"] = stop_update
                            updated_order["triggerPrice"] = stop_update
                        updated_orders.append(updated_order)
                    item["protectionOrders"] = updated_orders

        positions[positions.index(match)] = item
        # Quantity-based latest-price targets are confirmed by the next REST
        # order reconciliation. Never cache them as a position-level take-profit.
        if update.get("stopLoss") is not None:
            remaining.add(key)
        else:
            _pending_protection_cache_updates.pop(key, None)
    # Keep only updates that still need reconciliation for this user.
    for key in list(_pending_protection_cache_updates):
        if key[0] == int(user_id) and key not in remaining:
            _pending_protection_cache_updates.pop(key, None)


_WS_POSITION_PRESERVED_FIELDS = (
    "lastPrice",
    "protectionOrders",
    "positionStopLoss",
    "partialStopLoss",
    "partialTakeProfitLevels",
    "partialTakeProfit",
    "partialQuantity",
    "partialQuantityRatio",
    "stopLoss",
)


def _merge_websocket_futures_account(previous_account: object, futures_account: dict) -> dict:
    """Merge account.status data without erasing REST-only protection metadata."""

    previous_futures = {}
    if isinstance(previous_account, dict) and isinstance(previous_account.get("futures"), dict):
        previous_futures = previous_account["futures"]
    merged = deepcopy(futures_account)
    merged["available"] = True
    merged["snapshotAvailable"] = True
    old_positions = {
        (
            str(item.get("symbol") or "").upper(),
            str(item.get("positionSide") or "BOTH").upper(),
        ): item
        for item in previous_futures.get("positions") or []
        if isinstance(item, dict)
    }
    positions = []
    for raw in merged.get("positions") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        old = old_positions.get(
            (
                str(item.get("symbol") or "").upper(),
                str(item.get("positionSide") or "BOTH").upper(),
            )
        )
        if old:
            for field in _WS_POSITION_PRESERVED_FIELDS:
                if field not in item or item.get(field) is None:
                    item[field] = deepcopy(old.get(field))
        # account.status contains balances and position risk, but not the
        # open algo/conditional exit orders.  A preserved stop is therefore
        # only a last-known display value until the REST reconciler checks it.
        item["protectionState"] = "UNKNOWN"
        item["protectionCheckedAt"] = None
        positions.append(item)
    merged["positions"] = positions
    merged["protectionState"] = "UNKNOWN"
    merged["protectionCheckedAt"] = None
    # account.status does not expose the algo-order fields used by the UI;
    # retain the last known values until a REST reconciliation supplies them.
    for field in ("openOrderCount", "openAlgoOrderCount", "positionRiskError", "protectionError"):
        if field not in merged and field in previous_futures:
            merged[field] = deepcopy(previous_futures[field])
    return merged


def cache_binance_websocket_account_snapshot(
    user_id: int,
    futures_account: dict,
    credentials_profile: object = None,
    *,
    connection: dict | None = None,
) -> dict:
    """Publish a futures account.status response into the shared account cache."""

    if not isinstance(futures_account, dict):
        raise ValueError("WebSocket 合约账户格式无效")
    with _account_snapshot_lock:
        previous = _account_snapshots.get(int(user_id)) or {}
        previous_account = previous.get("account") if isinstance(previous.get("account"), dict) else {}
        account = deepcopy(previous_account)
        account["futures"] = _merge_websocket_futures_account(previous_account, futures_account)
        account.setdefault("spotAvailable", previous_account.get("spotAvailable", False))
    return cache_binance_account_snapshot(
        user_id,
        account,
        credentials_profile,
        stale=False,
        error=None,
        transport="WEBSOCKET",
        connection=connection,
    )


def mark_binance_account_websocket_error(
    user_id: int,
    error: object,
    credentials_profile: object = None,
    *,
    connection: dict | None = None,
) -> dict:
    """Mark the account line stale while preserving the last good payload."""

    return cache_binance_account_snapshot(
        user_id,
        None,
        credentials_profile,
        stale=True,
        error=str(error or "账户 WebSocket 暂时不可用"),
        transport="WEBSOCKET",
        connection=connection,
    )


def get_cached_binance_account_snapshot(user_id: int) -> dict | None:
    with _account_snapshot_lock:
        snapshot = _account_snapshots.get(int(user_id))
        return deepcopy(snapshot) if snapshot is not None else None


def update_cached_binance_futures_position_protection(
    user_id: int,
    symbol: str,
    position_side: str,
    *,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> dict | None:
    """Apply a confirmed protection response to the in-memory account view.

    A successful order response is authoritative for the one protection value
    it changed.  Updating that value immediately prevents the five-second
    monitor from seeing its own old cache and submitting the same stop again
    while the complete REST account reconciliation is still queued.
    """

    normalized_user_id = int(user_id)
    safe_symbol = str(symbol or "").strip().upper()
    safe_position_side = str(position_side or "BOTH").strip().upper()
    with _account_snapshot_lock:
        snapshot = _account_snapshots.get(normalized_user_id)
        if not isinstance(snapshot, dict):
            return None
        account = snapshot.get("account")
        if not isinstance(account, dict):
            return None
        futures = account.get("futures")
        if not isinstance(futures, dict) or not isinstance(futures.get("positions"), list):
            return None

        positions = deepcopy(futures["positions"])
        match_index = next(
            (
                index
                for index, item in enumerate(positions)
                if isinstance(item, dict)
                and str(item.get("symbol") or "").strip().upper() == safe_symbol
                and str(item.get("positionSide") or "BOTH").strip().upper() == safe_position_side
            ),
            None,
        )
        if match_index is None and safe_position_side == "BOTH":
            match_index = next(
                (
                    index
                    for index, item in enumerate(positions)
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").strip().upper() == safe_symbol
                ),
                None,
            )
        if match_index is None:
            return None

        position = dict(positions[match_index])
        if stop_loss is not None:
            normalized_stop = float(stop_loss)
            position["positionStopLoss"] = normalized_stop
            position["stopLoss"] = normalized_stop
            protection_orders = position.get("protectionOrders")
            if isinstance(protection_orders, list):
                updated_orders = []
                for order in protection_orders:
                    if not isinstance(order, dict):
                        updated_orders.append(order)
                        continue
                    updated = dict(order)
                    order_type = str(updated.get("type") or "").upper()
                    close_position = updated.get("closePosition") is True or str(
                        updated.get("closePosition") or ""
                    ).strip().lower() == "true"
                    if close_position and "STOP" in order_type and "TAKE_PROFIT" not in order_type:
                        updated["stopPrice"] = normalized_stop
                        updated["triggerPrice"] = normalized_stop
                    updated_orders.append(updated)
                position["protectionOrders"] = updated_orders
        positions[match_index] = position
        updated_account = deepcopy(account)
        updated_futures = deepcopy(futures)
        updated_futures["positions"] = positions
        updated_account["futures"] = updated_futures
        updated_snapshot = deepcopy(snapshot)
        updated_snapshot["account"] = updated_account
        updated_snapshot["checkedAt"] = int(time.time() * 1000)
        updated_snapshot["protectionSyncAt"] = updated_snapshot["checkedAt"]
        pending_key = (normalized_user_id, safe_symbol, safe_position_side)
        pending = _pending_protection_cache_updates.get(pending_key) or {}
        if stop_loss is not None:
            pending["stopLoss"] = float(stop_loss)
            pending["updatedAt"] = time.time()
            _pending_protection_cache_updates[pending_key] = pending
        else:
            _pending_protection_cache_updates.pop(pending_key, None)
        _account_snapshots[normalized_user_id] = updated_snapshot
        return deepcopy(updated_snapshot)


def clear_cached_binance_account_snapshot(user_id: int) -> None:
    with _account_snapshot_lock:
        _account_snapshots.pop(int(user_id), None)
        for key in list(_pending_protection_cache_updates):
            if key[0] == int(user_id):
                _pending_protection_cache_updates.pop(key, None)
    with _account_refresh_request_lock:
        _requested_account_refreshes.discard(int(user_id))
        _forced_account_refreshes.discard(int(user_id))
        _last_account_refresh_request_at.pop(int(user_id), None)


def request_binance_account_refresh(user_id: int, *, force_rest: bool = False) -> bool:
    """Queue one account reconciliation after an order or account change.

    ``force_rest`` is retained for REST-mode callers and explicit recovery.
    WebSocket-mode callers reconcile protection orders through the authenticated
    WebSocket API instead of falling back to a full REST account read.
    """

    normalized_user_id = int(user_id)
    now = time.monotonic()
    with _account_refresh_request_lock:
        last_requested = _last_account_refresh_request_at.get(normalized_user_id, 0.0)
        if force_rest:
            _forced_account_refreshes.add(normalized_user_id)
            if normalized_user_id in _requested_account_refreshes:
                return True
            _requested_account_refreshes.add(normalized_user_id)
            _last_account_refresh_request_at[normalized_user_id] = now
            return True
        if normalized_user_id in _requested_account_refreshes or now - last_requested < ACCOUNT_EVENT_RECONCILIATION_COOLDOWN_SECONDS:
            return False
        _requested_account_refreshes.add(normalized_user_id)
        _last_account_refresh_request_at[normalized_user_id] = now
        return True


def _websocket_account_line_is_healthy(user_id: int) -> bool:
    """Avoid REST while the selected authenticated socket is actually live."""

    try:
        from .binance_websocket_worker import is_account_line_healthy
    except ImportError:  # pragma: no cover - direct module execution compatibility
        try:
            from binance_websocket_worker import is_account_line_healthy
        except ImportError:
            return False
    try:
        return bool(is_account_line_healthy(int(user_id)))
    except Exception:
        return False


def _wake_websocket_account_line(user_id: int) -> bool:
    try:
        from .binance_websocket_worker import request_binance_websocket_account_refresh
    except ImportError:  # pragma: no cover - direct module execution compatibility
        try:
            from binance_websocket_worker import request_binance_websocket_account_refresh
        except ImportError:
            return False
    try:
        return bool(request_binance_websocket_account_refresh(int(user_id)))
    except Exception:
        return False


def refresh_requested_binance_accounts_once() -> int:
    """Perform explicitly queued REST account reconciliations."""

    with _account_refresh_request_lock:
        user_ids = list(_requested_account_refreshes)
        _requested_account_refreshes.clear()
        forced_user_ids = {
            user_id for user_id in user_ids if user_id in _forced_account_refreshes
        }
        for user_id in forced_user_ids:
            _forced_account_refreshes.discard(user_id)
    refreshed = 0
    for user_id in user_ids:
        try:
            connection_settings = db.get_user_binance_connection_settings(user_id)
            websocket_mode = str(connection_settings.get("connectionMode") or "REST").strip().upper() == "WEBSOCKET"
            if websocket_mode:
                # WebSocket mode keeps balances on its authenticated line;
                # protection-order reads use their REST-only endpoints.
                _wake_websocket_account_line(user_id)
            if websocket_mode and user_id not in forced_user_ids:
                credentials = db.get_user_binance_credentials(user_id, include_secrets=True)
                if credentials.get("configured") and credentials.get("apiKey") and credentials.get("apiSecret"):
                    _refresh_one_account_protection(
                        {
                            "userId": user_id,
                            "network": credentials["network"],
                            "apiKey": credentials["apiKey"],
                            "apiSecret": credentials["apiSecret"],
                        }
                    )
                continue
            credentials = db.get_user_binance_credentials(user_id, include_secrets=True)
            if credentials.get("configured") and _refresh_one_account(
                {
                    "userId": user_id,
                    "network": credentials["network"],
                    "apiKey": credentials["apiKey"],
                    "apiSecret": credentials["apiSecret"],
                }
            ):
                refreshed += 1
        except Exception:
            logger.warning("Binance requested account reconciliation failed for user %s", user_id, exc_info=True)
    return refreshed


def _refresh_one_account(credentials: dict) -> bool:
    user_id = int(credentials["userId"])
    try:
        account = get_binance_account_snapshot(
            credentials["network"],
            credentials["apiKey"],
            credentials["apiSecret"],
        )
        profile = db.get_user_binance_credentials(user_id, include_secrets=False)
        spot_unavailable = _spot_payload_unavailable(account)
        futures_unavailable = _futures_payload_unavailable(account)
        if spot_unavailable or futures_unavailable:
            previous = get_cached_binance_account_snapshot(user_id)
            previous_account = previous.get("account") if previous else None
            futures = account.get("futures") if isinstance(account, dict) else {}
            merged_account = deepcopy(account)
            if spot_unavailable and isinstance(previous_account, dict):
                for field in ("balances", "canTrade", "accountType", "permissions", "updateTime"):
                    if field in previous_account:
                        merged_account[field] = deepcopy(previous_account[field])
                merged_account["spotAvailable"] = False
            if futures_unavailable and isinstance(previous_account, dict) and isinstance(previous_account.get("futures"), dict):
                merged_account["futures"] = deepcopy(previous_account["futures"])
                merged_account["futures"]["stale"] = True
                merged_account["futures"]["error"] = str(futures.get("error") or "合约账户暂时不可用")
            errors = []
            if spot_unavailable:
                errors.append(str(account.get("spotError") or "现货账户暂时不可用"))
            if futures_unavailable:
                errors.append(str(futures.get("error") or "合约账户暂时不可用"))
            cache_binance_account_snapshot(
                user_id,
                merged_account,
                profile,
                stale=True,
                error="；".join(errors),
                transport="REST",
                connection={"mode": "REST", "status": "ERROR", "error": ";".join(errors)},
            )
            return False
        cache_binance_account_snapshot(
            user_id,
            account,
            profile,
            transport="REST",
            connection={"mode": "REST", "status": "CONNECTED"},
            protection_reconciled=True,
        )
        return True
    except Exception as exc:
        previous = get_cached_binance_account_snapshot(user_id)
        profile = db.get_user_binance_credentials(user_id, include_secrets=False)
        cache_binance_account_snapshot(
            user_id,
            previous.get("account") if previous else None,
            profile,
            stale=True,
            error=str(exc) or "账户数据暂时不可用",
            transport="REST",
            connection={"mode": "REST", "status": "ERROR", "error": str(exc) or "账户数据暂时不可用"},
        )
        logger.warning("Binance account refresh failed for user %s", user_id, exc_info=True)
        return False


def _usable_futures_account_snapshot(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("stale"):
        return False
    account = snapshot.get("account")
    if not isinstance(account, dict):
        return False
    futures = account.get("futures")
    return isinstance(futures, dict) and not _futures_payload_unavailable(account) and isinstance(
        futures.get("positions"), list
    )


def _refresh_one_account_protection(credentials: dict) -> bool:
    """Reconcile REST-only protection orders without replacing WS balances."""

    user_id = int(credentials["userId"])
    snapshot = get_cached_binance_account_snapshot(user_id)
    if not _usable_futures_account_snapshot(snapshot):
        return False
    account = snapshot.get("account")
    futures = account.get("futures") if isinstance(account, dict) else None
    if not isinstance(account, dict) or not isinstance(futures, dict):
        return False
    try:
        with websocket_api_requests(False):
            protection = get_futures_protection_state(
                credentials["network"],
                credentials["apiKey"],
                credentials["apiSecret"],
                futures.get("positions") if isinstance(futures.get("positions"), list) else [],
            )
        if not isinstance(protection, dict) or protection.get("protectionState") != "CONFIRMED":
            return False
        updated_account = deepcopy(account)
        updated_futures = deepcopy(futures)
        updated_futures["positions"] = deepcopy(protection.get("positions") or [])
        for field in (
            "openOrderCount",
            "openStandardOrderCount",
            "openAlgoOrderCount",
            "protectionState",
            "protectionCheckedAt",
        ):
            if field in protection:
                updated_futures[field] = deepcopy(protection[field])
        updated_futures["protectionError"] = None
        updated_account["futures"] = updated_futures
        profile = db.get_user_binance_credentials(user_id, include_secrets=False)
        cache_binance_account_snapshot(
            user_id,
            updated_account,
            profile,
            transport=str(snapshot.get("transport") or "WEBSOCKET").upper(),
            connection=snapshot.get("connection") if isinstance(snapshot.get("connection"), dict) else None,
            protection_reconciled=True,
        )
        return True
    except Exception:
        # A failed order read must not turn the last known stop into a false
        # deletion.  The next reconciliation retries against the same cache.
        logger.warning("Binance protection reconciliation failed for user %s", user_id, exc_info=True)
        return False


def refresh_binance_accounts_once() -> int:
    """Keep each configured account warm through periodic REST requests."""

    credentials_list = []
    for item in db.list_user_binance_credentials_for_refresh():
        if str(item.get("connectionMode") or "REST").strip().upper() != "WEBSOCKET":
            credentials_list.append(item)
            continue
        user_id = item.get("userId")
        try:
            normalized_user_id = int(user_id)
        except (TypeError, ValueError):
            continue
        # The account WebSocket owns balances/positions. Protection-order
        # reads use the REST-only endpoint pair and do not refresh balances.
        _wake_websocket_account_line(normalized_user_id)
        if _websocket_account_line_is_healthy(normalized_user_id):
            _refresh_one_account_protection(item)
        continue
    if not credentials_list:
        return 0
    with ThreadPoolExecutor(
        max_workers=min(4, len(credentials_list)),
        thread_name_prefix="binance-account-refresh",
    ) as executor:
        return sum(executor.map(_refresh_one_account, credentials_list))
