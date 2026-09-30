from __future__ import annotations

import json
import os
import re
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values


RESEND_EMAILS_URL = "https://api.resend.com/emails"
RESEND_USAGE_URL = "https://api.resend.com/usage"
BREVO_EMAILS_URL = "https://api.brevo.com/v3/smtp/email"
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# The backend's process environment may contain an old key from a scheduled
# task. For email specifically, make the documented project-local .env file
# authoritative while still allowing an environment-only deployment.
_LOCAL_EMAIL_ENV = dotenv_values(Path(__file__).resolve().parents[2] / ".env")


class EmailConfigurationError(ValueError):
    """The local email configuration is incomplete."""


class EmailDeliveryError(RuntimeError):
    """An email provider rejected the message or could not be reached."""


def _configured_sender() -> tuple[str, str]:
    api_key = str(_LOCAL_EMAIL_ENV.get("RESEND_API_KEY") or os.environ.get("RESEND_API_KEY") or "").strip()
    sender = str(_LOCAL_EMAIL_ENV.get("RESEND_FROM_EMAIL") or os.environ.get("RESEND_FROM_EMAIL") or "").strip()
    if not api_key:
        raise EmailConfigurationError("未配置 RESEND_API_KEY，请在项目根目录 .env 中填写 Resend API Key 后重启后端。")
    if not sender:
        raise EmailConfigurationError("未配置 RESEND_FROM_EMAIL，请在项目根目录 .env 中填写已验证的发件邮箱后重启后端。")
    return api_key, sender


def _configured_brevo() -> tuple[str, str]:
    api_key = str(_LOCAL_EMAIL_ENV.get("BREVO_API_KEY") or os.environ.get("BREVO_API_KEY") or "").strip()
    sender = str(_LOCAL_EMAIL_ENV.get("RESEND_FROM_EMAIL") or os.environ.get("RESEND_FROM_EMAIL") or "").strip()
    if not api_key:
        raise EmailConfigurationError("未配置 BREVO_API_KEY，请在项目根目录 .env 中填写 Brevo API Key 后重启后端。")
    if not sender:
        raise EmailConfigurationError("未配置 RESEND_FROM_EMAIL，请在项目根目录 .env 中填写已验证的发件邮箱后重启后端。")
    return api_key, sender


def _validated_recipient(value: object) -> str:
    recipient = str(value or "").strip()
    if not _EMAIL_PATTERN.fullmatch(recipient):
        raise ValueError("请输入有效的测试收件邮箱。")
    return recipient


