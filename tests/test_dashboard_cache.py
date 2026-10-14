"""The dashboard read is shared: tabs polling /api/state must not each open a heavy DB transaction."""
import threading
import time
import unittest
from unittest.mock import patch

import app as webapp


class DashboardBundleCacheTests(unittest.TestCase):
    def setUp(self):
        webapp._BUNDLE_CACHE.update(bundle=None, at=0.0)

    def test_repeated_polls_within_ttl_read_the_database_once(self):
        calls = []
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=lambda **kw: calls.append(1) or {"settings": {}}):
            for _ in range(20):
                webapp._dashboard_bundle()
        self.assertEqual(len(calls), 1)

    def test_concurrent_polls_do_not_queue_behind_a_slow_read(self):
        release = threading.Event()
        calls = []

        def slow(**kw):
            calls.append(1)
            release.wait(5)
            return {"settings": {"n": len(calls)}}

        webapp._BUNDLE_CACHE.update(bundle={"settings": {"n": 0}}, at=time.monotonic() - 60)  # stale but present
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=slow):
            refresher = threading.Thread(target=webapp._dashboard_bundle)
            refresher.start()
            time.sleep(0.2)
            started = time.monotonic()
            self.assertEqual(webapp._dashboard_bundle()["settings"]["n"], 0)  # served from cache immediately
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            refresher.join(5)
        self.assertEqual(len(calls), 1)

    def test_database_failure_serves_last_good_bundle(self):
        webapp._BUNDLE_CACHE.update(bundle={"settings": {"ok": 1}}, at=time.monotonic() - 60)
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=RuntimeError("Database connection pool is busy")):
            self.assertEqual(webapp._dashboard_bundle()["settings"], {"ok": 1})

    def test_database_failure_without_cache_still_raises(self):
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                webapp._dashboard_bundle()


if __name__ == "__main__":
    unittest.main()
