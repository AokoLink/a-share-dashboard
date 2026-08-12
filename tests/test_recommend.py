# -*- coding: utf-8 -*-
import datetime
import pandas as pd
import pytest
import analysis as an
import data_source as ds
import recommend


def make_summary():
    return pd.DataFrame({
        "code": ["885887", "885559", "885123"],
        "name": ["半导体", "白酒", "化工"],
        "change_pct": [5.0, -1.0, 0.5],
        "up_count": [90.0, 30.0, 50.0],
        "down_count": [5.0, 60.0, 50.0],
        "leader": ["X", "Y", "Z"],
        "leader_change_pct": [5.0, 0.5, 1.0],
        "turnover": [1e10, 1e9, 5e9],
    })


def make_spot():
    return pd.DataFrame({
        "code": ["600050", "600100", "600519", "688981", "300750"],
        "name": ["中国联通", "同方股份", "ST茅台", "中芯国际", "宁德时代"],
        "price": [5.0, 10.0, 1348.9, 45.0, 200.0],
        "change_pct": [3.0, 2.0, 1.0, 5.0, 0.0],
        "volume": [100000, 0, 682720, 90000, 90000],
        "amount": [2e8, 1e8, 9e8, 5e8, 5e8],
    })


def fake_store():
    class S:
        def get_sector_turnover_avg(self, *a, **k): return None
        def get_sector_prev_change(self, *a, **k): return None
        def get_sector_change_3d(self, *a, **k): return None
        def get_consecutive_days(self, *a, **k): return 2   # 模拟连续上榜 ≥2 天,使真实打分可入选
    return S()


def make_daily(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-07-{i % 28 + 1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [100000] * n})


def mock_sector(monkeypatch, composite=78.0, verdict="建议关注"):
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": verdict, "composite": composite, "consecutive_days": 0,
        "emotion": 80, "strength": 70, "risk": 10})


def test_select_sectors_filters_and_sorts(monkeypatch):
    real = an.collect_sector_metrics
    def fake(db, t, c, *a, **k):
        if c == "885559":                      # 白酒 → 观望,剔除
            return {**real(db, t, c, *a, **k), "verdict": "观望", "composite": 30.0}
        if c == "885123":                      # 化工 → 真实打分是观望(情绪/强度双低),强制 建议关注+综合90(最高)
            return {**real(db, t, c, *a, **k), "verdict": "建议关注", "composite": 90.0}
        return real(db, t, c, *a, **k)         # 半导体 → 真实打分(consecutive=2 → P3 建议关注)
    monkeypatch.setattr(an, "collect_sector_metrics", fake)
    strong = recommend.select_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                      datetime.datetime(2026, 8, 11, 15, 0))
    codes = [x["code"] for x in strong]
    assert codes == ["885123", "885887"]     # 观望被剔除;按 composite 降序(90 > 84.66)
    assert all(x["verdict"] in ("建议关注", "跟踪(热点延续)") for x in strong)


def test_filter_candidates_rules():
    codes = ["600050", "600100", "600519", "688981", "300750", "999999"]
    kept, not_in = recommend.filter_candidates(codes, make_spot(), exclude_codes={"688981"})
    kept_codes = {k["code"] for k in kept}
    assert "600100" not in kept_codes        # 停牌 volume=0
    assert "600519" not in kept_codes        # ST
    assert "688981" not in kept_codes        # 新股(排除集)
    assert "999999" not in kept_codes        # 不在 spot
    assert not_in == 1
    assert kept_codes == {"600050", "300750"}   # 300750 涨幅0/量正常 → 保留;无涨停/大跌


def test_rank_candidates():
    scored = [
        {"code": "a", "scores": {"composite": 80, "risk": 10}, "verdict": "关注"},
        {"code": "b", "scores": {"composite": 90, "risk": 80}, "verdict": "规避"},
        {"code": "c", "scores": {"composite": 70, "risk": 20}, "verdict": "持有/跟踪"},
        {"code": "d", "scores": {"composite": 60, "risk": 30}, "verdict": "观望"},
    ]
    ranked = recommend.rank_candidates(scored, 2)
    assert [x["code"] for x in ranked] == ["a", "c"]   # b 规避/高险剔除;按 composite 降序取 2


def test_build_recommend_happy_path(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "600100", "600519"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10 + i for i in range(30)]), False))
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=2, per_sector=5)
    assert stale is False
    assert payload["strong_count"] == 3        # 三个板块都被 mock 成 建议关注
    assert len(payload["sectors"]) == 2        # top_sectors=2 截断
    s0 = payload["sectors"][0]
    assert s0["match_type"] == "manual" and s0["constituent_source"] == "电子信息"
    # 成分股:600050 保留、600100 停牌剔除、600519 ST 剔除
    assert [x["code"] for x in s0["stocks"]] == ["sh600050"]
    assert "price" in s0["stocks"][0] and "change_pct" in s0["stocks"][0]
    assert payload["diagnostics"]["stocks_not_in_spot"] == 0
    assert payload["diagnostics"]["stocks_daily_failed"] == 0


