# -*- coding: utf-8 -*-
import datetime
import os
import tempfile
import threading

import pandas as pd
import pytest

import analysis as an
import app as app_mod
import data_source as ds
import store
import recommend


def make_spot():
    return pd.DataFrame({
        "code": ["600000", "600519", "000001", "688981"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际"],
        "price": [10.0, 1348.9, 12.0, 45.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0],
        "volume": [100000, 682720, 80000, 90000],
        "amount": [1e8, 9.2e8, 2e8, 5e8],
        "open": [9.8, 1348.0, 12.0, 45.0],
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


def make_daily(n=65, start=1348.9):
    closes = [float(start)] * n   # 缺省平盘(start=1348.9 与 make_quote 现价一致):避免 v3 下 回避 被剔;n 供短历史测试
    return pd.DataFrame({
        "date": [f"2026-05-{i%28+1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [682720] * n})


def make_minute():
    return pd.DataFrame({"time": ["10:00", "10:01"], "price": [1348.0, 1349.0],
                         "avg": [1342.0, 1343.0], "volume": [12000, 5000]})


def client_factory(monkeypatch, db_path=None):
    if db_path is None:
        db_path = os.path.join(tempfile.mkdtemp(), "api.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db_path)
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_index_realtime", lambda: (
        [{"code": "sh000001", "name": "上证指数", "price": 3456.78, "change_pct": 0.45}], False))
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600519", "600000"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(an, "is_after_close", lambda now: True)
    monkeypatch.setattr(an, "is_trading_time", lambda now: True)
    app.config["TESTING"] = True
    return app.test_client()


def _freeze_now_weekday(monkeypatch, day=datetime.date(2026, 8, 13)):
    """固定 app 内 now 为工作日,使 _signal_date 稳定返回 now.date()。

    生产 _signal_date 在周末/节假日回退 close_date;而 make_daily 的日历止于
    2026-05-xx,一旦真实 now 落到周末,硬编码 signal_date 的快照会因 > 当前
    signal_date 而查不到 prev_snapshot(测试随运行日期翻车)。固定为周四
    (2026-08-13, weekday=3)保证确定性。
    """
    class _FrozenDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(day.year, day.month, day.day, 15, 30)
    monkeypatch.setattr(app_mod, "datetime", _FrozenDT)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    return client_factory(monkeypatch, db_path=str(tmp_path / "api.db"))


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
    assert "overheated" in semi and semi["overheated"] is False   # Task 13:徽章字段可到达
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
    assert "overheated" in d and d["overheated"] is False         # Task 13:徽章字段可到达
    assert d["leaders_status"] == "ok"
    # fixture resolve → 600519/600000,均在 spot;600519 金额最大(9.2e8)→ 龙头池首位
    assert d["leaders"][0]["code"] == "600519"
    assert d["leaders"][0]["tag"] == "龙头+强势"
    assert "price" in d["leaders"][0] and "change_pct" in d["leaders"][0]
    assert d["leaders_source"] == "电子信息"


def test_api_sector_leaders_no_mapping(client, monkeypatch):
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": False, "reason": "no_mapping"})
    r = client.get("/api/sector?code=industry:885887")
    d = r.get_json()["data"]
    assert d["leaders"] == []
    assert d["leaders_status"] == "no_mapping"
    assert d["leaders_source"] is None
    assert d["index_history"]                     # 板块图表不阻塞


def test_api_sector_leaders_source_fail(client, monkeypatch):
    def boom(name):
        raise ds.DataSourceError("network down")
    monkeypatch.setattr(ds, "resolve_sector_constituents", boom)
    r = client.get("/api/sector?code=industry:885887")
    d = r.get_json()["data"]
    assert r.status_code == 200
    assert d["leaders"] == []
    assert d["leaders_status"] == "source_fail"
    assert d["index_history"]                     # 核心视图仍正常


def test_sector_bad_param(client):
    r = client.get("/api/sector?code=foo:123")
    body = r.get_json()
    assert r.status_code == 400 and body["error"]["code"] == "BAD_PARAM"
    r2 = client.get("/api/sector?code=industry:abc")
    assert r2.status_code == 400
    r3 = client.get("/api/sectors?type=xxx")
    assert r3.status_code == 400


def test_stock_two_code_forms(client, monkeypatch):
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: [])   # 钉死未映射 → sector_resolved=False,聚焦双 code 形态
    r1 = client.get("/api/stock?code=600519")
    r2 = client.get("/api/stock?code=sh600519")
    d1, d2 = r1.get_json()["data"], r2.get_json()["data"]
    assert d1["code"] == "sh600519" and d2["code"] == "sh600519"
    assert d1["name"] == "贵州茅台"
    assert d1["quote"]["change_pct"] == pytest.approx(0.3)
    assert d1["kline"][0]["date"].startswith("2026-")
    assert d1["intraday"][0]["time"] == "10:00"
    assert "position" in d1["scores"]
    assert "position" in d1 and d1["scores"]["position"] is not None
    assert "verdict" in d1 and d1["verdict"] in ("强烈关注", "关注", "持有/跟踪", "观望", "回避")
    assert "tier" in d1
    assert "sector_resolved" in d1 and d1["sector_resolved"] is False
    assert "verdict" not in d1["scores"]


def test_stock_bad_param(client):
    assert client.get("/api/stock?code=abc").status_code == 400
    assert client.get("/api/stock?code=600519extra").status_code == 400


def test_source_fail_returns_500(monkeypatch, tmp_path):
    db = str(tmp_path / "fail.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
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
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), True))
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": [], "match_type": "manual", "source_name": ""})
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/sector?code=industry:885887")
    assert r.get_json()["meta"]["stale"] is True


def test_stock_stale_propagates(monkeypatch, tmp_path):
    db = str(tmp_path / "stale_stock.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), True))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: [])   # 离线:绕过板块打分,聚焦 stale 传播
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/api/stock?code=600519")
    assert r.get_json()["meta"]["stale"] is True


def test_recommend_endpoint(client, monkeypatch):
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    r = client.get("/api/recommend")
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["generated_at"]
    # make_summary 两个板块都被 mock 成 建议关注 → 2 个 sector
    assert len(d["sectors"]) == 2
    s0 = d["sectors"][0]
    assert s0["match_type"] == "manual"
    assert "overheated" in s0 and isinstance(s0["overheated"], bool)   # Fix 1:P0 徽章出参可到达
    assert s0["stocks"] and "price" in s0["stocks"][0] and "change_pct" in s0["stocks"][0]
    assert d["diagnostics"] == {"stocks_not_in_spot": 0, "stocks_daily_failed": 0}
    meta = body["meta"]
    assert meta["coverage"]["mapped"] == 2
    assert meta["mapping_health"]["ok"] is True


def test_recommend_bad_param(client, monkeypatch):
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    assert client.get("/api/recommend?top_sectors=abc").status_code == 400
    r = client.get("/api/recommend?top_sectors=9&per_sector=0")
    d = r.get_json()["data"]
    assert len(d["sectors"]) == 2                # make_summary 两板块都入选;top=9 钳制到 5,但实际只有 2
    assert len(d["sectors"][0]["stocks"]) == 1   # per_sector=0 钳制到 1


def test_recommend_stale_propagates(monkeypatch, tmp_path):
    db = str(tmp_path / "reco_stale.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), True))   # 摘要 stale
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600519"], "match_type": "manual",
                                      "source_name": "电子信息"})
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), True))        # 候选 stale
    app.config["TESTING"] = True
    r = app.test_client().get("/api/recommend")
    assert r.get_json()["meta"]["stale"] is True     # 任一候选 stale → 整包 stale


