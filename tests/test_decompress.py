import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


if importlib.util.find_spec("datasette") is None:
    fake_datasette = types.ModuleType("datasette")
    fake_datasette.hookimpl = lambda fn: fn
    with mock.patch.dict(sys.modules, {"datasette": fake_datasette}):
        decompress = importlib.import_module("plugins.decompress")
else:
    decompress = importlib.import_module("plugins.decompress")


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


class RuntimeDefaultTests(unittest.TestCase):
    def test_sql_time_limit_default_is_60_seconds_everywhere(self):
        dockerfile = Path("Dockerfile").read_text()
        start_script = Path("scripts/start-datasette.sh").read_text()
        readme = Path("README.md").read_text()

        self.assertIn("ENV SQL_TIME_LIMIT_MS=60000", dockerfile)
        self.assertIn(': "${SQL_TIME_LIMIT_MS:=60000}"', start_script)
        self.assertIn("- `SQL_TIME_LIMIT_MS` (default `60000`)", readme)


class DashboardMetadataTests(unittest.TestCase):
    def test_codex_dashboard_includes_websocket_panels(self):
        metadata = json.loads(Path("metadata.json").read_text())
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
        metadata = json.loads(Path("metadata.json").read_text())
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
        metadata = json.loads(Path("metadata.json").read_text())
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