def test_build_recommend_stale_aggregation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10 + i for i in range(30)]), True))  # 候选 stale
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    assert stale is True                        # 任一候选股 stale → 整包 stale


def test_build_recommend_mapping_failure_skipped_with_score(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": False, "reason": "no_mapping"})
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=3, per_sector=5)
    assert payload["sectors"] == []
    assert payload["skipped_sectors"][0]["reason"] == "no_mapping"
    assert payload["skipped_sectors"][0]["composite_score"] == 78.0   # skipped 带分


def test_build_recommend_not_in_spot_diagnostics(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["999999"],   # 不在 spot
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily([1.0]), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    # 补位语义:无板块能凑够 top_sectors 时,强势列表全部试尽;sectors 空所以哨兵不提前触发
    assert payload["diagnostics"]["stocks_not_in_spot"] == 3
    assert len(payload["skipped_sectors"]) == 3
    # 候选全被过滤 → 板块降级进 skipped(too_few)
    assert payload["sectors"] == []
    assert payload["skipped_sectors"][0]["reason"] == "too_few"


def test_build_recommend_request_counts(monkeypatch):
    mock_sector(monkeypatch)
    counts = {"daily": 0, "cons": 0, "new": 0}
    monkeypatch.setattr(ds, "get_new_stocks", lambda: (counts.__setitem__("new", counts["new"] + 1), set())[1])
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: (counts.__setitem__("cons", counts["cons"] + 1),
                                      {"ok": True, "codes": ["600050"], "match_type": "manual",
                                       "source_name": "电子信息"})[1])
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (counts.__setitem__("daily", counts["daily"] + 1),
                                   (make_daily([10 + i for i in range(30)]), False))[1])
    recommend.build_recommend(make_summary(), make_spot(), ":db:", "industry",
                              datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    assert counts["daily"] == 1                 # 每候选恰好 1 次
    assert counts["cons"] == 1                  # top_sectors=1 → 首个强势板块解析 1 次后截断
    assert counts["new"] == 1


def test_build_recommend_concurrent_failure_isolation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "600100", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    import time
    def flaky(c):
        time.sleep(0.02)
        if c == "300750":                      # 300750 通过硬过滤,日线拉取失败
            raise ds.DataSourceError("boom")
        return make_daily([10 + i for i in range(30)]), False
    monkeypatch.setattr(ds, "get_stock_daily", flaky)
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    s0 = payload["sectors"][0]
    codes = [x["code"] for x in s0["stocks"]]
    assert codes == ["sh600050"]                # 单股失败,同板块其余不受影响
    assert payload["diagnostics"]["stocks_daily_failed"] == 1


def test_build_recommend_fills_slots(monkeypatch):
    # 补位语义(spec §4.1/§5.2):失败板块(no_mapping)不占 top_sectors 槽位,继续从强势列表补位。
    mock_sector(monkeypatch)                    # 3 板块全部 建议关注,composite 78.0 → 强势序 [半导体,白酒,化工]
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())

    def resolve(name):
        if name == "半导体":
            return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "电子信息"}
        if name == "白酒":
            return {"ok": False, "reason": "no_mapping"}
        # 化工:600050 通过硬过滤且打分非规避(风险 0 → 持有/跟踪),故可入选补位
        return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "食品饮料"}

    monkeypatch.setattr(ds, "resolve_sector_constituents", resolve)
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10 + i for i in range(30)]), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=2, per_sector=5)
    assert len(payload["sectors"]) == 2
    assert [s["name"] for s in payload["sectors"]] == ["半导体", "化工"]  # 白酒 失败 → 化工 补位
    assert len(payload["skipped_sectors"]) == 1
    assert payload["skipped_sectors"][0]["name"] == "白酒"
    assert payload["skipped_sectors"][0]["reason"] == "no_mapping"
    assert payload["strong_count"] == 3


def test_filter_candidates_hard_filters():
    # 三个硬过滤分支的独立覆盖:涨停买不进 / 大跌 / 流动性不足;每行恰好触发一个。
    spot_df = pd.DataFrame([
        {"code": "600000", "name": "极限股", "price": 10.0, "change_pct": 10.0,
         "volume": 100000, "amount": 5e8},    # 10.0 >= limit_threshold(600000)=9.9 → 涨停买不进
        {"code": "600001", "name": "大跌股", "price": 9.0, "change_pct": -8.0,
         "volume": 100000, "amount": 5e8},    # -8.0 <= -7.0 → 大跌
        {"code": "600002", "name": "低流股", "price": 5.0, "change_pct": 2.0,
         "volume": 100000, "amount": 5e7},    # 5e7 < 1e8 → 流动性不足
        {"code": "600003", "name": "正常股", "price": 8.0, "change_pct": 3.0,
         "volume": 100000, "amount": 5e8},    # 全部通过 → 保留
    ])
    kept, not_in = recommend.filter_candidates(
        ["600000", "600001", "600002", "600003"], spot_df, exclude_codes=set())
    assert {k["code"] for k in kept} == {"600003"}
    assert not_in == 0


