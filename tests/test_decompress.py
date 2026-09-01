import gzip
import importlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


if importlib.util.find_spec("datasette") is None:
    fake_datasette = types.ModuleType("datasette")
    fake_datasette.hookimpl = lambda fn: fn
    with mock.patch.dict(sys.modules, {"datasette": fake_datasette}):
        decompress = importlib.import_module("plugins.decompress")
else:
    decompress = importlib.import_module("plugins.decompress")

sys.path.insert(0, str(REPO_ROOT / "scripts"))
build_usage_cache = importlib.import_module("build_usage_cache")


class DecompressCacheTests(unittest.TestCase):
    def setUp(self):
        decompress._decode_cache.clear()

    def test_cache_uses_full_body_bytes(self):
        body1 = b'{"model":"same","value":"first"}'
        body2 = b'{"model":"same","value":"other"}'

        self.assertEqual(len(body1), len(body2))
        self.assertEqual(body1[:16], body2[:16])

        text1 = decompress._ungzip(body1)
        text2 = decompress._ungzip(body2)

        self.assertEqual(text1, body1.decode("utf-8"))
        self.assertEqual(text2, body2.decode("utf-8"))
        self.assertNotEqual(text1, text2)


def _usage(body, host):
    return json.loads(decompress._extract_usage(body, host))


class UsageExtractionTests(unittest.TestCase):
    def setUp(self):
        decompress._decode_cache.clear()
        decompress._usage_cache.clear()

    def test_anthropic_sse_uses_cumulative_output_not_sum(self):
        body = gzip.compress(
            "\n".join(
                [
                    "event: message_start",
                    'data: {"type":"message_start","message":{"usage":'
                    '{"input_tokens":1200,"cache_read_input_tokens":900,'
                    '"cache_creation_input_tokens":100}}}',
                    "event: message_delta",
                    'data: {"type":"message_delta","usage":{"output_tokens":50}}',
                    "event: message_delta",
                    'data: {"type":"message_delta","usage":{"output_tokens":300}}',
                ],
            ).encode(),
        )

        usage = _usage(body, "api.anthropic.com")

        self.assertEqual(usage["provider"], "anthropic")
        self.assertEqual(usage["input"], 1200)
        # 50 + 300 would double count the cumulative delta.
        self.assertEqual(usage["output"], 300)
        self.assertEqual(usage["cached"], 900)
        self.assertEqual(usage["cache_write"], 100)
        self.assertEqual(usage["found"], 1)

    def test_openai_compatible_json_body(self):
        body = json.dumps(
            {
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 10},
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
            },
        ).encode()

        usage = _usage(body, "api.groq.com")

        self.assertEqual(usage["provider"], "groq")
        self.assertEqual((usage["input"], usage["output"]), (80, 20))
        self.assertEqual((usage["cached"], usage["reasoning"]), (10, 4))

    def test_google_usage_metadata_is_deduplicated(self):
        body = "\n".join(
            [
                'data: {"usageMetadata":{"promptTokenCount":500,"candidatesTokenCount":10}}',
                'data: {"usageMetadata":{"promptTokenCount":500,"candidatesTokenCount":120,'
                '"thoughtsTokenCount":40,"cachedContentTokenCount":200}}',
            ],
        ).encode()

        usage = _usage(body, "cloudcode-pa.googleapis.com")

        self.assertEqual(usage["provider"], "google")
        self.assertEqual((usage["input"], usage["output"]), (500, 120))
        self.assertEqual((usage["cached"], usage["reasoning"]), (200, 40))

    def test_codex_websocket_response_completed(self):
        body = json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 700,
                        "output_tokens": 90,
                        "input_tokens_details": {"cached_tokens": 600},
                        "output_tokens_details": {"reasoning_tokens": 30},
                    },
                },
            },
        ).encode()

        usage = _usage(body, "chatgpt.com")

        self.assertEqual(usage["provider"], "openai-codex")
        self.assertEqual((usage["input"], usage["output"]), (700, 90))
        self.assertEqual((usage["cached"], usage["reasoning"]), (600, 30))

    def test_body_without_usage_is_reported_as_not_found(self):
        usage = _usage(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}',
            "api.githubcopilot.com",
        )

        self.assertEqual(usage["found"], 0)
        self.assertEqual(usage["input"], 0)
        self.assertEqual(usage["provider"], "github-copilot")

    def test_unknown_host_keeps_host_as_provider_label(self):
        self.assertEqual(
            _usage(b"{}", "llm.internal.example")["provider"],
            "llm.internal.example",
        )
        self.assertEqual(_usage(b"{}", None)["provider"], "unknown")

    def test_null_body_is_handled(self):
        self.assertEqual(_usage(None, "api.anthropic.com")["found"], 0)

    def test_cache_distinguishes_same_body_for_different_hosts(self):
        body = json.dumps({"usage": {"prompt_tokens": 5}}).encode()

        first = _usage(body, "api.openai.com")
        second = _usage(body, "api.mistral.ai")

        self.assertEqual(first["provider"], "openai")
        self.assertEqual(second["provider"], "mistral")

    def test_plain_non_json_bodies_survive_the_brotli_probe(self):
        self.assertEqual(
            decompress._ungzip(b"<html>not an api call</html>"),
            "<html>not an api call</html>",
        )

    @unittest.skipIf(decompress.brotli is None, "brotli not installed")
    def test_brotli_bodies_decode(self):
        payload = json.dumps(
            {"usage": {"input_tokens": 11, "output_tokens": 3}},
        ).encode()
        compressed = decompress.brotli.compress(payload)

        usage = _usage(compressed, "api.anthropic.com")

        self.assertEqual((usage["input"], usage["output"]), (11, 3))


