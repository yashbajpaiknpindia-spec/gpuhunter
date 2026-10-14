from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class RiskLimits:
    min_reliability: float = 0.99
    max_gpu_hourly_usd: float = 0.25
    max_total_hourly_usd: float = 0.30
    max_daily_spend_usd: float = 2.0
    min_revenue_cost_ratio: float = 1.50
    min_net_profit_usd_day: float = 0.50
    revenue_haircut: float = 0.30
    bandwidth_reserve_usd_day: float = 0.05
    default_pool_fee_pct: float = 1.0
    default_miner_fee_pct: float = 2.0


def parse_num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        txt = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def offer_hourly_cost(offer: dict[str, Any]) -> tuple[float, float, float]:
    """Return GPU rate, estimated all-in hourly rate, and disk rate.

    Vast's dph_total is generally the current total hourly offer price. To avoid undercounting,
    we compare it with base + disk when those itemized fields exist and use the larger value.
    """
    base = parse_num(offer.get("dph_base"), parse_num(offer.get("dph_total"), 0.0))
    disk = parse_num(offer.get("dph_disk"), 0.0)
    total = parse_num(offer.get("dph_total"), base + disk)
    all_in = max(total, base + disk)
    return max(base, 0.0), max(all_in, 0.0), max(disk, 0.0)


def evaluate_profit(
    *,
    gross_day_usd: float,
    hourly_cost_usd: float,
    gpu_hourly_usd: float,
    reliability: float,
    data_fresh: bool,
    profile_available: bool,
    pool_fee_pct: float,
    miner_fee_pct: float,
    limits: RiskLimits,
    live_capability_ready: bool = False,
    additional_reasons: list[str] | None = None,
) -> dict[str, Any]:
    reasons = list(additional_reasons or [])
    if gross_day_usd <= 0:
        reasons.append("Profitability provider returned no positive gross revenue")
    if not data_fresh:
        reasons.append("Profitability data is stale or its freshness cannot be verified")
    if reliability < limits.min_reliability:
        reasons.append(f"Host reliability {reliability:.3f} is below {limits.min_reliability:.3f}")
    # A cap of 0 disables only the fixed hourly price ceiling. Profitability, freshness,
    # daily budget, and host-reliability gates still apply. This lets the scanner compare
    # any GPU model on its expected economics instead of rejecting it on price alone.
    if limits.max_gpu_hourly_usd > 0 and gpu_hourly_usd > limits.max_gpu_hourly_usd:
        reasons.append(f"GPU rate ${gpu_hourly_usd:.4f}/h exceeds cap ${limits.max_gpu_hourly_usd:.4f}/h")
    if limits.max_total_hourly_usd > 0 and hourly_cost_usd > limits.max_total_hourly_usd:
        reasons.append(f"All-in rate ${hourly_cost_usd:.4f}/h exceeds cap ${limits.max_total_hourly_usd:.4f}/h")

    conservative_gross = max(gross_day_usd, 0) * (1 - min(max(limits.revenue_haircut, 0), 0.95))
    fees = min(max(pool_fee_pct, 0), 100) / 100 + min(max(miner_fee_pct, 0), 100) / 100
    after_fees = conservative_gross * max(0, 1 - fees)
    rent_day = max(hourly_cost_usd, 0) * 24
    total_cost_day = rent_day + max(limits.bandwidth_reserve_usd_day, 0)
    net_day = after_fees - total_cost_day
    ratio = after_fees / total_cost_day if total_cost_day > 0 else 0.0

    if ratio < limits.min_revenue_cost_ratio:
        reasons.append(f"Conservative revenue/cost ratio {ratio:.2f}x is below {limits.min_revenue_cost_ratio:.2f}x")
    if net_day < limits.min_net_profit_usd_day:
        reasons.append(f"Conservative net ${net_day:.3f}/day is below ${limits.min_net_profit_usd_day:.3f}/day minimum")
    if not profile_available:
        reasons.append("No enabled miner profile + wallet for this coin/algorithm/GPU; scan only")
    if not live_capability_ready:
        reasons.append("Live automation prerequisites are not all configured")

    # Distinguish whether this opportunity passes economics from whether it can be auto-rented.
    economics_ok = not any(
        x.startswith((
            "Profitability provider returned", "Profitability data is stale", "Host reliability",
            "GPU rate", "All-in rate", "Conservative revenue/cost ratio", "Conservative net $"
        )) for x in reasons
    )
    return {
        "gross_day_usd": round(max(gross_day_usd, 0), 6),
        "conservative_gross_day_usd": round(conservative_gross, 6),
        "after_fees_day_usd": round(after_fees, 6),
        "rent_day_usd": round(rent_day, 6),
        "total_cost_day_usd": round(total_cost_day, 6),
        "expected_net_day_usd": round(net_day, 6),
        "expected_net_hour_usd": round(net_day / 24, 6),
        "revenue_cost_ratio": round(ratio, 4),
        "economics_qualified": bool(economics_ok),
        "qualified": bool(economics_ok and profile_available and live_capability_ready),
        "reasons": reasons,
    }
