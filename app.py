from __future__ import annotations

import copy
import hmac
import mimetypes
import os
import re
import secrets
import time
from threading import Lock, Thread
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, Response, redirect, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

from gph.config import Config, ROOT
from gph.db import Database, now_iso
from gph.engine import OpportunityScanner
from gph.profiles import load_profiles
from gph.providers.vast import VastAPIError, VastClient
from gph.supervisor import Supervisor
from gph.profit import parse_num
from gph.finance import funding_plan, observed_net_24h
from gph.fx import get_usd_inr_rate


# Fail closed for public binds (including Render/Gunicorn imports, where __main__ does not run).
# Scanner and resource-control endpoints are sensitive because Vast keys can spend money.
_public_bind = os.getenv("APP_HOST", "127.0.0.1").strip() not in {"127.0.0.1", "localhost", "::1"}
_hosted = bool(os.getenv("RENDER_SERVICE_ID") or os.getenv("RENDER"))
_app_password = os.getenv("APP_PASSWORD", "").strip()
_app_secret = os.getenv("APP_SECRET_KEY", "local-dev-only-change-me").strip()
_placeholder_markers = ("replace_with", "paste_your", "change_me", "your_password", "your_secret")
if (_public_bind or _hosted) and (not _app_password or any(marker in _app_password.lower() for marker in _placeholder_markers)):
    raise RuntimeError("Set a real, strong APP_PASSWORD before exposing GPU Profit Hunter beyond localhost; replace any template placeholder.")
if (_public_bind or _hosted) and (_app_secret == "local-dev-only-change-me" or len(_app_secret) < 32 or any(marker in _app_secret.lower() for marker in _placeholder_markers)):
    raise RuntimeError("Set a strong random APP_SECRET_KEY (at least 32 characters) before deploying GPU Profit Hunter publicly; replace any template placeholder.")

config = Config()
if _hosted and not config.database_url:
    raise RuntimeError("DATABASE_URL is required on Render. Set it to your existing Render PostgreSQL connection string before redeploying.")
if _hosted and not config.database_url.startswith(("postgres://", "postgresql://")):
    raise RuntimeError("DATABASE_URL on Render must be your existing Render PostgreSQL connection string (postgres:// or postgresql://). SQLite is local-development-only.")
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=None)
# Render terminates HTTPS before forwarding to Gunicorn. Trust one proxy hop for
# client IP / scheme so the login throttle is per client and Secure cookies work.
# Host is deliberately not taken from X-Forwarded-Host.
if _hosted:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
# Serve bundled assets from the absolute app directory and send in-memory bytes.
# The explicit route avoids a deployment working-directory mismatch and makes a
# zero-byte/missing JS or CSS file a visible error instead of a silent broken UI.
_STATIC_ASSETS = {"app.js", "app.css", "login.css"}


