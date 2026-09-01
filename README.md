# VibePod Datasette

Datasette container for browsing both VibePod SQLite databases:

- `logs.db` for session/message history
- `proxy.db` for HTTP traffic captured by `vibepod-proxy`
- proxy requests include origin container name via `http_requests.source_container_name`
- all-agent token usage dashboard via `datasette-dashboards` at `/-/dashboards/agent-tokens`
- built-in HTTP observability dashboard via `datasette-dashboards` at `/-/dashboards/http-requests`
- agent sessions dashboard via `datasette-dashboards` at `/-/dashboards/agent-sessions`
- agent proxy requests dashboard via `datasette-dashboards` at `/-/dashboards/agent-proxy-requests`
- `usage.db` with per-call token usage materialized from `proxy.db` by `scripts/build_usage_cache.py`,
  attributed to the workspace and the proxy profile each call came from via `logs.db`
- token usage is priced from a bundled pricing dataset (`pricing/model_prices.json`), exposed
  alongside usage as cost cards/tables on the `agent-tokens` dashboard

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
- `PRICING_FILE_PATH` (model pricing JSON; defaults to the bundled `pricing/model_prices.json`)
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
- total cost and cost-by-agent chart (USD, calls priced from a confirmed rate only)
- estimated cost and estimated-cost-by-agent chart (USD, calls priced from a vague
  reference — a placeholder or a provider catch-all; see "How usage is priced" below)
- cost over time, confirmed vs estimated, at the selected bucket, plus a card comparing
  confirmed cost with the previous window of the same length (see "Cost over time" below)
- average, median and p95 cost per call (confirmed prices; see "Cost per call" below)
- top-cost-drivers table ranking every dimension — agent, workspace, profile, session, model,
  provider, host — with each one's share of spend
- calls-with-usage count
- tokens by agent and by provider, split into input vs output series (stacked bars)
- token trend over time (input vs output series)
- tokens by workspace, split into input vs output series, plus a count of active workspaces
- tokens by profile, split into input vs output series, plus a count of active profiles
- tokens by agent and model table, including cached, cache-write, and cost columns
- tokens by workspace and agent table
- tokens and cost by profile and agent table
- tokens and cost by session table, plus a count of active sessions
- recent calls table with per-call token fields
- usage-coverage table: calls per host split into parsed vs unparsed
- pricing-coverage table: calls per provider/model split into priced vs unpriced
- unpriced token volume, and what it would have cost at this window's confirmed rate
- pricing-quality table: how each call was priced — exact rate, prefix match, provider
  catch-all, placeholder, or not at all — with the age of the rate that was applied
- cost-per-call table by provider and model, with the call counts each statistic rests on
- most-expensive-calls table, ranked by cost and carrying the request id
- outlier-calls table: calls costing at least 3x the median call of their own model
- rate-changes table: models billed at more than one rate inside the window

Available dashboard filters:

- time range (`1h`, `2h`, `4h`, `24h`, `7d`, `30d`, `3m`, `6m`, `1y`, `all`; default `24h`)
- trend bucket (`auto`, `5min`, `hour`, `day`, `week`, `month`; `auto` uses 5-minute slots
  up to `4h`, hourly for `24h`, and daily beyond that)
- agent (derived from `source_container_name`, e.g. `vibepod-tau-...` -> `tau`)
- workspace (the directory the agent ran in, resolved from `logs.db`; see below)
- profile (the proxy filter profile the call ran under; see below)
- provider (`anthropic`, `openai-codex`, `google`, `groq`, ... derived from the request host)
- model (as reported by the response, so a dated snapshot appears under its own name)
- cost driver (which dimension the top-drivers table ranks; `all` lists every one)
- host
- table rows (`10`, `25`, `50`, `100`, `250`; default `10`, which is also what a query run
  without the parameter uses)

### Cost over time

The cost trend charts spend per period at the selected trend bucket, so the same chart answers
daily, weekly, and monthly with the `day`, `week`, and `month` buckets — a week starts on its
Monday, a month on the 1st. Confirmed and estimated cost are separate stacked series, never one
total, for the reason in "How usage is priced" below.