def test_actionable_leaders_endpoint(client, monkeypatch):
    payload = {
        "sectors_scanned": 3, "total": 1, "items": [
            {"code": "sh600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
             "tag": "龙头+强势", "tier": "可介入", "sector_code": "industry:885887",
             "sector_name": "半导体", "sector_verdict": "建议关注", "sector_composite": 78.0,
             "trend": 100, "volume_price": 90, "signal": 80, "composite": 91.5, "risk": 0,
             "bias_pct": -83.05},
        ],
        "skipped_sectors": [{"name": "白酒", "reason": "no_mapping"}],
        "diagnostics": {"stocks_daily_failed": 0},
    }
    monkeypatch.setattr(recommend, "collect_actionable_leaders",
                        lambda *a, **k: (payload, False))
    r = client.get("/api/actionable-leaders")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["generated_at"]
    assert d["total"] == 1
    it = d["items"][0]
    assert it["tier"] == "可介入" and it["bias_pct"] == -83.05
    assert it["sector_code"] == "industry:885887"
    cov = body["meta"]["coverage"]
    assert cov["scanned"] == 3 and cov["total"] == 1 and cov["skipped"] == 1
    assert cov["skipped_by_reason"] == {"no_mapping": 1}
    assert body["meta"]["mapping_health"]["ok"] is True
    assert body["meta"]["stale"] is False


