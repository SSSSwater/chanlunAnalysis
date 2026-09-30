"""Read-only cache for the public Binance Copy Trading lead portfolio."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import logging
import os
from threading import RLock
import time
from typing import Any

try:
    from . import database as db
    from .binance_account_worker import get_cached_binance_account_snapshot, _usable_futures_account_snapshot
    from .binance_futures_client import close_futures_position_market
    from ..fetch_binance_copy_trading_history import (
        _cookie_from_environment,
        _request_payload,
        fetch_positions,
        fetch_history,
        fetch_profile,
        fetch_smart_money_subscriptions,
    )
    from .email import send_resend_smart_money_alert
except ImportError:
    from services import database as db
    from services.binance_account_worker import get_cached_binance_account_snapshot, _usable_futures_account_snapshot
    from services.binance_futures_client import close_futures_position_market
    from fetch_binance_copy_trading_history import _cookie_from_environment, _request_payload, fetch_positions, fetch_history, fetch_profile, fetch_smart_money_subscriptions
    from services.email import send_resend_smart_money_alert


DEFAULT_TOP_TRADER_ID = "5132388877263187456"
DEFAULT_PAGE_SIZE = 10
DEFAULT_POSITION_PAGE_SIZE = 20
DEFAULT_LOOKBACK_DAYS = 30
# Keep this public web request on the same local Xray route as the Binance
# REST client. BINANCE_REST_PROXY can still point to another proxy at deploy time.
DEFAULT_REST_PROXY = "http://127.0.0.1:10808"

_cache_lock = RLock()
_cache_by_user: dict[int | None, dict[str, Any]] = {}
_pending_notifications_by_user: dict[int, list[dict[str, Any]]] = {}
logger = logging.getLogger(__name__)


def _empty_cache(user_id: int | None = None) -> dict[str, Any]:
    return {
        "topTraderId": DEFAULT_TOP_TRADER_ID,
        "items": [],
        "total": 0,
        "positions": [],
        "positionsTotal": 0,
        "traderSnapshots": [],
        "followedPositionSnapshots": [],
        "subscriptions": [],
        "subscriptionsTotal": 0,
        "updatedAt": None,
        "stale": True,
        "error": "尚未配置 Smart Money 登录态" if user_id is not None else "正在读取聪明钱操作记录",
    }


def _configured_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def get_cached_binance_copy_trading_history(user_id: int | None = None) -> dict[str, Any]:
    with _cache_lock:
        key = int(user_id) if user_id is not None else None
        return deepcopy(_cache_by_user.get(key) or _empty_cache(user_id))


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _record_quantity(record: dict[str, Any], price: Decimal | None) -> Decimal | None:
    for key in ("qty", "executedQty", "origQty", "positionAmt", "baseQty", "executedQuantity"):
        quantity = _decimal_value(record.get(key))
        if quantity is not None and quantity > 0:
            return quantity
    quote_quantity = _decimal_value(record.get("quantity"))
    if quote_quantity is not None and quote_quantity > 0 and price and price > 0:
        return quote_quantity / price
    return None


def _merge_records_by_minute(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    weights: dict[tuple[int, str, str, str], Decimal] = {}
    weighted_prices: dict[tuple[int, str, str, str], Decimal] = {}
    quantities: dict[tuple[int, str, str, str], Decimal] = {}
    profits: dict[tuple[int, str, str, str], Decimal] = {}
    fees: dict[tuple[int, str, str, str], Decimal] = {}

    for record in records:
        timestamp = _record_timestamp(record)
        symbol = str(record.get("symbol") or "").upper()
        side = str(record.get("side") or "").upper()
        position_side = str(record.get("positionSide") or "BOTH").upper()
        key = (timestamp // 60_000, symbol, side, position_side)
        if key not in grouped:
            grouped[key] = dict(record)
            weights[key] = Decimal("0")
            weighted_prices[key] = Decimal("0")
            quantities[key] = Decimal("0")
            profits[key] = Decimal("0")
            fees[key] = Decimal("0")

        current = grouped[key]
        current_timestamp = _record_timestamp(current)
        if timestamp >= current_timestamp:
            current.update({"time": timestamp} if "time" in record else {"orderUpdateTime": timestamp})

        price = _decimal_value(
            record.get("price")
            or record.get("avgPrice")
            or record.get("averagePrice")
            or record.get("avgFillPrice")
        )
        quantity = _record_quantity(record, price)
        if quantity is not None:
            quantities[key] += quantity
            if price is not None:
                weighted_prices[key] += price * quantity
                weights[key] += quantity

        profit = _decimal_value(record.get("realizedProfit") or record.get("realizedPnl"))
        if profit is None:
            profit = _decimal_value(record.get("totalPnl"))
        if profit is not None:
            profits[key] += profit
        fee = _decimal_value(record.get("fee"))
        if fee is not None:
            fees[key] += fee

    merged = []
    for key, record in grouped.items():
        quantity = quantities[key]
        if quantity > 0:
            record["qty"] = float(quantity)
            if "executedQty" in record:
                record["executedQty"] = float(quantity)
        if weights[key] > 0:
            average_price = weighted_prices[key] / weights[key]
            record["price"] = float(average_price)
            record["avgPrice"] = float(average_price)
            record["tradeValue"] = float(average_price * quantity)
        if "realizedProfit" in record:
            record["realizedProfit"] = float(profits[key])
        if "totalPnl" in record:
            record["totalPnl"] = float(profits[key])
        if "fee" in record:
            record["fee"] = float(fees[key])
        merged.append(record)
    merged.sort(key=_record_timestamp, reverse=True)
    return merged


def _record_timestamp(record: dict[str, Any]) -> int:
    for key in ("time", "updateTime", "orderUpdateTime", "orderTime", "createTime", "opened"):
        try:
            value = int(record.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _response_rows(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "rows", "items", "records", "orders", "positions", "subscriptions", "subscriptionList"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    for key in ("data", "result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            rows = _response_rows({"data": nested})
            if rows:
                return rows
    return []


def _response_total(response: Any, fallback: int) -> int:
    if isinstance(response, dict):
        for key in ("total", "totalCount", "count"):
            try:
                return int(response.get(key) or fallback)
            except (TypeError, ValueError):
                pass
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        for key in ("total", "totalCount", "count"):
            try:
                return int(data.get(key) or fallback)
            except (TypeError, ValueError):
                pass
    return fallback


def _normalize_history_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    aliases = {
        "symbol": ("pair", "contract", "contractSymbol"),
        "side": ("direction", "orderSide", "positionDirection"),
        "time": ("updateTime", "orderUpdateTime", "orderTime", "createTime"),
        "price": ("avgPrice", "averagePrice", "avgFillPrice", "executedPrice"),
        "qty": ("executedQty", "origQty", "baseQty", "executedQuantity"),
        "realizedProfit": ("realizedPnl", "pnl"),
    }
    for target, candidates in aliases.items():
        if normalized.get(target) not in (None, ""):
            continue
        for candidate in candidates:
            if normalized.get(candidate) not in (None, ""):
                normalized[target] = normalized[candidate]
                break
    return normalized


def _normalize_subscription(record: dict[str, Any]) -> dict[str, Any] | None:
    normalized = dict(record)
    top_trader_id = ""
    for key in ("topTraderId", "traderId", "leaderId", "userId"):
        value = normalized.get(key)
        if value not in (None, ""):
            top_trader_id = str(value).strip()
            break
    if not top_trader_id:
        return None
    normalized["topTraderId"] = top_trader_id
    return normalized


def _position_quantity(position: dict[str, Any]) -> str:
    for key in ("amount", "positionAmt", "quantity", "qty", "size"):
        value = _decimal_value(position.get(key))
        if value is not None:
            return format(value.normalize(), "f")
    return ""


def _position_side(position: dict[str, Any]) -> str:
    for value in (position.get("positionSide"), position.get("side"), position.get("direction")):
        raw_side = str(value or "").strip().upper()
        if raw_side in {"SHORT", "SELL"}:
            return "SHORT"
        if raw_side in {"LONG", "BUY"}:
            return "LONG"
    quantity = _decimal_value(position.get("amount") or position.get("positionAmt") or position.get("quantity"))
    return "SHORT" if quantity is not None and quantity < 0 else "LONG"


def _position_quantity_map(positions: list[dict[str, Any]]) -> dict[str, str]:
    result = {}
    for position in positions:
        symbol = str(position.get("symbol") or position.get("pair") or position.get("contractSymbol") or "").strip().upper()
        side = _position_side(position)
        if symbol:
            result[f"{symbol}:{side}"] = _position_quantity(position)
    return result


def _position_record_map(positions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or position.get("pair") or position.get("contractSymbol") or "").strip().upper()
        if symbol:
            result[f"{symbol}:{_position_side(position)}"] = position
    return result


def _position_value(position: dict[str, Any] | None) -> Decimal | None:
    if not isinstance(position, dict):
        return None
    quantity = _decimal_value(_position_quantity(position))
    price = next(
        (_decimal_value(position.get(key)) for key in ("entryPrice", "avgPrice", "averagePrice", "markPrice") if _decimal_value(position.get(key)) is not None),
        None,
    )
    if quantity is None or price is None or quantity == 0 or price <= 0:
        return None
    return abs(quantity) * price


def _snapshot_source_margin(snapshot: dict[str, Any] | None) -> Decimal | None:
    if not isinstance(snapshot, dict):
        return None
    profile = snapshot.get("profile") if isinstance(snapshot.get("profile"), dict) else {}
    for value in (snapshot.get("sourceTotalMargin"), snapshot.get("umMarginBalance"), profile.get("umMarginBalance")):
        margin = _decimal_value(value)
        if margin is not None and margin > 0:
            return margin
    return None


def _account_margin(user_id: int) -> Decimal | None:
    try:
        snapshot = get_cached_binance_account_snapshot(int(user_id)) or {}
    except Exception:
        return None
    account = snapshot.get("account") if isinstance(snapshot, dict) else None
    futures = account.get("futures") if isinstance(account, dict) else None
    if not isinstance(futures, dict):
        return None
    for key in ("totalMarginBalance", "totalWalletBalance"):
        margin = _decimal_value(futures.get(key))
        if margin is not None and margin > 0:
            return margin
    return None


def _account_position_values(user_id: int) -> tuple[dict[str, Decimal], bool]:
    """Return reliable current futures notional values keyed by symbol/side."""
    try:
        snapshot = get_cached_binance_account_snapshot(int(user_id))
    except Exception:
        return {}, False
    if not _usable_futures_account_snapshot(snapshot):
        return {}, False
    account = snapshot.get("account")
    futures = account.get("futures") if isinstance(account, dict) else None
    positions = futures.get("positions") if isinstance(futures, dict) else None
    if not isinstance(positions, list):
        return {}, False
    values: dict[str, Decimal] = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quantity = _decimal_value(position.get("quantity") or position.get("positionAmt") or position.get("amount"))
        if quantity is None or quantity == 0:
            continue
        raw_side = str(position.get("positionSide") or "").strip().upper()
        if raw_side in {"LONG", "SHORT"}:
            side = raw_side
        else:
            raw_side = str(position.get("side") or position.get("direction") or "").strip().upper()
            if raw_side in {"BUY", "LONG"}:
                side = "LONG"
            elif raw_side in {"SELL", "SHORT"}:
                side = "SHORT"
            else:
                side = "LONG" if quantity > 0 else "SHORT"
        value = _decimal_value(position.get("notional") or position.get("positionValue") or position.get("positionNotional"))
        if value is None or value <= 0:
            value = _position_value(position)
        if value is not None and value > 0:
            values[f"{symbol}:{side}"] = abs(value)
    return values, True


def cleanup_binance_smart_money_follows_without_real_positions(user_id: int) -> list[dict[str, Any]]:
    """Remove follow rows whose live futures position no longer exists.

    The cached account snapshot is intentionally the only source here. A
    stale, unavailable, or structurally incomplete snapshot must never be
    treated as an empty account, otherwise a temporary Binance failure could
    delete every follow relationship.
    """
    try:
        snapshot = get_cached_binance_account_snapshot(int(user_id))
    except Exception:
        logger.warning("Unable to read cached Binance account for follow cleanup", exc_info=True)
        return []
    if not _usable_futures_account_snapshot(snapshot):
        return []
    account = snapshot.get("account")
    futures = account.get("futures") if isinstance(account, dict) else None
    positions = futures.get("positions") if isinstance(futures, dict) else None
    if not isinstance(positions, list):
        return []

    active_keys: set[tuple[str, str]] = set()
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or position.get("pair") or position.get("contractSymbol") or "").strip().upper()
        if not symbol:
            continue
        quantity = None
        for key in ("positionAmt", "amount", "quantity", "qty", "size"):
            quantity = _decimal_value(position.get(key))
            if quantity is not None:
                break
        if quantity is None or quantity == 0:
            continue
        raw_side = str(position.get("positionSide") or "").strip().upper()
        if raw_side in {"LONG", "SHORT"}:
            active_keys.add((symbol, raw_side))
            continue
        raw_side = str(position.get("side") or position.get("direction") or "").strip().upper()
        if raw_side in {"BUY", "LONG"}:
            active_keys.add((symbol, "LONG"))
        elif raw_side in {"SELL", "SHORT"}:
            active_keys.add((symbol, "SHORT"))
        else:
            active_keys.add((symbol, "LONG" if quantity > 0 else "SHORT"))

    try:
        follows = db.list_user_binance_smart_money_position_follows(int(user_id))
    except Exception:
        logger.warning("Unable to load Smart Money follows for cleanup", exc_info=True)
        return []
    removed: list[dict[str, Any]] = []
    for follow in follows:
        if not isinstance(follow, dict):
            continue
        symbol = str(follow.get("symbol") or "").strip().upper()
        side = str(follow.get("positionSide") or "").strip().upper()
        if not symbol or side not in {"LONG", "SHORT"} or (symbol, side) in active_keys:
            continue
        try:
            db.set_user_binance_smart_money_position_follow(
                int(user_id),
                top_trader_id=follow.get("topTraderId"),
                symbol=symbol,
                position_side=side,
                enabled=False,
            )
        except Exception:
            logger.warning("Unable to remove stale Smart Money follow for user %s", user_id, exc_info=True)
            continue
        removed.append(follow)
    return removed


def _copy_multiplier(user_id: int, trader_id: str) -> Decimal:
    try:
        settings = db.get_user_binance_copy_trading_settings(int(user_id)) or {}
        multipliers = settings.get("copyMultipliers") if isinstance(settings, dict) else {}
        value = multipliers.get(str(trader_id)) if isinstance(multipliers, dict) else None
        if value in (None, "") and str(settings.get("topTraderId") or "") == str(trader_id):
            value = settings.get("copyMultiplier")
        multiplier = _decimal_value(value)
        if multiplier is not None and Decimal("1") <= multiplier <= Decimal("10"):
            return multiplier
    except Exception:
        logger.warning("Unable to load Smart Money copy multiplier for user %s", user_id, exc_info=True)
    return Decimal("1")


def _recommended_position_value(
    position: dict[str, Any] | None,
    source_margin: Decimal | None,
    account_margin: Decimal | None,
    multiplier: Decimal,
) -> Decimal | None:
    if position is None and source_margin is not None and account_margin is not None and source_margin > 0 and account_margin > 0:
        return Decimal("0")
    position_value = _position_value(position)
    if position_value is None or source_margin is None or account_margin is None:
        return None
    if source_margin <= 0 or account_margin <= 0 or multiplier <= 0:
        return None
    return account_margin * (position_value / source_margin) * multiplier


def _format_recommended_value(value: Decimal | None) -> str:
    if value is None:
        return "--"
    if abs(value) < Decimal("0.01"):
        return format(value, ".8f").rstrip("0").rstrip(".") or "0"
    return format(value.quantize(Decimal("0.01")), ".2f")


def _position_quantity_change_percent(previous: object, current: object) -> str:
    """Return the relative position-size change rounded to two decimals."""
    old_quantity = _decimal_value(previous)
    new_quantity = _decimal_value(current)
    if old_quantity is None or new_quantity is None:
        return ""
    if old_quantity == 0:
        return "（+100.00%）" if new_quantity != 0 else ""
    percentage = ((abs(new_quantity) - abs(old_quantity)) / abs(old_quantity)) * Decimal("100")
    rounded = percentage.quantize(Decimal("0.01"))
    return f"（{'+' if rounded >= 0 else ''}{rounded}%）"


def _position_change_entries(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    if "仓位：" in str(previous.get("error") or "") or "仓位：" in str(current.get("error") or ""):
        return []
    old_positions = _position_quantity_map(previous.get("positions") or [])
    new_positions = _position_quantity_map(current.get("positions") or [])
    entries = []
    for key in sorted(set(old_positions) | set(new_positions)):
        old_value = old_positions.get(key, "0")
        new_value = new_positions.get(key, "0")
        if old_value == new_value:
            continue
        old_quantity = _decimal_value(old_value) or Decimal("0")
        new_quantity = _decimal_value(new_value) or Decimal("0")
        entries.append({
            "key": key,
            "old": old_value,
            "new": new_value,
            "oldQuantity": old_quantity,
            "newQuantity": new_quantity,
            "reduction": max(Decimal("0"), abs(old_quantity) - abs(new_quantity)),
            "oldPosition": _position_record_map(previous.get("positions") or []).get(key),
            "newPosition": _position_record_map(current.get("positions") or []).get(key),
        })
    return entries


def _format_message_quantity(value: object) -> str:
    quantity = _decimal_value(value)
    return format(quantity.normalize(), "f") if quantity is not None else str(value or "0")


def _position_change_subject(trader_name: str, key: str, old_value: object, new_value: object) -> str:
    symbol = str(key).split(":", 1)[0] or "未知合约"
    position_side = str(key).split(":", 1)[1] if ":" in str(key) else "LONG"
    old_quantity = _decimal_value(old_value) or Decimal("0")
    new_quantity = _decimal_value(new_value) or Decimal("0")
    if old_quantity == 0:
        percentage = Decimal("100.00") if new_quantity != 0 else Decimal("0.00")
    else:
        percentage = ((abs(new_quantity) - abs(old_quantity)) / abs(old_quantity) * Decimal("100")).quantize(Decimal("0.01"))
    change = f"+{percentage:.2f}%" if percentage >= 0 else f"{percentage:.2f}%"
    return f"{trader_name}：{symbol}[{position_side}] {change}"


def _close_followed_real_positions(
    user_id: int,
    previous: dict[str, Any],
    current: dict[str, Any],
    successful_close_keys: set[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], str]:
    """Mirror Smart Money position reductions onto explicitly followed live positions."""
    try:
        follows = db.list_user_binance_smart_money_position_follows(int(user_id))
        credentials = db.get_user_binance_credentials(int(user_id), include_secrets=True)
    except Exception:
        logger.warning("Unable to load real-position follow settings for user %s", user_id, exc_info=True)
        return {}
    if not credentials.get("configured"):
        return {}
    follow_keys = {
        (
            str(item.get("topTraderId") or "").strip(),
            f"{str(item.get('symbol') or '').strip().upper()}:{str(item.get('positionSide') or '').strip().upper()}",
        )
        for item in follows
        if isinstance(item, dict)
    }
    if not follow_keys:
        return {}
    old_by_trader = {
        str(item.get("topTraderId") or ""): item
        for item in previous.get("traderSnapshots") or []
        if isinstance(item, dict)
    }
    notes: dict[tuple[str, str], str] = {}
    for snapshot in current.get("traderSnapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        trader_id = str(snapshot.get("topTraderId") or "")
        old_snapshot = old_by_trader.get(trader_id)
        if old_snapshot is None:
            continue
        for entry in _position_change_entries(old_snapshot, snapshot):
            if entry["reduction"] <= 0:
                continue
            follow_key = (trader_id, entry["key"])
            if follow_key not in follow_keys or entry["oldQuantity"] == 0:
                continue
            symbol, position_side = entry["key"].split(":", 1)
            ratio = entry["reduction"] / abs(entry["oldQuantity"]) * Decimal("100")
            try:
                result = close_futures_position_market(
                    credentials.get("network", "mainnet"),
                    credentials.get("apiKey", ""),
                    credentials.get("apiSecret", ""),
                    symbol=symbol,
                    position_side=position_side,
                    quantity_ratio=float(ratio),
                )
                notes[follow_key] = (
                    f"已按 {ratio.quantize(Decimal('0.01'))}% 平掉真实仓位 "
                    f"{_format_message_quantity(result.get('quantity'))}/"
                    f"{_format_message_quantity(result.get('positionQuantity'))}（原先真实仓位数）"
                )
                if successful_close_keys is not None:
                    successful_close_keys.add(follow_key)
            except Exception as exc:
                logger.warning(
                    "Unable to mirror Smart Money reduction for user %s, %s %s",
                    user_id,
                    symbol,
                    position_side,
                    exc_info=True,
                )
                notes[follow_key] = f"真实仓位平仓失败：{str(exc).strip() or '未知错误'}"
    return notes


def _position_change_message(
    snapshot: dict[str, Any],
    subscription: dict[str, Any],
    old_snapshot: dict[str, Any] | None,
    real_close_notes: dict[tuple[str, str], str] | None = None,
    account_margin: Decimal | None = None,
    multiplier: Decimal = Decimal("1"),
    account_position_values: dict[str, Decimal] | None = None,
    account_positions_reliable: bool = False,
    successful_close_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    if old_snapshot is None:
        return None
    trader_id = str(snapshot.get("topTraderId") or "")
    trader_name = str(subscription.get("traderName") or subscription.get("accountName") or trader_id)
    details = []
    subjects = []
    value_deviation = False
    auto_closed = False
    for entry in _position_change_entries(old_snapshot, snapshot):
        subjects.append(_position_change_subject(trader_name, entry["key"], entry["old"], entry["new"]))
        detail = (
            f"{entry['key']} {entry['old']} -> {entry['new']}"
            f"{_position_quantity_change_percent(entry['old'], entry['new'])}"
        )
        old_recommended = _recommended_position_value(
            entry.get("oldPosition"), _snapshot_source_margin(old_snapshot), account_margin, multiplier
        )
        new_recommended = _recommended_position_value(
            entry.get("newPosition"), _snapshot_source_margin(snapshot), account_margin, multiplier
        )
        if old_recommended is not None or new_recommended is not None:
            detail = (
                f"{detail}，推荐仓位价值 {_format_recommended_value(old_recommended)}"
                f" -> {_format_recommended_value(new_recommended)} USDT"
            )
        if new_recommended is not None and account_positions_reliable:
            actual_value = (account_position_values or {}).get(entry["key"], Decimal("0"))
            if abs(new_recommended - actual_value) > Decimal("5"):
                value_deviation = True
        close_note = (real_close_notes or {}).get((trader_id, entry["key"]))
        if close_note:
            detail = f"{detail}，{close_note}"
        if successful_close_keys and (trader_id, entry["key"]) in successful_close_keys:
            auto_closed = True
        details.append(detail)
    if "仓位：" in str(old_snapshot.get("error") or "") or "仓位：" in str(snapshot.get("error") or ""):
        details = []
    if not details:
        return None
    return {
        "subject": "；".join(subjects),
        "message": "；".join(
            f"{trader_name}仓位：{detail}"
            for detail in details
        ),
        "valueDeviation": value_deviation,
        "autoClosed": auto_closed,
    }


def _notify_smart_money_changes(
    user_id: int,
    notification_email: str,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    if not previous.get("updatedAt"):
        return True
    successful_close_keys: set[tuple[str, str]] = set()
    close_notes = _close_followed_real_positions(int(user_id), previous, current, successful_close_keys)
    account_margin = _account_margin(int(user_id))
    account_position_values, account_positions_reliable = _account_position_values(int(user_id))
    if not notification_email:
        return True
    subscriptions = {
        str(item.get("topTraderId") or ""): item
        for item in current.get("subscriptions") or []
        if isinstance(item, dict)
    }
    old_by_trader = {
        str(item.get("topTraderId") or ""): item
        for item in previous.get("traderSnapshots") or []
        if isinstance(item, dict)
    }
    queued_changes = deepcopy(_pending_notifications_by_user.get(int(user_id), []))
    changes = []
    for snapshot in current.get("traderSnapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        change = _position_change_message(
            snapshot,
            subscriptions.get(str(snapshot.get("topTraderId") or ""), {}),
            old_by_trader.get(str(snapshot.get("topTraderId") or "")),
            close_notes,
            account_margin,
            _copy_multiplier(int(user_id), str(snapshot.get("topTraderId") or "")),
            account_position_values,
            account_positions_reliable,
            successful_close_keys,
        )
        if change:
            changes.append(change)
    changes = queued_changes + changes
    if not changes:
        return True
    try:
        send_resend_smart_money_alert(notification_email, changes)
    except Exception:
        _pending_notifications_by_user[int(user_id)] = changes
        logger.warning("Smart Money notification failed for user %s", user_id, exc_info=True)
        return True
    _pending_notifications_by_user.pop(int(user_id), None)
    return True


def _fetch_trader_snapshot(
    top_trader_id: str,
    *,
    page_size: int,
    position_page_size: int,
    lookback_days: int,
    cookie: str,
    proxy: str,
    referer: str,
    auth: dict[str, str],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "topTraderId": top_trader_id,
        "items": [],
        "total": 0,
        "positions": [],
        "positionsTotal": 0,
        "profile": None,
        "sourceTotalMargin": None,
        "error": None,
    }
    errors = []
    try:
        records = []
        total = 0
        payload = _request_payload(top_trader_id, page_size, None, None, lookback_days)
        for page in range(1, 21):
            response = fetch_history(
                {**payload, "page": page},
                cookie=cookie,
                proxy=proxy,
                referer=referer,
                auth=auth,
            )
            if not isinstance(response, dict) or response.get("success") is False:
                raise RuntimeError(str(response.get("message") or "Binance Smart Money 操作记录响应失败"))
            page_rows = [_normalize_history_record(item) for item in _response_rows(response)]
            records.extend(page_rows)
            total = _response_total(response, total or len(records))
            merged = _merge_records_by_minute(records)
            # The history endpoint does not reliably expose a total. Read only
            # enough pages to fill the configured latest-operation view.
            if len(merged) >= page_size or len(page_rows) < page_size:
                break
        records.sort(key=_record_timestamp, reverse=True)
        snapshot.update({"items": _merge_records_by_minute(records)[:page_size], "total": total or len(records)})
    except Exception as exc:
        errors.append(f"操作记录：{exc}")
    try:
        positions_response = fetch_positions(
            top_trader_id,
            rows=position_page_size,
            cookie=cookie,
            proxy=proxy,
            referer=referer,
            auth=auth,
        )
        if not isinstance(positions_response, dict) or positions_response.get("success") is False:
            raise RuntimeError(str(positions_response.get("message") or "Binance Smart Money 仓位响应失败"))
        snapshot["positions"] = _response_rows(positions_response)
        snapshot["positionsTotal"] = _response_total(positions_response, len(snapshot["positions"]))
    except Exception as exc:
        errors.append(f"仓位：{exc}")
    try:
        profile_response = fetch_profile(
            top_trader_id,
            cookie=cookie,
            proxy=proxy,
            referer=referer,
            auth=auth,
        )
        if not isinstance(profile_response, dict) or profile_response.get("success") is False:
            raise RuntimeError(str(profile_response.get("message") or "Binance Smart Money 账户响应失败"))
        profile_data = profile_response.get("data")
        if not isinstance(profile_data, dict):
            raise RuntimeError("Binance Smart Money 账户响应格式无效")
        margin = _decimal_value(profile_data.get("umMarginBalance"))
        if margin is None or margin < 0:
            raise RuntimeError("Binance Smart Money 账户缺少有效 umMarginBalance")
        snapshot["profile"] = profile_data
        snapshot["sourceTotalMargin"] = float(margin)
    except Exception as exc:
        errors.append(f"账户：{exc}")
    snapshot["stale"] = bool(errors)
    snapshot["error"] = "；".join(errors) if errors else None
    return snapshot


def _fetch_all_subscriptions(
    *,
    cookie: str,
    proxy: str,
    referer: str,
    auth: dict[str, str],
) -> tuple[list[dict[str, Any]], int]:
    """Load every page in Binance's subscribed Smart Money list."""

    subscriptions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 1
    total = 0
    while True:
        response = fetch_smart_money_subscriptions(
            page=page,
            rows=10,
            only_show_sharing_position=False,
            cookie=cookie,
            proxy=proxy,
            referer=referer,
            auth=auth,
        )
        if not isinstance(response, dict) or response.get("success") is False:
            raise RuntimeError(str(response.get("message") or "Binance Smart Money 关注列表响应失败"))
        rows = _response_rows(response)
        total = max(total, _response_total(response, len(rows)))
        new_items = 0
        for item in rows:
            normalized = _normalize_subscription(item)
            if normalized and normalized["topTraderId"] not in seen_ids:
                subscriptions.append(normalized)
                seen_ids.add(normalized["topTraderId"])
                new_items += 1
        if not rows or len(rows) < 10 or (total and len(subscriptions) >= total) or not new_items:
            break
        page += 1
    return subscriptions, max(total, len(subscriptions))


