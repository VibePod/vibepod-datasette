"""Datasette plugin: registers HTTP body decompression helpers.

Historically the SQL layer used ``ungzip(blob)`` for request/response bodies.
Keep that name for compatibility, but decode both gzip and Zstandard bodies
when possible. Plain UTF-8 bodies are returned unchanged.

Performance: ``_ungzip`` maintains a small LRU cache keyed on the full body
bytes so repeated calls with the same blob within a single SQL row evaluation
are cheap without risking false cache hits.
"""

import gzip
from collections import OrderedDict

from datasette import hookimpl

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency at runtime
    zstandard = None

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
    return b.decode("utf-8", errors="replace")


def _ungzip(data):
    if data is None:
        return None
    if isinstance(data, bytes):
        b = data
    elif isinstance(data, str):
        b = data.encode("utf-8")
    else:
        b = bytes(data)
    key = b
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


@hookimpl
def prepare_connection(conn):
    conn.create_function("ungzip", 1, _ungzip)
    conn.create_function("decode_body", 1, _ungzip)
    conn.create_function("extract_model", 1, _extract_model)