def test_actionable_leaders_source_fail(client, monkeypatch):
    def boom(t):
        raise ds.DataSourceError("network down")
    monkeypatch.setattr(ds, "get_sector_summary", boom)
    r = client.get("/api/actionable-leaders")
    assert r.status_code == 500
    assert r.get_json()["error"]["code"] == "SOURCE_FAIL"


def test_stock_detail_sector_resolution(monkeypatch):
    # resolve 命中 600519→[白酒];score_all_sectors 给出白酒 composite=80 → bonus 8
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: ["白酒"])
    monkeypatch.setattr(recommend, "score_all_sectors",
                        lambda *a, **k: [{"name": "半导体", "composite": 50.0},
                                         {"name": "白酒", "composite": 80.0}])
    app = client_factory(monkeypatch)          # 见下方 Step 4 的 helper(复用现有 client fixture 逻辑)
    r = app.get("/api/stock?code=600519")
    d = r.get_json()["data"]
    assert d["sector_resolved"] is True
    assert "position" in d and d["position"] is not None
    assert d["verdict"] in ("强烈关注", "关注", "持有/跟踪", "观望", "回避")
    assert d["tier"] in ("可介入", "观察", None)
    # composite = stock_composite_v3(..., bonus=8),未舍入判定 verdict,展示 round 2 位
    sc = d["scores"]
    assert d["composite"] == pytest.approx(
        round(an.stock_composite_v3(sc["position"], sc["volume_price"], sc["trend"],
                                    sc["signal"], sc["risk"], 8), 2), abs=0.01)


def test_stock_sector_scores_only_resolved(monkeypatch):
    # Fix:个股接口只对所属板块打分(预过滤 summary),不重算全 ~90 板块
    captured = {}
    def fake_score(summary, *a, **k):
        captured["names"] = list(summary["name"])
        return [{"name": n, "composite": 80.0} for n in summary["name"]]
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: ["白酒"])
    monkeypatch.setattr(recommend, "score_all_sectors", fake_score)
    app = client_factory(monkeypatch)
    r = app.get("/api/stock?code=600519")
    assert captured["names"] == ["白酒"]           # 预过滤后只含白酒一行(非全量 2 行)
    assert r.get_json()["data"]["sector_resolved"] is True


def test_stock_detail_sector_unresolved(monkeypatch):
    # 解析失败(未映射)→ sector_resolved=false、加成 0、不 500
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: [])
    app = client_factory(monkeypatch)
    r = app.get("/api/stock?code=600519")
    d = r.get_json()["data"]
    assert d["sector_resolved"] is False
    assert d["composite"] is not None and d["verdict"] is not None


def test_stock_intraday_none_safe(monkeypatch):
    # Fix:get_stock_minute 归一后 avg 可为 None,intraday 须 None 安全(不 float(None) 抛 TypeError)
    app = client_factory(monkeypatch)
    minute = pd.DataFrame({"time": ["10:00"], "price": [1348.0],
                           "avg": [None], "volume": [12000.0]})
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (minute, False))
    r = app.get("/api/stock?code=600519")
    d = r.get_json()["data"]
    assert d["intraday"][0]["avg"] is None    # 序列化为 null,非 NaN/无 500


def test_stock_short_history_all_null(monkeypatch):
    app = client_factory(monkeypatch)
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(30), False))  # 30 根 <61,须在 factory 之后覆盖(其缺省 65 根)
    r = app.get("/api/stock?code=600519")
    d = r.get_json()["data"]
    assert d["scores"]["composite"] is None
    assert d["composite"] is None and d["verdict"] is None and d["tier"] is None
    assert d["sector_resolved"] is False


