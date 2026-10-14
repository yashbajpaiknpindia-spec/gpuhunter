import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from gph.pool_metrics import PoolMetricsClient


class PoolMetricsRealizedRevenueTests(unittest.TestCase):
    def _profile(self, realized_path=""):
        return {"pool_metrics": {
            "name": "test", "url": "https://pool.example/api/{wallet}/{worker}",
            "revenue_24h_usd_path": "data.usd_per_day",
            "source_updated_at_path": "data.updated_at",
            "max_source_age_seconds": 180,
            "realized_revenue_24h_usd_path": realized_path,
        }}

    @patch("gph.pool_metrics.requests.get")
    def test_does_not_infer_realized_earnings_from_usd_per_day_run_rate(self, get):
        response = Mock(ok=True)
        response.json.return_value = {"data": {"usd_per_day": 1.2, "earned_last_24h": 0.4, "updated_at": datetime.now(timezone.utc).isoformat()}}
        get.return_value = response
        result = PoolMetricsClient().fetch(self._profile(), "wallet", "worker", "coin")
        self.assertEqual(result["revenue_24h_usd"], 1.2)
        self.assertNotIn("realized_revenue_24h_usd", result)

    @patch("gph.pool_metrics.requests.get")
    def test_reads_explicit_realized_trailing_24h_field(self, get):
        response = Mock(ok=True)
        response.json.return_value = {"data": {"usd_per_day": 1.2, "earned_last_24h": 0.4}}
        get.return_value = response
        result = PoolMetricsClient().fetch(self._profile("data.earned_last_24h"), "wallet", "worker", "coin")
        self.assertEqual(result["revenue_24h_usd"], 1.2)
        self.assertEqual(result["realized_revenue_24h_usd"], 0.4)
        self.assertTrue(result.get("realized_revenue_updated_at"))

    @patch("gph.pool_metrics.requests.get")
    def test_rejects_stale_source_data_even_when_http_succeeds(self, get):
        response = Mock(ok=True)
        response.json.return_value = {
            "data": {
                "usd_per_day": 1.2,
                "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            }
        }
        get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "stale or future-dated"):
            PoolMetricsClient().fetch(self._profile(), "wallet", "worker", "coin")

    @patch("gph.pool_metrics.requests.get")
    def test_rejects_missing_source_timestamp_configuration(self, get):
        response = Mock(ok=True)
        response.json.return_value = {"data": {"usd_per_day": 1.2}}
        get.return_value = response
        profile = self._profile()
        del profile["pool_metrics"]["source_updated_at_path"]
        with self.assertRaisesRegex(RuntimeError, "source_updated_at_path"):
            PoolMetricsClient().fetch(profile, "wallet", "worker", "coin")


if __name__ == "__main__":
    unittest.main()
