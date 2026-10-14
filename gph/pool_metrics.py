from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import requests

HASH_UNITS = {
    "h/s": 1.0, "hs": 1.0, "kh/s": 1e3, "khs": 1e3,
    "mh/s": 1e6, "mhs": 1e6, "gh/s": 1e9, "ghs": 1e9,
    "th/s": 1e12, "ths": 1e12, "ph/s": 1e15, "phs": 1e15,
}


def value_at_path(data: Any, path: str | None) -> Any:
    if not path:
        return None
    value = data
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value


def _num(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else None
    except (TypeError, ValueError):
        return None


class PoolMetricsClient:
    """Fetches a profile-defined, read-only pool JSON endpoint.

    The required revenue path must report a current USD/day run-rate estimate (or the profile must
    point at a trusted adapter that converts native rewards into USD/day). An optional
    realized_revenue_24h_usd_path may report the actual total earned in the trailing 24 hours;
    never map a USD/day projection into that realized field. This prevents projections being
    labeled as achieved earnings.
    """
    def __init__(self, timeout: int = 12):
        self.timeout = timeout

    def fetch(self, profile: dict[str, Any], wallet: str, worker: str, coin_tag: str) -> dict[str, Any]:
        cfg = profile.get("pool_metrics") or {}
        url_template = str(cfg.get("url") or "").strip()
        revenue_path = str(cfg.get("revenue_24h_usd_path") or "").strip()
        if not url_template or not revenue_path:
            raise RuntimeError("Pool metrics URL and revenue_24h_usd_path are required for live mining")
        values = {
            "wallet": quote(wallet, safe=""),
            "worker": quote(worker, safe=""),
            "coin_tag": quote(coin_tag, safe=""),
        }
        url = url_template
        for key, val in values.items():
            url = url.replace("{" + key + "}", val)
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise RuntimeError("Pool metrics URL must use HTTPS")
        headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "GPU-Profit-Hunter/0.1"}
        bearer_env = str(cfg.get("bearer_token_env") or "").strip()
        if bearer_env:
            token = os.getenv(bearer_env, "").strip()
            if not token:
                raise RuntimeError(f"Pool API token environment variable {bearer_env} is not set")
            headers["Authorization"] = "Bearer " + token
        try:
            response = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"Pool metrics connection failed: {exc.__class__.__name__}") from exc
        if not response.ok:
            raise RuntimeError(f"Pool metrics API returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Pool metrics API returned non-JSON response") from exc

        revenue = _num(value_at_path(data, revenue_path))
        if revenue is None or revenue < 0:
            raise RuntimeError("Pool metrics response has no valid non-negative USD/day field at the configured path")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result: dict[str, Any] = {
            # revenue_24h_usd is a current USD/day run-rate estimate used by safety gates,
            # not necessarily money actually earned over the trailing 24 hours.
            "revenue_24h_usd": revenue,
            "updated_at": updated_at,
            "source": str(cfg.get("name") or cfg.get("provider") or parts.hostname),
        }
        # Only populate this optional field when the profile explicitly points at a
        # realized trailing-24h USD amount. Never infer realized income from USD/day.
        realized_path = str(cfg.get("realized_revenue_24h_usd_path") or "").strip()
        if realized_path:
            realized = _num(value_at_path(data, realized_path))
            if realized is not None and realized >= 0:
                result["realized_revenue_24h_usd"] = realized
                result["realized_revenue_updated_at"] = updated_at
        hashrate_path = cfg.get("hashrate_hs_path")
        hashrate = _num(value_at_path(data, str(hashrate_path) if hashrate_path else None))
        if hashrate is not None:
            unit = str(cfg.get("hashrate_unit") or "H/s").lower().replace(" ", "")
            result["pool_hashrate_hs"] = hashrate * HASH_UNITS.get(unit, 1.0)
        accepted_path = cfg.get("accepted_shares_path")
        accepted = _num(value_at_path(data, str(accepted_path) if accepted_path else None))
        if accepted is not None:
            result["pool_accepted_shares"] = accepted
        balance_path = cfg.get("balance_usd_path")
        balance = _num(value_at_path(data, str(balance_path) if balance_path else None))
        if balance is not None:
            result["pool_balance_usd"] = balance
        return result
