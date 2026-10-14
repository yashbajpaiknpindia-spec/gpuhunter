from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import requests


class KryptexAPIError(RuntimeError):
    pass


def _first_number(value: Any, keys: tuple[str, ...]) -> float | None:
    """Find a numeric field in documented API variants without assuming one response wrapper."""
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        for key in keys:
            if key in lowered:
                raw = lowered[key]
                if isinstance(raw, dict):
                    found = _first_number(raw, ("value", "amount", "hashrate", "balance", "total"))
                    if found is not None:
                        return found
                try:
                    n = float(str(raw).replace(",", ""))
                    if n == n and abs(n) != float("inf"):
                        return n
                except (ValueError, TypeError):
                    pass
        for nested in value.values():
            found = _first_number(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_number(item, keys)
            if found is not None:
                return found
    return None


def _short_error(response: requests.Response) -> str:
    body = (response.text or "").replace("\n", " ")[:160]
    return f"HTTP {response.status_code}" + (f": {body}" if body else "")


class KryptexPoolClient:
    """Read-only adapter for the documented public Kryptex Pool API.

    Kryptex account mining username/address is public pool identity, not an API secret.
    The API does not expose a universal $/day GPU rental profitability API, so this adapter
    reports pool/account telemetry separately and never mistakes balance for daily revenue.
    """

    BASE = "https://pool.kryptex.com"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self._local = threading.local()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._headers = {
            "Accept": "application/json",
            "User-Agent": "GPU-Profit-Hunter/0.3 (+https://pool.kryptex.com/api)",
        }

    def _session(self) -> requests.Session:
        # requests.Session is kept thread-local because the optional public API
        # endpoints are queried concurrently to keep the dashboard refresh fast.
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def _get(self, path: str, ttl: int = 60, optional: bool = False) -> Any:
        now = time.time()
        with self._cache_lock:
            cached = self._cache.get(path)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            response = self._session().get(f"{self.BASE}{path}", timeout=self.timeout)
        except requests.RequestException as exc:
            if optional:
                return None
            raise KryptexAPIError(f"Kryptex connection failed: {exc.__class__.__name__}") from exc
        if response.status_code == 404 and optional:
            with self._cache_lock:
                self._cache[path] = (time.time(), None)
            return None
        if not response.ok:
            if optional:
                return None
            raise KryptexAPIError(f"Kryptex API {_short_error(response)}")
        try:
            result = response.json()
        except ValueError as exc:
            if optional:
                return None
            raise KryptexAPIError("Kryptex API returned non-JSON data") from exc
        with self._cache_lock:
            self._cache[path] = (time.time(), result)
        return result

    @staticmethod
    def gpu_slug(gpu_name: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", str(gpu_name or "").lower()).strip("-")
        if value.startswith(("nvidia-geforce-", "nvidia-rtx-", "nvidia-cmp-", "amd-radeon-", "amd-rx-", "amd-instinct-")):
            return value
        if value.startswith(("rtx-pro-", "rtx-a", "tesla-")):
            return "nvidia-" + value
        if value.startswith(("geforce-", "rtx-", "gtx-")):
            return "nvidia-geforce-" + value
        if value.startswith("cmp-"):
            return "nvidia-" + value
        if value.startswith(("radeon-", "rx-", "instinct-")):
            return "amd-" + value
        if value.startswith("nvidia-"):
            rest = value[len("nvidia-"):]
            if rest.startswith(("rtx-", "gtx-")):
                return "nvidia-geforce-" + rest
            return value
        return value

    @staticmethod
    def _gpu_slug_candidates(gpu_name: str) -> list[str]:
        primary = KryptexPoolClient.gpu_slug(gpu_name)
        candidates = [primary]
        # Kryptex's public GPU URLs have used both GeForce and non-GeForce names
        # for certain workstation models; try aliases only as read-only fallbacks.
        if primary.startswith("nvidia-geforce-rtx-"):
            candidates.append(primary.replace("nvidia-geforce-rtx-", "nvidia-rtx-", 1))
        elif primary.startswith("nvidia-rtx-"):
            candidates.append(primary.replace("nvidia-rtx-", "nvidia-geforce-rtx-", 1))
        if primary.startswith("nvidia-geforce-gtx-"):
            candidates.append(primary.replace("nvidia-geforce-gtx-", "nvidia-gtx-", 1))
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _collect_gpu_coin_rows(data: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        coin_keys = ("coin", "coin_name", "symbol", "tag", "currency")
        algo_keys = ("algorithm", "algo", "algorithm_name")
        hash_keys = ("hashrate", "hash_rate", "speed")
        power_keys = ("power", "power_w", "power_watts", "consumption_w")
        daily_profit_keys = ("daily_profit_usd", "profit_day_usd", "profit_per_day_usd", "day_profit_usd", "profit24_usd", "profit_24h_usd", "daily_profit", "profit_per_day", "net_profit_day_usd", "profit_day", "dailyprofitusd", "profitdailyusd")
        daily_revenue_keys = ("daily_revenue_usd", "revenue_day_usd", "revenue_per_day_usd", "day_revenue_usd", "revenue24_usd", "revenue_24h_usd", "daily_revenue", "revenue_per_day", "revenue_day", "dailyrevenueusd", "revenuedailyusd")
        def get_number(obj: dict[str, Any], keys: tuple[str, ...]) -> float | None:
            lower = {str(k).lower(): v for k, v in obj.items()}
            for key in keys:
                if key not in lower:
                    continue
                try:
                    raw = str(lower[key]).replace(",", "")
                    try:
                        number = float(raw)
                    except ValueError:
                        match = re.search(r"-?\d+(?:\.\d+)?", raw)
                        if not match:
                            continue
                        number = float(match.group(0))
                    if number == number and abs(number) != float("inf"):
                        return number
                except (TypeError, ValueError):
                    continue
            return None
        def walk(node: Any) -> None:
            if isinstance(node, dict):
                lower = {str(k).lower(): v for k, v in node.items()}
                coin = next((str(lower[k]) for k in coin_keys if lower.get(k) not in (None, "") and not isinstance(lower.get(k), (dict, list))), None)
                algo = next((str(lower[k]) for k in algo_keys if lower.get(k) not in (None, "") and not isinstance(lower.get(k), (dict, list))), None)
                hashrate = get_number(node, hash_keys)
                power = get_number(node, power_keys)
                daily_profit = get_number(node, daily_profit_keys)
                daily_revenue = get_number(node, daily_revenue_keys)
                hashrate_display = None
                for hk in hash_keys:
                    raw_hash = lower.get(hk)
                    if raw_hash not in (None, ""):
                        hashrate_display = str(raw_hash)[:48]
                        break
                if coin and any(x is not None for x in (hashrate, power, daily_profit, daily_revenue)):
                    result.append({"coin": coin, "algorithm": algo, "hashrate": hashrate, "hashrate_display": hashrate_display, "power_w": power, "daily_profit_usd": daily_profit, "daily_revenue_usd": daily_revenue})
                for nested in node.values():
                    walk(nested)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(data)
        # Keep duplicate coin rows from nested data structures out of the dashboard.
        seen: set[tuple[str, str | None]] = set()
        unique = []
        for row in result:
            key = (row["coin"].upper(), row["algorithm"])
            if key not in seen:
                seen.add(key); unique.append(row)
        return unique[:40]

    def gpu_profitability(self, gpu_name: str) -> dict[str, Any]:
        """Fetch Kryptex's per-GPU benchmark endpoint when that model is indexed there."""
        candidates = self._gpu_slug_candidates(gpu_name)
        data = None
        used_slug = candidates[0]
        # Refresh GPU revenue/hashrate reference rows frequently enough to catch market swings.
        # Each GPU endpoint is cached independently so calls within one scan share the response.
        try:
            reference_ttl = max(60, int(os.getenv("KRYPTEX_GPU_REFERENCE_CACHE_SECONDS", os.getenv("KRYPTEX_GPU_SCAN_INTERVAL_SECONDS", "60"))))
        except (TypeError, ValueError):
            reference_ttl = 60
        for slug in candidates:
            data = self._get(f"/api/v1/device/gpu/{quote(slug, safe='')}", ttl=reference_ttl, optional=True)
            if data is not None:
                used_slug = slug
                break
        if data is None:
            return {"gpu_name": gpu_name, "slug": candidates[0], "available": False, "reference_updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        rows = self._collect_gpu_coin_rows(data)
        return {
            "gpu_name": gpu_name,
            "slug": used_slug,
            "available": True,
            "reference_updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "coin_rows": rows,
            "daily_profit_rows": sum(1 for x in rows if x.get("daily_profit_usd") is not None),
            "daily_revenue_rows": sum(1 for x in rows if x.get("daily_revenue_usd") is not None),
            "data_keys": sorted(str(k) for k in data.keys())[:40] if isinstance(data, dict) else [],
            "source": "Kryptex GPU API",
        }

    def account_status(self, coin: str, username: str, worker: str = "") -> dict[str, Any]:
        coin = quote((coin or "prl").strip().lower(), safe="")
        raw_username = username.strip()
        address = quote(raw_username, safe="")
        worker_name = (worker or "").strip()
        worker_encoded = quote(worker_name, safe="")
        if not raw_username:
            return {"configured": False, "connected": False, "error": "KRYPTEX_MINING_USERNAME is not configured"}
        suffix = f"/{worker_encoded}" if worker_encoded else ""
        try:
            # Pool/coin metadata tests the public API connection independently of whether a
            # worker has submitted a share yet. Miner-specific endpoints may return empty/404.
            pool_info = self._get(f"/{coin}/api/v1/pool/info", optional=False)
        except Exception as exc:
            return {"configured": True, "connected": False, "coin": coin, "username": "[configured]", "error": str(exc)[:220]}
        # Run optional telemetry lookups concurrently with independent thread-local
        # HTTP sessions. Previously 5 optional endpoints were queried sequentially,
        # turning five ordinary 2–4 second responses into a long dashboard spinner.
        endpoints = {
            "workers": (f"/{coin}/api/v3/miner/workers/{address}", 60),
            "chart": (f"/{coin}/api/v1/miner/chart/{address}{suffix}", 60),
            "balance": (f"/{coin}/api/v1/miner/balance/{address}", 60),
            "coin_info": (f"/api/v1/coin/{coin}/info", 300),
            "price_chart": (f"/api/v1/coin/{coin}/price/chart", 60),
        }
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="kryptex-api") as pool:
            futures = {
                name: pool.submit(self._get, path, ttl, True)
                for name, (path, ttl) in endpoints.items()
            }
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception:
                    # Public telemetry endpoints are optional; pool-info success remains
                    # distinct from whether this specific wallet has mined yet.
                    results[name] = None
        workers = results.get("workers")
        chart = results.get("chart")
        balance = results.get("balance")
        coin_info = results.get("coin_info")
        price_chart = results.get("price_chart")
        hr_keys = ("hashrate", "hash_rate", "current_hashrate", "reported_hashrate", "average_hashrate", "speed")
        bal_keys = ("total", "total_balance", "balance", "amount", "available", "unpaid", "immature")
        hashrate = _first_number(chart, hr_keys)
        if hashrate is None:
            hashrate = _first_number(workers, hr_keys)
        balance_value = _first_number(balance, bal_keys)
        if balance_value is None and isinstance(balance, dict):
            confirmed = _first_number(balance, ("confirmed",))
            unconfirmed = _first_number(balance, ("unconfirmed",))
            if confirmed is not None or unconfirmed is not None:
                balance_value = (confirmed or 0.0) + (unconfirmed or 0.0)
        price_keys = ("price_usd", "usd_price", "current_price", "price", "usd")
        coin_price = _first_number(coin_info, price_keys)
        if coin_price is None:
            coin_price = _first_number(price_chart, price_keys)
        worker_count = 0
        worker_rows: list[Any] = []
        if isinstance(workers, list):
            worker_rows = workers
        elif isinstance(workers, dict):
            for key in ("workers", "data", "items", "result"):
                if isinstance(workers.get(key), list):
                    worker_rows = workers[key]
                    break
            if not worker_rows and any(k in workers for k in ("worker", "name", "status")):
                worker_rows = [workers]
        worker_count = len(worker_rows)
        miner_data_available = worker_count > 0 or hashrate is not None or balance_value is not None
        # Return only a compact sample rather than an entire API document.
        worker_preview = []
        for row in worker_rows[:20]:
            if isinstance(row, dict):
                worker_preview.append({k: row.get(k) for k in ("name", "worker", "status", "hashrate", "hash_rate", "last_seen") if k in row})
        return {
            "configured": True,
            "connected": True,
            "account_data_available": miner_data_available,
            "source": "Kryptex Pool public API",
            "coin": coin.upper(),
            "username": raw_username[:4] + "…" + raw_username[-3:] if len(raw_username) > 8 else "[configured]",
            "worker_filter": worker_name or None,
            "worker_count": worker_count,
            "worker_preview": worker_preview,
            "hashrate_raw": hashrate,
            "balance_raw": balance_value,
            "coin_price_sample": coin_price,
            "coin_price_available": coin_price is not None,
            "chart_available": chart is not None,
            "balance_available": balance_value is not None,
            "payout_stats_available": False,
            "pool_info_available": pool_info is not None,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": ("Kryptex Pool API is reachable, but no usable worker, hashrate, or numeric balance data was returned for this username and coin. Confirm the exact public Mining Username and coin slug; a missing balance is not assumed to be zero." if not miner_data_available else None),
            "note": "Pool telemetry only; reported balance is not daily revenue. If no numeric balance or active worker/hashrate data is returned, verify the public Mining Username and coin slug. GPU profitability is calculated separately from mining benchmarks and rental prices.",
        }
