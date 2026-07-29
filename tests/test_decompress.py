import gzip
import importlib
import json
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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
                ]
            ).encode()
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
                }
            }
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
            ]
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
                    }
                },
            }
        ).encode()

        usage = _usage(body, "chatgpt.com")

        self.assertEqual(usage["provider"], "openai-codex")
        self.assertEqual((usage["input"], usage["output"]), (700, 90))
        self.assertEqual((usage["cached"], usage["reasoning"]), (600, 30))

    def test_body_without_usage_is_reported_as_not_found(self):
        usage = _usage(b'data: {"choices":[{"delta":{"content":"hi"}}]}', "api.githubcopilot.com")

        self.assertEqual(usage["found"], 0)
        self.assertEqual(usage["input"], 0)
        self.assertEqual(usage["provider"], "github-copilot")

    def test_unknown_host_keeps_host_as_provider_label(self):
        self.assertEqual(_usage(b"{}", "llm.internal.example")["provider"], "llm.internal.example")
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
        self.assertEqual(decompress._ungzip(b"<html>not an api call</html>"), "<html>not an api call</html>")

    @unittest.skipIf(decompress.brotli is None, "brotli not installed")
    def test_brotli_bodies_decode(self):
        payload = json.dumps({"usage": {"input_tokens": 11, "output_tokens": 3}}).encode()
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
        "limit": 200,
        "request_limit": 100,
    }

    SCHEMA = """
    CREATE TABLE http_requests (id TEXT PRIMARY KEY, timestamp TEXT, method TEXT,
        source_container_id TEXT, source_container_name TEXT, scheme TEXT, host TEXT,
        port INTEGER, path TEXT, query TEXT, url TEXT, headers TEXT, body BLOB,
        client_ip TEXT, client_port INTEGER, server_ip TEXT, server_port INTEGER);
    CREATE TABLE http_responses (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, status_code INTEGER, headers TEXT, body BLOB, bytes_in INTEGER,
        bytes_out INTEGER, duration_ms REAL);
    CREATE TABLE websocket_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
        timestamp TEXT, direction TEXT, type TEXT, content BLOB);
    """

    def setUp(self):
        decompress._decode_cache.clear()
        decompress._usage_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy_path = Path(self.tmp.name) / "proxy.db"
        self.usage_path = Path(self.tmp.name) / "usage.db"
        self.source = sqlite3.connect(self.proxy_path)
        self.addCleanup(self.source.close)
        self.source.executescript(self.SCHEMA)
        self.metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        self._seed()
        self.refresh()

    def refresh(self):
        counts = build_usage_cache.build(self.proxy_path, self.usage_path)
        self.conn = sqlite3.connect(self.usage_path)
        self.addCleanup(self.conn.close)
        return counts

    def _request(self, rid, host, path, container, model, ts="2026-07-26T10:00:00+00:00"):
        self.source.execute(
            "INSERT INTO http_requests (id, timestamp, method, source_container_name, host, "
            "path, body) VALUES (?, ?, 'POST', ?, ?, ?, ?)",
            (rid, ts, container, host, path, json.dumps({"model": model}).encode()),
        )

    def _response(self, rid, body, ts="2026-07-26T10:00:01+00:00"):
        self.source.execute(
            "INSERT INTO http_responses (request_id, timestamp, status_code, body) "
            "VALUES (?, ?, 200, ?)",
            (rid, ts, body),
        )

    def _seed(self):
        self._request("r1", "api.anthropic.com", "/v1/messages", "vibepod-claude-abc1", "opus")
        self._response(
            "r1",
            gzip.compress(
                b'data: {"type":"message_start","message":{"usage":'
                b'{"input_tokens":1200,"cache_read_input_tokens":900,'
                b'"cache_creation_input_tokens":100}}}\n'
                b'data: {"type":"message_delta","usage":{"output_tokens":300}}'
            ),
        )

        self._request("r2", "api.groq.com", "/openai/v1/chat/completions", "vibepod-tau-abc2", "l3")
        self._response("r2", json.dumps({"usage": {"prompt_tokens": 80, "completion_tokens": 20}}).encode())

        # Copilot call without usage: must not silently count as zero tokens.
        self._request("r3", "api.githubcopilot.com", "/chat/completions", "vibepod-copilot-abc3", "x")
        self._response("r3", b'data: {"choices":[{"delta":{"content":"hi"}}]}')

        # Codex: usage arrives on the websocket, the HTTP row must be skipped.
        self._request("r4", "chatgpt.com", "/backend-api/codex/responses", "vibepod-codex-abc4", "c")
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
                        "response": {"usage": {"input_tokens": 700, "output_tokens": 90}},
                    }
                ).encode(),
            ),
        )
        self.source.commit()

    def _rows(self, sql):
        return self.conn.execute(sql, self.PARAMS).fetchall()

    def test_totals_sum_every_agent_once(self):
        sql = self.metadata["databases"]["usage"]["queries"]["agent_token_totals"]["sql"]

        totals = {row[0]: row for row in self._rows(sql)}

        self.assertEqual(sorted(totals), ["claude", "codex", "tau"])
        self.assertEqual(totals["claude"][7], 1500)
        self.assertEqual(totals["tau"][7], 100)
        # One row only: the HTTP twin of the websocket call must not be counted.
        self.assertEqual(totals["codex"][1], 1)
        self.assertEqual(totals["codex"][7], 790)

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

        total = self.conn.execute(charts["total_tokens"]["query"], self.PARAMS).fetchone()[0]
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
            with self.subTest(chart=name):
                expected = " calls" if name == "calls_with_usage" else " tok"
                self.assertEqual(chart["display"]["suffix"], expected)

    def test_cache_write_card_sums_cache_creation_tokens(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        written = self.conn.execute(
            charts["total_cache_write_tokens"]["query"], self.PARAMS
        ).fetchone()[0]
        cached = self.conn.execute(
            charts["total_cached_tokens"]["query"], self.PARAMS
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
            charts[chart_name]["query"], dict(self.PARAMS, **params)
        ).fetchall()

    def _seed_usage_call(self, rid, ts, prompt_tokens, completion_tokens, container="vibepod-tau-hm"):
        self._request(rid, "api.groq.com", "/openai/v1/chat/completions", container, "l3", ts=ts)
        self._response(
            rid,
            json.dumps(
                {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}
            ).encode(),
            ts=ts,
        )

    def test_daily_heatmaps_group_tokens_per_day(self):
        day1 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00+00:00")
        day2 = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00")
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
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm4", ts, 5, 1)
        self.source.commit()
        self.refresh()

        # Always the full dense year grid, whatever range is selected.
        for time_range in ("1h", "24h", "7d", "30d", "3m", "6m", "1y", "all"):
            with self.subTest(time_range=time_range):
                rows = self._heatmap_rows("daily_input_heatmap", time_range=time_range)
                self.assertEqual(len(rows), 366)
                self.assertIn(ts[:10], [row[0] for row in rows])

    def test_time_range_dropdown_offers_long_ranges(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        self.assertEqual(
            filters["time_range"]["options"],
            ["1h", "24h", "7d", "30d", "3m", "6m", "1y", "all"],
        )

    def test_long_ranges_extend_existing_charts(self):
        # Dedicated agent so static seeds can't drift between buckets over time.
        ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm6", ts, 11, 3, container="vibepod-hmx-1")
        self.source.commit()
        self.refresh()

        query = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]["total_tokens"]["query"]
        within_30d = self.conn.execute(query, dict(self.PARAMS, time_range="30d", agent="hmx")).fetchone()[0]
        within_3m = self.conn.execute(query, dict(self.PARAMS, time_range="3m", agent="hmx")).fetchone()[0]

        self.assertEqual(within_30d, 0)
        self.assertEqual(within_3m, 14)

    def test_heatmaps_respect_agent_filter(self):
        # days=2 keeps this clear of the static 2026-07-26 claude seed: the
        # zero-token assertion below would fail on the one day now-2d lands on
        # it, and that date is already in the past.
        ts = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00+00:00")
        self._seed_usage_call("hm5", ts, 9, 2)
        self.source.commit()
        self.refresh()

        claude_rows = {row[0]: row for row in self._heatmap_rows("daily_input_heatmap", agent="claude")}
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
            charts["token_trend"]["query"], dict(self.PARAMS, **params)
        ).fetchall()
        return sorted({row[0] for row in rows})

    def test_five_minute_buckets_floor_to_the_slot(self):
        # 10:00:00 and 10:03:30 share a slot; 10:07:00 starts the next one.
        self._request("t1", "api.groq.com", "/v1/chat", "vibepod-tau-t1", "l3",
                      ts="2026-07-26T10:03:30+00:00")
        self._response("t1", json.dumps({"usage": {"prompt_tokens": 5}}).encode())
        self._request("t2", "api.groq.com", "/v1/chat", "vibepod-tau-t2", "l3",
                      ts="2026-07-26T10:07:00+00:00")
        self._response("t2", json.dumps({"usage": {"prompt_tokens": 5}}).encode())
        self.source.commit()
        self.refresh()

        buckets = self._trend_buckets(time_bucket="5min")

        self.assertIn("2026-07-26 10:00:00", buckets)
        self.assertIn("2026-07-26 10:05:00", buckets)
        self.assertNotIn("2026-07-26 10:03:00", buckets)

    def test_auto_bucket_uses_five_minutes_for_one_hour_range(self):
        # Seed a call inside the last hour so the 1h window actually has data.
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        self._request("a1", "api.groq.com", "/v1/chat", "vibepod-tau-a1", "l3",
                      ts=recent.isoformat())
        self._response("a1", json.dumps({"usage": {"prompt_tokens": 5}}).encode(),
                       ts=recent.isoformat())
        self.source.commit()
        self.refresh()

        buckets = self._trend_buckets(time_bucket="auto", time_range="1h")

        slot = recent.replace(second=0, microsecond=0)
        slot -= timedelta(minutes=slot.minute % 5)
        self.assertEqual(buckets, [slot.strftime("%Y-%m-%d %H:%M:%S")])

    def test_bucket_filter_offers_five_minute_option(self):
        filters = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["filters"]

        self.assertEqual(filters["time_bucket"]["options"], ["auto", "5min", "hour", "day"])

    def test_bar_charts_split_input_and_output_series(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        rows = self.conn.execute(charts["tokens_by_agent"]["query"], self.PARAMS).fetchall()

        by_key = {(row[0], row[1]): row[2] for row in rows}
        self.assertEqual(by_key[("claude", "input")], 1200)
        self.assertEqual(by_key[("claude", "output")], 300)
        self.assertEqual(by_key[("codex", "input")], 700)
        self.assertEqual(by_key[("codex", "output")], 90)
        # Stacked bars need one row per series, never a pre-summed total.
        self.assertEqual({row[1] for row in rows}, {"input", "output"})

    def test_provider_bar_chart_groups_by_provider(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]

        rows = self.conn.execute(charts["tokens_by_provider"]["query"], self.PARAMS).fetchall()

        by_key = {(row[0], row[1]): row[2] for row in rows}
        self.assertEqual(by_key[("anthropic", "input")], 1200)
        self.assertEqual(by_key[("groq", "output")], 20)

    def test_provider_filter_narrows_every_chart(self):
        charts = self.metadata["plugins"]["datasette-dashboards"]["agent-tokens"]["charts"]
        params = dict(self.PARAMS, provider="anthropic")

        total = self.conn.execute(charts["total_tokens"]["query"], params).fetchone()[0]
        agents = self.conn.execute(charts["tokens_by_agent"]["query"], params).fetchall()

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
            }
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
            "SELECT COUNT(*) FROM token_usage WHERE response_id = 'resp_dup'"
        ).fetchone()[0]
        counted = self.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens) FROM agent_token_usage WHERE response_id = 'resp_dup'"
        ).fetchone()

        self.assertEqual(stored, 2)
        self.assertEqual(counted, (1, 500))

    def test_duplicate_dedup_keeps_the_fullest_snapshot(self):
        for tokens, ts in ((10, "2026-07-26T12:00:00+00:00"), (900, "2026-07-26T12:00:02+00:00")):
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
                        }
                    ).encode(),
                ),
            )
        self.source.commit()
        self.refresh()

        kept = self.conn.execute(
            "SELECT input_tokens FROM agent_token_usage WHERE response_id = 'resp_partial'"
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
                        }
                    ).encode(),
                ),
            )
        self.source.commit()
        self.refresh()

        turns = self.conn.execute(
            "SELECT COUNT(*), SUM(input_tokens) FROM agent_token_usage "
            "WHERE response_id LIKE 'resp_turn_%'"
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
                    }
                ).encode(),
            ),
        )
        self.source.commit()
        self.refresh()

        model = self.conn.execute(
            "SELECT model FROM agent_token_usage WHERE response_id = 'resp_model'"
        ).fetchone()[0]

        self.assertEqual(model, "gpt-5-codex-high")

    def test_schema_upgrade_rebuilds_an_older_cache(self):
        legacy = Path(self.tmp.name) / "legacy-usage.db"
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE token_usage (source TEXT, row_id INTEGER, request_id TEXT)")
        conn.execute("CREATE TABLE sync_state (source TEXT PRIMARY KEY, last_row_id INTEGER, updated_at TEXT)")
        conn.execute("INSERT INTO sync_state VALUES ('http', 999, '2026-01-01')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        counts = build_usage_cache.build(self.proxy_path, legacy)

        conn = sqlite3.connect(legacy)
        self.addCleanup(conn.close)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(token_usage)")]
        self.assertIn("response_id", columns)
        self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 2)
        # The stale watermark was dropped, so everything is re-parsed.
        self.assertGreater(counts["http"], 0)

    def test_refresh_only_processes_new_rows(self):
        again = build_usage_cache.build(self.proxy_path, self.usage_path)

        self.assertEqual(again, {"http": 0, "ws": 0})

        self._request("r5", "api.groq.com", "/v1/chat", "vibepod-tau-abc5", "l3")
        self._response("r5", json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode())
        self.source.commit()

        counts = self.refresh()

        self.assertEqual(counts["http"], 1)
        rows = self.conn.execute(
            "SELECT SUM(input_tokens + output_tokens) FROM agent_token_usage WHERE agent = 'tau'"
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
            "SELECT COUNT(*) FROM token_usage WHERE request_id = 'r4'"
        ).fetchone()[0]
        deduped = self.conn.execute(
            "SELECT source FROM agent_token_usage WHERE request_id = 'r4'"
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
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0], 0)

    def test_cache_state_query_reports_watermarks(self):
        sql = self.metadata["databases"]["usage"]["queries"]["usage_cache_state"]["sql"]

        state = {row[0]: row for row in self.conn.execute(sql).fetchall()}

        self.assertEqual(sorted(state), ["http", "ws"])
        self.assertGreater(state["http"][1], 0)
        self.assertEqual(state["http"][3], 4)

    def test_agent_name_parsing(self):
        self.assertEqual(build_usage_cache.agent_from_container("vibepod-claude-a1b2"), "claude")
        self.assertEqual(build_usage_cache.agent_from_container("vibepod-tau"), "tau")
        self.assertEqual(build_usage_cache.agent_from_container("other-container"), "other-container")
        self.assertEqual(build_usage_cache.agent_from_container(""), "unknown")
        self.assertEqual(build_usage_cache.agent_from_container(None), "unknown")


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
                self.assertIn("FROM agent_token_usage", chart["query"])
                self.assertNotIn("extract_usage(", chart["query"])

    def test_agent_tokens_is_listed_first_and_legacy_dashboards_last(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())

        # datasette-dashboards renders the index in metadata key order, so this
        # ordering is what users see. claude-tokens/codex-tokens are superseded
        # by agent-tokens and sit at the bottom pending deprecation.
        order = list(metadata["plugins"]["datasette-dashboards"])

        self.assertEqual(order[0], "agent-tokens")
        self.assertEqual(order[-2:], ["claude-tokens", "codex-tokens"])

    def test_readme_documents_the_dashboard(self):
        readme = (REPO_ROOT / "README.md").read_text()

        self.assertIn("/-/dashboards/agent-tokens", readme)


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
    def test_codex_dashboard_includes_websocket_panels(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        codex = metadata["plugins"]["datasette-dashboards"]["codex-tokens"]

        self.assertIn("backend_codex_ws", codex["filters"]["api_shape"]["options"])

        expected_charts = (
            "total_websocket_messages",
            "websocket_direction_trend",
            "recent_websocket_messages",
        )
        for chart in expected_charts:
            self.assertIn(chart, codex["charts"])
            self.assertIn("websocket_messages ws", codex["charts"][chart]["query"])
            self.assertIn("/backend-api/codex/%", codex["charts"][chart]["query"])

        # Keep codex dashboard grid complete (3 columns per row) and place
        # the websocket metric in the former avg-tokens slot.
        self.assertTrue(all(len(row) == 3 for row in codex["layout"]))
        self.assertEqual(codex["layout"][1][2], "total_websocket_messages")

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

    def test_codex_dashboard_uses_ws_completed_with_http_fallback(self):
        metadata = json.loads((REPO_ROOT / "metadata.json").read_text())
        codex = metadata["plugins"]["datasette-dashboards"]["codex-tokens"]

        token_chart_queries = (
            "total_calls",
            "total_input_tokens",
            "total_output_tokens",
            "total_cached_tokens",
            "total_reasoning_tokens",
            "token_trend",
            "model_breakdown",
            "recent_calls",
        )

        for chart in token_chart_queries:
            query = codex["charts"][chart]["query"]

            self.assertIn("ws_calls AS", query)
            self.assertIn("http_calls AS", query)
            self.assertIn("FROM websocket_messages ws", query)
            self.assertIn("= 'response.completed'", query)
            self.assertIn("r.path LIKE '/backend-api/codex/%'", query)
            self.assertIn("r.path LIKE '/backend-api/codex/responses%'", query)

            # HTTP is kept as fallback but excluded when websocket completion exists.
            self.assertIn("NOT (", query)
            self.assertIn("EXISTS (", query)
            self.assertIn("ws2.request_id = r.id", query)

        self.assertNotIn("avg_tokens_per_call", codex["charts"])


if __name__ == "__main__":
    unittest.main()