It uses the same line mark as the token trend, and both trends bind their legend: click a
legend entry to isolate that series (the others fade out), click it again to bring them back. A bucket with calls but no cost on one series
reports zero, so the line dips through it instead of being drawn straight from the previous
point to the next; a bucket with no calls at all produces no point, exactly as on the token
trend. A period where calls happened but nothing could be priced reads as zero on a *cost*
chart — the pricing-coverage table is where unpriced calls show up.

Next to it, a card reports the **percentage change in confirmed cost** between two rolling
windows: the selected range ending now, and the window of the same length immediately before it.
At the default `24h` that is the last 24 hours against the 24 hours before those; at `7d`, the
last seven days against the seven before. These are rolling windows anchored on the current
time, not calendar days or weeks, so both sides are always the same length. Every other filter
(agent, workspace, profile, provider, model, host) applies to both windows, and estimated cost is
excluded from both — this card is about money with a confirmed price behind it. It reads blank
rather than showing a number in the two cases where a percentage would be meaningless: the `all`
range, which has no preceding window, and a previous window with no confirmed spend, which has
nothing to divide by.

`cost_over_time` (`/-/queries/usage/cost_over_time`) returns the same per-period figures as a
table — calls, confirmed cost, estimated cost, and tokens — for reconciling the chart against
the underlying rows.

### Top cost drivers

One table ranks every dimension the cache knows — agent, workspace, profile, session, model,
provider and host — with each row's share of the window's spend. The `driver` filter narrows it
to a single dimension; `all` lists them together. Ranking is *within* a dimension, not across:
every dimension covers the same spend, so the shares add to 100% per dimension rather than down
the table.

Its `cost_usd` column is confirmed and estimated cost **added together**, unlike the rest of the
dashboard, which keeps them apart because an estimate is priced from a placeholder or a
provider catch-all. This table is for finding what is expensive, where one number ranks better
than two; when the split matters, `top_cost_drivers`
(`/-/queries/usage/top_cost_drivers`) reports the same rows with `confirmed_cost_usd` and
`estimated_cost_usd` as separate columns.

Drilling into a driver is the dashboard's own filters — pick the agent, workspace, profile or
model and every panel follows, down to the most-expensive-calls table and its `request_id`s.

Workspaces are ranked by **basename** here (`api`, not `/home/you/clients/acme/api`). This is the
panel someone glances at over a shoulder, and a directory layout can say more about a business
than the spend does; the full paths stay in `workspace_token_totals`.

### Cost per call

Three cards report the distribution of what a single call costs — average, median and p95 — over
calls priced from a **confirmed** rate. Estimated ones are excluded on purpose: a placeholder or
a catch-all rate would move a percentile without anyone having verified the number behind it.

SQLite has no percentile function, so the rows are ranked and the statistic is read off the
ranking: the median averages the one or two middle ranks (so an even count works), and p95 is the
nearest-rank definition with the ceiling written as integer arithmetic, because `ceil()` is only
present when SQLite was compiled with its math functions.

`cost_per_call_by_model` repeats all of it per provider and model, next to the call counts each
statistic rests on: total calls, how many had a confirmed price, how many were estimated, and how
many could not be priced at all. A segment priced from two of its two hundred calls is then
visibly that, instead of showing a confident-looking average. Segments with no confirmed price
still appear, with empty statistics, rather than dropping out of the table.
`cost_per_request` (`/-/queries/usage/cost_per_request`) is the same figures as a query.

The most-expensive-calls table ranks individual calls and carries each one's `request_id`, which
is the key to look the call up in `proxy.db` where its request and response bodies live.

Two further tables answer questions the totals cannot. **Outlier calls** are judged against the
median call of their *own* model rather than a global one — $5 is unremarkable for Opus and
absurd for Haiku — and only for models with at least five calls in the window, so nothing is
flagged on the evidence of two; each row carries that median, the ratio, and the `request_id`.
**Models billed at more than one rate** in the window surface separately, because a rate change
moves cost while usage stands still, and would otherwise read as a usage change. That table is
the one place estimated rows are kept: a placeholder replaced by a confirmed rate is itself a
pricing change, and its `is_estimated` column keeps a change of rate distinct from a change of
confidence.

