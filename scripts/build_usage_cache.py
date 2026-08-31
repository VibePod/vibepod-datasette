#!/usr/bin/env python3
"""Materialize per-response token usage into a separate cache database.

Dashboards used to call ``extract_usage()`` over every captured body on every
chart render, so one dashboard load decompressed and JSON-parsed the same
responses a dozen times. This script parses each response exactly once and
writes the result to ``usage.db``; the dashboards then read plain integers.

The cache is incremental: ``http_responses.id`` and ``websocket_messages.id``
are autoincrement primary keys, so the highest processed id per source is a
sufficient watermark. ``proxy.db`` is opened read-only so the capture process
is never blocked by this job.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # Datasette is absent when this runs as a plain script.
    from plugins.decompress import _extract_usage
except ImportError:  # pragma: no cover - exercised via the container image
    import types

    fake = types.ModuleType("datasette")
    fake.hookimpl = lambda fn: fn
    sys.modules.setdefault("datasette", fake)
    from plugins.decompress import _extract_usage

# Bump when the stored columns or their meaning change (token_usage rows, or
# the model_pricing table shape) -- open_cache() then drops the affected
# tables/views and rebuilds them, because old rows/columns cannot be upgraded
# in place. model_pricing is cheap to drop: it holds no captured data, only a
# full reload of pricing/model_prices.json on the next refresh.
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    source             TEXT NOT NULL,
    row_id             INTEGER NOT NULL,
    request_id         TEXT NOT NULL,
    response_id        TEXT NOT NULL DEFAULT '',
    timestamp          TEXT NOT NULL,
    agent              TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    host               TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cached_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens   INTEGER NOT NULL DEFAULT 0,
    has_usage          INTEGER NOT NULL DEFAULT 0,
    container_id       TEXT NOT NULL DEFAULT '',
    container_name     TEXT NOT NULL DEFAULT '',
    workspace          TEXT NOT NULL DEFAULT '',
    workspace_name     TEXT NOT NULL DEFAULT '',
    -- When this row was written, which is what the resolution grace period
    -- counts from. The call's own timestamp cannot serve: backfilling an
    -- existing proxy.db reads calls that are days old, and those must still
    -- get their grace window before they are frozen as 'unknown'.
    ingested_at        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source, row_id)
);

-- Snapshot of logs.db sessions, refilled on every refresh. Workspace
-- resolution matches a call to the session of its container; Docker bundle
-- ids are reused for container names, so the id (12-char prefix) is the
-- reliable key, the name only a fallback.
CREATE TABLE IF NOT EXISTS session_windows (
    container_id12   TEXT NOT NULL,
    container_name   TEXT NOT NULL,
    workspace        TEXT NOT NULL,
    workspace_name   TEXT NOT NULL,
    started_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_windows_id ON session_windows(container_id12);
CREATE INDEX IF NOT EXISTS idx_session_windows_name ON session_windows(container_name, started_at);

-- Rows whose workspace has not been resolved yet; the partial index keeps
-- rescans cheap. Unresolvable rows age out after GRACE_SECONDS below and are
-- decided 'unknown' once, so they are not retried on every refresh.
CREATE INDEX IF NOT EXISTS idx_token_usage_pending ON token_usage(workspace) WHERE workspace = '';

CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_usage_agent ON token_usage(agent);
CREATE INDEX IF NOT EXISTS idx_token_usage_request ON token_usage(request_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_response ON token_usage(request_id, response_id);

CREATE TABLE IF NOT EXISTS sync_state (
    source       TEXT PRIMARY KEY,
    last_row_id  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Two de-duplication rules:
--   1. Codex subscription calls report usage on a websocket frame while their
--      HTTP twin carries none; keep the websocket row, drop the twin.
--   2. A provider can emit the same usage snapshot more than once for one
--      logical call, which inflates totals (see ccusage issue #884). When a
--      response id is present, keep a single row per (request_id, response_id)
--      -- the one reporting the most tokens, since a later snapshot may be the
--      complete one.
-- Explicit column list so the workspace columns can be normalized: rows
-- whose resolution is still pending would otherwise surface an empty label,
-- which is exactly the bucket the 'unknown' fallback avoids.
CREATE VIEW IF NOT EXISTS agent_token_usage AS
SELECT
    t.source,
    t.row_id,
    t.request_id,
    t.response_id,
    t.timestamp,
    t.agent,
    t.provider,
    t.model,
    t.host,
    t.input_tokens,
    t.output_tokens,
    t.cached_tokens,
    t.cache_write_tokens,
    t.reasoning_tokens,
    t.has_usage,
    t.container_id,
    t.container_name,
    CASE
        WHEN t.workspace = '' THEN 'unknown'
        ELSE t.workspace
    END AS workspace,
    CASE
        WHEN t.workspace_name = '' THEN 'unknown'
        ELSE t.workspace_name
    END AS workspace_name
FROM token_usage t
WHERE (
        t.source = 'ws'
        OR NOT EXISTS (
            SELECT 1 FROM token_usage w
            WHERE w.source = 'ws' AND w.request_id = t.request_id
        )
      )
  AND (
        t.response_id = ''
        OR t.row_id = (
            SELECT d.row_id FROM token_usage d
            WHERE d.source = t.source
              AND d.request_id = t.request_id
              AND d.response_id = t.response_id
            ORDER BY (d.input_tokens + d.output_tokens) DESC, d.row_id ASC
            LIMIT 1
        )
      );

-- Reference pricing data, fully replaced from pricing/model_prices.json on
-- every refresh (see sync_pricing()). A row with model = '' is a per-provider
-- catch-all: every model string is a match for the empty prefix, so it only
-- wins when no more specific entry exists for that provider. is_estimated
-- marks rows for flat-rate subscriptions (ChatGPT/Copilot): those providers
-- are not billed per token, so their price is the metered-API-equivalent
-- rate of the underlying model, shown as a "what this would cost" estimate
-- rather than real spend.
--
-- Prices change over time, so (provider, model) can have several rows with
-- different effective_from dates instead of one fixed price; a call is
-- priced at whichever rate was in effect on its own timestamp, not today's
-- rate, so historical cost totals stay correct after a price update. There
-- is no explicit "effective_to": a price's validity window implicitly ends
-- at the next later effective_from row for that same (provider, model), so
-- adding a new price point never requires touching the old row.
CREATE TABLE IF NOT EXISTS model_pricing (
    provider                 TEXT NOT NULL,
    model                    TEXT NOT NULL,
    effective_from            TEXT NOT NULL,
    input_price_per_1m       REAL NOT NULL DEFAULT 0,
    output_price_per_1m      REAL NOT NULL DEFAULT 0,
    cached_price_per_1m      REAL NOT NULL DEFAULT 0,
    cache_write_price_per_1m REAL NOT NULL DEFAULT 0,
    currency                 TEXT NOT NULL DEFAULT 'USD',
    is_estimated             INTEGER NOT NULL DEFAULT 0,
    price_source             TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider, model, effective_from)
);

-- agent_token_usage plus a per-row price match and computed cost. A call is
-- matched to the model_pricing row for its provider whose model is the
-- longest prefix of the captured model string and whose effective_from is
-- the latest one on or before the call's own timestamp -- an exact model
-- match is always the longest possible prefix, so this one rule covers both
-- exact pricing entries and dated snapshots (e.g. 'claude-opus-4-5-20260101'
-- priced as 'claude-opus-4-5') without separate logic, and picking the
-- latest effective_from <= the call's timestamp applies whichever price was
-- actually in effect when the call happened. reasoning_tokens are already
-- included in output_tokens by every provider's own accounting, so there is
-- no separate reasoning price. has_price/cost_usd are only set when
-- has_usage = 1: a call with no parsed token counts has nothing to price,
-- regardless of whether a matching pricing entry exists. is_estimated flags
-- cost_usd as a metered-equivalent estimate rather than real spend (see
-- model_pricing above); dashboards must sum real and estimated cost
-- separately so a flat-rate plan's estimate never inflates actual spend.
CREATE VIEW IF NOT EXISTS agent_token_cost AS
WITH matched AS (
    SELECT
        t.*,
        (
            SELECT mp.model FROM model_pricing mp
            WHERE mp.provider = t.provider
              AND t.model LIKE mp.model || '%'
              AND datetime(mp.effective_from) <= datetime(t.timestamp)
            ORDER BY length(mp.model) DESC, datetime(mp.effective_from) DESC
            LIMIT 1
        ) AS priced_model,
        (
            SELECT mp.effective_from FROM model_pricing mp
            WHERE mp.provider = t.provider
              AND t.model LIKE mp.model || '%'
              AND datetime(mp.effective_from) <= datetime(t.timestamp)
            ORDER BY length(mp.model) DESC, datetime(mp.effective_from) DESC
            LIMIT 1
        ) AS priced_effective_from
    FROM agent_token_usage t
)
SELECT
    m.*,
    mp.input_price_per_1m,
    mp.output_price_per_1m,
    mp.cached_price_per_1m,
    mp.cache_write_price_per_1m,
    mp.currency AS price_currency,
    mp.price_source,
    mp.effective_from AS price_effective_from,
    COALESCE(mp.is_estimated, 0) AS is_estimated,
    CASE WHEN m.has_usage = 1 AND mp.provider IS NOT NULL THEN 1 ELSE 0 END AS has_price,
    CASE
        WHEN m.has_usage = 1 AND mp.provider IS NOT NULL THEN
            (m.input_tokens * mp.input_price_per_1m
             + m.output_tokens * mp.output_price_per_1m
             + m.cached_tokens * mp.cached_price_per_1m
             + m.cache_write_tokens * mp.cache_write_price_per_1m) / 1000000.0
        ELSE NULL
    END AS cost_usd
FROM matched m
LEFT JOIN model_pricing mp
    ON mp.provider = m.provider
   AND mp.model = m.priced_model
   AND mp.effective_from = m.priced_effective_from;
"""

