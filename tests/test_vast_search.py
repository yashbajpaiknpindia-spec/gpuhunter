"""Regression tests for Vast search fallback and visibility of rejected offers."""
import tempfile
import unittest
import json
from pathlib import Path

from gph.config import Config
from gph.db import Database
from gph.engine import OpportunityScanner
from gph.providers.vast import VastClient


class VastOfferSearchTests(unittest.TestCase):
    def test_strict_response_is_not_dropped_when_optional_flags_are_omitted(self):
        client = VastClient("fake-key")
        client._request = lambda method, path, **kwargs: {
            "success": True,
            "offers": [{"id": 123, "gpu_name": "RTX 4090", "num_gpus": 1, "reliability": 0.995}],
        }
        result = client.search_offers_with_diagnostics(limit=500, min_reliability=0.99)
        self.assertEqual(result["strict_count"], 1)
        self.assertEqual(result["raw_count"], 1)
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(result["offers"][0]["_gph_search_stage"], "strict")


    def test_broad_audit_runs_even_when_strict_query_has_results(self):
        client = VastClient("fake-key")
        payloads = [
            {"offers": [{"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1, "verified": True, "rentable": True, "reliability": 0.995}]},
            {"offers": [
                {"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1, "verified": True, "rentable": True, "reliability": 0.995},
                {"id": 2, "gpu_name": "RTX 4090 host with 2 GPUs", "num_gpus": 2, "verified": True, "rentable": True, "reliability": 0.995},
            ]},
        ]
        calls = []
        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("params", {})))
            return payloads.pop(0)
        client._request = fake_request
        result = client.search_offers_with_diagnostics()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["strict_count"], 1)
        self.assertEqual(result["fallback_count"], 2)
        self.assertEqual(result["raw_count"], 2)  # strict and broad duplicate merged by ID
        by_id = {row["id"]: row for row in result["offers"]}
        self.assertEqual(by_id[1]["_gph_search_stage"], "strict")
        self.assertEqual(by_id[2]["_gph_search_stage"], "fallback")


    def test_strict_query_evidence_wins_conflicting_broad_metadata(self):
        client = VastClient("fake-key")
        payloads = [
            {"offers": [{"id": 44, "gpu_name": "RTX 4090"}]},
            {"offers": [{"id": 44, "gpu_name": "RTX 4090", "num_gpus": 4, "verified": False, "rentable": False, "reliability": 0.1}]},
        ]
        client._request = lambda method, path, **kwargs: payloads.pop(0)
        result = client.search_offers_with_diagnostics()
        self.assertEqual(len(result["offers"]), 1)
        row = result["offers"][0]
        self.assertEqual(row["_gph_search_stage"], "strict")
        self.assertNotIn("num_gpus", row)
        self.assertNotIn("verified", row)
        self.assertNotIn("rentable", row)
        self.assertNotIn("reliability", row)

    def test_zero_strict_results_retries_broad_single_gpu_query(self):
        client = VastClient("fake-key")
        calls = []
        payloads = [
            {"offers": []},
            {"success": True, "data": {"offers": [
                {"id": 555, "gpu_name": "RTX 3060", "num_gpus": 1, "dph_total": 0.04}
            ]}},
        ]
        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("params", {})))
            return payloads.pop(0)
        client._request = fake_request
        result = client.search_offers_with_diagnostics(limit=500, min_reliability=0.99)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(calls[1][2]["q"]).get("num_gpus"), {"gte": 1})  # broad fallback includes multi-GPU hosts
        self.assertEqual(result["strict_count"], 0)
        self.assertEqual(result["fallback_count"], 1)
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["offers"][0]["_gph_search_stage"], "fallback")
        self.assertIn("fallback", result["diagnostic"].lower())


    def test_both_search_stages_use_documented_cloud_get_endpoint(self):
        client = VastClient("fake-key")
        calls = []
        payloads = [
            {"offers": [{"id": 778, "gpu_name": "RTX 4090", "num_gpus": 1, "verified": True, "rentable": True, "reliability": 0.995}]},
            {"offers": []},
        ]
        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return payloads.pop(0)
        client._request = fake_request
        result = client.search_offers_with_diagnostics()
        self.assertEqual(result["strict_count"], 1)
        self.assertEqual(result["offers"][0]["_gph_search_stage"], "strict")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] == "GET" and call[1] == "/bundles/" for call in calls))
        self.assertTrue(all("q" in call[2]["params"] for call in calls))
        self.assertIn("cloud.vast.ai", client.base_url)

    def test_broad_fallback_query_does_not_hide_multi_gpu_hosts(self):
        client = VastClient("fake-key")
        calls = []
        payloads = [{"offers": []}, {"offers": [{"id": 1, "num_gpus": 4}]}]
        def fake_request(method, path, **kwargs):
            calls.append((method, kwargs.get("params", {})))
            return payloads.pop(0)
        client._request = fake_request
        result = client.search_offers_with_diagnostics()
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(json.loads(calls[1][1]["q"]).get("num_gpus"), {"gte": 1})

    def test_broad_fallback_rows_are_preserved_and_not_live_qualified_when_metadata_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "review.sqlite3")
            config = Config(database_path=db_path, vast_api_key="fake-vast", whattomine_api_token="fake-wtm")
            db = Database(db_path)
            scanner = OpportunityScanner(config, db)

            class FakeVast:
                def search_offers_with_diagnostics(self, **kwargs):
                    return {
                        "offers": [{"id": 901, "gpu_name": "RTX 4090", "num_gpus": 1,
                                    "dph_total": 0.4, "_gph_search_stage": "fallback"}],
                        "raw_count": 1, "strict_count": 0, "fallback_count": 1,
                        "fallback_used": True, "query_stage": "fallback",
                        "diagnostic": "strict query returned zero; fallback returned one",
                    }

            class FakeWtm:
                def get_gpus(self):
                    return [{"name": "NVIDIA GeForce RTX 4090", "algorithms": []}]
                def get_coins(self):
                    return []
                def calculate_gpu(self, benchmark_gpu):
                    return []

            scanner._vast_client = FakeVast()
            scanner._wtm_client = FakeWtm()
            result = scanner.scan()
            self.assertEqual(result["offers_seen"], 1)
            self.assertEqual(len(result["offer_diagnostics"]), 1)
            review = result["offer_diagnostics"][0]
            self.assertEqual(review["offer_id"], 901)
            self.assertIsNone(review["verified"])
            self.assertIsNone(review["rentable"])
            self.assertTrue(any("verification status" in x.lower() for x in review["reasons"]))
            self.assertTrue(any("rentability status" in x.lower() for x in review["reasons"]))
            self.assertTrue(any("no coin calculation rows" in x.lower() for x in review["reasons"]))
            self.assertEqual(len(db.get_setting("last_scan_offer_diagnostics", [])), 1)


    def test_offer_without_numeric_id_stays_visible_in_offer_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "missing-id.sqlite3")
            config = Config(database_path=db_path, vast_api_key="fake-vast", whattomine_api_token="fake-wtm")
            db = Database(db_path)
            scanner = OpportunityScanner(config, db)
            scanner._vast_client = type("FakeVast", (), {"search_offers": lambda self, **kw: [{"gpu_name": "RTX 4090", "num_gpus": 1}]})()
            scanner._wtm_client = type("FakeWtm", (), {"get_gpus": lambda self: [], "get_coins": lambda self: []})()
            result = scanner.scan()
            self.assertEqual(len(result["offer_diagnostics"]), 1)
            review = result["offer_diagnostics"][0]
            self.assertEqual(review["status"], "REJECTED — missing numeric offer ID")
            self.assertIn("cannot safely create an instance", review["reasons"][0].lower())

    def test_missing_gpu_benchmark_is_saved_as_an_offer_level_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "unknown.sqlite3")
            config = Config(database_path=db_path, vast_api_key="fake-vast", whattomine_api_token="fake-wtm")
            db = Database(db_path)
            scanner = OpportunityScanner(config, db)
            scanner._vast_client = type("FakeVast", (), {"search_offers": lambda self, **kw: [{"id": 902, "gpu_name": "Some Future GPU", "num_gpus": 1}]})()
            scanner._wtm_client = type("FakeWtm", (), {"get_gpus": lambda self: [], "get_coins": lambda self: []})()
            result = scanner.scan()
            review = result["offer_diagnostics"][0]
            self.assertIn("no gpu benchmark match", review["status"].lower())
            self.assertTrue(any("no whattomine gpu benchmark" in x.lower() for x in review["reasons"]))


    def test_get_offer_revalidates_exact_id_with_strict_provider_filters(self):
        client = VastClient("fake-key")
        captured = {}
        def fake_request(method, path, **kwargs):
            captured.update(method=method, path=path, params=kwargs.get("params", {}))
            return {"offers": [{"id": 55, "gpu_name": "RTX 4090", "num_gpus": 1}]}
        client._request = fake_request
        offer = client.get_offer(55, 0.99)
        self.assertEqual(offer["id"], 55)
        self.assertEqual(offer["_gph_search_stage"], "strict")
        query = json.loads(captured["params"]["q"])
        self.assertEqual(query["id"], {"eq": 55})
        self.assertEqual(query["num_gpus"], {"eq": 1})
        self.assertEqual(query["verified"], {"eq": True})
        self.assertEqual(query["rentable"], {"eq": True})

    def test_destroy_rejection_is_not_reported_as_success(self):
        from gph.providers.vast import VastAPIError
        client = VastClient("fake-key")
        client._request = lambda *args, **kwargs: {"success": False, "error": "instance still running"}
        with self.assertRaises(VastAPIError):
            client.destroy_instance(99)

    def test_malformed_instance_list_is_not_treated_as_empty_fleet(self):
        from gph.providers.vast import VastAPIError
        client = VastClient("fake-key")
        client._request = lambda *args, **kwargs: {"status": "ok", "message": "unexpected envelope"}
        with self.assertRaisesRegex(VastAPIError, "unexpected instance-list response"):
            client.list_instances()

    def test_instance_list_rejects_non_object_entries(self):
        from gph.providers.vast import VastAPIError
        client = VastClient("fake-key")
        client._request = lambda *args, **kwargs: {"instances": [{"id": 1}, None]}
        with self.assertRaisesRegex(VastAPIError, "non-object entries"):
            client.list_instances()

if __name__ == "__main__":
    unittest.main()
