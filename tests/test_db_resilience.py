import tempfile
import unittest
from pathlib import Path

from gph.db import Database
from gph.config import Config
from gph.supervisor import Supervisor


class _FakeRawConnection:
    def __init__(self):
        self.rollback_calls = 0
        self.commit_calls = 0

    def rollback(self):
        self.rollback_calls += 1

    def commit(self):
        self.commit_calls += 1


class _FakePoolConnectionContext:
    def __init__(self, raw, owner):
        self.raw = raw
        self.owner = owner

    def __enter__(self):
        return self.raw

    def __exit__(self, exc_type, exc, tb):
        self.owner.exit_exception = exc_type
        # Mimic psycopg_pool's context ownership. The Database must not issue an
        # additional commit/rollback before this handler sees the exception.
        return False


class _FakePool:
    def __init__(self, raw):
        self.raw = raw
        self.exit_exception = None

    def connection(self, timeout=3):
        return _FakePoolConnectionContext(self.raw, self)


class DatabaseResilienceTests(unittest.TestCase):
    def test_pooled_session_does_not_double_rollback_broken_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "unused.sqlite"))
            db.is_postgres = True
            raw = _FakeRawConnection()
            pool = _FakePool(raw)
            db._pool = pool
            with self.assertRaisesRegex(RuntimeError, "original query error"):
                with db.session():
                    raise RuntimeError("original query error")
            self.assertEqual(raw.rollback_calls, 0)
            self.assertEqual(raw.commit_calls, 0)
            self.assertIs(pool.exit_exception, RuntimeError)

    def test_dashboard_bundle_loads_state_in_a_compact_read_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "dashboard.sqlite"))
            db.set_setting("last_scan_at", "2026-10-14T09:00:00Z")
            db.set_setting("target_active_instances", 2)
            db.replace_opportunities([
                {"id": "low", "scanned_at": "2026-10-14T09:00:00Z", "offer_id": 1, "coin_tag": "A", "expected_net_day_usd": 0.10},
                {"id": "high", "scanned_at": "2026-10-14T09:01:00Z", "offer_id": 2, "coin_tag": "B", "expected_net_day_usd": 1.10},
            ])
            db.log("INFO", "dashboard_bundle_test", {"ok": True})
            bundle = db.get_dashboard_bundle(opportunity_limit=1, event_limit=5)
            self.assertEqual(bundle["settings"]["target_active_instances"], 2)
            self.assertEqual([x["id"] for x in bundle["opportunities"]], ["high"])
            self.assertEqual(bundle["events"][0]["event"], "dashboard_bundle_test")
            self.assertEqual(bundle["managed_instances"], [])

    def test_kryptex_dashboard_config_is_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kryptex-config.sqlite")
            cfg = Config(database_path=db_path, kryptex_mining_username="")
            db = Database(db_path)
            supervisor = Supervisor(cfg, db)
            result = supervisor.configure_kryptex("wallet_public_123456", "prl", "gpu-worker-1")
            saved = db.get_setting("kryptex_connection_config", {})
            self.assertTrue(result["configured"])
            self.assertEqual(cfg.kryptex_mining_username, "wallet_public_123456")
            self.assertEqual(saved["coin"], "prl")
            self.assertEqual(saved["worker"], "gpu-worker-1")
            self.assertNotIn("wallet_public_123456", str(result))

    def test_kryptex_configuration_rejects_secret_like_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "kryptex-invalid.sqlite")
            supervisor = Supervisor(Config(database_path=db_path), Database(db_path))
            with self.assertRaises(ValueError):
                supervisor.configure_kryptex("", "prl")
            with self.assertRaises(ValueError):
                supervisor.configure_kryptex("seed phrase with spaces", "prl")
            with self.assertRaises(ValueError):
                supervisor.configure_kryptex("valid_public_wallet_123", "bad coin!")


if __name__ == "__main__":
    unittest.main()
