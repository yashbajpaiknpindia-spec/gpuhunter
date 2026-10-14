"""Focused integration tests for the user-facing login/session flow."""
import os
import tempfile
import unittest

# Isolate app import from any worker/market scanning; the tests must never initiate rentals.
_TEST_DIR = tempfile.TemporaryDirectory(prefix="gph-auth-test-")
os.environ["APP_HOST"] = "127.0.0.1"
os.environ["APP_USER"] = "test-operator"
os.environ["APP_PASSWORD"] = "test-password-123"
os.environ["APP_SECRET_KEY"] = "test-secret-key-not-for-production-0000000000"
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_PATH"] = os.path.join(_TEST_DIR.name, "auth.sqlite3")
os.environ["AUTO_SCAN_ENABLED"] = "false"
os.environ["AUTOMATION_MODE"] = "scan"
os.environ["ENABLE_LIVE_MINING"] = "false"
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_SERVICE_ID", None)

import app as webapp  # noqa: E402

webapp.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
webapp.get_usd_inr_rate = lambda **kwargs: {  # noqa: E731
    "available": False, "rate": None, "source": "test", "fetched_at": None,
    "source_updated_at": None, "age_hours": None, "stale": True, "error": "test fixture", "accounting_currency": "USD",
}


class LoginPageTests(unittest.TestCase):
    def setUp(self):
        self.client = webapp.app.test_client()

    def test_dashboard_redirects_to_explained_login_page_without_basic_auth_popup(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertNotIn("WWW-Authenticate", response.headers)
        login = self.client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b'name="username"', login.data)
        self.assertIn(b'name="password"', login.data)
        self.assertIn(b"test-operator", login.data)  # deployment username is shown directly
        self.assertIn(b'value="test-operator"', login.data)  # username starts pre-filled
        self.assertIn(b"APP_PASSWORD", login.data)

    def test_wrong_credentials_explain_where_to_check_environment(self):
        response = self.client.post("/login", data={"username": "wrong", "password": "nope"})
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Couldn't sign you in", response.data)
        self.assertIn(b"APP_USER", response.data)
        self.assertIn(b"APP_PASSWORD", response.data)
        self.assertNotIn(b"test-password-123", response.data)

    def test_valid_credentials_open_dashboard_and_protect_state_api(self):
        response = self.client.post("/login", data={"username": "test-operator", "password": "test-password-123"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        state = self.client.get("/api/state")
        self.assertEqual(state.status_code, 200)
        self.assertIn(b'"fx"', state.data)

    def test_same_origin_https_post_is_not_rejected_just_for_proxy_scheme(self):
        client = self.client
        client.post('/login', data={'username': 'test-operator', 'password': 'test-password-123'}, base_url='https://gph.example')
        response = client.post('/api/fx/refresh', json={}, base_url='https://gph.example', headers={'Origin': 'https://gph.example'})
        self.assertEqual(response.status_code, 200)

    def test_cross_origin_post_is_rejected(self):
        client = self.client
        client.post('/login', data={'username': 'test-operator', 'password': 'test-password-123'}, base_url='https://gph.example')
        response = client.post('/api/fx/refresh', json={}, base_url='https://gph.example', headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'Cross-origin request rejected', response.data)


    def test_dashboard_static_assets_are_non_empty_and_health_reports_them(self):
        for name, minimum_bytes in (("app.js", 1000), ("app.css", 1000), ("login.css", 1000)):
            response = self.client.get(f"/static/{name}?v=0.7.10")
            self.assertEqual(response.status_code, 200, name)
            self.assertGreater(len(response.data), minimum_bytes, name)
            self.assertIn("no-cache", response.headers.get("Cache-Control", ""))
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        payload = health.get_json()
        self.assertTrue(payload["assets_ok"])
        self.assertEqual(payload["version"], "0.7.10")

    def test_saved_ui_instance_target_can_exceed_legacy_environment_default(self):
        login = self.client.post("/login", data={"username": "test-operator", "password": "test-password-123"})
        self.assertEqual(login.status_code, 302)
        response = self.client.post("/api/automation-settings", json={"target_active_instances": 9, "auto_rent_enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["automation"]["target_active_instances"], 9)

    def test_saved_ui_target_rejects_values_above_absolute_ceiling(self):
        self.client.post("/login", data={"username": "test-operator", "password": "test-password-123"})
        response = self.client.post("/api/automation-settings", json={"target_active_instances": 26, "auto_rent_enabled": False})
        self.assertEqual(response.status_code, 400)
        self.assertIn("0 to 25", response.get_json()["error"])

    def test_logout_clears_session(self):
        self.client.post("/login", data={"username": "test-operator", "password": "test-password-123"})
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"expired", response.data)

    def test_health_remains_public_for_render_health_checks(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"version":"0.7.10"', response.data)


if __name__ == "__main__":
    unittest.main()
