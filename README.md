# VibePod Datasette

Datasette container for browsing both VibePod SQLite databases:

- `logs.db` for session/message history
- `proxy.db` for HTTP traffic captured by `vibepod-proxy`
- proxy requests include origin container name via `http_requests.source_container_name`

## Environment

- `LOGS_DB_PATH` (default `/data/logs.db`)
- `PROXY_DB_PATH` (default `/proxy/proxy.db`)
- `DATASETTE_HOST` (default `0.0.0.0`)
- `DATASETTE_PORT` (default `8001`)
- `SQL_TIME_LIMIT_MS` (default `10000`)
- `TRUNCATE_CELLS_HTML` (default `80`, compacts long cell values in list/table views)

## Usage

Build:

```bash
docker build -t vibepod/datasette:latest .
```

Run:

```bash
docker run --rm -p 8001:8001 \
  -v "$HOME/.config/vibepod:/vibepod:rw" \
  -e LOGS_DB_PATH=/vibepod/logs.db \
  -e PROXY_DB_PATH=/vibepod/proxy/proxy.db \
  vibepod/datasette:latest
```
