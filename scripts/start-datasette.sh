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

mkdir -p /data
mkdir -p "$(dirname "$LOGS_DB_PATH")" "$(dirname "$PROXY_DB_PATH")"
touch "$LOGS_DB_PATH" "$PROXY_DB_PATH"

# Canonical paths keep database names stable as "logs" and "proxy".
if [ "$LOGS_DB_PATH" != "/data/logs.db" ]; then
  ln -sf "$LOGS_DB_PATH" /data/logs.db
fi
ln -sf "$PROXY_DB_PATH" /data/proxy.db

exec datasette \
  /data/logs.db \
  /data/proxy.db \
  --crossdb \
  --plugins-dir /app/plugins \
  --metadata /app/metadata.json \
  --host "$DATASETTE_HOST" \
  --port "$DATASETTE_PORT" \
  --setting sql_time_limit_ms "$SQL_TIME_LIMIT_MS" \
  --setting default_page_size "$DEFAULT_PAGE_SIZE" \
  --setting max_returned_rows "$MAX_RETURNED_ROWS" \
  --setting truncate_cells_html "$TRUNCATE_CELLS_HTML"