def test_pick_leaders_mixed():
    rows = [
        {"code": "600001", "name": "甲股", "price": 10.0, "change_pct": 2.0, "amount": 5e8, "volume": 10000},
        {"code": "600002", "name": "乙股", "price": 11.0, "change_pct": 9.0, "amount": 3e8, "volume": 10000},
        {"code": "600003", "name": "丙股", "price": 12.0, "change_pct": 1.0, "amount": 8e8, "volume": 10000},
        {"code": "600004", "name": "丁股", "price": 13.0, "change_pct": 5.0, "amount": 1e8, "volume": 10000},
        {"code": "600005", "name": "戊股", "price": 14.0, "change_pct": 3.0, "amount": 4e8, "volume": 10000},
    ]
    leaders = recommend.pick_leaders(rows, total=5)
    # 龙头池(金额 top3):600003(8e8) 600001(5e8) 600005(4e8)
    # 强势池(涨幅 top3):600002(9) 600004(5) 600005(3)
    # 合并去重(龙头在前):600003 600001 600005 600002 600004
    assert [x["code"] for x in leaders] == ["600003", "600001", "600005", "600002", "600004"]
    assert [x["tag"] for x in leaders] == ["龙头", "龙头", "龙头+强势", "强势", "强势"]
    assert leaders[0]["price"] == 12.0
    assert leaders[2]["change_pct"] == 3.0


def test_pick_leaders_excludes():
    rows = [
        {"code": "600001", "name": "正常股", "price": 10.0, "change_pct": 2.0, "amount": 5e8, "volume": 10000},
        {"code": "600002", "name": "ST坏股", "price": 11.0, "change_pct": 9.0, "amount": 3e8, "volume": 10000},
        {"code": "600003", "name": "停牌股", "price": 0.0, "change_pct": 1.0, "amount": 8e8, "volume": 0},
        {"code": "600004", "name": "新股", "price": 13.0, "change_pct": 5.0, "amount": 1e8, "volume": 10000},
    ]
    leaders = recommend.pick_leaders(rows, total=5, exclude_codes={"600004"})
    # ST(名含 ST)、停牌(price/volume 空)、新股(exclude_codes)均被排除 → 仅剩 600001
    assert [x["code"] for x in leaders] == ["600001"]
    assert leaders[0]["tag"] == "龙头+强势"


def test_pick_leaders_empty_and_degenerate():
    assert recommend.pick_leaders([], total=5) == []
    all_dead = [{"code": "600001", "name": "全停牌", "price": 0.0, "change_pct": None,
                 "amount": None, "volume": 0}]
    assert recommend.pick_leaders(all_dead, total=5) == []
    # 金额全 None → 龙头池空,仅强势池
    no_amount = [
        {"code": "600001", "name": "无额A", "price": 10.0, "change_pct": 3.0, "amount": None, "volume": 10000},
        {"code": "600002", "name": "无额B", "price": 11.0, "change_pct": 2.0, "amount": None, "volume": 10000},
    ]
    l1 = recommend.pick_leaders(no_amount, total=5)
    assert [x["code"] for x in l1] == ["600001", "600002"]
    assert all(x["tag"] == "强势" for x in l1)
    assert l1[0]["amount"] is None


def test_score_all_sectors_returns_all_sorted(monkeypatch):
    real = an.collect_sector_metrics
    def fake(db, t, c, *a, **k):
        if c == "885559":                      # 白酒 → 观望,但 score_all_sectors 仍返回
            return {**real(db, t, c, *a, **k), "verdict": "观望", "composite": 30.0}
        return real(db, t, c, *a, **k)         # 半导体/化工 → 真实打分
    monkeypatch.setattr(an, "collect_sector_metrics", fake)
    all_rows = recommend.score_all_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                           datetime.datetime(2026, 8, 11, 15, 0))
    codes = [x["code"] for x in all_rows]
    assert codes[0] == "885887"                                   # 真实打分(consecutive=2)最高,居首
    assert set(codes) == {"885887", "885559", "885123"}           # 全板块都返回(不过滤)
    strong = recommend.select_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                      datetime.datetime(2026, 8, 11, 15, 0))
    assert [x["code"] for x in strong] == ["885887"]    # select_sectors 仍过滤(白酒/化工观望)


