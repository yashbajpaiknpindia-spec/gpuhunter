import tempfile
import unittest
from pathlib import Path

from gph.config import Config
from gph.db import Database
from gph.supervisor import Supervisor


class KryptexSnapshotTests(unittest.TestCase):
    def test_material_reference_changes_are_written_to_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(database_path=str(Path(tmp) / "gph.sqlite3"), kryptex_gpu_scan_interval_seconds=300,
                         kryptex_gpu_reference_enabled=True)
            db = Database(cfg.database_path)
            db.set_setting("last_gpu_models_seen", ["RTX 3060"])
            sup = Supervisor(cfg, db)
            row = {"gpu_name": "RTX 3060", "available": True, "coin_rows": [{
                "coin": "PRL", "algorithm": "PearlHash", "hashrate": 47.0,
                "power_w": 120.0, "daily_profit_usd": 0.5, "daily_revenue_usd": 0.61,
            }], "data_keys": ["rows"]}
            sup.kryptex.gpu_profitability = lambda name: dict(row, gpu_name=name)
            first = sup.refresh_kryptex_gpu_market(force=True)
            self.assertEqual(first["attempted_models"], 1)
            self.assertEqual(first["available_models"], 1)

            row["coin_rows"] = [{
                "coin": "PRL", "algorithm": "PearlHash", "hashrate": 55.0,
                "power_w": 120.0, "daily_profit_usd": 0.7, "daily_revenue_usd": 0.76,
            }]
            second = sup.refresh_kryptex_gpu_market(force=True)
            self.assertEqual(second["available_models"], 1)
            events = db.get_events(20)
            self.assertTrue(any(e["event"] == "kryptex_gpu_benchmark_fluctuation" for e in events))


    def test_undocumented_gpu_reference_is_disabled_and_stale_rows_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(database_path=str(Path(tmp) / "gph.sqlite3"), kryptex_gpu_reference_enabled=False)
            db = Database(cfg.database_path)
            db.set_setting("kryptex_gpu_market", {"enabled": True, "models": [{"gpu_name": "RTX 4090"}]})
            sup = Supervisor(cfg, db)
            sup.kryptex.gpu_profitability = lambda name: self.fail("undocumented endpoint must not be called by default")
            snapshot = sup.refresh_kryptex_gpu_market(force=True)
            self.assertFalse(snapshot["enabled"])
            self.assertEqual(snapshot["models"], [])
            self.assertFalse(db.get_setting("kryptex_gpu_market").get("models"))

if __name__ == "__main__":
    unittest.main()
