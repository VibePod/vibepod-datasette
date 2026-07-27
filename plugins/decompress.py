"""Datasette plugin: registers HTTP body decompression and usage helpers.

Historically the SQL layer used ``ungzip(blob)`` for request/response bodies.
Keep that name for compatibility, but decode gzip, Zstandard and Brotli bodies
when possible. Plain UTF-8 bodies are returned unchanged.

``extract_usage(body, host)`` normalizes token accounting across providers so a
single query can total tokens for every agent. Each provider names the same
numbers differently (``input_tokens`` vs ``prompt_tokens`` vs
``promptTokenCount``), and streamed responses repeat cumulative usage in many
events, so values are merged with ``max`` per field rather than summed.

Performance: both helpers keep small LRU caches keyed on a digest of the full
body bytes so repeated calls with the same blob within a single SQL row
evaluation are cheap without holding every cached body in memory.
"""

import gzip
import hashlib
import json
from collections import OrderedDict

from datasette import hookimpl

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency at runtime
    zstandard = None

try:
    import brotli
except ImportError:  # pragma: no cover - optional dependency at runtime
    try:
        import brotlicffi as brotli
    except ImportError:
        brotli = None

# Small LRU cache: 8 slots is enough to cover both request + response bodies
# referenced multiple times in the same row without unbounded memory growth.
_CACHE_SIZE = 8
_decode_cache: OrderedDict = OrderedDict()