HTTP_SQL = """
SELECT resp.id, r.id, COALESCE(r.timestamp, resp.timestamp), r.source_container_name,
       r.source_container_id, r.host, r.body, resp.body
FROM http_responses resp
JOIN http_requests r ON r.id = resp.request_id
WHERE resp.id > ? AND r.method = 'POST' AND resp.body IS NOT NULL AND length(resp.body) > 0
ORDER BY resp.id
LIMIT ?
"""

WS_SQL = """
SELECT ws.id, r.id, ws.timestamp, r.source_container_name, r.source_container_id,
       r.host, r.body, ws.content
FROM websocket_messages ws
JOIN http_requests r ON r.id = ws.request_id
WHERE ws.id > ?
  AND json_valid(CAST(ws.content AS TEXT))
  AND json_extract(CAST(ws.content AS TEXT), '$.type') = 'response.completed'
ORDER BY ws.id
LIMIT ?
"""


def agent_from_container(container: str | None) -> str:
    name = (container or "").strip()
    if not name:
        return "unknown"
    if not name.startswith("vibepod-"):
        return name
    rest = name[len("vibepod-") :]
    head, sep, _ = rest.partition("-")
    return head if sep else rest


def extract_model(body) -> str:
    if body is None:
        return "unknown"
    try:
        from plugins.decompress import _extract_model

        return _extract_model(body) or "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def open_source(path: Path) -> sqlite3.Connection:
    """Open the capture database read-only so the proxy is never blocked."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version and version < SCHEMA_VERSION:
        # Older rows lack response ids and took the model from the request body,
        # so they cannot be migrated; drop and re-parse from proxy.db.
        # agent_token_cost depends on both agent_token_usage and model_pricing,
        # so it must be dropped first.
        conn.executescript(
            "DROP VIEW IF EXISTS agent_token_cost;"
            "DROP VIEW IF EXISTS agent_token_usage;"
            "DROP TABLE IF EXISTS token_usage;"
            "DROP TABLE IF EXISTS sync_state;"
            # Refilled from logs.db on every refresh, so dropping it costs
            # nothing and keeps a later column change from being missed here.
            "DROP TABLE IF EXISTS session_windows;"
            # Refilled from pricing/model_prices.json on every refresh (see
            # sync_pricing()), so dropping it loses nothing either -- this is
            # what actually fixes a column-shape change like adding
            # is_estimated/effective_from that CREATE TABLE IF NOT EXISTS
            # cannot apply to an existing table.
            "DROP TABLE IF EXISTS model_pricing;",
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def watermark(cache: sqlite3.Connection, source: str) -> int:
    row = cache.execute(
        "SELECT last_row_id FROM sync_state WHERE source = ?",
        (source,),
    ).fetchone()
    return int(row[0]) if row else 0


def set_watermark(cache: sqlite3.Connection, source: str, row_id: int) -> None:
    cache.execute(
        "INSERT INTO sync_state (source, last_row_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET last_row_id = excluded.last_row_id, "
        "updated_at = excluded.updated_at",
        (source, row_id, datetime.now(UTC).isoformat()),
    )


def _row_values(
    source,
    row_id,
    request_id,
    ts,
    container,
    container_id,
    host,
    request_body,
    payload,
    ingested_at="",
):
    usage = json.loads(_extract_usage(payload, host))
    # The response payload names the model for the call that actually ran; the
    # request body is only a fallback (a websocket upgrade carries no model, and
    # one connection can serve several models).
    model = usage.get("model") or extract_model(request_body)
    return (
        source,
        row_id,
        request_id,
        usage.get("response_id") or "",
        ts or "",
        agent_from_container(container),
        usage.get("provider") or "unknown",
        model or "unknown",
        host or "unknown",
        int(usage.get("input") or 0),
        int(usage.get("output") or 0),
        int(usage.get("cached") or 0),
        int(usage.get("cache_write") or 0),
        int(usage.get("reasoning") or 0),
        int(usage.get("found") or 0),
        # Docker's short id is the prefix of the full one, so truncating here
        # matches either length to the session's container_id.
        (container_id or "")[:12],
        container or "",
        # Workspace is resolved in the dedicated pass after ingest, because the
        # session row may only appear in logs.db after the call was captured.
        "",
        "",
        ingested_at or datetime.now(UTC).isoformat(),
    )


INSERT_SQL = (
    "INSERT INTO token_usage (source, row_id, request_id, response_id, timestamp, agent, "
    "provider, model, host, input_tokens, output_tokens, cached_tokens, cache_write_tokens, "
    "reasoning_tokens, has_usage, container_id, container_name, workspace, workspace_name, "
    "ingested_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(source, row_id) DO NOTHING"
)


def sync_source(source, sql, src, cache, batch_size, max_rows):
    processed = 0
    while max_rows == 0 or processed < max_rows:
        limit = batch_size if max_rows == 0 else min(batch_size, max_rows - processed)
        last_id = watermark(cache, source)
        rows = src.execute(sql, (last_id, limit)).fetchall()
        if not rows:
            # Advance past rows that never match (usage-free frames, non-POST
            # or bodyless responses) so they are not rescanned every refresh.
            table = "websocket_messages" if source == "ws" else "http_responses"
            head = src.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            if head and head > last_id:
                set_watermark(cache, source, int(head))
                cache.commit()
            break

        ingested_at = datetime.now(UTC).isoformat()
        values = [_row_values(source, *row, ingested_at=ingested_at) for row in rows]
        cache.executemany(INSERT_SQL, values)
        set_watermark(cache, source, int(rows[-1][0]))
        cache.commit()
        processed += len(rows)
        if len(rows) < limit:
            break
    return processed


def default_pricing_path() -> Path:
    return Path(__file__).resolve().parent.parent / "pricing" / "model_prices.json"


PRICING_UPSERT_SQL = (
    "INSERT INTO model_pricing (provider, model, effective_from, input_price_per_1m, "
    "output_price_per_1m, cached_price_per_1m, cache_write_price_per_1m, currency, "
    "is_estimated, price_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _pricing_row(entry: dict):
    return (
        str(entry["provider"]),
        str(entry["model"]),
        str(entry["effective_from"]),
        float(entry.get("input_price_per_1m") or 0),
        float(entry.get("output_price_per_1m") or 0),
        float(entry.get("cached_price_per_1m") or 0),
        float(entry.get("cache_write_price_per_1m") or 0),
        str(entry.get("currency") or "USD"),
        1 if entry.get("is_estimated") else 0,
        str(entry.get("price_source") or ""),
    )


def sync_pricing(cache: sqlite3.Connection, pricing_path: Path) -> int:
    """Fully replace model_pricing from the bundled pricing file.

    Unlike token_usage (an incremental cache of captured traffic), pricing is
    small, static reference data, so every refresh just reloads it wholesale:
    an entry removed from the file is gone from the table too, and a bad or
    missing file leaves the previous rows in place rather than wiping cost
    data because of a transient read error.
    """
    if not pricing_path.exists():
        print(f"pricing cache: no pricing file at {pricing_path}, skipping", flush=True)
        return 0
    try:
        entries = json.loads(pricing_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"pricing cache: skipped ({exc})", file=sys.stderr, flush=True)
        return 0

    rows = []
    for entry in entries:
        try:
            rows.append(_pricing_row(entry))
        except (KeyError, TypeError, ValueError) as exc:
            print(f"pricing cache: skipped a malformed entry ({exc})", file=sys.stderr, flush=True)

    cache.execute("DELETE FROM model_pricing")
    cache.executemany(PRICING_UPSERT_SQL, rows)
    cache.commit()
    return len(rows)


GRACE_SECONDS = 15 * 60


def _parse_ts(value: str) -> datetime | None:
    """Parse a timestamp from either source db; naive strings are assumed UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def resolve_workspace(container_id12: str, container_name: str, ts, windows):
    """Match a call to its container's session: exact id first, name fallback.

    The id match needs no time logic, because Docker container ids are never
    reused. The name fallback covers rows the proxy captured without an id and
    rows whose id matches no session at all (logs.db pruned, or a session that
    predates the id column); there the newest session that started before the
    call wins, covering `--name` reuse. ended_at is ignored on purpose: a call
    after session end from the same container still inherits that container's
    workspace. Returns (workspace, workspace_name, resolution_path) or None.
    """
    for id12, _name, workspace, workspace_name, _started_at in windows:
        if id12 and id12 == container_id12:
            return (workspace, workspace_name, "by_id")
    best = None  # newest session started before the call
    best_started = None
    for _id12, name, workspace, workspace_name, started_at in windows:
        if name != container_name:
            continue
        started = _parse_ts(started_at)
        if started is None:
            continue
        if ts is not None and started > ts:
            continue
        if best_started is None or started > best_started:
            best, best_started = (workspace, workspace_name), started
    if best is None:
        return None
    return (best[0], best[1], "by_name")


