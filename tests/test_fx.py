import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
import gph.fx as fx
from gph.fx import usd_to_inr


class CurrencyConversionTests(unittest.TestCase):
    def setUp(self):
        fx._LAST_GOOD = None
        fx._LAST_ATTEMPT = None
        fx._LAST_ERROR = None

    def test_usd_to_inr_is_simple_amount_times_positive_rate(self):
        self.assertEqual(usd_to_inr(1.2, 84.0), 100.8)
        self.assertEqual(usd_to_inr(2.0, 84.5), 169.0)
        self.assertEqual(usd_to_inr(-1.0, 84.0), -84.0)

    def test_conversion_rejects_missing_or_invalid_values(self):
        self.assertIsNone(usd_to_inr(None, 84.0))
        self.assertIsNone(usd_to_inr(1, None))
        self.assertIsNone(usd_to_inr(1, 0))
        self.assertIsNone(usd_to_inr(1, 10000))
        self.assertIsNone(usd_to_inr(float("nan"), 84))

    def test_rate_provider_response_is_validated_and_timestamped(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "result": "success", "rates": {"INR": 84.25},
            "time_last_update_unix": int(datetime.now(timezone.utc).timestamp()),
        }
        with patch("gph.fx.requests.get", return_value=response):
            result = fx.get_usd_inr_rate(force=True)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["rate"], 84.25)
        self.assertIsNotNone(result["source_updated_at"])
        self.assertEqual(result["accounting_currency"], "USD")

    def test_unavailable_provider_does_not_invent_an_inr_rate(self):
        with patch("gph.fx.requests.get", side_effect=TimeoutError("offline")):
            result = fx.get_usd_inr_rate(force=True)
        self.assertFalse(result["available"])
        self.assertIsNone(result["rate"])
        self.assertTrue(result["stale"])
        self.assertIn("INR", result["error"])

    def test_manual_refresh_is_rate_limited_and_reports_retry_time(self):
        now = datetime.now(timezone.utc)
        fx._LAST_GOOD = {"rate": 84.25, "fetched_at": now,
                         "source_updated_at": now, "source": "test"}
        fx._LAST_ATTEMPT = now - timedelta(seconds=60)
        with patch("gph.fx.requests.get") as get:
            result = fx.get_usd_inr_rate(force=True)
        get.assert_not_called()
        self.assertTrue(result["refresh_throttled"])
        self.assertGreater(result["retry_after_seconds"], 0)
        self.assertTrue(result["available"])

    def test_old_cached_rate_is_hidden_when_provider_fails(self):
        now = datetime.now(timezone.utc)
        fx._LAST_GOOD = {"rate": 84.25, "fetched_at": now - timedelta(hours=25),
                         "source_updated_at": now, "source": "test"}
        with patch("gph.fx.requests.get", side_effect=TimeoutError("offline")):
            result = fx.get_usd_inr_rate(force=True)
        self.assertFalse(result["available"])
        self.assertIsNone(result["rate"])
        self.assertEqual(result["last_known_rate"], 84.25)


if __name__ == "__main__":
    unittest.main()
