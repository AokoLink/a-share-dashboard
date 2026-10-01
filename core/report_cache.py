"""Bounded cache for immutable/background reports; never performs acquisition."""
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=64)
def _load(path, modified, size, expected):
    content = Path(path).read_bytes()
    if expected and hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("report_hash_mismatch")
    return json.loads(content.decode("utf-8"))


def read(path, expected=None):
    path = Path(path).resolve()
    stat = path.stat()
    return copy.deepcopy(_load(str(path), stat.st_mtime_ns, stat.st_size, expected))