def sync_workspace(logs_path: Path, cache: sqlite3.Connection, grace_seconds=GRACE_SECONDS):
    """Refresh session_windows, then resolve every pending token_usage row.

    Rows that fail resolution AND were ingested longer ago than the grace
    period are decided 'unknown' once, so they do not get rescanned on every
    refresh. Recently ingested rows stay pending so a session row arriving in
    logs.db later still resolves them.
    Returns resolution counts for the log line.
    """
    if not logs_path.exists() or not table_exists(cache, "session_windows"):
        return None
    logs = open_source(logs_path)
    try:
        if not table_exists(logs, "sessions"):
            return None
        rows = logs.execute(
            "SELECT container_id, container_name, workspace, started_at FROM sessions",
        ).fetchall()
    finally:
        logs.close()
    windows = tuple(
        (
            (row[0] or "")[:12],
            row[1] or "",
            row[2] or "",
            Path(row[2] or "").name or row[2] or "",
            row[3] or "",
        )
        for row in rows
    )
    cache.execute("DELETE FROM session_windows")
    cache.executemany(
        "INSERT INTO session_windows (container_id12, container_name, workspace, "
        "workspace_name, started_at) VALUES (?, ?, ?, ?, ?)",
        windows,
    )
    pending = cache.execute(
        "SELECT source, row_id, container_id, container_name, timestamp, ingested_at "
        "FROM token_usage WHERE workspace = ''",
    ).fetchall()
    deadline = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    updates = []
    counts = {"by_id": 0, "by_name": 0, "unknown": 0, "pending": 0}
    for source, row_id, id12, name, ts, ingested_at in pending:
        resolved = resolve_workspace(id12, name, _parse_ts(ts), windows)
        workspace = None
        if resolved is not None:
            (workspace, workspace_name, path) = resolved
            counts[path] += 1
        else:
            # An unparseable ingest stamp must still age out, otherwise the row
            # is rescanned on every refresh forever.
            ingested = _parse_ts(ingested_at)
            if ingested is None or ingested <= deadline:
                workspace = "unknown"
                workspace_name = "unknown"
                counts["unknown"] += 1
            else:
                counts["pending"] += 1
        if workspace is not None:
            updates.append((workspace, workspace_name, source, row_id))
    if updates:
        cache.executemany(
            "UPDATE token_usage SET workspace = ?, workspace_name = ? "
            "WHERE source = ? AND row_id = ?",
            updates,
        )
    cache.commit()
    return counts


