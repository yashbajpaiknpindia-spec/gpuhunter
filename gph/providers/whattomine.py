from __future__ import annotations

import os
import time
from typing import Any
import requests


class WhatToMineError(RuntimeError):
    pass


class WhatToMineClient:
    BASE = "https://whattomine.com/api/v1"

    def __init__(self, api_token: str, timeout: int = 25):
        if not api_token:
            raise WhatToMineError("WHATTOMINE_API_TOKEN is not configured")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GPU-Profit-Hunter/0.1"
        })
        self._cache: dict[str, tuple[float, Any]] = {}

    def _get(self, endpoint: str, ttl: int = 900) -> Any:
        now = time.time()
        cached = self._cache.get(endpoint)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            r = self.session.get(f"{self.BASE}{endpoint}", timeout=self.timeout)
        except requests.RequestException as exc:
            raise WhatToMineError(f"WhatToMine connection failed: {exc.__class__.__name__}") from exc
        if not r.ok:
            raise WhatToMineError(f"WhatToMine returned HTTP {r.status_code}; check API token and quota")
        try:
            data = r.json()
        except ValueError as exc:
            raise WhatToMineError("WhatToMine returned non-JSON response") from exc
        self._cache[endpoint] = (now, data)
        return data

    def get_gpus(self) -> list[dict[str, Any]]:
        data = self._get("/gpus", ttl=3600)
        if isinstance(data, dict):
            data = data.get("gpus", data.get("data", []))
        if not isinstance(data, list):
            raise WhatToMineError("Unexpected /gpus response shape")
        return [x for x in data if isinstance(x, dict)]

    def get_coins(self) -> list[dict[str, Any]]:
        # Keep the all-coins catalog quota-aware; the fast scan loop reuses this snapshot.
        coins_ttl = max(300, int(os.getenv("WHATTOMINE_COINS_CACHE_SECONDS", "300")))
        data = self._get("/coins", ttl=coins_ttl)
        if isinstance(data, dict):
            data = data.get("coins", data.get("data", []))
        if not isinstance(data, list):
            raise WhatToMineError("Unexpected /coins response shape")
        return [x for x in data if isinstance(x, dict)]

    def calculate_gpu(self, benchmark_gpu: dict[str, Any]) -> list[dict[str, Any]]:
        """Calculate revenue across the algorithms the provider lists for this GPU.

        Provider data is treated as a benchmark, not a guarantee. The returned results are further
        haircut and checked against actual instance rent before any live action.
        """
        name = str(benchmark_gpu.get("name", "unknown"))
        cache_key = f"calc:{name}"
        now = time.time()
        cached = self._cache.get(cache_key)
        calc_ttl = max(60, int(os.getenv("WHATTOMINE_CALC_CACHE_SECONDS", "60")))
        if cached and now - cached[0] < calc_ttl:
            return cached[1]
        algorithms = benchmark_gpu.get("algorithms") or []
        settings = []
        for item in algorithms:
            if not isinstance(item, dict):
                continue
            algo = item.get("name") or item.get("algorithm")
            try:
                hashrate = float(item.get("hashrate", 0) or 0)
                power = float(item.get("power", 0) or 0)
            except (ValueError, TypeError):
                continue
            if algo and hashrate > 0:
                settings.append({"algorithm": str(algo), "power": max(0, power), "hashrate": hashrate, "fee": 0})
        if not settings:
            return []
        try:
            r = self.session.post(
                f"{self.BASE}/calculate",
                json={"cost": 0, "fee": 0, "settings": settings},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WhatToMineError(f"WhatToMine calculation failed: {exc.__class__.__name__}") from exc
        if not r.ok:
            raise WhatToMineError(f"WhatToMine calculation returned HTTP {r.status_code}; check API quota")
        try:
            rows = r.json()
        except ValueError as exc:
            raise WhatToMineError("WhatToMine calculation returned non-JSON response") from exc
        if isinstance(rows, dict):
            rows = rows.get("coins", rows.get("data", rows.get("results", [])))
        if not isinstance(rows, list):
            raise WhatToMineError("Unexpected /calculate response shape")
        self._cache[cache_key] = (now, rows)
        return [x for x in rows if isinstance(x, dict)]
