# -*- coding: utf-8 -*-
"""Web 层通用 helper:统一响应包裹、时钟间接层、参数解析。

now() 通过 `import app as app_mod; app_mod.datetime.now()` 在**调用时**读取 app 模块的
datetime 属性——这样测试对 app_mod.datetime 的 monkeypatch 能穿透到已拆分的路由里
(见 tests/test_api.py 的 _freeze_now_weekday)。
"""
from flask import jsonify

from core import data_source as ds
from web import config


def ok(data, stale=False, extra_meta=None):
    meta = {"stale": stale, "updated_at": ds.last_updated_at}
    if extra_meta:
        meta.update(extra_meta)
    return jsonify({"ok": True, "meta": meta, "data": data})


def err(code, message, http):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), http


def now():
    import app as app_mod
    return app_mod.datetime.now()


def today():
    return now().strftime("%Y-%m-%d")


def _num(v):
    """None/NaN/非数值 → None,其余 → float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sort_key(v):
    """数值排序键:None/非数值排最后。"""
    n = _num(v)
    return float("-inf") if n is None else n


def _parse_sector_code(raw):
    if not raw or ":" not in raw:
        return None
    t, c = raw.split(":", 1)
    if t not in config.SECTOR_TYPES or not (c.isdigit() and len(c) == 6):
        return None
    return t, c


def _parse_stock_code(raw):
    if not raw:
        return None
    c = ds.normalize_code(raw)
    if not (c.isdigit() and len(c) == 6):
        return None
    return c
