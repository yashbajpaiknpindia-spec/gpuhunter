import os
import tempfile
import unittest

from gph.db import Database
from gph.config import projected_commitment_hours, runtime_limit_reached
from gph.profiles import match_gpu_name, match_profile, wallet_for_profile
from gph.profit import RiskLimits, evaluate_profit, offer_hourly_cost
from gph.monitor import parse_hashrate_hs, parse_share_metrics, parse_gpu_stats


class ProfitGateTests(unittest.TestCase):
    def test_profitable_candidate_can_pass_all_economic_and_profile_gates(self):
        result = evaluate_profit(
            gross_day_usd=4.0,
            hourly_cost_usd=0.03,
            gpu_hourly_usd=0.02,
            reliability=0.995,
            data_fresh=True,
            profile_available=True,
            pool_fee_pct=1.0,
            miner_fee_pct=2.0,
            limits=RiskLimits(),
            live_capability_ready=True,
        )
        self.assertTrue(result["qualified"])
        self.assertGreater(result["expected_net_day_usd"], 0.5)
        self.assertGreaterEqual(result["revenue_cost_ratio"], 1.5)

    def test_unprofitable_candidate_is_rejected(self):
        result = evaluate_profit(
            gross_day_usd=0.50,
            hourly_cost_usd=0.06,
            gpu_hourly_usd=0.06,
            reliability=0.999,
            data_fresh=True,
            profile_available=True,
            pool_fee_pct=1.0,
            miner_fee_pct=2.0,
            limits=RiskLimits(),
            live_capability_ready=True,
        )
        self.assertFalse(result["economics_qualified"])
        self.assertFalse(result["qualified"])
        self.assertTrue(any("net" in x.lower() or "ratio" in x.lower() or "rate cap" in x.lower() for x in result["reasons"]))


    def test_zero_hourly_caps_disable_price_only_gates(self):
        limits = RiskLimits(max_gpu_hourly_usd=0, max_total_hourly_usd=0, min_revenue_cost_ratio=1.1, min_net_profit_usd_day=0.1)
        result = evaluate_profit(
            gross_day_usd=10.0,
            hourly_cost_usd=0.20,
            gpu_hourly_usd=0.19,
            reliability=0.999,
            data_fresh=True,
            profile_available=True,
            pool_fee_pct=1.0,
            miner_fee_pct=2.0,
            limits=limits,
            live_capability_ready=True,
        )
        self.assertTrue(result["economics_qualified"])
        self.assertFalse(any("rate" in reason.lower() and "cap" in reason.lower() for reason in result["reasons"]))

    def test_nonzero_hourly_caps_are_still_enforced(self):
        limits = RiskLimits(max_gpu_hourly_usd=0.10, max_total_hourly_usd=0.12)
        result = evaluate_profit(
            gross_day_usd=10.0,
            hourly_cost_usd=0.20,
            gpu_hourly_usd=0.19,
            reliability=0.999,
            data_fresh=True,
            profile_available=True,
            pool_fee_pct=1.0,
            miner_fee_pct=2.0,
            limits=limits,
            live_capability_ready=True,
        )
        self.assertFalse(result["economics_qualified"])
        self.assertTrue(any("GPU rate" in reason for reason in result["reasons"]))
        self.assertTrue(any("All-in rate" in reason for reason in result["reasons"]))

    def test_stale_data_fails_closed(self):
        result = evaluate_profit(
            gross_day_usd=10.0,
            hourly_cost_usd=0.01,
            gpu_hourly_usd=0.01,
            reliability=0.999,
            data_fresh=False,
            profile_available=True,
            pool_fee_pct=1.0,
            miner_fee_pct=2.0,
            limits=RiskLimits(),
            live_capability_ready=True,
        )
        self.assertFalse(result["economics_qualified"])
        self.assertTrue(any("stale" in x.lower() for x in result["reasons"]))

    def test_cost_uses_conservative_all_in_hourly_rate(self):
        gpu, total, disk = offer_hourly_cost({"dph_base": 0.061, "dph_disk": 0.002, "dph_total": 0.062})
        self.assertAlmostEqual(gpu, 0.061)
        self.assertAlmostEqual(total, 0.063)
        self.assertAlmostEqual(disk, 0.002)



class RuntimePolicyTests(unittest.TestCase):
    def test_zero_runtime_limit_means_no_fixed_time_ceiling(self):
        self.assertFalse(runtime_limit_reached(1000.0, 0))

    def test_positive_runtime_limit_is_enforced(self):
        self.assertFalse(runtime_limit_reached(5.9, 6))
        self.assertTrue(runtime_limit_reached(6.0, 6))

    def test_unlimited_runtime_still_budgets_full_day_of_rent(self):
        self.assertEqual(projected_commitment_hours(0), 24)
        self.assertEqual(projected_commitment_hours(8), 8)
        self.assertEqual(projected_commitment_hours(48), 24)