### How tokens are attributed to a workspace

Token counts come from `proxy.db`, but the directory an agent ran in only exists in
`logs.db` as `sessions.workspace` — `http_requests` has no workspace column. Chart queries
cannot join across databases (see the note at the end of this README), so
`scripts/build_usage_cache.py` resolves the workspace while building the cache and stores
it on the row. `logs.db` is opened **read-only**, like `proxy.db`; pass it with `--logs-db`
(default `LOGS_DB_PATH`, already wired up in the container).

The link between the two databases is the container:

- **Primary key is the container id.** `sessions.container_id` and
  `http_requests.source_container_id` are compared on their first 12 characters, because
  Docker's short id is exactly the prefix of the full one and the two sides need not store
  the same length. Container ids are never reused, so this needs no time logic at all.
- **The container name is the fallback**, used when the proxy captured no id or the id
  matches no session (a pruned `logs.db`, or a session predating the id column). The newest
  session that started before the call wins, which covers containers pinned to a fixed name
  via `vp run --name`. `ended_at` is deliberately ignored: a call made after a session ended
  still comes from that container, and a container's workspace is bind-mounted at creation
  and cannot change while it lives.

The full path is stored, and the basename is used as the chart label — hovering a bar shows
the whole path. Two checkouts sharing a basename remain separate rows.

A call that resolves to no session is **not dropped**: it is counted under `unknown`, so the
workspace totals still add up to the overall total. Because a call can cross the proxy before
its session row lands in `logs.db`, resolution is retried on every refresh; a row that stays
unresolved for 15 minutes after it was ingested is decided `unknown` once and not rescanned
again. Run with `--full` to re-resolve everything after fixing a missing or misplaced
`logs.db`.

Every refresh logs its outcome (`workspace resolution: by_id=…, by_name=…, unknown=…,
pending=…`), and two queries make the attribution auditable:
`workspace_token_totals` (`/-/queries/usage/workspace_token_totals`) for the per-workspace
sums, and `workspace_resolution` for the per-container breakdown of resolved and still
pending rows. A dashboard dominated by `unknown` means the container link is not working —
which is visible as a number rather than as silently misattributed tokens.

### How calls are attributed to a profile

A profile is the proxy filter profile a container runs under (`vibepod profile list`), which
decides which hosts that agent may reach. Attributing tokens to it answers what a given
policy actually costs, and pairs with the filter panels on the HTTP Requests dashboard that
show what it blocked.

Unlike the workspace, the profile has two possible sources, and they are tried in this order:

- **The request row wins.** The proxy writes `http_requests.profile` when it applies the
  policy, so that value is the profile the call was actually filtered under, and the cache
  stores it verbatim at ingest.
- **The session is the fallback**, resolved from `sessions.profile` through the same
  container match the workspace uses (id first, name second). It covers calls the proxy
  captured without a profile.

Everything else mirrors the workspace contract: an unresolved profile is retried on every
refresh, decided `unknown` once the 15-minute grace window closes, and shown as `unknown`
rather than as a blank label, so per-profile totals still add up to the overall total. The
refresh logs `profile resolution: by_session=…, unknown=…, pending=…` next to the workspace
line, and `profile_token_totals` (`/-/queries/usage/profile_token_totals`) and
`profile_resolution` make the attribution auditable the same way.

Both source columns are recent, so the cache tolerates databases written without them: a
`proxy.db` whose `http_requests` has no `profile` column still ingests (those rows fall back
to the session), and a `logs.db` whose `sessions` has none simply resolves no profile. The
profile panels on the **HTTP Requests** and **Agent Sessions** dashboards read those source
columns directly, so they need a proxy and a CLI new enough to write them; the token
dashboards do not, because they read the cache.

### How calls are attributed to a session

The session is the run an agent was doing when it made the call — one `vp run`, one row in
`logs.db`'s `sessions` table. It answers "what did *this* run cost", which is the question
behind an unexpected bill: a workspace total says which project, a session total says which run.

