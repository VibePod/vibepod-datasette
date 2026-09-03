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
- token usage is priced from the shared [`pydantic/genai-prices`](https://github.com/pydantic/genai-prices)
  dataset, exposed alongside usage as cost cards/tables on the `agent-tokens` dashboard

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
- total cost and cost-by-agent chart (USD, calls priced from a confirmed rate only)
- estimated cost and estimated-cost-by-agent chart (USD, calls whose provider had to be
  inferred from the model name; see "How usage is priced" below)
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
- pricing-quality table: how each call was priced — exact rate, alias match, inferred
  provider, or not at all — with the age of the rate that was applied
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
dashboard, which keeps them apart because an estimate is priced from an inferred provider.
This table is for finding what is expensive, where one number ranks better
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
calls priced from a **confirmed** rate. Estimated ones are excluded on purpose: a rate reached by
guessing which provider billed the call would move a percentile on an assumption.

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
moves cost while usage stands still, and would otherwise read as a usage change. Rows are grouped
by the rate that was actually applied rather than by a published start date, so both a dated price
change (OpenAI cut `o3` from $10 to $2 per 1M input tokens in June 2025) and an undated one (an
off-peak window like DeepSeek's) show up. That table is the one place estimated rows are kept, and
its `is_estimated` column keeps a change of rate distinct from a change of confidence.

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

Rates come from [`pydantic/genai-prices`](https://github.com/pydantic/genai-prices), a shared,
community-maintained dataset of published provider pricing. This repository holds no rates of its
own: `scripts/model_pricing.py` hands the package a call's provider, model and token counts and
stores what comes back. That dataset covers far more models than a table kept by hand here ever
did, and it carries the awkward parts of real pricing — historic rates, dated price changes,
off-peak windows, and context tiers where a long prompt bills at a higher rate.

Pricing runs **once per call, at ingest**, and the result is stored on the `token_usage` row.
Matching a model string against the dataset means regexes, aliases and date constraints, which is
Python work rather than SQL, and doing it per render is exactly what the usage cache exists to
avoid. `agent_token_cost` (usage.db) is therefore a plain projection of `agent_token_usage` plus
the stored price columns, not a search.

Three things decide what a call costs:

- **Which provider billed it.** The cache labels a call by the host it went to, which names a
  product, not always a billing provider. `anthropic`, `openai`, `google`, `groq`, `mistral`,
  `deepseek`, `xai` and `openrouter` map straight through, and so does `openai-codex`: Codex is
  OpenAI's own product and bills at OpenAI's published rates, so `gpt-5-codex` prices at the
  `gpt-5` rate as confirmed spend. A host that fronts several vendors (`github-copilot`) never
  says who billed the call, so the provider is identified from the model string instead — see
  `is_estimated` below.
- **Which model ran.** The dataset matches aliases and dated snapshots, so
  `claude-opus-4-5-20260101` prices as `claude-opus-4-5`. A model string nothing matches leaves
  the call unpriced rather than guessed at.
- **When it ran.** Rates are resolved against the call's own `timestamp`, never today's, so
  historical cost totals stay correct after a provider changes a price. A call older than every
  rate the dataset knows for that model is priced at the earliest one rather than dropped.

Cache tokens need care, because providers disagree about what "input tokens" means: Anthropic
reports `input_tokens` **excluding** cache reads and writes, while OpenAI and Google count cached
tokens **inside** their input total. The field names cannot tell the two apart (Anthropic and
OpenAI's Responses API both call it `input_tokens`), so the extractor records which convention a
body used and the cache counts are added back only when they are not already included. Each part
is then billed at its own rate. This also fixes a double charge: cached OpenAI and Google tokens
used to be billed once at the input rate and again at the cache rate.

`reasoning_tokens` are not passed to the pricer. Providers disagree about whether they already sit
inside the output count, and only a couple of models price them separately, so feeding in an
ambiguous number would move a cost figure on a guess.

`is_estimated` says one thing: whether the provider was **known** or **inferred**. A call to a
host that names its billing provider is confirmed cost. A call whose provider had to be read off
the model string is priced from a published rate too, but which provider that rate belongs to is a
guess, so it is reported separately. Estimated cost is never summed into the same number as
confirmed cost, so a guess can't inflate the dashboard's "actual dollars" figure.

Both `has_price` and `cost_usd` are only set when a call also has `has_usage = 1`: a call with no
parsed token counts has nothing to price, whatever its model. On the `agent-tokens` dashboard,
`total_cost` / `cost_by_agent` sum only calls priced from a confirmed rate (`has_price = 1 AND
is_estimated = 0`); `total_estimated_value` / `estimated_value_by_agent` sum only the inferred
ones (`is_estimated = 1`) as a separate figure. The `tokens_by_agent_model` table and the
`pricing_coverage` chart/query (also at `/-/queries/usage/pricing_coverage`) both report
`real_cost_usd` and `estimated_cost_usd` as separate columns for the same reason, alongside
`priced_calls`/`unpriced_calls` so calls with usage but no matching price stay visible instead of
silently under-reporting. The rates actually applied to captured calls are queryable at
`/-/queries/usage/model_pricing_table`, each row carrying the `price_source` it came from.

Because costs are stored rather than recomputed, each row also records the `genai-prices` release
that priced it. Upgrading the package (rebuild the image, or `pip install -U genai-prices`) makes
the next refresh re-price the rows that release did not produce, and log
`pricing: repriced N rows`; a cache that is already current does no work. Prices are not fetched
over the network — they ship with the installed package version, so the container stays offline.
Without `genai-prices` installed, usage still ingests and totals normally and every call is simply
left unpriced.

### Pricing coverage and quality

`pricing_quality` (chart, and `/-/queries/usage/pricing_quality`) says how every call got its
price, which is the difference between "this cost $0" and "we could not price this":

- **exact rate** — the model string matched a priced model directly.
- **alias match** — matched under another name, e.g. `claude-opus-4-5-20260101` at the
  `claude-opus-4-5` rate, or `gpt-5-codex` at the `gpt-5` rate. Correct by design, but worth
  seeing.
- **provider inferred** — the host serves several vendors, so the provider was identified from
  the model string. Priced, but not counted as confirmed spend.
- **unpriced** — nothing matched, so the call has no cost and is missing from every cost total.

Each row carries the `price_effective_from` of the rate it applied and how old that rate was when
the call was made. Most published rates carry no start date, and those read as blank rather than
inventing one. There is deliberately no "stale" verdict: an old rate is only a problem if the real
price moved, which the dataset cannot know, so the age is reported and the reader decides.

Two cards size the gap: the token volume nothing could price, and what that volume **would**
have cost at the average confirmed rate of the same window. The second is an extrapolation and
its title says so — it answers "how much money is this coverage gap hiding", not "what was
spent". With no confirmed calls in the window it is blank rather than zero.

A missing or wrong rate is worth fixing upstream, where every project using the dataset gets it:
the provider files live in [`prices/providers`](https://github.com/pydantic/genai-prices/tree/main/prices)
and take pull requests. Note the project's own warning that these prices are a best-effort
estimate rather than a bill.

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
