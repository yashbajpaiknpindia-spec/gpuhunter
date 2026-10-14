"""Transparent dashboard-only funding and observed-profit calculations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .profit import parse_iso_date, parse_num

ACTIVE_STATUSES = {"running", "active", "loading", "created", "creating", "starting"}


def funding_plan(
    opportunities: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    target_instances: int,
    balance_status: dict[str, Any] | None,
    bandwidth_reserve_usd_day: float = 0.0,
) -> dict[str, Any]:
    """Estimate funding required to cover the target for the next 24 hours.

    Existing active rentals are costed at their current rate. Empty target slots use
    the strongest expected-net candidate from the current scan; an economically
    positive but not fully-live-qualified candidate is marked as planning-only.
    This estimate is not a billing quote and does not authorize spending.
    """
    live = [x for x in instances if str(x.get("status", "")).lower() in ACTIVE_STATUSES]
    target = max(0, int(target_instances or 0))
    reserve = max(0.0, parse_num(bandwidth_reserve_usd_day, 0.0))
    active_day = sum(max(0.0, parse_num(x.get("hourly_cost_usd"), 0.0)) * 24 + reserve for x in live)
    slots = max(0, target - len(live)) if target > 0 else 0

    candidates = [x for x in opportunities if bool(x.get("economics_qualified"))]
    candidate = max(candidates, key=lambda x: parse_num(x.get("expected_net_day_usd"), -1e9), default=None)
    required = active_day
    source = "active rentals only" if live else "scan-only target; no new rental planned"
    candidate_live_qualified = None
    candidate_day_cost = None
    if slots > 0:
        if candidate is None:
            required_value = None
            source = "waiting for an economically positive candidate"
        else:
            per_instance_day = parse_num(candidate.get("total_cost_day_usd"), None)
            if per_instance_day is None:
                per_instance_day = max(0.0, parse_num(candidate.get("total_hourly_usd"), 0.0)) * 24 + reserve
            candidate_day_cost = max(0.0, per_instance_day)
            required += candidate_day_cost * slots
            source = "best economic candidate; verify live gates before deployment"
            candidate_live_qualified = bool(candidate.get("qualified"))
            required_value = round(required, 4)
    else:
        required_value = round(required, 4)

    balance_usd = None
    balance_available = bool((balance_status or {}).get("available"))
    if balance_available:
        balance_usd = parse_num((balance_status or {}).get("balance_usd"), None)
        if balance_usd is None:
            balance_available = False

    shortfall = max(0.0, required_value - balance_usd) if required_value is not None and balance_available else None
    return {
        "target_instances": target,
        "active_instances": len(live),
        "additional_slots": slots,
        "active_rental_24h_usd": round(active_day, 4),
        "candidate_cost_per_instance_24h_usd": round(candidate_day_cost, 4) if candidate_day_cost is not None else None,
        "required_24h_usd": required_value,
        "required_24h_includes_current_active_and_target_slots": True,
        "estimate_source": source,
        "candidate_gpu_name": candidate.get("gpu_name") if candidate else None,
        "candidate_offer_id": candidate.get("offer_id") if candidate else None,
        "candidate_is_live_qualified": candidate_live_qualified,
        "wallet_balance_available": balance_available,
        "wallet_balance_usd": round(balance_usd, 4) if balance_available and balance_usd is not None else None,
        "shortfall_usd": round(shortfall, 4) if shortfall is not None else None,
        "covers_next_24h": bool(shortfall is not None and shortfall <= 0),
    }


def observed_net_24h(instances: list[dict[str, Any]], bandwidth_reserve_usd_day: float = 0.0) -> dict[str, Any]:
    """Compute an indicative rolling-24h net only when revenue and rent windows align.

    Requires every active rental to have fresh pool-reported 24h revenue and at least
    24 hours of known runtime. No pool balance is misrepresented as earnings.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    live = [x for x in instances if str(x.get("status", "")).lower() in ACTIVE_STATUSES]
    if not live:
        return {"net_24h_usd": None, "reason": "No active rental with an aligned 24-hour earnings window yet."}

    total = 0.0
    reserve = max(0.0, parse_num(bandwidth_reserve_usd_day, 0.0))
    for instance in live:
        # pool_revenue_24h_usd is only a current USD/day run-rate estimate. Achieved
        # profit requires a distinct explicitly configured realized trailing-24h amount.
        revenue = parse_num(instance.get("pool_realized_revenue_24h_usd"), None)
        started = parse_iso_date(instance.get("started_at"))
        updated = parse_iso_date(instance.get("pool_realized_revenue_updated_at") or instance.get("pool_metrics_updated_at"))
        if revenue is None:
            return {"net_24h_usd": None, "reason": "Achieved profit is not available: the configured pool connector reports a current USD/day run-rate, but no actual trailing-24-hour USD earnings total. Configure realized_revenue_24h_usd_path only if the pool API supplies that realized amount."}
        if started is None or started > cutoff:
            return {"net_24h_usd": None, "reason": "Waiting for at least 24 hours of rental history before reporting achieved net."}
        if updated is None or (now - updated).total_seconds() > 60 * 60:
            return {"net_24h_usd": None, "reason": "Realized pool earnings data is missing or older than 60 minutes; refresh telemetry before reporting achieved net."}
        rent = max(0.0, parse_num(instance.get("hourly_cost_usd"), 0.0)) * 24
        total += revenue - rent - reserve
    return {
        "net_24h_usd": round(total, 4),
        "reason": "Actual trailing-24-hour pool-reported USD earnings minus estimated 24-hour rental cost and configured bandwidth reserve. Any unreported fees, storage, payout adjustments, or differences in pool accounting are excluded.",
    }
