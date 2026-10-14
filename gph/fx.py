"""Small, cached USD/INR quote helper used only for display conversions.

USD remains the accounting/base currency. The FX value never changes profitability
calculations, admission gates, or rental decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

import requests

_URL = "https://open.er-api.com/v6/latest/USD"
_CACHE_TTL_SECONDS = 24 * 3600
_MAX_FETCH_AGE_SECONDS = 24 * 3600
_MAX_SOURCE_AGE_SECONDS = 72 * 3600
_LOCK = Lock()
_LAST_GOOD: dict[str, Any] | None = None
_LAST_ATTEMPT: datetime | None = None
_LAST_ERROR: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def _response(*, now: datetime, cached: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    if not cached:
        return {
            "available": False,
            "rate": None,
            "source": "open.er-api.com",
            "fetched_at": None,
            "source_updated_at": None,
            "age_hours": None,
            "stale": True,
            "error": error or "No USD/INR quote is available. INR conversions are hidden until a quote can be verified.",
            "accounting_currency": "USD",
        }
    fetched_at = cached["fetched_at"]
    source_updated_at = cached.get("source_updated_at")
    fetch_age = max(0.0, (now - fetched_at).total_seconds())
    source_age = max(0.0, (now - source_updated_at).total_seconds()) if source_updated_at else float("inf")
    stale = fetch_age > _MAX_FETCH_AGE_SECONDS or source_age > _MAX_SOURCE_AGE_SECONDS
    available = not stale
    return {
        "available": available,
        "rate": cached["rate"] if available else None,
        "last_known_rate": cached["rate"],
        "source": cached.get("source", "open.er-api.com"),
        "fetched_at": _iso(fetched_at),
        "source_updated_at": _iso(source_updated_at),
        "age_hours": round(fetch_age / 3600, 2),
        "source_age_hours": round(source_age / 3600, 2) if source_updated_at else None,
        "stale": stale,
        "error": (error or "The quote is stale; INR conversions are hidden to avoid showing misleading values.") if stale else None,
        "accounting_currency": "USD",
    }


def get_usd_inr_rate(*, force: bool = False, allow_network: bool = True) -> dict[str, Any]:
    """Return a cached quote, refreshing at most hourly unless explicitly forced.

    If the provider is down, only a quote within the freshness window is exposed as
    usable; older rates are labelled stale and are not used by the dashboard.
    """
    global _LAST_GOOD, _LAST_ATTEMPT, _LAST_ERROR
    now = _utc_now()
    with _LOCK:
        cache_age = (now - _LAST_GOOD["fetched_at"]).total_seconds() if _LAST_GOOD else float("inf")
        attempted_age = (now - _LAST_ATTEMPT).total_seconds() if _LAST_ATTEMPT else float("inf")
        if not force and _LAST_GOOD and cache_age < _CACHE_TTL_SECONDS:
            return _response(now=now, cached=_LAST_GOOD, error=_LAST_ERROR)
        if not allow_network:
            return _response(now=now, cached=_LAST_GOOD, error=_LAST_ERROR or "No cached USD/INR quote is available yet; use Refresh FX to request one.")
        # The source is daily and anti-abuse throttled; a manual refresh should not
        # be able to turn the button into a high-frequency external API poll.
        if _LAST_ATTEMPT and attempted_age < 300:
            result = _response(now=now, cached=_LAST_GOOD, error=_LAST_ERROR)
            result["refresh_throttled"] = True
            result["retry_after_seconds"] = max(1, int(300 - attempted_age))
            return result

        _LAST_ATTEMPT = now
        try:
            response = requests.get(_URL, timeout=(1.5, 2.5), headers={"User-Agent": "GPU-Profit-Hunter/0.7.2"})
            response.raise_for_status()
            payload = response.json()
            rate = float((payload.get("rates") or {}).get("INR"))
            if payload.get("result") != "success" or not (rate > 0 and rate < 1000):
                raise ValueError("FX provider returned an invalid USD/INR quote")
            source_updated_at = None
            try:
                source_updated_at = datetime.fromtimestamp(float(payload["time_last_update_unix"]), tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                pass
            _LAST_GOOD = {
                "rate": rate,
                "fetched_at": now,
                "source_updated_at": source_updated_at,
                "source": "open.er-api.com",
            }
            _LAST_ERROR = None
        except Exception as exc:  # network/provider errors should not take the dashboard down
            _LAST_ERROR = f"Could not refresh USD/INR: {str(exc)[:160]}"
        result = _response(now=_utc_now(), cached=_LAST_GOOD, error=_LAST_ERROR)
        result["refresh_throttled"] = False
        result["retry_after_seconds"] = 0
        return result


def usd_to_inr(amount_usd: Any, rate: Any) -> float | None:
    """Convert a finite USD amount with a verified positive rate; otherwise return None."""
    try:
        amount = float(amount_usd)
        fx_rate = float(rate)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (amount == amount and abs(amount) != float("inf") and fx_rate > 0 and fx_rate < 1000):
        return None
    return round(amount * fx_rate, 2)
