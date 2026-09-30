from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")
MINIMUM_COMMISSION = Decimal("0.30")
STOCK_CODE_PATTERN = re.compile(r"\d{6}")


def resolve_a_share_exchange(symbol: str) -> str:
    """Resolve the exchange from the first digit of an A-share stock code."""
    symbol_text = str(symbol or "").strip()
    code_match = STOCK_CODE_PATTERN.search(symbol_text)
    stock_code = code_match.group(0) if code_match else symbol_text
    return "SH" if stock_code[:1] in {"5", "6", "9"} else "SZ"


def estimate_a_share_trade_fee(action: str, symbol: str, amount: float | Decimal) -> dict:
    normalized_action = str(action or "").strip().upper()
    order_amount = max(Decimal("0"), Decimal(str(amount)))
    exchange = resolve_a_share_exchange(symbol)

    commission = max(_round_cent(order_amount * Decimal("0.0001")), MINIMUM_COMMISSION)
    stamp_duty = _round_cent(order_amount * Decimal("0.0005")) if normalized_action == "SELL" else Decimal("0")
    other_fees = Decimal("0") if exchange == "SH" else _round_cent(order_amount * Decimal("0.00001"))
    total = _round_cent(commission + stamp_duty + other_fees)

    return {
        "exchange": exchange,
        "commission": float(commission),
        "stampDuty": float(stamp_duty),
        "otherFees": float(other_fees),
        "total": float(total),
    }


def _round_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)
