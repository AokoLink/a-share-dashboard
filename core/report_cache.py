"""Bounded cache for immutable/background reports; never performs acquisition."""
import copy
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from threading import RLock


CACHE_BYTES = 8 * 1024 * 1024
ITEM_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 64
_cache = OrderedDict()
_bytes = 0
_lock = RLock()


def read(path, expected=None, *, project=None, projection_key=None):
    """Cache small views, not whole large runs. Verify signed bytes on every read.

    Unsigned mutable reports use file metadata for invalidation. A projection key
    names its schema and parameters, so different pages cannot share a result.
    Budgets measure serialized JSON bytes; Python objects have extra overhead.
    """
    global _bytes
    if project is not None and not projection_key:
        raise ValueError("projection_key_required")
    path = Path(path).resolve()
    stat = path.stat()
    content = None
    if expected:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("report_hash_mismatch")
    key = (str(path), stat.st_mtime_ns, stat.st_size, expected, projection_key)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return copy.deepcopy(_cache[key][0])
    value = json.loads((content if content is not None else path.read_bytes()).decode("utf-8"))
    if project is not None:
        value = project(value)
    size = stat.st_size if project is None and stat.st_size > ITEM_BYTES else len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size <= ITEM_BYTES:
        with _lock:
            # A second concurrent reader may have filled the same key.
            previous = _cache.pop(key, None)
            if previous:
                _bytes -= previous[1]
            while _cache and (_bytes + size > CACHE_BYTES or len(_cache) >= MAX_ITEMS):
                _, (_, removed) = _cache.popitem(last=False)
                _bytes -= removed
            _cache[key] = (value, size)
            _bytes += size
    return copy.deepcopy(value)


def cache_info():
    with _lock:
        return {"items": len(_cache), "serialized_bytes": _bytes,
                "max_items": MAX_ITEMS, "max_serialized_bytes": CACHE_BYTES}
