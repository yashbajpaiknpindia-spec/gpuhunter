from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def load_json_file(path: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(Path(path).expanduser(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except (OSError, ValueError):
        return fallback


def load_profiles(path: str) -> list[dict[str, Any]]:
    data = load_json_file(path, {"profiles": []})
    profiles = data.get("profiles", [])
    return [p for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []


def load_wallets(path: str) -> dict[str, Any]:
    data = load_json_file(path, {"wallets": {}, "workers": {}})
    return data if isinstance(data, dict) else {"wallets": {}, "workers": {}}


def normalized_gpu(value: str) -> str:
    s = value.lower().replace("nvidia", "").replace("geforce", "")
    return re.sub(r"[^a-z0-9]+", "", s)


def match_gpu_name(offer_name: str, benchmark_name: str) -> bool:
    a, b = normalized_gpu(offer_name), normalized_gpu(benchmark_name)
    if not a or not b:
        return False
    # Match model tokens first so an RTX 3060 never accidentally matches RTX 3060 Ti.
    tokens_a = re.findall(r"(?:rtx|gtx|a\d{4}|l\d{4}|h\d{4})\d{3,4}(?:ti|super)?", a)
    tokens_b = re.findall(r"(?:rtx|gtx|a\d{4}|l\d{4}|h\d{4})\d{3,4}(?:ti|super)?", b)
    if tokens_a and tokens_b:
        return tokens_a[0] == tokens_b[0]
    return a == b


def match_profile(coin_tag: str, algorithm: str, gpu_name: str, profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    tag = (coin_tag or "").upper()
    algo = (algorithm or "").lower().replace(" ", "")
    for profile in profiles:
        if not profile.get("enabled", False):
            continue
        tags = [str(x).upper() for x in profile.get("coin_tags", [])]
        algos = [str(x).lower().replace(" ", "") for x in profile.get("algorithms", [])]
        gpus = profile.get("gpu_models", ["*"])
        if tags and tag not in tags and "*" not in tags:
            continue
        if algos and algo not in algos and "*" not in algos:
            continue
        if gpus and "*" not in gpus and not any(match_gpu_name(gpu_name, str(g)) for g in gpus):
            continue
        return profile
    return None


def wallet_file_status(path: str) -> dict[str, Any]:
    """Describe data/wallets.json without hiding problems (missing file, bad JSON)."""
    target = Path(path).expanduser()
    if not target.exists():
        return {"exists": False, "error": f"Wallet file not found at {target.name}", "count": 0}
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return {"exists": True, "error": f"Wallet file is not valid JSON ({exc.__class__.__name__}: {str(exc)[:80]})", "count": 0}
    wallets = data.get("wallets") if isinstance(data, dict) else None
    if not isinstance(wallets, dict):
        return {"exists": True, "error": 'Wallet file must look like {"wallets": {"PRL": "address"}}', "count": 0}
    return {"exists": True, "error": None, "count": sum(bool(str(v or "").strip()) for v in wallets.values())}


def wallet_for_profile(profile: dict[str, Any], wallets_path: str) -> str:
    wallets = load_wallets(wallets_path)
    key = str(profile.get("wallet_key", "")).strip().upper()
    mapping = wallets.get("wallets", {}) or {}
    val = ""
    if key and isinstance(mapping, dict):
        # Case-insensitive: "prl", "PRL" and "Prl" in wallets.json all match wallet_key "PRL".
        for name, value in mapping.items():
            if str(name).strip().upper() == key and str(value or "").strip():
                val = value
                break
    if not val:
        val = os.getenv(f"WALLET_{key}", "") if key else ""
    return str(val or "").strip()
