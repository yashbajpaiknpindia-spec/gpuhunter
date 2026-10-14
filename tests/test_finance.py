import unittest
from datetime import datetime, timedelta, timezone

from gph.finance import funding_plan, observed_net_24h


class FundingPlanTests(unittest.TestCase):
    def test_daily_funding_shortfall_for_one_target_instance(self):
        candidate = {
            "offer_id": 100,
            "gpu_name": "RTX Test",
            "economics_qualified": True,
            "qualified": False,
            "total_hourly_usd": 0.03,
            "total_cost_day_usd": 0.77,
            "expected_net_day_usd": 1.25,
        }
        plan = funding_plan([candidate], [], 1, {"available": True, "balance_usd": 0.50}, 0.05)
        self.assertEqual(plan["target_instances"], 1)
        self.assertEqual(plan["active_instances"], 0)
        self.assertAlmostEqual(plan["required_24h_usd"], 0.77)
        self.assertAlmostEqual(plan["shortfall_usd"], 0.27)
        self.assertFalse(plan["covers_next_24h"])
        self.assertFalse(plan["candidate_is_live_qualified"])

    def test_existing_active_rental_and_target_slot_are_both_budgeted(self):
        candidate = {"economics_qualified": True, "qualified": True, "total_cost_day_usd": 0.77, "expected_net_day_usd": 1}
        instance = {"status": "running", "hourly_cost_usd": 0.02}
        plan = funding_plan([candidate], [instance], 2, {"available": True, "balance_usd": 2.0}, 0.05)
        self.assertEqual(plan["active_instances"], 1)
        self.assertEqual(plan["additional_slots"], 1)
        self.assertAlmostEqual(plan["required_24h_usd"], 1.30)
        self.assertTrue(plan["covers_next_24h"])

    def test_unknown_balance_does_not_claim_fully_funded(self):
        plan = funding_plan([], [], 1, {"available": False, "balance_usd": 0.1}, 0.05)
        self.assertIsNone(plan["shortfall_usd"])
        self.assertFalse(plan["covers_next_24h"])
        self.assertIsNone(plan["required_24h_usd"])

    def test_observed_net_waits_for_aligned_24h_window(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=3)
        result = observed_net_24h([{
            "status": "running", "hourly_cost_usd": 0.03,
            "pool_revenue_24h_usd": 1.5,
            "started_at": recent.isoformat(),
            "pool_metrics_updated_at": datetime.now(timezone.utc).isoformat(),
        }], 0.05)
        self.assertIsNone(result["net_24h_usd"])
        self.assertIn("actual trailing-24-hour", result["reason"])

    def test_run_rate_is_not_misreported_as_achieved_profit(self):
        started = datetime.now(timezone.utc) - timedelta(hours=30)
        result = observed_net_24h([{
            "status": "running", "hourly_cost_usd": 0.03,
            "pool_revenue_24h_usd": 1.5,
            "started_at": started.isoformat(),
            "pool_metrics_updated_at": datetime.now(timezone.utc).isoformat(),
        }], 0.05)
        self.assertIsNone(result["net_24h_usd"])
        self.assertIn("current USD/day run-rate", result["reason"])

    def test_observed_net_subtracts_full_day_rent_and_reserve_when_aligned(self):
        started = datetime.now(timezone.utc) - timedelta(hours=30)
        result = observed_net_24h([{
            "status": "running", "hourly_cost_usd": 0.03,
            "pool_revenue_24h_usd": 1.5,
            "pool_realized_revenue_24h_usd": 1.5,
            "pool_realized_revenue_updated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started.isoformat(),
            "pool_metrics_updated_at": datetime.now(timezone.utc).isoformat(),
        }], 0.05)
        self.assertAlmostEqual(result["net_24h_usd"], 0.73)
        self.assertIn("Actual trailing-24-hour pool-reported", result["reason"])


if __name__ == "__main__":
    unittest.main()