def _log_workspace_counts(counts) -> None:
    if counts is None:
        return
    print(
        "workspace resolution: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        flush=True,
    )


def build(
    proxy_path: Path,
    usage_path: Path,
    logs_path: Path | None = None,
    batch_size=500,
    max_rows=0,
    full=False,
    pricing_path: Path | None = None,
):
    cache = open_cache(usage_path)
    if full:
        cache.execute("DELETE FROM token_usage")
        cache.execute("DELETE FROM sync_state")
        cache.execute("DELETE FROM session_windows")
        cache.commit()

    priced_rows = sync_pricing(cache, pricing_path or default_pricing_path())
    print(f"pricing cache: {priced_rows} price rows loaded", flush=True)

    if not proxy_path.exists():
        if logs_path is not None:
            _log_workspace_counts(sync_workspace(logs_path, cache))
        cache.close()
        return {"http": 0, "ws": 0}

    src = open_source(proxy_path)
    counts = {"http": 0, "ws": 0}
    try:
        if table_exists(src, "http_responses") and table_exists(src, "http_requests"):
            counts["http"] = sync_source(
                "http",
                HTTP_SQL,
                src,
                cache,
                batch_size,
                max_rows,
            )
        if table_exists(src, "websocket_messages"):
            counts["ws"] = sync_source("ws", WS_SQL, src, cache, batch_size, max_rows)
        if logs_path is not None:
            _log_workspace_counts(sync_workspace(logs_path, cache))
    finally:
        src.close()
        cache.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--proxy-db",
        default=os.environ.get("PROXY_DB_PATH", "/data/proxy.db"),
    )
    parser.add_argument(
        "--usage-db",
        default=os.environ.get("USAGE_DB_PATH", "/data/usage.db"),
    )
    parser.add_argument(
        "--logs-db",
        default=os.environ.get("LOGS_DB_PATH", "/data/logs.db"),
        help="session log database used to resolve workspaces",
    )
    parser.add_argument(
        "--pricing-file",
        default=os.environ.get("PRICING_FILE_PATH", ""),
        help="model pricing JSON; defaults to the bundled pricing/model_prices.json",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="0 processes everything",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="rebuild the cache from scratch",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="keep refreshing on an interval",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("USAGE_REFRESH_SECONDS", "300")),
    )
    args = parser.parse_args()

    proxy_path = Path(args.proxy_db)
    usage_path = Path(args.usage_db)
    logs_path = Path(args.logs_db)
    pricing_path = Path(args.pricing_file) if args.pricing_file else default_pricing_path()

    while True:
        started = time.time()
        try:
            counts = build(
                proxy_path,
                usage_path,
                logs_path=logs_path,
                batch_size=args.batch_size,
                max_rows=args.max_rows,
                full=args.full,
                pricing_path=pricing_path,
            )
            print(
                f"usage cache: +{counts['http']} http, +{counts['ws']} ws rows "
                f"in {time.time() - started:.1f}s",
                flush=True,
            )
        except sqlite3.Error as exc:  # pragma: no cover - runtime resilience
            print(f"usage cache: skipped refresh ({exc})", file=sys.stderr, flush=True)
        if not args.loop:
            return 0
        args.full = False
        time.sleep(max(args.interval, 10))


if __name__ == "__main__":
    raise SystemExit(main())
