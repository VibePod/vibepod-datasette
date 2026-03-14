# Codex Websocket Findings (2026-03-13)

This document captures the SQL investigation used to update the `codex-tokens` dashboard token accounting.

## Data Findings

- Codex websocket traffic exists in `proxy.websocket_messages` for `chatgpt.com/backend-api/codex/%`.
- Usage tokens are present on websocket `response.completed` messages.
- `response.create` and `response.completed` events pair by sequence for websocket-backed Codex sessions.
- A single HTTP `request_id` can contain many websocket completions (multiple model calls), so counting only HTTP requests undercounts calls.
- For the analyzed data:
  - websocket completed calls: `36`
  - HTTP usage on requests with websocket traffic: `0`
  - HTTP usage appears on requests without websocket traffic.

## Token Semantics Observed

- `response.completed` usage is snapshot-style and may go up or down between consecutive calls.
- Negative step deltas occur, so a naive delta-only model is unstable.
- The most reliable per-call accounting source is each `response.completed` event itself.

## Dashboard Rules Implemented

- Use websocket `response.completed` rows as the primary Codex call source for `chatgpt.com/backend-api/codex/%`.
- Extract usage from websocket payload fields:
  - input/prompt tokens
  - output/completion tokens
  - cached tokens
  - reasoning tokens
- Use HTTP request/response usage as fallback for non-websocket Codex/OpenAI calls.
- Prevent double counting by excluding HTTP rows for backend Codex requests when websocket `response.completed` exists for the same `request_id`.
- Keep dashboard filters (`time_range`, `model`, `container`, `api_shape`) applied consistently across websocket and HTTP sources.

## Validation Queries Used

The proxy canned queries in `metadata.json` used during this investigation:

- `codex_ws_recent_messages`
- `codex_ws_message_type_counts`
- `codex_ws_token_field_coverage`
- `codex_ws_usage_event_duplicates`
- `codex_ws_tokens_vs_http_by_request`
