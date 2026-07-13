import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_DB = Path(tempfile.gettempdir()) / "vision_car_auth_test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from app.database import engine, init_db
from app.routers.auth import router as auth_router
from app.services.auth_email import EmailDeliveryError, send_verification_email


init_db()
app = FastAPI()
app.include_router(auth_router)


class AuthenticationFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        engine.dispose()
        if TEST_DB.exists():
            TEST_DB.unlink()

    @patch("app.routers.auth.send_verification_email")
    def test_password_registration_login_and_me(self, send_email):
        sent = self.client.post(
            "/api/auth/send-code",
            json={"email": "password@example.com", "purpose": "register"},
        )
        self.assertEqual(sent.status_code, 200)
        self.assertNotIn("code", sent.json())
        registration_code = send_email.call_args.args[1]

        registered = self.client.post(
            "/api/auth/register",
            json={
                "username": "password_user",
                "password": "safe-password-123",
                "email": "password@example.com",
                "verification_code": registration_code,
            },
        )
        self.assertEqual(registered.status_code, 200)

        logged_in = self.client.post(
            "/api/auth/login",
            json={"username": "password_user", "password": "safe-password-123"},
        )
        self.assertEqual(logged_in.status_code, 200)
        token = logged_in.json()["access_token"]
        current_user = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(current_user.status_code, 200)
        self.assertEqual(current_user.json()["username"], "password_user")

    @patch("app.routers.auth.send_verification_email")
    def test_email_verification_code_login(self, send_email):
        register_code_response = self.client.post(
            "/api/auth/send-code",
            json={"email": "code@example.com", "purpose": "register"},
        )
        self.assertEqual(register_code_response.status_code, 200)
        register_code = send_email.call_args.args[1]
        registered = self.client.post(
            "/api/auth/register",
            json={
                "username": "code_user",
                "password": "safe-password-123",
                "email": "code@example.com",
                "verification_code": register_code,
            },
        )
        self.assertEqual(registered.status_code, 200)

        sent = self.client.post(
            "/api/auth/send-code",
            json={"email": "code@example.com", "purpose": "login"},
        )
        self.assertEqual(sent.status_code, 200)
        self.assertNotIn("code", sent.json())
        login_code = send_email.call_args.args[1]
        self.assertRegex(login_code, r"^\d{6}$")

        logged_in = self.client.post(
            "/api/auth/login-code",
            json={"email": "code@example.com", "code": login_code},
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertIn("access_token", logged_in.json())

        reused = self.client.post(
            "/api/auth/login-code",
            json={"email": "code@example.com", "code": login_code},
        )
        self.assertEqual(reused.status_code, 400)

    @patch("app.routers.auth.send_verification_email")
    def test_login_code_requires_a_registered_email(self, send_email):
        sent = self.client.post(
            "/api/auth/send-code",
            json={"email": "missing@example.com", "purpose": "login"},
        )
        self.assertEqual(sent.status_code, 404)
        send_email.assert_not_called()

    def test_login_page_contains_only_requested_login_tabs(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("密码登录", html)
        self.assertIn("验证码登录", html)
        self.assertIn("注册账号", html)
        self.assertNotIn("微信扫码", html)
        self.assertNotIn("每个注册账号都会进入自己的独立工作台", html)

    @patch("app.services.auth_email.smtplib.SMTP")
    def test_verification_email_uses_starttls_smtp(self, smtp):
        with (
            patch("app.services.auth_email.settings.smtp_host", "smtp.example.com"),
            patch("app.services.auth_email.settings.smtp_port", 587),
            patch("app.services.auth_email.settings.smtp_user", "sender@example.com"),
            patch("app.services.auth_email.settings.smtp_password", "mail-token"),
            patch("app.services.auth_email.settings.smtp_use_tls", True),
        ):
            send_verification_email("recipient@example.com", "123456", "register")

        connection = smtp.return_value.__enter__.return_value
        connection.starttls.assert_called_once_with()
        connection.login.assert_called_once_with("sender@example.com", "mail-token")
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com")
        self.assertIn("123456", message.get_content())

    def test_verification_email_requires_smtp_configuration(self):
        with (
            patch("app.services.auth_email.settings.smtp_host", ""),
            patch("app.services.auth_email.settings.smtp_user", ""),
            patch("app.services.auth_email.settings.smtp_password", ""),
        ):
            with self.assertRaises(EmailDeliveryError):
                send_verification_email("recipient@example.com", "123456", "login")

    def test_wechat_scan_confirmation_login(self):
        created = self.client.post("/api/auth/wechat/qrcode")
        self.assertEqual(created.status_code, 200)
        session = created.json()

        qr_image = self.client.get(session["qrcode_url"])
        self.assertEqual(qr_image.status_code, 200)
        self.assertEqual(qr_image.headers["content-type"], "image/png")
        self.assertEqual(self.client.get(session["poll_url"]).json()["status"], "pending")

        confirmed = self.client.post(f"/api/auth/wechat/confirm/{session['session_id']}")
        self.assertEqual(confirmed.status_code, 200)
        polled = self.client.get(session["poll_url"])
        self.assertEqual(polled.status_code, 200)
        self.assertEqual(polled.json()["status"], "confirmed")
        self.assertIn("access_token", polled.json())


if __name__ == "__main__":
    unittest.main()