class AgentTokenSqlTests(unittest.TestCase):
    """Build the usage cache from a synthetic proxy.db, then run the dashboard SQL.

    The dashboards read the cache, so the fixture exercises the same path the
    container does: proxy.db -> build_usage_cache -> usage.db -> chart queries.
    """

    PARAMS = {
        "time_range": "all",
        "time_bucket": "auto",
        "agent": "all",
        "provider": "all",
        "host": "all",
        "workspace": "all",
        "profile": "all",
        "model": "all",
        "limit": 200,
        "request_limit": 100,
    }

    SCHEMA = """
    CREATE TABLE http_requests (id TEXT PRIMARY KEY, timestamp TEXT, method TEXT,
        source_container_id TEXT, source_container_name TEXT, profile TEXT, scheme TEXT,
        host TEXT, port INTEGER, path TEXT, query TEXT, url TEXT, headers TEXT, body BLOB,
        client_ip TEXT, client_port INTEGER, server_ip TEXT, server_port INTEGER);
    CREATE TABLE http_responses (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, status_code INTEGER, headers TEXT, body BLOB, bytes_in INTEGER,
        bytes_out INTEGER, duration_ms REAL);
    CREATE TABLE websocket_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, direction TEXT, type TEXT, content BLOB);
    """

    LOGS_SCHEMA = """
    -- Mirrors session_logger.py: the session fixture the resolver joins against.
    CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, image TEXT,
        workspace TEXT, container_id TEXT, container_name TEXT, started_at TEXT,
        ended_at TEXT, exit_reason TEXT, vibepod_version TEXT,
        profile TEXT NOT NULL DEFAULT 'default');
    """

    def _session(
        self,
        container_id,
        container_name,
        workspace,
        started_at,
        session_id=None,
        profile="default",
    ):
        self.logs.execute(
            "INSERT INTO sessions (id, agent, image, workspace, container_id, "
            "container_name, started_at, ended_at, exit_reason, vibepod_version, profile) "
            "VALUES (?, 'agent', 'image', ?, ?, ?, ?, NULL, NULL, '', ?)",
            (
                session_id or f"sess-{container_id}",
                workspace,
                container_id,
                container_name,
                started_at,
                profile,
            ),
        )
        self.logs.commit()

    def setUp(self):
        decompress._decode_cache.clear()
        decompress._usage_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy_path = Path(self.tmp.name) / "proxy.db"
        self.usage_path = Path(self.tmp.name) / "usage.db"
        self.logs_path = Path(self.tmp.name) / "logs.db"
        self.source = sqlite3.connect(self.proxy_path)
        self.addCleanup(self.source.close)
        self.source.executescript(self.SCHEMA)
        self.logs = sqlite3.connect(self.logs_path)
        self.addCleanup(self.logs.close)
        self.logs.executescript(self.LOGS_SCHEMA)
        self.metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        self._seed()
        self.refresh()

    def refresh(self):
        counts = build_usage_cache.build(self.proxy_path, self.usage_path, self.logs_path)
        self.conn = sqlite3.connect(self.usage_path)
        self.addCleanup(self.conn.close)
        return counts

    def _request(
        self,
        rid,
        host,
        path,
        container,
        model,
        container_id=None,
        ts="2026-07-26T10:00:00+00:00",
        profile=None,
    ):
        self.source.execute(
            "INSERT INTO http_requests (id, timestamp, method, source_container_name, "
            "source_container_id, profile, host, path, body) "
            "VALUES (?, ?, 'POST', ?, ?, ?, ?, ?, ?)",
            (
                rid,
                ts,
                container,
                container_id,
                profile,
                host,
                path,
                json.dumps({"model": model}).encode(),
            ),
        )

    def _response(self, rid, body, ts="2026-07-26T10:00:01+00:00"):
        self.source.execute(
            "INSERT INTO http_responses (request_id, timestamp, status_code, body) "
            "VALUES (?, ?, 200, ?)",
            (rid, ts, body),
        )

    def _seed(self):
        # Two seeds resolve to different workspaces; the copilot row has no
        # session and must land in the 'unknown' bucket instead of an empty label.
        self._session(
            "abc1id123456",
            "vibepod-claude-abc1",
            "/home/g/projects/alpha",
            "2026-07-25T10:00:00+00:00",
        )
        self._session(
            "abc2id123456",
            "vibepod-tau-abc2",
            "/home/g/projects/beta",
            "2026-07-25T10:00:00+00:00",
        )

        self._request(
            "r1",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-claude-abc1",
            "opus",
            container_id="abc1id123456",
        )
        self._response(
            "r1",
            gzip.compress(
                b'data: {"type":"message_start","message":{"usage":'
                b'{"input_tokens":1200,"cache_read_input_tokens":900,'
                b'"cache_creation_input_tokens":100}}}\n'
                b'data: {"type":"message_delta","usage":{"output_tokens":300}}',
            ),
        )

        self._request(
            "r2",
            "api.groq.com",
            "/openai/v1/chat/completions",
            "vibepod-tau-abc2",
            "l3",
            container_id="abc2id123456",
        )
        self._response(
            "r2",
            json.dumps(
                {"usage": {"prompt_tokens": 80, "completion_tokens": 20}},
            ).encode(),
        )

        # Copilot call without usage: must not silently count as zero tokens.
        self._request(
            "r3",
            "api.githubcopilot.com",
            "/chat/completions",
            "vibepod-copilot-abc3",
            "x",
        )
        self._response("r3", b'data: {"choices":[{"delta":{"content":"hi"}}]}')

        # Codex: usage arrives on the websocket, the HTTP row must be skipped.
        self._request(
            "r4",
            "chatgpt.com",
            "/backend-api/codex/responses",
            "vibepod-codex-abc4",
            "c",
            container_id="abc4id123456",
        )
        self._session(
            "abc4id123456",
            "vibepod-codex-abc4",
            "/home/g/projects/gamma",
            "2026-07-25T10:00:00+00:00",
        )
        self._response("r4", b'{"ok":true}')
        self.source.execute(
            "INSERT INTO websocket_messages (request_id, timestamp, direction, type, content) "
            "VALUES (?, ?, 'incoming', 'text', ?)",
            (
                "r4",
                "2026-07-26T10:00:02+00:00",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "usage": {"input_tokens": 700, "output_tokens": 90},
                        },
                    },
                ).encode(),
            ),
        )
        self.source.commit()

    def _workspace(self, agent=None):
        params = dict(self.PARAMS)
        if agent is not None:
            params["agent"] = agent
        sql = self.metadata["databases"]["usage"]["queries"]["workspace_token_totals"]["sql"]
        return {row[1]: row for row in self.conn.execute(sql, params).fetchall()}

    def _rows(self, sql):
        return self.conn.execute(sql, self.PARAMS).fetchall()

    def _stored_workspace(self, request_id):
        """The raw, un-normalized workspace of a row ('' while still pending)."""
        return self.conn.execute(
            "SELECT workspace FROM token_usage WHERE request_id = ?",
            (request_id,),
        ).fetchone()[0]

    def _stored_profile(self, request_id):
        """The raw, un-normalized profile of a row ('' while still pending)."""
        return self.conn.execute(
            "SELECT profile FROM token_usage WHERE request_id = ?",
            (request_id,),
        ).fetchone()[0]

    def test_totals_sum_every_agent_once(self):
        sql = self.metadata["databases"]["usage"]["queries"]["agent_token_totals"]["sql"]

        totals = {row[0]: row for row in self._rows(sql)}

        self.assertEqual(sorted(totals), ["claude", "codex", "tau"])
        self.assertEqual(totals["claude"][7], 1500)
        self.assertEqual(totals["tau"][7], 100)
        # One row only: the HTTP twin of the websocket call must not be counted.
        self.assertEqual(totals["codex"][1], 1)
        self.assertEqual(totals["codex"][7], 790)

    def test_workspace_totals_sum_across_all_workspaces(self):
        # With usage but without a session: the row must count toward 'unknown'
        # instead of silently dropping out of every bucket.
        self._request("w1", "api.groq.com", "/v1/chat", "vibepod-tau-nowhere", "l3")
        self._response(
            "w1",
            json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 1}}).encode(),
        )
        self.source.commit()
        self.refresh()

        workspaces = self._workspace()

        self.assertEqual(
            sorted(workspaces),
            ["alpha", "beta", "gamma", "unknown"],
        )
        # Nothing falls out of the totals: unknown buckets make gaps visible.
        total = sum(row[8] for row in workspaces.values())
        self.assertEqual(total, 1500 + 100 + 790 + 6)
        self.assertEqual(workspaces["alpha"][0], "/home/g/projects/alpha")
        self.assertEqual(workspaces["alpha"][8], 1500)  # claude seed resolved by id
        self.assertEqual(workspaces["beta"][8], 100)
        self.assertEqual(workspaces["gamma"][8], 790)
        self.assertEqual(workspaces["unknown"][8], 6)

    def test_profile_flows_from_http_requests_to_agent_token_usage(self):
        self._request(
            "p1",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-claude-work",
            "opus",
            profile="work",
        )
        self._response(
            "p1",
            json.dumps({"usage": {"input_tokens": 10, "output_tokens": 2}}).encode(),
        )
        self.source.commit()
        self.refresh()

        profile = self.conn.execute(
            "SELECT profile FROM agent_token_usage WHERE request_id = 'p1'",
        ).fetchone()[0]

        self.assertEqual(profile, "work")

    def test_profile_without_a_proxy_value_comes_from_the_session(self):
        # r1 was seeded without a profile (unmapped source IP, e.g.), so the
        # session of its container is what names the profile.
        profile = self.conn.execute(
            "SELECT profile FROM agent_token_usage WHERE request_id = 'r1'",
        ).fetchone()[0]

        self.assertEqual(profile, "default")

    def test_profile_with_no_source_at_all_reads_as_unknown(self):
        # The copilot seed has neither a proxy profile nor a session: the row
        # stays pending in the table but must never surface an empty label.
        raw = self._stored_profile("r3")
        shown = self.conn.execute(
            "SELECT profile FROM agent_token_usage WHERE request_id = 'r3'",
        ).fetchone()[0]

        self.assertEqual(raw, "")
        self.assertEqual(shown, "unknown")

    def test_coverage_lists_calls_without_usage(self):
        sql = self.metadata["databases"]["usage"]["queries"]["agent_token_coverage"]["sql"]

        coverage = {row[0]: row for row in self._rows(sql)}

        self.assertEqual(coverage["api.githubcopilot.com"][4], 1)
        self.assertEqual(coverage["api.githubcopilot.com"][3], 0)
        self.assertEqual(coverage["api.anthropic.com"][3], 1)

    def test_every_dashboard_chart_query_runs(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.conn.execute(chart["query"], self.PARAMS).fetchall()

        total = self.conn.execute(
            charts["total_tokens"]["query"],
            self.PARAMS,
        ).fetchone()[0]
        self.assertEqual(total, 1500 + 100 + 790)

    def test_agent_filter_narrows_totals(self):
        sql = self.metadata["databases"]["usage"]["queries"]["agent_token_totals"]["sql"]
        params = dict(self.PARAMS, agent="claude")

        rows = self.conn.execute(sql, params).fetchall()

        self.assertEqual([row[0] for row in rows], ["claude"])

    def test_filter_queries_run(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        agents = [row[0] for row in self.conn.execute(filters["agent"]["query"]).fetchall()]
        hosts = [row[0] for row in self.conn.execute(filters["host"]["query"]).fetchall()]

        self.assertEqual(agents[0], "all")
        self.assertIn("claude", agents)
        self.assertIn("api.anthropic.com", hosts)

    def test_token_metrics_use_the_tok_unit(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        for name, chart in charts.items():
            if chart["library"] != "metric":
                continue
            priced_in_dollars = {
                "total_cost",
                "total_estimated_value",
                "avg_cost_per_call",
                "median_cost_per_call",
                "p95_cost_per_call",
                "unpriced_impact",
            }
            if name in priced_in_dollars or name == "cost_change":
                continue
            counted = {
                "calls_with_usage": " calls",
                "active_workspaces": " workspaces",
                "active_profiles": " profiles",
                "active_sessions": " sessions",
            }
            with self.subTest(chart=name):
                self.assertEqual(chart["display"]["suffix"], counted.get(name, " tok"))

    def test_cost_metric_uses_a_dollar_prefix(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        for name in (
            "total_cost",
            "total_estimated_value",
            "avg_cost_per_call",
            "median_cost_per_call",
            "p95_cost_per_call",
            "unpriced_impact",
        ):
            with self.subTest(chart=name):
                self.assertEqual(charts[name]["display"]["prefix"], "$")

    def test_dashboard_cost_charts_sum_bundled_pricing(self):
        # claude-opus-4-5 is priced in the bundled pricing/model_prices.json at
        # $5/$25 per 1M input/output tokens; round-numbered tokens make the
        # expected cost easy to check end to end through the real pipeline.
        self._request(
            "r5",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-claude-abc1",
            "claude-opus-4-5",
            container_id="abc1id123456",
        )
        self._response(
            "r5",
            json.dumps(
                {"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}},
            ).encode(),
        )
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        total_cost = self.conn.execute(charts["total_cost"]["query"], self.PARAMS).fetchone()[0]
        by_agent = {
            row[0]: row[1]
            for row in self.conn.execute(charts["cost_by_agent"]["query"], self.PARAMS).fetchall()
        }

        # The seeded codex call (r4) ran model "c", which matches no pricing
        # entry and falls back to the openai-codex catch-all: that is a guess
        # about which model ran, so it is estimated, never confirmed cost.
        self.assertAlmostEqual(total_cost, 5.0 + 25.0)
        self.assertAlmostEqual(by_agent["claude"], 5.0 + 25.0)
        self.assertNotIn("codex", by_agent)

    def test_confirmed_cost_covers_codex_and_copilot_models_by_name(self):
        # A published rate for the exact model is confirmed cost whichever
        # product served the call: Codex bills at OpenAI's rates, and a
        # Copilot call is priced at the rate of the model it actually ran.
        for rid, host, container, model in (
            ("cx", "chatgpt.com", "vibepod-codex-abc4", "gpt-5-codex"),
            ("cp", "api.githubcopilot.com", "vibepod-copilot-abc3", "claude-sonnet-4-5"),
        ):
            self._request(rid, host, "/v1/chat", container, model, ts="2026-07-26T10:00:00+00:00")
            self._response(
                rid,
                json.dumps(
                    {"model": model, "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
                ).encode(),
            )
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        by_agent = {
            row[0]: row[1]
            for row in self.conn.execute(charts["cost_by_agent"]["query"], self.PARAMS).fetchall()
        }

        self.assertAlmostEqual(by_agent["codex"], 1.25)
        self.assertAlmostEqual(by_agent["copilot"], 3.0)

    def test_dashboard_estimated_value_charts_cover_unverified_placeholder_models(self):
        # gpt-5.6-sol has no confirmed openai.com/api/pricing figure yet, so the
        # bundled entry is a PLACEHOLDER mirroring the gpt-5 tier and stays
        # is_estimated until a real source replaces it. This is the "we're
        # guessing" bucket, kept out of total_cost/cost_by_agent.
        self._request(
            "r5",
            "api.openai.com",
            "/v1/chat/completions",
            "vibepod-openai-plc1",
            "gpt-5.6-sol",
            # gpt-5.6-sol's bundled price is only effective_from 2026-08-01;
            # the seed's default timestamp (2026-07-26) predates it.
            ts="2026-08-15T10:00:00+00:00",
        )
        self._response(
            "r5",
            json.dumps(
                {
                    "model": "gpt-5.6-sol",
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
                },
            ).encode(),
            ts="2026-08-15T10:00:01+00:00",
        )
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        total_estimated_value = self.conn.execute(
            charts["total_estimated_value"]["query"],
            self.PARAMS,
        ).fetchone()[0]
        by_agent = {
            row[0]: row[1]
            for row in self.conn.execute(
                charts["estimated_value_by_agent"]["query"],
                self.PARAMS,
            ).fetchall()
        }

        # Both kinds of vague reference land here and nowhere else: the
        # placeholder above, and the seeded codex call priced by the
        # openai-codex catch-all because its model matched no entry.
        codex_catch_all = 700 * 1.25 / 1_000_000 + 90 * 10.0 / 1_000_000
        placeholder = 1.25 + 10.0
        self.assertAlmostEqual(total_estimated_value, placeholder + codex_catch_all, places=4)
        self.assertAlmostEqual(by_agent["openai"], placeholder, places=4)
        self.assertAlmostEqual(by_agent["codex"], codex_catch_all, places=4)

    def _seed_priced_call(self, rid, ts, tokens, estimated=False, container="vibepod-cst-1"):
        """One call that prices at a known figure, on either side of is_estimated."""
        if estimated:
            # gpt-5.6-sol is a PLACEHOLDER rate: $1.25 per 1M input tokens.
            self._request(
                rid, "api.openai.com", "/v1/chat/completions", container, "gpt-5.6-sol", ts=ts
            )
            body = {
                "model": "gpt-5.6-sol",
                "usage": {"prompt_tokens": tokens, "completion_tokens": 0},
            }
        else:
            # claude-opus-4-5 is a confirmed rate: $5.00 per 1M input tokens.
            self._request(
                rid, "api.anthropic.com", "/v1/messages", container, "claude-opus-4-5", ts=ts
            )
            body = {"usage": {"input_tokens": tokens, "output_tokens": 0}}
        self._response(rid, json.dumps(body).encode(), ts=ts)

    def _cost_trend(self, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        rows = self.conn.execute(
            charts["cost_trend"]["query"],
            dict(self.PARAMS, agent="cst", **params),
        ).fetchall()
        return {(row[0], row[1]): row[2] for row in rows}

    def test_cost_trend_splits_confirmed_and_estimated_per_bucket(self):
        older = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00+00:00")
        newer = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_priced_call("ct1", older, 1_000_000)
        self._seed_priced_call("ct2", newer, 1_000_000)
        self._seed_priced_call("ct3", newer, 2_000_000)
        self._seed_priced_call("ct4", newer, 1_000_000, estimated=True)
        self.source.commit()
        self.refresh()

        rows = self._cost_trend(time_bucket="day")

        older_day, newer_day = older[:10], newer[:10]
        self.assertAlmostEqual(rows[(older_day, "confirmed")], 5.0)
        self.assertAlmostEqual(rows[(newer_day, "confirmed")], 15.0)
        self.assertAlmostEqual(rows[(newer_day, "estimated")], 1.25)
        # A bucket with calls but nothing on this series reports zero rather
        # than dropping out: the line would otherwise be drawn straight from
        # the previous point to the next, across a period that spent nothing.
        self.assertAlmostEqual(rows[(older_day, "estimated")], 0.0)

    def test_cost_trend_buckets_by_week_and_month(self):
        # Two calls ~5 weeks apart: same quarter, different weeks and months.
        first = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT10:00:00+00:00")
        second = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_priced_call("cw1", first, 1_000_000)
        self._seed_priced_call("cw2", second, 1_000_000)
        self.source.commit()
        self.refresh()

        weekly = self._cost_trend(time_range="3m", time_bucket="week")
        monthly = self._cost_trend(time_range="3m", time_bucket="month")

        self.assertEqual(len({bucket for bucket, _series in weekly}), 2)
        self.assertEqual(len({bucket for bucket, _series in monthly}), 2)
        # Week buckets start on a Monday; month buckets on the first.
        for bucket, _series in weekly:
            self.assertEqual(datetime.fromisoformat(bucket).weekday(), 0)
        for bucket, _series in monthly:
            self.assertTrue(bucket.endswith("-01"), bucket)
        self.assertAlmostEqual(sum(weekly.values()), sum(monthly.values()))

    def test_cost_change_compares_against_the_previous_window(self):
        current = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        previous = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
        self._seed_priced_call("cc1", current, 1_000_000)  # $5 this 24h
        self._seed_priced_call("cc2", previous, 2_000_000)  # $10 the 24h before
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        change = self.conn.execute(
            charts["cost_change"]["query"],
            dict(self.PARAMS, agent="cst", time_range="24h"),
        ).fetchone()[0]

        self.assertAlmostEqual(change, -50.0)

    def test_cost_change_reports_nothing_without_a_comparable_window(self):
        # 'all' has no preceding window, and a previous period of $0 would
        # divide by zero: both must read as blank, not as 0% or infinity.
        self._seed_priced_call("cn1", datetime.now(UTC).isoformat(), 1_000_000)
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        for time_range in ("all", "24h"):
            with self.subTest(time_range=time_range):
                change = self.conn.execute(
                    charts["cost_change"]["query"],
                    dict(self.PARAMS, agent="cst", time_range=time_range),
                ).fetchone()[0]
                self.assertIsNone(change)

    def test_cost_over_time_query_reconciles_with_the_chart(self):
        ts = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_priced_call("cr1", ts, 1_000_000)
        self._seed_priced_call("cr2", ts, 1_000_000, estimated=True)
        self.source.commit()
        self.refresh()

        sql = self.metadata["databases"]["usage"]["queries"]["cost_over_time"]["sql"]
        rows = {
            row[0]: row
            for row in self.conn.execute(sql, dict(self.PARAMS, agent="cst", time_bucket="day"))
        }
        charted = self._cost_trend(time_bucket="day")

        day = ts[:10]
        self.assertAlmostEqual(rows[day][2], charted[(day, "confirmed")])
        self.assertAlmostEqual(rows[day][3], charted[(day, "estimated")])

    def test_model_filter_narrows_the_charts(self):
        ts = datetime.now(UTC).isoformat()
        self._seed_priced_call("cm1", ts, 1_000_000)
        self._seed_priced_call("cm2", ts, 1_000_000, estimated=True)
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        total = self.conn.execute(
            charts["total_cost"]["query"],
            dict(self.PARAMS, agent="cst", model="claude-opus-4-5"),
        ).fetchone()[0]

        self.assertAlmostEqual(total, 5.0)

    def test_trend_legends_isolate_a_single_series(self):
        # The legend binding is what lets a reader look at one series alone;
        # without the opacity condition the click has no visible effect.
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        for name in ("token_trend", "cost_trend"):
            with self.subTest(chart=name):
                display = charts[name]["display"]
                param = display["params"][0]
                self.assertEqual(param["bind"], "legend")
                self.assertEqual(param["select"]["fields"], ["series"])
                condition = display["encoding"]["opacity"]["condition"]
                self.assertEqual(condition["param"], param["name"])
                # An empty selection must match everything, or the chart opens
                # with every series dimmed until something is clicked.
                self.assertTrue(condition["empty"])

    def test_every_chart_query_references_model_filter(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.assertIn(":model", chart["query"])

    def _percentile_cards(self, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        return {
            name: self.conn.execute(
                charts[name]["query"],
                dict(self.PARAMS, agent="pct", **params),
            ).fetchone()[0]
            for name in ("avg_cost_per_call", "median_cost_per_call", "p95_cost_per_call")
        }

    def test_cost_percentiles_match_a_known_distribution(self):
        # Ten calls at $1..$10 of confirmed cost: mean 5.5, median 5.5 (the two
        # middle ranks averaged), p95 at nearest rank ceil(0.95 * 10) = 10.
        ts = datetime.now(UTC).isoformat()
        for i in range(1, 11):
            self._seed_priced_call(f"pc{i}", ts, i * 200_000, container="vibepod-pct-1")
        self.source.commit()
        self.refresh()

        cards = self._percentile_cards()

        self.assertAlmostEqual(cards["avg_cost_per_call"], 5.5, places=4)
        self.assertAlmostEqual(cards["median_cost_per_call"], 5.5, places=4)
        self.assertAlmostEqual(cards["p95_cost_per_call"], 10.0, places=4)

    def test_cost_percentiles_handle_an_odd_count(self):
        # Three calls at $1/$2/$3: the median is the single middle rank, and
        # p95 is ceil(0.95 * 3) = rank 3.
        ts = datetime.now(UTC).isoformat()
        for i in range(1, 4):
            self._seed_priced_call(f"po{i}", ts, i * 200_000, container="vibepod-pct-1")
        self.source.commit()
        self.refresh()

        cards = self._percentile_cards()

        self.assertAlmostEqual(cards["median_cost_per_call"], 2.0, places=4)
        self.assertAlmostEqual(cards["p95_cost_per_call"], 3.0, places=4)

    def test_cost_percentiles_ignore_calls_without_a_confirmed_price(self):
        # An estimated call must not drag the distribution: these statistics
        # describe money with a confirmed rate behind it.
        ts = datetime.now(UTC).isoformat()
        self._seed_priced_call("pe1", ts, 1_000_000, container="vibepod-pct-1")
        self._seed_priced_call("pe2", ts, 1_000_000, container="vibepod-pct-1", estimated=True)
        self.source.commit()
        self.refresh()

        cards = self._percentile_cards()

        self.assertAlmostEqual(cards["avg_cost_per_call"], 5.0, places=4)

    def test_cost_per_call_by_model_shows_what_each_average_rests_on(self):
        # A segment whose calls are mostly unpriced would otherwise report a
        # confident average drawn from a couple of rows.
        ts = datetime.now(UTC).isoformat()
        self._seed_priced_call("ps1", ts, 1_000_000, container="vibepod-pct-1")
        self._seed_priced_call("ps2", ts, 1_000_000, container="vibepod-pct-1", estimated=True)
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(
            charts["cost_per_call_by_model"]["query"],
            dict(self.PARAMS, agent="pct"),
        )
        columns = [c[0] for c in cursor.description]
        rows = {
            (row[columns.index("provider")], row[columns.index("model")]): row for row in cursor
        }

        confirmed = rows[("anthropic", "claude-opus-4-5")]
        self.assertEqual(confirmed[columns.index("calls")], 1)
        self.assertEqual(confirmed[columns.index("confirmed_calls")], 1)
        self.assertAlmostEqual(confirmed[columns.index("avg_cost_usd")], 5.0, places=4)

        # The estimated-only segment is still listed, with no percentiles and
        # its call counted, rather than dropped by the confirmed-price join.
        estimated = rows[("openai", "gpt-5.6-sol")]
        self.assertEqual(estimated[columns.index("confirmed_calls")], 0)
        self.assertEqual(estimated[columns.index("estimated_calls")], 1)
        self.assertIsNone(estimated[columns.index("avg_cost_usd")])

    def test_most_expensive_calls_are_ranked_and_traceable(self):
        ts = datetime.now(UTC).isoformat()
        self._seed_priced_call("mx1", ts, 1_000_000, container="vibepod-pct-1")
        self._seed_priced_call("mx2", ts, 3_000_000, container="vibepod-pct-1")
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(
            charts["most_expensive_calls"]["query"],
            dict(self.PARAMS, agent="pct"),
        )
        columns = [c[0] for c in cursor.description]
        rows = cursor.fetchall()

        self.assertAlmostEqual(rows[0][columns.index("cost_usd")], 15.0, places=4)
        # The request id is what makes an outlier followable into proxy.db.
        self.assertEqual(rows[0][columns.index("request_id")], "mx2")

    def _outlier_chart(self, name, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(
            charts[name]["query"],
            dict(self.PARAMS, agent="anm", time_bucket="day", **params),
        )
        columns = [c[0] for c in cursor.description]
        return columns, cursor.fetchall()

    def test_outlier_calls_are_judged_against_their_own_model(self):
        ts = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
        for i in range(5):
            self._seed_priced_call(f"ol{i}", ts, 200_000, container="vibepod-anm-1")
        self._seed_priced_call("olx", ts, 2_000_000, container="vibepod-anm-1")
        self.source.commit()
        self.refresh()

        columns, rows = self._outlier_chart("outlier_calls")

        self.assertEqual([row[columns.index("request_id")] for row in rows], ["olx"])
        self.assertAlmostEqual(rows[0][columns.index("model_median_cost_usd")], 1.0, places=4)
        self.assertAlmostEqual(rows[0][columns.index("vs_median")], 10.0, places=1)

    def test_outliers_need_a_segment_big_enough_to_have_a_median(self):
        ts = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_priced_call("os1", ts, 200_000, container="vibepod-anm-1")
        self._seed_priced_call("os2", ts, 2_000_000, container="vibepod-anm-1")
        self.source.commit()
        self.refresh()

        _columns, rows = self._outlier_chart("outlier_calls")

        self.assertEqual(rows, [])

    def test_rate_changes_show_both_rates_a_model_was_billed_at(self):
        # gpt-4o's bundled pricing has a real price cut: $5/1M before
        # 2024-08-06 and $2.50 after. Calls either side must surface as one
        # model billed at two rates, which moves cost without usage moving.
        for rid, ts in (("rc1", "2024-06-01T10:00:00+00:00"), ("rc2", "2024-09-01T10:00:00+00:00")):
            self._request("x" + rid, "api.openai.com", "/v1/chat", "vibepod-anm-1", "gpt-4o", ts=ts)
            self._response(
                "x" + rid,
                json.dumps(
                    {
                        "model": "gpt-4o",
                        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                    },
                ).encode(),
                ts=ts,
            )
        self.source.commit()
        self.refresh()

        columns, rows = self._outlier_chart("rate_changes")
        rates = {
            row[columns.index("price_effective_from")]: row[columns.index("input_price_per_1m")]
            for row in rows
        }

        self.assertEqual(rates, {"2024-05-13": 5.0, "2024-08-06": 2.5})

    def _quality_rows(self, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(
            charts["pricing_quality"]["query"],
            dict(self.PARAMS, **params),
        )
        columns = [c[0] for c in cursor.description]
        return columns, cursor.fetchall()

    def test_pricing_quality_names_how_each_call_was_priced(self):
        # One call of each kind the matcher can produce.
        ts = "2026-08-15T10:00:00+00:00"
        self._seed_priced_call("q1", ts, 1_000_000, container="vibepod-qly-1")  # exact rate
        self._seed_priced_call(
            "q2",
            ts,
            1_000_000,
            estimated=True,
            container="vibepod-qly-1",
        )  # placeholder
        # A dated snapshot prices off the undated entry: matched by prefix.
        self._request(
            "q3",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-qly-1",
            "claude-opus-4-5-20260101",
            ts=ts,
        )
        self._response(
            "q3",
            json.dumps({"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}).encode(),
            ts=ts,
        )
        # groq has no catch-all, so nothing can price this one at all.
        self._request("q4", "api.groq.com", "/v1/chat", "vibepod-qly-1", "l3", ts=ts)
        self._response("q4", json.dumps({"usage": {"prompt_tokens": 400}}).encode(), ts=ts)
        self.source.commit()
        self.refresh()

        columns, rows = self._quality_rows(agent="qly")
        status = {row[columns.index("model")]: row[columns.index("match_status")] for row in rows}

        self.assertEqual(status["claude-opus-4-5"], "exact rate")
        self.assertEqual(status["gpt-5.6-sol"], "placeholder rate")
        self.assertEqual(status["claude-opus-4-5-20260101"], "prefix match")
        self.assertEqual(status["l3"], "unpriced")

    def test_pricing_quality_reports_the_age_of_the_rate_it_applied(self):
        # No staleness verdict: the age of the applied rate is a column, since
        # an old rate is only a problem if the real price moved.
        ts = "2026-08-15T10:00:00+00:00"
        self._seed_priced_call("qa1", ts, 1_000_000, container="vibepod-qly-1")
        self.source.commit()
        self.refresh()

        columns, rows = self._quality_rows(agent="qly")
        row = next(r for r in rows if r[columns.index("model")] == "claude-opus-4-5")

        # claude-opus-4-5's confirmed rate is effective from 2025-11-01.
        self.assertEqual(row[columns.index("price_effective_from")], "2025-11-01")
        self.assertGreater(row[columns.index("price_age_days")], 280)

    def test_unpriced_volume_is_reported_in_tokens_and_valued_transparently(self):
        # A priced call sets the window's rate; an unpriced one of the same
        # size should then be valued at that same rate.
        ts = "2026-08-15T10:00:00+00:00"
        self._seed_priced_call("ui1", ts, 1_000_000, container="vibepod-qly-1")  # $5 / 1M tokens
        self._request("ui2", "api.groq.com", "/v1/chat", "vibepod-qly-1", "l3", ts=ts)
        self._response("ui2", json.dumps({"usage": {"prompt_tokens": 1_000_000}}).encode(), ts=ts)
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        params = dict(self.PARAMS, agent="qly")
        tokens = self.conn.execute(charts["unpriced_tokens"]["query"], params).fetchone()[0]
        impact = self.conn.execute(charts["unpriced_impact"]["query"], params).fetchone()[0]

        self.assertEqual(tokens, 1_000_000)
        self.assertAlmostEqual(impact, 5.0, places=4)

    def test_tokens_by_workspace_agent_includes_cost_columns(self):
        # Same priced call as test_dashboard_cost_charts_sum_bundled_pricing:
        # claude-opus-4-5 at $5/$25 per 1M input/output tokens.
        self._request(
            "r5",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-claude-abc1",
            "claude-opus-4-5",
            container_id="abc1id123456",
        )
        self._response(
            "r5",
            json.dumps(
                {"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}},
            ).encode(),
        )
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(charts["tokens_by_workspace_agent"]["query"], self.PARAMS)
        columns = [c[0] for c in cursor.description]
        rows = {(row[0], row[1]): row for row in cursor.fetchall()}
        real_idx = columns.index("real_cost_usd")
        estimated_idx = columns.index("estimated_cost_usd")

        alpha_claude = rows[("alpha", "claude")]
        self.assertAlmostEqual(alpha_claude[real_idx], 5.0 + 25.0)
        self.assertAlmostEqual(alpha_claude[estimated_idx], 0.0)

        # The seeded codex call ran an unlisted model, so the catch-all prices
        # it and the table reports it as estimated, never as confirmed cost.
        gamma_codex = rows[("gamma", "codex")]
        self.assertAlmostEqual(gamma_codex[real_idx], 0.0)
        self.assertAlmostEqual(
            gamma_codex[estimated_idx],
            round(700 * 1.25 / 1_000_000 + 90 * 10.0 / 1_000_000, 4),
        )

    def test_recent_calls_includes_cost_and_estimated_flag(self):
        self._request(
            "r5",
            "api.anthropic.com",
            "/v1/messages",
            "vibepod-claude-abc1",
            "claude-opus-4-5",
            container_id="abc1id123456",
        )
        self._response(
            "r5",
            json.dumps(
                {"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}},
            ).encode(),
        )
        # gpt-5.6-sol is an unverified PLACEHOLDER price, so this row must
        # still surface is_estimated = 1 even though most calls now don't.
        self._request(
            "r6",
            "api.openai.com",
            "/v1/chat/completions",
            "vibepod-openai-plc1",
            "gpt-5.6-sol",
            # gpt-5.6-sol's bundled price is only effective_from 2026-08-01;
            # the seed's default timestamp (2026-07-26) predates it.
            ts="2026-08-15T10:00:00+00:00",
        )
        self._response(
            "r6",
            json.dumps(
                {
                    "model": "gpt-5.6-sol",
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
                },
            ).encode(),
            ts="2026-08-15T10:00:01+00:00",
        )
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        cursor = self.conn.execute(charts["recent_calls"]["query"], self.PARAMS)
        columns = [c[0] for c in cursor.description]
        # Keyed by model, not agent: r1 (model "opus") and r5 (model
        # "claude-opus-4-5") share both an agent and a timestamp, so an
        # agent-keyed dict could collapse onto either row.
        rows = {row[columns.index("model")]: row for row in cursor.fetchall()}
        cost_idx = columns.index("cost_usd")
        estimated_idx = columns.index("is_estimated")

        self.assertAlmostEqual(rows["claude-opus-4-5"][cost_idx], 5.0 + 25.0)
        self.assertEqual(rows["claude-opus-4-5"][estimated_idx], 0)
        # Priced by the openai-codex catch-all, so flagged estimated.
        self.assertEqual(rows["c"][estimated_idx], 1)
        self.assertEqual(rows["gpt-5.6-sol"][estimated_idx], 1)

    def test_cache_write_card_sums_cache_creation_tokens(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        written = self.conn.execute(
            charts["total_cache_write_tokens"]["query"],
            self.PARAMS,
        ).fetchone()[0]
        cached = self.conn.execute(
            charts["total_cached_tokens"]["query"],
            self.PARAMS,
        ).fetchone()[0]

        self.assertEqual(written, 100)
        self.assertEqual(cached, 900)

    def test_tables_expose_cache_write_tokens(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        for name in ("tokens_by_agent_model", "recent_calls"):
            with self.subTest(chart=name):
                cursor = self.conn.execute(charts[name]["query"], self.PARAMS)
                self.assertIn("cache_write_tokens", [c[0] for c in cursor.description])

    def _heatmap_rows(self, chart_name, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        return self.conn.execute(
            charts[chart_name]["query"],
            dict(self.PARAMS, **params),
        ).fetchall()

    def _seed_usage_call(
        self,
        rid,
        ts,
        prompt_tokens,
        completion_tokens,
        container="vibepod-tau-hm",
    ):
        self._request(rid, "api.groq.com", "/openai/v1/chat/completions", container, "l3", ts=ts)
        self._response(
            rid,
            json.dumps(
                {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}},
            ).encode(),
            ts=ts,
        )

    def test_daily_heatmaps_group_tokens_per_day(self):
        day1 = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00+00:00")
        day2 = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm1", day1, 40, 4)
        self._seed_usage_call("hm2", day1, 60, 6)
        self._seed_usage_call("hm3", day2, 7, 70)
        self.source.commit()
        self.refresh()

        input_rows = {row[0]: row for row in self._heatmap_rows("daily_input_heatmap")}
        output_rows = {row[0]: row for row in self._heatmap_rows("daily_output_heatmap")}

        d1, d2 = day1[:10], day2[:10]
        # Columns: day, week, weekday, tokens, calls
        self.assertEqual(input_rows[d1][3], 100)
        self.assertEqual(input_rows[d1][4], 2)
        self.assertEqual(input_rows[d2][3], 7)
        self.assertEqual(output_rows[d1][3], 10)
        self.assertEqual(output_rows[d2][3], 70)
        self.assertIn(input_rows[d1][2], {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"})

        # GitHub-style dense grid: every day of the window is a row, idle days
        # included with zero tokens so they render as empty cells.
        self.assertEqual(len(input_rows), 366)
        idle = [row for row in input_rows.values() if row[3] == 0 and row[4] == 0]
        self.assertGreaterEqual(len(idle), 300)

    def test_heatmaps_ignore_time_range_filter(self):
        ts = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm4", ts, 5, 1)
        self.source.commit()
        self.refresh()

        # Always the full dense year grid, whatever range is selected.
        for time_range in ("1h", "24h", "7d", "30d", "3m", "6m", "1y", "all"):
            with self.subTest(time_range=time_range):
                rows = self._heatmap_rows("daily_input_heatmap", time_range=time_range)
                self.assertEqual(len(rows), 366)
                self.assertIn(ts[:10], [row[0] for row in rows])

    def test_time_range_dropdown_offers_short_and_long_ranges(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        self.assertEqual(
            filters["time_range"]["options"],
            ["1h", "2h", "4h", "24h", "7d", "30d", "3m", "6m", "1y", "all"],
        )

    def test_every_offered_time_range_narrows_the_charts(self):
        # A range the dropdown offers but no chart's CASE maps silently falls
        # through to the ELSE branch and quietly shows the wrong window.
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        self._seed_usage_call("tr1", ts, 7, 2, container="vibepod-trx-1")
        self.source.commit()
        self.refresh()

        query = charts["total_tokens"]["query"]
        seen = {}
        for time_range in filters["time_range"]["options"]:
            seen[time_range] = self.conn.execute(
                query,
                dict(self.PARAMS, time_range=time_range, agent="trx"),
            ).fetchone()[0]

        # The call is 3 hours old: inside 4h and everything longer, outside 1h/2h.
        self.assertEqual(seen["1h"], 0)
        self.assertEqual(seen["2h"], 0)
        self.assertEqual(seen["4h"], 9)
        self.assertEqual(seen["24h"], 9)
        self.assertEqual(seen["all"], 9)

    def test_auto_bucket_keeps_short_ranges_on_five_minute_slots(self):
        # Auto must not drop a 2h/4h window onto daily slots -- that renders as
        # a single bar and hides everything the short range was chosen to show.
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        for offset in (10, 100):
            ts = (datetime.now(UTC) - timedelta(minutes=offset)).isoformat()
            self._seed_usage_call(f"tb{offset}", ts, 5, 1, container="vibepod-tbx-1")
        self.source.commit()
        self.refresh()

        buckets = {
            row[0]
            for row in self.conn.execute(
                charts["token_trend"]["query"],
                dict(self.PARAMS, time_range="4h", time_bucket="auto", agent="tbx"),
            ).fetchall()
        }

        self.assertEqual(len(buckets), 2)

    def test_long_ranges_extend_existing_charts(self):
        # Dedicated agent so static seeds can't drift between buckets over time.
        ts = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm6", ts, 11, 3, container="vibepod-hmx-1")
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        query = charts["total_tokens"]["query"]
        within_30d = self.conn.execute(
            query,
            dict(self.PARAMS, time_range="30d", agent="hmx"),
        ).fetchone()[0]
        within_3m = self.conn.execute(
            query,
            dict(self.PARAMS, time_range="3m", agent="hmx"),
        ).fetchone()[0]

        self.assertEqual(within_30d, 0)
        self.assertEqual(within_3m, 14)

    def test_heatmaps_respect_agent_filter(self):
        # days=2 keeps this clear of the static 2026-07-26 claude seed: the
        # zero-token assertion below would fail on the one day now-2d lands on
        # it, and that date is already in the past.
        ts = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm5", ts, 9, 2)
        self.source.commit()
        self.refresh()

        claude_rows = {
            row[0]: row for row in self._heatmap_rows("daily_input_heatmap", agent="claude")
        }
        tau_rows = {row[0]: row for row in self._heatmap_rows("daily_input_heatmap", agent="tau")}

        self.assertEqual(claude_rows[ts[:10]][3], 0)
        self.assertEqual(tau_rows[ts[:10]][3], 9)

    def test_layout_is_uniform_and_places_heatmaps(self):
        dash = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]

        self.assertEqual({len(row) for row in dash["layout"]}, {6})
        self.assertEqual(
            dash["layout"][0],
            ["daily_input_heatmap"] * 3 + ["daily_output_heatmap"] * 3,
        )
        names = {name for row in dash["layout"] for name in row}
        self.assertTrue(names <= set(dash["charts"].keys()))

    def _trend_buckets(self, **params):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        rows = self.conn.execute(
            charts["token_trend"]["query"],
            dict(self.PARAMS, **params),
        ).fetchall()
        return sorted({row[0] for row in rows})

    def test_five_minute_buckets_floor_to_the_slot(self):
        # 10:00:00 and 10:03:30 share a slot; 10:07:00 starts the next one.
        self._request(
            "t1",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-t1",
            "l3",
            ts="2026-07-26T10:03:30+00:00",
        )
        self._response("t1", json.dumps({"usage": {"prompt_tokens": 5}}).encode())
        self._request(
            "t2",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-t2",
            "l3",
            ts="2026-07-26T10:07:00+00:00",
        )
        self._response("t2", json.dumps({"usage": {"prompt_tokens": 5}}).encode())
        self.source.commit()
        self.refresh()

        buckets = self._trend_buckets(time_bucket="5min")

        self.assertIn("2026-07-26 10:00:00", buckets)
        self.assertIn("2026-07-26 10:05:00", buckets)
        self.assertNotIn("2026-07-26 10:03:00", buckets)

    def test_auto_bucket_uses_five_minutes_for_one_hour_range(self):
        # Seed a call inside the last hour so the 1h window actually has data.
        recent = datetime.now(UTC) - timedelta(minutes=10)
        self._request(
            "a1",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-a1",
            "l3",
            ts=recent.isoformat(),
        )
        self._response(
            "a1",
            json.dumps({"usage": {"prompt_tokens": 5}}).encode(),
            ts=recent.isoformat(),
        )
        self.source.commit()
        self.refresh()

        buckets = self._trend_buckets(time_bucket="auto", time_range="1h")

        slot = recent.replace(second=0, microsecond=0)
        slot -= timedelta(minutes=slot.minute % 5)
        self.assertEqual(buckets, [slot.strftime("%Y-%m-%d %H:%M:%S")])

    def test_bucket_filter_spans_five_minutes_to_months(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        self.assertEqual(
            filters["time_bucket"]["options"],
            ["auto", "5min", "hour", "day", "week", "month"],
        )

    def test_bar_charts_split_input_and_output_series(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        rows = self.conn.execute(
            charts["tokens_by_agent"]["query"],
            self.PARAMS,
        ).fetchall()

        by_key = {(row[0], row[1]): row[2] for row in rows}
        self.assertEqual(by_key[("claude", "input")], 1200)
        self.assertEqual(by_key[("claude", "output")], 300)
        self.assertEqual(by_key[("codex", "input")], 700)
        self.assertEqual(by_key[("codex", "output")], 90)
        # Stacked bars need one row per series, never a pre-summed total.
        self.assertEqual({row[1] for row in rows}, {"input", "output"})

    def test_provider_bar_chart_groups_by_provider(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        rows = self.conn.execute(
            charts["tokens_by_provider"]["query"],
            self.PARAMS,
        ).fetchall()

        by_key = {(row[0], row[1]): row[2] for row in rows}
        self.assertEqual(by_key[("anthropic", "input")], 1200)
        self.assertEqual(by_key[("groq", "output")], 20)

    def test_provider_filter_narrows_every_chart(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        params = dict(self.PARAMS, provider="anthropic")

        total = self.conn.execute(charts["total_tokens"]["query"], params).fetchone()[0]
        agents = self.conn.execute(
            charts["tokens_by_agent"]["query"],
            params,
        ).fetchall()

        self.assertEqual(total, 1500)
        self.assertEqual({row[0] for row in agents}, {"claude"})

    def test_provider_filter_options_query_runs(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        providers = [row[0] for row in self.conn.execute(filters["provider"]["query"]).fetchall()]

        self.assertEqual(providers[0], "all")
        self.assertIn("anthropic", providers)
        self.assertIn("openai-codex", providers)

    def test_duplicate_completion_frames_are_counted_once(self):
        """A repeated usage snapshot must not inflate totals (ccusage #884)."""
        frame = json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_dup",
                    "model": "gpt-5-codex",
                    "usage": {"input_tokens": 500, "output_tokens": 50},
                },
            },
        ).encode()
        for ts in ("2026-07-26T11:00:00+00:00", "2026-07-26T11:00:01+00:00"):
            self.source.execute(
                "INSERT INTO websocket_messages (request_id, timestamp, direction, type, content) "
                "VALUES ('r4', ?, 'incoming', 'text', ?)",
                (ts, frame),
            )
        self.source.commit()
        self.refresh()

        stored = self.conn.execute(
            "SELECT COUNT(*) FROM token_usage WHERE response_id = 'resp_dup'",
        ).fetchone()[0]
        counted = self.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens) FROM agent_token_usage "
            "WHERE response_id = 'resp_dup'",
        ).fetchone()

        self.assertEqual(stored, 2)
        self.assertEqual(counted, (1, 500))

    def test_duplicate_dedup_keeps_the_fullest_snapshot(self):
        for tokens, ts in (
            (10, "2026-07-26T12:00:00+00:00"),
            (900, "2026-07-26T12:00:02+00:00"),
        ):
            self.source.execute(
                "INSERT INTO websocket_messages (request_id, timestamp, direction, type, content) "
                "VALUES ('r4', ?, 'incoming', 'text', ?)",
                (
                    ts,
                    json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_partial",
                                "usage": {"input_tokens": tokens, "output_tokens": 1},
                            },
                        },
                    ).encode(),
                ),
            )
        self.source.commit()
        self.refresh()

        kept = self.conn.execute(
            "SELECT input_tokens FROM agent_token_usage WHERE response_id = 'resp_partial'",
        ).fetchall()

        self.assertEqual(kept, [(900,)])

    def test_distinct_completions_on_one_connection_all_count(self):
        """One websocket request carries many model calls; each must be kept."""
        for idx in range(3):
            self.source.execute(
                "INSERT INTO websocket_messages (request_id, timestamp, direction, type, content) "
                "VALUES ('r4', ?, 'incoming', 'text', ?)",
                (
                    f"2026-07-26T13:0{idx}:00+00:00",
                    json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": f"resp_turn_{idx}",
                                "usage": {"input_tokens": 100, "output_tokens": 10},
                            },
                        },
                    ).encode(),
                ),
            )
        self.source.commit()
        self.refresh()

        turns = self.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens) FROM agent_token_usage "
            "WHERE response_id LIKE 'resp_turn_%'",
        ).fetchone()

        self.assertEqual(turns, (3, 300))

    def test_model_comes_from_the_response_not_the_upgrade_request(self):
        self.source.execute(
            "INSERT INTO http_requests (id, timestamp, method, source_container_name, host, "
            "path, body) VALUES ('r9', ?, 'POST', 'vibepod-codex-abc9', 'chatgpt.com', "
            "'/backend-api/codex/responses', NULL)",
            ("2026-07-26T14:00:00+00:00",),
        )
        self.source.execute(
            "INSERT INTO websocket_messages (request_id, timestamp, direction, type, content) "
            "VALUES ('r9', ?, 'incoming', 'text', ?)",
            (
                "2026-07-26T14:00:01+00:00",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_model",
                            "model": "gpt-5-codex-high",
                            "usage": {"input_tokens": 5, "output_tokens": 1},
                        },
                    },
                ).encode(),
            ),
        )
        self.source.commit()
        self.refresh()

        model = self.conn.execute(
            "SELECT model FROM agent_token_usage WHERE response_id = 'resp_model'",
        ).fetchone()[0]

        self.assertEqual(model, "gpt-5-codex-high")

    def test_schema_upgrade_rebuilds_an_older_cache(self):
        legacy = Path(self.tmp.name) / "legacy-usage.db"
        conn = sqlite3.connect(legacy)
        conn.execute(
            "CREATE TABLE token_usage (source TEXT, row_id INTEGER, request_id TEXT)",
        )
        conn.execute(
            "CREATE TABLE sync_state "
            "(source TEXT PRIMARY KEY, last_row_id INTEGER, updated_at TEXT)",
        )
        conn.execute("INSERT INTO sync_state VALUES ('http', 999, '2026-01-01')")
        # Pre-dates is_estimated/effective_from: CREATE TABLE IF NOT EXISTS
        # would otherwise leave this shape in place forever.
        conn.execute(
            "CREATE TABLE model_pricing (provider TEXT, model TEXT, "
            "input_price_per_1m REAL, PRIMARY KEY (provider, model))",
        )
        conn.execute("INSERT INTO model_pricing VALUES ('stale', 'model', 1.0)")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        counts = build_usage_cache.build(self.proxy_path, legacy)

        conn = sqlite3.connect(legacy)
        self.addCleanup(conn.close)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(token_usage)")]
        self.assertIn("response_id", columns)
        for col in ("container_id", "container_name", "profile", "workspace", "workspace_name"):
            self.assertIn(col, columns)
        pricing_columns = [row[1] for row in conn.execute("PRAGMA table_info(model_pricing)")]
        for col in ("effective_from", "is_estimated"):
            self.assertIn(col, pricing_columns)
        # The stale row belonged to the old shape and was dropped with the table.
        stale = conn.execute(
            "SELECT 1 FROM model_pricing WHERE provider = 'stale'",
        ).fetchone()
        self.assertIsNone(stale)
        self.assertEqual(
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            build_usage_cache.SCHEMA_VERSION,
        )
        # The stale watermark was dropped, so everything is re-parsed.
        self.assertGreater(counts["http"], 0)

    def test_refresh_only_processes_new_rows(self):
        again = build_usage_cache.build(self.proxy_path, self.usage_path)

        self.assertEqual(again, {"http": 0, "ws": 0})

        self._request("r5", "api.groq.com", "/v1/chat", "vibepod-tau-abc5", "l3")
        self._response(
            "r5",
            json.dumps(
                {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
            ).encode(),
        )
        self.source.commit()

        counts = self.refresh()

        self.assertEqual(counts["http"], 1)
        rows = self.conn.execute(
            "SELECT SUM(input_tokens + output_tokens) FROM agent_token_usage WHERE agent = 'tau'",
        ).fetchone()
        self.assertEqual(rows[0], 110)

    def test_rebuild_is_idempotent(self):
        before = self.conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]

        build_usage_cache.build(self.proxy_path, self.usage_path, full=True)
        self.conn = sqlite3.connect(self.usage_path)

        after = self.conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
        self.assertEqual(before, after)

    def test_dedup_view_drops_http_twin_of_websocket_call(self):
        raw = self.conn.execute(
            "SELECT COUNT(*) FROM token_usage WHERE request_id = 'r4'",
        ).fetchone()[0]
        deduped = self.conn.execute(
            "SELECT source FROM agent_token_usage WHERE request_id = 'r4'",
        ).fetchall()

        self.assertEqual(raw, 2)
        self.assertEqual(deduped, [("ws",)])

    def test_missing_proxy_db_creates_empty_cache(self):
        missing = Path(self.tmp.name) / "nope.db"
        target = Path(self.tmp.name) / "empty-usage.db"

        counts = build_usage_cache.build(missing, target)

        self.assertEqual(counts, {"http": 0, "ws": 0})
        conn = sqlite3.connect(target)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0],
            0,
        )

    def test_cache_state_query_reports_watermarks(self):
        sql = self.metadata["databases"]["usage"]["queries"]["usage_cache_state"]["sql"]

        state = {row[0]: row for row in self.conn.execute(sql).fetchall()}

        self.assertEqual(sorted(state), ["http", "ws"])
        self.assertGreater(state["http"][1], 0)
        self.assertEqual(state["http"][3], 4)

    seed_container = "vibepod-claude-abc1"

    def test_container_id_match_beats_ambiguous_name(self):
        # A second, newer session shares the container name but has a different
        # id and workspace; the id lookup must win over the name ambiguity.
        self._session(
            "aaa1id000001",
            self.seed_container,
            "/elsewhere/clone",
            "2026-07-25T11:00:00+00:00",
        )
        self._request(
            "cim",
            "api.groq.com",
            "/v1/chat",
            self.seed_container,
            "opus",
            container_id="abc1id123456",
            ts="2026-07-26T10:05:00+00:00",
        )
        self._response(
            "cim",
            json.dumps({"usage": {"prompt_tokens": 9}}).encode(),
            ts="2026-07-26T10:05:00+00:00",
        )
        self.source.commit()
        self.refresh()

        workspaces = self._workspace(agent="claude")

        self.assertEqual(
            list(workspaces),
            ["alpha"],  # a name match would have picked the newer 'clone' workspace
        )

    def test_container_id_prefix_matches_full_id(self):
        self._request(
            "cid1",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-prefix",
            "l3",
            container_id="abc2id123456",
            ts="2026-07-26T10:01:00+00:00",
        )
        self._response(
            "cid1",
            json.dumps({"usage": {"prompt_tokens": 5}}).encode(),
            ts="2026-07-26T10:01:00+00:00",
        )
        self.source.commit()
        self.refresh()

        workspaces = self._workspace(agent="tau")

        self.assertIn("beta", workspaces)

    def test_name_fallback_prefers_latest_session_before_call(self):
        # A call whose id matches no session (logs.db pruned) still resolves by
        # name; of two sessions with that name the newest one started before
        # the call wins, and the one started after it is ignored.
        self._session(
            "nev1id000001",
            "vibepod-tau-fb",
            "/elsewhere/clone/new",
            "2026-07-25T11:00:00+00:00",
        )
        self._session(
            "nev2id000002",
            "vibepod-tau-fb",
            "/elsewhere/clone/old",
            "2026-07-25T09:00:00+00:00",
        )
        self._session(
            "nev3id000003",
            "vibepod-tau-fb",
            "/elsewhere/clone/later",
            "2026-07-27T09:00:00+00:00",
        )
        self._request(
            "fb",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-fb",
            "l3",
            container_id="gone0id00001",
            ts="2026-07-26T10:01:00+00:00",
        )
        self._response(
            "fb",
            json.dumps({"usage": {"prompt_tokens": 6}}).encode(),
            ts="2026-07-26T10:01:00+00:00",
        )
        self.source.commit()
        self.refresh()

        workspaces = self._workspace(agent="tau")

        self.assertIn("new", workspaces)
        self.assertNotIn("old", workspaces)
        self.assertNotIn("later", workspaces)

    def test_bare_meta_workspace_name_is_basename_even_for_trailing_slash(self):
        self._session(
            "nev3id000003",
            "vibepod-tau-trailing",
            "/home/g/projects/delta/",
            "2026-07-25T11:00:00+00:00",
        )
        self._request(
            "twl",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-trailing",
            "l3",
            container_id="nev3id000003",
            ts="2026-07-26T10:01:00+00:00",
        )
        self._response(
            "twl",
            json.dumps({"usage": {"prompt_tokens": 3}}).encode(),
            ts="2026-07-26T10:01:00+00:00",
        )
        self.source.commit()
        self.refresh()

        workspaces = self._workspace(agent="tau")

        self.assertIn("delta", workspaces)

    def test_refresh_resolves_rows_parsed_before_their_session(self):
        # Session row arrives in logs.db only after the call was parsed: the
        # resolution pass must re-resolve pending rows on the next refresh.
        self._request(
            "late",
            "api.groq.com",
            "/v1/chat",
            "vibepod-tau-late",
            "l3",
            container_id="lateid00000001",
            ts="2026-07-26T10:02:00+00:00",
        )
        self._response(
            "late",
            json.dumps({"usage": {"prompt_tokens": 4}}).encode(),
            ts="2026-07-26T10:02:00+00:00",
        )
        self.source.commit()
        self.refresh()
        self._session(
            "lateid00000001",
            "vibepod-tau-late",
            "/elsewhere/later",
            "2026-07-26T10:01:00+00:00",
        )
        self.logs.commit()
        self.refresh()

        workspaces = self._workspace(agent="tau")

        self.assertIn("later", workspaces)

    def test_grace_period_marks_unresolvable_rows_unknown_only(self):
        # The grace window counts from ingest, not from the call timestamp: a
        # freshly parsed row stays pending however old the traffic is, so a
        # session arriving later can still claim it. Once the window closes the
        # row is decided 'unknown' exactly once and never re-scanned.
        old_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        self._request("grc", "api.groq.com", "/v1/chat", "vibepod-grc-1", "l3", ts=old_ts)
        self._response("grc", json.dumps({"usage": {"prompt_tokens": 2}}).encode(), ts=old_ts)
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_workspace("grc"), "")

        counts = build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)
        self.assertEqual(self._stored_workspace("grc"), "unknown")
        self.assertGreaterEqual(counts["workspace"]["unknown"], 1)
        # Decided once: a later pass no longer sees the row at all.
        counts = build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)
        self.assertEqual(counts["workspace"]["unknown"], 0)
        self.assertEqual(self._stored_workspace("grc"), "unknown")

    def test_workspace_chart_sums_workspaces_sharing_a_basename(self):
        # Two checkouts named 'api' under different parents share the bar label:
        # the chart must add their tokens up instead of reporting one of them.
        self._session(
            "dup1id000001",
            "vibepod-tau-dup1",
            "/home/g/a/api",
            "2026-07-25T10:00:00+00:00",
        )
        self._session(
            "dup2id000002",
            "vibepod-tau-dup2",
            "/home/g/b/api",
            "2026-07-25T10:00:00+00:00",
        )
        for rid, cid, tokens in (("dup1", "dup1id000001", 10), ("dup2", "dup2id000002", 20)):
            self._request(
                rid,
                "api.groq.com",
                "/v1/chat",
                f"vibepod-tau-{rid}",
                "l3",
                container_id=cid,
            )
            self._response(rid, json.dumps({"usage": {"prompt_tokens": tokens}}).encode())
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        sql = charts["tokens_by_workspace"]["query"]
        rows = {(row[0], row[1]): row for row in self.conn.execute(sql, self.PARAMS).fetchall()}

        self.assertEqual(rows[("api", "input")][2], 30)
        self.assertEqual(rows[("api", "input")][4], 2)

    def test_every_chart_query_references_workspace_filter(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.assertIn(":workspace", chart["query"])

    def test_resolution_diagnostic_counts_unresolved_containers(self):
        # The diagnostics query buckets unresolved containers separately so a
        # broken id match shows up as a number, not a silent mis-association.
        sql = self.metadata["databases"]["usage"]["queries"]["workspace_resolution"]["sql"]

        rows = self.conn.execute(sql).fetchall()

        containers = {row[0] for row in rows}
        self.assertIn("abc1id123456", containers)
        # the copilot seed (no session) must not blend into 'unknown' invisible
        self.assertIn("", containers)

    def test_empty_workspace_labels_are_never_exposed(self):
        # The view normalizes both columns; no chart may ever show a blank label.
        rows = self.conn.execute(
            "SELECT DISTINCT workspace, workspace_name FROM agent_token_usage",
        ).fetchall()

        self.assertNotIn("", [v for pair in rows for v in pair])

    def _window(self, container_id12, name, workspace, profile, session_id, started_at):
        return build_usage_cache.SessionWindow(
            container_id12=container_id12,
            container_name=name,
            workspace=workspace,
            workspace_name=Path(workspace).name,
            profile=profile,
            session_id=session_id,
            started_at=started_at,
        )

    def test_resolve_session_unit(self):
        windows = [
            self._window(
                "abc1id123456",
                "vibepod-claude-abc1",
                "/hs/alpha",
                "work",
                "sess-alpha",
                "2026-07-25T10:00:00+00:00",
            ),
            self._window(
                "xbc1idzzzzzz",
                "vibepod-clone",
                "/hs/clone",
                "personal",
                "sess-clone",
                "2026-07-25T10:00:00+00:00",
            ),
        ]
        ts = build_usage_cache._parse_ts("2026-07-26T10:00:00+00:00")

        window, path = build_usage_cache.resolve_session(
            "abc1id123456",
            "vibepod-other",
            ts,
            windows,
        )
        self.assertEqual(
            (window.workspace, window.profile, window.session_id),
            ("/hs/alpha", "work", "sess-alpha"),
        )
        self.assertEqual(path, "by_id")

        window, path = build_usage_cache.resolve_session("", "vibepod-clone", ts, windows)
        self.assertEqual((window.workspace, window.session_id), ("/hs/clone", "sess-clone"))
        self.assertEqual(path, "by_name")

        # An id that matches no session still falls back to the name.
        window, path = build_usage_cache.resolve_session(
            "zzz1idxxxxxx",
            "vibepod-clone",
            ts,
            windows,
        )
        self.assertEqual(window.session_id, "sess-clone")
        self.assertEqual(path, "by_name")

        self.assertIsNone(build_usage_cache.resolve_session("", "vibepod-nope", ts, windows))
        self.assertIsNone(
            build_usage_cache.resolve_session("abc1idxxxxxx", "vibepod-nope", ts, windows),
        )

    def test_resolve_session_picks_the_session_the_call_belongs_to(self):
        # One container can host several sessions (re-attaching opens another),
        # so the id alone names the container, not the session; the call's own
        # time has to choose between them.
        windows = [
            self._window(
                "dup1id000001",
                "vibepod-dup-1",
                "/hs/dup",
                "work",
                "sess-first",
                "2026-07-25T10:00:00+00:00",
            ),
            self._window(
                "dup1id000001",
                "vibepod-dup-1",
                "/hs/dup",
                "work",
                "sess-second",
                "2026-07-26T10:00:00+00:00",
            ),
        ]

        during_first = build_usage_cache._parse_ts("2026-07-25T18:00:00+00:00")
        during_second = build_usage_cache._parse_ts("2026-07-26T18:00:00+00:00")
        before_any = build_usage_cache._parse_ts("2026-07-01T10:00:00+00:00")

        self.assertEqual(
            build_usage_cache.resolve_session("dup1id000001", "x", during_first, windows)[
                0
            ].session_id,
            "sess-first",
        )
        self.assertEqual(
            build_usage_cache.resolve_session("dup1id000001", "x", during_second, windows)[
                0
            ].session_id,
            "sess-second",
        )
        # A call older than every session of that container is still that
        # container's traffic, so it keeps the earliest rather than going
        # unattributed.
        self.assertEqual(
            build_usage_cache.resolve_session("dup1id000001", "x", before_any, windows)[
                0
            ].session_id,
            "sess-first",
        )

    def test_resolution_beats_the_grace_decision(self):
        # A row that can be resolved gets its workspace even when it is far
        # older than the grace period; resolution precedes the grace decision.
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        started = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        self._request(
            "grz",
            "api.groq.com",
            "/v1/chat",
            "vibepod-grz-1",
            "l3",
            container_id="grz1id000002",
            ts=old,
        )
        self._response("grz", json.dumps({"usage": {"prompt_tokens": 1}}).encode(), ts=old)
        self._session("grz1id000002", "vibepod-grz-1", "/elsewhere/resolved", started)
        self.source.commit()
        self.refresh()
        build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)

        workspaces = self._workspace(agent="grz")

        self.assertIn("resolved", workspaces)

    def _profiles(self):
        sql = self.metadata["databases"]["usage"]["queries"]["profile_token_totals"]["sql"]
        return {row[0]: row for row in self.conn.execute(sql, self.PARAMS).fetchall()}

    def _stored_session(self, request_id):
        """The raw, un-normalized session id of a row ('' while still pending)."""
        return self.conn.execute(
            "SELECT session_id FROM token_usage WHERE request_id = ?",
            (request_id,),
        ).fetchone()[0]

    def _sessions(self):
        sql = self.metadata["databases"]["usage"]["queries"]["session_token_totals"]["sql"]
        return {row[0]: row for row in self.conn.execute(sql, self.PARAMS).fetchall()}

    def test_calls_are_attributed_to_the_session_of_their_container(self):
        # The seeds resolve by container id; the session id is what makes
        # per-session totals possible at all, since proxy.db never sees it.
        self.assertEqual(self._stored_session("r1"), "sess-abc1id123456")
        self.assertEqual(self._stored_session("r2"), "sess-abc2id123456")

    def test_session_totals_aggregate_every_call_of_one_session(self):
        totals = self._sessions()

        # r1: 1200 input (900 of it cached) + 300 output, one call.
        row = totals["sess-abc1id123456"]
        self.assertEqual(row[4], 1)
        self.assertEqual(row[11], 1500)
        self.assertEqual(row[1], "claude")
        self.assertEqual(row[2], "alpha")

    def test_calls_without_a_session_are_counted_under_unknown(self):
        # The copilot seed has no session row: it must stay in the totals as
        # 'unknown' rather than vanishing from per-session reporting.
        shown = self.conn.execute(
            "SELECT session_id FROM agent_token_usage WHERE request_id = 'r3'",
        ).fetchone()[0]

        self.assertEqual(self._stored_session("r3"), "")
        self.assertEqual(shown, "unknown")

        build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)

        self.assertEqual(self._stored_session("r3"), "unknown")

    def test_session_resolution_survives_a_second_session_on_one_container(self):
        # Re-attaching opens a second session on the same container; calls must
        # land on the session that was running when they were made.
        self._session(
            "seq1id000001",
            "vibepod-seq-1",
            "/home/g/projects/seq",
            "2026-07-25T10:00:00+00:00",
            session_id="sess-early",
        )
        self._session(
            "seq1id000001",
            "vibepod-seq-1",
            "/home/g/projects/seq",
            "2026-07-27T10:00:00+00:00",
            session_id="sess-late",
        )
        for rid, ts in (("sq1", "2026-07-26T10:00:00+00:00"), ("sq2", "2026-07-28T10:00:00+00:00")):
            self._request(
                rid,
                "api.groq.com",
                "/v1/chat",
                "vibepod-seq-1",
                "l3",
                container_id="seq1id000001",
                ts=ts,
            )
            self._response(rid, json.dumps({"usage": {"prompt_tokens": 5}}).encode(), ts=ts)
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_session("sq1"), "sess-early")
        self.assertEqual(self._stored_session("sq2"), "sess-late")

    def test_session_resolution_diagnostic_lists_containers(self):
        sql = self.metadata["databases"]["usage"]["queries"]["session_resolution"]["sql"]

        rows = self.conn.execute(sql).fetchall()

        self.assertIn("sess-abc1id123456", {row[2] for row in rows})

    def test_session_totals_and_workspace_totals_agree(self):
        # Both are cuts of the same rows, so the same call cannot be counted in
        # one and missing from the other.
        sessions = self._sessions()
        workspaces = self._workspace()

        self.assertEqual(
            sum(row[11] for row in sessions.values()),
            sum(row[8] for row in workspaces.values()),
        )

    def test_profile_recorded_by_the_proxy_wins_over_the_session(self):
        # The request row is the profile the proxy actually filtered under; a
        # session that later reports another one must not overwrite it.
        self._session(
            "prf1id000001",
            "vibepod-prf-1",
            "/home/g/projects/prf",
            "2026-07-25T10:00:00+00:00",
            profile="personal",
        )
        self._request(
            "pr1",
            "api.groq.com",
            "/v1/chat",
            "vibepod-prf-1",
            "l3",
            container_id="prf1id000001",
            profile="work",
        )
        self._response("pr1", json.dumps({"usage": {"prompt_tokens": 5}}).encode())
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_profile("pr1"), "work")

    def test_profile_falls_back_to_the_session_when_the_proxy_logged_none(self):
        self._session(
            "prf2id000002",
            "vibepod-prf-2",
            "/home/g/projects/prf2",
            "2026-07-25T10:00:00+00:00",
            profile="personal",
        )
        self._request(
            "pr2",
            "api.groq.com",
            "/v1/chat",
            "vibepod-prf-2",
            "l3",
            container_id="prf2id000002",
        )
        self._response("pr2", json.dumps({"usage": {"prompt_tokens": 6}}).encode())
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_profile("pr2"), "personal")

    def test_profile_ages_out_to_unknown_without_a_session(self):
        # Same grace contract as the workspace: pending until the window
        # closes, then decided once so it is not rescanned forever.
        self._request("pr3", "api.groq.com", "/v1/chat", "vibepod-prf-3", "l3")
        self._response("pr3", json.dumps({"usage": {"prompt_tokens": 7}}).encode())
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_profile("pr3"), "")

        counts = build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)
        self.assertEqual(self._stored_profile("pr3"), "unknown")
        self.assertGreaterEqual(counts["profile"]["unknown"], 1)
        counts = build_usage_cache.sync_sessions(self.logs_path, self.conn, grace_seconds=0)
        self.assertEqual(counts["profile"]["unknown"], 0)

    def test_profile_resolves_on_a_later_refresh_like_the_workspace(self):
        # The session row can reach logs.db after the call was parsed; the
        # profile must be filled in on the next pass instead of staying empty.
        self._request(
            "pr4",
            "api.groq.com",
            "/v1/chat",
            "vibepod-prf-4",
            "l3",
            container_id="prf4id000004",
        )
        self._response("pr4", json.dumps({"usage": {"prompt_tokens": 8}}).encode())
        self.source.commit()
        self.refresh()

        self.assertEqual(self._stored_profile("pr4"), "")

        self._session(
            "prf4id000004",
            "vibepod-prf-4",
            "/home/g/projects/prf4",
            "2026-07-25T10:00:00+00:00",
            profile="late",
        )
        self.refresh()

        self.assertEqual(self._stored_profile("pr4"), "late")

    def test_empty_profile_labels_are_never_exposed(self):
        rows = self.conn.execute("SELECT DISTINCT profile FROM agent_token_usage").fetchall()

        self.assertNotIn("", [row[0] for row in rows])

    def test_profile_totals_split_usage_per_profile(self):
        for rid, profile, tokens in (("pa", "work", 10), ("pb", "personal", 25)):
            self._request(
                rid,
                "api.groq.com",
                "/v1/chat",
                f"vibepod-tot-{rid}",
                "l3",
                profile=profile,
            )
            self._response(rid, json.dumps({"usage": {"prompt_tokens": tokens}}).encode())
        self.source.commit()
        self.refresh()

        totals = self._profiles()

        self.assertEqual(totals["work"][2], 10)
        self.assertEqual(totals["personal"][2], 25)

    def test_profile_chart_sums_input_and_output_per_profile(self):
        for rid, tokens in (("pc1", 10), ("pc2", 20)):
            self._request(
                rid,
                "api.groq.com",
                "/v1/chat",
                f"vibepod-chart-{rid}",
                "l3",
                profile="shared",
            )
            self._response(rid, json.dumps({"usage": {"prompt_tokens": tokens}}).encode())
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        sql = charts["tokens_by_profile"]["query"]
        rows = {(row[0], row[1]): row for row in self.conn.execute(sql, self.PARAMS).fetchall()}

        self.assertEqual(rows[("shared", "input")][2], 30)
        self.assertEqual(rows[("shared", "input")][3], 2)

    def test_profile_filter_narrows_the_charts(self):
        self._request("pf1", "api.groq.com", "/v1/chat", "vibepod-flt-1", "l3", profile="work")
        self._response("pf1", json.dumps({"usage": {"prompt_tokens": 40}}).encode())
        self.source.commit()
        self.refresh()

        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        params = dict(self.PARAMS, profile="work")
        total = self.conn.execute(charts["total_tokens"]["query"], params).fetchone()[0]

        self.assertEqual(total, 40)

    def test_every_chart_query_references_profile_filter(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.assertIn(":profile", chart["query"])

    def test_profile_resolution_diagnostic_buckets_pending_rows(self):
        sql = self.metadata["databases"]["usage"]["queries"]["profile_resolution"]["sql"]

        rows = self.conn.execute(sql).fetchall()

        self.assertIn("abc1id123456", {row[0] for row in rows})

    def test_agent_name_parsing(self):
        self.assertEqual(
            build_usage_cache.agent_from_container("vibepod-claude-a1b2"),
            "claude",
        )
        self.assertEqual(build_usage_cache.agent_from_container("vibepod-tau"), "tau")
        self.assertEqual(
            build_usage_cache.agent_from_container("other-container"),
            "other-container",
        )
        self.assertEqual(build_usage_cache.agent_from_container(""), "unknown")
        self.assertEqual(build_usage_cache.agent_from_container(None), "unknown")


class ProxyAndSessionDashboardSqlTests(unittest.TestCase):
    """Run the proxy- and logs-backed chart queries against their real schemas.

    Those dashboards read the capture databases directly instead of the usage
    cache, so a column that only exists in a newer proxy or CLI has to be
    exercised here or nothing catches a typo in it until the dashboard renders.
    """

    PROXY_SCHEMA = """
    CREATE TABLE http_requests (id TEXT PRIMARY KEY, timestamp TEXT, method TEXT,
        source_container_id TEXT, source_container_name TEXT, scheme TEXT, host TEXT,
        port INTEGER, path TEXT, query TEXT, url TEXT, headers TEXT, body BLOB,
        client_ip TEXT, client_port INTEGER, server_ip TEXT, server_port INTEGER,
        blocked INTEGER NOT NULL DEFAULT 0, filter_mode TEXT, block_reason TEXT,
        profile TEXT);
    CREATE TABLE http_responses (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, status_code INTEGER, headers TEXT, body BLOB, bytes_in INTEGER,
        bytes_out INTEGER, duration_ms REAL);
    CREATE TABLE http_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, error_type TEXT, message TEXT);
    CREATE TABLE websocket_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, direction TEXT, type TEXT, content BLOB);
    """

    LOGS_SCHEMA = """
    CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, image TEXT, workspace TEXT,
        container_id TEXT, container_name TEXT, started_at TEXT, ended_at TEXT,
        exit_reason TEXT, vibepod_version TEXT, profile TEXT NOT NULL DEFAULT 'default');
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
        timestamp TEXT, role TEXT, content TEXT);
    """

    PROXY_PARAMS = {
        "time_range": "all",
        "time_bucket": "auto",
        "host_query": "",
        "method": "all",
        "agent": "all",
        "status_class": "all",
        "host_sort": "request_count",
        "request_sort": "timestamp_desc",
        "request_limit": 100,
    }

    LOGS_PARAMS = {
        "time_range": "all",
        "time_bucket": "auto",
        "agent": "all",
        "workspace": "all",
        "session_limit": 100,
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.metadata = json.loads((REPO_ROOT / "metadata.json").read_text())

        self.proxy = sqlite3.connect(Path(self.tmp.name) / "proxy.db")
        self.addCleanup(self.proxy.close)
        self.proxy.executescript(self.PROXY_SCHEMA)
        # One blocked and one passed request, on different profiles.
        self.proxy.executemany(
            "INSERT INTO http_requests (id, timestamp, method, source_container_name, "
            "source_container_id, host, path, blocked, filter_mode, block_reason, profile) "
            "VALUES (?, '2026-07-26T10:00:00+00:00', 'POST', 'vibepod-tau-1', 'c1', ?, "
            "'/v1/chat', ?, ?, ?, ?)",
            [
                ("blk", "evil.example", 1, "allow", "allow-miss", "work"),
                ("ok", "api.groq.com", 0, "allow", None, "personal"),
                ("noprof", "api.groq.com", 0, "allow", None, None),
            ],
        )
        self.proxy.execute(
            "INSERT INTO http_responses (request_id, timestamp, status_code, duration_ms) "
            "VALUES ('ok', '2026-07-26T10:00:01+00:00', 200, 12.5)",
        )
        self.proxy.commit()

        self.logs = sqlite3.connect(Path(self.tmp.name) / "logs.db")
        self.addCleanup(self.logs.close)
        self.logs.executescript(self.LOGS_SCHEMA)
        self.logs.execute(
            "INSERT INTO sessions (id, agent, image, workspace, container_id, container_name, "
            "started_at, profile) VALUES ('s1', 'tau', 'img', '/ws/a', 'c1', 'vibepod-tau-1', "
            "'2026-07-26T09:00:00+00:00', 'work')",
        )
        self.logs.commit()

    def test_every_http_requests_chart_query_runs(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["http-requests"]["charts"]

        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.proxy.execute(chart["query"], self.PROXY_PARAMS).fetchall()

    def test_every_agent_sessions_chart_query_runs(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-sessions"]["charts"]

        for name, chart in charts.items():
            with self.subTest(chart=name):
                self.logs.execute(chart["query"], self.LOGS_PARAMS).fetchall()

    def test_filter_decisions_are_counted_per_profile(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["http-requests"]["charts"]
        sql = charts["decisions_by_profile"]["query"]

        rows = {(row[0], row[1]): row[2] for row in self.proxy.execute(sql, self.PROXY_PARAMS)}

        self.assertEqual(rows[("work", "blocked")], 1)
        self.assertEqual(rows[("personal", "passed")], 1)
        # A request the proxy captured without a profile must stay visible.
        self.assertEqual(rows[("unknown", "passed")], 1)


class LegacySourceSchemaTests(unittest.TestCase):
    """Databases written before profiles were logged must still be ingestible.

    The proxy and the CLI grew their profile columns after this cache did, so
    both shapes are in the field; a missing column has to cost the profile of
    those rows, never the whole refresh.
    """

    PROXY_SCHEMA = """
    CREATE TABLE http_requests (id TEXT PRIMARY KEY, timestamp TEXT, method TEXT,
        source_container_id TEXT, source_container_name TEXT, scheme TEXT,
        host TEXT, port INTEGER, path TEXT, query TEXT, url TEXT, headers TEXT,
        body BLOB, client_ip TEXT, client_port INTEGER, server_ip TEXT,
        server_port INTEGER);
    CREATE TABLE http_responses (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, status_code INTEGER, headers TEXT, body BLOB, bytes_in INTEGER,
        bytes_out INTEGER, duration_ms REAL);
    """

    def setUp(self):
        decompress._decode_cache.clear()
        decompress._usage_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy_path = Path(self.tmp.name) / "proxy.db"
        self.usage_path = Path(self.tmp.name) / "usage.db"
        self.logs_path = Path(self.tmp.name) / "logs.db"
        self.source = sqlite3.connect(self.proxy_path)
        self.addCleanup(self.source.close)
        self.source.executescript(self.PROXY_SCHEMA)
        self.source.execute(
            "INSERT INTO http_requests (id, timestamp, method, source_container_name, "
            "source_container_id, host, path, body) "
            "VALUES ('old', '2026-07-26T10:00:00+00:00', 'POST', 'vibepod-old-1', "
            "'old1id000001', 'api.groq.com', '/v1/chat', ?)",
            (json.dumps({"model": "l3"}).encode(),),
        )
        self.source.execute(
            "INSERT INTO http_responses (request_id, timestamp, status_code, body) "
            "VALUES ('old', '2026-07-26T10:00:01+00:00', 200, ?)",
            (json.dumps({"usage": {"prompt_tokens": 12}}).encode(),),
        )
        self.source.commit()

    def _logs(self, schema, insert):
        logs = sqlite3.connect(self.logs_path)
        self.addCleanup(logs.close)
        logs.executescript(schema)
        logs.execute(insert)
        logs.commit()

    def _profile(self):
        conn = sqlite3.connect(self.usage_path)
        self.addCleanup(conn.close)
        return conn.execute("SELECT profile FROM token_usage WHERE request_id = 'old'").fetchone()[
            0
        ]

    def test_proxy_db_without_a_profile_column_still_ingests_and_resolves(self):
        self._logs(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, workspace TEXT, "
            "container_id TEXT, container_name TEXT, started_at TEXT, profile TEXT);",
            "INSERT INTO sessions VALUES ('s1', 'agent', '/ws/old', 'old1id000001', "
            "'vibepod-old-1', '2026-07-25T10:00:00+00:00', 'personal')",
        )

        counts = build_usage_cache.build(self.proxy_path, self.usage_path, self.logs_path)

        self.assertEqual(counts["http"], 1)
        self.assertEqual(self._profile(), "personal")

    def test_logs_db_without_a_profile_column_leaves_the_profile_unresolved(self):
        self._logs(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, agent TEXT, workspace TEXT, "
            "container_id TEXT, container_name TEXT, started_at TEXT);",
            "INSERT INTO sessions VALUES ('s1', 'agent', '/ws/old', 'old1id000001', "
            "'vibepod-old-1', '2026-07-25T10:00:00+00:00')",
        )

        build_usage_cache.build(self.proxy_path, self.usage_path, self.logs_path)

        # The workspace still resolves; only the profile has no source at all.
        conn = sqlite3.connect(self.usage_path)
        self.addCleanup(conn.close)
        workspace, profile = conn.execute(
            "SELECT workspace, profile FROM token_usage WHERE request_id = 'old'",
        ).fetchone()
        self.assertEqual(workspace, "/ws/old")
        self.assertEqual(profile, "")

        counts = build_usage_cache.sync_sessions(self.logs_path, conn, grace_seconds=0)
        self.assertEqual(counts["profile"]["unknown"], 1)
        self.assertEqual(self._profile(), "unknown")


class PricingTests(unittest.TestCase):
    """agent_token_cost matching/cost logic, independent of the proxy pipeline.

    These insert directly into usage.db's token_usage/model_pricing tables so
    each scenario only sets up the columns it needs, instead of round-tripping
    through a synthetic proxy.db (that full path is covered by
    AgentTokenSqlTests.test_dashboard_cost_charts_sum_bundled_pricing).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usage_path = Path(self.tmp.name) / "usage.db"
        self.conn = build_usage_cache.open_cache(self.usage_path)
        self.addCleanup(self.conn.close)

    def _insert_usage(
        self,
        row_id,
        provider,
        model,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        has_usage=1,
        host="test.host",
        agent="agent",
        timestamp="2026-08-30T00:00:00+00:00",
    ):
        # Other columns (response_id, reasoning_tokens, container_id,
        # container_name, profile, workspace, workspace_name, ingested_at) are
        # left to their table defaults; none of these tests exercise them.
        self.conn.execute(
            "INSERT INTO token_usage (source, row_id, request_id, timestamp, agent, "
            "provider, model, host, input_tokens, output_tokens, cached_tokens, "
            "cache_write_tokens, has_usage) VALUES ('http', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                f"req-{row_id}",
                timestamp,
                agent,
                provider,
                model,
                host,
                input_tokens,
                output_tokens,
                cached_tokens,
                cache_write_tokens,
                has_usage,
            ),
        )
        self.conn.commit()

    def _insert_price(
        self,
        provider,
        model,
        input_price=0,
        output_price=0,
        cached_price=0,
        cache_write_price=0,
        price_source="test",
        # Old enough to be "in effect" for every call these tests insert,
        # unless a test overrides it to exercise date-ranged pricing.
        effective_from="2000-01-01",
        is_estimated=False,
    ):
        self.conn.execute(
            "INSERT INTO model_pricing (provider, model, effective_from, input_price_per_1m, "
            "output_price_per_1m, cached_price_per_1m, cache_write_price_per_1m, currency, "
            "is_estimated, price_source) VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', ?, ?)",
            (
                provider,
                model,
                effective_from,
                input_price,
                output_price,
                cached_price,
                cache_write_price,
                1 if is_estimated else 0,
                price_source,
            ),
        )
        self.conn.commit()

    def _cost_row(self, row_id):
        return self.conn.execute(
            "SELECT has_price, cost_usd, is_estimated FROM agent_token_cost WHERE row_id = ?",
            (row_id,),
        ).fetchone()

    def test_exact_match_prices_a_call(self):
        self._insert_price("acme", "model-a", input_price=2.0, output_price=8.0)
        self._insert_usage(1, "acme", "model-a", input_tokens=1_000_000, output_tokens=500_000)

        has_price, cost, is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 1)
        self.assertAlmostEqual(cost, 2.0 * 1 + 8.0 * 0.5)
        self.assertEqual(is_estimated, 0)

    def test_dated_snapshot_matches_via_longest_prefix(self):
        self._insert_price("acme", "model-a", input_price=2.0, output_price=8.0)
        self._insert_usage(1, "acme", "model-a-20260101", input_tokens=1_000_000)

        has_price, cost, _is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 1)
        self.assertAlmostEqual(cost, 2.0)

    def test_more_specific_price_beats_shorter_prefix(self):
        self._insert_price("acme", "model-a", input_price=2.0)
        self._insert_price("acme", "model-a-20260101", input_price=9.0)
        self._insert_usage(1, "acme", "model-a-20260101", input_tokens=1_000_000)

        _has_price, cost, _is_estimated = self._cost_row(1)

        self.assertAlmostEqual(cost, 9.0)

    def test_no_matching_price_leaves_cost_null(self):
        self._insert_usage(1, "acme", "unknown-model", input_tokens=1_000_000)

        has_price, cost, is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 0)
        self.assertIsNone(cost)
        self.assertEqual(is_estimated, 0)

    def test_price_change_uses_the_rate_in_effect_at_call_time(self):
        # A real provider price cut: calls before the change price at the old
        # rate, calls on/after it price at the new rate. "Now" (whenever the
        # dashboard is viewed) must never leak into a historical call's cost.
        self._insert_price(
            "acme",
            "model-a",
            input_price=5.0,
            effective_from="2024-05-13",
        )
        self._insert_price(
            "acme",
            "model-a",
            input_price=2.5,
            effective_from="2024-08-06",
        )
        self._insert_usage(
            1,
            "acme",
            "model-a",
            input_tokens=1_000_000,
            timestamp="2024-06-01T00:00:00+00:00",
        )
        self._insert_usage(
            2,
            "acme",
            "model-a",
            input_tokens=1_000_000,
            timestamp="2024-09-01T00:00:00+00:00",
        )

        before_change = self._cost_row(1)
        after_change = self._cost_row(2)

        self.assertAlmostEqual(before_change[1], 5.0)
        self.assertAlmostEqual(after_change[1], 2.5)

    def test_call_before_any_known_price_is_unpriced(self):
        self._insert_price("acme", "model-a", input_price=2.5, effective_from="2024-08-06")
        self._insert_usage(
            1,
            "acme",
            "model-a",
            input_tokens=1_000_000,
            timestamp="2024-01-01T00:00:00+00:00",
        )

        has_price, cost, _is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 0)
        self.assertIsNone(cost)

    def test_provider_catch_all_prices_every_model_at_its_rate(self):
        # A provider catch-all still prices the call (not $0) and is flagged
        # is_estimated, because which model ran is a guess.
        self._insert_price(
            "acme-sub",
            "",
            input_price=2.0,
            output_price=8.0,
            is_estimated=True,
        )
        self._insert_usage(
            1,
            "acme-sub",
            "whatever-model-string",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        has_price, cost, is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 1)
        self.assertAlmostEqual(cost, 2.0 + 8.0)
        self.assertEqual(is_estimated, 1)

    def test_specific_price_beats_provider_catch_all(self):
        self._insert_price("acme-sub", "", input_price=0, output_price=0, is_estimated=True)
        self._insert_price("acme-sub", "premium-model", input_price=5.0, output_price=5.0)
        self._insert_usage(1, "acme-sub", "premium-model", input_tokens=1_000_000)

        has_price, cost, is_estimated = self._cost_row(1)

        self.assertAlmostEqual(cost, 5.0)
        self.assertEqual(is_estimated, 0)

    def test_call_without_parsed_usage_is_never_priced(self):
        # A provider catch-all would otherwise price this, hiding
        # the fact that its actual token counts are unknown.
        self._insert_price("acme-sub", "", input_price=2.0, output_price=8.0, is_estimated=True)
        self._insert_usage(1, "acme-sub", "model-a", input_tokens=1_000_000, has_usage=0)

        has_price, cost, _is_estimated = self._cost_row(1)

        self.assertEqual(has_price, 0)
        self.assertIsNone(cost)

    def test_pricing_coverage_query_separates_priced_from_unpriced(self):
        self._insert_price("acme", "model-a", input_price=2.0)
        self._insert_usage(1, "acme", "model-a", input_tokens=1_000_000)
        self._insert_usage(2, "acme", "unpriced-model", input_tokens=1_000_000)

        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        sql = metadata["databases"]["usage"]["queries"]["pricing_coverage"]["sql"]
        rows = {(r[0], r[1]): r for r in self.conn.execute(sql, {"limit": 100}).fetchall()}

        self.assertEqual(rows[("acme", "model-a")][3], 1)
        self.assertEqual(rows[("acme", "model-a")][4], 0)
        self.assertEqual(rows[("acme", "unpriced-model")][3], 0)
        self.assertEqual(rows[("acme", "unpriced-model")][4], 1)

    def test_loader_is_idempotent_and_drops_removed_entries(self):
        pricing_file = Path(self.tmp.name) / "prices.json"
        pricing_file.write_text(
            json.dumps(
                [
                    {
                        "provider": "a",
                        "model": "x",
                        "effective_from": "2024-01-01",
                        "input_price_per_1m": 1,
                    },
                    {
                        "provider": "a",
                        "model": "y",
                        "effective_from": "2024-01-01",
                        "input_price_per_1m": 2,
                    },
                ],
            ),
        )

        loaded = build_usage_cache.sync_pricing(self.conn, pricing_file)
        self.assertEqual(loaded, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0],
            2,
        )

        # Re-running with the same file must not duplicate rows.
        build_usage_cache.sync_pricing(self.conn, pricing_file)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0],
            2,
        )

        # Removing an entry from the file removes it from the table too.
        pricing_file.write_text(
            json.dumps(
                [
                    {
                        "provider": "a",
                        "model": "x",
                        "effective_from": "2024-01-01",
                        "input_price_per_1m": 1,
                    },
                ],
            ),
        )
        build_usage_cache.sync_pricing(self.conn, pricing_file)
        rows = self.conn.execute("SELECT provider, model FROM model_pricing").fetchall()
        self.assertEqual(rows, [("a", "x")])

    def test_missing_pricing_file_leaves_existing_rows_untouched(self):
        self._insert_price("acme", "model-a", input_price=2.0)

        loaded = build_usage_cache.sync_pricing(
            self.conn,
            Path(self.tmp.name) / "does-not-exist.json",
        )

        self.assertEqual(loaded, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM model_pricing").fetchone()[0],
            1,
        )

    def test_malformed_entry_is_skipped_not_fatal(self):
        pricing_file = Path(self.tmp.name) / "prices.json"
        pricing_file.write_text(
            json.dumps(
                [
                    {
                        "provider": "a",
                        "model": "x",
                        "effective_from": "2024-01-01",
                        "input_price_per_1m": 1,
                    },
                    {"model": "missing-provider-key"},
                ],
            ),
        )

        loaded = build_usage_cache.sync_pricing(self.conn, pricing_file)

        self.assertEqual(loaded, 1)

    def test_bundled_pricing_file_flags_only_vague_references_as_estimated(self):
        loaded = build_usage_cache.sync_pricing(self.conn, build_usage_cache.default_pricing_path())

        self.assertGreater(loaded, 0)
        providers = {
            row[0]
            for row in self.conn.execute("SELECT DISTINCT provider FROM model_pricing").fetchall()
        }
        self.assertIn("openai-codex", providers)
        self.assertIn("github-copilot", providers)

        # A rate published for the exact model is confirmed, whichever product
        # served the call.
        for provider, model in (
            ("openai-codex", "gpt-5-codex"),
            ("github-copilot", "claude-sonnet-4-5"),
        ):
            with self.subTest(provider=provider):
                price, estimated = self.conn.execute(
                    "SELECT input_price_per_1m, is_estimated FROM model_pricing "
                    "WHERE provider = ? AND model = ?",
                    (provider, model),
                ).fetchone()
                self.assertGreater(price, 0)
                self.assertEqual(estimated, 0)

        # The two vague references: a catch-all applied to a model that matched
        # nothing, and a placeholder rate nobody has confirmed for that model.
        vague = self.conn.execute(
            "SELECT provider, model, is_estimated, price_source FROM model_pricing "
            "WHERE model = '' OR price_source LIKE 'PLACEHOLDER%'",
        ).fetchall()
        self.assertGreater(len(vague), 0)
        for provider, model, estimated, source in vague:
            with self.subTest(provider=provider, model=model):
                self.assertEqual(estimated, 1, source)

        # Nothing else may be flagged: an unexplained estimate is exactly what
        # this rule exists to prevent.
        unexplained = self.conn.execute(
            "SELECT provider, model FROM model_pricing WHERE is_estimated = 1 "
            "AND model != '' AND price_source NOT LIKE 'PLACEHOLDER%'",
        ).fetchall()
        self.assertEqual(unexplained, [])


class AgentTokenDashboardMetadataTests(unittest.TestCase):
    def test_dashboard_layout_and_charts_are_registered(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        dashboard = metadata["plugins"]["datasette-dashboards"]["agent-tokens"]

        self.assertTrue(all(len(row) == 6 for row in dashboard["layout"]))

        charted = {name for row in dashboard["layout"] for name in row}
        self.assertEqual(charted, set(dashboard["charts"]))

        for name, chart in dashboard["charts"].items():
            with self.subTest(chart=name):
                # Charts read the materialized cache; parsing bodies per render
                # is what made this dashboard slow in the first place.
                self.assertEqual(chart["db"], "usage")
                # agent_token_cost is agent_token_usage plus a price join, so
                # both are reads of the materialized cache, never live bodies.
                self.assertTrue(
                    "FROM agent_token_usage" in chart["query"]
                    or "FROM agent_token_cost" in chart["query"],
                )
                self.assertNotIn("extract_usage(", chart["query"])

    def test_agent_tokens_is_listed_first_and_supersedes_the_per_agent_ones(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())

        # datasette-dashboards renders the index in metadata key order, so this
        # ordering is what users see. The per-agent claude-tokens/codex-tokens
        # dashboards were removed: agent-tokens covers both, from the usage
        # cache rather than by parsing bodies on every render.
        order = list(metadata["plugins"]["datasette-dashboards"])

        self.assertEqual(order[0], "agent-tokens")
        self.assertNotIn("claude-tokens", order)
        self.assertNotIn("codex-tokens", order)

    def test_readme_documents_the_dashboard(self):
        readme = (REPO_ROOT / "README.md").read_text()

        self.assertIn("/-/dashboards/agent-tokens", readme)

    def test_profile_filter_and_panels_are_registered(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        dashboard = metadata["plugins"]["datasette-dashboards"]["agent-tokens"]

        self.assertEqual(dashboard["filters"]["profile"]["db"], "usage")
        self.assertIn("FROM agent_token_usage", dashboard["filters"]["profile"]["query"])
        for chart in ("tokens_by_profile", "active_profiles", "tokens_by_profile_agent"):
            self.assertIn(chart, dashboard["charts"])
        # The profile panels sit with the workspace ones, not at the bottom of
        # an already long dashboard.
        rows = ["".join(dict.fromkeys(row)) for row in dashboard["layout"]]
        self.assertEqual(
            rows.index("tokens_by_profileactive_profiles")
            - rows.index(
                "tokens_by_workspaceactive_workspaces",
            ),
            2,
        )

    def test_outlier_and_rate_panels_state_what_they_rest_on(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        charts = metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        # An outlier verdict drawn from a guessed rate would be flagging the
        # guess, so this one reads confirmed prices only.
        self.assertIn("is_estimated = 0", charts["outlier_calls"]["query"])
        # The rate panel is the opposite case on purpose: a placeholder
        # replaced by a confirmed rate is itself a pricing change, so it keeps
        # estimated rows and carries the flag to keep the two distinct.
        self.assertIn("r.is_estimated", charts["rate_changes"]["query"])

    def test_usage_canned_queries_cover_profiles(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        queries = metadata["databases"]["usage"]["queries"]

        self.assertIn("GROUP BY profile", queries["profile_token_totals"]["sql"])
        self.assertIn("unknown-pending", queries["profile_resolution"]["sql"])

    def test_filter_visibility_attributes_decisions_to_a_profile(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        charts = metadata["plugins"]["datasette-dashboards"]["http-requests"]["charts"]

        # Which profile a block came from is the point of the panel: without it
        # a shared allow-list miss cannot be traced back to a filter file.
        self.assertIn("decisions_by_profile", charts)
        self.assertIn("r.blocked = 1", charts["decisions_by_profile"]["query"])
        for chart in ("blocked_hosts_table", "passed_hosts_table", "recent_requests"):
            self.assertIn("r.profile", charts[chart]["query"])

    def test_recent_sessions_shows_the_profile_it_ran_under(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        sessions = metadata["plugins"]["datasette-dashboards"]["agent-sessions"]["charts"]

        self.assertIn("f.profile", sessions["recent_sessions"]["query"])


class UsageCachePersistenceTests(unittest.TestCase):
    def test_cache_defaults_next_to_the_mounted_proxy_db(self):
        script = (REPO_ROOT / "scripts/start-datasette.sh").read_text()
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()

        # /data is the container layer: a cache there is lost on every
        # `docker run`, forcing a full re-parse of proxy.db.
        self.assertIn(': "${USAGE_DB_PATH:=}"', script)
        self.assertIn('proxy_dir="$(dirname "$PROXY_DB_PATH")"', script)
        self.assertIn('USAGE_DB_PATH="$proxy_dir/usage.db"', script)
        self.assertIn("USAGE_DB_PATH=/data/usage.db", script)  # read-only fallback
        self.assertNotIn("ENV USAGE_DB_PATH", dockerfile)

    def test_refresher_starts_before_datasette(self):
        script = (REPO_ROOT / "scripts/start-datasette.sh").read_text()

        refresher = script.index("build_usage_cache.py")
        serve = script.index("exec datasette")
        self.assertLess(refresher, serve)
        self.assertIn("--loop", script)


class RuntimeDefaultTests(unittest.TestCase):
    def test_sql_time_limit_default_is_60_seconds_everywhere(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        start_script = (REPO_ROOT / "scripts/start-datasette.sh").read_text()
        readme = (REPO_ROOT / "README.md").read_text()

        self.assertIn("ENV SQL_TIME_LIMIT_MS=60000", dockerfile)
        self.assertIn(': "${SQL_TIME_LIMIT_MS:=60000}"', start_script)
        self.assertIn("- `SQL_TIME_LIMIT_MS` (default `60000`)", readme)


class DashboardMetadataTests(unittest.TestCase):
    def test_proxy_canned_queries_include_codex_websocket_discovery(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        proxy_queries = metadata["databases"]["proxy"]["queries"]

        expected_queries = (
            "codex_ws_recent_messages",
            "codex_ws_message_type_counts",
            "codex_ws_token_field_coverage",
            "codex_ws_usage_event_duplicates",
            "codex_ws_tokens_vs_http_by_request",
        )
        for query_name in expected_queries:
            self.assertIn(query_name, proxy_queries)
            self.assertIn("websocket_messages ws", proxy_queries[query_name]["sql"])
            self.assertIn("/backend-api/codex/%", proxy_queries[query_name]["sql"])


if __name__ == "__main__":
    unittest.main()
