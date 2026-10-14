"""Wallet / missing-key reporting regressions."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DIR = tempfile.TemporaryDirectory(prefix="gph-setup-test-")
# Same values as tests/test_auth.py: the app module is shared across the whole test process and
# reads these at request time, so they must never differ between test files.
for _k, _v in {
    "APP_HOST": "127.0.0.1", "APP_USER": "test-operator", "APP_PASSWORD": "test-password-123",
    "APP_SECRET_KEY": "test-secret-key-not-for-production-0000000000", "DATABASE_URL": "",
    "DATABASE_PATH": os.path.join(_TEST_DIR.name, "setup.sqlite3"), "AUTO_SCAN_ENABLED": "false",
    "AUTOMATION_MODE": "scan", "ENABLE_LIVE_MINING": "false",
}.items():
    os.environ.setdefault(_k, _v)
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_SERVICE_ID", None)

import app as webapp  # noqa: E402
from gph.config import Config  # noqa: E402
from gph.db import Database  # noqa: E402
from gph.profiles import wallet_file_status, wallet_for_profile  # noqa: E402
from gph.supervisor import Supervisor  # noqa: E402
from gph.providers.vast import VastClient  # noqa: E402

webapp.app.config.update(TESTING=True)


def _login(client):
    client.post("/login", data={"username": os.environ["APP_USER"], "password": os.environ["APP_PASSWORD"]})


class MissingKeyReportingTests(unittest.TestCase):
    def test_gaps_name_every_missing_key_and_its_impact(self):
        cfg = Config(vast_api_key="", whattomine_api_token="", kryptex_mining_username="")
        ids = {g["id"]: g for g in cfg.setup_gaps([])}
        self.assertEqual(ids["vast"]["where"], "VAST_API_KEY")
        self.assertEqual(ids["vast"]["severity"], "required")
        self.assertEqual(ids["whattomine"]["where"], "WHATTOMINE_API_TOKEN")
        self.assertEqual(ids["kryptex"]["severity"], "optional")
        self.assertIn("KRYPTEX_MINING_USERNAME", ids["kryptex"]["where"])

    def test_no_gaps_reported_for_configured_keys(self):
        cfg = Config(vast_api_key="k", whattomine_api_token="t", kryptex_mining_username="u", kryptex_coin="prl")
        ids = {g["id"] for g in cfg.setup_gaps([])}
        self.assertNotIn("vast", ids)
        self.assertNotIn("whattomine", ids)
        self.assertNotIn("kryptex", ids)

    def test_setup_endpoint_works_when_database_is_down(self):
        client = webapp.app.test_client()
        _login(client)
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=RuntimeError("db down")), \
                patch.object(webapp.config, "vast_api_key", ""), patch.object(webapp.config, "whattomine_api_token", ""), \
                patch.object(webapp, "get_usd_inr_rate", lambda **kw: {"available": False, "rate": None, "stale": True, "error": "test"}):
            with patch.object(webapp, "_STATE_CACHE", None), \
                    patch.dict(webapp._BUNDLE_CACHE, {"bundle": None, "at": 0.0}):   # first load: no snapshot to fall back on
                state = client.get("/api/state")
            self.assertEqual(state.status_code, 503)
            body = state.get_json()
            self.assertIn("RuntimeError", body["error"])          # real error type is shown
            self.assertTrue(any(g["id"] == "vast" for g in body["setup_gaps"]))
            with patch.object(webapp, "_STATE_CACHE", {"ok": True, "opportunities": []}), \
                    patch.dict(webapp._BUNDLE_CACHE, {"bundle": None, "at": 0.0}):   # stale snapshot path
                stale = client.get("/api/state")
            self.assertEqual(stale.status_code, 200)
            self.assertTrue(stale.get_json()["degraded"])
            self.assertTrue(any(g["id"] == "vast" for g in stale.get_json()["setup_gaps"]))
            setup = client.get("/api/setup/status")
            self.assertEqual(setup.status_code, 200)
            self.assertTrue(any(g["id"] == "whattomine" for g in setup.get_json()["setup_gaps"]))

    def test_setup_endpoint_requires_login(self):
        self.assertNotEqual(webapp.app.test_client().get("/api/setup/status").status_code, 200)


class WalletFileTests(unittest.TestCase):
    def _write(self, tmp, text):
        path = Path(tmp) / "wallets.json"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_bad_json_is_reported_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = wallet_file_status(self._write(tmp, '{"wallets": {"PRL": "addr",}}'))
            self.assertIn("not valid JSON", status["error"])
            cfg = Config(wallets_path=str(Path(tmp) / "wallets.json"))
            self.assertTrue(any(g["id"] == "wallet_file" for g in cfg.setup_gaps([])))

    def test_missing_file_is_reported(self):
        self.assertIn("not found", wallet_file_status("/nonexistent/wallets.json")["error"])

    def test_wallet_key_lookup_ignores_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({"wallets": {"prl": "  addr123  "}, "workers": {}}))
            self.assertEqual(wallet_for_profile({"wallet_key": "PRL"}, path), "addr123")
            self.assertEqual(wallet_for_profile({"wallet_key": "Prl"}, path), "addr123")

    def test_enabled_profile_without_wallet_is_called_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({"wallets": {"BTC": "x"}}))
            cfg = Config(wallets_path=path)
            gaps = cfg.setup_gaps([{"id": "p", "enabled": True, "wallet_key": "PRL"}])
            self.assertTrue(any(g["id"] == "profile_wallets" and "PRL" in g["label"] for g in gaps))


class VastBalanceFieldTests(unittest.TestCase):
    def _balance(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.sqlite3")
            sup = Supervisor(Config(database_path=path, vast_api_key="fake"), Database(path))

            class Fake:
                def __init__(self, *a, **k):
                    pass

                def get_current_user(self):
                    return payload

            with patch("gph.supervisor.VastClient", Fake):
                return sup.vast_balance_status(force=True)

    def test_credit_field_is_used_when_balance_is_absent(self):
        result = self._balance({"username": "u", "credit": 25.0})
        self.assertTrue(result["available"])
        self.assertEqual(result["balance_usd"], 25.0)

    def test_balance_field_still_preferred(self):
        self.assertEqual(self._balance({"balance": 3.5, "credit": 25.0})["balance_usd"], 3.5)

    def test_missing_both_fields_gives_clear_error(self):
        result = self._balance({"username": "u"})
        self.assertFalse(result["available"])
        self.assertIn("balance or credit", result["error"])

    def test_missing_key_message_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "n.sqlite3")
            sup = Supervisor(Config(database_path=path, vast_api_key=""), Database(path))
            self.assertIn("VAST_API_KEY", sup.vast_balance_status()["error"])


if __name__ == "__main__":
    unittest.main()
