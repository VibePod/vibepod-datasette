# VibePod Datasette

Datasette container for browsing both VibePod SQLite databases:

- `logs.db` for session/message history
- `proxy.db` for HTTP traffic captured by `vibepod-proxy`
- proxy requests include origin container name via `http_requests.source_container_name`
- built-in HTTP observability dashboard via `datasette-dashboards` at `/-/dashboards/http-requests`
- dedicated Codex token dashboard via `datasette-dashboards` at `/-/dashboards/codex-tokens`

## Environment

- `LOGS_DB_PATH` (default `/data/logs.db`)
- `PROXY_DB_PATH` (default `/proxy/proxy.db`)
- `DATASETTE_HOST` (default `0.0.0.0`)
- `DATASETTE_PORT` (default `8001`)
- `SQL_TIME_LIMIT_MS` (default `60000`)
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

## HTTP Requests Dashboard

Open `http://localhost:8001/-/dashboards/http-requests` to view an aggregated dashboard for proxied HTTP traffic.
The dashboards index is available at `http://localhost:8001/-/dashboards`.

It includes:

- total request volume and error-rate summary cards
- status code distribution
- top hosts by traffic/errors/latency
- request/error trend chart (hourly or daily buckets)
- filterable/sortable recent-request table

Available dashboard filters/sorting:

- time range (`1h`, `24h`, `7d`, `30d`, `all`)
- host substring match
- method filter
- agent filter (derived from `source_container_name`, e.g. `vibepod-codex-...` -> `codex`)
- status-class filter (`2xx`, `3xx`, `4xx`, `5xx`, `error`)
- request table sort (time/status/duration/error-priority)
- host ranking sort (volume/error-count/latency)

If the dashboard reports that `http_requests` is missing, start VibePod traffic capture first (the proxy DB schema is created by `vibepod-proxy` once traffic is recorded).

## Codex Token Dashboard

Open `http://localhost:8001/-/dashboards/codex-tokens` to view token and model usage for Codex traffic proxied through `chatgpt.com` and `api.openai.com`.

It includes:

- total API calls
- total input and output tokens
- cached input token totals
- reasoning token totals
- token trend over time
- model and endpoint breakdowns
- websocket message volume and direction trend
- recent websocket-message table (with message type + content preview)
- recent-call table with per-request token fields

Available dashboard filters:

- time range (`1h`, `24h`, `7d`, `30d`, `all`)
- trend bucket (`auto`, `hour`, `day`)
- model
- container
- endpoint (`backend_codex`, `backend_codex_ws`, `responses`, `chat_completions`)
- request row limit

The dashboard only includes requests attributed to the `codex` agent from `source_container_name`.

## Docs

- Codex websocket token-source findings and implementation notes:
  - `docs/codex-websocket-findings.md`

## Codex Websocket Discovery Queries

Use these proxy canned queries to inspect websocket payload structure and validate token calculations:

- `codex_ws_recent_messages`
- `codex_ws_message_type_counts`
- `codex_ws_token_field_coverage`
- `codex_ws_usage_event_duplicates`
- `codex_ws_tokens_vs_http_by_request`

Open from Datasette under the `proxy` database query list, or directly via paths like:

- `/-/queries/proxy/codex_ws_token_field_coverage`
- `/-/queries/proxy/codex_ws_tokens_vs_http_by_request`
