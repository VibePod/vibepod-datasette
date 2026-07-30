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
from datetime import UTC, datetime
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

# Bump when the stored columns or their meaning change; open_cache() then drops
# the cache and re-parses, because old rows cannot be upgraded in place.
SCHEMA_VERSION = 2

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
    PRIMARY KEY (source, row_id)
);

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
CREATE VIEW IF NOT EXISTS agent_token_usage AS
SELECT * FROM token_usage t
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
"""

HTTP_SQL = """
SELECT resp.id, r.id, COALESCE(r.timestamp, resp.timestamp), r.source_container_name,
       r.host, r.body, resp.body
FROM http_responses resp
JOIN http_requests r ON r.id = resp.request_id
WHERE resp.id > ? AND r.method = 'POST' AND resp.body IS NOT NULL AND length(resp.body) > 0
ORDER BY resp.id
LIMIT ?
"""

WS_SQL = """
SELECT ws.id, r.id, ws.timestamp, r.source_container_name, r.host, r.body, ws.content
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
        conn.executescript(
            "DROP VIEW IF EXISTS agent_token_usage;"
            "DROP TABLE IF EXISTS token_usage;"
            "DROP TABLE IF EXISTS sync_state;",
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


def _row_values(source, row_id, request_id, ts, container, host, request_body, payload):
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
    )


INSERT_SQL = (
    "INSERT INTO token_usage (source, row_id, request_id, response_id, timestamp, agent, "
    "provider, model, host, input_tokens, output_tokens, cached_tokens, cache_write_tokens, "
    "reasoning_tokens, has_usage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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

        values = [_row_values(source, *row) for row in rows]
        cache.executemany(INSERT_SQL, values)
        set_watermark(cache, source, int(rows[-1][0]))
        cache.commit()
        processed += len(rows)
        if len(rows) < limit:
            break
    return processed


def build(proxy_path: Path, usage_path: Path, batch_size=500, max_rows=0, full=False):
    cache = open_cache(usage_path)
    if full:
        cache.execute("DELETE FROM token_usage")
        cache.execute("DELETE FROM sync_state")
        cache.commit()

    if not proxy_path.exists():
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

    while True:
        started = time.time()
        try:
            counts = build(
                proxy_path,
                usage_path,
                batch_size=args.batch_size,
                max_rows=args.max_rows,
                full=args.full,
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