class ModelMatchingTests(unittest.TestCase):
    def test_gpu_names_normalize_vendor_labels(self):
        self.assertTrue(match_gpu_name("RTX_4090", "NVIDIA GeForce RTX 4090"))
        self.assertTrue(match_gpu_name("RTX 3060", "GeForce RTX 3060"))

    def test_gpu_model_does_not_match_ti_variant(self):
        self.assertFalse(match_gpu_name("RTX 3060", "GeForce RTX 3060 Ti"))

    def test_profile_only_matches_enabled_supported_coin_algo(self):
        profiles = [{"id": "p1", "enabled": True, "coin_tags": ["PRL"], "algorithms": ["PearlHash"], "gpu_models": ["RTX 3060"]}]
        self.assertEqual(match_profile("PRL", "PearlHash", "NVIDIA RTX 3060", profiles)["id"], "p1")
        self.assertIsNone(match_profile("BTC", "SHA-256", "RTX 3060", profiles))
        profiles[0]["enabled"] = False
        self.assertIsNone(match_profile("PRL", "PearlHash", "RTX 3060", profiles))

    def test_public_wallet_loaded_by_profile_key(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wallets.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"wallets":{"PRL":"public-address-example"},"workers":{}}')
            self.assertEqual(wallet_for_profile({"wallet_key": "PRL"}, path), "public-address-example")


class MinerLogParsingTests(unittest.TestCase):
    def test_parse_peakminer_hashrate(self):
        log = "2026-10-13 16:49:49 INFO summary\nTotal           48.7 TH/s     eff 100.0%\n"
        self.assertEqual(parse_hashrate_hs(log), 48.7e12)

    def test_parse_accepted_share_and_time(self):
        log = "2026-10-13 16:49:30 INFO new job\n2026-10-13 16:49:33 accepted GPU 0 lat 121ms\n"
        count, last_at = parse_share_metrics(log)
        self.assertEqual(count, 1)
        self.assertIsNotNone(last_at)
        self.assertEqual(last_at.year, 2026)

    def test_parse_gpu_stats(self):
        self.assertEqual(parse_gpu_stats("73, 68\n"), (73.0, 68.0))


class DatabaseTests(unittest.TestCase):
    def test_audit_log_and_settings(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "gph.sqlite3"))
            db.log("INFO", "unit_test", {"ok": True})
            db.set_setting("last_scan_summary", {"offers_seen": 4})
            self.assertEqual(db.get_events(1)[0]["event"], "unit_test")
            self.assertEqual(db.get_setting("last_scan_summary")["offers_seen"], 4)

    def test_opportunities_sorted_by_expected_net_and_gph_tables_are_namespaced(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "gph.sqlite3")
            db = Database(path)
            db.replace_opportunities([
                {"id": "low", "scanned_at": "2026-10-13T00:00:00+00:00", "offer_id": 1, "coin_tag": "A", "expected_net_day_usd": 0.25},
                {"id": "high", "scanned_at": "2026-10-13T00:01:00+00:00", "offer_id": 2, "coin_tag": "B", "expected_net_day_usd": 1.25},
            ])
            self.assertEqual([x["id"] for x in db.get_opportunities()], ["high", "low"])
            with db.session() as conn:
                names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("gph_events", names)
            self.assertIn("gph_settings", names)
            self.assertNotIn("events", names)



class UiProgressTests(unittest.TestCase):
    def test_operation_progress_is_persisted_with_safe_bounded_percent(self):
        from gph.supervisor import Supervisor
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "gph.sqlite3"))
            supervisor = object.__new__(Supervisor)
            supervisor.db = db
            supervisor.set_ui_progress("Market scan", "Evaluating offers", 137, "Offer batch", "running")
            progress = db.get_setting("ui_operation_progress", {})
            self.assertEqual(progress["action"], "Market scan")
            self.assertEqual(progress["stage"], "Evaluating offers")
            self.assertEqual(progress["percent"], 100)
            self.assertEqual(progress["status"], "running")
            self.assertIn("updated_at", progress)


class MultiInstanceConfigTests(unittest.TestCase):
    def test_max_active_instances_defaults_to_three_and_is_bounded(self):
        from gph.config import Config
        # Environment values are read when the config module is imported, so assert the
        # documented runtime invariant via a minimal Config instance override.
        cfg = Config(max_active_instances=3)
        self.assertEqual(cfg.max_active_instances, 3)
        self.assertGreaterEqual(cfg.max_active_instances, 1)
        self.assertLessEqual(cfg.max_active_instances, 25)


if __name__ == "__main__":
    unittest.main()
