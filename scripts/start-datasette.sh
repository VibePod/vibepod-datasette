#!/bin/sh
set -eu

: "${LOGS_DB_PATH:=/data/logs.db}"
: "${PROXY_DB_PATH:=/proxy/proxy.db}"
: "${DATASETTE_HOST:=0.0.0.0}"
: "${DATASETTE_PORT:=8001}"
: "${SQL_TIME_LIMIT_MS:=60000}"
: "${DEFAULT_PAGE_SIZE:=50}"
: "${MAX_RETURNED_ROWS:=2000}"
: "${TRUNCATE_CELLS_HTML:=80}"
# Token usage cache: parsed once per captured response, then reused by dashboards.
: "${USAGE_DB_PATH:=}"
: "${USAGE_REFRESH_SECONDS:=300}"

mkdir -p /data

# Default the cache next to proxy.db, which callers mount from the host, so it
# survives container recreation. /data lives in the container layer, so a cache
# kept there is rebuilt from scratch on every `docker run`.
if [ -z "$USAGE_DB_PATH" ]; then
  proxy_dir="$(dirname "$PROXY_DB_PATH")"
  mkdir -p "$proxy_dir" 2>/dev/null || true
  if [ -w "$proxy_dir" ]; then
    USAGE_DB_PATH="$proxy_dir/usage.db"
  else
    USAGE_DB_PATH=/data/usage.db
  fi
fi
mkdir -p "$(dirname "$LOGS_DB_PATH")" "$(dirname "$PROXY_DB_PATH")"
touch "$LOGS_DB_PATH" "$PROXY_DB_PATH"

# Canonical paths keep database names stable as "logs" and "proxy".
if [ "$LOGS_DB_PATH" != "/data/logs.db" ]; then
  ln -sf "$LOGS_DB_PATH" /data/logs.db
fi
ln -sf "$PROXY_DB_PATH" /data/proxy.db

mkdir -p "$(dirname "$USAGE_DB_PATH")"
if [ "$USAGE_DB_PATH" != "/data/usage.db" ]; then
  touch "$USAGE_DB_PATH"
  ln -sf "$USAGE_DB_PATH" /data/usage.db
fi

# Refresh in the background so Datasette starts serving immediately; the first
# backfill of an existing proxy.db fills the dashboard in as it progresses.
# The refresher is supervised: if it dies on an unhandled error, it is
# restarted instead of silently leaving the cache stale.
(
  while :; do
    python /app/scripts/build_usage_cache.py \
      --proxy-db /data/proxy.db \
      --usage-db /data/usage.db \
      --logs-db /data/logs.db \
      --loop \
      --interval "$USAGE_REFRESH_SECONDS" && break
    echo "usage cache refresher exited unexpectedly; restarting in 10s" >&2
    sleep 10
  done
) &

exec datasette \
  /data/logs.db \
  /data/proxy.db \
  /data/usage.db \
  --crossdb \
  --plugins-dir /app/plugins \
  --metadata /app/metadata.json \
  --host "$DATASETTE_HOST" \
  --port "$DATASETTE_PORT" \
  --setting sql_time_limit_ms "$SQL_TIME_LIMIT_MS" \
  --setting default_page_size "$DEFAULT_PAGE_SIZE" \
  --setting max_returned_rows "$MAX_RETURNED_ROWS" \
  --setting truncate_cells_html "$TRUNCATE_CELLS_HTML"
