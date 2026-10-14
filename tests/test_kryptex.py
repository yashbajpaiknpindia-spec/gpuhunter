import unittest

from gph.providers.kryptex import KryptexPoolClient


class KryptexGPUReferenceTests(unittest.TestCase):
    def test_gpu_slug_adds_manufacturer_prefix(self):
        self.assertEqual(
            KryptexPoolClient.gpu_slug("NVIDIA GeForce RTX 3060"),
            "nvidia-geforce-rtx-3060",
        )
        self.assertEqual(
            KryptexPoolClient.gpu_slug("AMD Radeon RX 7900 XTX"),
            "amd-radeon-rx-7900-xtx",
        )
        self.assertEqual(
            KryptexPoolClient.gpu_slug("RTX 3060"),
            "nvidia-geforce-rtx-3060",
        )

    def test_parser_only_treats_explicit_daily_field_as_daily(self):
        sample = {
            "rows": [
                {
                    "coin": "PRL",
                    "algorithm": "PearlHash",
                    "hashrate": "47.00 TH/s",
                    "power": "120 W",
                    "daily_profit_usd": "$0.61/day",
                    "annual_profit_usd": 222.27,
                },
                {
                    "coin": "QTC",
                    "algorithm": "GhostRider",
                    "hashrate": 147,
                    "power_w": 67,
                    "profit": 202.75,
                },
            ]
        }
        rows = KryptexPoolClient._collect_gpu_coin_rows(sample)
        self.assertEqual(len(rows), 2)
        prl = next(x for x in rows if x["coin"] == "PRL")
        qtc = next(x for x in rows if x["coin"] == "QTC")
        self.assertAlmostEqual(prl["daily_profit_usd"], 0.61)
        self.assertIsNone(prl["daily_revenue_usd"])
        self.assertIsNone(qtc["daily_profit_usd"])
        self.assertIsNone(qtc["daily_revenue_usd"])

    def test_account_status_reports_live_public_pool_and_wallet_telemetry(self):
        client = KryptexPoolClient(timeout=1)
        seen = []

        def fake_get(path, ttl=60, optional=False):
            seen.append(path)
            if path.endswith("/pool/info"):
                return {"pool": "active"}
            if "/workers/" in path:
                return {"workers": [{"name": "worker-1", "hashrate": 123}]}
            if "/chart/" in path:
                return {"hashrate": 123}
            if "/balance/" in path:
                return {"balance": 0.75}
            if path.endswith("/coin/prl/info"):
                return {"price_usd": 0.12}
            if path.endswith("/coin/prl/price/chart"):
                return [{"price": 0.12}]
            return None

        client._get = fake_get
        status = client.account_status("prl", "public_wallet_123456", "worker-1")
        self.assertTrue(status["connected"])
        self.assertTrue(status["account_data_available"])
        self.assertTrue(status["balance_available"])
        self.assertAlmostEqual(status["balance_raw"], 0.75)
        self.assertAlmostEqual(status["coin_price_sample"], 0.12)
        self.assertEqual(status["worker_count"], 1)
        self.assertTrue(any("/pool/info" in path for path in seen))
        self.assertFalse(status["payout_stats_available"])

    def test_balance_uses_total_or_sums_confirmed_and_unconfirmed(self):
        from gph.providers.kryptex import _first_number

        self.assertAlmostEqual(_first_number({"total": "1.25", "confirmed": 1.0, "unconfirmed": 0.25}, ("total", "total_balance", "balance", "amount", "available", "unpaid", "immature")), 1.25)
        balance = {"confirmed": "0.50", "unconfirmed": "0.25"}
        value = _first_number(balance, ("total", "total_balance", "balance", "amount", "available", "unpaid", "immature"))
        if value is None:
            confirmed = _first_number(balance, ("confirmed",))
            unconfirmed = _first_number(balance, ("unconfirmed",))
            if confirmed is not None or unconfirmed is not None:
                value = (confirmed or 0.0) + (unconfirmed or 0.0)
        self.assertAlmostEqual(value, 0.75)

    def test_empty_worker_list_is_not_account_data(self):
        client = KryptexPoolClient(timeout=1)
        client._get = lambda path, ttl=60, optional=False: (
            {"pool": "active"} if path.endswith("/pool/info")
            else [] if "/workers/" in path
            else None
        )
        status = client.account_status("prl", "public_wallet_123456")
        self.assertTrue(status["connected"])
        self.assertEqual(status["worker_count"], 0)
        self.assertFalse(status["account_data_available"])
        self.assertFalse(status["balance_available"])

    def test_pool_reachable_but_no_wallet_telemetry_is_explicit(self):
        client = KryptexPoolClient(timeout=1)
        client._get = lambda path, ttl=60, optional=False: ({"pool": "active"} if path.endswith("/pool/info") else None)
        status = client.account_status("prl", "public_wallet_123456")
        self.assertTrue(status["connected"])
        self.assertFalse(status["account_data_available"])
        self.assertIn("Confirm the exact mining address/username", status["error"])

    def test_gpu_reference_ttl_defaults_to_one_minute(self):
        import os
        from unittest.mock import patch
        client = KryptexPoolClient()
        seen_ttls = []
        def fake_get(path, ttl=60, optional=False):
            seen_ttls.append(ttl)
            return None
        client._get = fake_get
        with patch.dict(os.environ, {"KRYPTEX_GPU_REFERENCE_CACHE_SECONDS": "60"}, clear=False):
            client.gpu_profitability("RTX 3060")
        self.assertTrue(seen_ttls)
        self.assertTrue(all(ttl == 60 for ttl in seen_ttls))


if __name__ == "__main__":
    unittest.main()