def test_tier_for_verdict():
    assert recommend.tier_for_verdict("关注") == "可介入"
    assert recommend.tier_for_verdict("持有/跟踪") == "观察"
    for v in ("观望", "回调风险", "规避"):
        assert recommend.tier_for_verdict(v) is None
    assert recommend.tier_for_verdict(None) is None


def test_bias_pct():
    daily = make_daily([10 + i for i in range(30)])   # 收盘 10..39,末位 MA20 = 29.5
    assert abs(recommend.bias_pct(daily, 33.0) - 11.86) < 0.01
    assert abs(recommend.bias_pct(daily, 29.5)) < 0.001
    assert recommend.bias_pct(make_daily([1.0] * 10), 1.0) is None   # 不足 20 根 → 无 MA20
    assert recommend.bias_pct(pd.DataFrame(), 10.0) is None          # 空 df
    assert recommend.bias_pct(daily, None) is None


def test_collect_actionable_leaders_two_tiers_and_dedupe(monkeypatch):
    mock_sector(monkeypatch)                       # 3 板块全部 建议关注/composite 78
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    by_code = {
        "600050": {"trend": 100, "vp": 90, "signal": 80, "risk": 0},    # 综合 91.5 → 关注 → 可介入
        "688981": {"trend": 60, "vp": 60, "signal": 50, "risk": 10},    # 综合 57.5 → 持有/跟踪 → 观察
    }
    def fake_score_candidate(row, daily_df, now):
        c = str(row["code"])
        s = by_code[c]
        composite = an.stock_composite(s["trend"], s["vp"], s["signal"])
        return {"code": ds.with_prefix(c), "name": str(row["name"]),
                "price": row["price"], "change_pct": row["change_pct"],
                "scores": {"trend": s["trend"], "volume_price": s["vp"], "signal": s["signal"],
                           "risk": s["risk"], "composite": composite},
                "verdict": an.stock_verdict(composite, s["risk"])}
    monkeypatch.setattr(recommend, "_score_candidate", fake_score_candidate)
    daily = make_daily([10 + i for i in range(30)])
    payload, stale = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0),
        lambda name: {"ok": True, "codes": ["600050", "600100", "688981"],
                      "match_type": "manual", "source_name": "电子信息"},
        lambda c: (daily, False))
    assert stale is False
    assert payload["sectors_scanned"] == 3
    assert payload["skipped_sectors"] == []
    # 600100 停牌(volume=0)→ pick_leaders 排除;600050/688981 为各板块龙头
    # 三板块 composite 相同(78)→ 按板块序先到先得,保留首个板块(半导体)那条
    assert [x["code"] for x in payload["items"]] == ["sh600050", "sh688981"]
    assert [x["tier"] for x in payload["items"]] == ["可介入", "观察"]
    assert payload["items"][0]["sector_name"] == "半导体"
    assert payload["items"][0]["tag"] == "龙头+强势"
    assert payload["items"][0]["composite"] == 91.5
    assert payload["items"][0]["bias_pct"] is not None     # bias 用真实 daily(现价 5.0 vs MA20 29.5)
    assert payload["diagnostics"]["stocks_daily_failed"] == 0


def test_collect_actionable_leaders_resolve_failure_skips(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())

    def resolve(name):
        if name == "白酒":
            return {"ok": False, "reason": "no_mapping"}
        if name == "半导体":
            raise ds.DataSourceError("boom")     # 网络异常 → source_fail
        return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "食品饮料"}
    payload, _ = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), resolve,
        lambda c: (make_daily([10 + i for i in range(30)]), False))
    reasons = sorted(s["reason"] for s in payload["skipped_sectors"])
    assert reasons == ["no_mapping", "source_fail"]
    assert payload["sectors_scanned"] == 3
    assert payload["total"] == 1                 # 化工 600050 → 关注/持有跟踪 之一
    assert payload["items"][0]["code"] == "sh600050"


def test_collect_actionable_leaders_daily_failure_skips(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())

    def resolve(name):
        if name == "半导体":
            return {"ok": True, "codes": ["600050", "688981"],
                    "match_type": "manual", "source_name": "电子信息"}
        return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "食品饮料"}

    def flaky(c):
        if c == "688981":
            raise ds.DataSourceError("boom")
        return make_daily([10 + i for i in range(30)]), False
    payload, _ = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), resolve, flaky)
    codes = [x["code"] for x in payload["items"]]
    assert codes == ["sh600050"]                 # 688981 日线失败被跳过,同板块其余保留
    assert payload["diagnostics"]["stocks_daily_failed"] == 1


def test_collect_actionable_leaders_stale_aggregation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    payload, stale = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0),
        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "电子信息"},
        lambda c: (make_daily([10 + i for i in range(30)]), True))   # 候选 stale
    assert stale is True
    assert payload["items"][0]["code"] == "sh600050"
