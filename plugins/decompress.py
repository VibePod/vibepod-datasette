"""Datasette plugin: registers ungzip(blob) as a SQLite scalar function.

Returns the gzip-decompressed text of a blob, or the blob decoded as UTF-8
if it is not gzip-compressed. Returns NULL for NULL input.
"""

import gzip

from datasette import hookimpl


def _ungzip(data):
    if data is None:
        return None
    b = bytes(data) if not isinstance(data, (bytes, bytearray)) else data
    if b[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(b).decode("utf-8", errors="replace")
        except Exception:
            pass
    return b.decode("utf-8", errors="replace")


@hookimpl
def prepare_connection(conn):
    conn.create_function("ungzip", 1, _ungzip)