@app.get("/static/<path:filename>", endpoint="static")
def static_asset(filename: str):
    if filename not in _STATIC_ASSETS:
        return "Not found", 404
    asset_path = ROOT / "static" / filename
    try:
        payload = asset_path.read_bytes()
    except OSError:
        app.logger.exception("Static asset is missing: %s", filename)
        return "Static asset is missing. Redeploy the complete application package.", 404
    if not payload:
        app.logger.error("Static asset is empty: %s", filename)
        return "Static asset is empty. Redeploy the complete application package.", 503
    response = Response(payload, mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream")
    response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Length"] = str(len(payload))
    return response


app.config["SECRET_KEY"] = config.secret_key
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(_hosted or os.getenv("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"})
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

db = Database(config.database_url or config.database_path)
scanner = OpportunityScanner(config, db)
supervisor = Supervisor(config, db)

# Small in-process login throttle; Render is configured for one Gunicorn worker.
_LOGIN_LIMIT = 10
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_FAILURES_LOCK = Lock()
_STATE_CACHE_LOCK = Lock()
_STATE_CACHE: dict[str, Any] | None = None


def _login_is_limited(remote_addr: str) -> bool:
    now = time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(remote_addr, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _LOGIN_FAILURES[remote_addr] = attempts
        return len(attempts) >= _LOGIN_LIMIT


def _record_login_failure(remote_addr: str) -> None:
    now = time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        # Prune inactive sources and cap memory use if a public endpoint is probed
        # from many addresses. Render's forwarded client IP is enabled above.
        for address, values in list(_LOGIN_FAILURES.items()):
            fresh = [t for t in values if now - t < _LOGIN_WINDOW_SECONDS]
            if fresh:
                _LOGIN_FAILURES[address] = fresh
            else:
                _LOGIN_FAILURES.pop(address, None)
        if len(_LOGIN_FAILURES) >= 1024 and remote_addr not in _LOGIN_FAILURES:
            oldest = min(_LOGIN_FAILURES, key=lambda key: _LOGIN_FAILURES[key][-1])
            _LOGIN_FAILURES.pop(oldest, None)
        attempts = [t for t in _LOGIN_FAILURES.get(remote_addr, []) if now - t < _LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        _LOGIN_FAILURES[remote_addr] = attempts


def _clear_login_failures(remote_addr: str) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(remote_addr, None)


def _safe_next_path(value: str | None) -> str:
    """Only allow relative app paths after login; never redirect to an external host."""
    candidate = (value or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//") and "\\" not in candidate and "\r" not in candidate and "\n" not in candidate:
        return candidate
    return url_for("index")


def _is_same_origin(origin: str) -> bool:
    """Compare origin host and effective port without relying on proxy scheme guesses."""
    try:
        origin_parts = urlsplit(origin)
        request_parts = urlsplit(request.host_url)
        if origin_parts.scheme not in {"http", "https"} or not origin_parts.hostname or not request_parts.hostname:
            return False
        origin_port = origin_parts.port or (443 if origin_parts.scheme == "https" else 80)
        request_port = request_parts.port or (443 if request_parts.scheme == "https" else 80)
        return origin_parts.hostname.lower() == request_parts.hostname.lower() and origin_port == request_port
    except (ValueError, TypeError):
        return False


@app.before_request
def local_access_guard() -> Response | None:
    # Keep health checks public, but use an in-app form and signed session for all
    # dashboard/API access instead of the browser's opaque Basic Auth popup.
    if request.method in {"POST", "PUT", "DELETE"} and (request.path.startswith("/api/") or request.path == "/logout"):
        origin = request.headers.get("Origin")
        if origin and not _is_same_origin(origin):
            return jsonify({"error": "Cross-origin request rejected"}), 403

    if request.path == "/api/health" or request.path.startswith("/static/") or request.endpoint in {"login", "logout"}:
        return None

    password = os.getenv("APP_PASSWORD", "")
    # No password is accepted only in local development; public/Render startup fails closed above.
    if not password:
        return None
    configured_user = os.getenv("APP_USER", "gph").strip() or "gph"
    if session.get("gph_authenticated") is True and session.get("gph_username") == configured_user:
        return None
    session.clear()

    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Your login session is missing or expired. Sign in again to continue.", "login_url": url_for("login", expired="1")}), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    expected_user = os.getenv("APP_USER", "gph").strip() or "gph"
    expected_password = os.getenv("APP_PASSWORD", "")
    if not expected_password:
        # Avoid a confusing login prompt when using an intentionally local-only setup.
        if _hosted or _public_bind:
            return "Login is unavailable because APP_PASSWORD is not configured. Configure it in the service environment.", 503
        return redirect(url_for("index"))
    if session.get("gph_authenticated") is True and request.method == "GET":
        return redirect(url_for("index"))

    next_url = _safe_next_path(request.values.get("next"))
    error = None
    entered_username = ""
    if request.method == "POST":
        entered_username = request.form.get("username", "").strip()
        supplied_password = request.form.get("password", "")
        remote_addr = request.remote_addr or "unknown"
        if _login_is_limited(remote_addr):
            error = "Too many failed sign-in attempts from this address. Wait 15 minutes before trying again."
            return render_template("login.html", error=error, entered_username=entered_username, next_url=next_url, expired=False, logged_out=False, login_username=expected_user), 429
        username_ok = hmac.compare_digest(entered_username.encode("utf-8"), expected_user.encode("utf-8"))
        password_ok = hmac.compare_digest(supplied_password.encode("utf-8"), expected_password.encode("utf-8"))
        if username_ok and password_ok:
            _clear_login_failures(remote_addr)
            session.clear()
            session["gph_authenticated"] = True
            session["gph_username"] = expected_user
            session.permanent = True
            try:
                db.log("INFO", "login_succeeded", {"auth": "session"})
            except Exception:
                app.logger.warning("Login succeeded but its audit event could not be saved")
            return redirect(next_url)
        _record_login_failure(remote_addr)
        try:
            db.log("WARNING", "login_failed", {"remote_addr": remote_addr})
        except Exception:
            app.logger.warning("Failed-login audit event could not be saved")
        error = "The username or password didn't match. Both are case-sensitive. This deployment expects the username shown below. Check APP_USER and APP_PASSWORD in Render → your web service → Environment."
        return render_template("login.html", error=error, entered_username=entered_username, next_url=next_url, expired=False, logged_out=False, login_username=expected_user), 401

    return render_template("login.html", error=error, entered_username=entered_username, next_url=next_url, expired=request.args.get("expired") == "1", logged_out=request.args.get("logged_out") == "1", login_username=expected_user)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login", logged_out="1"))


def _managed_lookup(managed_rows: list[dict[str, Any]] | None = None) -> dict[int, dict[str, Any]]:
    rows = managed_rows if managed_rows is not None else db.get_managed_instances()
    return {int(x["instance_id"]): x for x in rows}


def _instances_for_ui(snapshot: dict[str, Any] | None = None, managed_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    # The dashboard snapshot is loaded in one database transaction by /api/state.
    # This helper must not open additional connections when that snapshot is supplied.
    snapshot = snapshot if isinstance(snapshot, dict) else (db.get_setting("vast_instances_snapshot", {}) or {})
    raw = snapshot.get("instances", []) if isinstance(snapshot, dict) else []
    if not isinstance(raw, list):
        raw = []
    managed_rows = managed_rows if managed_rows is not None else db.get_managed_instances()
    managed = _managed_lookup(managed_rows)
    result = []
    for x in raw:
        try:
            iid = int(x.get("id") or x.get("instance_id") or x.get("contract_id"))
        except (ValueError, TypeError):
            continue
        m = managed.get(iid)
        metadata = (m or {}).get("metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        pool_metrics = metadata.get("pool_metrics", {}) or {}
        status = str(x.get("actual_status") or x.get("status") or "unknown")
        # A provider-visible instance can still be billable after DELETE was accepted.
        # Surface the local termination-pending state until monitor_once confirms it is gone.
        if str((m or {}).get("status") or "").lower() == "destroy_requested":
            status = "destroy_requested"
        result.append({
            "instance_id": iid,
            "gpu_name": str(x.get("gpu_name") or x.get("gpu_name_display") or (m or {}).get("gpu_name") or "Unknown GPU"),
            "status": status,
            "hourly_cost_usd": round(parse_num(x.get("dph_total"), parse_num(x.get("dph_base"), parse_num((m or {}).get("hourly_cost"), 0))), 5),
            "managed_by_gph": bool(m),
            "coin_tag": (m or {}).get("coin_tag"),
            "expected_net_day_usd": (m or {}).get("latest_net_day"),
            "conservative_gross_day_usd": (m or {}).get("latest_gross_day"),
            "observed_hashrate_hs": (m or {}).get("observed_hashrate_hs", 0),
            "gpu_util": (m or {}).get("observed_gpu_util", 0),
            "gpu_temp": (m or {}).get("observed_gpu_temp", 0),
            "accepted_share_lines": (m or {}).get("accepted_share_lines", 0),
            "pool_revenue_24h_usd": pool_metrics.get("revenue_24h_usd"),
            "pool_realized_revenue_24h_usd": pool_metrics.get("realized_revenue_24h_usd"),
            "pool_realized_revenue_updated_at": pool_metrics.get("realized_revenue_updated_at"),
            "pool_balance_usd": pool_metrics.get("pool_balance_usd"),
            "pool_hashrate_hs": pool_metrics.get("pool_hashrate_hs"),
            "pool_metrics_source": pool_metrics.get("source"),
            "pool_metrics_updated_at": pool_metrics.get("updated_at"),
            "pool_metrics_error": metadata.get("pool_metrics_last_error", ""),
            "started_at": (m or {}).get("started_at"),
            "destroy_reason": ((m or {}).get("metadata", {}) or {}).get("destroy_reason") if isinstance((m or {}).get("metadata", {}), dict) else None,
            "destroyed_at": ((m or {}).get("metadata", {}) or {}).get("destroyed_at") if isinstance((m or {}).get("metadata", {}), dict) else None,
            "last_monitor_at": (m or {}).get("last_monitor_at"),
        })
    # Vast stops listing destroyed instances, but keep locally managed rows visible so the
    # dashboard preserves deployment and destruction history with the guardrail reason.
    seen_ids = {int(row["instance_id"]) for row in result}
    for m in managed_rows:
        try:
            iid = int(m.get("instance_id"))
        except (TypeError, ValueError):
            continue
        if iid in seen_ids:
            continue
        metadata = m.get("metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        result.append({
            "instance_id": iid, "gpu_name": m.get("gpu_name") or "Unknown GPU",
            "status": m.get("status") or "missing_from_vast", "hourly_cost_usd": float(m.get("hourly_cost") or 0),
            "managed_by_gph": True, "coin_tag": m.get("coin_tag"),
            "expected_net_day_usd": m.get("latest_net_day"), "conservative_gross_day_usd": m.get("latest_gross_day"),
            "observed_hashrate_hs": m.get("observed_hashrate_hs", 0), "gpu_util": m.get("observed_gpu_util", 0),
            "gpu_temp": m.get("observed_gpu_temp", 0), "accepted_share_lines": m.get("accepted_share_lines", 0),
            "pool_revenue_24h_usd": (metadata.get("pool_metrics") or {}).get("revenue_24h_usd"),
            "pool_realized_revenue_24h_usd": (metadata.get("pool_metrics") or {}).get("realized_revenue_24h_usd"),
            "pool_realized_revenue_updated_at": (metadata.get("pool_metrics") or {}).get("realized_revenue_updated_at"),
            "pool_balance_usd": (metadata.get("pool_metrics") or {}).get("pool_balance_usd"),
            "pool_hashrate_hs": (metadata.get("pool_metrics") or {}).get("pool_hashrate_hs"),
            "pool_metrics_source": (metadata.get("pool_metrics") or {}).get("source"),
            "pool_metrics_updated_at": (metadata.get("pool_metrics") or {}).get("updated_at"),
            "pool_metrics_error": metadata.get("pool_metrics_last_error", ""),
            "started_at": m.get("started_at"), "destroy_reason": metadata.get("destroy_reason"),
            "destroyed_at": metadata.get("destroyed_at"), "last_monitor_at": m.get("last_monitor_at"),
        })
    return result


def _summary(opportunities: list[dict[str, Any]], instances: list[dict[str, Any]]) -> dict[str, Any]:
    live = [x for x in instances if str(x.get("status", "")).lower() in {"running", "active", "loading", "created", "starting", "destroy_requested", "missing_from_vast"}]
    running_hourly = sum(parse_num(x.get("hourly_cost_usd"), 0) for x in live)
    managed = [x for x in instances if x.get("managed_by_gph")]
    active_statuses = {"running", "active", "loading", "created", "creating", "starting", "destroy_requested", "missing_from_vast"}
    active_managed = [x for x in managed if str(x.get("status", "")).lower() in active_statuses]
    estimated_net = sum(parse_num(x.get("expected_net_day_usd"), 0) for x in active_managed if x.get("expected_net_day_usd") is not None)
    best = max((x for x in opportunities if x.get("economics_qualified")), key=lambda x: parse_num(x.get("expected_net_day_usd"), -1e9), default=None)
    return {
        "live_instances": len(live),
        "managed_instances": len(managed),
        "current_hourly_cost_usd": round(running_hourly, 5),
        "projected_current_daily_rent_usd": round(running_hourly * 24, 4),
        "managed_estimated_net_day_usd": round(estimated_net, 4),
        "opportunities_count": len(opportunities),
        "economically_positive_count": sum(bool(x.get("economics_qualified")) for x in opportunities),
        "fully_auto_qualified_count": sum(bool(x.get("qualified")) for x in opportunities),
        "best_candidate": best,
        "actual_payout_revenue": None,
        "actual_payout_note": "Not connected: pool-reported earnings are provider-specific. Current revenue values are estimates until a supported pool payout connector is configured.",
    }


@app.get("/")
def index():
    return render_template("index.html", current_username=session.get("gph_username"))


@app.get("/favicon.ico")
def favicon():
    # Browsers request this path automatically even when the dashboard has no custom icon.
    # Return an empty successful response instead of polluting service logs with a 404.
    return "", 204


@app.get("/api/health")
def health():
    asset_sizes = {}
    for name in sorted(_STATIC_ASSETS):
        try:
            asset_sizes[name] = (ROOT / "static" / name).stat().st_size
        except OSError:
            asset_sizes[name] = 0
    assets_ok = all(size > 0 for size in asset_sizes.values())
    with _STATE_CACHE_LOCK:
        snapshot_at = float(_BUNDLE_CACHE.get("at") or 0.0)
        snapshot_available = _BUNDLE_CACHE.get("bundle") is not None
    snapshot_age = max(0.0, time.monotonic() - snapshot_at) if snapshot_available else None
    return jsonify({
        "ok": True, "app": "GPU Profit Hunter", "version": "0.7.15",
        "time": now_iso(), "asset_sizes_bytes": asset_sizes, "assets_ok": assets_ok,
        "database": {"backend": db.backend, "connection_pool_enabled": db.pool_enabled,
                     "connection_pool_stats": db.pool_stats()},
        "dashboard_snapshot": {
            "available": snapshot_available,
            "age_seconds": round(snapshot_age, 1) if snapshot_age is not None else None,
            "refresh_in_progress": _BUNDLE_READ_LOCK.locked(),
            "freshness_limit_seconds": int(_BUNDLE_TTL_SECONDS),
        },
        "max_ui_target_instances": 25,
    })



def _safe_error_text(exc: BaseException) -> str:
    """Short exception text for the UI with any connection URL removed."""
    return re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", "[url]", str(exc))[:160]



def _safe_setup_gaps() -> list[dict[str, Any]]:
    """Missing keys/files list; file/env checks only, so it works while the database is down."""
    try:
        return config.setup_gaps(load_profiles(config.miner_profiles_path))
    except Exception:
        app.logger.exception("Could not compute setup gaps")
        return []


@app.get("/api/setup/status")
def setup_status():
    """Database-independent setup report: which API keys/files are missing and what that disables."""
    profiles = load_profiles(config.miner_profiles_path)
    return jsonify({
        "ok": True,
        "config": config.public_dict(),
        "login": {"username": (os.getenv("APP_USER", "gph").strip() or "gph"), "password_configured": bool(os.getenv("APP_PASSWORD", "")), "session_authenticated": session.get("gph_authenticated") is True},
        "fx": get_usd_inr_rate(allow_network=False),
        "live_blockers": config.live_blockers(profiles),
        "profiles_total": len(profiles),
        "profiles_enabled": sum(bool(p.get("enabled")) for p in profiles),
        "setup_gaps": _safe_setup_gaps(),
    })


@app.get("/api/progress")
def progress():
    # This endpoint is polled while scans/actions are in progress. Keep it fully
    # in-memory so a slow PostgreSQL connection cannot freeze the progress UI.
    return jsonify({"ok": True, "progress": supervisor.current_ui_progress()})


@app.post("/api/vast/balance/refresh")
def refresh_vast_balance():
    """Start a read-only Vast balance refresh without blocking on the provider."""
    started_at = time.monotonic()
    status = supervisor.request_vast_balance_refresh()
    app.logger.info("Vast balance refresh accepted (refreshing=%s, elapsed_ms=%d)", bool(status.get("refreshing")), int((time.monotonic() - started_at) * 1000))
    return jsonify({"ok": True, "vast_balance": status}), 202


@app.get("/api/vast/balance/status")
def vast_balance_refresh_status():
    """Return cached balance plus refresh state; this route does no provider I/O."""
    return jsonify({"ok": True, "vast_balance": supervisor.vast_balance_refresh_state()})


_BUNDLE_CACHE: dict[str, Any] = {"bundle": None, "at": 0.0}
_BUNDLE_READ_LOCK = Lock()
_BUNDLE_TTL_SECONDS = 10.0


def _annotate_dashboard_bundle(bundle: dict[str, Any], cached_at: float, refreshing: bool) -> dict[str, Any]:
    """Return a shallow copy with cache freshness metadata for the UI."""
    age = max(0.0, time.monotonic() - float(cached_at or 0.0))
    result = dict(bundle)
    result["_gph_dashboard_meta"] = {
        "age_seconds": round(age, 1),
        "stale": age >= _BUNDLE_TTL_SECONDS,
        "refreshing": bool(refreshing),
    }
    return result


def _refresh_dashboard_bundle_worker() -> None:
    """Refresh the shared snapshot away from the request path.

    A DB/socket read can occasionally outlive its PostgreSQL statement timeout (for
    example, during a broken cross-region TLS connection). Never let that hold a Gunicorn
    request thread until the browser's 25-second timeout. Only one reader is permitted;
    while it runs, requests get the last snapshot or a small safe cold-start response.
    """
    started = time.monotonic()
    try:
        bundle = db.get_dashboard_bundle(opportunity_limit=150, event_limit=50)
        with _STATE_CACHE_LOCK:
            _BUNDLE_CACHE.update(bundle=bundle, at=time.monotonic())
        app.logger.info(
            "Dashboard snapshot refresh completed in %.2fs (opportunities=%d, events=%d)",
            time.monotonic() - started,
            len(bundle.get("opportunities", []) or []),
            len(bundle.get("events", []) or []),
        )
    except Exception as exc:
        app.logger.warning(
            "Dashboard snapshot refresh failed after %.2fs (%s); pool_stats=%s",
            time.monotonic() - started,
            exc.__class__.__name__,
            db.pool_stats(),
        )
    finally:
        _BUNDLE_READ_LOCK.release()


def _dashboard_bundle() -> dict[str, Any]:
    """Return the shared dashboard snapshot without blocking on database I/O.

    Stale/missing snapshots trigger one background read. Requests immediately receive
    the last successful data with freshness metadata; during a cold start, they receive
    the existing safe degraded fallback rather than tying up all web threads.
    """
    with _STATE_CACHE_LOCK:
        cached, cached_at = _BUNDLE_CACHE["bundle"], float(_BUNDLE_CACHE["at"] or 0.0)
    age = time.monotonic() - cached_at
    if cached is not None and age < _BUNDLE_TTL_SECONDS:
        return _annotate_dashboard_bundle(cached, cached_at, refreshing=False)

    # Acquire once and transfer lock ownership to the worker. If a reader is already
    # running, no request waits for it or starts another database read.
    if _BUNDLE_READ_LOCK.acquire(blocking=False):
        try:
            Thread(
                target=_refresh_dashboard_bundle_worker,
                name="gph-dashboard-snapshot-refresh",
                daemon=True,
            ).start()
        except Exception:
            _BUNDLE_READ_LOCK.release()
            app.logger.exception("Could not start dashboard snapshot refresh worker")

    with _STATE_CACHE_LOCK:
        cached, cached_at = _BUNDLE_CACHE["bundle"], float(_BUNDLE_CACHE["at"] or 0.0)
    if cached is not None:
        return _annotate_dashboard_bundle(cached, cached_at, refreshing=_BUNDLE_READ_LOCK.locked())
    raise RuntimeError("Dashboard snapshot refresh is still running; no previous snapshot is available yet")


@app.get("/api/state")
def state():
    """Return a compact dashboard state with stale-snapshot fallback on DB outages."""
    global _STATE_CACHE
    try:
        bundle = _dashboard_bundle()
        snapshot_meta = bundle.pop("_gph_dashboard_meta", {"age_seconds": None, "stale": False, "refreshing": False})
        settings = bundle.get("settings", {})
        opportunities = bundle.get("opportunities", [])
        instances = _instances_for_ui(
            snapshot=settings.get("vast_instances_snapshot", {}) or {},
            managed_rows=bundle.get("managed_instances", []),
        )
        profiles = load_profiles(config.miner_profiles_path)
        blockers = config.live_blockers(profiles)
        vast_balance = supervisor.vast_balance_refresh_state()
        summary = _summary(opportunities, instances)
        target = supervisor.target_instances(settings=settings)
        funding = funding_plan(opportunities, instances, target, vast_balance, config.bandwidth_reserve_usd_day)
        observed = observed_net_24h(instances, config.bandwidth_reserve_usd_day)
        summary["observed_net_24h_usd"] = observed.get("net_24h_usd")
        summary["observed_net_24h_note"] = observed.get("reason")
        payload = {
            "ok": True,
            "config": config.public_dict(),
            "login": {"username": (os.getenv("APP_USER", "gph").strip() or "gph"), "password_configured": bool(os.getenv("APP_PASSWORD", "")), "session_authenticated": session.get("gph_authenticated") is True},
            "fx": get_usd_inr_rate(allow_network=False),
            "live_blockers": blockers,
            "profiles_total": len(profiles),
            "profiles_enabled": sum(bool(p.get("enabled")) for p in profiles),
            "last_scan_at": settings.get("last_scan_at"),
            "last_scan_summary": settings.get("last_scan_summary", {}),
            "gpu_affordability_catalog": settings.get("gpu_affordability_catalog", {}) or {},
            "offer_diagnostics": settings.get("last_scan_offer_diagnostics", []) or [],
            "ui_progress": supervisor.current_ui_progress(),
            "automation": supervisor.automation_state(settings=settings),
            "vast_instance_snapshot": settings.get("vast_instances_snapshot", {}) or {},
            "kryptex": supervisor.cached_kryptex_status(),
            "kryptex_gpu_market": supervisor.cached_kryptex_gpu_market() or settings.get("kryptex_gpu_market", {}),
            "opportunities": opportunities,
            "instances": instances,
            "summary": summary,
            "vast_balance": vast_balance,
            "funding": funding,
            "events": bundle.get("events", []),
            "database_status": {
                "available": not bool(snapshot_meta.get("stale")),
                "backend": db.backend,
                "snapshot": "stale" if snapshot_meta.get("stale") else "fresh",
                "snapshot_age_seconds": snapshot_meta.get("age_seconds"),
                "refreshing": bool(snapshot_meta.get("refreshing")),
                "error": (
                    "Dashboard database refresh is running in the background; showing the last saved snapshot."
                    if snapshot_meta.get("stale") and snapshot_meta.get("refreshing")
                    else "Dashboard snapshot is stale; the last background refresh did not complete. The UI will retry automatically."
                    if snapshot_meta.get("stale")
                    else None
                ),
            },
            "setup_gaps": config.setup_gaps(profiles),
            "degraded": bool(snapshot_meta.get("stale")),
        }
        with _STATE_CACHE_LOCK:
            _STATE_CACHE = copy.deepcopy(payload)
        return jsonify(payload)
    except Exception as exc:
        # Do not try to write the error back to the failing database. Render logs are
        # the source of truth for this incident; a last-known UI snapshot is explicitly
        # tagged stale and live actions still re-read/revalidate their required state.
        if isinstance(exc, RuntimeError) and "Dashboard snapshot refresh is still running" in str(exc):
            # Expected single-flight contention during a cold cache refresh: the safe
            # degraded response below is deliberate, so do not emit an ERROR traceback
            # for every concurrent browser poll.
            app.logger.warning("Dashboard refresh busy; returning degraded fallback")
        else:
            app.logger.exception("Dashboard snapshot read failed (database/API state unavailable)")
        with _STATE_CACHE_LOCK:
            stale = copy.deepcopy(_STATE_CACHE) if _STATE_CACHE is not None else None
        if stale is not None:
            stale["degraded"] = True
            stale["database_status"] = {
                "available": False, "backend": db.backend, "snapshot": "stale",
                "error": "Dashboard database read failed; showing the last successful snapshot. No live rental can use this snapshot as revalidation.",
                "error_type": exc.__class__.__name__,
            }
            # These two statuses are process-local so a database failure does not hide
            # a completed provider balance/connection check.
            stale["vast_balance"] = supervisor.vast_balance_refresh_state()
            stale["kryptex"] = supervisor.cached_kryptex_status()
            stale["setup_gaps"] = _safe_setup_gaps()
            return jsonify(stale)
        # Cold-start/database-outage fallback: keep the dashboard renderable instead of
        # returning a 503 that leaves cards stuck at "Checking…". This is display-only;
        # all paid actions must continue to revalidate provider/account state.
        try:
            safe_config = config.public_dict()
        except Exception:
            safe_config = {}
        try:
            safe_progress = supervisor.current_ui_progress()
        except Exception:
            safe_progress = {"action": None, "stage": "Unavailable", "percent": 0,
                             "status": "idle", "detail": "Database snapshot unavailable"}
        try:
            safe_blockers = config.live_blockers(load_profiles(config.miner_profiles_path))
        except Exception:
            safe_blockers = ["Dashboard database unavailable; live spending remains blocked"]
        return jsonify({
            "ok": True,
            "config": safe_config,
            "login": {"username": (os.getenv("APP_USER", "gph").strip() or "gph"),
                      "password_configured": bool(os.getenv("APP_PASSWORD", "")),
                      "session_authenticated": session.get("gph_authenticated") is True},
            "fx": get_usd_inr_rate(allow_network=False),
            "live_blockers": safe_blockers,
            "profiles_total": 0,
            "profiles_enabled": 0,
            "last_scan_at": None,
            "last_scan_summary": {},
            "offer_diagnostics": [],
            "ui_progress": safe_progress,
            "automation": {},
            "vast_instance_snapshot": {},
            "kryptex": supervisor.cached_kryptex_status(),
            "kryptex_gpu_market": {},
            "opportunities": [],
            "instances": [],
            "summary": {},
            "vast_balance": supervisor.vast_balance_refresh_state(),
            "funding": {},
            "events": [],
            "database_status": {"available": False, "backend": db.backend, "snapshot": "unavailable",
                                "error": "Dashboard database read is temporarily unavailable. Showing safe fallback data; retry shortly.",
                                "error_type": exc.__class__.__name__},
            "setup_gaps": _safe_setup_gaps(),
            "degraded": True,
        }), 200


@app.post("/api/scan")
def scan():
    """Start or attach to a scan without holding an HTTP request open for provider calls."""
    started_at = time.monotonic()
    try:
        # The scanner selects its coin from configured pool settings (PRL by default)
        # and chooses GPU models/prices automatically from fresh benchmark economics.
        app.logger.info("Automatic profitability-first market-scan request received")
        started = supervisor.start_scan_async()
        progress = supervisor.current_ui_progress()
        app.logger.info("Manual market-scan request acknowledged (started=%s, elapsed_ms=%d)", started, int((time.monotonic() - started_at) * 1000))
        return jsonify({
            "ok": True, "started": started, "already_running": not started,
            "progress": progress,
            "message": "Market scan started in the background." if started else "A market scan is already running; follow its progress.",
        }), 202
    except Exception as exc:
        app.logger.exception("Could not start market scan worker")
        try:
            db.log("ERROR", "market_scan_start_failed", {"error": str(exc)[:300]})
            supervisor.set_ui_progress("Market scan", "Failed", 100, str(exc)[:240], "failed")
        except Exception:
            app.logger.exception("Could not persist market-scan startup failure")
        return jsonify({"ok": False, "error": f"Could not start market scan: {str(exc)[:240]}"}), 500


@app.post("/api/fx/refresh")
def refresh_fx():
    """Refresh the display-only USD/INR quote. This never affects mining decisions."""
    return jsonify({"ok": True, "fx": get_usd_inr_rate(force=True)})


@app.post("/api/automation-settings")
def automation_settings():
    data = request.get_json(silent=True) or {}
    try:
        target = int(data.get("target_active_instances", supervisor.target_instances()))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "target_active_instances must be an integer"}), 400
    # The saved UI target is the user's explicit total-instance choice. It may
    # exceed the legacy MAX_ACTIVE_INSTANCES environment default, but remains
    # bounded by an absolute software ceiling of 25. Daily spend, per-offer cost,
    # balance, profitability and provider safety checks remain mandatory.
    cap = 25
    if target < 0 or target > cap:
        return jsonify({"ok": False, "error": f"Choose a whole number from 0 to {cap} instances."}), 400
    auto_rent = bool(data.get("auto_rent_enabled", supervisor.auto_rent_is_enabled()))
    if auto_rent:
        blockers = supervisor.live_blockers()
        if blockers:
            return jsonify({"ok": False, "error": "Auto-deploy remains blocked: " + "; ".join(blockers)}), 400
        if target < 1:
            return jsonify({"ok": False, "error": "Set a target of at least 1 before enabling auto-deploy."}), 400
    db.set_setting("target_active_instances", target)
    db.set_setting("auto_rent_enabled", auto_rent)
    db.log("WARNING" if auto_rent else "INFO", "automation_settings_changed", {"target_active_instances": target, "auto_rent_enabled": auto_rent})
    return jsonify({"ok": True, "automation": supervisor.automation_state(), "message": "Automation settings saved. Auto-deploy only runs if live mode and all safety gates are configured."})


@app.post("/api/deploy-target")
def deploy_target():
    supervisor.set_ui_progress("Deploy target", "Checking deployment permission", 5, "Checking auto-deploy setting and live safety blockers")
    if not supervisor.auto_rent_is_enabled():
        message = "Auto-deploy is not enabled. Save the target and enable auto-deploy only after completing all live setup gates."
        supervisor.set_ui_progress("Deploy target", "Blocked", 100, message, "failed")
        return jsonify({"ok": False, "error": message}), 400
    blockers = supervisor.live_blockers()
    if blockers:
        message = "Deployment blocked: " + "; ".join(blockers)
        supervisor.set_ui_progress("Deploy target", "Blocked by safety gates", 100, message, "failed")
        return jsonify({"ok": False, "error": message}), 400
    try:
        supervisor.set_ui_progress("Deploy target", "Evaluating eligible opportunities", 35, "Checking current candidates, active instance count and spend caps")
        created = supervisor.auto_rent_to_target(progress_callback=lambda stage, percent, detail: supervisor.set_ui_progress("Deploy target", stage, percent, detail))
        supervisor.set_ui_progress("Deploy target", "Complete", 100, f"{len(created)} rental request(s) returned by the deployment evaluator", "complete")
        return jsonify({"ok": True, "created": created, "count": len(created), "message": f"Deployment check completed: {len(created)} new rental(s) requested. Any unfilled slots did not have a candidate passing all checks."})
    except Exception as exc:
        db.log("ERROR", "deploy_target_failed", {"error": str(exc)[:300]})
        supervisor.set_ui_progress("Deploy target", "Failed", 100, str(exc)[:240], "failed")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/kryptex/configure")
def configure_kryptex():
    data = request.get_json(silent=True) or {}
    try:
        saved = supervisor.configure_kryptex(
            username=data.get("username", ""), coin=data.get("coin", "prl"), worker=data.get("worker", "")
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Kryptex configuration could not be persisted")
        return jsonify({"ok": False, "error": "Could not save Kryptex configuration because the database is temporarily unavailable.", "error_type": exc.__class__.__name__}), 503
    status = supervisor.request_kryptex_refresh()
    return jsonify({"ok": True, "saved": saved, "kryptex": status, "message": "Kryptex public pool identity saved; connection test started in the background."}), 202


@app.post("/api/kryptex/refresh")
def refresh_kryptex():
    status = supervisor.request_kryptex_refresh()
    if not status.get("configured"):
        return jsonify({"ok": False, "error": status.get("error") or "Configure a public Kryptex username/wallet first."}), 400
    return jsonify({
        "ok": True, "kryptex": status, "kryptex_gpu_market": supervisor.cached_kryptex_gpu_market(),
        "message": "Kryptex account/worker telemetry refresh started asynchronously. The undocumented per-GPU reference endpoint remains disabled and is not used for rental economics.",
    }), 202


@app.get("/api/kryptex/status")
def kryptex_status():
    return jsonify({"ok": True, "kryptex": supervisor.kryptex_refresh_state(), "kryptex_gpu_market": supervisor.cached_kryptex_gpu_market()})


@app.post("/api/rent")
def rent():
    data = request.get_json(silent=True) or {}
    opportunity_id = str(data.get("opportunity_id", "")).strip()
    if not opportunity_id:
        return jsonify({"ok": False, "error": "opportunity_id is required"}), 400
    try:
        supervisor.set_ui_progress("Rent GPU", "Starting server-side revalidation", 10, "The server rechecks the selected saved opportunity and its live gates")
        result = supervisor.rent_opportunity(opportunity_id)
        supervisor.set_ui_progress("Rent GPU", "Request completed", 100, "Rental request returned; refresh the Instances page to verify provider status", "complete")
        return jsonify({"ok": True, "result": result, "message": "Instance creation requested. It will be monitored and terminated if safety conditions fail."})
    except Exception as exc:
        db.log("WARNING", "manual_rent_rejected", {"opportunity_id": opportunity_id, "error": str(exc)[:300]})
        supervisor.set_ui_progress("Rent GPU", "Blocked or failed", 100, str(exc)[:240], "failed")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/destroy/<int:instance_id>")
def destroy(instance_id: int):
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"ok": False, "error": "Explicit confirmation is required"}), 400
    managed = db.get_managed_instance(instance_id)
    if not managed:
        return jsonify({"ok": False, "error": "This instance was not created/managed by GPU Profit Hunter. Destroy it from Vast Console instead."}), 403
    try:
        supervisor.set_ui_progress("Destroy GPU instance", "Sending destroy request to Vast", 45, f"Requesting termination of managed instance {instance_id}")
        accepted = supervisor._destroy(instance_id, "user requested destroy from dashboard")
        if not accepted:
            message = "Vast did not accept the destroy request. Check Event log; the instance remains under monitoring and is not marked destroyed."
            supervisor.set_ui_progress("Destroy GPU instance", "Destroy request failed", 100, message, "failed")
            return jsonify({"ok": False, "error": message}), 502
        supervisor.set_ui_progress("Destroy GPU instance", "Waiting for provider confirmation", 90, f"Vast accepted termination request for instance {instance_id}; monitoring will continue until it disappears from the provider list", "running")
        return jsonify({"ok": True, "message": "Vast accepted the termination request. GPU Profit Hunter will keep checking until the instance is confirmed gone; acceptance does not yet prove billing has stopped."})
    except Exception as exc:
        db.log("ERROR", "user_destroy_failed", {"instance_id": instance_id, "error": str(exc)[:300]})
        supervisor.set_ui_progress("Destroy GPU instance", "Failed", 100, str(exc)[:240], "failed")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/monitor")
def monitor():
    if not (config.automation_mode == "live" and config.enable_live_mining):
        return jsonify({"ok": False, "error": "Live monitoring is off. Enable live monitoring only after completing setup and risk checks."}), 400
    return jsonify({"ok": True, **supervisor.monitor_once()})


@app.errorhandler(Exception)
def api_unexpected_error(error):
    """Keep API errors machine-readable while recording the real exception in logs."""
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": error.description, "status": error.code}), error.code
        return error
    incident = secrets.token_hex(4)
    app.logger.exception("Unhandled request exception id=%s method=%s path=%s", incident, request.method, request.path)
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": f"Server error while processing this API request (reference {incident}). Check Render service logs for the matching exception.",
            "incident_id": incident,
        }), 500
    return "Unexpected server error. Check the application logs.", 500


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"ok": False, "error": "Request too large. Reduce the request payload and try again."}), 413


