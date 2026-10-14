"""Money-safety regressions for live offer revalidation and destroy confirmation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gph.config import Config
from gph.db import Database, now_iso
from gph.providers.vast import VastAPIError
from gph.supervisor import Supervisor


class FakeVast:
    destroyed_ids = []
    create_calls = []
    offer = None
    destroy_error = None

    def __init__(self, api_key, base_url="https://cloud.vast.ai/api/v0"):
        self.api_key = api_key
        self.base_url = base_url

    def get_offer(self, offer_id, min_reliability=0.99):
        return dict(self.offer) if self.offer else None

    def create_instance(self, offer_id, payload):
        self.create_calls.append((offer_id, payload))
        return {"new_contract": 8080}

    def destroy_instance(self, instance_id):
        if self.destroy_error:
            raise self.destroy_error
        self.destroyed_ids.append(instance_id)
        return {"success": True}


class RentSafetyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "safe.sqlite3")
        self.config = Config(database_path=self.db_path, vast_api_key="fake-key", whattomine_api_token="fake-wtm")
        self.db = Database(self.db_path)
        self.supervisor = Supervisor(self.config, self.db)
        self.supervisor.live_blockers = lambda: []
        FakeVast.destroyed_ids = []
        FakeVast.create_calls = []
        FakeVast.offer = None
        FakeVast.destroy_error = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_price_is_rechecked_and_unprofitable_offer_never_creates_instance(self):
        FakeVast.offer = {
            "id": 101, "gpu_name": "RTX 4090", "num_gpus": 1,
            "verified": True, "rentable": True, "reliability": 0.999,
            "dph_base": 0.01, "dph_disk": 0.0, "dph_total": 1.0,
            "_gph_search_stage": "strict",
        }
        item = {
            "id": "opportunity-101", "offer_id": 101, "gpu_name": "RTX 4090",
            "coin_tag": "TST", "algorithm": "test", "qualified": True, "data_fresh": True,
            "gross_day_usd": 3.0, "pool_fee_pct": 1.0, "miner_fee_pct": 2.0,
            "scanned_at": now_iso(), "total_hourly_usd": 0.10, "gpu_hourly_usd": 0.10,
            "expected_net_day_usd": 1.0,
        }
        with patch("gph.supervisor.VastClient", FakeVast):
            with self.assertRaisesRegex(RuntimeError, "current offer price no longer passes"):
                self.supervisor._rent(item, {"id": "test", "enabled": True}, "unit test")
        self.assertEqual(FakeVast.create_calls, [], "a price-inflated offer must be rejected before paid creation")

    def test_accepted_delete_stays_pending_until_provider_listing_confirms_absence(self):
        self.db.upsert_managed_instance({
            "instance_id": 44, "offer_id": 22, "gpu_name": "RTX test", "hourly_cost": 0.05,
            "expected_gross_day": 1.0, "benchmark_hashrate_hs": 100.0,
            "observed_hashrate_hs": 0.0, "observed_gpu_util": 0.0, "observed_gpu_temp": 0.0,
            "accepted_share_lines": 0, "last_log": "", "started_at": now_iso(),
            "status": "running", "metadata_json": {"worker": "test"},
        })
        with patch("gph.supervisor.VastClient", FakeVast):
            accepted = self.supervisor._destroy(44, "unprofitable test instance")
            self.assertTrue(accepted)
            before_confirm = self.db.get_managed_instance(44)
            self.assertEqual(before_confirm["status"], "destroy_requested")
            self.assertIsNone(before_confirm["metadata"].get("destroyed_at"))

            self.supervisor._current_instances = lambda: []
            result = self.supervisor.monitor_once()
            self.assertEqual(result["monitored"], 0)
            after_confirm = self.db.get_managed_instance(44)
            self.assertEqual(after_confirm["status"], "destroyed")
            self.assertTrue(after_confirm["metadata"].get("destruction_confirmed"))
            self.assertIsNotNone(after_confirm["metadata"].get("destroyed_at"))

    def test_live_instance_snapshot_is_cached_for_dashboard_readers(self):
        live_rows = [{"id": 123, "actual_status": "running", "gpu_name": "RTX test"}]
        with patch("gph.supervisor.VastClient") as vast_cls:
            vast_cls.return_value.list_instances.return_value = live_rows
            rows = self.supervisor._current_instances()
        self.assertEqual(rows, live_rows)
        snapshot = self.db.get_setting("vast_instances_snapshot", {})
        self.assertEqual(snapshot.get("instances"), live_rows)
        self.assertEqual(snapshot.get("count"), 1)
        self.assertIsNotNone(snapshot.get("updated_at"))
        self.assertIsNone(snapshot.get("last_error"))

    def test_monitor_worker_can_start_when_auto_scanning_is_disabled(self):
        from unittest.mock import MagicMock
        self.config.auto_scan_enabled = False
        with patch("gph.supervisor.threading.Thread") as thread_cls:
            thread_cls.return_value.is_alive.return_value = False
            self.supervisor.start()
        names = [call.kwargs.get("name") for call in thread_cls.call_args_list]
        self.assertIn("gph-supervisor", names)
        self.assertIn("gph-instance-monitor", names)
        self.assertEqual(thread_cls.call_count, 2)

    def test_failed_delete_is_not_marked_destroyed_and_keeps_attempt_audit(self):
        self.db.upsert_managed_instance({
            "instance_id": 45, "offer_id": 23, "gpu_name": "RTX test", "hourly_cost": 0.05,
            "expected_gross_day": 1.0, "benchmark_hashrate_hs": 100.0,
            "observed_hashrate_hs": 0.0, "observed_gpu_util": 0.0, "observed_gpu_temp": 0.0,
            "accepted_share_lines": 0, "last_log": "", "started_at": now_iso(),
            "status": "running", "metadata_json": {},
        })
        FakeVast.destroy_error = VastAPIError("simulated Vast delete failure")
        with patch("gph.supervisor.VastClient", FakeVast):
            accepted = self.supervisor._destroy(45, "unprofitable test instance")
        row = self.db.get_managed_instance(45)
        self.assertFalse(accepted)
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["metadata"]["destroy_attempt_count"], 1)
        self.assertIn("simulated Vast delete failure", row["metadata"]["last_destroy_error"])


if __name__ == "__main__":
    unittest.main()
