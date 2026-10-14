import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from gph.config import Config
from gph.db import Database
from gph.supervisor import Supervisor
from gph.providers.vast import VastClient


class VastBalanceApiTests(unittest.TestCase):
    def test_current_user_balance_uses_read_only_endpoint(self):
        client = VastClient("fake-token")
        calls = []
        client._request = lambda method, path, **kwargs: calls.append((method, path)) or {
            "id": 7, "username": "sample-account", "balance": 1.2345
        }
        user = client.get_current_user()
        self.assertEqual(calls, [("GET", "/users/current/")])
        self.assertEqual(user["balance"], 1.2345)
        self.assertEqual(user["username"], "sample-account")

    def test_current_user_balance_unwraps_user_payload(self):
        client = VastClient("fake-token")
        client._request = lambda method, path, **kwargs: {"user": {"id": 7, "username": "sample-account", "balance": 3.0}}
        self.assertEqual(client.get_current_user()["balance"], 3.0)

    def test_manual_balance_refresh_returns_before_provider_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "vast-async.sqlite3")
            cfg = Config(database_path=path, vast_api_key="fake-token")
            db = Database(path)
            supervisor = Supervisor(cfg, db)
            provider_entered = threading.Event()
            release_provider = threading.Event()

            timeouts = []
            class SlowVast:
                def __init__(self, api_key, base_url, timeout=20):
                    timeouts.append(timeout)
                    self.timeout = timeout

                def get_current_user(self):
                    provider_entered.set()
                    release_provider.wait(2)
                    return {"username": "sample-account", "balance": 4.25}

            with patch("gph.supervisor.VastClient", SlowVast):
                started = supervisor.request_vast_balance_refresh()
                self.assertTrue(started["refreshing"])
                self.assertTrue(provider_entered.wait(1))
                self.assertEqual(timeouts, [6])
                release_provider.set()
                deadline = time.monotonic() + 2
                status = supervisor.vast_balance_refresh_state()
                while status.get("refreshing") and time.monotonic() < deadline:
                    time.sleep(0.01)
                    status = supervisor.vast_balance_refresh_state()

            self.assertFalse(status["refreshing"])
            self.assertTrue(status["available"])
            self.assertEqual(status["balance_usd"], 4.25)

    def test_supervisor_persists_live_balance_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "vast-balance.sqlite3")
            cfg = Config(database_path=db_path, vast_api_key="fake-token")
            db = Database(db_path)
            supervisor = Supervisor(cfg, db)

            class FakeVast:
                def __init__(self, api_key, base_url, timeout=20):
                    self.api_key = api_key

                def get_current_user(self):
                    return {"username": "sample-account", "balance": 2.75}

            with patch("gph.supervisor.VastClient", FakeVast):
                live = supervisor.vast_balance_status(force=True)
                cached = supervisor.vast_balance_status(force=False)

            self.assertTrue(live["available"])
            self.assertEqual(live["balance_usd"], 2.75)
            self.assertEqual(live["account_username"], "sample-account")
            self.assertEqual(cached["balance_usd"], 2.75)
            self.assertIsNotNone(cached["last_success_at"])


if __name__ == "__main__":
    unittest.main()
