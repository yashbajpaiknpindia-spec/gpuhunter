from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def runtime_limit_reached(runtime_hours: float, configured_limit_hours: int) -> bool:
    """Return true only when a positive, fixed runtime limit has been reached. Zero means unlimited."""
    return configured_limit_hours > 0 and runtime_hours >= configured_limit_hours


def projected_commitment_hours(configured_limit_hours: int) -> int:
    """Budget a full day when runtime is unlimited; otherwise budget up to 24 hours."""
    return min(configured_limit_hours, 24) if configured_limit_hours > 0 else 24



def _vast_base() -> str:
    value = os.getenv("VAST_API_BASE", "https://cloud.vast.ai/api/v0").strip().rstrip("/")
    # Vast documents cloud.vast.ai as the REST API host. Upgrade the former console
    # host transparently so an old Render environment variable cannot preserve it.
    if value.startswith("https://console.vast.ai"):
        value = value.replace("https://console.vast.ai", "https://cloud.vast.ai", 1)
    return value or "https://cloud.vast.ai/api/v0"

@dataclass
class Config:
    app_host: str = os.getenv("APP_HOST", "127.0.0.1")
    app_port: int = _int("APP_PORT", 8088)
    secret_key: str = os.getenv("APP_SECRET_KEY", "local-dev-only-change-me")
    # Render deployments should set DATABASE_URL to the existing Render PostgreSQL instance.
    # DATABASE_PATH remains the local/dev fallback when no URL is configured.
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    database_path: str = os.getenv("DATABASE_PATH", str(ROOT / "data" / "gph.sqlite3"))
    vast_api_key: str = os.getenv("VAST_API_KEY", "").strip()
    # Expected account identity is public metadata; mismatch fails closed before displaying a verified balance.
    vast_expected_account_id: str = os.getenv("VAST_EXPECTED_ACCOUNT_ID", "32570835").strip()
    # Optional SEARCH-ONLY keys from other Vast accounts (comma/newline/semicolon separated). Never used to rent or pay.
    vast_search_api_keys: tuple = tuple(k.strip() for k in re.split(r"[,;\s]+", os.getenv("VAST_SEARCH_API_KEYS", "")) if k.strip())
    whattomine_api_token: str = os.getenv("WHATTOMINE_API_TOKEN", "").strip()
    kryptex_mining_username: str = os.getenv("KRYPTEX_MINING_USERNAME", "").strip()
    kryptex_coin: str = os.getenv("KRYPTEX_COIN", "prl").strip().lower()
    kryptex_worker: str = os.getenv("KRYPTEX_WORKER", "").strip()
    kryptex_poll_interval_seconds: int = _int("KRYPTEX_POLL_INTERVAL_SECONDS", 60)
    kryptex_gpu_scan_interval_seconds: int = _int("KRYPTEX_GPU_SCAN_INTERVAL_SECONDS", 60)
    # The Kryptex per-GPU route used by older versions is not listed in its public Pool API docs.
    # Keep it out of rental economics unless the operator has independently verified the endpoint.
    kryptex_gpu_reference_enabled: bool = _bool("KRYPTEX_GPU_REFERENCE_ENABLED", False)
    vast_api_base: str = _vast_base()
    automation_mode: str = os.getenv("AUTOMATION_MODE", "scan").strip().lower()
    enable_live_mining: bool = _bool("ENABLE_LIVE_MINING", False)
    mining_eligibility_confirmed: bool = _bool("MINING_ELIGIBILITY_CONFIRMED", False)
    require_pool_metrics_for_live: bool = _bool("REQUIRE_POOL_METRICS_FOR_LIVE", True)
    vast_credit_source: str = os.getenv("VAST_CREDIT_SOURCE", "unknown").strip().lower()
    ssh_private_key_path: str = os.getenv("SSH_PRIVATE_KEY_PATH", "").strip()
    ssh_connect_timeout_seconds: int = _int("SSH_CONNECT_TIMEOUT_SECONDS", 8)
    miner_profiles_path: str = os.getenv("MINER_PROFILES_PATH", str(ROOT / "data" / "mining_profiles.json"))
    wallets_path: str = os.getenv("WALLETS_PATH", str(ROOT / "data" / "wallets.json"))
    min_reliability: float = _float("MIN_RELIABILITY", 0.99)
    max_gpu_hourly_usd: float = _float("MAX_GPU_HOURLY_USD", 0.0)
    max_total_hourly_usd: float = _float("MAX_TOTAL_HOURLY_USD", 0.0)
    max_daily_spend_usd: float = _float("MAX_DAILY_SPEND_USD", 1.75)
    max_active_instances: int = max(1, min(25, _int("MAX_ACTIVE_INSTANCES", 3)))
    min_revenue_cost_ratio: float = _float("MIN_REVENUE_COST_RATIO", 1.50)
    min_net_profit_usd_day: float = _float("MIN_NET_PROFIT_USD_DAY", 0.50)
    revenue_haircut: float = _float("REVENUE_HAIRCUT", 0.30)
    bandwidth_reserve_usd_day: float = _float("BANDWIDTH_RESERVE_USD_DAY", 0.05)
    default_pool_fee_pct: float = _float("DEFAULT_POOL_FEE_PCT", 1.0)
    default_miner_fee_pct: float = _float("DEFAULT_MINER_FEE_PCT", 2.0)
    default_disk_gb: int = _int("DEFAULT_DISK_GB", 12)
    max_disk_gb: int = _int("MAX_DISK_GB", 20)
    auto_scan_enabled: bool = _bool("AUTO_SCAN_ENABLED", True)
    # Requested autonomous preference defaults on, but live mode and every spend/profitability gate remain mandatory.
    auto_rent_enabled: bool = _bool("AUTO_RENT_ENABLED", True)
    # Vast rental opportunities refresh every 60 seconds by default. Provider revenue
    # feeds retain their own quota-aware caches, so this does not spam WhatToMine.
    scan_interval_seconds: int = _int("SCAN_INTERVAL_SECONDS", 60)
    # Vast meters offer searches by ROWS returned against a daily quota (20,000/day on this
    # account), not by request count. The scan interval is therefore a floor: the supervisor
    # stretches it automatically so the day's rows last until the UTC reset.
    vast_daily_row_budget: int = _int("VAST_DAILY_ROW_BUDGET", 20000)
    vast_row_reserve: int = _int("VAST_ROW_RESERVE", 1500)  # kept free for pre-rental revalidation
    vast_search_limit: int = max(10, min(500, _int("VAST_SEARCH_LIMIT", 100)))
    vast_audit_limit: int = max(0, min(500, _int("VAST_AUDIT_LIMIT", 500)))  # broad hourly audit discovers more GPU models; quota-aware pacing remains in force
    vast_audit_interval_seconds: int = _int("VAST_AUDIT_INTERVAL_SECONDS", 3600)
    vast_search_order: str = os.getenv("VAST_SEARCH_ORDER", "dph_total").strip()  # "-field" = descending; "" = Vast default
    monitor_interval_seconds: int = _int("MONITOR_INTERVAL_SECONDS", 45)
    max_profit_data_age_minutes: int = _int("MAX_PROFIT_DATA_AGE_MINUTES", 15)
    startup_grace_minutes: int = _int("STARTUP_GRACE_MINUTES", 10)
    max_no_share_minutes: int = _int("MAX_NO_SHARE_MINUTES", 15)
    min_observed_hashrate_ratio: float = _float("MIN_OBSERVED_HASHRATE_RATIO", 0.70)
    max_instance_runtime_hours: int = _int("MAX_INSTANCE_RUNTIME_HOURS", 0)

    def public_dict(self) -> dict[str, Any]:
        return {
            "vast_configured": bool(self.vast_api_key),
            "vast_search_keys": len([k for k in self.vast_search_api_keys if k and k != self.vast_api_key]),
            "whattomine_configured": bool(self.whattomine_api_token),
            "kryptex_configured": bool(self.kryptex_mining_username and self.kryptex_coin),
            "kryptex_coin": self.kryptex_coin,
            "kryptex_worker_configured": bool(self.kryptex_worker),
            "kryptex_gpu_scan_interval_seconds": max(60, self.kryptex_gpu_scan_interval_seconds),
            "kryptex_gpu_reference_enabled": self.kryptex_gpu_reference_enabled,
            "ssh_key_configured": bool(self.ssh_private_key_path and Path(self.ssh_private_key_path).expanduser().exists()),
            "automation_mode": self.automation_mode,
            "enable_live_mining": self.enable_live_mining,
            "mining_eligibility_confirmed": self.mining_eligibility_confirmed,
            "require_pool_metrics_for_live": self.require_pool_metrics_for_live,
            "vast_credit_source": self.vast_credit_source,
            "min_reliability": self.min_reliability,
            "max_gpu_hourly_usd": self.max_gpu_hourly_usd,
            "max_total_hourly_usd": self.max_total_hourly_usd,
            "max_daily_spend_usd": self.max_daily_spend_usd,
            "max_active_instances": self.max_active_instances,
            "min_revenue_cost_ratio": self.min_revenue_cost_ratio,
            "min_net_profit_usd_day": self.min_net_profit_usd_day,
            "revenue_haircut_pct": round(self.revenue_haircut * 100, 1),
            "bandwidth_reserve_usd_day": self.bandwidth_reserve_usd_day,
            "default_disk_gb": self.default_disk_gb,
            "max_disk_gb": self.max_disk_gb,
            "auto_scan_enabled": self.auto_scan_enabled,
            "auto_rent_enabled_env": self.auto_rent_enabled,
            "scan_interval_seconds": max(60, self.scan_interval_seconds),
            "vast_daily_row_budget": self.vast_daily_row_budget,
            "vast_search_limit": self.vast_search_limit,
            "vast_audit_limit": self.vast_audit_limit,
            "max_active_instances_cap": 25,
            "max_ui_target_instances": 25,
            "max_instance_runtime_hours": max(0, self.max_instance_runtime_hours),
            "database_configured": bool(self.database_url or self.database_path),
            "database_backend": "postgresql" if self.database_url else "sqlite",
        }

    def setup_gaps(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Every missing/broken key or file, in plain words, so the UI can say exactly what to add.

        Pure in-memory/file checks: safe to call when the database is down.
        severity: "required" (core features off), "optional" (one panel off), "live" (only blocks paid mining).
        """
        from .profiles import wallet_file_status  # local import: profiles has no config dependency
        gaps: list[dict[str, Any]] = []

        def add(gap_id: str, label: str, where: str, severity: str, impact: str) -> None:
            gaps.append({"id": gap_id, "label": label, "where": where, "severity": severity, "impact": impact})

        if not self.vast_api_key:
            add("vast", "Vast.ai API key", "VAST_API_KEY", "required",
                "Wallet balance, offer scanning and instance management are off.")
        if not self.whattomine_api_token:
            add("whattomine", "WhatToMine API token", "WHATTOMINE_API_TOKEN", "required",
                "Mining revenue cannot be calculated, so no opportunities can be produced.")
        if not (self.kryptex_mining_username and self.kryptex_coin):
            add("kryptex", "Kryptex pool username / payout wallet", "KRYPTEX_MINING_USERNAME (or the Kryptex connection panel)", "optional",
                "Kryptex pool telemetry is off. Scanning and rental economics still work.")
        if not self.ssh_private_key_path or not Path(self.ssh_private_key_path).expanduser().exists():
            add("ssh", "SSH private key for miner monitoring", "SSH_PRIVATE_KEY_PATH", "live",
                "Live rentals cannot be monitored, so paid mining stays blocked.")
        wallet_file = wallet_file_status(self.wallets_path)
        if wallet_file.get("error"):
            add("wallet_file", "Payout wallet file", "data/wallets.json", "live", wallet_file["error"] + ". Mining payouts cannot be configured.")
        enabled = [p for p in profiles if p.get("enabled")]
        if not wallet_file.get("error") and wallet_file.get("count", 0) == 0 and not any(k.startswith("WALLET_") and v.strip() for k, v in os.environ.items()):
            add("wallet", "Payout wallet address", "data/wallets.json or a WALLET_<COIN> variable", "live",
                "No payout address is configured, so no profile can mine to a wallet.")
        elif enabled:
            from .profiles import wallet_for_profile
            missing = sorted({str(p.get("wallet_key") or p.get("id") or "?").upper() for p in enabled if not wallet_for_profile(p, self.wallets_path)})
            if missing:
                add("profile_wallets", "Wallet for enabled mining profile(s): " + ", ".join(missing),
                    "data/wallets.json (or WALLET_<COIN>)", "live", "Those profiles cannot start mining until a payout address is added.")
        if not enabled:
            add("profiles", "Enabled mining profile", "data/mining_profiles.json", "live",
                "No coin/algorithm has a reviewed miner command, so only scanning is possible.")
        return gaps

    def live_blockers(self, profiles: list[dict[str, Any]]) -> list[str]:
        blockers: list[str] = []
        if self.automation_mode != "live":
            blockers.append("AUTOMATION_MODE is not set to live")
        if not self.enable_live_mining:
            blockers.append("ENABLE_LIVE_MINING is false")
        if not self.mining_eligibility_confirmed:
            blockers.append("Vast mining-credit eligibility has not been explicitly confirmed")
        if self.vast_credit_source == "credit_card":
            blockers.append("Vast credits are marked as credit-card-funded; mining is prohibited by Vast terms")
        if self.vast_credit_source not in {"crypto", "promo", "other_eligible"}:
            blockers.append("VAST_CREDIT_SOURCE must be crypto, promo, or other_eligible after checking current terms")
        if not self.vast_api_key:
            blockers.append("VAST_API_KEY is missing")
        if not self.whattomine_api_token:
            blockers.append("WHATTOMINE_API_TOKEN is missing; profitability data would be unavailable")
        if not self.kryptex_mining_username:
            blockers.append("KRYPTEX_MINING_USERNAME is missing; Kryptex account/pool telemetry is not connected")
        if not self.auto_scan_enabled:
            blockers.append("AUTO_SCAN_ENABLED is false; live mode requires recurring market rechecks")
        if not self.ssh_private_key_path or not Path(self.ssh_private_key_path).expanduser().exists():
            blockers.append("SSH_PRIVATE_KEY_PATH is missing or file does not exist; remote miner monitoring cannot work")
        if not profiles:
            blockers.append("No enabled mining profiles are configured")
        elif not any(p.get("enabled") for p in profiles):
            blockers.append("No mining profile is enabled")
        if self.require_pool_metrics_for_live:
            metric_profiles = [
                p for p in profiles
                if p.get("enabled")
                and (p.get("pool_metrics") or {}).get("url")
                and (p.get("pool_metrics") or {}).get("revenue_24h_usd_path")
                and (p.get("pool_metrics") or {}).get("source_updated_at_path")
            ]
            if not metric_profiles:
                blockers.append(
                    "No enabled profile has a pool connector with USD/day revenue and a source update timestamp; "
                    "live mining requires both so stale cached rates cannot pass the safety gate"
                )
        if self.max_active_instances < 1 or self.max_daily_spend_usd <= 0:
            blockers.append("Risk limits are not valid")
        return blockers