def project_binance_copy_trading_follows(copy_trading: dict[str, Any], follows: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach follow-only display data from already refreshed trader snapshots."""

    projected = deepcopy(copy_trading)
    snapshots_by_trader = {
        str(snapshot.get("topTraderId") or "").strip(): snapshot
        for snapshot in projected.get("traderSnapshots") or []
        if isinstance(snapshot, dict) and str(snapshot.get("topTraderId") or "").strip()
    }
    followed_trader_ids = list(dict.fromkeys(
        str(item.get("topTraderId") or "").strip()
        for item in follows
        if str(item.get("topTraderId") or "").strip()
    ))
    projected["follows"] = deepcopy(follows)
    projected["followedPositionSnapshots"] = [
        {
            "topTraderId": trader_id,
            "positions": list(snapshot.get("positions") or []),
            "sourceTotalMargin": snapshot.get("sourceTotalMargin"),
            "error": snapshot.get("error"),
        }
        for trader_id in followed_trader_ids
        if (snapshot := snapshots_by_trader.get(trader_id)) is not None
    ]
    return projected


def refresh_binance_copy_trading_history_once(user_id: int | None = None) -> dict[str, Any]:
    if user_id is not None:
        user_settings = db.get_user_binance_copy_trading_settings(int(user_id))
        top_trader_id = str(user_settings.get("topTraderId") or DEFAULT_TOP_TRADER_ID).strip()
    else:
        top_trader_id = DEFAULT_TOP_TRADER_ID
    page_size = _configured_int("BINANCE_COPY_TRADING_PAGE_SIZE", DEFAULT_PAGE_SIZE, 1, 10)
    position_page_size = _configured_int("BINANCE_SMART_MONEY_POSITION_PAGE_SIZE", DEFAULT_POSITION_PAGE_SIZE, 1, 100)
    lookback_days = _configured_int("BINANCE_COPY_TRADING_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS, 1, 90)
    proxy = str(os.environ.get("BINANCE_REST_PROXY", DEFAULT_REST_PROXY)).strip()
    referer = f"https://www.binance.com/zh-CN/smart-money/profile/{top_trader_id}"
    # A logged-in user's request must use only that user's encrypted settings.
    # Do not touch the legacy process-level capture-file fallback first: a stale
    # or non-ASCII path there must not prevent the database credentials from
    # being loaded.
    cookie = "" if user_id is not None else _cookie_from_environment()
    result = get_cached_binance_copy_trading_history(user_id)
    previous_result = deepcopy(result)
    errors = []
    now_ms = int(time.time() * 1000)
    result["topTraderId"] = top_trader_id
    auth: dict[str, str] = {}
    if user_id is not None:
        try:
            stored_auth = db.get_user_binance_smart_money_auth(int(user_id), include_secrets=True)
            if not stored_auth.get("configured"):
                result.update({"updatedAt": now_ms, "stale": True, "error": "请在 Binance 设置中保存 Smart Money 登录态"})
                with _cache_lock:
                    _cache_by_user[int(user_id)] = result
                    return deepcopy(result)
            auth = {key: value for key, value in stored_auth.items() if key not in {"configured", "updatedAt"}}
            cookie = auth.get("cookie", "")
            referer = auth.get("referer") or referer
        except Exception as exc:
            result.update({"updatedAt": now_ms, "stale": True, "error": f"登录态读取失败：{exc}"})
            with _cache_lock:
                _cache_by_user[int(user_id)] = result
                return deepcopy(result)
    try:
        subscriptions, subscriptions_total = _fetch_all_subscriptions(
            cookie=cookie,
            proxy=proxy,
            referer=referer,
            auth=auth,
        )
        result.update({"subscriptions": subscriptions, "subscriptionsTotal": subscriptions_total})
    except Exception as exc:
        errors.append(f"关注列表：{exc}")
    subscriptions = list(result.get("subscriptions") or [])
    if user_id is not None:
        try:
            db.ensure_user_binance_smart_money_trader_settings(
                int(user_id),
                [item.get("topTraderId") for item in subscriptions if isinstance(item, dict)],
            )
        except ValueError:
            # Test and recovery refreshes can use a cache-only user identity.
            pass
    trader_ids = list(dict.fromkeys(
        str(item.get("topTraderId") or "").strip()
        for item in subscriptions
        if str(item.get("topTraderId") or "").strip()
    ))
    trader_snapshots = [
        _fetch_trader_snapshot(
            trader_id,
            page_size=page_size,
            position_page_size=position_page_size,
            lookback_days=lookback_days,
            cookie=cookie,
            proxy=proxy,
            referer=referer,
            auth=auth,
        )
        for trader_id in trader_ids
    ]
    # A transient successful-but-empty history response must not erase the
    # previous operation baseline. Keep the last non-empty history for change
    # detection while still accepting the freshly fetched positions/profile.
    previous_snapshots = {
        str(item.get("topTraderId") or ""): item
        for item in previous_result.get("traderSnapshots") or []
        if isinstance(item, dict)
    }
    for snapshot in trader_snapshots:
        old_snapshot = previous_snapshots.get(str(snapshot.get("topTraderId") or ""))
        if old_snapshot and not snapshot.get("items") and old_snapshot.get("items"):
            snapshot["items"] = list(old_snapshot.get("items") or [])
    result["traderSnapshots"] = trader_snapshots
    selected_snapshot = next(
        (snapshot for snapshot in trader_snapshots if snapshot["topTraderId"] == top_trader_id),
        None,
    )
    if selected_snapshot is None:
        errors.append("当前聪明钱用户未返回快照")
    else:
        result.update({
            "items": selected_snapshot["items"],
            "total": selected_snapshot["total"],
            "positions": selected_snapshot["positions"],
            "positionsTotal": selected_snapshot["positionsTotal"],
            "profile": selected_snapshot["profile"],
            "umMarginBalance": selected_snapshot["sourceTotalMargin"],
            "sourceTotalMargin": selected_snapshot["sourceTotalMargin"],
        })
        if selected_snapshot["error"]:
            errors.append(str(selected_snapshot["error"]))
    if user_id is not None:
        cleanup_binance_smart_money_follows_without_real_positions(int(user_id))
        result = project_binance_copy_trading_follows(
            result,
            db.list_user_binance_smart_money_position_follows(int(user_id)),
        )
    result.update({"updatedAt": now_ms, "stale": bool(errors), "error": "；".join(errors) if errors else None})
    if user_id is not None:
        notification_email = ""
        try:
            notification_email = str(db.get_user_notification_settings(int(user_id)).get("email") or "").strip()
        except ValueError:
            # A mocked refresh can use a user that is absent from the local database.
            pass
        notification_delivered = _notify_smart_money_changes(
            int(user_id),
            notification_email,
            previous_result,
            result,
        )
        if not notification_delivered:
            logger.warning("Smart Money cache retained for user %s so the alert can be retried", user_id)
            return previous_result
    with _cache_lock:
        _cache_by_user[int(user_id) if user_id is not None else None] = result
        return deepcopy(result)


def refresh_binance_copy_trading_histories_once() -> None:
    """Refresh each configured user's Smart Money snapshot independently."""
    for item in db.list_user_binance_smart_money_auth():
        refresh_binance_copy_trading_history_once(int(item["userId"]))