`proxy.db` has no session column, so the session id is resolved exactly like the workspace, from
the same `session_windows` snapshot and the same container match. One extra rule applies, because
a container id names a *container*, not a session, and a container can host more than one (re-
attaching opens another): among the sessions of the matched container, the call is attributed to
the one that started last on or before the call's own timestamp. A call older than every session
of that container keeps the earliest one rather than going unattributed.

A call whose container has no session row at all is counted under `unknown`, so per-session
totals still add up to the overall total, and the refresh logs `session resolution: resolved=…,
unknown=…, pending=…` beside the workspace and profile lines. `session_token_totals`
(`/-/queries/usage/session_token_totals`) reports calls, tokens, and confirmed/estimated cost per
session with its agent, workspace and profile; `session_resolution` breaks resolved and still
pending rows down per container.

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

### How usage is priced

`pricing/model_prices.json` is a bundled, curated table of $-per-million-token rates per
provider/model (input, output, cached input, cache write). `scripts/build_usage_cache.py`
loads it into `usage.db`'s `model_pricing` table on every refresh, fully replacing the table
each time: it is small, static reference data, not derived from captured traffic, so an entry
removed from the file disappears from the table too. A missing or unreadable pricing file
leaves the previous rows in place rather than wiping cost data over a transient error. Point
`PRICING_FILE_PATH` at a different JSON file to override the bundled dataset.

A call is priced by matching `model_pricing` rows for its provider and picking the one whose
`model` is the **longest prefix** of the captured model string. An exact match is always the
longest possible prefix, so this one rule covers both exact entries and dated snapshots (e.g.
`claude-opus-4-5-20260101` prices as `claude-opus-4-5`) without separate logic. `reasoning_tokens`
are already included in `output_tokens` by every provider's own accounting, so there is no
separate reasoning price.

