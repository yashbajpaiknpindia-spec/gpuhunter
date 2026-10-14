from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _PostgresConnection:
    """Small adapter for the SQL API used by this app; keeps '?' placeholders portable."""

    def __init__(self, conn: Any):
        # The underlying connection is either a direct psycopg connection or a
        # connection borrowed from psycopg_pool. Pool ownership/lifetime is handled
        # by Database.session(), not by this lightweight SQL adapter.
        self._conn = conn

    def execute(self, sql: str, params: Iterable[Any] = ()):
        # All user SQL in this app uses qmark placeholders and has no literal question marks.
        return self._conn.execute(sql.replace("?", "%s"), tuple(params))

    def executescript(self, sql: str) -> None:
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)

    def executemany(self, sql: str, params_seq: Iterable[Iterable[Any]]):
        # Use psycopg's batched execution for repeated writes. With the app and
        # database in different Render regions, one execute() per row adds a full
        # network round trip per opportunity and can hold a transaction open for
        # tens of seconds while the dashboard waits for fresh data.
        return self._conn.executemany(sql.replace("?", "%s"), params_seq)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class Database:
    """Persistent audit store using DATABASE_URL (PostgreSQL) or DATABASE_PATH (SQLite)."""

    def __init__(self, location: str):
        self.location = str(location).strip()
        self.is_postgres = self.location.startswith(("postgres://", "postgresql://"))
        self._pool = None
        if not self.location:
            raise RuntimeError("Set DATABASE_URL for Render PostgreSQL or DATABASE_PATH for local SQLite")
        if self.is_postgres:
            # Render's DB logs showed a new TLS/auth handshake for nearly every
            # individual SELECT/UPDATE. The dashboard reads many small rows per
            # request, so per-query connections can turn /api/state into a long
            # sequence of handshakes and keep the UI on its initial "Checking…"
            # state. Reuse a small, bounded pool. If psycopg_pool is missing in a
            # local/test environment, retain the old direct-connect fallback.
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
                conninfo = self.location
                if conninfo.startswith("postgres://"):
                    conninfo = "postgresql://" + conninfo[len("postgres://"):]
                self._pool = ConnectionPool(
                    conninfo=conninfo,
                    min_size=2,
                    # Keep the pool bounded. Dashboard reads have been consolidated into
                    # one transaction and monitor fan-out is limited, so we avoid using
                    # PostgreSQL connections as a substitute for request/task queues.
                    max_size=10,
                    timeout=6,
                    max_lifetime=300,
                    max_idle=60,
                    reconnect_timeout=30,
                    # Render has returned a pooled SSL connection in BAD state.
                    # Validate reused connections before handing them to a request.
                    check=ConnectionPool.check_connection,
                    kwargs={
                        "row_factory": dict_row,
                        "connect_timeout": 5,
                        "application_name": "gpu-profit-hunter",
                        "keepalives": 1,
                        "keepalives_idle": 30,
                        "keepalives_interval": 10,
                        "keepalives_count": 3,
                    },
                    open=True,
                )
            except ImportError:
                self._pool = None
        else:
            self.location = str(Path(self.location).expanduser())
            Path(self.location).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    # Seconds to wait for a pooled connection. 3s was too tight: a few dashboard tabs
    # polling heavy reads at once made every other caller (scan, supervisor) time out.
    checkout_timeout = 8

    @property
    def backend(self) -> str:
        return "postgresql" if self.is_postgres else "sqlite"

    @property
    def pool_enabled(self) -> bool:
        """Whether Postgres requests reuse pooled connections."""
        return self._pool is not None

    def pool_stats(self) -> dict[str, Any]:
        """Return cheap in-process pool diagnostics; never checks out a DB connection."""
        if not (self.is_postgres and self._pool is not None):
            return {"enabled": False}
        try:
            stats = self._pool.get_stats()
            safe_keys = (
                "pool_size", "pool_available", "requests_waiting", "requests_num",
                "requests_errors", "connections_errors", "connections_lost",
                "requests_wait_ms", "usage_ms",
            )
            return {"enabled": True, **{key: stats.get(key) for key in safe_keys if key in stats}}
        except Exception as exc:
            return {"enabled": True, "diagnostic_error": exc.__class__.__name__}

    def connect(self):
        if self.is_postgres:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:  # pragma: no cover - only exercised on PostgreSQL deploys
                raise RuntimeError("DATABASE_URL is set for PostgreSQL, but psycopg is not installed") from exc
            url = self.location
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            return _PostgresConnection(psycopg.connect(url, row_factory=dict_row, connect_timeout=5))
        conn = sqlite3.connect(self.location, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def session(self, timeout: float | None = None):
        if self.is_postgres and self._pool is not None:
            # psycopg_pool's connection context owns commit/rollback and discards a
            # connection that fails health checks. Do not duplicate rollback here:
            # on an SSL/TLS disconnect rollback itself can raise and mask the original
            # OperationalError, as seen in the Render traceback. Always let the pool
            # clean up the connection exactly once.
            try:
                with self._pool.connection(timeout=self.checkout_timeout if timeout is None else max(0.1, float(timeout))) as raw_conn:
                    yield _PostgresConnection(raw_conn)
            except Exception as exc:
                if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "PoolTimeout":
                    raise RuntimeError("Database connection pool is busy; the operation timed out waiting for a connection") from exc
                raise
            return
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with _LOCK, self.session() as conn:
            if self.is_postgres:
                statements = [
                    """CREATE TABLE IF NOT EXISTS gph_opportunities (
                        id TEXT PRIMARY KEY,
                        scanned_at TEXT NOT NULL,
                        offer_id BIGINT NOT NULL,
                        coin_tag TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )""",
                    "CREATE INDEX IF NOT EXISTS ix_gph_opportunities_scanned_at ON gph_opportunities(scanned_at)",
                    """CREATE TABLE IF NOT EXISTS gph_events (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        level TEXT NOT NULL,
                        event TEXT NOT NULL,
                        details_json TEXT NOT NULL
                    )""",
                    """CREATE TABLE IF NOT EXISTS gph_managed_instances (
                        instance_id BIGINT PRIMARY KEY,
                        offer_id BIGINT,
                        opportunity_id TEXT,
                        coin_tag TEXT,
                        gpu_name TEXT,
                        hourly_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                        expected_gross_day DOUBLE PRECISION NOT NULL DEFAULT 0,
                        benchmark_hashrate_hs DOUBLE PRECISION NOT NULL DEFAULT 0,
                        miner_profile_id TEXT,
                        started_at TEXT NOT NULL,
                        last_monitor_at TEXT,
                        observed_hashrate_hs DOUBLE PRECISION NOT NULL DEFAULT 0,
                        observed_gpu_util DOUBLE PRECISION NOT NULL DEFAULT 0,
                        observed_gpu_temp DOUBLE PRECISION NOT NULL DEFAULT 0,
                        accepted_share_lines BIGINT NOT NULL DEFAULT 0,
                        last_share_at TEXT,
                        last_log TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'creating',
                        latest_gross_day DOUBLE PRECISION,
                        latest_net_day DOUBLE PRECISION,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )""",
                    """CREATE TABLE IF NOT EXISTS gph_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""",
                ]
                for statement in statements:
                    conn.execute(statement)
                columns = {row["column_name"] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'gph_managed_instances'"
                ).fetchall()}
                migrations = {
                    "latest_gross_day": "DOUBLE PRECISION",
                    "latest_net_day": "DOUBLE PRECISION",
                    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                for name, sql_type in migrations.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE gph_managed_instances ADD COLUMN {name} {sql_type}")
            else:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS gph_opportunities (
                        id TEXT PRIMARY KEY,
                        scanned_at TEXT NOT NULL,
                        offer_id INTEGER NOT NULL,
                        coin_tag TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_gph_opportunities_scanned_at ON gph_opportunities(scanned_at);
                    CREATE TABLE IF NOT EXISTS gph_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        level TEXT NOT NULL,
                        event TEXT NOT NULL,
                        details_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gph_managed_instances (
                        instance_id INTEGER PRIMARY KEY,
                        offer_id INTEGER,
                        opportunity_id TEXT,
                        coin_tag TEXT,
                        gpu_name TEXT,
                        hourly_cost REAL NOT NULL DEFAULT 0,
                        expected_gross_day REAL NOT NULL DEFAULT 0,
                        benchmark_hashrate_hs REAL NOT NULL DEFAULT 0,
                        miner_profile_id TEXT,
                        started_at TEXT NOT NULL,
                        last_monitor_at TEXT,
                        observed_hashrate_hs REAL NOT NULL DEFAULT 0,
                        observed_gpu_util REAL NOT NULL DEFAULT 0,
                        observed_gpu_temp REAL NOT NULL DEFAULT 0,
                        accepted_share_lines INTEGER NOT NULL DEFAULT 0,
                        last_share_at TEXT,
                        last_log TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'creating',
                        latest_gross_day REAL,
                        latest_net_day REAL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS gph_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(gph_managed_instances)").fetchall()}
                migrations = {
                    "latest_gross_day": "REAL",
                    "latest_net_day": "REAL",
                    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                for name, sql_type in migrations.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE gph_managed_instances ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _is_transient_connection_error(exc: BaseException) -> bool:
        """Classify broken pooled TLS/socket connections for idempotent read retry only."""
        name = exc.__class__.__name__
        message = str(exc).lower()
        return name in {"OperationalError", "InterfaceError"} or any(
            marker in message for marker in (
                "ssl error", "connection is lost", "consuming input failed",
                "server closed the connection", "connection reset by peer",
            )
        )

    def get_dashboard_bundle(self, opportunity_limit: int = 150, event_limit: int = 50) -> dict[str, Any]:
        """Read dashboard data using one pool checkout and one short transaction.

        The prior `/api/state` flow checked out a pooled connection independently for
        opportunities, managed instances, each setting, and events. During monitoring
        concurrency this multiplied connection pressure and made one dashboard page
        require many successful database checkouts. This method keeps the reads bounded
        and consistent without holding a connection during any external API calls.
        """
        for attempt in range(2):
            try:
                with self.session(timeout=3) as conn:
                    # Prevent cross-region/network or lock delays from pinning a web worker.
                    # This is a read-only dashboard snapshot; it must fail quickly and let
                    # /api/state serve a clearly marked degraded response rather than stall.
                    conn.execute("SET LOCAL statement_timeout = '5000ms'")
                    conn.execute("SET LOCAL lock_timeout = '1500ms'")
                    # Bound this read defensively: legacy deployments may have accumulated
                    # rows before the table became a current-scan snapshot. Rank a recent
                    # window rather than keeping a pool checkout open for an unbounded fetch.
                    # Each row carries a large JSON payload; a 3,000-row read (left over from the old
                    # 1,000-offer scans) held a pooled connection for ~18s per dashboard poll.
                    # Fetch at most twice the displayed count (minimum 300). The old
                    # 8x/3,000-row cap was intended for historical scans, but dashboard
                    # cards need only the current ranked shortlist and this transfer is
                    # particularly costly over the current cross-region database link.
                    row_cap = min(max(int(opportunity_limit) * 2, 300), 800)
                    opportunity_rows = conn.execute(
                        "SELECT payload_json FROM gph_opportunities ORDER BY scanned_at DESC LIMIT ?",
                        (row_cap,),
                    ).fetchall()
                    setting_rows = conn.execute("SELECT key,value_json FROM gph_settings").fetchall()
                    managed_rows = conn.execute(
                        "SELECT * FROM gph_managed_instances ORDER BY started_at DESC"
                    ).fetchall()
                    event_rows = conn.execute(
                        "SELECT * FROM gph_events ORDER BY id DESC LIMIT ?", (int(event_limit),)
                    ).fetchall()
                break
            except Exception as exc:
                pool_busy = isinstance(exc, RuntimeError) and "pool is busy" in str(exc)
                if attempt == 0 and self.is_postgres and (pool_busy or self._is_transient_connection_error(exc)):
                    import logging
                    logging.getLogger(__name__).warning(
                        "Transient PostgreSQL connection failure during dashboard read; retrying once (%s)",
                        exc.__class__.__name__,
                    )
                    continue
                raise

        opportunities = []
        for row in opportunity_rows:
            try:
                value = json.loads(row["payload_json"])
                if isinstance(value, dict):
                    opportunities.append(value)
            except (TypeError, ValueError, KeyError):
                continue
        opportunities.sort(key=lambda item: float(item.get("expected_net_day_usd") or 0), reverse=True)
        opportunities = opportunities[:max(0, int(opportunity_limit))]

        settings: dict[str, Any] = {}
        for row in setting_rows:
            try:
                settings[str(row["key"])] = json.loads(row["value_json"])
            except (TypeError, ValueError, KeyError):
                continue

        managed_instances: list[dict[str, Any]] = []
        for row in managed_rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json", "{}") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
                item.pop("metadata_json", None)
            managed_instances.append(item)

        events: list[dict[str, Any]] = []
        for row in event_rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError):
                details = {"parse_error": True}
            events.append({
                "id": row["id"], "created_at": row["created_at"], "level": row["level"],
                "event": row["event"], "details": details,
            })
        return {
            "opportunities": opportunities,
            "settings": settings,
            "managed_instances": managed_instances,
            "events": events,
        }

    def get_all_settings(self) -> dict[str, Any]:
        """All settings in one light query (no opportunity/event rows); used at boot."""
        with self.session() as conn:
            rows = conn.execute("SELECT key,value_json FROM gph_settings").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[str(row["key"])] = json.loads(row["value_json"])
            except (TypeError, ValueError, KeyError):
                continue
        return settings

    def log(self, level: str, event: str, details: dict[str, Any] | None = None) -> None:
        with self.session() as conn:
            conn.execute(
                "INSERT INTO gph_events(created_at,level,event,details_json) VALUES(?,?,?,?)",
                (now_iso(), level.upper(), event, json.dumps(details or {}, default=str)),
            )

    def get_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM gph_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"id": r["id"], "created_at": r["created_at"], "level": r["level"], "event": r["event"],
             "details": json.loads(r["details_json"])} for r in rows
        ]

    def replace_opportunities(self, items: list[dict[str, Any]]) -> None:
        # Serialize once, then batch the PostgreSQL upserts. Do not perform a full
        # request/response round trip for each row: the scanner's persistence phase
        # used to hold a write transaction open while the remote database was in
        # Singapore and the web service was in Ohio.
        prepared = [
            (item["id"], item["scanned_at"], int(item["offer_id"]), item["coin_tag"],
             json.dumps(item, default=str))
            for item in items
        ]
        with _LOCK, self.session() as conn:
            conn.execute("DELETE FROM gph_opportunities")
            if self.is_postgres:
                conn.executemany(
                    "INSERT INTO gph_opportunities(id,scanned_at,offer_id,coin_tag,payload_json) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET scanned_at=EXCLUDED.scanned_at,offer_id=EXCLUDED.offer_id,coin_tag=EXCLUDED.coin_tag,payload_json=EXCLUDED.payload_json",
                    prepared,
                )
            else:
                conn.executemany(
                    "INSERT OR REPLACE INTO gph_opportunities(id,scanned_at,offer_id,coin_tag,payload_json) VALUES(?,?,?,?,?)",
                    prepared,
                )

    def get_opportunities(self, limit: int = 300) -> list[dict[str, Any]]:
        # Bound DB transfer time if a legacy deployment contains extra retained rows.
        row_cap = min(max(int(limit) * 5, 2000), 10000)
        with self.session() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM gph_opportunities ORDER BY scanned_at DESC LIMIT ?",
                (row_cap,),
            ).fetchall()
        items = []
        for row in rows:
            try:
                item = json.loads(row["payload_json"])
                if isinstance(item, dict):
                    items.append(item)
            except (TypeError, ValueError, KeyError):
                continue
        items.sort(key=lambda item: float(item.get("expected_net_day_usd") or 0), reverse=True)
        return items[:limit]

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        with self.session() as conn:
            row = conn.execute("SELECT payload_json FROM gph_opportunities WHERE id=?", (opportunity_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def upsert_managed_instance(self, item: dict[str, Any]) -> None:
        fields = [
            "instance_id", "offer_id", "opportunity_id", "coin_tag", "gpu_name", "hourly_cost",
            "expected_gross_day", "benchmark_hashrate_hs", "miner_profile_id", "started_at", "last_monitor_at",
            "observed_hashrate_hs", "observed_gpu_util", "observed_gpu_temp", "accepted_share_lines",
            "last_share_at", "last_log", "status", "latest_gross_day", "latest_net_day", "metadata_json"
        ]
        values = []
        for k in fields:
            v = item.get(k)
            if k == "metadata_json":
                if v is None:
                    v = item.get("metadata", {}) or {}
                if isinstance(v, dict):
                    v = json.dumps(v, default=str)
            values.append(v)
        updates = ",".join(f"{k}=excluded.{k}" for k in fields if k != "instance_id")
        with self.session() as conn:
            conn.execute(
                f"INSERT INTO gph_managed_instances({','.join(fields)}) VALUES({','.join('?' for _ in fields)}) "
                f"ON CONFLICT(instance_id) DO UPDATE SET {updates}", values
            )

    def get_managed_instances(self) -> list[dict[str, Any]]:
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM gph_managed_instances ORDER BY started_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
            result.append(d)
        return result

    def get_managed_instance(self, instance_id: int) -> dict[str, Any] | None:
        return next((x for x in self.get_managed_instances() if int(x["instance_id"]) == int(instance_id)), None)

    def set_setting(self, key: str, value: Any) -> None:
        with self.session() as conn:
            conn.execute(
                "INSERT INTO gph_settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value), now_iso()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.session() as conn:
            row = conn.execute("SELECT value_json FROM gph_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default