# Gunicorn imports app:app without executing the __main__ block. Start the supervisor
# when scanning/live automation is enabled OR whenever a Vast key exists so previously
# rented instances remain monitored even if the operator disables new scans. Render is
# configured with one Gunicorn worker; multiple workers would duplicate these threads.
if config.auto_scan_enabled or (config.automation_mode == "live" and config.enable_live_mining) or config.vast_api_key:
    supervisor.start()


def _warm_fx_reference() -> None:
    """Load the display-only exchange-rate cache off the request path."""
    try:
        get_usd_inr_rate(allow_network=True)
    except Exception:
        app.logger.warning("Initial USD/INR refresh failed", exc_info=True)


# The initial HTML no longer remains in "INR conversion loading" just because
# an FX provider call is needed. This background warm-up cannot block /api/state.
def _prime_dashboard_cache() -> None:
    # Warm the shared dashboard read so the first page load does not hit a cold, slow database.
    for _ in range(5):
        try:
            _dashboard_bundle()
            return
        except Exception:
            time.sleep(5)


Thread(target=_prime_dashboard_cache, name="gph-dashboard-prime", daemon=True).start()
Thread(target=_warm_fx_reference, name="gph-fx-warmup", daemon=True).start()


if __name__ == "__main__":
    host = config.app_host
    password = os.getenv("APP_PASSWORD", "")
    if host not in {"127.0.0.1", "localhost", "::1"} and not password:
        raise SystemExit("Refusing to expose the dashboard on a non-local interface without APP_PASSWORD. Set a strong password in .env or bind to 127.0.0.1.")
    if config.auto_scan_enabled or (config.automation_mode == "live" and config.enable_live_mining):
        supervisor.start()
    print(f"GPU Profit Hunter starting at http://{host}:{config.app_port}")
    print("Live spending is disabled by default. Read README.md before enabling it.")
    app.run(host=host, port=config.app_port, debug=False, threaded=True)
