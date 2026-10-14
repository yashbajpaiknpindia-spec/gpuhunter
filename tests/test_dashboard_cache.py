"""Dashboard reads are single-flight and never block a request thread on slow DB I/O."""
import threading
import time
import unittest
from unittest.mock import patch

import app as webapp


class DashboardBundleCacheTests(unittest.TestCase):
    def setUp(self):
        # Ensure any previous background worker from a test has returned its lock.
        deadline = time.monotonic() + 3
        while webapp._BUNDLE_READ_LOCK.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(webapp._BUNDLE_READ_LOCK.locked(), "previous dashboard refresh did not finish")
        webapp._BUNDLE_CACHE.update(bundle=None, at=0.0)

    def wait_for_refresh(self):
        deadline = time.monotonic() + 3
        while webapp._BUNDLE_READ_LOCK.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(webapp._BUNDLE_READ_LOCK.locked(), "background dashboard refresh timed out")

    def test_cold_cache_starts_one_background_read_and_populates_cache(self):
        calls = []
        done = threading.Event()

        def read(**kw):
            calls.append(1)
            try:
                return {"settings": {}}
            finally:
                done.set()

        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=read):
            for _ in range(20):
                try:
                    webapp._dashboard_bundle()
                except RuntimeError:
                    pass  # expected while the cold cache is being populated
            self.assertTrue(done.wait(2))
            self.wait_for_refresh()
            for _ in range(20):
                self.assertEqual(webapp._dashboard_bundle()["settings"], {})
        self.assertEqual(len(calls), 1)

    def test_concurrent_polls_do_not_queue_behind_a_slow_read(self):
        release = threading.Event()
        calls = []

        def slow(**kw):
            calls.append(1)
            release.wait(5)
            return {"settings": {"n": len(calls)}}

        webapp._BUNDLE_CACHE.update(bundle={"settings": {"n": 0}}, at=time.monotonic() - 60)
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=slow):
            started = time.monotonic()
            self.assertEqual(webapp._dashboard_bundle()["settings"]["n"], 0)
            self.assertLess(time.monotonic() - started, 0.5)
            deadline = time.monotonic() + 1
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(calls), 1)
            started = time.monotonic()
            self.assertEqual(webapp._dashboard_bundle()["settings"]["n"], 0)
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            self.wait_for_refresh()
        self.assertEqual(len(calls), 1)
        self.assertEqual(webapp._dashboard_bundle()["settings"]["n"], 1)

    def test_database_failure_serves_last_good_bundle_and_logs_it(self):
        webapp._BUNDLE_CACHE.update(bundle={"settings": {"ok": 1}}, at=time.monotonic() - 60)
        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=RuntimeError("Database connection pool is busy")):
            with self.assertLogs("app", level="WARNING"):
                self.assertEqual(webapp._dashboard_bundle()["settings"], {"ok": 1})
                self.wait_for_refresh()
            # Failure doesn't destroy the last known-good data.
            self.assertEqual(webapp._dashboard_bundle()["settings"], {"ok": 1})

    def test_database_failure_without_cache_returns_immediate_cold_fallback_signal(self):
        finished = threading.Event()

        def fail(**kw):
            try:
                raise RuntimeError("down")
            finally:
                finished.set()

        with patch.object(webapp.db, "get_dashboard_bundle", side_effect=fail):
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                webapp._dashboard_bundle()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(finished.wait(2))
            self.wait_for_refresh()


if __name__ == "__main__":
    unittest.main()