def test_recommend_prev_snapshot(monkeypatch, tmp_path):
    db = str(tmp_path / "reco_snap_api.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    # 第一次:无上一期 → prev_snapshot None
    r1 = c.get("/api/recommend").get_json()["data"]
    assert r1["prev_snapshot"] is None
    # 上一期快照 signal_date 须等于当前 prev_trading_date 才触发 is_next_day(spec §5.2 锚点=signal_date)
    prev_sig = r1["prev_trading_date"]
    store.upsert_recommend_snapshot(db, prev_sig, "2026-08-12 17:40:00",
                                    prev_sig, r1["prev_trading_date"],
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps is not None and ps["signal_date"] == prev_sig
    assert ps["is_next_day"] is True                       # prev.signal_date == 当前 prev_trading_date
    assert len(ps["stocks"]) == 1
    s = ps["stocks"][0]
    assert s["code"] == "600000" and s["signal_close"] == 10.0
    assert s["today_open"] == pytest.approx(9.8)           # make_spot open 列 600000 → 9.8
    assert abs(s["gap_pct"] - (-2.0)) < 0.01               # (9.8/10.0 - 1)*100


def test_recommend_degenerate_snapshot_not_written(monkeypatch, tmp_path):
    # Fix 2:全部强势板块映射失败 → close_date=None → 不得写退化快照(遮蔽上一期真实快照,且致次日 gap 计算 TypeError)
    db = str(tmp_path / "reco_degen.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": False, "reason": "no_mapping"})
    r = c.get("/api/recommend")
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["sectors"] == []
    assert d["close_date"] is None
    # 退化快照不得落库:recommend_snapshot 表应保持空
    assert store.get_recommend_snapshot_before(db, "9999-99-99") is None
    import sqlite3
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM recommend_snapshot").fetchone()[0]
    conn.close()
    assert n == 0


def test_recommend_prev_snapshot_intraday(monkeypatch, tmp_path):
    # Fix:盘中生成上一期快照(signal_date=D, close_date=D-1,二者不等)→ 仍判定 is_next_day(锚点=signal_date)
    db = str(tmp_path / "reco_snap_intraday.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    r1 = c.get("/api/recommend").get_json()["data"]
    prev_td = r1["prev_trading_date"]          # 当前 prev_trading_date(锚点)
    # signal_date=prev_td, close_date=prev_td 前一交易日(盘中快照末根在前一日)
    store.upsert_recommend_snapshot(db, prev_td, "2026-05-28 10:30:00",
                                    "2026-05-27", prev_td,
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps["is_next_day"] is True           # close_date(05-27) != prev_trading_date(05-28),但 signal_date 命中


def test_recommend_prev_snapshot_gap_days(monkeypatch, tmp_path):
    # Fix:跨多日 gap 计数按 signal_date 锚点(非 close_date)
    _freeze_now_weekday(monkeypatch)
    db = str(tmp_path / "reco_snap_gap.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    # make_daily 日期确定性回绕 2026-05-01..28,trading_dates=最近 20 交易日(升序 05-09..05-28)。
    # prev signal_date=倒数第 3 交易日(05-26),close_date 故意设最早(05-09):
    #   若锚点错用 close_date,gap_days≈19;signal_date 锚点应数出 05-27/05-28 两个交易日 = 2。
    store.upsert_recommend_snapshot(db, "2026-05-26", "2026-05-26 17:40:00",
                                    "2026-05-09", "2026-05-25",
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps["is_next_day"] is False
    assert ps["gap_days"] == 2


def test_recommend_prev_snapshot_bj_prefix(monkeypatch, tmp_path):
    # Fix 3:上一期快照含北交所股(bj 前缀)→ 今日开盘对比须剔除前缀命中 spot(bj 不再被静默跳过)
    _freeze_now_weekday(monkeypatch)
    db = str(tmp_path / "reco_bj.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    spot = make_spot().copy()
    spot.loc[4] = {"code": "830799", "name": "北交所股", "price": 11.0, "change_pct": 1.0,
                   "volume": 10000, "amount": 1e8, "open": 10.5}
    monkeypatch.setattr(ds, "get_market_spot", lambda: (spot, False))
    r1 = c.get("/api/recommend").get_json()["data"]
    prev_close = r1["prev_trading_date"]
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 17:40:00",
                                    prev_close, r1["prev_trading_date"],
                                    [{"code": "bj830799", "name": "北交所股", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps is not None
    bj = [x for x in ps["stocks"] if x["code"] == "bj830799"]
    assert bj and bj[0]["today_open"] == pytest.approx(10.5)
