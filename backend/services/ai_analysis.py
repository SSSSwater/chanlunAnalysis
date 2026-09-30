from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any

from openai import OpenAI


DEFAULT_MODEL = "gpt-4.1-mini"
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)


def analyze_with_ai(
    *,
    result: dict,
    intraday: dict | None,
    target_type: str,
    base_url: str,
    api_key: str,
    model: str | None = None,
) -> dict:
    if not api_key:
        raise ValueError("缺少 OpenAI API Key")
    if not base_url:
        raise ValueError("缺少 OpenAI Base URL")

    payload = _build_payload(result, intraday, target_type)
    client = OpenAI(api_key=api_key, base_url=_normalize_base_url(base_url), timeout=180)
    request = {
        "model": model or DEFAULT_MODEL,
        "input": _build_prompt(payload),
        "temperature": 0.2,
    }

    try:
        response = client.responses.create(
            **request,
            tools=[{"type": "web_search_preview"}],
        )
    except Exception as exc:
        message = str(exc)
        if not _is_tool_unsupported_error(message):
            raise
        response = client.responses.create(**request)

    text = (getattr(response, "output_text", "") or _extract_response_text(response)).strip()
    if not text:
        raise RuntimeError("模型未返回可显示的分析内容")
    return {
        "targetKey": payload["targetKey"],
        "symbol": payload["symbol"],
        "name": payload["name"],
        "targetType": target_type,
        "model": request["model"],
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "marketActionDate": payload["marketActionDate"],
        "marketStatus": payload["marketStatus"],
        "analysis": text,
        "inputSnapshot": payload,
    }


def _build_payload(result: dict, intraday: dict | None, target_type: str) -> dict:
    symbol = result.get("symbol") or ("000001" if target_type == "index" else "")
    name = result.get("name") or ("上证指数" if target_type == "index" else "")
    now = datetime.now()
    trading_time = MARKET_OPEN <= now.time() <= MARKET_CLOSE and now.weekday() < 5
    action_date = now.date().isoformat() if trading_time else _next_trade_day(now).date().isoformat()

    return {
        "targetKey": f"{target_type}:{symbol}",
        "targetType": target_type,
        "symbol": symbol,
        "name": name,
        "generatedAt": now.isoformat(timespec="seconds"),
        "marketStatus": "trading" if trading_time else "closed",
        "marketActionDate": action_date,
        "dateRange": result.get("dateRange"),
        "summary": result.get("summary"),
        "latestDailyKline": _last_items(result.get("rawKlines", []), 1),
        "recentDailyKlines": _last_items(result.get("rawKlines", []), 35),
        "priceAction": _compact_price_action(result.get("priceAction") or {}),
        "intraday": _compact_intraday(intraday or result.get("intraday") or {}),
    }


def _normalize_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if not text:
        raise ValueError("缺少 OpenAI Base URL")
    if text in {"https://api.openai.com", "http://api.openai.com", "api.openai.com"}:
        return "https://api.openai.com/v1"
    if text.startswith(("http://", "https://")):
        return text
    return f"https://{text}"


def _is_tool_unsupported_error(message: str) -> bool:
    text = message.lower()
    tool_markers = ["web_search", "web search", "tool", "tools"]
    unsupported_markers = ["unsupported", "unknown", "invalid", "not supported", "unrecognized"]
    return any(item in text for item in tool_markers) and any(item in text for item in unsupported_markers)


def _build_prompt(payload: dict) -> str:
    return (
        "你是严格的A股技术面分析助手。请结合我提供的行情技术面数据，并联网搜索最新市场背景、"
        "指数环境、公司或指数相关新闻后给出中文建议。不要编造未搜索到的消息。"
        "你必须只输出一个合法JSON对象，不要使用Markdown代码块，不要添加JSON之外的文字。"
        "JSON格式必须严格如下："
        "{"
        "\"longTerm\":{\"stance\":\"看多/震荡/看空/不确定\",\"summary\":\"长期走势判断\",\"evidence\":[\"依据1\",\"依据2\"]},"
        "\"shortTerm\":{\"stance\":\"看多/震荡/看空/不确定\",\"summary\":\"短期走势判断\",\"evidence\":[\"依据1\",\"依据2\"]},"
        "\"levels\":{\"actionDate\":\"YYYY-MM-DD\",\"buyWatch\":\"买入触发价、上限和条件\",\"riskStop\":\"结构失效/止损价和条件\",\"sellWatch\":\"减仓、卖出或磁铁目标价和条件\"},"
        "\"risks\":[\"风险1\",\"风险2\",\"风险3\"],"
        "\"conclusion\":\"一句话结论\","
        "\"publicInfo\":[\"联网搜索到的公开信息要点；如无可靠信息则写未检索到可靠新增信息\"]"
        "}。"
        "要求：按照环境、位置、结构、确认、失效条件的顺序解释；明确区分已确认、待确认、失败、需复核和等待。"
        "只做技术面和公开信息辅助，不承诺收益；如果数据不足要明确写出；不得以模型意见覆盖账户风险或纪律闸门。"
        "字段必须完整，数组至少保留空数组，不要改字段名。\n\n"
        f"当前交易状态: {payload['marketStatus']}，操作日期参考: {payload['marketActionDate']}。\n"
        "行情技术面数据JSON如下：\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _compact_intraday(intraday: dict) -> dict:
    periods = {}
    for period, item in (intraday.get("periods") or {}).items():
        periods[period] = {
            "summary": item.get("summary"),
            "latestKline": _last_items(item.get("rawKlines", []), 1),
            "recentKlines": _last_items(item.get("rawKlines", []), 45),
            "priceAction": _compact_price_action(item.get("priceAction") or {}),
        }
    return {
        "summary": intraday.get("summary"),
        "errors": intraday.get("errors", {}),
        "periods": periods,
    }


def _last_items(items: list[dict] | None, count: int) -> list[dict]:
    if not items:
        return []
    return items[-count:]


def _compact_price_action(price_action: dict) -> dict:
    return {
        "version": price_action.get("version"),
        "timeframe": price_action.get("timeframe"),
        "environment": price_action.get("environment"),
        "metrics": price_action.get("metrics"),
        "levels": _last_items(price_action.get("levels", []), 12),
        "setups": _last_items(price_action.get("setups", []), 12),
        "signals": _last_items(price_action.get("signals", []), 8),
        "assessment": price_action.get("assessment"),
    }


def _next_trade_day(value: datetime) -> datetime:
    candidate = value
    while True:
        candidate = candidate.replace(hour=9, minute=30, second=0, microsecond=0)
        if value.time() >= MARKET_CLOSE or value.weekday() >= 5:
            from datetime import timedelta

            candidate = candidate + timedelta(days=1)
        if candidate.weekday() < 5:
            return candidate
        value = candidate


def _extract_response_text(response: Any) -> str:
    try:
        data = response.model_dump()
    except Exception:
        return str(response)
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                parts.append(text)
    return "\n".join(parts)
