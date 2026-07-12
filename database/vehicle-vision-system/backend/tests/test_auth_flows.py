import os
import sys
import tempfile
import unittest
from pathlib import Path


TEST_DB = Path(tempfile.gettempdir()) / "vision_car_auth_test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from app.database import engine, init_db
from app.routers.auth import router as auth_router


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

    def test_password_registration_login_and_me(self):
        registered = self.client.post(
            "/api/auth/register",
            json={"username": "password_user", "password": "safe-password-123", "email": "password@example.com"},
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

    def test_email_verification_code_login(self):
        sent = self.client.post("/api/auth/send-code", json={"target": "code@example.com", "target_type": "email"})
        self.assertEqual(sent.status_code, 200)
        self.assertRegex(sent.json()["code"], r"^\d{6}$")

        logged_in = self.client.post(
            "/api/auth/login-code",
            json={"target": "code@example.com", "target_type": "email", "code": sent.json()["code"]},
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertIn("access_token", logged_in.json())

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
