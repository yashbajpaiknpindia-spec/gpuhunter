"""Regression coverage for market-scan timestamp initialization."""
import tempfile
import unittest
import threading
import time
from unittest.mock import patch
from pathlib import Path

from gph.config import Config
from gph.db import Database
from gph.engine import OpportunityScanner
from gph.supervisor import Supervisor


class FakeVast:
    def __init__(self, offers):
        self.offers = offers

    def search_offers(self, **kwargs):
        return list(self.offers)


class FakeWhatToMine:
    def get_gpus(self):
        return []

    def get_coins(self):
        return []


class ScanTimestampRegressionTests(unittest.TestCase):
    def test_target_defaults_to_one_but_preserves_explicit_scan_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "target.sqlite3")
            cfg = Config(database_path=path, max_active_instances=3)
            db = Database(path)
            supervisor = Supervisor(cfg, db)
            self.assertEqual(supervisor.target_instances(), 1)
            db.set_setting("target_active_instances", 0)
            self.assertEqual(supervisor.target_instances(), 0)
            db.set_setting("target_active_instances", 9)
            self.assertEqual(supervisor.target_instances(), 9)  # explicit UI target can exceed legacy env default
            db.set_setting("target_active_instances", 99)
            self.assertEqual(supervisor.target_instances(), 25)  # absolute app ceiling

    def test_async_scan_acknowledgement_does_not_wait_for_database_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "async-scan.sqlite3")
            cfg = Config(database_path=path, vast_api_key="fake", whattomine_api_token="fake")
            db = Database(path)
            supervisor = Supervisor(cfg, db)
            worker_entered = threading.Event()
            release_worker = threading.Event()

            def blocked_scan():
                worker_entered.set()
                release_worker.wait(2)
                return {"scanned_at": "test"}

            supervisor._scan_locked = blocked_scan
            try:
                with patch.object(db, "set_setting") as persist:
                    accepted = supervisor.start_scan_async()
                    self.assertTrue(accepted)
                    self.assertTrue(worker_entered.wait(1))
                    progress = supervisor.current_ui_progress()
                    self.assertEqual(progress["status"], "running")
                    self.assertEqual(progress["percent"], 1)
                    persist.assert_not_called()
            finally:
                release_worker.set()
                deadline = time.monotonic() + 2
                while supervisor._scan_lock.locked() and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(supervisor._scan_lock.locked())

    def test_scan_completes_with_empty_offer_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "scan.sqlite3"))
            cfg = Config(
                database_path=str(Path(tmp) / "scan.sqlite3"),
                vast_api_key="test-vast-key",
                whattomine_api_token="test-wtm-token",
            )
            scanner = OpportunityScanner(cfg, db)
            scanner._vast_client = FakeVast([])
            scanner._wtm_client = FakeWhatToMine()

            result = scanner.scan()

            self.assertEqual(result["offers_seen"], 0)
            self.assertEqual(result["coins_seen"], 0)
            self.assertTrue(result["scanned_at"])

    def test_scan_completes_when_offers_exist_but_have_no_benchmark_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "scan.sqlite3"))
            cfg = Config(
                database_path=str(Path(tmp) / "scan.sqlite3"),
                vast_api_key="test-vast-key",
                whattomine_api_token="test-wtm-token",
            )
            scanner = OpportunityScanner(cfg, db)
            scanner._vast_client = FakeVast([{"id": 12345, "gpu_name": "GPU with no benchmark match", "num_gpus": 1}])
            scanner._wtm_client = FakeWhatToMine()

            result = scanner.scan()

            self.assertEqual(result["offers_seen"], 1)
            self.assertEqual(result["unknown_gpu_models"], 1)
            self.assertTrue(result["scanned_at"])


if __name__ == "__main__":
    unittest.main()