Provider prices change over time, so `(provider, model)` can have several `model_pricing` rows
with different `effective_from` dates rather than one fixed price. A call is priced at whichever
rate was in effect on its own `timestamp` — the latest `effective_from` on or before that
timestamp — never at today's rate, so historical cost totals stay correct after a price update.
There is no explicit "effective to": a price's validity window implicitly ends at the next later
`effective_from` row for that same `(provider, model)`, so adding a new price point never
requires editing the old row. `pricing/model_prices.json` includes a real example of this
(`openai`/`gpt-4o`'s August 2024 price cut) alongside single-point entries for models with only
one known price so far.

`is_estimated` says one thing: how solid the price reference is. A rate published for that exact
model is never estimated, whichever product served the call — Codex bills at OpenAI's rates, and
a Copilot call is priced at the published rate of the model it actually ran (OpenAI, Anthropic or
Google). `is_estimated = 1` is for rates with a vague reference, and there are two kinds:
placeholders for models newer than this dataset was last verified against (`PLACEHOLDER,
unverified` in `price_source`), and per-provider catch-alls. Every provider gets a catch-all row
with `model = ''`: the empty string is a prefix of every model string, so it wins only when no
more specific entry exists (an unlisted Codex model still prices at the `gpt-5`-family rate, but
as a guess about which model ran, hence estimated). Estimated cost is never summed into the same
number as confirmed cost (see below), so a guessed rate can't inflate the dashboard's
"actual dollars" figure.

The `agent_token_cost` view (usage.db) is `agent_token_usage` plus this price match and a
computed `cost_usd`/`is_estimated`. Both `has_price` and `cost_usd` are only set when a call
also has `has_usage = 1`: a call with no parsed token counts has nothing to price, regardless of
whether a pricing entry exists for its model. On the `agent-tokens` dashboard, `total_cost` /
`cost_by_agent` sum only calls priced from a confirmed rate (`has_price = 1 AND
is_estimated = 0`); `total_estimated_value` / `estimated_value_by_agent` sum only the calls
priced from a vague reference (`is_estimated = 1`) as a separate figure. The
`tokens_by_agent_model` table and the
`pricing_coverage` chart/query (also at `/-/queries/usage/pricing_coverage`) both report
`real_cost_usd` and `estimated_cost_usd` as separate columns for the same reason, alongside
`priced_calls`/`unpriced_calls` so calls with usage but no matching price stay visible instead of
silently under-reporting. The current pricing table itself is queryable at
`/-/queries/usage/model_pricing_table`.

### Pricing coverage and quality

`pricing_quality` (chart, and `/-/queries/usage/pricing_quality`) says how every call got its
price, which is the difference between "this cost $0" and "we could not price this":

- **exact rate** — a pricing entry for that exact model string.
- **prefix match** — a dated snapshot priced off its undated entry, e.g.
  `claude-opus-4-5-20260101` at the `claude-opus-4-5` rate. Correct by design, but worth seeing.
- **provider catch-all** — the `model = ''` row, applied because nothing more specific matched.
- **placeholder rate** — a rate nobody has confirmed for that model.
- **unpriced** — no entry matched at all, so the call has no cost and is missing from every
  cost total.

Each row carries the `price_effective_from` it used and how old that rate was when the call was
made. There is deliberately no "stale" verdict: an old rate is only a problem if the real price
moved, which the dataset cannot know, so the age is reported and the reader decides.

Two cards size the gap: the token volume nothing could price, and what that volume **would**
have cost at the average confirmed rate of the same window. The second is an extrapolation and
its title says so — it answers "how much money is this coverage gap hiding", not "what was
spent". With no confirmed calls in the window it is blank rather than zero.

`pricing/model_prices.json` is manually maintained; update it directly (add a new row with a
later `effective_from` for a price change, rather than editing the old one) and the next refresh
picks it up, no rebuild step required. Some entries are placeholders for models newer than this
dataset was last verified against (marked `PLACEHOLDER, unverified` in `price_source`) — check
those against the provider's current pricing page before trusting them.

## HTTP Requests Dashboard

Open `http://localhost:8001/-/dashboards/http-requests` to view an aggregated dashboard for proxied HTTP traffic.
The dashboards index is available at `http://localhost:8001/-/dashboards`.

It includes:

- total request volume and error-rate summary cards
- status code distribution
- top hosts by traffic/errors/latency
- request/error trend chart (hourly or daily buckets)
- filter visibility: the mode last seen, how many requests were blocked, blocked hosts
  (allow-list candidates) and hosts that passed with the filter active (deny-list candidates)
- filter decisions by profile: blocked vs passed per proxy filter profile, so a block can be
  traced back to the policy that caused it
- filterable/sortable recent-request table

Available dashboard filters/sorting:

- time range (`1h`, `2h`, `4h`, `24h`, `7d`, `30d`, `all`)
- host substring match
- method filter
- agent filter (derived from `source_container_name`, e.g. `vibepod-codex-...` -> `codex`)
- status-class filter (`2xx`, `3xx`, `4xx`, `5xx`, `error`)
- request table sort (time/status/duration/error-priority)
- host ranking sort (volume/error-count/latency)

The profile columns on those panels come from `http_requests.profile`, written by the proxy
when it applies a policy; against a proxy that does not record it yet, only those panels
report an error, the rest of the dashboard is unaffected.

If the dashboard reports that `http_requests` is missing, start VibePod traffic capture first (the proxy DB schema is created by `vibepod-proxy` once traffic is recorded).

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
- recent sessions drill-down table (per-session message counts and the profile the session
  ran under, from `sessions.profile`)

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

Codex reports token usage on a websocket frame rather than in the HTTP response, which is what
`scripts/build_usage_cache.py` reads. Use these proxy canned queries to inspect that payload
structure and validate the token calculations against it:

- `codex_ws_recent_messages`
- `codex_ws_message_type_counts`
- `codex_ws_token_field_coverage`
- `codex_ws_usage_event_duplicates`
- `codex_ws_tokens_vs_http_by_request`

Open from Datasette under the `proxy` database query list, or directly via paths like:

- `/-/queries/proxy/codex_ws_token_field_coverage`
- `/-/queries/proxy/codex_ws_tokens_vs_http_by_request`
