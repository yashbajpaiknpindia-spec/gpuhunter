"""Daily Vast search-row budget, pacing and 429 cool-down.

Vast meters ``GET /bundles/`` by the number of *rows returned*, against a daily quota
(20,000 rows on this account). The previous scanner requested two 500-row queries every
60 seconds, i.e. about 1,000 rows a minute, so the whole day's quota was gone in ~20
minutes and every later search failed with HTTP 429 ``search_quota_exceeded``.

This tracker:
  * counts rows per UTC day (Vast's ``retry_after`` lands on UTC midnight) and persists the
    counter so a Render redeploy/restart cannot reset the budget;
  * paces automatic scans so the remaining rows last until the reset;
  * keeps a reserve for pre-rental revalidation (``get_offer``), which uses the same quota;
  * remembers a 429 cool-down so no search is sent while Vast says it will refuse it.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

from .providers.vast import VastQuotaError, format_duration, seconds_until_utc_midnight

SETTING_KEY = "vast_search_budget"
log = logging.getLogger(__name__)


def _utc_day(now: float) -> int:
    return int(now // 86400)


class VastSearchBudget:
    def __init__(
        self,
        db: Any,
        *,
        daily_rows: int = 20000,
        reserve_rows: int = 1500,
        strict_limit: int = 100,
        audit_limit: int = 40,
        audit_interval_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
        setting_key: str = SETTING_KEY,
    ):
        self.db = db
        self.setting_key = setting_key
        self.daily_rows = max(0, int(daily_rows))
        self.reserve_rows = max(0, min(int(reserve_rows), self.daily_rows))
        self.strict_limit = max(1, min(int(strict_limit), 500))
        self.audit_limit = max(0, min(int(audit_limit), 500))
        self.audit_interval = max(60, int(audit_interval_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._loaded = False
        self._state: dict[str, Any] = {"day": _utc_day(clock()), "rows": 0, "cooldown_until": 0.0,
                                       "cooldown_reason": "", "last_audit_at": 0.0, "searches": 0}

    # ---- persistence -------------------------------------------------------------------
    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            saved = self.db.get_setting(self.setting_key, None)
        except Exception as exc:  # DB may be briefly unavailable; retry on the next call
            log.warning("Could not load Vast search budget: %s", exc.__class__.__name__)
            return
        with self._lock:
            if isinstance(saved, dict):
                for key in self._state:
                    if key in saved:
                        self._state[key] = saved[key]
            self._loaded = True

    def _save(self) -> None:
        try:
            self.db.set_setting(self.setting_key, dict(self._state))
        except Exception as exc:
            log.warning("Could not persist Vast search budget: %s", exc.__class__.__name__)

    def _roll_day(self) -> None:
        today = _utc_day(self._clock())
        if self._state.get("day") != today:
            self._state.update(day=today, rows=0, searches=0)

    # ---- accounting --------------------------------------------------------------------
    @property
    def usable_rows(self) -> int:
        return max(0, self.daily_rows - self.reserve_rows)

    def rows_used_today(self) -> int:
        with self._lock:
            self._roll_day()
            return int(self._state.get("rows", 0))

    def rows_remaining_today(self) -> int:
        return max(0, self.usable_rows - self.rows_used_today())

    def record_rows(self, rows: int) -> None:
        with self._lock:
            self._roll_day()
            self._state["rows"] = int(self._state.get("rows", 0)) + max(0, int(rows))
            self._state["searches"] = int(self._state.get("searches", 0)) + 1
            self._save()

    def mark_audit_ran(self) -> None:
        with self._lock:
            self._state["last_audit_at"] = self._clock()
            self._save()

    def note_rate_limit(self, retry_after: float, *, quota: bool, reason: str = "") -> None:
        """Remember a 429 so searches stop until Vast's retry window has passed."""
        with self._lock:
            self._roll_day()
            self._state["cooldown_until"] = self._clock() + max(1.0, float(retry_after))
            self._state["cooldown_reason"] = reason[:240]
            if quota:
                # Vast says the day's rows are gone: mirror that locally so pacing agrees.
                self._state["rows"] = max(int(self._state.get("rows", 0)), self.daily_rows)
            self._save()

    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, float(self._state.get("cooldown_until", 0.0)) - self._clock())

    # ---- planning ----------------------------------------------------------------------
    def check_can_search(self) -> None:
        """Raise ``VastQuotaError`` (without any HTTP call) if searching now is pointless."""
        wait = self.cooldown_remaining()
        if wait > 0:
            reason = self._state.get("cooldown_reason") or "Vast search quota exhausted"
            raise VastQuotaError(f"{reason}. Searching resumes in {format_duration(wait)}.", retry_after=wait)
        if self.daily_rows and self.rows_remaining_today() < min(self.strict_limit, 10):
            wait = seconds_until_utc_midnight(self._clock())
            raise VastQuotaError(
                f"Local Vast search-row budget used up ({self.rows_used_today():,} of {self.usable_rows:,} usable rows today; "
                f"{self.reserve_rows:,} are reserved for rental revalidation). Searching resumes in {format_duration(wait)}.",
                retry_after=wait,
            )

    def plan(self) -> dict[str, Any]:
        """Decide this scan's row limits. Raises ``VastQuotaError`` when no search should be sent."""
        self.ensure_loaded()
        self.check_can_search()
        with self._lock:
            remaining = self.rows_remaining_today() if self.daily_rows else 10**9
            strict = min(self.strict_limit, remaining)
            audit_limit = 0
            skip = ""
            if self.audit_limit <= 0:
                skip = "broad audit disabled (VAST_AUDIT_LIMIT=0) to conserve the daily Vast row quota"
            else:
                since = self._clock() - float(self._state.get("last_audit_at", 0.0))
                if since < self.audit_interval:
                    skip = f"next broad audit in {format_duration(self.audit_interval - since)} (conserving daily Vast row quota)"
                elif remaining - strict < self.audit_limit:
                    skip = "not enough daily Vast row budget left for an audit"
                else:
                    audit_limit = self.audit_limit
            return {"strict_limit": strict, "include_audit": audit_limit > 0, "audit_limit": audit_limit,
                    "audit_skip_reason": skip}

    def recommended_interval(self) -> float:
        """Seconds between automatic scans so the remaining rows last until the daily reset."""
        with self._lock:
            self._roll_day()
            if not self.daily_rows:
                return 0.0
            secs_left = max(60.0, seconds_until_utc_midnight(self._clock()))
            remaining = self.rows_remaining_today()
            if self.audit_limit > 0:
                remaining -= self.audit_limit * math.ceil(secs_left / self.audit_interval)
            if remaining < self.strict_limit:
                return secs_left  # nothing to spend; wait for the reset
            return secs_left * self.strict_limit / remaining

    def status(self) -> dict[str, Any]:
        """In-memory snapshot for the dashboard (never touches the database)."""
        with self._lock:
            self._roll_day()
            return {
                "daily_row_budget": self.daily_rows,
                "reserve_rows": self.reserve_rows,
                "rows_used_today": int(self._state.get("rows", 0)),
                "rows_remaining_today": self.rows_remaining_today(),
                "strict_limit": self.strict_limit,
                "audit_limit": self.audit_limit,
                "audit_interval_seconds": self.audit_interval,
                "recommended_scan_interval_seconds": int(self.recommended_interval()),
                "cooldown_seconds": int(self.cooldown_remaining()),
                "cooldown_reason": self._state.get("cooldown_reason") or None,
                "resets_in_seconds": int(seconds_until_utc_midnight(self._clock())),
            }
