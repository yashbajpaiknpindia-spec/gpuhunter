"""Regression: scan stuck at "Searching preferred Vast offers" (12%) with no opportunities.

Root cause: no overall time limit on the Vast offer search, so one stalled provider call
kept the scan lock forever. These tests reproduce the stall with real sockets.
"""
import http.server
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gph.config import Config
from gph.db import Database
from gph.engine import OpportunityScanner
from gph.providers.vast import VastClient, VastAPIError
from gph.supervisor import Supervisor


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "silent"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.mode == "silent":      # accepts the request, never answers
            time.sleep(30)
            return
        if self.mode == "trickle":     # keeps sending bytes so the read timeout never fires
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(100000):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except Exception:
                pass


def _serve(mode):
    handler = type("H", (_Handler,), {"mode": mode})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class VastSearchDeadlineTests(unittest.TestCase):
    def _run(self, mode, deadline):
        server = _serve(mode)
        try:
            client = VastClient("fake", f"http://127.0.0.1:{server.server_port}/api/v0", timeout=20, search_deadline=deadline)
            started = time.monotonic()
            result = client.search_offers_with_diagnostics(limit=10)
            return result, time.monotonic() - started
        finally:
            server.shutdown()

    def test_silent_server_cannot_hang_search(self):
        result, elapsed = self._run("silent", deadline=2)
        self.assertLess(elapsed, 10)
        self.assertEqual(result["offers"], [])
        self.assertTrue(result["strict_error"])

    def test_trickling_response_cannot_hang_search(self):
        result, elapsed = self._run("trickle", deadline=3)
        self.assertLess(elapsed, 12)
        self.assertEqual(result["offers"], [])
        self.assertTrue(result["strict_error"])


class _FakeWTM:
    def get_gpus(self):
        return [{"name": "RTX 4090", "algorithms": []}]

    def get_coins(self):
        return []

    def calculate_gpu(self, bench):
        return []


class _FailingVast:
    def search_offers_with_diagnostics(self, **kwargs):
        return {"offers": [], "strict_error": "Vast offer search did not finish within 45s", "fallback_error": None,
                "strict_count": 0, "fallback_count": 0, "query_stage": "empty"}


class ScanFailureKeepsDataTests(unittest.TestCase):
    def test_provider_failure_aborts_scan_and_keeps_previous_opportunities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "keep.sqlite3")
            cfg = Config(database_path=path, vast_api_key="k", whattomine_api_token="t")
            db = Database(path)
            prior = {"id": "abc", "scanned_at": "2026-01-01T00:00:00+00:00", "offer_id": 1, "coin_tag": "BTC",
                     "expected_net_day_usd": 1.0}
            db.replace_opportunities([prior])
            scanner = OpportunityScanner(cfg, db)
            scanner._vast_client, scanner._wtm_client = _FailingVast(), _FakeWTM()
            with self.assertRaises(RuntimeError) as ctx:
                scanner.scan()
            self.assertIn("previous results were kept", str(ctx.exception))
            self.assertEqual(len(db.get_opportunities()), 1)


class ScanWatchdogTests(unittest.TestCase):
    def test_stalled_scan_fails_fast_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "wd.sqlite3")
            cfg = Config(database_path=path, vast_api_key="k", whattomine_api_token="t")
            db = Database(path)
            sup = Supervisor(cfg, db)
            sup.scan_hard_timeout_override = 1.5

            class Stalled:
                def scan(self, progress_callback=None):
                    progress_callback("Searching preferred Vast offers", 12, "x")
                    time.sleep(5)  # a stalled provider call
                    progress_callback("Saving scan results", 94, "must never run")
                    raise AssertionError("abandoned scan continued")

            sup.scanner = Stalled()
            started = time.monotonic()
            self.assertTrue(sup.start_scan_async())
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                p = sup.current_ui_progress()
                if p.get("status") == "failed":
                    break
                time.sleep(0.1)
            self.assertEqual(sup.current_ui_progress().get("status"), "failed")
            self.assertIn("timed out", sup.current_ui_progress().get("detail", ""))
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(0.3)
            # The lock is free, so the next scan starts fresh instead of "attaching" to a dead one.
            self.assertTrue(sup._scan_lock.acquire(blocking=False))
            sup._scan_lock.release()
            time.sleep(4.5)  # let the abandoned worker finish inside the temp dir


if __name__ == "__main__":
    unittest.main()
