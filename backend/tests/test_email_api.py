from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app as app_module
from backend.services import database as db
from backend.services import email as email_service


class TestEmailApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = os.environ.get("CHANLUN_DB_PATH")
        os.environ["CHANLUN_DB_PATH"] = str(Path(self.temp_dir.name) / "email-api.db")
        db.init_db()
        app_module.app.config.update(TESTING=True)
        app_module._test_email_last_sent_at.clear()
        self.client = app_module.app.test_client()

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("CHANLUN_DB_PATH", None)
        else:
            os.environ["CHANLUN_DB_PATH"] = self.old_db_path
        self.temp_dir.cleanup()

    def _headers(self) -> dict[str, str]:
        response = self.client.post(
            "/api/auth/register",
            json={"username": "email-user", "password": "secure-password"},
        )
        self.assertEqual(response.status_code, 201)
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    def test_test_email_requires_login(self):
        response = self.client.post("/api/notifications/test-email", json={"recipient": "user@example.com"})
        self.assertEqual(response.status_code, 401)

    def test_test_email_sends_only_through_backend_service(self):
        with patch.object(app_module, "send_resend_test_email", return_value="email_123") as send_email:
            response = self.client.post(
                "/api/notifications/test-email",
                json={"recipient": "user@example.com"},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"sent": True, "messageId": "email_123"})
        send_email.assert_called_once_with("user@example.com")

    def test_test_email_rejects_missing_recipient(self):
        with patch.object(app_module, "send_resend_test_email", side_effect=ValueError("请输入有效的测试收件邮箱。")) as send_email:
            response = self.client.post(
                "/api/notifications/test-email",
                json={"recipient": ""},
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 400)
        send_email.assert_called_once_with("")

    def test_test_email_uses_the_saved_shared_notification_email(self):
        headers = self._headers()
        # The test client uses a bearer token, so resolve the user through the
        # same request path as the endpoint instead of relying on a global.
        with self.client.application.test_request_context(headers=headers):
            user, _ = app_module._current_user_from_request()
        db.save_user_notification_settings(user["id"], {"email": "shared@example.com"})
        with patch.object(app_module, "send_resend_test_email", return_value="email_456") as send_email:
            response = self.client.post("/api/notifications/test-email", json={}, headers=headers)

        self.assertEqual(response.status_code, 200)
        send_email.assert_called_once_with("shared@example.com")

    def test_smart_money_alert_subject_describes_trader_contract_and_change(self):
        with patch.object(email_service, "_send_resend_email", return_value="email_subject") as send_email:
            message_id = email_service.send_resend_smart_money_alert(
                "user@example.com",
                [{
                    "subject": "Trader：BTCUSDT[LONG] -50.00%",
                    "message": "Trader仓位：BTCUSDT:LONG 2 -> 1（-50.00%）",
                }],
            )

        self.assertEqual(message_id, "email_subject")
        self.assertEqual(send_email.call_args.kwargs["subject"], "Trader：BTCUSDT[LONG] -50.00%")
        self.assertNotIn("聪明钱数据出现变化", send_email.call_args.kwargs["text"])

    def test_smart_money_alert_joins_multiple_changes_without_intro(self):
        with patch.object(email_service, "_send_resend_email", return_value="email_multiple") as send_email:
            email_service.send_resend_smart_money_alert(
                "user@example.com",
                [
                    {"subject": "Trader：BTCUSDT[LONG] -50.00%", "message": "Trader仓位：BTCUSDT:LONG 2 -> 1（-50.00%）"},
                    {"subject": "Trader：ETHUSDT[SHORT] +66.67%", "message": "Trader仓位：ETHUSDT:SHORT 3 -> 5（+66.67%）"},
                ],
            )

        kwargs = send_email.call_args.kwargs
        self.assertEqual(kwargs["subject"], "Trader：BTCUSDT[LONG] -50.00%；Trader：ETHUSDT[SHORT] +66.67%")
        self.assertEqual(kwargs["text"], "Trader仓位：BTCUSDT:LONG 2 -> 1（-50.00%）\nTrader仓位：ETHUSDT:SHORT 3 -> 5（+66.67%）")

    def test_smart_money_alert_puts_value_and_auto_close_flags_at_subject_start(self):
        with patch.object(email_service, "_send_resend_email", return_value="email_flags") as send_email:
            email_service.send_resend_smart_money_alert(
                "user@example.com",
                [{
                    "subject": "Trader：BTCUSDT[LONG] -50.00%",
                    "message": "Trader仓位：BTCUSDT:LONG 2 -> 1",
                    "valueDeviation": True,
                    "autoClosed": True,
                }],
            )

        subject = send_email.call_args.kwargs["subject"]
        self.assertTrue(subject.startswith("价值偏差｜自动平仓｜"))
        self.assertIn("Trader：BTCUSDT[LONG] -50.00%", subject)

    def test_test_email_uses_the_smart_money_alert_template(self):
        with patch.object(email_service, "_send_resend_email", return_value="email_template") as send_email:
            message_id = email_service.send_resend_test_email("user@example.com")

        self.assertEqual(message_id, "email_template")
        kwargs = send_email.call_args.kwargs
        self.assertEqual(
            kwargs["subject"],
            "自动平仓｜测试聪明钱：BTCUSDT[LONG] +50.00%；测试聪明钱：ETHUSDT[SHORT] -33.33%",
        )
        self.assertIn("BTCUSDT:LONG 200 -> 300（+50.00%）", kwargs["text"])
        self.assertIn("ETHUSDT:SHORT 3 -> 2（-33.33%）", kwargs["text"])
        self.assertIn("已按 50.00% 平掉真实仓位 0.5/1", kwargs["text"])

    def test_email_dispatch_uses_resend_when_usage_has_capacity(self):
        with (
            patch.object(email_service, "_configured_sender", return_value=("resend-key", "sender@example.com")),
            patch.object(email_service, "_resend_usage_exhausted", return_value=False) as usage_check,
            patch.object(email_service, "_send_resend_request", return_value="resend-id") as resend_send,
            patch.object(email_service, "_send_brevo_email") as brevo_send,
        ):
            message_id = email_service._send_resend_email(
                recipient="user@example.com",
                subject="Subject",
                text="Text",
                html="<p>Text</p>",
            )

        self.assertEqual(message_id, "resend-id")
        usage_check.assert_called_once_with("resend-key")
        resend_send.assert_called_once()
        brevo_send.assert_not_called()

    def test_email_dispatch_uses_brevo_when_any_resend_usage_limit_is_exhausted(self):
        with (
            patch.object(email_service, "_configured_sender", return_value=("resend-key", "sender@example.com")),
            patch.object(email_service, "_resend_usage_exhausted", return_value=True),
            patch.object(email_service, "_send_resend_request") as resend_send,
            patch.object(email_service, "_send_brevo_email", return_value="brevo-id") as brevo_send,
        ):
            message_id = email_service._send_resend_email(
                recipient="user@example.com",
                subject="Subject",
                text="Text",
                html="<p>Text</p>",
            )

        self.assertEqual(message_id, "brevo-id")
        resend_send.assert_not_called()
        brevo_send.assert_called_once_with(
            recipient="user@example.com",
            subject="Subject",
            text="Text",
            html="<p>Text</p>",
        )
