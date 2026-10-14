from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .db import Database, now_iso
from .profiles import load_profiles, match_gpu_name, match_profile, normalized_gpu, wallet_for_profile
from .profit import RiskLimits, evaluate_profit, offer_hourly_cost, parse_iso_date, parse_num
from .providers.vast import VastClient, VastRateLimitError
from .search_budget import VastSearchBudget
from .providers.whattomine import WhatToMineClient


def _algokey(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _tag(text: Any) -> str:
    return str(text or "").strip().upper()


def _gpu_benchmark_for_offer(offer_name: str, gpus: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [g for g in gpus if match_gpu_name(offer_name, str(g.get("name", "")))]
    if not matches:
        return None
    # Prefer exact normalized match; the normalizer handles vendors/spacing in match_gpu_name.
    return matches[0]


def _freshness(coin_row: dict[str, Any], coin_ref: dict[str, Any] | None) -> tuple[bool, str]:
    updated = coin_row.get("last_update") or (coin_ref or {}).get("last_update")
    if not updated:
        return False, "unknown"
    dt = parse_iso_date(updated)
    if not dt:
        return False, "unknown"
    age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    return age_min <= 120, f"{max(age_min, 0):.0f} minutes"


def _build_coin_map(coins: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for coin in coins:
        tag = _tag(coin.get("tag") or coin.get("symbol"))
        if tag:
            result[tag] = coin
    return result


def _optional_bool(value: Any) -> bool | None:
    """Parse provider boolean fields without treating missing values as False/True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _offer_safety_reasons(offer: dict[str, Any], min_reliability: float) -> tuple[list[str], float, int, bool | None, bool | None]:
    """Assess an offer for display and live-rental safety, including unknown metadata."""
    stage = str(offer.get("_gph_search_stage") or "").lower()
    strict_query_passed = stage == "strict"
    reasons: list[str] = []
    gpu_count_raw = offer.get("num_gpus")
    gpu_count = int(parse_num(gpu_count_raw, 1 if strict_query_passed else 0))
    if gpu_count_raw is None and not strict_query_passed:
        reasons.append("Vast omitted GPU count in the broad query; cannot confirm this is a single-GPU offer")
    elif gpu_count != 1:
        reasons.append(f"Offer has {gpu_count} GPUs; this engine rents one-GPU offers only")

    verified = _optional_bool(offer.get("verified"))
    rentable = _optional_bool(offer.get("rentable"))
    if verified is None and strict_query_passed:
        verified = True  # the strict API query already required verified=true
    if rentable is None and strict_query_passed:
        rentable = True  # the strict API query already required rentable=true
    if verified is False:
        reasons.append("Vast explicitly marks this host as unverified")
    elif verified is None and not strict_query_passed:
        reasons.append("Vast response omitted verification status in the broad fallback query; manual confirmation required")
    if rentable is False:
        reasons.append("Vast explicitly marks this offer as not rentable")
    elif rentable is None and not strict_query_passed:
        reasons.append("Vast response omitted rentability status in the broad fallback query; availability is uncertain")

    reliability_raw = offer.get("reliability")
    if reliability_raw is None and strict_query_passed:
        reliability = float(min_reliability)  # server already applied the strict reliability predicate
    else:
        reliability = parse_num(reliability_raw, 0.0)
        if reliability_raw is None:
            reasons.append("Vast response omitted host reliability in the broad fallback query")
    if reliability < min_reliability:
        reasons.append(f"Host reliability {reliability:.3f} is below configured minimum {min_reliability:.3f}")
    return reasons, reliability, gpu_count, verified, rentable


class OpportunityScanner:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        # Keep provider clients alive between scans so their TTL caches reduce API calls/quota use.
        self._vast_client: VastClient | None = None
        self._wtm_client: WhatToMineClient | None = None
        self.search_budget = VastSearchBudget(
            db, daily_rows=config.vast_daily_row_budget, reserve_rows=config.vast_row_reserve,
            strict_limit=config.vast_search_limit, audit_limit=config.vast_audit_limit,
            audit_interval_seconds=config.vast_audit_interval_seconds,
        )
        self.search_budget.ensure_loaded()
        # Optional SEARCH-ONLY Vast keys (other accounts). They are used for GET /bundles/ offer
        # searches exclusively, each with its own persisted daily row counter. Renting, payment,
        # balance, monitoring and destroy ALWAYS use config.vast_api_key (the funded account).
        self._extra_slots: list[tuple[str, VastClient, VastSearchBudget]] = []
        main_key = (config.vast_api_key or "").strip()
        seen = {main_key}
        for key in tuple(getattr(config, "vast_search_api_keys", ()) or ()):
            key = (key or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            n = len(self._extra_slots) + 1
            fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
            extra_budget = VastSearchBudget(
                db, daily_rows=config.vast_daily_row_budget, reserve_rows=0,
                strict_limit=config.vast_search_limit, audit_limit=config.vast_audit_limit,
                audit_interval_seconds=config.vast_audit_interval_seconds,
                setting_key=f"vast_search_budget_{fingerprint}",
            )
            extra_budget.ensure_loaded()
            self._extra_slots.append((f"search-key-{n}", VastClient(key, config.vast_api_base), extra_budget))

    def _search_slots(self) -> list[tuple[str, VastClient, VastSearchBudget]]:
        """Search order: extra search-only keys first, the funded main key last, so the main key's
        daily rows stay available for pre-rental revalidation (get_offer)."""
        if self._vast_client is None:
            self._vast_client = VastClient(self.config.vast_api_key, self.config.vast_api_base)
        return list(self._extra_slots) + [("main", self._vast_client, self.search_budget)]

    def search_pacing(self) -> tuple[float, float]:
        """(recommended interval, cooldown) of the search key that can search soonest."""
        budgets = [b for _, _, b in self._extra_slots] + [self.search_budget]
        best = min(budgets, key=lambda b: max(b.recommended_interval(), b.cooldown_remaining()))
        return best.recommended_interval(), best.cooldown_remaining()

    def search_status(self) -> list[dict[str, Any]]:
        out = []
        for label, _client, b in [(l, c, bb) for l, c, bb in self._extra_slots] + [("main", None, self.search_budget)]:
            st = b.status()
            out.append({"key": label, "rows_used_today": st["rows_used_today"],
                        "rows_remaining_today": st["rows_remaining_today"],
                        "cooldown_seconds": st["cooldown_seconds"]})
        return out

    def _limits(self) -> RiskLimits:
        return RiskLimits(
            min_reliability=self.config.min_reliability,
            max_gpu_hourly_usd=self.config.max_gpu_hourly_usd,
            max_total_hourly_usd=self.config.max_total_hourly_usd,
            max_daily_spend_usd=self.config.max_daily_spend_usd,
            min_revenue_cost_ratio=self.config.min_revenue_cost_ratio,
            min_net_profit_usd_day=self.config.min_net_profit_usd_day,
            revenue_haircut=self.config.revenue_haircut,
            bandwidth_reserve_usd_day=self.config.bandwidth_reserve_usd_day,
            default_pool_fee_pct=self.config.default_pool_fee_pct,
            default_miner_fee_pct=self.config.default_miner_fee_pct,
        )

    def _pick_search_slot(self, slots):
        """First search key whose budget allows a search now; else raise the soonest-resuming error."""
        errors = []
        for label, client, budget in slots:
            try:
                return label, client, budget, budget.plan()
            except VastRateLimitError as exc:
                errors.append(exc)
        raise min(errors, key=lambda e: float(getattr(e, "retry_after", 0) or 0))

    def _run_vast_search(self, vast, plan, progress):
        provider_search: dict[str, Any]
        detailed_search = getattr(vast, "search_offers_with_diagnostics", None)
        if callable(detailed_search):
            provider_search = detailed_search(
                limit=plan["strict_limit"], min_reliability=self.config.min_reliability,
                progress_callback=progress,
                include_audit=plan["include_audit"], audit_limit=plan["audit_limit"] or None,
                audit_skip_reason=plan["audit_skip_reason"],
                order=self.config.vast_search_order,
                max_total_hourly_usd=self.config.max_total_hourly_usd,
            )
            offers = list(provider_search.get("offers") or [])
        else:
            offers = vast.search_offers(limit=plan["strict_limit"], min_reliability=self.config.min_reliability)
            provider_search = {
                "offers": offers, "raw_count": len(offers), "strict_count": len(offers),
                "fallback_count": 0, "fallback_used": False, "query_stage": "legacy-client",
                "rows_returned": len(offers),
                "diagnostic": "Provider client does not expose detailed query diagnostics.",
            }
        return offers, provider_search

    def scan(self, progress_callback=None) -> dict[str, Any]:
        progress = progress_callback or (lambda stage, percent, detail: None)
        progress("Validating provider configuration", 3, "Checking required Vast and WhatToMine credentials")
        if not self.config.vast_api_key:
            raise RuntimeError("VAST_API_KEY is missing. Add it to .env to scan current rentals.")
        if not self.config.whattomine_api_token:
            raise RuntimeError("WHATTOMINE_API_TOKEN is missing. Add a WhatToMine API token to calculate mining estimates.")

        if self._wtm_client is None:
            self._wtm_client = WhatToMineClient(self.config.whattomine_api_token)
        wtm = self._wtm_client
        slots = self._search_slots()
        tried = 0
        while True:
            # Raises VastQuotaError, without sending any request, when every search key is in a
            # 429 window or has spent its local daily row budget. The previous results are kept.
            label, vast, budget, plan = self._pick_search_slot(slots)
            via = "" if len(slots) == 1 else f" via {label} key"
            progress("Requesting current Vast GPU offers", 10,
                     f"Searching up to {plan['strict_limit']} verified, rentable single-GPU offers{via} "
                     f"({budget.rows_used_today():,}/{budget.usable_rows:,} daily search rows used)")
            try:
                offers, provider_search = self._run_vast_search(vast, plan, progress)
                provider_search["search_key"] = label
                break
            except VastRateLimitError as exc:
                # Vast refused the search (HTTP 429). Persist the cool-down so neither this process nor
                # a restarted one retries before the window closes; the last good results are kept.
                budget.note_rate_limit(exc.retry_after, quota=exc.quota, reason=str(exc))
                tried += 1
                if tried >= len(slots):
                    raise
        rows_returned = int(provider_search.get("rows_returned") or provider_search.get("raw_count") or 0)
        budget.record_rows(rows_returned)
        if provider_search.get("audit_ran"):
            budget.mark_audit_ran()
        late_limit = provider_search.get("rate_limit")
        if late_limit:  # the strict query worked but the audit query was refused
            budget.note_rate_limit(late_limit.get("retry_after") or 60, quota=bool(late_limit.get("quota")),
                                   reason=str(provider_search.get("fallback_error") or ""))
        provider_search["rows_used_today"] = budget.rows_used_today()
        provider_search["daily_row_budget"] = budget.daily_rows
        provider_search["usable_daily_rows"] = budget.usable_rows
        provider_search.pop("offers", None)
        if not offers and (provider_search.get("strict_error") or provider_search.get("fallback_error")):
            # A provider/network failure is not "the market is empty". Abort before the save
            # step so the last good opportunity snapshot is kept instead of being wiped.
            detail = provider_search.get("strict_error") or provider_search.get("fallback_error")
            raise RuntimeError(f"Vast offer search failed, so the previous results were kept: {detail}")
        progress("Loading WhatToMine GPU benchmarks", 20, f"Vast returned {len(offers)} offers ({provider_search.get('query_stage', 'unknown')} query); requesting GPU benchmark catalogue")
        gpus = wtm.get_gpus()
        progress("Loading supported coin and algorithm catalogue", 25, f"Received {len(gpus)} GPU benchmark records")
        coins = wtm.get_coins()
        progress("Preparing profitability calculations", 29, f"Loaded {len(coins)} coin records; matching offers to supported GPU models")
        coin_map = _build_coin_map(coins)
        profiles = load_profiles(self.config.miner_profiles_path)

        # Optional independent Kryptex cross-check. Only use values whose field is explicitly
        # labelled as daily revenue, and only while the Kryptex snapshot is fresh. Its GPU pages
        # can expose annual and daily statistics side-by-side, so generic "profit"/"revenue" is
        # deliberately not consumed here.
        kryptex_daily_index: dict[tuple[str, str, str], float] = {}
        kryptex_snapshot = self.db.get_setting("kryptex_gpu_market", {}) or {}
        kryptex_snapshot_updated = parse_iso_date(kryptex_snapshot.get("updated_at")) if isinstance(kryptex_snapshot, dict) else None
        kryptex_max_age = max(180, int(getattr(self.config, "kryptex_gpu_scan_interval_seconds", 60)) * 3)
        kryptex_snapshot_fresh = bool(
            getattr(self.config, "kryptex_gpu_reference_enabled", False)
            and kryptex_snapshot_updated
            and (datetime.now(timezone.utc) - kryptex_snapshot_updated).total_seconds() <= kryptex_max_age
        )
        coin_name_to_tag: dict[str, str] = {}
        for tag, coin_ref_item in coin_map.items():
            coin_name_to_tag[str(tag).casefold()] = tag
            coin_name_to_tag[str(coin_ref_item.get("name") or "").casefold()] = tag
            coin_name_to_tag[str(coin_ref_item.get("symbol") or "").casefold()] = tag
        if kryptex_snapshot_fresh:
            for model in kryptex_snapshot.get("models", []) if isinstance(kryptex_snapshot, dict) else []:
                gpu_key = normalized_gpu(str(model.get("gpu_name") or ""))
                if not gpu_key or not model.get("available"):
                    continue
                for coin_row in model.get("coin_rows", []) or []:
                    daily_revenue = coin_row.get("daily_revenue_usd")
                    algo = _algokey(coin_row.get("algorithm"))
                    raw_coin = str(coin_row.get("coin") or "").strip().casefold()
                    tag = coin_name_to_tag.get(raw_coin, raw_coin.upper())
                    if daily_revenue is None or not algo or not tag:
                        continue
                    try:
                        value = float(daily_revenue)
                    except (TypeError, ValueError):
                        continue
                    if value >= 0:
                        kryptex_daily_index[(gpu_key, tag.upper(), algo)] = value

        # Warm calculations once per distinct GPU model, not once for each marketplace offer.
        calc_cache: dict[str, list[dict[str, Any]]] = {}
        bench_cache: dict[str, dict[str, Any] | None] = {}
        opportunities: list[dict[str, Any]] = []
        offer_diagnostics: list[dict[str, Any]] = []
        unknown_gpu_count = 0
        gpu_models_seen: set[str] = set()
        errors: list[str] = []
        scanned_at = now_iso()
        live_blockers = self.config.live_blockers(profiles)
        live_capability_ready = not live_blockers

        total_offers = max(1, len(offers))
        for offer_index, offer in enumerate(offers, start=1):
            if offer_index == 1 or offer_index % 10 == 0 or offer_index == len(offers):
                progress("Evaluating GPU / coin economics", 30 + int(45 * offer_index / total_offers), f"Evaluating offer {offer_index} of {len(offers)}; reusing cached calculations per GPU model")
            offer_id_raw = offer.get("id") or offer.get("offer_id")
            try:
                offer_id = int(offer_id_raw)
            except (TypeError, ValueError):
                errors.append(f"Vast returned an offer without a numeric id at result index {offer_index}")
                missing_name = str(offer.get("gpu_name") or offer.get("gpu_name_display") or offer.get("model_name") or "Unknown GPU")
                offer_diagnostics.append({
                    "offer_id": str(offer_id_raw or "missing"), "gpu_name": missing_name,
                    "region": offer.get("geolocation") or offer.get("country") or offer.get("region") or "Unknown",
                    "gpu_count": int(parse_num(offer.get("num_gpus"), 0)),
                    "gpu_hourly_usd": round(parse_num(offer.get("dph_total"), parse_num(offer.get("dph_base"), 0)), 6),
                    "total_hourly_usd": round(offer_hourly_cost(offer)[1], 6),
                    "reliability": parse_num(offer.get("reliability"), 0.0),
                    "verified": _optional_bool(offer.get("verified")), "rentable": _optional_bool(offer.get("rentable")),
                    "search_stage": str(offer.get("_gph_search_stage") or provider_search.get("query_stage") or "unknown"),
                    "benchmark_matched": False, "benchmark_gpu_name": None,
                    "coin_pairs_evaluated": 0, "economically_positive_pairs": 0,
                    "best_expected_net_day_usd": None, "status": "REJECTED — missing numeric offer ID",
                    "reasons": ["Vast response did not include a usable numeric offer ID; cannot safely create an instance."],
                    "scanned_at": scanned_at,
                })
                continue
            gpu_name = str(offer.get("gpu_name") or offer.get("gpu_name_display") or offer.get("model_name") or "Unknown GPU")
            safety_reasons, reliability, normalized_gpu_count, verified_status, rentable_status = _offer_safety_reasons(offer, self.config.min_reliability)
            gpu_rate, total_rate, disk_rate = offer_hourly_cost(offer)
            review: dict[str, Any] = {
                "offer_id": offer_id, "gpu_name": gpu_name,
                "region": offer.get("geolocation") or offer.get("country") or offer.get("region") or "Unknown",
                "gpu_count": normalized_gpu_count, "gpu_hourly_usd": round(gpu_rate, 6),
                "total_hourly_usd": round(total_rate, 6), "reliability": round(reliability, 5),
                "verified": verified_status, "rentable": rentable_status,
                "search_stage": str(offer.get("_gph_search_stage") or provider_search.get("query_stage") or "unknown"),
                "benchmark_matched": False, "benchmark_gpu_name": None,
                "coin_pairs_evaluated": 0, "economically_positive_pairs": 0,
                "best_expected_net_day_usd": None, "status": "Evaluating",
                "reasons": list(safety_reasons), "scanned_at": scanned_at,
            }
            if gpu_name and gpu_name != "Unknown GPU":
                gpu_models_seen.add(gpu_name.strip())
            key = gpu_name.lower()
            if key not in bench_cache:
                bench_cache[key] = _gpu_benchmark_for_offer(gpu_name, gpus)
            bench = bench_cache[key]
            if not bench:
                unknown_gpu_count += 1
                review["status"] = "REJECTED — no GPU benchmark match"
                review["reasons"].append(f"No WhatToMine GPU benchmark matched Vast model name: {gpu_name}")
                offer_diagnostics.append(review)
                continue
            review["benchmark_matched"] = True
            bname = str(bench.get("name", ""))
            review["benchmark_gpu_name"] = bname
            review_opportunity_start = len(opportunities)
            if bname not in calc_cache:
                try:
                    calc_cache[bname] = wtm.calculate_gpu(bench)
                except Exception as exc:
                    errors.append(f"{bname}: {exc}")
                    calc_cache[bname] = []
            rows = calc_cache[bname]
            if not rows:
                review["status"] = "UNCERTAIN — no calculable WhatToMine rows"
                review["reasons"].append(f"WhatToMine returned no coin calculation rows for benchmark {bname}")
            for row in rows:
                coin_tag = _tag(row.get("tag") or row.get("symbol"))
                if not coin_tag:
                    continue
                coin_ref = coin_map.get(coin_tag)
                coin_name = str(row.get("name") or (coin_ref or {}).get("name") or coin_tag)
                algorithm = str(row.get("algorithm") or (coin_ref or {}).get("algorithm") or "")
                if not algorithm:
                    # We do not infer an algorithm by coin rank; unknown algorithm is scanner-only.
                    continue
                is_fresh, freshness_label = _freshness(row, coin_ref)
                if (row.get("lagging") is True) or (coin_ref or {}).get("lagging") is True:
                    is_fresh = False
                if (row.get("testing") is True) or (coin_ref or {}).get("testing") is True:
                    is_fresh = False
                whattomine_gross = parse_num(row.get("revenue24"), parse_num(row.get("revenue"), parse_num(row.get("profit24"), parse_num(row.get("profit"), 0))))
                gross = whattomine_gross
                kryptex_daily_revenue = None
                if kryptex_snapshot_fresh:
                    kryptex_daily_revenue = kryptex_daily_index.get((normalized_gpu(gpu_name), coin_tag, _algokey(algorithm)))
                    if kryptex_daily_revenue is not None:
                        gross = min(max(whattomine_gross, 0.0), max(kryptex_daily_revenue, 0.0))
                # Find benchmark hashrate for this coin's algorithm, retained for runtime monitor.
                bench_algo = next((a for a in bench.get("algorithms", []) if _algokey(a.get("name") or a.get("algorithm")) == _algokey(algorithm)), None)
                benchmark_hashrate_hs = parse_num((bench_algo or {}).get("hashrate"), 0)
                profile = match_profile(coin_tag, algorithm, gpu_name, profiles)
                wallet = wallet_for_profile(profile, self.config.wallets_path) if profile else ""
                pool_metrics_cfg = (profile or {}).get("pool_metrics") or {}
                pool_metrics_ready = bool(pool_metrics_cfg.get("url") and pool_metrics_cfg.get("revenue_24h_usd_path"))
                profile_ready = bool(profile and wallet and profile.get("onstart") and not str(profile.get("onstart", "")).lstrip().startswith("#") and (pool_metrics_ready or not self.config.require_pool_metrics_for_live))
                if profile and not wallet:
                    profile_reason = "Profile wallet is missing in data/wallets.json"
                elif profile and self.config.require_pool_metrics_for_live and not pool_metrics_ready:
                    profile_reason = "Profile needs a pool_metrics URL and revenue_24h_usd_path for loss monitoring"
                elif profile and not profile_ready:
                    profile_reason = "Profile start command is a placeholder or missing"
                else:
                    profile_reason = ""
                pool_fee = parse_num((coin_ref or {}).get("default_fee"), self.config.default_pool_fee_pct)
                miner_fee = parse_num((profile or {}).get("miner_fee_pct"), self.config.default_miner_fee_pct)
                extra_reasons: list[str] = list(safety_reasons)
                if (coin_ref or {}).get("lagging") or row.get("lagging"):
                    extra_reasons.append("Provider marks this coin as lagging")
                if (coin_ref or {}).get("testing") or row.get("testing"):
                    extra_reasons.append("Provider marks this coin as testing")
                p = evaluate_profit(
                    gross_day_usd=gross,
                    hourly_cost_usd=total_rate,
                    gpu_hourly_usd=gpu_rate,
                    reliability=reliability,
                    data_fresh=is_fresh,
                    profile_available=profile_ready,
                    pool_fee_pct=pool_fee,
                    miner_fee_pct=miner_fee,
                    limits=self._limits(),
                    live_capability_ready=live_capability_ready,
                    additional_reasons=extra_reasons,
                )
                if profile_reason:
                    p["reasons"].append(profile_reason)
                    p["qualified"] = False
                if safety_reasons:
                    p["qualified"] = False
                if not offer_id:
                    continue
                oid = hashlib.sha256(f"{offer_id}|{coin_tag}|{algorithm}".encode()).hexdigest()[:20]
                item = {
                    "id": oid,
                    "scanned_at": scanned_at,
                    "offer_id": int(offer_id),
                    "gpu_name": gpu_name,
                    "coin_tag": coin_tag,
                    "coin_name": coin_name,
                    "algorithm": algorithm,
                    "benchmark_gpu_name": bname,
                    "benchmark_hashrate_hs": benchmark_hashrate_hs,
                    "whattomine_gross_day_usd": round(max(whattomine_gross, 0.0), 6),
                    "kryptex_daily_revenue_usd": round(kryptex_daily_revenue, 6) if kryptex_daily_revenue is not None else None,
                    "gross_crosscheck_source": "lower of WhatToMine and Kryptex explicitly daily revenue" if kryptex_daily_revenue is not None else "WhatToMine; no matched fresh Kryptex daily revenue field",
                    "kryptex_reference_fresh": bool(kryptex_snapshot_fresh),
                    "gpu_hourly_usd": round(gpu_rate, 6),
                    "total_hourly_usd": round(total_rate, 6),
                    "disk_hourly_usd": round(disk_rate, 6),
                    "reliability": round(reliability, 5),
                    "verified": verified_status if verified_status is not None else (True if str(offer.get("_gph_search_stage") or "") == "strict" else False),
                    "rentable": rentable_status if rentable_status is not None else (True if str(offer.get("_gph_search_stage") or "") == "strict" else False),
                    "offer_safety_passed": not safety_reasons,
                    "search_stage": str(offer.get("_gph_search_stage") or provider_search.get("query_stage") or "unknown"),
                    "host_id": offer.get("host_id"),
                    "machine_id": offer.get("machine_id"),
                    "region": offer.get("geolocation") or offer.get("country") or offer.get("region") or "Unknown",
                    "gpu_count": normalized_gpu_count,
                    "data_fresh": is_fresh,
                    "data_freshness": freshness_label,
                    "data_updated_at": row.get("last_update") or (coin_ref or {}).get("last_update"),
                    "profile_id": profile.get("id") if profile else None,
                    "profile_available": profile_ready,
                    "pool_fee_pct": round(pool_fee, 3),
                    "miner_fee_pct": round(miner_fee, 3),
                    "gpu_memory_gb": parse_num(offer.get("gpu_ram"), 0),
                    **p,
                }
                opportunities.append(item)

            offer_rows = opportunities[review_opportunity_start:]
            review["coin_pairs_evaluated"] = len(offer_rows)
            review["economically_positive_pairs"] = sum(bool(x.get("economics_qualified")) for x in offer_rows)
            if offer_rows:
                best_row = max(offer_rows, key=lambda x: parse_num(x.get("expected_net_day_usd"), -1e9))
                review["best_expected_net_day_usd"] = best_row.get("expected_net_day_usd")
                # Keep all offer-level safety reasons, then include the best-scoring coin-pair rejection reasons.
                for reason in list(best_row.get("reasons") or []):
                    if reason not in review["reasons"]:
                        review["reasons"].append(reason)
                if not safety_reasons and best_row.get("qualified"):
                    review["status"] = "LIVE-QUALIFIED candidate"
                elif best_row.get("economics_qualified"):
                    review["status"] = "Economics positive — live gates not passed"
                else:
                    review["status"] = "REJECTED — no coin pair passed economics gates"
            else:
                review["status"] = "UNCERTAIN — no valid coin/algorithm rows"
                review["reasons"].append("No usable coin/algorithm rows were produced for this offer; inspect benchmark/provider coverage")
            offer_diagnostics.append(review)

        # Sort net descending; unknown/negative estimates remain visible for diagnosis but will never be rented.
        opportunities.sort(key=lambda x: (bool(x.get("qualified")), x.get("expected_net_day_usd", -999)), reverse=True)
        opportunities = opportunities[:1000]
        prior_rows = self.db.get_opportunities(limit=2000)
        prior_by_id = {str(x.get("id")): x for x in prior_rows}
        notable_changes = []
        for current in opportunities:
            previous = prior_by_id.get(str(current.get("id")))
            if not previous:
                continue
            rate_old = parse_num(previous.get("total_hourly_usd"), 0)
            rate_new = parse_num(current.get("total_hourly_usd"), 0)
            gross_old = parse_num(previous.get("gross_day_usd"), 0)
            gross_new = parse_num(current.get("gross_day_usd"), 0)
            rate_delta = (rate_new - rate_old) / rate_old if rate_old > 0 else 0.0
            gross_delta = (gross_new - gross_old) / gross_old if gross_old > 0 else 0.0
            if abs(rate_delta) >= 0.05 or abs(gross_delta) >= 0.10:
                notable_changes.append({
                    "gpu_name": current.get("gpu_name"), "offer_id": current.get("offer_id"),
                    "coin_tag": current.get("coin_tag"), "algorithm": current.get("algorithm"),
                    "rent_hourly_before_usd": rate_old, "rent_hourly_now_usd": rate_new,
                    "rent_change_pct": round(rate_delta * 100, 2),
                    "gross_day_before_usd": gross_old, "gross_day_now_usd": gross_new,
                    "gross_change_pct": round(gross_delta * 100, 2),
                    "net_day_now_usd": current.get("expected_net_day_usd"),
                })
        if notable_changes:
            notable_changes.sort(key=lambda x: max(abs(x["rent_change_pct"]), abs(x["gross_change_pct"])), reverse=True)
            self.db.log("INFO", "market_fluctuation_summary", {"changed_combinations": len(notable_changes), "top_changes": notable_changes[:20]})
        progress("Saving scan results and audit summary", 94, "Persisting ranked opportunities and scan diagnostics")
        self.db.replace_opportunities(opportunities)
        # Refresh active instances' revenue baseline with the latest provider calculation for the same GPU/coin.
        # If no corresponding fresh row exists, the live monitor later fails closed when data ages out.
        for managed in self.db.get_managed_instances():
            if str(managed.get("status", "")).lower() == "destroyed":
                continue
            meta = managed.get("metadata", {}) or {}
            match = next((x for x in opportunities
                          if x.get("coin_tag") == managed.get("coin_tag")
                          and normalized_gpu(str(x.get("gpu_name", ""))) == normalized_gpu(str(managed.get("gpu_name", "")))
                          and x.get("data_fresh") and parse_num(x.get("gross_day_usd"), 0) > 0), None)
            if match:
                managed["expected_gross_day"] = float(match.get("gross_day_usd", 0) or 0)
                managed["benchmark_hashrate_hs"] = float(match.get("benchmark_hashrate_hs", 0) or managed.get("benchmark_hashrate_hs", 0) or 0)
                managed["latest_gross_day"] = float(match.get("conservative_gross_day_usd", 0) or 0)
                meta["last_profitability_scan_at"] = scanned_at
                meta["latest_coin_data_updated_at"] = match.get("data_updated_at")
                managed["metadata_json"] = meta
                self.db.upsert_managed_instance(managed)
        rejection_counts: dict[str, int] = {}
        for review in offer_diagnostics:
            status_key = str(review.get("status") or "UNKNOWN")
            rejection_counts[status_key] = rejection_counts.get(status_key, 0) + 1
        self.db.set_setting("last_scan_offer_diagnostics", offer_diagnostics[:500])
        self.db.log("INFO", "market_scan_completed", {
            "offers_seen": len(offers), "strict_count": provider_search.get("strict_count", len(offers)),
            "fallback_count": provider_search.get("fallback_count", 0),
            "get_fallback_count": provider_search.get("get_fallback_count", 0),
            "query_stage": provider_search.get("query_stage"),
            "benchmark_gpus": len(gpus), "coin_rows": len(coins),
            "opportunities": len(opportunities), "qualified": sum(bool(x.get("qualified")) for x in opportunities),
            "offer_reviews": len(offer_diagnostics), "rejection_counts": rejection_counts,
            "unknown_gpu_models": unknown_gpu_count, "provider_diagnostic": provider_search.get("diagnostic"),
            "response_keys": provider_search.get("response_keys", [])[:20], "errors": errors[:10],
        })
        progress("Scan complete", 100, f"Saved {len(opportunities)} ranked opportunities")
        return {
            "scanned_at": scanned_at,
            "offers_seen": len(offers),
            "gpu_benchmarks": len(gpus),
            "coins_seen": len(coins),
            "opportunities": len(opportunities),
            "qualified": sum(bool(x.get("qualified")) for x in opportunities),
            "unknown_gpu_models": unknown_gpu_count,
            "gpu_models_seen": sorted(gpu_models_seen, key=str.casefold),
            "provider_diagnostics": {**provider_search, "returned_offer_count": len(offers)},
            "offer_diagnostics": offer_diagnostics[:500],
            "rejection_counts": rejection_counts,
            "errors": errors[:10],
            "items": opportunities[:100],
            "live_blockers": live_blockers,
        }
