from __future__ import annotations

import json
import re
import statistics
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .config import Config, projected_commitment_hours, runtime_limit_reached
from .db import Database, now_iso
from .engine import OpportunityScanner, _algokey, _offer_safety_reasons
from .monitor import RemoteMinerMonitor
from .profiles import load_profiles, match_gpu_name, wallet_for_profile
from .pool_metrics import PoolMetricsClient
from .providers.vast import VastClient, VastAPIError, VastRateLimitError, format_duration
from .providers.kryptex import KryptexPoolClient
from .profit import evaluate_profit, offer_hourly_cost, parse_num


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _mask(text: str) -> str:
    if not text:
        return ""
    if len(text) <= 8:
        return "[configured]"
    return f"{text[:4]}…{text[-4:]}"


class Supervisor:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.scanner = OpportunityScanner(config, db)
        self.monitor = RemoteMinerMonitor(config)
        self.pool_metrics = PoolMetricsClient()
        self.kryptex = KryptexPoolClient(timeout=4)
        self._last_kryptex_poll = 0.0
        self._kryptex_lock = threading.Lock()  # serializes upstream refreshes
        self._kryptex_status_lock = threading.Lock()  # protects status reads without waiting on network I/O
        self._kryptex_config_lock = threading.Lock()
        self._kryptex_generation = 0
        self._kryptex_status_cache: dict[str, Any] = {}
        self._kryptex_refresh_state_lock = threading.Lock()
        self._kryptex_refreshing = False
        self._kryptex_refresh_started_at: str | None = None
        self._kryptex_refresh_finished_at: str | None = None
        self._last_kryptex_gpu_poll = 0.0
        self._kryptex_gpu_lock = threading.Lock()  # serializes refresh work only
        self._kryptex_gpu_cache_lock = threading.Lock()  # readers never wait for provider I/O
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.monitor_thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._last_scan_monotonic = 0.0
        self._vast_balance_lock = threading.Lock()
        self._balance_refresh_state_lock = threading.Lock()
        self._balance_refreshing = False
        self._balance_refresh_started_at: str | None = None
        self._balance_refresh_finished_at: str | None = None
        self._last_vast_balance_poll = 0.0
        self._ui_progress_lock = threading.Lock()
        self._ui_progress: dict[str, Any] = {}
        self._vast_balance_cache: dict[str, Any] = {}
        self._kryptex_gpu_market_cache: dict[str, Any] = {}
        # Read persisted dashboard snapshots once at boot; repeated browser polling
        # must not consume PostgreSQL connections merely to display cached telemetry.
        try:
            # Settings only: the old call also pulled 2,000+ opportunity payloads at boot.
            settings = self.db.get_all_settings()
        except Exception:
            settings = {}
        saved_progress = settings.get("ui_operation_progress", {}) or {}
        self._ui_progress = saved_progress if isinstance(saved_progress, dict) else {}
        saved_balance = settings.get("vast_balance_status", {}) or {}
        self._vast_balance_cache = saved_balance if isinstance(saved_balance, dict) else {}
        saved_kryptex = settings.get("kryptex_status", {}) or {}
        self._kryptex_status_cache = saved_kryptex if isinstance(saved_kryptex, dict) else {}
        saved_gpu_market = settings.get("kryptex_gpu_market", {}) or {}
        self._kryptex_gpu_market_cache = saved_gpu_market if isinstance(saved_gpu_market, dict) else {}
        # The public Kryptex pool identity (not a secret) can be entered in the UI.
        # Render environment variables remain the preferred override when supplied.
        saved_kryptex_config = settings.get("kryptex_connection_config", {}) or {}
        if isinstance(saved_kryptex_config, dict) and str(saved_kryptex_config.get("username", "")).strip():
            # An explicitly saved dashboard configuration takes precedence after a
            # restart; environment variables are the fallback for first-time setup.
            self.config.kryptex_mining_username = str(saved_kryptex_config.get("username", "")).strip()
            self.config.kryptex_coin = str(saved_kryptex_config.get("coin", "prl")).strip().lower() or "prl"
            self.config.kryptex_worker = str(saved_kryptex_config.get("worker", "")).strip()
        self._monitor_sync_failures = 0
        self._monitor_cycle_lock = threading.Lock()

    def start(self) -> None:
        # Keep provider scans and live monitoring on different workers. A slow or
        # rate-limited market API must never block safety checks on paid instances.
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.run_loop, name="gph-supervisor", daemon=True)
            self.thread.start()
        # Always keep managing existing paid instances whenever a Vast key is present,
        # even if the operator switches future rentals back to scanner-only mode.
        if self.config.vast_api_key:
            if not self.monitor_thread or not self.monitor_thread.is_alive():
                self.monitor_thread = threading.Thread(target=self.run_monitor_loop, name="gph-instance-monitor", daemon=True)
                self.monitor_thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def live_blockers(self) -> list[str]:
        return self.config.live_blockers(load_profiles(self.config.miner_profiles_path))

    def set_ui_progress(self, action: str, stage: str, percent: int, detail: str = "", status: str = "running", *, persist: bool = True) -> None:
        snapshot = {
            "action": action, "stage": stage, "percent": max(0, min(100, int(percent))),
            "detail": str(detail)[:300], "status": status, "updated_at": now_iso(),
        }
        # Retain compatibility with lightweight Supervisor instances used by unit
        # tests and small integrations that construct the object without __init__.
        if not hasattr(self, "_ui_progress_lock"):
            self._ui_progress_lock = threading.Lock()
        with self._ui_progress_lock:
            self._ui_progress = snapshot
        # Progress updates must not fail the task just because persistence is slow.
        if persist:
            try:
                self.db.set_setting("ui_operation_progress", snapshot)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Could not persist UI progress: %s", exc.__class__.__name__)

    def current_ui_progress(self) -> dict[str, Any]:
        if not hasattr(self, "_ui_progress_lock"):
            self._ui_progress_lock = threading.Lock()
        with self._ui_progress_lock:
            return dict(getattr(self, "_ui_progress", {}))

    def scan(self) -> dict[str, Any]:
        """Run a market scan synchronously for internal supervisor callers."""
        with self._scan_lock:
            return self._scan_locked()

    def start_scan_async(self) -> bool:
        """Reserve the scan lock and execute the scan on a daemon worker.

        The HTTP handler returns immediately, avoiding Render/proxy request timeouts
        while the server makes several provider calls. False means another scan owns
        the lock; callers should poll the existing progress state instead.
        """
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            # A manual scan must be acknowledged without waiting for a database
            # connection. The browser can start polling immediately after this.
            self.set_ui_progress("Market scan", "Starting scan", 1, "Scan accepted; starting provider requests", "running", persist=False)
            worker = threading.Thread(target=self._scan_async_worker, name="gph-manual-market-scan", daemon=True)
            worker.start()
        except Exception:
            self._scan_lock.release()
            raise
        return True

    def _scan_async_worker(self) -> None:
        try:
            self._scan_locked()
        except VastRateLimitError as exc:
            self._record_search_paused(exc)
        except Exception as exc:
            message = str(exc)[:300]
            # Update memory first so polling can show the real failure even when
            # PostgreSQL is the component that failed.
            self.set_ui_progress("Market scan", "Failed", 100, message, "failed", persist=False)
            try:
                self.db.log("ERROR", "market_scan_failed", {"error": message})
                self.set_ui_progress("Market scan", "Failed", 100, message, "failed")
            except Exception:
                # Preserve the original exception in Python/Gunicorn logs even if DB
                # persistence itself is the reason the task failed.
                import logging
                logging.getLogger(__name__).exception("Async market scan and failure persistence both failed")
        finally:
            self._scan_lock.release()

    def _record_search_paused(self, exc: VastRateLimitError) -> None:
        """Vast refused (or we withheld) a search because of its 429 window / row quota.

        This is an expected pause, not a crash: no traceback, one WARNING event, and the
        dashboard shows when searching resumes. The last good opportunities stay on screen.
        """
        import logging
        message = str(exc)[:300]
        logging.getLogger(__name__).warning("Vast search paused: %s", message)
        self.set_ui_progress("Market scan", "Paused - Vast search quota", 100, message, "failed", persist=False)
        try:
            self.db.log("WARNING", "vast_search_paused", {"error": message, "retry_after_seconds": int(exc.retry_after), "quota": bool(exc.quota)})
            self.set_ui_progress("Market scan", "Paused - Vast search quota", 100, message, "failed")
        except Exception:
            logging.getLogger(__name__).warning("Could not persist Vast search pause because the database is unavailable")

    def _vast_search_status(self) -> dict[str, Any] | None:
        budget = getattr(self.scanner, "search_budget", None)
        if budget is None:
            return None
        status = budget.status()
        status["last_call"] = getattr(getattr(self.scanner, "_vast_client", None), "last_call", None)
        keys = getattr(self.scanner, "search_status", None)
        if callable(keys) and len(getattr(self.scanner, "_extra_slots", ()) or ()) > 0:
            status["search_keys"] = keys()
        return status

    def effective_scan_interval(self) -> float:
        """Seconds to wait between automatic scans.

        The configured interval is a floor. Vast meters searches by rows against a daily quota,
        so the interval is stretched so the remaining rows last until the reset, and it never
        ends before an active 429 cool-down does.
        """
        interval = float(max(60, int(self.config.scan_interval_seconds)))
        budget = getattr(self.scanner, "search_budget", None)
        pacing = getattr(self.scanner, "search_pacing", None)
        if callable(pacing) and getattr(self.scanner, "_extra_slots", None):
            recommended, cooldown = pacing()  # the search key that can search soonest
            interval = max(interval, recommended, cooldown)
        elif budget is not None:
            interval = max(interval, budget.recommended_interval(), budget.cooldown_remaining())
        return interval

    def _scan_locked(self) -> dict[str, Any]:
        self.set_ui_progress("Market scan", "Starting scan", 1, "Preparing provider clients", persist=False)
        # Intermediate stage changes are polled from memory. Persisting every progress
        # tick created unnecessary DB transactions/connection churn during provider I/O;
        # the final result and terminal progress are still written durably below.
        #
        # The provider work runs under a hard wall-clock watchdog. Previously a single
        # stalled provider call (DNS, TLS, a slow-drip response) kept this method - and
        # therefore the scan lock - forever: the dashboard sat at "Searching preferred
        # Vast offers 12%" and no opportunity was ever saved. Now the scan fails with a
        # clear message after SCAN_HARD_TIMEOUT_SECONDS and the lock is always released.
        deadline = getattr(self, "scan_hard_timeout_override", None) or max(60, int(os.getenv("SCAN_HARD_TIMEOUT_SECONDS", "240")))
        self._scan_generation = getattr(self, "_scan_generation", 0) + 1
        generation = self._scan_generation
        last_stage = {"stage": "Starting scan", "percent": 1}

        def guarded_progress(stage: str, percent: int, detail: str) -> None:
            if generation != self._scan_generation:
                # This scan was abandoned by the watchdog; stop it before it can save stale data.
                raise RuntimeError("Market scan was abandoned after timing out")
            last_stage.update(stage=stage, percent=percent)
            self.set_ui_progress("Market scan", stage, percent, detail, persist=False)

        box: dict[str, Any] = {}

        def run_scanner() -> None:
            try:
                box["result"] = self.scanner.scan(progress_callback=guarded_progress)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
                box["error"] = exc

        worker = threading.Thread(target=run_scanner, name="gph-scan-worker", daemon=True)
        worker.start()
        worker.join(deadline)
        if worker.is_alive():
            self._scan_generation += 1  # invalidates guarded_progress for the abandoned worker
            raise RuntimeError(
                f"Market scan timed out after {deadline}s while at '{last_stage['stage']}' ({last_stage['percent']}%). "
                "A provider request stalled; the previous results were kept. Try again, and check the Event log."
            )
        if "error" in box:
            raise box["error"]
        result = box["result"]
        self._last_scan_monotonic = time.monotonic()
        self.db.set_setting("last_scan_at", result.get("scanned_at"))
        self.db.set_setting("last_scan_summary", {
            k: result.get(k) for k in (
                "offers_seen", "gpu_benchmarks", "coins_seen", "opportunities", "qualified",
                "unknown_gpu_models", "scanned_at", "errors", "provider_diagnostics", "rejection_counts",
            )
        } | {"offer_review_count": len(result.get("offer_diagnostics") or [])})
        self.db.set_setting("last_gpu_models_seen", result.get("gpu_models_seen", []))
        # Reference refresh is deliberately decoupled from the interactive scan.
        threading.Thread(target=self.refresh_kryptex_gpu_market, kwargs={"force": False}, name="gph-kryptex-gpu-market", daemon=True).start()
        self.set_ui_progress("Market scan", "Complete", 100, f"{result.get('offers_seen', 0)} offers scanned; results saved", "complete")
        self._refresh_managed_profit_context(result.get("scanned_at"))
        return result

    def vast_balance_status(self, force: bool = False) -> dict[str, Any]:
        """Read the Vast credit balance using the account's read-only current-user API.

        Dashboard state reads the cached snapshot so page loads never wait on the
        provider. The background supervisor and explicit refresh button force a live
        query. Balance is display-only and is never treated as mining revenue.
        """
        if not self.config.vast_api_key:
            return {"configured": False, "available": False, "balance_usd": None,
                    "updated_at": None, "error": "VAST_API_KEY is not configured in the service environment."}
        cached = dict(self._vast_balance_cache or {})
        with self._balance_refresh_state_lock:
            refreshing = self._balance_refreshing
            refresh_started_at = self._balance_refresh_started_at
        if not force:
            if isinstance(cached, dict) and cached:
                return {**cached, "refreshing": refreshing, "refresh_started_at": refresh_started_at}
            return {
                "configured": True, "available": False, "balance_usd": None,
                "updated_at": None, "refreshing": refreshing, "refresh_started_at": refresh_started_at,
                "error": "Balance check is in progress." if refreshing else "Balance has not been checked yet. Use Refresh balance."}
        with self._vast_balance_lock:
            cached = dict(self._vast_balance_cache or {})
            try:
                # This read-only call uses a short timeout. The dashboard starts it
                # in a worker and polls status instead of tying up a request thread.
                user = VastClient(self.config.vast_api_key, self.config.vast_api_base, timeout=6).get_current_user()
                raw_account_id = user.get("id", user.get("user_id", user.get("uid")))
                account_id = str(raw_account_id).strip() if raw_account_id is not None else ""
                expected_account_id = str(getattr(self.config, "vast_expected_account_id", "") or "").strip()
                if not account_id:
                    raise ValueError("Vast current-user response did not include an account ID; balance is not verified")
                if expected_account_id and account_id != expected_account_id:
                    raise ValueError(f"Vast API key belongs to account ID {account_id}, not the configured expected account; balance is not verified")
                raw_balance = user.get("balance")
                if raw_balance is None or isinstance(raw_balance, bool):
                    raw_balance = user.get("credit")
                if raw_balance is None or isinstance(raw_balance, bool):
                    raise ValueError("Vast current-user response did not include a numeric balance or credit field")
                balance = float(raw_balance)
                if not (balance == balance and abs(balance) != float("inf")):
                    raise ValueError("Vast returned a non-finite account balance")
                result = {
                    "configured": True, "available": True, "balance_usd": round(balance, 4),
                    "account_username": str(user.get("username") or "")[:80],
                    "account_id": account_id,
                    "account_verified": True,
                    "updated_at": now_iso(), "last_success_at": now_iso(), "error": None, "refreshing": False,
                }
                self._vast_balance_cache = dict(result)
                try:
                    self.db.set_setting("vast_balance_status", result)
                except Exception as persist_exc:
                    import logging
                    logging.getLogger(__name__).warning("Could not persist Vast balance snapshot: %s", persist_exc.__class__.__name__)
                self._last_vast_balance_poll = time.monotonic()
                return result
            except Exception as exc:
                # Keep last-known dollars available only as explicitly stale context.
                result = {
                    "configured": True, "available": False,
                    "balance_usd": (cached.get("balance_usd") if isinstance(cached, dict) else None),
                    "account_username": (cached.get("account_username", "") if isinstance(cached, dict) else ""),
                    "account_id": (cached.get("account_id") if isinstance(cached, dict) else None),
                    "account_verified": False,
                    "updated_at": (cached.get("updated_at") if isinstance(cached, dict) else None),
                    "last_success_at": (cached.get("last_success_at") if isinstance(cached, dict) else None),
                    "error": str(exc)[:240], "refreshing": False,
                }
                self._vast_balance_cache = dict(result)
                try:
                    self.db.set_setting("vast_balance_status", result)
                except Exception as persist_exc:
                    import logging
                    logging.getLogger(__name__).warning("Could not persist failed Vast balance check: %s", persist_exc.__class__.__name__)
                self._last_vast_balance_poll = time.monotonic()
                try:
                    self.db.log("WARNING", "vast_balance_refresh_failed", {"error": str(exc)[:220]})
                except Exception:
                    pass
                return result

    def request_vast_balance_refresh(self) -> dict[str, Any]:
        """Launch a read-only balance lookup and immediately return its status."""
        if not self.config.vast_api_key:
            return self.vast_balance_status(force=False)
        already_running = False
        with self._balance_refresh_state_lock:
            if self._balance_refreshing:
                already_running = True
            else:
                self._balance_refreshing = True
                self._balance_refresh_started_at = now_iso()
                self._balance_refresh_finished_at = None
        if already_running:
            return self.vast_balance_refresh_state()
        try:
            threading.Thread(target=self._run_vast_balance_refresh, name="gph-manual-vast-balance", daemon=True).start()
        except Exception:
            with self._balance_refresh_state_lock:
                self._balance_refreshing = False
                self._balance_refresh_finished_at = now_iso()
            raise
        return self.vast_balance_refresh_state()

    def _run_vast_balance_refresh(self) -> None:
        try:
            self.vast_balance_status(force=True)
        finally:
            with self._balance_refresh_state_lock:
                self._balance_refreshing = False
                self._balance_refresh_finished_at = now_iso()

    def vast_balance_refresh_state(self) -> dict[str, Any]:
        status = self.vast_balance_status(force=False)
        with self._balance_refresh_state_lock:
            status["refreshing"] = self._balance_refreshing
            status["refresh_started_at"] = self._balance_refresh_started_at
            status["refresh_finished_at"] = self._balance_refresh_finished_at
        return status

    def target_instances(self, settings: dict[str, Any] | None = None) -> int:
        try:
            configured = int((settings or {}).get("target_active_instances", 1) if settings is not None else self.db.get_setting("target_active_instances", 1))
        except (TypeError, ValueError):
            configured = 1
        return max(0, min(configured, 25))

    def auto_rent_is_enabled(self, settings: dict[str, Any] | None = None) -> bool:
        """This deployment is intentionally manual-setup/monitor-only: never create rentals."""
        return False

    def automation_state(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "auto_scan_enabled": bool(self.config.auto_scan_enabled),
            "scan_interval_seconds": max(60, int(self.config.scan_interval_seconds)),
            "effective_scan_interval_seconds": int(self.effective_scan_interval()),
            "vast_search": self._vast_search_status(),
            "kryptex_poll_interval_seconds": max(60, int(self.config.kryptex_poll_interval_seconds)),
            "target_active_instances": self.target_instances(settings=settings),
            "max_active_instances_cap": 25,
            "legacy_max_active_instances_setting": max(1, int(self.config.max_active_instances)),
            "auto_rent_enabled": False,
            "manual_setup_only": True,
            "live_mode": self.config.automation_mode == "live" and self.config.enable_live_mining,
        }

    def cached_kryptex_status(self) -> dict[str, Any]:
        """Return an in-memory snapshot: dashboard requests never open a DB connection."""
        with self._kryptex_status_lock:
            cached = dict(self._kryptex_status_cache or {})
        if cached:
            return cached
        if not (self.config.kryptex_mining_username and self.config.kryptex_coin):
            return {"configured": False, "connected": False,
                    "error": "Enter the public Kryptex mining username/wallet and coin below, then save and test the connection."}
        return {"configured": True, "connected": False,
                "username": _mask(self.config.kryptex_mining_username), "coin": self.config.kryptex_coin.upper(),
                "error": "Kryptex status has not been checked in this process yet."}

    def configure_kryptex(self, username: str, coin: str = "prl", worker: str = "") -> dict[str, Any]:
        """Persist a public Kryptex pool identity and activate it in this process.

        This does not require an API key; it is a public pool lookup. Wallet seed phrases,
        private keys, and account passwords are neither accepted nor needed.
        """
        username = str(username or "").strip()
        coin = str(coin or "prl").strip().lower()
        worker = str(worker or "").strip()
        if not username:
            raise ValueError("Kryptex mining username or public payout wallet is required")
        if len(username) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+:/%-]{0,199}", username):
            raise ValueError("Kryptex username/wallet must be 1–200 letters, digits or standard wallet characters; do not enter a password or seed phrase")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,15}", coin):
            raise ValueError("Coin must be a Kryptex pool slug such as prl or qtc")
        if worker and (len(worker) > 32 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", worker)):
            raise ValueError("Worker name must be 1–32 letters, digits, dots, dashes or underscores")
        saved = {"username": username, "coin": coin, "worker": worker, "updated_at": now_iso(), "source": "dashboard"}
        # Persist first; if PostgreSQL is unavailable, do not claim a durable config save.
        self.db.set_setting("kryptex_connection_config", saved)
        with self._kryptex_config_lock:
            self._kryptex_generation += 1
            self.config.kryptex_mining_username = username
            self.config.kryptex_coin = coin
            self.config.kryptex_worker = worker
            self.kryptex = KryptexPoolClient(timeout=4)
            self._last_kryptex_poll = 0.0
        with self._kryptex_status_lock:
            self._kryptex_status_cache = {"configured": True, "connected": False, "coin": coin.upper(),
                                          "username": _mask(username), "worker_filter": worker or None,
                                          "updated_at": None, "error": "Configuration saved; testing the public Kryptex Pool API."}
        return {"configured": True, "coin": coin.upper(), "username": _mask(username), "worker_configured": bool(worker)}

    def kryptex_status(self, force: bool = False) -> dict[str, Any]:
        """Refresh pool/account telemetry using a stable config snapshot.

        A dashboard edit may replace the public wallet/coin while an older request is
        in flight. Generation checks keep that stale response from overwriting the new
        identity's status, and cached reads never hold the provider/network lock.
        """
        with self._kryptex_config_lock:
            username = str(self.config.kryptex_mining_username or "")
            coin = str(self.config.kryptex_coin or "prl")
            worker = str(self.config.kryptex_worker or "")
            client = self.kryptex
            generation = self._kryptex_generation
        if not (username and coin):
            return self.cached_kryptex_status()

        current = time.monotonic()
        with self._kryptex_status_lock:
            cached = dict(self._kryptex_status_cache or {})
        elapsed = current - self._last_kryptex_poll
        ttl = max(60, int(self.config.kryptex_poll_interval_seconds))
        if not force and cached and elapsed < ttl:
            return cached

        with self._kryptex_lock:
            with self._kryptex_config_lock:
                # Configuration can change while waiting for the network lock.
                username = str(self.config.kryptex_mining_username or "")
                coin = str(self.config.kryptex_coin or "prl")
                worker = str(self.config.kryptex_worker or "")
                client = self.kryptex
                generation = self._kryptex_generation
            current = time.monotonic()
            with self._kryptex_status_lock:
                cached = dict(self._kryptex_status_cache or {})
            if not force and cached and current - self._last_kryptex_poll < ttl:
                return cached
            status = client.account_status(coin, username, worker)
            with self._kryptex_config_lock:
                config_unchanged = generation == self._kryptex_generation
            self._last_kryptex_poll = time.monotonic()
            if not config_unchanged:
                # Never publish a result belonging to the prior public identity.
                return self.cached_kryptex_status()
            with self._kryptex_status_lock:
                self._kryptex_status_cache = dict(status)

        # Telemetry must still be returned if audit storage is degraded.
        try:
            self.db.set_setting("kryptex_status", status)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Could not persist Kryptex status snapshot: %s", exc.__class__.__name__)
        try:
            if status.get("connected"):
                self.db.log("INFO", "kryptex_status_refreshed", {"coin": status.get("coin"), "worker_count": status.get("worker_count"), "source": status.get("source")})
            else:
                self.db.log("WARNING", "kryptex_connection_issue", {"error": status.get("error", "unknown")})
        except Exception:
            pass
        return dict(status)

    def request_kryptex_refresh(self) -> dict[str, Any]:
        """Start public API lookups in a daemon worker and return without blocking a web thread."""
        if not (self.config.kryptex_mining_username and self.config.kryptex_coin):
            return self.cached_kryptex_status() | {"refreshing": False}
        with self._kryptex_refresh_state_lock:
            already_running = self._kryptex_refreshing
            if not already_running:
                self._kryptex_refreshing = True
                self._kryptex_refresh_started_at = now_iso()
                self._kryptex_refresh_finished_at = None
        if already_running:
            return self.kryptex_refresh_state()
        try:
            threading.Thread(target=self._run_kryptex_refresh, name="gph-kryptex-account-refresh", daemon=True).start()
        except Exception:
            with self._kryptex_refresh_state_lock:
                self._kryptex_refreshing = False
                self._kryptex_refresh_finished_at = now_iso()
            raise
        return self.kryptex_refresh_state()

    def _run_kryptex_refresh(self) -> None:
        try:
            self.kryptex_status(force=True)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Kryptex refresh worker failed")
            with self._kryptex_config_lock:
                username = str(self.config.kryptex_mining_username or "")
                coin = str(self.config.kryptex_coin or "prl")
            with self._kryptex_status_lock:
                self._kryptex_status_cache = {
                    "configured": bool(username), "connected": False,
                    "coin": coin.upper(), "username": _mask(username),
                    "error": str(exc)[:220], "updated_at": now_iso(),
                }
        finally:
            with self._kryptex_refresh_state_lock:
                self._kryptex_refreshing = False
                self._kryptex_refresh_finished_at = now_iso()

    def kryptex_refresh_state(self) -> dict[str, Any]:
        status = self.cached_kryptex_status()
        # A provider/database worker must never leave the browser spinner running
        # forever. Requests have short network timeouts, so a refresh older than 30s
        # is considered wedged and is surfaced as a terminal diagnostic. A late result
        # may still update the cache, but the UI can recover and request another check.
        with self._kryptex_refresh_state_lock:
            started = _dt(self._kryptex_refresh_started_at)
            age = (datetime.now(timezone.utc) - started).total_seconds() if started else 0
            if self._kryptex_refreshing and age > 30:
                self._kryptex_refreshing = False
                self._kryptex_refresh_finished_at = now_iso()
                status["connected"] = bool(status.get("connected"))
                status["error"] = "Kryptex refresh timed out after 30 seconds. No worker is assumed active; retry the read-only status check."
                status["updated_at"] = now_iso()
            status["refreshing"] = self._kryptex_refreshing
            status["refresh_started_at"] = self._kryptex_refresh_started_at
            status["refresh_finished_at"] = self._kryptex_refresh_finished_at
        return status

    def cached_kryptex_gpu_market(self) -> dict[str, Any]:
        with self._kryptex_gpu_cache_lock:
            return dict(self._kryptex_gpu_market_cache or {})

    def refresh_kryptex_gpu_market(self, force: bool = False) -> dict[str, Any]:
        """Poll optional Kryptex per-GPU references, disabled until that endpoint is verified.

        The documented public Kryptex Pool API provides pool/miner/coin telemetry but does not
        list the per-GPU `/api/v1/device/gpu/{slug}` endpoint used by older app versions. Keep
        that undocumented feed out of rental economics by default; the documented WhatToMine
        calculation remains the gross-revenue benchmark unless the operator explicitly opts in.
        """
        if not self.config.kryptex_gpu_reference_enabled:
            cached = self.cached_kryptex_gpu_market()
            disabled = {
                "enabled": False,
                "source": "Kryptex per-GPU benchmark endpoint (disabled until verified)",
                "updated_at": now_iso(),
                "attempted_models": 0,
                "available_models": 0,
                "missing_models": 0,
                "models": [],
                "errors": [],
                "note": "Not used for rental economics. Enable KRYPTEX_GPU_REFERENCE_ENABLED only after independently verifying the per-GPU endpoint and its field definitions.",
            }
            with self._kryptex_gpu_cache_lock:
                self._kryptex_gpu_market_cache = dict(disabled)
            if not isinstance(cached, dict) or cached.get("enabled") is not False or cached.get("models"):
                try:
                    self.db.set_setting("kryptex_gpu_market", disabled)
                except Exception:
                    pass
            return disabled
        interval = max(60, int(self.config.kryptex_gpu_scan_interval_seconds))
        current = time.monotonic()
        cached = self.db.get_setting("kryptex_gpu_market", {}) or {}
        if not force and cached and current - self._last_kryptex_gpu_poll < interval:
            return cached
        with self._kryptex_gpu_lock:
            current = time.monotonic()
            cached = self.db.get_setting("kryptex_gpu_market", {}) or {}
            if not force and cached and current - self._last_kryptex_gpu_poll < interval:
                return cached
            opportunities = self.db.get_opportunities(limit=2000)
            gpu_names: list[str] = []
            seen: set[str] = set()
            source_names = self.db.get_setting("last_gpu_models_seen", []) or []
            for name_value in list(source_names) + [x.get("gpu_name") for x in opportunities]:
                name = str(name_value or "").strip()
                key = name.casefold()
                if name and key not in seen:
                    seen.add(key)
                    gpu_names.append(name)
            previous_models = {str(x.get("gpu_name", "")).casefold(): x for x in (cached.get("models", []) if isinstance(cached, dict) else [])}
            models: list[dict[str, Any]] = []
            errors: list[str] = []
            if gpu_names:
                # Bound concurrency and API fan-out; each model's successful/missing response is
                # locally cached by KryptexPoolClient for the configured short reference-cache interval.
                with ThreadPoolExecutor(max_workers=6) as pool:
                    future_map = {pool.submit(self.kryptex.gpu_profitability, name): name for name in gpu_names}
                    for future in as_completed(future_map):
                        name = future_map[future]
                        try:
                            row = future.result()
                        except Exception as exc:
                            row = {"gpu_name": name, "available": False, "error": exc.__class__.__name__}
                            errors.append(f"{name}: {exc.__class__.__name__}")
                        models.append(row)
            models.sort(key=lambda x: str(x.get("gpu_name", "")).casefold())
            old_rows = {}
            for model in cached.get("models", []) if isinstance(cached, dict) else []:
                for coinrow in model.get("coin_rows", []) if isinstance(model, dict) else []:
                    key = (str(model.get("gpu_name", "")).casefold(), str(coinrow.get("coin", "")).upper(), str(coinrow.get("algorithm") or "").casefold())
                    old_rows[key] = {k: coinrow.get(k) for k in ("hashrate", "power_w", "daily_profit_usd", "daily_revenue_usd")}
            fluctuations = []
            for model in models:
                for coinrow in model.get("coin_rows", []) if isinstance(model, dict) else []:
                    key = (str(model.get("gpu_name", "")).casefold(), str(coinrow.get("coin", "")).upper(), str(coinrow.get("algorithm") or "").casefold())
                    before = old_rows.get(key)
                    if not before:
                        continue
                    changes = {}
                    for metric in ("hashrate", "power_w", "daily_profit_usd", "daily_revenue_usd"):
                        old = parse_num(before.get(metric), -1.0)
                        new = parse_num(coinrow.get(metric), -1.0)
                        if old > 0 and new >= 0:
                            delta = (new - old) / old * 100.0
                            threshold = 10.0 if metric in {"daily_profit_usd", "daily_revenue_usd"} else 15.0
                            if abs(delta) >= threshold:
                                changes[metric] = {"previous": old, "current": new, "change_pct": round(delta, 2)}
                    if changes:
                        fluctuations.append({"gpu_name": model.get("gpu_name"), "coin": coinrow.get("coin"), "algorithm": coinrow.get("algorithm"), "changes": changes})
            snapshot = {
                "enabled": True,
                "source": "Kryptex public GPU benchmark endpoint (operator-enabled)",
                "updated_at": now_iso(),
                "poll_interval_seconds": interval,
                "attempted_models": len(gpu_names),
                "available_models": sum(1 for x in models if x.get("available")),
                "missing_models": sum(1 for x in models if not x.get("available")),
                "models": models,
                "errors": errors[:20],
                "note": "Kryptex GPU references are supplementary. Daily revenue/profit is shown only when the API explicitly labels a daily field; unlabelled or annual figures are not treated as daily income.",
            }
            with self._kryptex_gpu_cache_lock:
                self._kryptex_gpu_market_cache = dict(snapshot)
            try:
                self.db.set_setting("kryptex_gpu_market", snapshot)
            except Exception as persist_exc:
                import logging
                logging.getLogger(__name__).warning("Could not persist Kryptex GPU reference snapshot: %s", persist_exc.__class__.__name__)
            self._last_kryptex_gpu_poll = time.monotonic()
            if fluctuations:
                self.db.log("WARNING", "kryptex_gpu_benchmark_fluctuation", {"count": len(fluctuations), "changes": fluctuations[:30], "source": snapshot["source"]})
            self.db.log("INFO", "kryptex_gpu_market_refreshed", {"attempted_models": snapshot["attempted_models"], "available_models": snapshot["available_models"], "missing_models": snapshot["missing_models"], "fluctuations": len(fluctuations), "errors": len(errors)})
            return snapshot

    def _refresh_managed_profit_context(self, scanned_at: str | None) -> None:
        """Renew running instances' market freshness from the latest scan, not the rental-time scan."""
        if not scanned_at:
            return
        candidates = self.db.get_opportunities(limit=2000)
        changed = 0
        for managed in self.db.get_managed_instances():
            if str(managed.get("status", "")).lower() == "destroyed":
                continue
            gpu = str(managed.get("gpu_name") or "").lower().replace("nvidia geforce ", "").replace("nvidia ", "")
            coin = str(managed.get("coin_tag") or "").upper()
            managed_algorithm = str((managed.get("metadata") or {}).get("algorithm") or "").strip()
            matches = [
                x for x in candidates
                if str(x.get("coin_tag") or "").upper() == coin
                and str(x.get("gpu_name") or "").lower().replace("nvidia geforce ", "").replace("nvidia ", "") == gpu
                and (not managed_algorithm or _algokey(x.get("algorithm")) == _algokey(managed_algorithm))
                # A new scan timestamp does not make old provider economics fresh.
                # Only refresh the rental's profitability clock when the underlying
                # revenue data itself is marked fresh and still yields positive revenue.
                and bool(x.get("data_fresh"))
                and parse_num(x.get("gross_day_usd"), 0) > 0
                and parse_num(x.get("conservative_gross_day_usd"), 0) > 0
            ]
            if not matches:
                continue
            best = max(matches, key=lambda x: float(x.get("conservative_gross_day_usd", 0) or 0))
            meta = managed.get("metadata", {}) or {}
            if not isinstance(meta, dict):
                meta = {}
            meta["last_profitability_scan_at"] = scanned_at
            meta["latest_coin_data_updated_at"] = best.get("data_updated_at")
            meta["latest_market_gross_day_usd"] = best.get("conservative_gross_day_usd")
            meta["latest_market_net_day_usd"] = best.get("expected_net_day_usd")
            meta["latest_market_all_in_hourly_usd"] = best.get("total_hourly_usd")
            managed["metadata_json"] = meta
            managed["expected_gross_day"] = float(best.get("gross_day_usd", managed.get("expected_gross_day", 0)) or 0)
            managed["latest_gross_day"] = float(best.get("conservative_gross_day_usd", managed.get("latest_gross_day", 0)) or 0)
            managed["latest_net_day"] = float(best.get("expected_net_day_usd", managed.get("latest_net_day", 0)) or 0)
            managed["last_monitor_at"] = now_iso()
            self.db.upsert_managed_instance(managed)
            changed += 1
        if changed:
            self.db.log("INFO", "managed_profitability_refreshed", {"instances": changed, "scanned_at": scanned_at})

    def _risk_state(self, instances: list[dict[str, Any]]) -> tuple[int, float]:
        active = 0
        projected_daily = 0.0
        for inst in instances:
            status = str(inst.get("actual_status") or inst.get("status") or "").lower()
            if status in {"running", "loading", "created", "starting", "booting"}:
                active += 1
                hourly = parse_num(inst.get("dph_total"), parse_num(inst.get("dph_base"), parse_num(inst.get("hourly_cost"), 0)))
                if hourly > 0:
                    projected_daily += hourly * 24
        return active, projected_daily

    def _profile_for_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        profile_id = item.get("profile_id")
        for profile in load_profiles(self.config.miner_profiles_path):
            if profile.get("enabled") and profile.get("id") == profile_id:
                return profile
        return None

    def adopt_manual_instance(self, instance_id: int, profile_id: str, coin_tag: str = "", algorithm: str = "") -> dict[str, Any]:
        """Enroll one operator-created, currently running Vast instance for safety monitoring.

        This method never starts or reconfigures a miner. Enrollment is explicit because
        the monitor may permanently destroy the selected instance and its local disk.
        """
        if not self.config.vast_api_key:
            raise RuntimeError("VAST_API_KEY is missing; the instance cannot be safely monitored")
        if not self.config.ssh_private_key_path or not os.path.isfile(os.path.expanduser(self.config.ssh_private_key_path)):
            raise RuntimeError("SSH_PRIVATE_KEY_PATH must point to an installed private key before enrolling a manual instance")

        profile_id = str(profile_id or "").strip()
        profiles = load_profiles(self.config.miner_profiles_path)
        profile = next((p for p in profiles if str(p.get("id") or "").strip() == profile_id and p.get("enabled")), None)
        if not profile:
            raise RuntimeError("Choose an enabled monitoring profile from data/mining_profiles.json")
        pool_cfg = profile.get("pool_metrics") if isinstance(profile.get("pool_metrics"), dict) else {}
        if not (str(pool_cfg.get("url") or "").strip()
                and str(pool_cfg.get("revenue_24h_usd_path") or "").strip()
                and str(pool_cfg.get("source_updated_at_path") or "").strip()):
            raise RuntimeError("The selected profile needs an HTTPS pool-metrics URL, current USD/day revenue path, and source_updated_at_path")
        tags = [str(x).strip().upper() for x in profile.get("coin_tags", []) if str(x).strip()]
        selected_coin = str(coin_tag or (tags[0] if len(tags) == 1 and tags[0] != "*" else "")).strip().upper()
        if not selected_coin:
            raise RuntimeError("A coin tag is required; use one coin and algorithm per monitoring profile or enter the coin tag")
        if tags and "*" not in tags and selected_coin not in tags:
            raise RuntimeError(f"Coin {selected_coin} is not allowed by the selected profile")
        algos = [str(x).strip() for x in profile.get("algorithms", []) if str(x).strip()]
        selected_algorithm = str(algorithm or (algos[0] if len(algos) == 1 and algos[0] != "*" else "")).strip()
        if not selected_algorithm:
            raise RuntimeError("An algorithm is required; use one coin and algorithm per monitoring profile or enter the algorithm")
        if algos and "*" not in algos and _algokey(selected_algorithm) not in {_algokey(x) for x in algos}:
            raise RuntimeError(f"Algorithm {selected_algorithm} is not allowed by the selected profile")

        current_instances = self._current_instances()
        provider_instance = None
        for item in current_instances:
            try:
                current_id = int(item.get("id") or item.get("instance_id") or item.get("contract_id"))
            except (TypeError, ValueError):
                continue
            if current_id == int(instance_id):
                provider_instance = item
                break
        if provider_instance is None:
            raise RuntimeError("That instance is not present in the latest live Vast listing; refresh and try again")
        status = str(provider_instance.get("actual_status") or provider_instance.get("status") or "unknown").lower()
        if status not in {"running", "active"}:
            raise RuntimeError(f"Only a running instance can be enrolled; Vast currently reports {status}")
        existing = self.db.get_managed_instance(int(instance_id))
        if existing and str(existing.get("status") or "").lower() == "destroyed":
            raise RuntimeError("Local history marks this instance destroyed; refusing to re-enroll a conflicting instance ID")
        if existing:
            existing_meta = self._metadata_dict(existing)
            if existing_meta.get("manual_setup") and str(existing.get("miner_profile_id") or "") == profile_id:
                return {"instance_id": int(instance_id), "already_monitored": True}
            raise RuntimeError("This instance is already tracked. Do not reassign a running instance to another monitor profile.")

        gpu_name = str(provider_instance.get("gpu_name") or provider_instance.get("gpu_name_display") or "Unknown GPU")
        gpu_models = profile.get("gpu_models", ["*"]) or ["*"]
        if "*" not in [str(x).strip() for x in gpu_models] and not any(match_gpu_name(gpu_name, str(x)) for x in gpu_models):
            raise RuntimeError(f"Profile {profile_id} is not configured for this instance GPU ({gpu_name})")
        hourly_rate = parse_num(provider_instance.get("dph_total"), parse_num(provider_instance.get("dph_base"), 0.0))
        if hourly_rate <= 0:
            raise RuntimeError("Vast did not report a positive current all-in hourly rate; profitability cannot be verified")

        wallet = wallet_for_profile(profile, self.config.wallets_path)
        if not wallet:
            raise RuntimeError(f"Missing payout wallet for the selected profile wallet_key={profile.get('wallet_key')}")
        worker = str(profile.get("worker") or "gph-worker").strip()
        pool_report = self.pool_metrics.fetch(profile, wallet, worker, selected_coin)
        pool_run_rate = parse_num(pool_report.get("revenue_24h_usd"), -1.0)
        if pool_run_rate < 0:
            raise RuntimeError("Pool API did not provide a valid current revenue run-rate; manual monitoring cannot be enabled")

        opportunities = self.db.get_opportunities(limit=2000)
        matching = [
            row for row in opportunities
            if bool(row.get("data_fresh"))
            and str(row.get("coin_tag") or "").upper() == selected_coin
            and _algokey(row.get("algorithm")) == _algokey(selected_algorithm)
            and match_gpu_name(gpu_name, str(row.get("gpu_name") or ""))
            and parse_num(row.get("gross_day_usd"), 0.0) > 0
        ]
        best = max(matching, key=lambda row: parse_num(row.get("conservative_gross_day_usd"), parse_num(row.get("gross_day_usd"), 0.0))) if matching else None
        benchmark_rate = parse_num(profile.get("expected_hashrate_hs"), 0.0)
        if benchmark_rate <= 0 and best:
            benchmark_rate = parse_num(best.get("benchmark_hashrate_hs"), 0.0)
        expected_gross = parse_num(best.get("gross_day_usd"), 0.0) if best else pool_run_rate
        expected_gross = expected_gross if expected_gross > 0 else pool_run_rate
        now = now_iso()
        haircut = 1 - min(max(self.config.revenue_haircut, 0), 0.95)
        conservative_gross = min(expected_gross, pool_run_rate) * haircut
        fee_pct = parse_num(profile.get("miner_fee_pct"), self.config.default_miner_fee_pct) + parse_num(profile.get("pool_fee_pct"), self.config.default_pool_fee_pct)
        after_fees = conservative_gross * max(0.0, 1.0 - fee_pct / 100.0)
        cost_day = hourly_rate * 24 + self.config.bandwidth_reserve_usd_day
        net_day = after_fees - cost_day
        meta = {
            "manual_setup": True,
            "adopted_at": now,
            "worker": worker,
            "algorithm": selected_algorithm,
            "last_profitability_scan_at": (best or {}).get("scanned_at") or now,
            "latest_coin_data_updated_at": (best or {}).get("data_updated_at"),
            "pool_metrics": pool_report,
            "pool_metrics_error_count": 0,
            "pool_revenue_missing_count": 0,
            "monitor_fail_count": 0,
            "initial_pool_run_rate_day_usd": pool_run_rate,
            "initial_conservative_net_day_usd": net_day,
        }
        raw_offer_id = provider_instance.get("actual_offer_id") or provider_instance.get("offer_id")
        try:
            offer_id = int(raw_offer_id) if raw_offer_id is not None else None
        except (TypeError, ValueError):
            offer_id = None
        managed = {
            "instance_id": int(instance_id),
            "offer_id": offer_id,
            "opportunity_id": (best or {}).get("id"),
            "coin_tag": selected_coin,
            "gpu_name": gpu_name,
            "hourly_cost": hourly_rate,
            "expected_gross_day": expected_gross,
            "benchmark_hashrate_hs": benchmark_rate,
            "miner_profile_id": profile_id,
            "started_at": now,
            "last_monitor_at": now,
            "observed_hashrate_hs": 0,
            "observed_gpu_util": 0,
            "observed_gpu_temp": 0,
            "accepted_share_lines": 0,
            "last_share_at": None,
            "last_log": "",
            "status": status,
            "latest_gross_day": conservative_gross,
            "latest_net_day": net_day,
            "metadata_json": meta,
        }
        self.db.upsert_managed_instance(managed)
        self.db.log("WARNING", "manual_instance_enrolled_for_monitoring", {
            "instance_id": int(instance_id), "profile_id": profile_id, "coin_tag": selected_coin,
            "algorithm": selected_algorithm, "hourly_cost_usd": hourly_rate,
            "pool_run_rate_day_usd": pool_run_rate, "initial_conservative_net_day_usd": net_day,
            "auto_destroy_enabled": True,
        })
        # Trigger an immediate first safety cycle; all normal background cycles continue
        # independently, and no miner start/restart command is ever executed.
        threading.Thread(target=self.monitor_once, name=f"gph-manual-adopt-{int(instance_id)}", daemon=True).start()
        return {"instance_id": int(instance_id), "already_monitored": False, "initial_net_day_usd": round(net_day, 4)}

    def _current_instances(self) -> list[dict[str, Any]]:
        """Fetch a validated live fleet snapshot for monitoring and budget decisions.

        Dashboard readers consume this cached snapshot rather than waiting on Vast. Cache
        persistence is best-effort so a dashboard-cache write cannot turn a valid provider
        response into a false fleet-sync failure.
        """
        try:
            instances = VastClient(self.config.vast_api_key, self.config.vast_api_base).list_instances()
        except Exception as exc:
            try:
                previous = self.db.get_setting("vast_instances_snapshot", {}) or {}
                if not isinstance(previous, dict):
                    previous = {}
                previous["last_attempt_at"] = now_iso()
                previous["last_error"] = str(exc)[:240]
                self.db.set_setting("vast_instances_snapshot", previous)
            except Exception:
                pass
            raise
        snapshot = {
            "instances": instances,
            "updated_at": now_iso(),
            "last_attempt_at": now_iso(),
            "last_error": None,
            "count": len(instances),
        }
        try:
            self.db.set_setting("vast_instances_snapshot", snapshot)
        except Exception as exc:
            self.db.log("WARNING", "vast_instances_snapshot_cache_failed", {"error": str(exc)[:200]})
        return instances

    def _estimated_spend_last_24h(self) -> float:
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - 24 * 3600
        total = 0.0
        for row in self.db.get_managed_instances():
            start = _dt(row.get("started_at"))
            if not start:
                continue
            meta = row.get("metadata", {}) or {}
            end = _dt(meta.get("destroyed_at")) if isinstance(meta, dict) else None
            end = end or now
            overlap_start = max(start.timestamp(), cutoff)
            overlap_end = min(end.timestamp(), now.timestamp())
            hours = max(0.0, overlap_end - overlap_start) / 3600
            total += hours * parse_num(row.get("hourly_cost"), 0.0)
        return total

    def _rent(self, item: dict[str, Any], profile: dict[str, Any], reason: str, *, active_count_override: int | None = None, pending_daily_usd: float = 0.0, instance_limit: int | None = None, progress_callback=None) -> dict[str, Any]:
        progress = progress_callback or (lambda stage, percent, detail: None)
        progress("Checking live safety blockers", 18, "Verifying live mode and required monitoring/provider configuration")
        blockers = self.live_blockers()
        if blockers:
            raise RuntimeError("Live rental blocked: " + "; ".join(blockers))
        if not item.get("qualified"):
            raise RuntimeError("This opportunity is not marked safe for live rental")
        if not item.get("data_fresh"):
            raise RuntimeError("Profitability data is not currently fresh; run a new scan before renting")
        progress("Revalidating live offer and price", 24, "Looking up the exact offer ID on Vast and recalculating conservative net profit using its current all-in rate")
        vast_client = VastClient(self.config.vast_api_key, self.config.vast_api_base)
        live_offer = vast_client.get_offer(int(item.get("offer_id")), self.config.min_reliability)
        if not live_offer:
            raise RuntimeError("The selected Vast offer is no longer available as a verified, rentable, reliable single-GPU on-demand offer")
        safety_reasons, live_reliability, live_gpu_count, verified, rentable = _offer_safety_reasons(live_offer, self.config.min_reliability)
        if safety_reasons or live_gpu_count != 1 or verified is not True or rentable is not True:
            raise RuntimeError("Live Vast offer safety revalidation failed: " + "; ".join(safety_reasons or ["The offer no longer meets strict verification/rentability/single-GPU requirements"]))
        live_gpu_rate, live_total_rate, live_disk_rate = offer_hourly_cost(live_offer)
        if live_total_rate <= 0 or live_gpu_rate <= 0:
            raise RuntimeError("Vast returned a missing or zero current rental rate; refusing to create an instance")
        fresh_profit = evaluate_profit(
            gross_day_usd=parse_num(item.get("gross_day_usd"), 0.0),
            hourly_cost_usd=live_total_rate,
            gpu_hourly_usd=live_gpu_rate,
            reliability=live_reliability,
            data_fresh=bool(item.get("data_fresh")),
            profile_available=True,
            pool_fee_pct=parse_num(item.get("pool_fee_pct"), self.config.default_pool_fee_pct),
            miner_fee_pct=parse_num(item.get("miner_fee_pct"), self.config.default_miner_fee_pct),
            limits=self.scanner._limits(),
            live_capability_ready=True,
        )
        if not fresh_profit.get("economics_qualified"):
            raise RuntimeError("The current offer price no longer passes conservative profitability gates: " + "; ".join(fresh_profit.get("reasons") or ["Economics did not pass"]))
        item.update(fresh_profit)
        item.update({"gpu_hourly_usd": live_gpu_rate, "total_hourly_usd": live_total_rate,
                     "disk_hourly_usd": live_disk_rate, "reliability": live_reliability,
                     "verified": verified, "rentable": rentable, "gpu_count": live_gpu_count,
                     "offer_safety_passed": True})
        progress("Validating miner profile and payout wallet", 30, "Checking the enabled profile, public wallet and start command")
        wallet = wallet_for_profile(profile, self.config.wallets_path)
        if not wallet:
            raise RuntimeError(f"Missing wallet for profile wallet_key={profile.get('wallet_key')}")
        onstart = str(profile.get("onstart", "")).strip()
        if not onstart or onstart.startswith("#"):
            raise RuntimeError("Mining profile start command is empty/placeholder")
        disk_gb = int(profile.get("disk_gb", self.config.default_disk_gb) or self.config.default_disk_gb)
        if disk_gb <= 0 or disk_gb > self.config.max_disk_gb:
            raise RuntimeError(f"Profile disk size {disk_gb} GB is outside configured limits")
        progress("Checking active rentals and spend limits", 48, "Refreshing active instance count and projected daily commitment")
        existing = self._current_instances()
        active, projected_daily = self._risk_state(existing)
        active = max(active, int(active_count_override or 0))
        projected_daily += max(0.0, float(pending_daily_usd or 0.0))
        spent_last_24h = self._estimated_spend_last_24h()
        # The explicit dashboard target is the operational instance ceiling. A hard
        # software ceiling of 25 prevents accidental unbounded targets; all cost,
        # balance, provider and qualification gates below still apply.
        effective_limit = min(25, instance_limit if instance_limit is not None else self.target_instances())
        if active >= effective_limit:
            raise RuntimeError(f"Active instance cap reached ({active}/{effective_limit})")
        # If MAX_INSTANCE_RUNTIME_HOURS=0, there is no fixed runtime ceiling;
        # still reserve one full day of cost when deciding whether a new rental
        # fits the daily budget. Monitoring continues to stop unprofitable instances.
        commitment_hours = projected_commitment_hours(self.config.max_instance_runtime_hours)
        new_day_cost = parse_num(item.get("total_hourly_usd"), 0) * commitment_hours
        projected_commitment = max(spent_last_24h, projected_daily) + new_day_cost
        if projected_commitment > self.config.max_daily_spend_usd:
            raise RuntimeError(f"Projected daily spend would exceed MAX_DAILY_SPEND_USD ({max(spent_last_24h, projected_daily):.3f} existing/used + {new_day_cost:.3f} new > {self.config.max_daily_spend_usd:.3f})")
        scanned = _dt(item.get("scanned_at"))
        if not scanned or (datetime.now(timezone.utc) - scanned).total_seconds() > min(self.config.max_profit_data_age_minutes, 10) * 60:
            raise RuntimeError("This opportunity scan is too old for a paid rental. Run a fresh scan first.")
        if self.config.max_total_hourly_usd > 0 and parse_num(item.get("total_hourly_usd"), 1e9) > self.config.max_total_hourly_usd:
            raise RuntimeError("Offer exceeds all-in hourly cost cap")
        if self.config.max_gpu_hourly_usd > 0 and parse_num(item.get("gpu_hourly_usd"), 1e9) > self.config.max_gpu_hourly_usd:
            raise RuntimeError("Offer exceeds GPU hourly price cap")

        env = {
            "GPH_WALLET": wallet,
            "GPH_WORKER": str(profile.get("worker") or "gph-worker"),
            "GPH_COIN_TAG": str(item.get("coin_tag") or ""),
            "GPH_ALGORITHM": str(item.get("algorithm") or ""),
        }
        payload: dict[str, Any] = {
            "image": str(profile.get("image") or "nvidia/cuda:12.2.2-base-ubuntu22.04"),
            "label": f"gph-{item.get('coin_tag','coin').lower()}-{item.get('gpu_name','gpu').lower().replace(' ', '-')[:28]}",
            "disk": disk_gb,
            "runtype": "ssh_direct",
            "onstart": onstart,
            "env": env,
            "cancel_unavail": True,
        }
        if profile.get("template_hash_id"):
            payload.pop("image", None)
            payload["template_hash_id"] = profile["template_hash_id"]
        progress("Submitting rental to Vast", 78, "All local gates passed; requesting creation of the paid instance")
        result = vast_client.create_instance(int(item["offer_id"]), payload)
        instance_id = int(result.get("new_contract") or result.get("id"))
        managed = {
            "instance_id": instance_id,
            "offer_id": int(item["offer_id"]),
            "opportunity_id": item["id"],
            "coin_tag": item["coin_tag"],
            "gpu_name": item["gpu_name"],
            "hourly_cost": float(item["total_hourly_usd"]),
            "expected_gross_day": float(item["gross_day_usd"]),
            "benchmark_hashrate_hs": float(item.get("benchmark_hashrate_hs", 0) or profile.get("expected_hashrate_hs", 0) or 0),
            "miner_profile_id": profile.get("id"),
            "started_at": now_iso(),
            "status": "creating",
            "latest_gross_day": float(item.get("conservative_gross_day_usd", 0) or 0),
            "latest_net_day": float(item["expected_net_day_usd"]),
            "metadata_json": {"reason": reason, "worker": env["GPH_WORKER"], "image": payload.get("image", "template"), "algorithm": item.get("algorithm"), "expected_net_day_usd": item["expected_net_day_usd"], "benchmark_gpu_name": item.get("benchmark_gpu_name"), "last_profitability_scan_at": item.get("scanned_at"), "latest_coin_data_updated_at": item.get("data_updated_at")},
        }
        self.db.upsert_managed_instance(managed)
        self.db.log("WARNING", "instance_created", {
            "instance_id": instance_id, "offer_id": item["offer_id"], "gpu_name": item["gpu_name"],
            "coin_tag": item["coin_tag"], "hourly_cost_usd": item["total_hourly_usd"],
            "expected_net_day_usd": item["expected_net_day_usd"], "reason": reason,
            "wallet": _mask(wallet),
        })
        return {"instance_id": instance_id, "vast_response": result}

    def rent_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        raise RuntimeError("GPU Profit Hunter is in manual-setup/monitor-only mode and will not create Vast rentals. Set up the instance in Vast and enroll it under Instances for safety monitoring.")
        self.set_ui_progress("Rent GPU", "Loading selected opportunity", 8, "Fetching the saved opportunity before live revalidation")
        item = self.db.get_opportunity(opportunity_id)
        if not item:
            raise RuntimeError("Opportunity not found. Run a fresh scan first.")
        profile = self._profile_for_item(item)
        if not profile:
            raise RuntimeError("No enabled, compatible miner profile is linked to this opportunity")
        return self._rent(item, profile, "manual dashboard request", instance_limit=self.target_instances(), progress_callback=lambda stage, percent, detail: self.set_ui_progress("Rent GPU", stage, percent, detail))

    def auto_rent_to_target(self, progress_callback=None) -> list[dict[str, Any]]:
        """Auto-rent is intentionally disabled for this manual-setup/monitor-only deployment."""
        try:
            self.db.log("INFO", "auto_rent_disabled_manual_setup_only", {})
        except Exception:
            pass
        return []
        progress = progress_callback or (lambda stage, percent, detail: None)
        progress("Checking target and current rentals", 40, "Calculating available slots and existing spend commitments")
        if not (self.config.automation_mode == "live" and self.config.enable_live_mining and self.auto_rent_is_enabled()):
            return []
        blockers = self.live_blockers()
        if blockers:
            self.db.log("INFO", "auto_rent_skipped", {"blockers": blockers})
            return []
        target = self.target_instances()
        if target < 1:
            return []
        active_rows = self._current_instances()
        active_count, projected_daily = self._risk_state(active_rows)
        managed_rows = self.db.get_managed_instances()
        active_ids = set()
        active_offer_ids = set()
        for inst in active_rows:
            try:
                active_ids.add(int(inst.get("id") or inst.get("instance_id") or inst.get("contract_id")))
            except (TypeError, ValueError):
                pass
        terminal_states = {"destroyed"}
        for row in managed_rows:
            row_status = str(row.get("status", "")).lower()
            if row_status not in terminal_states and int(row.get("instance_id") or -1) not in active_ids:
                # Include an instance in 'creating' state even if Vast has not listed it yet.
                active_ids.add(int(row.get("instance_id") or -1))
                projected_daily += max(0.0, float(row.get("hourly_cost", 0) or 0)) * 24
            if row_status not in terminal_states and row.get("offer_id") is not None:
                active_offer_ids.add(int(row["offer_id"]))
        active_count = max(active_count, len(active_ids))
        slots = target - active_count
        if slots <= 0:
            return []
        deployed: list[dict[str, Any]] = []
        used_offer_ids = set(active_offer_ids)
        candidates = [x for x in self.db.get_opportunities(limit=2000) if x.get("qualified") and x.get("profile_available")]
        # Explicitly rank by conservative expected net AFTER rent, disk, fees and reserves.
        # Freshness and reliability are tie-breakers; stale data still fails the hard qualification gate.
        candidates.sort(key=lambda x: (
            float(x.get("expected_net_day_usd") or 0),
            bool(x.get("data_fresh")),
            float(x.get("reliability") or 0),
            -float(x.get("total_hourly_usd") or 0),
        ), reverse=True)
        progress("Reviewing qualified candidates", 48, f"Found {len(candidates)} eligible candidates; ranked by conservative net profit after modeled costs")
        for candidate_index, item in enumerate(candidates, start=1):
            progress("Evaluating deployment candidates", min(88, 48 + int(35 * candidate_index / max(1, len(candidates)))), f"Checking candidate {candidate_index} of {len(candidates)}; enforcing duplicate, profile and spend gates")
            if len(deployed) >= slots:
                break
            try:
                offer_id = int(item.get("offer_id"))
            except (TypeError, ValueError):
                continue
            if offer_id in used_offer_ids:
                continue
            profile = self._profile_for_item(item)
            if not profile:
                continue
            try:
                result = self._rent(
                    item, profile, "automatic best qualified opportunity",
                    active_count_override=active_count + len(deployed),
                    pending_daily_usd=sum(float(x.get("total_hourly_usd", 0) or 0) * 24 for x in deployed),
                    instance_limit=target,
                    progress_callback=lambda stage, percent, detail: progress(stage, min(95, 50 + int(percent * 0.45)), detail),
                )
                used_offer_ids.add(offer_id)
                result.update({"offer_id": offer_id, "gpu_name": item.get("gpu_name"), "coin_tag": item.get("coin_tag"), "expected_net_day_usd": item.get("expected_net_day_usd")})
                deployed.append(result)
            except Exception as exc:
                self.db.log("WARNING", "auto_rent_candidate_rejected", {"opportunity_id": item.get("id"), "offer_id": offer_id, "error": str(exc)[:300]})
        if deployed:
            self.db.log("WARNING", "auto_rent_target_filled_partially", {"target": target, "deployed": deployed, "remaining_slots": max(0, target - active_count - len(deployed))})
        return deployed

    def auto_rent_best(self) -> dict[str, Any] | None:
        """Backward-compatible helper: deploy the first item returned by target-fill logic."""
        results = self.auto_rent_to_target()
        return results[0] if results else None

    @staticmethod
    def _metadata_dict(managed: dict[str, Any]) -> dict[str, Any]:
        meta = managed.get("metadata", managed.get("metadata_json", {})) or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        return dict(meta) if isinstance(meta, dict) else {}

    def _confirm_destroyed(self, managed: dict[str, Any], source: str) -> None:
        """Mark a rental destroyed only after the provider listing confirms it is gone."""
        meta = self._metadata_dict(managed)
        if not meta.get("destroyed_at"):
            meta["destroyed_at"] = now_iso()
        meta["destruction_confirmed"] = True
        meta["destruction_confirmation_source"] = source
        managed["status"] = "destroyed"
        managed["metadata_json"] = meta
        managed["last_monitor_at"] = now_iso()
        self.db.upsert_managed_instance(managed)
        self.db.log("INFO", "instance_destruction_confirmed", {
            "instance_id": managed.get("instance_id"), "source": source,
            "destroy_reason": meta.get("destroy_reason"),
        })

    def _destroy(self, instance_id: int, reason: str) -> bool:
        """Request destruction and keep monitoring until Vast confirms termination.

        A successful HTTP DELETE only proves the provider accepted the request; it does
        not prove billing has stopped. This method therefore records destroy_requested,
        never destroyed, and returns False on an unaccepted/failed request.
        """
        managed = self.db.get_managed_instance(instance_id) or {"instance_id": instance_id}
        if str(managed.get("status") or "").lower() == "destroyed":
            return True
        meta = self._metadata_dict(managed)
        now = datetime.now(timezone.utc)
        retry_seconds = max(30, min(int(self.config.monitor_interval_seconds), 60))
        last_attempt = _dt(meta.get("last_destroy_attempt_at"))
        if str(managed.get("status") or "").lower() == "destroy_requested" and last_attempt:
            if (now - last_attempt).total_seconds() < retry_seconds:
                return True  # already requested recently; monitor will re-check provider state

        attempt_at = now_iso()
        meta.setdefault("destroy_reason", reason)
        meta["last_destroy_trigger"] = reason
        meta["last_destroy_attempt_at"] = attempt_at
        meta["destroy_attempt_count"] = int(meta.get("destroy_attempt_count", 0) or 0) + 1
        managed["metadata_json"] = meta
        managed["last_monitor_at"] = attempt_at
        self.db.upsert_managed_instance(managed)
        try:
            result = VastClient(self.config.vast_api_key, self.config.vast_api_base).destroy_instance(instance_id)
            latest = self.db.get_managed_instance(instance_id) or managed
            latest_meta = self._metadata_dict(latest)
            latest_meta.setdefault("destroy_reason", reason)
            latest_meta["last_destroy_trigger"] = reason
            latest_meta["last_destroy_attempt_at"] = attempt_at
            latest_meta["destroy_request_accepted_at"] = now_iso()
            latest_meta["last_destroy_error"] = ""
            latest["status"] = "destroy_requested"
            latest["metadata_json"] = latest_meta
            latest["last_monitor_at"] = now_iso()
            self.db.upsert_managed_instance(latest)
            self.db.log("WARNING", "instance_destroy_requested", {
                "instance_id": instance_id, "reason": latest_meta.get("destroy_reason"),
                "result": result, "confirmation_pending": True,
            })
            return True
        except Exception as exc:
            latest = self.db.get_managed_instance(instance_id) or managed
            latest_meta = self._metadata_dict(latest)
            latest_meta.setdefault("destroy_reason", reason)
            latest_meta["last_destroy_trigger"] = reason
            latest_meta["last_destroy_attempt_at"] = attempt_at
            latest_meta["last_destroy_error"] = str(exc)[:260]
            latest["metadata_json"] = latest_meta
            latest["last_monitor_at"] = now_iso()
            self.db.upsert_managed_instance(latest)
            self.db.log("ERROR", "auto_destroy_failed", {
                "instance_id": instance_id, "reason": reason, "error": str(exc)[:300],
                "attempt": latest_meta.get("destroy_attempt_count", 1),
            })
            return False

    def _monitor_managed(self, instance: dict[str, Any], managed: dict[str, Any]) -> None:
        instance_id = int(managed["instance_id"])
        # DB read helpers expose the JSON column as metadata; upsert accepts metadata_json.
        if not managed.get("metadata_json"):
            managed["metadata_json"] = managed.get("metadata", {}) or {}
        status = str(instance.get("actual_status") or instance.get("status") or "unknown").lower()
        current_rate = parse_num(instance.get("dph_total"), parse_num(instance.get("dph_base"), float(managed.get("hourly_cost", 0) or 0)))
        if self.config.max_total_hourly_usd > 0 and current_rate > self.config.max_total_hourly_usd:
            self._destroy(instance_id, f"Current Vast rate ${current_rate:.4f}/h exceeds configured all-in hourly cap ${self.config.max_total_hourly_usd:.4f}/h")
            return
        # Do not destroy solely because the rate changed by a fixed percentage.
        # Store the live rate and recalculate the current conservative net forecast below;
        # if the rental becomes uneconomic, the profitability guard will terminate it.
        managed["hourly_cost"] = current_rate or float(managed.get("hourly_cost", 0) or 0)
        managed["status"] = status
        managed["last_monitor_at"] = now_iso()
        started = _dt(managed.get("started_at")) or datetime.now(timezone.utc)
        runtime_hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if runtime_limit_reached(runtime_hours, self.config.max_instance_runtime_hours):
            self._destroy(instance_id, f"Maximum runtime reached ({self.config.max_instance_runtime_hours}h)")
            return
        if status in {"exited", "unknown", "offline", "error", "failed", "destroyed"}:
            self._destroy(instance_id, f"Instance status became {status}")
            return
        if status not in {"running", "active"}:
            if runtime_hours * 60 > self.config.startup_grace_minutes:
                self._destroy(instance_id, f"Instance never reached running within startup grace ({self.config.startup_grace_minutes}m)")
            else:
                self.db.upsert_managed_instance(managed)
            return
        meta_fresh = self._metadata_dict(managed)
        manual_setup = bool(meta_fresh.get("manual_setup"))
        last_scan = _dt(meta_fresh.get("last_profitability_scan_at"))
        # Operator-created instances use a timestamp-validated live pool feed every
        # cycle, so scanner recency is not a prerequisite for manual monitoring.
        if not manual_setup and (not last_scan or (datetime.now(timezone.utc) - last_scan).total_seconds() > self.config.max_profit_data_age_minutes * 60):
            self._destroy(instance_id, "Profitability data is stale; refusing to mine without recent market/reward rechecks")
            return
        profile = next((p for p in load_profiles(self.config.miner_profiles_path) if p.get("id") == managed.get("miner_profile_id")), None)
        if not profile:
            self._destroy(instance_id, "Mining profile was removed while instance was running")
            return
        try:
            metrics = self.monitor.read_metrics(instance, profile)
        except Exception as exc:
            meta = managed.get("metadata", {}) or {}
            fail_count = int(meta.get("monitor_fail_count", 0)) + 1
            meta["monitor_fail_count"] = fail_count
            managed["metadata_json"] = meta
            managed["last_monitor_at"] = now_iso()
            self.db.upsert_managed_instance(managed)
            if runtime_hours * 60 > self.config.startup_grace_minutes and fail_count >= 3:
                self._destroy(instance_id, f"Remote miner monitoring unavailable after startup grace: {exc.__class__.__name__}")
            else:
                self.db.log("WARNING", "remote_monitor_retry", {"instance_id": instance_id, "attempt": fail_count, "error": str(exc)[:200]})
            return

        meta = self._metadata_dict(managed)
        requires_pool_metrics = manual_setup or self.config.require_pool_metrics_for_live
        pool_report: dict[str, Any] | None = None
        pool_error = ""
        if (profile.get("pool_metrics") or {}).get("url") and (profile.get("pool_metrics") or {}).get("revenue_24h_usd_path"):
            try:
                worker = str(meta.get("worker") or profile.get("worker") or "gph-worker")
                wallet = wallet_for_profile(profile, self.config.wallets_path)
                if not wallet:
                    raise RuntimeError("Pool metrics configured but the profile's public wallet is missing")
                pool_report = self.pool_metrics.fetch(profile, wallet, worker, str(managed.get("coin_tag") or ""))
                meta["pool_metrics"] = pool_report
                meta["pool_metrics_error_count"] = 0
                meta["pool_metrics_last_error"] = ""
            except Exception as exc:
                pool_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
                meta["pool_metrics_error_count"] = int(meta.get("pool_metrics_error_count", 0) or 0) + 1
                meta["pool_metrics_last_error"] = pool_error
                self.db.log("WARNING", "pool_metrics_fetch_failed", {
                    "instance_id": instance_id, "attempt": meta["pool_metrics_error_count"], "error": pool_error
                })
                if requires_pool_metrics and runtime_hours * 60 > self.config.startup_grace_minutes:
                    managed["metadata_json"] = meta
                    self.db.upsert_managed_instance(managed)
                    if meta["pool_metrics_error_count"] >= 3:
                        self._destroy(instance_id, "Required pool-reported run-rate telemetry failed repeatedly; refusing to keep paying without it")
                    return
        elif requires_pool_metrics:
            self._destroy(instance_id, "Required pool metrics connector is missing at runtime")
            return

        # A successful HTTP response is not sufficient if the configured pool adapter
        # did not provide the explicitly configured current 24h gross-revenue field.
        # Do not silently fall back to a benchmark forever in live mode.
        pool_run_rate = parse_num((pool_report or {}).get("revenue_24h_usd"), -1.0)
        if requires_pool_metrics and runtime_hours * 60 > self.config.startup_grace_minutes:
            if pool_run_rate < 0:
                missing_count = int(meta.get("pool_revenue_missing_count", 0) or 0) + 1
                meta["pool_revenue_missing_count"] = missing_count
                meta["pool_metrics_last_error"] = "Pool response did not contain a valid configured revenue_24h_usd value"
                managed["metadata_json"] = meta
                managed["last_monitor_at"] = now_iso()
                self.db.upsert_managed_instance(managed)
                if missing_count >= 3:
                    self._destroy(instance_id, "Required live pool gross-revenue telemetry is missing after startup grace; refusing to continue paying on benchmark estimates alone")
                return
            meta["pool_revenue_missing_count"] = 0
        hashrate = float(metrics.get("hashrate_hs", 0) or 0)
        bench_rate = float(managed.get("benchmark_hashrate_hs", 0) or profile.get("expected_hashrate_hs", 0) or 0)
        share_lines = int(metrics.get("accepted_share_lines", 0) or 0)
        last_share = metrics.get("last_share_at")
        util = float(metrics.get("gpu_util", 0) or 0)
        temp = float(metrics.get("gpu_temp", 0) or 0)
        samples = meta.get("hashrate_samples", [])
        if not isinstance(samples, list):
            samples = []
        if hashrate > 0:
            samples.append(hashrate)
            samples = samples[-6:]
        meta["hashrate_samples"] = samples
        meta["monitor_fail_count"] = 0
        meta["last_metrics_error"] = metrics.get("monitor_error", "")
        managed.update({
            "last_monitor_at": now_iso(), "observed_hashrate_hs": hashrate,
            "observed_gpu_util": util, "observed_gpu_temp": temp,
            "accepted_share_lines": share_lines, "last_share_at": last_share,
            "last_log": str(metrics.get("log_tail", ""))[-1000:], "metadata_json": meta,
        })
        # No observed hashrate or accepted share by startup deadline: destroy instead of paying blindly.
        if runtime_hours * 60 > self.config.startup_grace_minutes:
            if bench_rate <= 0 and not manual_setup:
                self.db.upsert_managed_instance(managed)
                self._destroy(instance_id, "No positive benchmark hashrate is available for live profitability verification")
                return
            if hashrate <= 0:
                self.db.upsert_managed_instance(managed)
                self._destroy(instance_id, "No parseable miner hashrate after startup grace")
                return
            if share_lines <= 0:
                self.db.upsert_managed_instance(managed)
                self._destroy(instance_id, "No accepted-share evidence after startup grace")
                return
        if (bench_rate > 0 or manual_setup) and hashrate > 0 and (pool_run_rate >= 0 or not manual_setup):
            # A manual instance can use the timestamp-validated live pool run-rate
            # when no scanner benchmark exists for the user's independently set-up miner.
            ratio = hashrate / bench_rate if bench_rate > 0 else 1.0
            if bench_rate > 0 and len(samples) >= 3:
                median_rate = statistics.median(samples[-3:])
                median_ratio = median_rate / bench_rate
                if median_ratio < self.config.min_observed_hashrate_ratio:
                    self.db.upsert_managed_instance(managed)
                    self._destroy(instance_id, f"Observed hashrate below guardrail ({median_ratio:.2f}x benchmark)")
                    return
            gross = parse_num(managed.get("expected_gross_day"), 0.0)
            if manual_setup and bench_rate <= 0:
                gross = pool_run_rate
            elif gross <= 0:
                gross = pool_run_rate
            haircut = 1 - min(max(self.config.revenue_haircut, 0), 0.95)
            haircut_gross = gross * haircut * min(max(ratio, 0), 1.25)
            if pool_run_rate >= 0:
                pool_conservative_gross = pool_run_rate * haircut
                # Fail conservative: use the lower of benchmark-based earnings and the configured pool adapter's current 24h-equivalent run-rate.
                haircut_gross = min(haircut_gross, pool_conservative_gross)
                meta["pool_metrics_conservative_gross_day_usd"] = pool_conservative_gross
            miner_fee_pct = float(profile.get("miner_fee_pct", self.config.default_miner_fee_pct) or 0)
            pool_fee_pct = float(profile.get("pool_fee_pct", self.config.default_pool_fee_pct) or 0)
            after_fees = haircut_gross * max(0, 1 - (miner_fee_pct + pool_fee_pct) / 100)
            cost_day = float(managed.get("hourly_cost", 0) or 0) * 24 + self.config.bandwidth_reserve_usd_day
            net_day = after_fees - cost_day
            managed["latest_gross_day"] = haircut_gross
            managed["latest_net_day"] = net_day
            meta["profitability_check_at"] = now_iso()
            meta["live_rent_cost_day_usd"] = cost_day
            meta["conservative_after_fees_day_usd"] = after_fees
            meta["pool_run_rate_day_usd"] = pool_run_rate if pool_run_rate >= 0 else None
            # Forecast safety gate: a current rate drop is reflected immediately through the
            # lower pool run-rate, and the lower of pool and benchmark estimates is used.
            if net_day < self.config.min_net_profit_usd_day or (cost_day > 0 and after_fees / cost_day < self.config.min_revenue_cost_ratio):
                managed["metadata_json"] = meta
                self.db.upsert_managed_instance(managed)
                self._destroy(instance_id, f"Live conservative net forecast failed: ${net_day:.3f}/day after current rent and reserves")
                return
            # If the pool explicitly exposes realized trailing-24h earnings, use them as a
            # second independent check after a full day of runtime. Never compare a partial
            # startup period against a full day's rent cost.
            realized_24h = parse_num((pool_report or {}).get("realized_revenue_24h_usd"), -1.0)
            if runtime_hours >= 24 and realized_24h >= 0:
                realized_net_24h = realized_24h * max(0, 1 - (miner_fee_pct + pool_fee_pct) / 100) - cost_day
                meta["realized_revenue_24h_usd"] = realized_24h
                meta["realized_net_24h_usd"] = realized_net_24h
                if realized_net_24h < self.config.min_net_profit_usd_day or (cost_day > 0 and (realized_24h * max(0, 1 - (miner_fee_pct + pool_fee_pct) / 100)) / cost_day < self.config.min_revenue_cost_ratio):
                    managed["metadata_json"] = meta
                    self.db.upsert_managed_instance(managed)
                    self._destroy(instance_id, f"Realized trailing-24h profitability failed: ${realized_net_24h:.3f}/day net")
                    return
        # Stale accepted shares cause a fail-safe destroy only after startup and if configured.
        share_dt = _dt(last_share)
        if share_dt and (datetime.now(timezone.utc) - share_dt).total_seconds() > self.config.max_no_share_minutes * 60:
            self.db.upsert_managed_instance(managed)
            self._destroy(instance_id, f"No accepted share in last {self.config.max_no_share_minutes} minutes")
            return
        if temp >= 88:
            self.db.upsert_managed_instance(managed)
            self._destroy(instance_id, f"GPU temperature reached {temp:.1f}C safety threshold")
            return
        self.db.upsert_managed_instance(managed)

    def monitor_once(self) -> dict[str, Any]:
        """Run at most one monitoring cycle at a time, including manual UI requests."""
        if not self._monitor_cycle_lock.acquire(blocking=False):
            return {"monitored": 0, "skipped": True, "reason": "A previous monitoring cycle is still running"}
        try:
            return self._monitor_once_locked()
        finally:
            self._monitor_cycle_lock.release()

    def _monitor_once_locked(self) -> dict[str, Any]:
        if not self.config.vast_api_key:
            return {"monitored": 0, "error": "VAST_API_KEY not configured"}
        try:
            api_instances = self._current_instances()
            self._monitor_sync_failures = 0
        except Exception as exc:
            self._monitor_sync_failures += 1
            self.db.log("ERROR", "instance_sync_failed", {
                "error": str(exc)[:300], "consecutive_failures": self._monitor_sync_failures,
            })
            # On listing failure, keep trying to terminate paid instances after repeated
            # misses. Never mark them destroyed based on an unverified DELETE response.
            if self._monitor_sync_failures >= 2:
                retry_seconds = max(30, min(int(self.config.monitor_interval_seconds), 60))
                now = datetime.now(timezone.utc)
                for managed in self.db.get_managed_instances():
                    if str(managed.get("status") or "").lower() == "destroyed":
                        continue
                    meta = self._metadata_dict(managed)
                    last_attempt = _dt(meta.get("last_destroy_attempt_at"))
                    if last_attempt and (now - last_attempt).total_seconds() < retry_seconds:
                        continue
                    try:
                        self._destroy(int(managed["instance_id"]), "Could not verify live instance status after consecutive Vast listing failures; best-effort fail-safe termination")
                    except Exception as destroy_exc:
                        self.db.log("ERROR", "monitor_sync_fail_safe_destroy_failed", {"instance_id": managed.get("instance_id"), "error": str(destroy_exc)[:220]})
            return {"monitored": 0, "error": str(exc)}

        by_id: dict[int, dict[str, Any]] = {}
        for inst in api_instances:
            try:
                by_id[int(inst.get("id") or inst.get("instance_id") or inst.get("contract_id"))] = inst
            except (TypeError, ValueError):
                continue
        managed_rows = self.db.get_managed_instances()
        count = 0
        missing_ids: list[int] = []
        to_monitor: list[tuple[dict[str, Any], dict[str, Any]]] = []
        retry_seconds = max(30, min(int(self.config.monitor_interval_seconds), 60))
        now = datetime.now(timezone.utc)
        for managed in managed_rows:
            if str(managed.get("status") or "").lower() == "destroyed":
                continue
            instance_id = int(managed["instance_id"])
            inst = by_id.get(instance_id)
            meta = self._metadata_dict(managed)
            if not inst:
                if meta.get("destroy_request_accepted_at"):
                    self._confirm_destroyed(managed, "absent_from_vast_instance_list_after_accepted_delete")
                    continue
                # Absence is ambiguous until a deletion request is accepted. Keep the
                # cost reservation and make a fail-safe destroy attempt after repeated misses.
                missing_count = int(meta.get("instance_missing_sync_count", 0) or 0) + 1
                meta["instance_missing_sync_count"] = missing_count
                managed["status"] = "missing_from_vast"
                managed["metadata_json"] = meta
                managed["last_monitor_at"] = now_iso()
                self.db.upsert_managed_instance(managed)
                last_attempt = _dt(meta.get("last_destroy_attempt_at"))
                if missing_count >= 2 and (not last_attempt or (now - last_attempt).total_seconds() >= retry_seconds):
                    missing_ids.append(instance_id)
                continue

            api_status = str(inst.get("actual_status") or inst.get("status") or "unknown").lower()
            if api_status == "destroyed":
                self._confirm_destroyed(managed, "provider_instance_status_destroyed")
                continue
            # A destroy request is not the same as a completed destroy. While the instance
            # remains visible, retry DELETE on a bounded cadence and do not resume mining.
            if str(managed.get("status") or "").lower() == "destroy_requested":
                last_attempt = _dt(meta.get("last_destroy_attempt_at"))
                if not last_attempt or (now - last_attempt).total_seconds() >= retry_seconds:
                    self._destroy(instance_id, str(meta.get("destroy_reason") or "Retrying termination because instance remains visible in Vast"))
                continue

            if meta.get("instance_missing_sync_count"):
                meta.pop("instance_missing_sync_count", None)
                managed["metadata_json"] = meta
                self.db.upsert_managed_instance(managed)
            to_monitor.append((inst, managed))

        # SSH and pool APIs can each take several seconds. Monitor a bounded number of
        # instances concurrently so one dead SSH port cannot delay every other rental.
        if to_monitor:
            workers = min(2, len(to_monitor))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gph-monitor-item") as pool:
                futures = {pool.submit(self._monitor_managed, inst, managed): int(managed["instance_id"])
                           for inst, managed in to_monitor}
                for future in as_completed(futures):
                    instance_id = futures[future]
                    try:
                        future.result()
                        count += 1
                    except Exception as exc:
                        self.db.log("ERROR", "managed_instance_monitor_error", {"instance_id": instance_id, "error": str(exc)[:300]})

        # Execute missing-list fail-safe actions after iterating the current row snapshot.
        for instance_id in missing_ids:
            row = self.db.get_managed_instance(instance_id)
            if row and str(row.get("status") or "").lower() != "destroyed":
                self._destroy(instance_id, "Managed instance was absent from consecutive Vast listings; requesting termination to avoid unverified ongoing charges")
        return {"monitored": count, "destroy_requests": len(missing_ids)}

    def run_loop(self) -> None:
        # -inf (not 0.0): time.monotonic() can be < 60 right after a host boots, which made the
        # first balance/scan/Kryptex checks wait a full interval instead of running immediately.
        last_kryptex = last_vast_balance = float("-inf")
        # Absolute time of the next automatic scan (not "time since last"): the wait can be a
        # quota cool-down of hours, which must not shrink as the clock advances.
        next_scan_at = float("-inf")
        while not self.stop_event.is_set():
            now = time.monotonic()
            if self.config.vast_api_key and now - last_vast_balance >= 60:
                threading.Thread(target=self._refresh_vast_balance_background, name="gph-vast-balance-poll", daemon=True).start()
                last_vast_balance = time.monotonic()
            if self.config.kryptex_mining_username and now - last_kryptex >= max(60, self.config.kryptex_poll_interval_seconds):
                try:
                    self.request_kryptex_refresh()
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).warning("Kryptex refresh could not be started: %s", exc.__class__.__name__)
                last_kryptex = time.monotonic()
            if self.config.auto_scan_enabled and self.config.vast_api_key and self.config.whattomine_api_token and now >= next_scan_at:
                try:
                    self.scan()
                    next_scan_at = time.monotonic() + self.effective_scan_interval()
                    # Automatic rental creation is disabled: scans never launch paid instances.
                except VastRateLimitError as exc:
                    self._record_search_paused(exc)
                    next_scan_at = time.monotonic() + max(self.effective_scan_interval(), exc.retry_after)
                except Exception as exc:
                    import logging
                    message = str(exc)[:300]
                    self.set_ui_progress("Market scan", "Automatic scan failed", 100, message, "failed", persist=False)
                    logging.getLogger(__name__).exception("Automatic market scan failed")
                    try:
                        self.db.log("ERROR", "auto_scan_failed", {"error": message})
                    except Exception:
                        logging.getLogger(__name__).warning("Could not persist automatic-scan failure because the database is unavailable")
                    # Respect the scan interval after failures too; do not hammer a
                    # failing provider every supervisor tick. The next scheduled pass retries.
                    next_scan_at = time.monotonic() + self.effective_scan_interval()
            self.stop_event.wait(max(10, min(self.config.monitor_interval_seconds, 60)))

    def run_monitor_loop(self) -> None:
        """Monitor managed rentals independently of scans, provider refreshes, and auto-rent."""
        interval = max(10, min(int(self.config.monitor_interval_seconds), 60))
        while not self.stop_event.is_set():
            try:
                self.monitor_once()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).exception("Live instance monitor cycle failed")
                try:
                    self.db.log("ERROR", "live_monitor_loop_failed", {"error": str(exc)[:240]})
                except Exception:
                    logging.getLogger(__name__).warning("Could not persist monitor failure because the database is unavailable")
            self.stop_event.wait(interval)

    def _refresh_vast_balance_background(self) -> None:
        with self._balance_refresh_state_lock:
            if self._balance_refreshing:
                return
            self._balance_refreshing = True
            self._balance_refresh_started_at = now_iso()
            self._balance_refresh_finished_at = None
        self._run_vast_balance_refresh()
