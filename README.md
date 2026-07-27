# VibePod Datasette

Datasette container for browsing both VibePod SQLite databases:

- `logs.db` for session/message history
- `proxy.db` for HTTP traffic captured by `vibepod-proxy`
- proxy requests include origin container name via `http_requests.source_container_name`
- all-agent token usage dashboard via `datasette-dashboards` at `/-/dashboards/agent-tokens`
- built-in HTTP observability dashboard via `datasette-dashboards` at `/-/dashboards/http-requests`
- agent sessions dashboard via `datasette-dashboards` at `/-/dashboards/agent-sessions`
- agent proxy requests dashboard via `datasette-dashboards` at `/-/dashboards/agent-proxy-requests`
- dedicated Codex token dashboard via `datasette-dashboards` at `/-/dashboards/codex-tokens`
- `usage.db` with per-call token usage materialized from `proxy.db` by `scripts/build_usage_cache.py`

## Environment

- `LOGS_DB_PATH` (default `/data/logs.db`)
- `PROXY_DB_PATH` (default `/proxy/proxy.db`)
- `DATASETTE_HOST` (default `0.0.0.0`)
- `DATASETTE_PORT` (default `8001`)
- `SQL_TIME_LIMIT_MS` (default `60000`)
- `USAGE_DB_PATH` (the materialized token-usage cache; defaults to `usage.db` next to
  `PROXY_DB_PATH` so it persists across container recreation, falling back to
  `/data/usage.db` when that directory is not writable)
- `USAGE_REFRESH_SECONDS` (default `300`, how often the cache picks up new traffic)
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

## All Agents Token Usage Dashboard

Open `http://localhost:8001/-/dashboards/agent-tokens` for total token consumption
across every agent, not just Claude or Codex. It reads the `usage` database, which
`scripts/build_usage_cache.py` fills from `proxy.db` by normalizing provider-specific
usage fields through the `extract_usage(body, host)` SQL function registered by
`plugins/decompress.py`.

It includes:

- total tokens, input, output, cached input, cache write, and reasoning summary cards
  (token counts are shown with a `tok` unit)
- calls-with-usage count
- tokens by agent and by provider, split into input vs output series (stacked bars)
- token trend over time (input vs output series)
- tokens by agent and model table, including cached and cache-write columns
- recent calls table with per-call token fields
- usage-coverage table: calls per host split into parsed vs unparsed

Available dashboard filters:

- time range (`1h`, `24h`, `7d`, `30d`, `all`; default `24h`)
- trend bucket (`auto`, `5min`, `hour`, `day`; `auto` uses 5-minute slots for `1h`,
  hourly for `24h`, and daily beyond that)
- agent (derived from `source_container_name`, e.g. `vibepod-tau-...` -> `tau`)
- provider (`anthropic`, `openai-codex`, `google`, `groq`, ... derived from the request host)
- host
- table row limit

### Why there is a usage cache

Response bodies are gzip/zstd/brotli blobs, so reading token counts out of them means
decompressing and JSON-parsing every captured call. Doing that inside the chart queries
meant each of the thirteen panels re-parsed the same bodies on every render and every
60-second autorefresh, which does not scale with a growing `proxy.db`.

`scripts/build_usage_cache.py` parses each response exactly once and writes one row per
call into `usage.db`; the dashboard then sums plain integers. On a 1500-call, 18 MB
fixture one live-scan panel took 0.65s (~8.5s for all thirteen), against ~0.02s for the
whole dashboard from the cache, with a one-time ~1.0s build and a ~1ms no-op refresh.

The cache is incremental and nothing is recomputed once parsed. `http_responses.id` and
`websocket_messages.id` are autoincrement keys, so the highest processed id per source is
stored in `sync_state` and each refresh only reads rows above that watermark; a refresh
with no new traffic is a no-op. `proxy.db` is opened **read-only**, so the capture process
is never blocked.

Because the cache lives next to `proxy.db` on a mounted volume, it also survives
restarting or recreating the container — only genuinely new calls are ever parsed. Point
`USAGE_DB_PATH` at a container-local path (or run with no mount) and you trade that away:
the cache is then rebuilt from scratch on every start.

The container starts the refresher in the background before Datasette, so the UI is
available immediately and an existing `proxy.db` backfills while you browse:

```bash
# manual refresh, or backfill an existing capture
python scripts/build_usage_cache.py --proxy-db ~/.config/vibepod/proxy/proxy.db \
  --usage-db ~/.config/vibepod/usage.db

# rebuild from scratch after changing the extraction logic
python scripts/build_usage_cache.py --full
```

Check freshness with the `usage_cache_state` query (`/-/queries/usage/usage_cache_state`),
which lists per-source watermarks, cached row counts, and the last update time. Two more
queries make the de-duplication auditable: `usage_duplicate_events` lists repeated
snapshots that were suppressed from the totals, and `agent_token_calls_per_connection`
shows how many model calls each captured connection carried.