def _resend_usage_exhausted(api_key: str) -> bool:
    request = Request(
        RESEND_USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "chanlun-analysis/1.0",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            usage = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise EmailDeliveryError(f"Resend 用量查询失败：{_resend_error_message(error)}") from error
    except (URLError, TimeoutError) as error:
        raise EmailDeliveryError("无法连接 Resend 查询用量，请检查网络后重试。") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmailDeliveryError("Resend 用量接口返回了无法识别的响应。") from error

    emails = usage.get("emails") if isinstance(usage, dict) else None
    if not isinstance(emails, dict):
        raise EmailDeliveryError("Resend 用量响应缺少 emails 字段。")
    for period in ("daily", "monthly"):
        values = emails.get(period)
        if not isinstance(values, dict):
            raise EmailDeliveryError(f"Resend 用量响应缺少 {period} 字段。")
        used = values.get("used")
        limit = values.get("limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and used >= limit:
            return True
    return False


def _send_resend_request(*, recipient: str, subject: str, text: str, html: str, api_key: str, sender: str) -> str:
    payload = {
        "from": sender,
        "to": [recipient],
        "subject": str(subject).strip()[:200],
        "html": html,
        "text": text,
    }
    request = Request(
        RESEND_EMAILS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "chanlun-analysis/1.0",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise EmailDeliveryError(f"Resend 发信失败：{_resend_error_message(error)}") from error
    except (URLError, TimeoutError) as error:
        raise EmailDeliveryError("无法连接 Resend，请检查网络后重试。") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmailDeliveryError("Resend 返回了无法识别的响应。") from error

    message_id = str(result.get("id") or "").strip()
    if not message_id:
        raise EmailDeliveryError("Resend 未返回邮件标识，请稍后重试。")
    return message_id


def _send_brevo_email(*, recipient: str, subject: str, text: str, html: str) -> str:
    api_key, sender = _configured_brevo()
    payload = {
        "sender": {"email": sender},
        "to": [{"email": recipient}],
        "subject": str(subject).strip()[:200],
        "htmlContent": html,
        "textContent": text,
    }
    request = Request(
        BREVO_EMAILS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "chanlun-analysis/1.0",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise EmailDeliveryError(f"Brevo 发信失败：{_brevo_error_message(error)}") from error
    except (URLError, TimeoutError) as error:
        raise EmailDeliveryError("无法连接 Brevo，请检查网络后重试。") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmailDeliveryError("Brevo 返回了无法识别的响应。") from error

    message_id = str(result.get("messageId") or "").strip()
    if not message_id:
        raise EmailDeliveryError("Brevo 未返回邮件标识，请稍后重试。")
    return message_id


def _brevo_error_message(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        message = str(payload.get("message") or payload.get("code") or "").strip()
        if message:
            return message
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        pass
    return f"Brevo 返回 HTTP {error.code}。"


def _send_resend_email(*, recipient: object, subject: str, text: str, html: str) -> str:
    to_email = _validated_recipient(recipient)
    resend_api_key, sender = _configured_sender()
    if _resend_usage_exhausted(resend_api_key):
        return _send_brevo_email(recipient=to_email, subject=subject, text=text, html=html)
    return _send_resend_request(
        recipient=to_email,
        subject=subject,
        text=text,
        html=html,
        api_key=resend_api_key,
        sender=sender,
    )


def _resend_error_message(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        message = str(payload.get("message") or "").strip()
        if message:
            details = []
            error_name = str(payload.get("name") or "").strip()
            status_code = str(payload.get("statusCode") or "").strip()
            if error_name:
                details.append(error_name)
            if status_code and status_code != str(error.code):
                details.append(f"code {status_code}")
            suffix = f"（{', '.join(details)}）" if details else ""
            return f"{message}{suffix}"
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        pass
    return f"Resend 返回 HTTP {error.code}。"


def send_resend_test_email(recipient: object) -> str:
    """Send the real Smart Money alert template with representative sample changes."""
    return send_resend_smart_money_alert(
        recipient,
        [
            {
                "subject": "测试聪明钱：BTCUSDT[LONG] +50.00%",
                "message": "测试聪明钱仓位：BTCUSDT:LONG 200 -> 300（+50.00%），已按 50.00% 平掉真实仓位 0.5/1（原先真实仓位数）",
                "autoClosed": True,
            },
            {
                "subject": "测试聪明钱：ETHUSDT[SHORT] -33.33%",
                "message": "测试聪明钱仓位：ETHUSDT:SHORT 3 -> 2（-33.33%），已按 33.33% 平掉真实仓位 1/3（原先真实仓位数）",
                "autoClosed": True,
            },
        ],
    )


def send_resend_smart_money_alert(recipient: object, changes: list[dict]) -> str:
    """Send a concise Smart Money change notification."""

    subjects = [str(change.get("subject") or "").strip() for change in changes if isinstance(change, dict)]
    subjects = [subject for subject in subjects if subject]
    prefixes = []
    if any(isinstance(change, dict) and change.get("valueDeviation") for change in changes):
        prefixes.append("价值偏差")
    if any(isinstance(change, dict) and change.get("autoClosed") for change in changes):
        prefixes.append("自动平仓")
    subject = "｜".join(prefixes + (["；".join(subjects)] if subjects else [])) or "聪明钱数据变化提醒"
    lines = []
    html_lines = []
    for change in changes:
        line = str(change.get("message") or "数据发生变化")
        lines.append(line)
        html_lines.append(f"<p>{escape(line)}</p>")
    return _send_resend_email(
        recipient=recipient,
        subject=subject[:200],
        text="\n".join(lines),
        html="".join(html_lines),
    )
