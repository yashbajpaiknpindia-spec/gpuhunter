from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import requests


class VastAPIError(RuntimeError):
    def __init__(self, message: str = "", status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class VastRateLimitError(VastAPIError):
    """HTTP 429 from Vast. ``retry_after`` is the number of seconds Vast asked us to wait."""

    quota = False

    def __init__(self, message: str, retry_after: float = 60.0, limit: int | None = None, remaining: int | None = None):
        super().__init__(message, status_code=429)
        self.retry_after = max(1.0, float(retry_after))
        self.limit = limit
        self.remaining = remaining


class VastQuotaError(VastRateLimitError):
    """The account's daily search-row quota is used up (``search_quota_exceeded``).

    Every further search is rejected until the quota resets, so callers must stop searching
    for ``retry_after`` seconds instead of retrying on the normal scan interval.
    """

    quota = True


def seconds_until_utc_midnight(now: float | None = None) -> float:
    now = time.time() if now is None else now
    return 86400.0 - (now % 86400.0)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _rate_limit_error(response: Any, raw: bytes) -> VastRateLimitError:
    """Turn a 429 response into a typed error carrying Vast's own retry hint."""
    body: Any = None
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        pass
    info = body if isinstance(body, dict) else {}
    code = str(info.get("error") or "").lower()
    text = str(info.get("msg") or info.get("message") or info.get("error") or raw[:200].decode("utf-8", errors="replace"))

    def number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    retry_after = number(info.get("retry_after"))
    if retry_after is None:
        retry_after = number(response.headers.get("Retry-After"))
    limit, remaining = number(info.get("limit")), number(info.get("remaining"))
    is_quota = "quota" in code or "quota" in text.lower()
    if is_quota:
        wait = retry_after if retry_after and retry_after > 0 else seconds_until_utc_midnight()
        detail = f"{int(limit):,} rows/day, " if limit is not None else ""
        message = (f"Vast daily search quota exhausted ({detail}{int(remaining) if remaining is not None else 0} rows remaining); "
                   f"searching is paused for {format_duration(wait)}")
        return VastQuotaError(message, retry_after=wait,
                              limit=int(limit) if limit is not None else None,
                              remaining=int(remaining) if remaining is not None else None)
    wait = retry_after if retry_after and retry_after > 0 else 60.0
    return VastRateLimitError(f"Vast rate limit hit (HTTP 429); retry in {format_duration(wait)}: {text[:160]}", retry_after=wait)


# Hard wall-clock ceiling for one offer-search HTTP call. ``requests`` timeouts are
# per-socket-operation: they do not bound DNS resolution, and a response that keeps
# trickling bytes never trips the read timeout. Either can leave a scan sitting on
# "Searching preferred Vast offers" indefinitely while it owns the scan lock, which is
# exactly the "stuck at 12%, no opportunities" failure seen on Render.
DEFAULT_SEARCH_DEADLINE_SECONDS = 30
MAX_RESPONSE_BYTES = 40 * 1024 * 1024


def _run_with_deadline(fn: Callable[[], Any], seconds: float, label: str) -> Any:
    """Run ``fn`` on a daemon thread and give up after ``seconds`` of wall-clock time.

    A thread that is stuck in a blocking socket/DNS call cannot be killed, so it is
    abandoned (daemon) and the caller gets a normal, descriptive error it can report.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller below
            box["error"] = exc

    worker = threading.Thread(target=target, name=f"gph-deadline-{label[:24]}", daemon=True)
    worker.start()
    worker.join(max(0.1, float(seconds)))
    if worker.is_alive():
        raise VastAPIError(f"{label} did not finish within {int(seconds)}s (provider or network stalled)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class VastClient:
    def __init__(self, api_key: str, base_url: str = "https://cloud.vast.ai/api/v0", timeout: int = 20,
                 search_deadline: float = DEFAULT_SEARCH_DEADLINE_SECONDS):
        if not api_key:
            raise VastAPIError("VAST_API_KEY is not configured")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.search_deadline = search_deadline
        self._api_key = api_key
        self.session = self._new_session()
        self.last_call: dict[str, Any] | None = None

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GPU-Profit-Hunter/0.1"
        })
        return session

    def _reset_session(self) -> None:
        """Drop pooled keep-alive sockets after a stall so the next call starts clean."""
        old = self.session
        self.session = self._new_session()
        try:
            old.close()
        except Exception:
            pass

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        url = path if path.startswith(("https://", "http://")) else f"{self.base_url}{path}"
        call_started = time.monotonic()
        self.last_call = {"path": path, "status": None, "seconds": None, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

        def note(status: Any) -> None:
            elapsed = round(time.monotonic() - call_started, 2)
            self.last_call.update(status=status, seconds=elapsed)
            if path.startswith("/bundles") and (status != 200 or elapsed > 8):
                import logging
                logging.getLogger("gph.vast").warning("Vast GET %s -> %s in %.1fs", path, status, elapsed)
        # (connect, read) timeouts, plus a total wall-clock budget enforced while the body
        # is streamed, so a slow-drip response cannot outlive the caller's deadline.
        budget = max(float(self.timeout), float(self.search_deadline)) - 2.0
        started = time.monotonic()
        try:
            response = self.session.request(method, url, timeout=(5, self.timeout), stream=True, **kwargs)
        except requests.RequestException as exc:
            note(f"connection error: {exc.__class__.__name__}")
            raise VastAPIError(f"Vast API connection failed: {exc.__class__.__name__}") from exc
        try:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise VastAPIError("Vast API response exceeded the maximum allowed size")
                if time.monotonic() - started > budget:
                    raise VastAPIError(f"Vast API response was too slow (over {int(budget)}s)")
            raw = b"".join(chunks)
        except requests.RequestException as exc:
            raise VastAPIError(f"Vast API connection failed while reading: {exc.__class__.__name__}") from exc
        finally:
            response.close()
        note(response.status_code)
        if response.status_code == 429:
            raise _rate_limit_error(response, raw)
        if not response.ok:
            body = raw[:400].decode("utf-8", errors="replace")
            raise VastAPIError(f"Vast API returned HTTP {response.status_code}: {body}", status_code=response.status_code)
        try:
            return json.loads(raw.decode(response.encoding or "utf-8", errors="replace"))
        except ValueError as exc:
            raise VastAPIError("Vast API returned non-JSON response") from exc

    @staticmethod
    def _extract_offers(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
        """Unwrap supported API response envelopes without silently dropping payloads."""
        keys_seen: list[str] = []
        current = data
        for _ in range(4):
            if isinstance(current, list):
                return [x for x in current if isinstance(x, dict)], keys_seen
            if not isinstance(current, dict):
                return [], keys_seen
            keys_seen.extend(str(k) for k in current.keys())
            found = False
            empty_list_seen = False
            for key in ("offers", "results", "data", "machines", "bundles"):
                value = current.get(key)
                if isinstance(value, list):
                    rows = [x for x in value if isinstance(x, dict)]
                    if rows:
                        return rows, keys_seen
                    empty_list_seen = True
                if isinstance(value, dict):
                    current = value
                    found = True
                    break
            if empty_list_seen and not found:
                return [], keys_seen
            if not found:
                return [], keys_seen
        return [], keys_seen

    @staticmethod
    def _order_clause(order: str | None) -> list[list[str]] | None:
        """Convert ``"dph_total"`` / ``"-score"`` (Vast CLI style) to the REST ``order`` pairs."""
        text = (order or "").strip()
        if not text:
            return None
        pairs: list[list[str]] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            pairs.append([part[1:], "desc"] if part.startswith("-") else [part, "asc"])
        return pairs or None

    def _search_blocked(self) -> VastRateLimitError | None:
        """The last 429 we saw, while its retry window is still open (no HTTP call needed)."""
        blocked = getattr(self, "_blocked", None)
        if blocked and time.time() < blocked[0]:
            return blocked[1]
        return None

    def search_offers_with_diagnostics(
        self,
        limit: int = 100,
        min_reliability: float = 0.99,
        progress_callback: Callable[[str, int, str], None] | None = None,
        *,
        include_audit: bool = True,
        audit_limit: int | None = None,
        audit_skip_reason: str = "",
        order: str | None = None,
        max_total_hourly_usd: float = 0.0,
        affordability_budgets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run a strict low-cost query and a quota-paced broad marketplace audit.

        Vast bills searches by *rows returned* against a daily quota, so every row requested
        here has a cost. The strict query finds offers that meet provider-side live filters
        (sorted server-side so the ``limit`` keeps the most useful rows). The broad query only
        exists to explain filtered-out offers, so the caller can skip it (``include_audit=False``)
        to conserve quota. Nothing here starts a rental; missing or uncertain metadata is
        handled fail-closed by the scanner.

        A 429 on the strict query is raised (``VastQuotaError`` / ``VastRateLimitError``): it is
        not "the market is empty" and retrying it on the normal scan interval just wastes calls.
        """
        report = progress_callback or (lambda stage, percent, detail: None)
        safe_limit = min(max(int(limit), 1), 500)
        safe_audit_limit = min(max(int(audit_limit if audit_limit is not None else safe_limit), 1), 500)
        order_pairs = self._order_clause(order)
        strict_query: dict[str, Any] = {
            "num_gpus": {"eq": 1},
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "reliability": {"gte": min_reliability},
            "type": "ondemand",
            "limit": safe_limit,
        }
        if max_total_hourly_usd and max_total_hourly_usd > 0:
            # dph_total is a lower bound of the all-in rate the scanner enforces, so rows above the
            # cap would be rejected locally anyway; filtering them server-side saves quota rows.
            strict_query["dph_total"] = {"lte": float(max_total_hourly_usd)}

        def search(query: dict[str, Any], ordered: bool) -> tuple[list[dict[str, Any]], list[str]]:
            blocked = self._search_blocked()
            if blocked is not None:
                raise blocked  # still inside Vast's retry window: do not spend another call
            if ordered and order_pairs and getattr(self, "_order_supported", True):
                query = {**query, "order": order_pairs}
            # Current Vast REST documentation specifies GET /bundles/?q=<JSON query>.
            def call() -> Any:
                return self._request("GET", "/bundles/", params={"q": json.dumps(query, separators=(",", ":"))})
            try:
                try:
                    payload = _run_with_deadline(call, self.search_deadline, "Vast offer search")
                except VastAPIError as exc:
                    if "order" in query and exc.status_code in (400, 422):
                        # The REST docs and CLI docs disagree on the sort syntax. If this account/API
                        # version rejects it, remember that and fall back to Vast's default ordering.
                        self._order_supported = False
                        query = {k: v for k, v in query.items() if k != "order"}
                        payload = _run_with_deadline(call, self.search_deadline, "Vast offer search")
                    else:
                        raise
            except VastRateLimitError as exc:
                self._blocked = (time.time() + exc.retry_after, exc)
                raise
            except VastAPIError as exc:
                if "did not finish" in str(exc):
                    self._reset_session()  # abandoned thread may hold a half-open socket
                raise
            return self._extract_offers(payload)

        def safe_error(exc: Exception) -> str:
            message = str(exc)[:220]
            configured_key = str(self.session.headers.get("Authorization", "")).replace("Bearer ", "")
            return message.replace(configured_key, "[redacted]") if configured_key else message

        report("Searching preferred Vast offers", 12,
               f"Querying up to {safe_limit} verified, rentable, on-demand single-GPU offers"
               + (f" sorted by {order}" if order_pairs else "") + " through GET /bundles/")
        strict_offers: list[dict[str, Any]] = []
        strict_keys: list[str] = []
        strict_error = ""
        try:
            strict_offers, strict_keys = search(strict_query, True)
        except VastRateLimitError:
            raise
        except Exception as exc:
            strict_error = safe_error(exc)
        report("Preferred Vast search finished", 15,
               f"{len(strict_offers)} preferred offers" + (f" (search problem: {strict_error[:120]})" if strict_error else ""))

        targeted_offers: list[dict[str, Any]] = []
        targeted_errors: list[str] = []
        targeted_budgets = affordability_budgets if isinstance(affordability_budgets, list) else []
        # Use previously measured conservative per-model economics to ask Vast for the exact
        # GPU models at or below their affordable all-in rate. This targeted pass runs only
        # alongside the quota-paced broad audit; current local economics are recalculated later.
        if include_audit and not strict_error and targeted_budgets:
            for budget_row in targeted_budgets[:6]:
                if not isinstance(budget_row, dict):
                    continue
                model = str(budget_row.get("gpu_name") or "").strip()
                try:
                    cap = float(budget_row.get("max_total_hourly_usd") or 0)
                except (TypeError, ValueError):
                    continue
                if not model or cap <= 0:
                    continue
                if max_total_hourly_usd and max_total_hourly_usd > 0:
                    cap = min(cap, float(max_total_hourly_usd))
                targeted_query = {
                    "gpu_name": {"in": [model]},
                    "num_gpus": {"eq": 1},
                    "verified": {"eq": True},
                    "rentable": {"eq": True},
                    "reliability": {"gte": min_reliability},
                    "type": "ondemand",
                    "dph_total": {"lte": cap},
                    "limit": 10,
                }
                try:
                    rows, _ = search(targeted_query, True)
                    for row in rows:
                        tagged = dict(row)
                        tagged["_gph_search_stage"] = "targeted"
                        tagged["_gph_affordability_cap_usd_h"] = round(cap, 6)
                        tagged["_gph_target_gpu_name"] = model
                        targeted_offers.append(tagged)
                except VastRateLimitError as exc:
                    targeted_errors.append(safe_error(exc))
                    rate_limit = {"retry_after": exc.retry_after, "quota": exc.quota, "limit": exc.limit, "remaining": exc.remaining}
                    break
                except Exception as exc:
                    targeted_errors.append(f"{model}: {safe_error(exc)}")

        fallback_offers: list[dict[str, Any]] = []
        fallback_keys: list[str] = []
        fallback_error = ""
        audit_ran = False
        rate_limit: dict[str, Any] | None = rate_limit
        if include_audit and strict_error:
            audit_skip_reason = "the preferred query failed, so a second query would likely fail too"
            include_audit = False
        if include_audit:
            # A broad query is the discovery net: it exposes cheaper/unusual GPU models and offers
            # filtered by the strict server-side predicates. The hourly audit interval and shared
            # daily row budget limit quota use; all offers are rechecked locally before any rental.
            report("Auditing broader Vast market", 16, f"Checking up to {safe_audit_limit} wider on-demand offers to explain filtered or uncertain offers")
            audit_ran = True
            fallback_query = {"num_gpus": {"gte": 1}, "type": "ondemand", "limit": safe_audit_limit}
            try:
                fallback_offers, fallback_keys = search(fallback_query, True)
            except VastRateLimitError as exc:
                fallback_error = safe_error(exc)
                rate_limit = {"retry_after": exc.retry_after, "quota": exc.quota, "limit": exc.limit, "remaining": exc.remaining}
            except Exception as exc:
                fallback_error = safe_error(exc)
        else:
            report("Skipping broad Vast audit", 16, audit_skip_reason or "Broad audit not requested for this scan")

        # Merge by offer ID. Strict results win conflicting safety fields because those records
        # passed the strict provider query; broad-only records remain tagged as fallback candidates.
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        def key_for(offer: dict[str, Any], position: int, stage: str) -> str:
            offer_id = offer.get("id") or offer.get("offer_id")
            if offer_id is not None:
                return f"id:{offer_id}"
            signature = "|".join(str(offer.get(k, "")) for k in ("gpu_name", "num_gpus", "dph_total", "geolocation", "country"))
            return f"missing:{signature}"

        for position, offer in enumerate(fallback_offers, start=1):
            key = key_for(offer, position, "fallback")
            row = dict(offer)
            row["_gph_search_stage"] = "fallback"
            if key not in merged:
                merged[key] = row
                order.append(key)
            else:
                merged[key].update(row)
                merged[key]["_gph_search_stage"] = "fallback"
        for position, offer in enumerate(targeted_offers, start=1):
            key = key_for(offer, position, "targeted")
            row = dict(offer)
            row["_gph_search_stage"] = "targeted"
            if key not in merged:
                merged[key] = row
                order.append(key)
            else:
                merged[key].update(row)
                merged[key]["_gph_search_stage"] = "targeted"
        for position, offer in enumerate(strict_offers, start=1):
            key = key_for(offer, position, "strict")
            row = dict(offer)
            row["_gph_search_stage"] = "strict"
            if key not in merged:
                merged[key] = row
                order.append(key)
            else:
                # Strict query membership is evidence for fields the strict query required even
                # when its response omits those keys. Do not let conflicting broad-query metadata
                # override or poison that stricter result; retain all non-safety display fields.
                merged[key].update(row)
                for safety_key in ("num_gpus", "verified", "rentable", "reliability"):
                    if safety_key not in row:
                        merged[key].pop(safety_key, None)
                merged[key]["_gph_search_stage"] = "strict"

        offers = [merged[key] for key in order]
        order_note = f", sorted by {order}" if order_pairs and getattr(self, "_order_supported", True) else ""
        if not audit_ran:
            # Do not claim "broad GET returned zero": it was deliberately not run, to save quota rows.
            if strict_offers:
                diagnostic = (
                    f"Strict GET returned {len(strict_offers)} rows (limit {safe_limit}{order_note}). "
                    f"Broad audit skipped: {audit_skip_reason or 'not requested for this scan'}."
                )
            else:
                diagnostic = (
                    f"Strict GET returned zero eligible offers (limit {safe_limit}{order_note}); broad audit skipped: "
                    f"{audit_skip_reason or 'not requested for this scan'}. No individual offer rows are available to label."
                )
        elif fallback_error and not fallback_offers:
            diagnostic = (
                f"Strict GET returned {len(strict_offers)} rows. The broad audit query failed, so filtered-out offers "
                "are not listed for this scan (this is not an empty market)."
            )
        elif fallback_offers:
            diagnostic = (
                f"Strict GET returned {len(strict_offers)} rows; broad fallback GET returned {len(fallback_offers)} rows; "
                f"{len(offers)} unique offers retained for the audit (duplicates merged). Broad-only offers "
                "are reviewed locally and cannot rent unless one-GPU, verification, rentability and reliability checks pass."
            )
        elif offers:
            diagnostic = (
                f"Strict GET returned {len(strict_offers)} eligible rows, but broad GET returned zero. "
                "Retained strict results; inspect API response shape/permissions if broader rejected rows are expected."
            )
        else:
            diagnostic = (
                "Strict GET and broad GET returned zero offer rows. No individual offer rows "
                "are available to label. Check Vast API key/permissions, API base URL, query response shape and account status. "
                "Public marketplace inventory does not guarantee identical authenticated API results."
            )
        request_errors = []
        if strict_error:
            request_errors.append(f"Strict GET error: {strict_error}")
        if targeted_errors:
            request_errors.append("Targeted affordability search errors: " + "; ".join(targeted_errors[:3]))
        if fallback_error:
            request_errors.append(f"Broad GET error: {fallback_error}")
        if request_errors:
            diagnostic += " " + " ".join(request_errors)

        query_stage = "combined" if strict_offers and fallback_offers else (
            "strict" if strict_offers else ("fallback" if fallback_offers else "empty")
        )
        return {
            "offers": offers,
            "raw_count": len(offers),
            "strict_count": len(strict_offers),
            "targeted_count": len(targeted_offers),
            "fallback_count": len(fallback_offers),
            # Rows Vast returned for this scan; this is what counts against the daily search quota.
            "rows_returned": len(strict_offers) + len(targeted_offers) + len(fallback_offers),
            "strict_limit": safe_limit,
            "audit_limit": safe_audit_limit if audit_ran else 0,
            "audit_ran": audit_ran,
            "audit_skipped_reason": None if audit_ran else (audit_skip_reason or "not requested"),
            "order": order if order_pairs and getattr(self, "_order_supported", True) else None,
            "rate_limit": rate_limit,
            # Retain compatibility fields for existing dashboard/DB consumers, while making
            # clear that no alternate GET/POST endpoint was attempted.
            "get_fallback_count": 0,
            "strict_error": strict_error or None,
            "fallback_error": fallback_error or None,
            "fallback_post_error": fallback_error or None,
            "get_fallback_error": None,
            "fallback_used": bool(fallback_offers),
            "query_stage": query_stage,
            "response_keys": list(dict.fromkeys(strict_keys + fallback_keys))[:30],
            "diagnostic": diagnostic,
        }

    def get_offer(self, offer_id: int, min_reliability: float = 0.99) -> dict[str, Any] | None:
        """Fetch one offer from the live marketplace for pre-rental revalidation.

        The server-side query asks for the selected ID plus the required safety gates.
        A missing row is treated as no longer available; callers must fail closed.
        """
        query = {
            "id": {"eq": int(offer_id)},
            "num_gpus": {"eq": 1},
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "reliability": {"gte": float(min_reliability)},
            "type": "ondemand",
            "limit": 2,  # an exact-ID query matches at most one row; keep its quota cost minimal
        }
        payload = self._request("GET", "/bundles/", params={"q": json.dumps(query, separators=(",", ":"))})
        offers, _keys = self._extract_offers(payload)
        for offer in offers:
            raw_id = offer.get("id") or offer.get("offer_id")
            try:
                if int(raw_id) == int(offer_id):
                    row = dict(offer)
                    row["_gph_search_stage"] = "strict"
                    return row
            except (TypeError, ValueError):
                continue
        return None

    def search_offers(self, limit: int = 100, min_reliability: float = 0.99) -> list[dict[str, Any]]:
        """Compatibility wrapper; prefer search_offers_with_diagnostics in the scanner."""
        return self.search_offers_with_diagnostics(limit, min_reliability)["offers"]

    def list_instances(self) -> list[dict[str, Any]]:
        """List the account fleet through Vast's v1 endpoint.

        Vast has deprecated the v0 account-wide instance-list route. Never turn an
        unrecognized or incomplete response into an empty fleet: doing so can make a
        running, billable instance invisible to the monitor.
        """
        url = "https://cloud.vast.ai/api/v1/instances/?owner=me"
        data = self._request("GET", url)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            if data.get("success") is False or data.get("error"):
                raise VastAPIError(f"Vast instance-list request was rejected: {str(data.get('error') or data)[:240]}")
            rows = None
            for key in ("instances", "results", "data"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
            if rows is None:
                keys = ", ".join(sorted(str(k) for k in data.keys())[:20]) or "no fields"
                raise VastAPIError(f"Vast v1 returned an unexpected instance-list response (expected instances/results/data array; keys: {keys})")
            pagination = data.get("pagination")
            has_more = bool(data.get("has_more") or data.get("hasMore") or data.get("next_cursor"))
            if isinstance(pagination, dict):
                has_more = has_more or bool(pagination.get("has_more") or pagination.get("next_cursor") or pagination.get("next"))
            if has_more:
                raise VastAPIError("Vast v1 instance list is paginated; refusing to treat the first page as the complete fleet")
        else:
            raise VastAPIError("Vast v1 returned an unexpected instance-list response type")
        if any(not isinstance(row, dict) for row in rows):
            raise VastAPIError("Vast v1 instance-list response contains non-object entries; refusing to infer fleet state")
        return rows

    def get_current_user(self) -> dict[str, Any]:
        """Return the authenticated Vast account profile, including its credit balance.

        This is a read-only call. Keep the account payload inside the provider layer so
        callers can deliberately select safe fields instead of exposing it to the UI.
        """
        data = self._request("GET", "/users/current/")
        if not isinstance(data, dict):
            raise VastAPIError("Vast returned an unexpected current-user response")
        user = data.get("user")
        if isinstance(user, dict):
            merged = dict(user)
            for key in ("balance", "credit", "username", "id"):
                if key not in merged and key in data:
                    merged[key] = data[key]
            return merged
        return data

    def create_instance(self, offer_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._request("PUT", f"/asks/{int(offer_id)}/", json=payload)
        if not isinstance(data, dict):
            raise VastAPIError("Unexpected instance-creation response")
        if data.get("success") is False or data.get("error"):
            raise VastAPIError(f"Vast rejected instance creation: {data.get('error') or data}")
        if not data.get("new_contract") and not data.get("id"):
            raise VastAPIError(f"Vast did not return a new instance ID: {data}")
        return data

    def destroy_instance(self, instance_id: int) -> dict[str, Any]:
        data = self._request("DELETE", f"/instances/{int(instance_id)}/")
        if not isinstance(data, dict):
            raise VastAPIError("Unexpected instance-destroy response")
        if data.get("success") is False or data.get("error"):
            raise VastAPIError(f"Vast rejected instance destruction: {data.get('error') or data}")
        return data
