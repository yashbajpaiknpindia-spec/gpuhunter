"""Regression tests for the Vast ``search_quota_exceeded`` (HTTP 429) outage.

Production symptom: two 500-row searches every 60s burned the 20,000-rows/day quota in about
20 minutes; every later scan failed with 429 and no opportunities were shown.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gph.config import Config
from gph.db import Database
from gph.engine import OpportunityScanner
from gph.providers.vast import VastClient, VastQuotaError, VastRateLimitError, VastAPIError
from gph.search_budget import VastSearchBudget
from gph.supervisor import Supervisor

# Body copied from the Render log.
QUOTA_BODY = b'{"error":"search_quota_exceeded","msg":"Daily search row quota exhausted","retry_after":45511,"limit":20000,"remaining":0}'


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=0):
        yield self._body

    def close(self):
        pass


def client_with(status, body, headers=None):
    client = VastClient("fake-key")
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(status, body, headers)

    client.session.request = request
    return client, calls


def offers(n, start=1):
    return {"offers": [{"id": start + i, "gpu_name": "RTX 4090", "num_gpus": 1, "dph_total": 0.3} for i in range(n)]}


class QuotaErrorParsingTests(unittest.TestCase):
    def test_quota_429_becomes_typed_error_with_retry_hint(self):
        client, _ = client_with(429, QUOTA_BODY)
        with self.assertRaises(VastQuotaError) as ctx:
            client._request("GET", "/bundles/")
        err = ctx.exception
        self.assertEqual(err.retry_after, 45511)
        self.assertEqual((err.limit, err.remaining), (20000, 0))
        self.assertTrue(err.quota)
        self.assertIn("12h", str(err))

    def test_plain_429_is_a_rate_limit_not_a_quota_error(self):
        client, _ = client_with(429, b"slow down", {"Retry-After": "17"})
        with self.assertRaises(VastRateLimitError) as ctx:
            client._request("GET", "/bundles/")
        self.assertNotIsInstance(ctx.exception, VastQuotaError)
        self.assertEqual(ctx.exception.retry_after, 17)

    def test_other_http_errors_stay_generic_with_status_code(self):
        client, _ = client_with(500, b"boom")
        with self.assertRaises(VastAPIError) as ctx:
            client._request("GET", "/bundles/")
        self.assertNotIsInstance(ctx.exception, VastRateLimitError)
        self.assertEqual(ctx.exception.status_code, 500)


class SearchBehaviourTests(unittest.TestCase):
    def test_quota_on_strict_query_raises_and_blocks_further_calls(self):
        client, calls = client_with(429, QUOTA_BODY)
        with self.assertRaises(VastQuotaError):
            client.search_offers_with_diagnostics(limit=100)
        self.assertEqual(len(calls), 1)  # strict only: the audit is not attempted after a quota error
        with self.assertRaises(VastQuotaError):
            client.search_offers_with_diagnostics(limit=100)
        self.assertEqual(len(calls), 1)  # inside the retry window: no HTTP call at all

    def test_audit_can_be_skipped_and_is_reported_honestly(self):
        client = VastClient("fake-key")
        calls = []
        client._request = lambda method, path, **kw: calls.append(kw["params"]) or offers(3)
        result = client.search_offers_with_diagnostics(limit=100, include_audit=False, audit_skip_reason="next broad audit in 40m")
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["audit_ran"])
        self.assertEqual(result["rows_returned"], 3)
        self.assertIn("audit skipped", result["diagnostic"].lower())
        self.assertIn("next broad audit in 40m", result["diagnostic"])
        self.assertNotIn("broad get returned zero", result["diagnostic"].lower())

    def test_skipped_audit_with_zero_strict_rows_is_not_called_a_failed_broad_get(self):
        client = VastClient("fake-key")
        client._request = lambda *a, **kw: {"offers": []}
        result = client.search_offers_with_diagnostics(include_audit=False, audit_skip_reason="conserving quota")
        self.assertIn("zero eligible offers", result["diagnostic"])
        self.assertIn("conserving quota", result["diagnostic"])
        self.assertEqual(result["offers"], [])

    def test_quota_hit_by_audit_keeps_strict_results_and_reports_it(self):
        client = VastClient("fake-key")
        state = {"n": 0}

        def request(method, path, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return offers(2)
            raise VastQuotaError("quota", retry_after=3600, limit=20000, remaining=0)

        client._request = request
        result = client.search_offers_with_diagnostics(limit=100, audit_limit=40)
        self.assertEqual(len(result["offers"]), 2)
        self.assertEqual(result["rate_limit"]["retry_after"], 3600)
        self.assertTrue(result["rate_limit"]["quota"])
        self.assertIn("broad audit query failed", result["diagnostic"])
        self.assertNotIn("returned zero", result["diagnostic"])

    def test_query_uses_limit_order_audit_limit_and_price_cap(self):
        client = VastClient("fake-key")
        queries = []
        client._request = lambda method, path, **kw: queries.append(json.loads(kw["params"]["q"])) or offers(1)
        client.search_offers_with_diagnostics(limit=100, audit_limit=40, order="dph_total", max_total_hourly_usd=0.5)
        strict, audit = queries
        self.assertEqual(strict["limit"], 100)
        self.assertEqual(strict["order"], [["dph_total", "asc"]])
        self.assertEqual(strict["dph_total"], {"lte": 0.5})
        self.assertEqual(audit["limit"], 40)
        self.assertNotIn("dph_total", audit)

    def test_descending_and_multi_field_order_syntax(self):
        self.assertEqual(VastClient._order_clause("-score"), [["score", "desc"]])
        self.assertEqual(VastClient._order_clause("dph_total,-reliability"), [["dph_total", "asc"], ["reliability", "desc"]])
        self.assertIsNone(VastClient._order_clause(""))

    def test_rejected_sort_syntax_falls_back_once_and_is_remembered(self):
        client = VastClient("fake-key")
        queries = []

        def request(method, path, **kw):
            query = json.loads(kw["params"]["q"])
            queries.append(query)
            if "order" in query:
                raise VastAPIError("Vast API returned HTTP 400: bad order", status_code=400)
            return offers(1)

        client._request = request
        result = client.search_offers_with_diagnostics(limit=50, include_audit=False, order="dph_total")
        self.assertEqual(len(result["offers"]), 1)
        self.assertEqual(["order" in q for q in queries], [True, False])
        client.search_offers_with_diagnostics(limit=50, include_audit=False, order="dph_total")
        self.assertEqual(["order" in q for q in queries], [True, False, False])  # not retried with order again

    def test_revalidation_query_asks_for_a_single_row(self):
        client = VastClient("fake-key")
        seen = {}
        client._request = lambda method, path, **kw: seen.update(q=json.loads(kw["params"]["q"])) or offers(1, start=55)
        self.assertIsNotNone(client.get_offer(55, 0.99))
        self.assertLessEqual(seen["q"]["limit"], 2)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "b.sqlite3"))
        self.now = 86400 * 20000 + 3600.0  # 01:00 UTC
        self.clock = lambda: self.now

    def tearDown(self):
        self.tmp.cleanup()

    def budget(self, **kw):
        args = dict(daily_rows=20000, reserve_rows=1500, strict_limit=100, audit_limit=40, audit_interval_seconds=3600, clock=self.clock)
        args.update(kw)
        return VastSearchBudget(self.db, **args)

    def test_default_pacing_cannot_exhaust_the_daily_quota(self):
        b = self.budget()
        interval = b.recommended_interval()
        secs_left = 86400 - (self.now % 86400)
        rows_until_reset = (secs_left / interval) * 100 + -(-secs_left // 3600) * 40
        self.assertLessEqual(rows_until_reset, 20000 - 1500)
        self.assertGreater(interval, 300)  # was 60s with 1,000 rows per scan

    def test_first_scan_runs_audit_then_audit_waits_for_its_interval(self):
        b = self.budget()
        first = b.plan()
        self.assertTrue(first["include_audit"])
        self.assertEqual((first["strict_limit"], first["audit_limit"]), (100, 40))
        b.record_rows(140)
        b.mark_audit_ran()
        self.now += 600
        second = b.plan()
        self.assertFalse(second["include_audit"])
        self.assertIn("next broad audit", second["audit_skip_reason"])
        self.now += 3600
        self.assertTrue(b.plan()["include_audit"])

    def test_audit_can_be_disabled(self):
        plan = self.budget(audit_limit=0).plan()
        self.assertFalse(plan["include_audit"])
        self.assertIn("disabled", plan["audit_skip_reason"])

    def test_usage_and_cooldown_survive_a_restart(self):
        b = self.budget()
        b.plan()
        b.record_rows(500)
        b.note_rate_limit(45511, quota=True, reason="quota")
        b2 = self.budget()
        b2.ensure_loaded()
        self.assertEqual(b2.rows_used_today(), 20000)  # server said quota is gone -> mirrored locally
        self.assertGreater(b2.cooldown_remaining(), 45000)
        with self.assertRaises(VastQuotaError):
            b2.plan()

    def test_counter_resets_at_utc_midnight(self):
        b = self.budget()
        b.record_rows(18495)  # fewer than 10 usable rows left
        with self.assertRaises(VastQuotaError):
            b.plan()
        self.now = (self.now // 86400 + 1) * 86400 + 5
        self.assertEqual(b.rows_used_today(), 0)
        self.assertEqual(b.plan()["strict_limit"], 100)

    def test_last_rows_shrink_the_strict_limit_instead_of_overshooting(self):
        b = self.budget()
        b.record_rows(18500 - 60)
        self.assertEqual(b.plan()["strict_limit"], 60)

    def test_cooldown_is_absolute_not_relative_to_last_scan(self):
        b = self.budget()
        b.note_rate_limit(7200, quota=True)
        self.now += 3600
        self.assertAlmostEqual(b.cooldown_remaining(), 3600, delta=1)


class EngineQuotaTests(unittest.TestCase):
    def make_scanner(self, tmp, client):
        db_path = str(Path(tmp) / "q.sqlite3")
        config = Config(database_path=db_path, vast_api_key="fake-vast", whattomine_api_token="fake-wtm")
        db = Database(db_path)
        scanner = OpportunityScanner(config, db)
        scanner._vast_client = client
        scanner._wtm_client = type("W", (), {"get_gpus": lambda self: [], "get_coins": lambda self: []})()
        return scanner, db

    def test_quota_error_propagates_sets_cooldown_and_stops_further_calls(self):
        calls = []

        class Client:
            def search_offers_with_diagnostics(self, **kw):
                calls.append(kw)
                raise VastQuotaError("quota", retry_after=45511, limit=20000, remaining=0)

        with tempfile.TemporaryDirectory() as tmp:
            scanner, db = self.make_scanner(tmp, Client())
            with self.assertRaises(VastQuotaError):
                scanner.scan()
            self.assertEqual(len(calls), 1)
            self.assertGreater(scanner.search_budget.cooldown_remaining(), 45000)
            self.assertGreater(db.get_setting("vast_search_budget")["cooldown_until"], 0)
            with self.assertRaises(VastQuotaError):  # second attempt must not reach the provider
                scanner.scan()
            self.assertEqual(len(calls), 1)

    def test_scan_requests_small_limit_and_records_rows(self):
        seen = {}

        class Client:
            def search_offers_with_diagnostics(self, **kw):
                seen.update(kw)
                rows = [{"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1}]
                return {"offers": rows, "raw_count": 1, "strict_count": 1, "fallback_count": 0, "rows_returned": 1,
                        "query_stage": "strict", "audit_ran": False}

        with tempfile.TemporaryDirectory() as tmp:
            scanner, _db = self.make_scanner(tmp, Client())
            result = scanner.scan()
            self.assertEqual(seen["limit"], 100)  # not 500
            self.assertEqual(seen["audit_limit"], 40)
            self.assertEqual(seen["order"], "dph_total")
            self.assertEqual(scanner.search_budget.rows_used_today(), 1)
            self.assertEqual(result["provider_diagnostics"]["rows_used_today"], 1)

    def test_audit_quota_hit_after_good_strict_results_pauses_future_searches(self):
        class Client:
            def search_offers_with_diagnostics(self, **kw):
                rows = [{"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1}]
                return {"offers": rows, "raw_count": 1, "strict_count": 1, "fallback_count": 0, "rows_returned": 1,
                        "query_stage": "strict", "audit_ran": True, "fallback_error": "quota",
                        "rate_limit": {"retry_after": 5000, "quota": True}}

        with tempfile.TemporaryDirectory() as tmp:
            scanner, _db = self.make_scanner(tmp, Client())
            result = scanner.scan()
            self.assertEqual(result["offers_seen"], 1)  # strict results are still saved
            self.assertGreater(scanner.search_budget.cooldown_remaining(), 4900)


class SupervisorPacingTests(unittest.TestCase):
    def fake(self, cooldown=0.0, recommended=0.0, interval=60):
        budget = SimpleNamespace(recommended_interval=lambda: recommended, cooldown_remaining=lambda: cooldown)
        return SimpleNamespace(config=SimpleNamespace(scan_interval_seconds=interval), scanner=SimpleNamespace(search_budget=budget))

    def test_interval_is_floor_then_budget_then_cooldown(self):
        self.assertEqual(Supervisor.effective_scan_interval(self.fake()), 60)
        self.assertEqual(Supervisor.effective_scan_interval(self.fake(recommended=480)), 480)
        self.assertEqual(Supervisor.effective_scan_interval(self.fake(recommended=480, cooldown=45511)), 45511)

    def test_default_config_no_longer_scans_every_minute_against_vast(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "s.sqlite3")
            config = Config(database_path=db_path, vast_api_key="k", whattomine_api_token="t")
            scanner = OpportunityScanner(config, Database(db_path))
            scanner.search_budget._clock = lambda: 86400 * 20000 + 3600.0  # 01:00 UTC, full budget
            self.assertGreater(Supervisor.effective_scan_interval(SimpleNamespace(config=config, scanner=scanner)), 300)


if __name__ == "__main__":
    unittest.main()