The cache carries a schema version. Bumping it (as the `response_id` column did) makes the
next run drop and re-parse automatically, so no manual `--full` is needed after an upgrade. The
original body-scanning queries remain available under the `proxy` database as
`agent_token_usage_live`, `agent_token_totals_live` and `agent_token_coverage_live` for
verifying the cache against raw traffic.

How the numbers are produced:

- usage is read from response bodies (and from Codex `response.completed` websocket
  frames, whose HTTP twin is excluded by the `agent_token_usage` view so a call is never
  counted twice)
- streamed responses repeat cumulative usage in many events, so fields are merged with
  `max`, not `sum`
- supported field shapes: `input_tokens`/`output_tokens` (Anthropic, OpenAI Responses),
  `prompt_tokens`/`completion_tokens` (OpenAI-compatible APIs), and
  `usageMetadata.promptTokenCount`/`candidatesTokenCount` (Google), plus their cached
  and reasoning sub-fields
- a call whose usage cannot be parsed is **excluded** from the totals and counted in
  `unparsed_calls` instead, so gaps are visible rather than silently reducing totals
- the model is read from the response payload (`$.response.model`, `$.model`) and only
  falls back to the request body: a websocket upgrade carries no model, and one
  connection can serve several
- repeated usage snapshots for the same `response.id` are collapsed to a single row (the
  one reporting the most tokens). Counting the same snapshot twice is the most common
  real-world inflation bug in this space — ccusage's Codex parser matched final session
  totals in only 131 of 732 sessions until it deduplicated ([issue #884](https://github.com/ccusage/ccusage/issues/884))
- distinct `response.id` values on one connection are separate model calls and are all
  summed, because per-response usage is a snapshot for that call rather than a running
  total (matching OpenAI's per-Response accounting in the
  [Realtime cost guide](https://developers.openai.com/api/docs/guides/realtime-costs))

Reading usage off the wire also avoids a whole bug class that affects tools parsing Codex
session logs: forked sessions and subagent threads replay their parent's usage history
into new files, which has produced up to 91x inflation
([issue #950](https://github.com/ccusage/ccusage/issues/950)). Each call crosses the proxy
exactly once.

If a host shows many `unparsed_calls`, that provider either does not report usage on the
captured response or uses a field shape that is not mapped yet. Bodies are stored as sent
by the server, so gzip, Zstandard, and Brotli responses are all decoded before parsing.

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

> Superseded by the All Agents Token Usage dashboard, which covers Codex alongside every other agent. Kept for now and listed last on the dashboards index.

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

## Agent Sessions Dashboard

Open `http://localhost:8001/-/dashboards/agent-sessions` to view session and message usage by agent over time. It is built on the `logs` database (`sessions` and `messages` tables).

It includes:

- total sessions and user messages (summary cards)
- average sessions per day and messages per session
- average session duration (from `started_at`/`ended_at`)
- sessions-by-agent breakdown chart
- top workspaces by session and message volume
- sessions-over-time trend (multi-series per agent, daily or hourly buckets)
- sessions by hour-of-day distribution (work-habits view)
- recent sessions drill-down table (per-session message counts)

Available dashboard filters:

- time range (`24h`, `7d`, `30d`, `all`; default `7d`)
- trend bucket (`auto`, `hour`, `day`; `auto` uses hour for `24h` and day otherwise)
- agent (populated from `sessions.agent`)
- workspace (populated from `sessions.workspace`)
- session table row limit (20–500)

Agent identity comes from `sessions.agent`.

## Agent Proxy Requests Dashboard

Open `http://localhost:8001/-/dashboards/agent-proxy-requests` to view proxy HTTP request volume by agent over time. It is built on the `proxy` database (`http_requests` table).

It includes:

- total proxy requests (summary card)
- requests-by-agent breakdown chart
- requests-over-time trend (multi-series per agent, daily or hourly buckets)
- recent proxy requests drill-down table

Available dashboard filters:

- time range (`24h`, `7d`, `30d`, `all`; default `7d`)
- trend bucket (`auto`, `hour`, `day`; `auto` uses hour for `24h` and day otherwise)
- agent (populated from `http_requests.source_container_name` via the `vibepod-<agent>-...` parsing used by the other proxy dashboards)
- request table row limit (20–500)

Agent identity is derived from `http_requests.source_container_name` (e.g. `vibepod-codex-...` -> `codex`), consistent with the HTTP Requests and token dashboards. This dashboard does not support a workspace filter because `http_requests` has no workspace column.

> **Why two dashboards?** Each dashboard chart runs against a single database connection. Although Datasette is started with `--crossdb`, cross-database joins (e.g. `logs.sessions` joined to `proxy.http_requests` in one query) are not available to chart queries in this setup, so the session-side and proxy-side views are split into separate dashboards that each stay within one database.

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
