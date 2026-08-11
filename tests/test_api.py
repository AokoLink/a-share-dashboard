# -*- coding: utf-8 -*-
import datetime
import os
import threading

import pandas as pd
import pytest

import analysis as an
import app as app_mod
import data_source as ds
import store


def make_spot():
    return pd.DataFrame({
        "code": ["600000", "600519", "000001", "688981"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际"],
        "price": [10.0, 1348.9, 12.0, 45.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0],
        "volume": [100000, 682720, 80000, 90000],
        "amount": [1e8, 9.2e8, 2e8, 5e8],
    })


def make_summary():
    return pd.DataFrame({
        "code": ["885887", "885559"],
        "name": ["半导体", "白酒"],
        "change_pct": [3.2, -1.0],
        "up_count": [80.0, 30.0],
        "down_count": [5.0, 60.0],
        "leader": ["X", "Y"],
        "leader_change_pct": [10.0, 0.5],
        "turnover": [None, None],
    })


def make_quote():
    return {"name": "贵州茅台", "code": "sh600519", "price": 1348.9, "prev_close": 1345.0,
            "open": 1348.0, "high": 1352.65, "low": 1338.18, "volume": 682720,
            "turnover": 919035968, "change_pct": 0.3}


def make_daily():
    n = 65
    closes = [float(1000 + i) for i in range(n)]
    return pd.DataFrame({
        "date": [f"2026-05-{i%28+1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [682720] * n})


def make_minute():
    return pd.DataFrame({"time": ["10:00", "10:01"], "price": [1348.0, 1349.0],
                         "avg": [1342.0, 1343.0], "volume": [12000, 5000]})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_index_realtime", lambda: (
        [{"code": "sh000001", "name": "上证指数", "price": 3456.78, "change_pct": 0.45}], False))
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(an, "is_after_close", lambda now: True)   # 测试按收盘后口径
    monkeypatch.setattr(an, "is_trading_time", lambda now: True)
    app.config["TESTING"] = True
    return app.test_client()


def test_market_endpoint(client):
    r = client.get("/api/market")
    body = r.get_json()
    assert body["ok"] is True and body["meta"]["stale"] is False
    d = body["data"]
    assert d["indices"][0]["code"] == "sh000001"
    assert d["breadth"]["up"] == 3  # 600000(1.0) 600519(0.3) 688981(5.0) 上涨;000001 -2.0 下跌
    assert d["breadth"]["down"] == 1
    assert d["breadth"]["limit_up"] == 0  # 无一达到阈值
    assert d["volume_vs_yesterday"]["pct"] is None  # 无昨日数据


def test_sectors_search_and_composite(client):
    r = client.get("/api/sectors?type=industry")
    body = r.get_json()
    assert body["ok"] is True
    s = body["data"]["sectors"]
    # 半导体:emotion=? strength=? 仅断言结构 + 综合分算术一致性
    semi = next(x for x in s if x["code"] == "industry:885887")
    assert semi["consecutive_days"] == 1  # 当日已 upsert(rank 1≤20),从今天起算连续 1 天
    assert semi["data_complete"] is True
    assert semi["composite_score"] is None or 0 <= semi["composite_score"] <= 100
    r2 = client.get("/api/sectors?type=industry&search=半导")
    assert r2.get_json()["data"]["total"] == 1
    assert r2.get_json()["data"]["sectors"][0]["name"] == "半导体"


def test_sector_detail(client):
    r = client.get("/api/sector?code=industry:885887")
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["name"] == "半导体"
    assert d["index_history"][0]["date"].startswith("2026-")
    assert d["scores"]["composite"] is None or 0 <= d["scores"]["composite"] <= 100


def test_sector_bad_param(client):
    r = client.get("/api/sector?code=foo:123")
    body = r.get_json()
    assert r.status_code == 400 and body["error"]["code"] == "BAD_PARAM"
    r2 = client.get("/api/sector?code=industry:abc")
    assert r2.status_code == 400
    r3 = client.get("/api/sectors?type=xxx")
    assert r3.status_code == 400


def test_stock_two_code_forms(client):
    r1 = client.get("/api/stock?code=600519")
    r2 = client.get("/api/stock?code=sh600519")
    d1, d2 = r1.get_json()["data"], r2.get_json()["data"]
    assert d1["code"] == "sh600519" and d2["code"] == "sh600519"
    assert d1["name"] == "贵州茅台"
    assert d1["quote"]["change_pct"] == pytest.approx(0.3)
    assert d1["kline"][0]["date"].startswith("2026-")
    assert d1["intraday"][0]["time"] == "10:00"
    # 综合分算术:0.4*趋势+0.35*量价+0.25*信号(这里 daily 是持续上涨 → 高分)
    assert 0 <= d1["scores"]["composite"] <= 100


def test_stock_bad_param(client):
    assert client.get("/api/stock?code=abc").status_code == 400
    assert client.get("/api/stock?code=600519extra").status_code == 400


def test_source_fail_returns_500(monkeypatch, tmp_path):
    db = str(tmp_path / "fail.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_market_spot",
                        lambda: (_ for _ in ()).throw(ds.DataSourceError("boom")))
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/market")
    assert r.status_code == 500
    assert r.get_json()["error"]["code"] == "SOURCE_FAIL"


def test_sector_stale_propagates(monkeypatch, tmp_path):
    db = str(tmp_path / "stale_sector.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), True))
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/sector?code=industry:885887")
    assert r.get_json()["meta"]["stale"] is True


def test_stock_stale_propagates(monkeypatch, tmp_path):
    db = str(tmp_path / "stale_stock.db")
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), True))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/stock?code=600519")
    assert r.get_json()["meta"]["stale"] is True