def _decompress(b: bytes) -> str:
    """Decompress bytes (gzip / zstd / plain) and return as UTF-8 string."""
    if b[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(b).decode("utf-8", errors="replace")
        except Exception:
            pass
    if b[:4] == b"\x28\xb5\x2f\xfd" and zstandard is not None:
        try:
            dctx = zstandard.ZstdDecompressor()
            try:
                return dctx.decompress(b).decode("utf-8", errors="replace")
            except zstandard.ZstdError:
                # Streaming frame without embedded content size — use stream_reader
                with dctx.stream_reader(b) as reader:
                    return reader.read().decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        text = b.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    # Brotli streams carry no magic bytes, so decide by what the plain decode
    # produced: a body that already looks like JSON or an SSE stream is taken as
    # is, anything else is worth a Brotli attempt before giving up. A clean
    # plain-text decode is only replaced when the Brotli result is a recognized
    # payload, since arbitrary text can happen to be a valid Brotli stream.
    if brotli is not None and not _looks_like_payload(text):
        try:
            decoded = brotli.decompress(b).decode("utf-8", errors="replace")
        except Exception:
            pass
        else:
            if text is None or _looks_like_payload(decoded):
                return decoded
    if text is not None:
        return text
    return b.decode("utf-8", errors="replace")


def _looks_like_payload(text) -> bool:
    """True when text is already a readable JSON / SSE / NDJSON body."""
    if not text:
        return False
    head = text.lstrip()[:16]
    return head.startswith(("{", "[", "data:", "event:"))


def _ungzip(data):
    if data is None:
        return None
    if isinstance(data, bytes):
        b = data
    elif isinstance(data, str):
        b = data.encode("utf-8")
    else:
        b = bytes(data)
    key = hashlib.sha256(b).digest()
    if key in _decode_cache:
        _decode_cache.move_to_end(key)
        return _decode_cache[key]
    result = _decompress(b)
    _decode_cache[key] = result
    if len(_decode_cache) > _CACHE_SIZE:
        _decode_cache.popitem(last=False)
    return result


def _extract_model(data):
    """Decompress body once and extract model/model_slug in a single pass."""
    import json as _json

    text = _ungzip(data)
    if not text:
        return ""
    try:
        obj = _json.loads(text)
        model = obj.get("model") or obj.get("model_slug")
        if model:
            return str(model)
    except Exception:
        pass
    for key, offset in [
        ('"model":"', 9),
        ('"model": "', 10),
        ('"model_slug":"', 14),
        ('"model_slug": "', 15),
    ]:
        idx = text.find(key)
        if idx >= 0:
            start = idx + offset
            end = text.find('"', start)
            if end > start:
                return text[start:end]
    return ""


# Host substring -> provider label. First match wins, so keep the more specific
# hosts above the generic ones.
_PROVIDER_HOSTS = (
    ("api.anthropic.com", "anthropic"),
    ("chatgpt.com", "openai-codex"),
    ("api.openai.com", "openai"),
    ("openrouter.ai", "openrouter"),
    ("githubcopilot.com", "github-copilot"),
    ("cloudcode-pa.googleapis.com", "google"),
    ("generativelanguage.googleapis.com", "google"),
    ("api.mistral.ai", "mistral"),
    ("api.groq.com", "groq"),
    ("api.deepseek.com", "deepseek"),
    ("api.x.ai", "xai"),
    ("huggingface.co", "huggingface"),
    ("augmentcode.com", "augment"),
)

# Normalized field -> the provider-specific paths that carry it. Paths are
# tuples of nested keys inside a usage object.
_USAGE_FIELDS = {
    "input": (
        ("input_tokens",),
        ("prompt_tokens",),
        ("promptTokenCount",),
    ),
    "output": (
        ("output_tokens",),
        ("completion_tokens",),
        ("candidatesTokenCount",),
    ),
    "cached": (
        ("cache_read_input_tokens",),
        ("input_tokens_details", "cached_tokens"),
        ("prompt_tokens_details", "cached_tokens"),
        ("cachedContentTokenCount",),
    ),
    "cache_write": (("cache_creation_input_tokens",),),
    "reasoning": (
        ("output_tokens_details", "reasoning_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
        ("thoughtsTokenCount",),
    ),
}

# Where a usage object can sit inside an event payload.
_USAGE_CONTAINERS = (
    ("usage",),
    ("usageMetadata",),
    ("response", "usage"),
    ("response", "usageMetadata"),
    ("message", "usage"),
    ("delta", "usage"),
)

_USAGE_CACHE_SIZE = 32
_usage_cache: OrderedDict = OrderedDict()


def _provider_for_host(host):
    lowered = str(host or "").lower()
    for needle, label in _PROVIDER_HOSTS:
        if needle in lowered:
            return label
    return lowered or "unknown"


def _dig(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _merge_usage(usage, totals):
    """Merge one usage object into ``totals`` keeping the largest value seen.

    Streaming APIs emit cumulative usage repeatedly (Anthropic sends the running
    ``output_tokens`` in every ``message_delta``, Google repeats
    ``usageMetadata`` per chunk), so max is correct where sum would multiply.
    """
    found = False
    for field, paths in _USAGE_FIELDS.items():
        for path in paths:
            value = _dig(usage, path)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            found = True
            if value > totals[field]:
                totals[field] = int(value)
            break
    return found


def _collect_usage(obj, totals):
    if not isinstance(obj, dict):
        return False
    found = False
    for path in _USAGE_CONTAINERS:
        usage = _dig(obj, path)
        if isinstance(usage, dict) and _merge_usage(usage, totals):
            found = True
    return found


# Where the model name and the call identifier live, per provider event shape.
# Codex websocket frames carry both on `$.response`, and the HTTP request body
# of a websocket upgrade has no model at all, so the frame has to win.
_MODEL_PATHS = (
    ("response", "model"),
    ("model",),
    ("message", "model"),
)

_RESPONSE_ID_PATHS = (
    ("response", "id"),
    ("message", "id"),
    ("id",),
)


def _collect_identity(obj, identity):
    """Record the first model / response id seen in an event stream."""
    if not isinstance(obj, dict):
        return
    for field, paths in (("model", _MODEL_PATHS), ("response_id", _RESPONSE_ID_PATHS)):
        if identity[field]:
            continue
        for path in paths:
            value = _dig(obj, path)
            if isinstance(value, str) and value:
                identity[field] = value
                break


def _iter_events(text):
    """Yield JSON objects from a plain body, an SSE stream, or NDJSON."""
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            yield json.loads(stripped)
            return
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _extract_usage(data, host=None):
    """Return normalized token usage for a response body as a JSON string.

    Output keys: ``provider``, ``model``, ``response_id``, ``input``,
    ``output``, ``cached``, ``cache_write``, ``reasoning``, ``found``.
    ``found`` is 0 when no usage could be parsed, which lets dashboards
    separate "zero tokens" from "not captured" instead of silently
    under-reporting.

    ``response_id`` exists so callers can drop duplicate usage events for the
    same logical call. Tools that count from Codex session logs have hit
    exactly this: duplicate usage snapshots inflated their totals until they
    deduplicated (ccusage issue #884).
    """
    if isinstance(data, (bytes, bytearray, memoryview)):
        key = (hashlib.sha256(bytes(data)).digest(), host)
    elif isinstance(data, str):
        key = (hashlib.sha256(data.encode("utf-8", errors="replace")).digest(), host)
    else:
        key = (data, host)
    try:
        cached = _usage_cache.get(key)
    except TypeError:  # pragma: no cover - unhashable body type
        cached = None
        key = None
    if cached is not None:
        if key is not None:
            _usage_cache.move_to_end(key)
        return cached

    totals = {field: 0 for field in _USAGE_FIELDS}
    found = False
    identity = {"model": "", "response_id": ""}
    text = _ungzip(data) if data is not None else ""
    if text:
        for event in _iter_events(text):
            _collect_identity(event, identity)
            if _collect_usage(event, totals):
                found = True

    result = json.dumps(
        {
            "provider": _provider_for_host(host),
            "model": identity["model"],
            "response_id": identity["response_id"],
            "input": totals["input"],
            "output": totals["output"],
            "cached": totals["cached"],
            "cache_write": totals["cache_write"],
            "reasoning": totals["reasoning"],
            "found": 1 if found else 0,
        }
    )
    if key is not None:
        _usage_cache[key] = result
        if len(_usage_cache) > _USAGE_CACHE_SIZE:
            _usage_cache.popitem(last=False)
    return result


@hookimpl
def prepare_connection(conn):
    conn.create_function("ungzip", 1, _ungzip)
    conn.create_function("decode_body", 1, _ungzip)
    conn.create_function("extract_model", 1, _extract_model)
    conn.create_function("extract_usage", 2, _extract_usage)
