"""Search-only extra Vast keys: they may search, but renting/paying always uses the main key."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gph.config import Config
from gph.db import Database, now_iso
from gph.engine import OpportunityScanner
from gph.providers.vast import VastQuotaError
from gph.supervisor import Supervisor

MAIN, EXTRA1, EXTRA2 = "main-funded-key", "extra-key-one", "extra-key-two"


class FakeSearch:
    def __init__(self, name, log, error=None):
        self.name, self.log, self.error = name, log, error
        self.last_call = None

    def search_offers_with_diagnostics(self, **kw):
        self.log.append(self.name)
        if self.error:
            raise self.error
        rows = [{"id": 1, "gpu_name": "RTX 4090", "num_gpus": 1}]
        return {"offers": rows, "raw_count": 1, "strict_count": 1, "fallback_count": 0, "rows_returned": 1,
                "query_stage": "strict", "audit_ran": False}


class SearchKeyTests(unittest.TestCase):
    def scanner(self, tmp, extras=(EXTRA1,), main=MAIN):
        path = str(Path(tmp) / "k.sqlite3")
        cfg = Config(database_path=path, vast_api_key=main, whattomine_api_token="w", vast_search_api_keys=tuple(extras))
        sc = OpportunityScanner(cfg, Database(path))
        sc._wtm_client = type("W", (), {"get_gpus": lambda s: [], "get_coins": lambda s: []})()
        return sc

    def install(self, sc, log, extra_errors=None, main_error=None):
        sc._vast_client = FakeSearch("main", log, main_error)
        for i, (label, _c, budget) in enumerate(list(sc._extra_slots)):
            err = (extra_errors or {}).get(label)
            sc._extra_slots[i] = (label, FakeSearch(label, log, err), budget)

    def test_extra_key_searches_first_and_main_rows_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc, log = self.scanner(tmp), []
            self.install(sc, log)
            result = sc.scan()
            self.assertEqual(log, ["search-key-1"])
            self.assertEqual(sc.search_budget.rows_used_today(), 0)
            self.assertEqual(sc._extra_slots[0][2].rows_used_today(), 1)
            self.assertEqual(result["provider_diagnostics"]["search_key"], "search-key-1")

    def test_extra_key_quota_fails_over_to_next_key_then_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc, log = self.scanner(tmp, extras=(EXTRA1, EXTRA2)), []
            err = VastQuotaError("quota", retry_after=40000, limit=20000, remaining=0)
            self.install(sc, log, extra_errors={"search-key-1": err})
            sc.scan()
            self.assertEqual(log, ["search-key-1", "search-key-2"])
            self.assertGreater(sc._extra_slots[0][2].cooldown_remaining(), 39000)
            log.clear()
            sc.scan()  # key 1 is cooling down: no request is sent to it
            self.assertEqual(log, ["search-key-2"])

    def test_all_keys_exhausted_raises_and_keeps_going_nowhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc, log = self.scanner(tmp), []
            err = VastQuotaError("quota", retry_after=40000, limit=20000, remaining=0)
            self.install(sc, log, extra_errors={"search-key-1": err}, main_error=err)
            with self.assertRaises(VastQuotaError):
                sc.scan()
            self.assertEqual(log, ["search-key-1", "main"])
            with self.assertRaises(VastQuotaError):
                sc.scan()
            self.assertEqual(log, ["search-key-1", "main"])  # nothing further was sent

    def test_key_equal_to_main_or_blank_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = self.scanner(tmp, extras=(MAIN, "", EXTRA1, EXTRA1))
            self.assertEqual(len(sc._extra_slots), 1)

    def test_no_extra_keys_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc, log = self.scanner(tmp, extras=()), []
            self.install(sc, log)
            sc.scan()
            self.assertEqual(log, ["main"])
            self.assertEqual(sc.search_budget.rows_used_today(), 1)

    def test_rent_path_only_ever_uses_the_main_key(self):
        used = []

        class Rec:
            def __init__(self, api_key, base_url="x"):
                used.append(api_key)

            def get_offer(self, offer_id, min_reliability=0.99):
                return {"id": 101, "gpu_name": "RTX 4090", "num_gpus": 1, "verified": True, "rentable": True,
                        "reliability": 0.999, "dph_base": 0.01, "dph_disk": 0.0, "dph_total": 1.0,
                        "_gph_search_stage": "strict"}

            def create_instance(self, *a, **k):
                raise AssertionError("must not create in this test")

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "r.sqlite3")
            cfg = Config(database_path=path, vast_api_key=MAIN, whattomine_api_token="w", vast_search_api_keys=(EXTRA1, EXTRA2))
            sup = Supervisor(cfg, Database(path))
            sup.live_blockers = lambda: []
            item = {"id": "o", "offer_id": 101, "gpu_name": "RTX 4090", "coin_tag": "T", "algorithm": "a",
                    "qualified": True, "data_fresh": True, "gross_day_usd": 3.0, "pool_fee_pct": 1.0,
                    "miner_fee_pct": 2.0, "scanned_at": now_iso(), "total_hourly_usd": 0.1,
                    "gpu_hourly_usd": 0.1, "expected_net_day_usd": 1.0}
            with patch("gph.supervisor.VastClient", Rec):
                with self.assertRaises(RuntimeError):
                    sup._rent(item, {"id": "t", "enabled": True}, "unit test")
            self.assertTrue(used)
            self.assertEqual(set(used), {MAIN})

    def test_supervisor_never_references_search_keys(self):
        src = (Path(__file__).resolve().parents[1] / "gph" / "supervisor.py").read_text()
        self.assertNotIn("vast_search_api_keys", src)


if __name__ == "__main__":
    unittest.main()
