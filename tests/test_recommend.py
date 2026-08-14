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


def make_consolidated():
    """65 根低位横盘(先下跌 40 根再横盘 25 根)→ 位置≈65/风险0 → 综合≈55 → 持有跟踪。

    v3 乘法折扣下,[10+i] 递增 65 根会让 600050(现价 5.0)落在 60 日区间顶部 → 位置≈5 → 综合≈27 → 回避 被剔除,
    无法验证"可介入候选被排名"的集成行为;此 fixture 使候选落在 持有跟踪(52≤x<62,非回避),供排名与过滤路径使用。
    """
    return make_daily([7.0 - 0.04 * i for i in range(40)] + [5.4] * 25)


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
        {"code": "a", "composite": 80, "scores": {"composite": 80, "risk": 10}, "verdict": "关注"},
        {"code": "b", "composite": 90, "scores": {"composite": 90, "risk": 80}, "verdict": "回避"},
        {"code": "c", "composite": 70, "scores": {"composite": 70, "risk": 20}, "verdict": "持有/跟踪"},
        {"code": "d", "composite": 60, "scores": {"composite": 60, "risk": 30}, "verdict": "观望"},
    ]
    ranked = recommend.rank_candidates(scored, 2)
    assert [x["code"] for x in ranked] == ["a", "c"]   # b 回避/高险剔除;按 composite 降序取 2


def test_build_recommend_happy_path(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "600100", "600519"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_consolidated(), False))
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
                        lambda c: (make_consolidated(), True))  # 候选 stale
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
                                   (make_consolidated(), False))[1])
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
        return make_consolidated(), False
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
                        lambda c: (make_consolidated(), False))
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


def test_price_floor_excludes_low_price(monkeypatch):
    monkeypatch.setattr(recommend, "PRICE_FLOOR", 3.0)
    spot = make_spot().copy()
    spot.loc[spot["code"] == "600050", "price"] = 2.5          # 压到 <3
    # 600100 在 make_spot 中 volume=0(停牌)→ 恒被停牌过滤剔除;用 300750 验证价格护栏独立排除
    kept, _ = recommend.filter_candidates(["600050", "300750"], spot, set())
    codes = [str(r["code"]) for r in kept]
    assert "600050" not in codes and "300750" in codes


def test_price_floor_default_off():
    assert recommend.PRICE_FLOOR is None
    spot = make_spot()
    kept, _ = recommend.filter_candidates(["600050"], spot, set())
    assert any(str(r["code"]) == "600050" for r in kept)


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


def test_tier_for_verdict_v3():
    assert recommend.tier_for_verdict("强烈关注") == "可介入"
    assert recommend.tier_for_verdict("关注") == "可介入"
    assert recommend.tier_for_verdict("持有/跟踪") == "观察"
    for v in ("观望", "回避", None):
        assert recommend.tier_for_verdict(v) is None


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
        "600050": {"position": 90, "vp": 60, "trend": 50, "signal": 60, "risk": 0},   # quality=0.55*90+0.15*60+0.10*50+0.20*60=75.5 → 强烈关注/可介入
        "688981": {"position": 48, "vp": 48, "trend": 48, "signal": 48, "risk": 0},   # quality=48 未含板块 → 观望;Task 5 加板块+8=56 ∈ [52,62) → 持有跟踪 → 观察(两档保持)
    }
    def fake_score_candidate(row, daily_df, now, sector_composite=None):
        c = str(row["code"])
        s = by_code[c]
        final = an.stock_composite_v3(s["position"], s["vp"], s["trend"], s["signal"],
                                      s["risk"], recommend.sector_bonus(sector_composite))
        return {"code": ds.with_prefix(c), "name": str(row["name"]),
                "price": row["price"], "change_pct": row["change_pct"],
                "scores": {"position": s["position"], "trend": s["trend"],
                           "volume_price": s["vp"], "signal": s["signal"],
                           "risk": s["risk"], "composite": final},
                "composite": final, "verdict": an.stock_verdict(final)}
    monkeypatch.setattr(recommend, "_score_candidate", fake_score_candidate)
    daily = make_daily([10 + i for i in range(65)])
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
    assert payload["items"][0]["composite"] == pytest.approx(round(79.0, 2))   # HOT_SIGNAL_WEIGHTS 0.40/0.15/0.10/0.35 → 0.40*90+0.15*60+0.10*50+0.35*60=71,+bonus 8=79
    assert payload["items"][0]["bias_pct"] is not None     # bias 用真实 daily(现价 5.0 vs MA20 64.5)
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
        lambda c: (make_consolidated(), False))
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
        return make_consolidated(), False
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
        lambda c: (make_consolidated(), True))   # 候选 stale
    assert stale is True
    assert payload["items"][0]["code"] == "sh600050"


def test_sector_bonus_thresholds():
    # 阈值经 Task 8 校准按 live 分布重锚(§9.4):70/55/40 → 68/60/50
    assert recommend.sector_bonus(None) == 0
    assert recommend.sector_bonus(68) == 8
    assert recommend.sector_bonus(67.9) == 4
    assert recommend.sector_bonus(60) == 4
    assert recommend.sector_bonus(59.9) == 0
    assert recommend.sector_bonus(50) == 0
    assert recommend.sector_bonus(49.9) == -5


def test_sector_bonus_quality_gate(monkeypatch):
    # 决策:quality<50 不给加成
    monkeypatch.setattr(recommend, "BONUS_QUALITY_GATE", True)
    monkeypatch.setattr(recommend, "BONUS_GE75", False)
    assert recommend.sector_bonus(70.0, quality=40) == 0        # quality<50 → 无加成
    assert recommend.sector_bonus(70.0, quality=60) == 8        # ≥68 档
    assert recommend.sector_bonus(65.0, quality=60) == 4
    assert recommend.sector_bonus(70.0, quality=None) == 8      # quality 缺失 → 不 gate


def test_sector_bonus_ge75(monkeypatch):
    monkeypatch.setattr(recommend, "BONUS_GE75", True)
    monkeypatch.setattr(recommend, "BONUS_QUALITY_GATE", False)
    assert recommend.sector_bonus(70.0) == 4                    # <75 → 降档
    assert recommend.sector_bonus(76.0) == 8


def test_score_candidate_applies_sector_bonus(monkeypatch):
    # 纯因子全 55,risk=0:quality=55;板块 composite=78 → bonus=8 → final=63 → 关注/可介入(校准后 62 起可介入)
    monkeypatch.setattr(an, "score_stock",
                        lambda df, q, now: {"position": 55, "trend": 55, "volume_price": 55,
                                            "signal": 55, "risk": 0, "composite": 55.0})
    row = {"code": "600050", "name": "X", "price": 5.0, "change_pct": 1.0,
           "volume": 100000, "amount": 2e8}
    scored = recommend._score_candidate(row, make_daily([10 + i for i in range(65)]),
                                        datetime.datetime(2026, 8, 11, 15, 0), 78.0)
    assert scored["composite"] == pytest.approx(63.0)
    assert scored["verdict"] == "关注"
    assert recommend.tier_for_verdict(scored["verdict"]) == "可介入"
    # 弱板块 39.9 → bonus=-5 → final=50 → 观望 → 不可介入
    scored2 = recommend._score_candidate(row, make_daily([10 + i for i in range(65)]),
                                         datetime.datetime(2026, 8, 11, 15, 0), 39.9)
    assert scored2["composite"] == pytest.approx(50.0)
    assert recommend.tier_for_verdict(scored2["verdict"]) is None


def test_score_candidate_short_history_skipped(monkeypatch):
    monkeypatch.setattr(an, "score_stock",
                        lambda df, q, now: {"position": None, "trend": None, "volume_price": None,
                                            "signal": None, "risk": None, "composite": None})
    row = {"code": "600050", "name": "X", "price": 5.0, "change_pct": 1.0,
           "volume": 100000, "amount": 2e8}
    assert recommend._score_candidate(row, make_daily([10 + i for i in range(30)]),
                                      datetime.datetime(2026, 8, 11, 15, 0)) is None


def test_score_sector_stocks_returns_dates(monkeypatch):
    # 原 test_score_sector_stocks_skips_short_history 更名为本函数:_score_sector_stocks 返回 4 元组(新增 dates)
    monkeypatch.setattr(an, "score_stock",
                        lambda df, q, now: {"position": None, "trend": None, "volume_price": None,
                                            "signal": None, "risk": None, "composite": None})
    rows = [{"code": "600050", "name": "X", "price": 5.0, "change_pct": 1.0,
             "volume": 100000, "amount": 2e8}]
    ranked, daily_failed, any_stale, dates = recommend._score_sector_stocks(
        rows, lambda c: (make_daily([10 + i for i in range(30)]), False),
        datetime.datetime(2026, 8, 11, 15, 0), per_sector=5, sector_composite=78.0)
    assert ranked == []                 # None 被守卫跳过 → 不崩 rank_candidates
    assert daily_failed == 0 and any_stale is False
    assert len(dates) == 28             # 日期并集照常收集;make_daily 30 根按 i%28+1 回绕 → 28 个唯一日期


def test_build_recommend_snapshot_fields(monkeypatch):
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual",
                                      "source_name": "电子信息"})
    daily_df = make_consolidated()                  # 65 根,本文件 make_daily 末根 date = "2026-07-09"
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (daily_df, False))
    now = datetime.datetime(2026, 8, 13, 10, 0)      # 2026-08-13 周四盘中 → signal_date=2026-08-13
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry", now, 1, 5)
    assert payload["signal_date"] == "2026-08-13"
    assert payload["close_date"] == str(daily_df.iloc[-1]["date"])          # 末根日线日期(勿硬编码,见 make_daily 生成规则)
    # 交易日历 = make_daily 回绕生成的 2026-07-01..28;prev_trading_date = signal_date 前最近交易日 = 日历最大值
    assert payload["prev_trading_date"] == str(daily_df["date"].max())
    assert payload["trading_dates"] and payload["trading_dates"][-1] < "2026-08-13"
    s = payload["sectors"][0]["stocks"][0]
    assert s["signal_close"] == pytest.approx(5.4)   # 末根 close = 5.4(make_consolidated)
    assert "close_date" not in s                      # close_date 为 payload 级众数聚合,不放入每股响应(设计 §5.2)


def test_build_recommend_sector_overheated_key(monkeypatch):
    # Fix 1 (P0 徽章):出参 sector 须带结构化 overheated(bool)。mock 打分返回 True → 透传到 sector 输出。
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 0,
        "emotion": 80, "strength": 70, "risk": 10, "overheated": True})
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual",
                                      "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_consolidated(), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    s = payload["sectors"][0]
    assert "overheated" in s
    assert isinstance(s["overheated"], bool)
    assert s["overheated"] is True                       # 真实过热 → 徽章路径可见


def test_build_recommend_sector_overheated_default_false(monkeypatch):
    # mock 未返回 overheated → 防御默认 False(缺失键不崩,前端徽章不渲染)
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual",
                                      "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_consolidated(), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    s = payload["sectors"][0]
    assert "overheated" in s and isinstance(s["overheated"], bool)
    assert s["overheated"] is False


def test_signal_date_fallback_non_trading(monkeypatch):
    # 周六 10:00 → 非交易日 → signal_date 回退 close_date(无得分股票 → 均为 None)
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")   # 板块入选,成分股空 → 无得分 → too_few 跳过
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": [], "match_type": "manual",
                                      "source_name": "电子信息"})
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 15, 10, 0), 1, 5)   # 2026-08-15 周六(weekday()=5)
    assert payload["sectors"] == []
    assert payload["signal_date"] is None and payload["close_date"] is None
    assert payload["signal_date"] == payload["close_date"]


def test_hot_sector_rel_strength_applied(monkeypatch):
    # composite=78(热)→ 热谓词命中 → HOT_WEIGHT_MODE="signal" → 按 HOT_SIGNAL_WEIGHTS 重算。
    # signal 模式 rel_strength 不计算(rel_map 空)恒 None;复合分须为 signal 权重重算(非 v3 默认 55/15/10/20)。
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "688981", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    # 末根 close = 22.8(signal_close 断言锚点);先跌后平 → 位置分高,热 signal 重算后非回避、可入推荐。
    # (brief 原上升序列在 signal 权重下全成分 回避 → 板块空,无法测到重算;此夹具保持 22.8 锚点但形状可存活)
    daily_closes = [22.8 + 0.1 * (54 - i) for i in range(55)] + [22.8] * 10
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily(daily_closes), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    assert payload["sectors"], "热板块应产生推荐"
    for s in payload["sectors"][0]["stocks"]:
        assert s["rel_strength"] is None                     # signal 模式:rel_map 空 → rel_strength 恒 None
        # 复合分按 HOT_SIGNAL_WEIGHTS 重算(板块 78 → sector_bonus=8;风险折扣保留)
        expected = an.stock_composite_v3(
            s["scores"]["position"], s["scores"]["volume_price"], s["scores"]["trend"],
            s["scores"]["signal"], s["scores"]["risk"], recommend.sector_bonus(78.0),
            rel_strength=None, weights=an.HOT_SIGNAL_WEIGHTS)
        assert s["composite"] == pytest.approx(round(expected, 2))
        assert s["signal_close"] == pytest.approx(22.8)      # 末根 close(价夹具,与权重无关)


def test_non_hot_sector_no_rel_strength(monkeypatch):
    # composite=60(非热)→ 谓词未命中 → 不套 rel_strength / signal 权重,保持 v3 权重。
    mock_sector(monkeypatch, composite=60.0, verdict="跟踪(热点延续)")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "688981", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    daily_closes = [22.8 + 0.1 * (54 - i) for i in range(55)] + [22.8] * 10
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily(daily_closes), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    assert payload["sectors"]
    s = payload["sectors"][0]["stocks"][0]
    assert "rel_strength" not in s or s["rel_strength"] is None


def test_rel_strengths_ties_and_small_base():
    # 并列分位 = 均值秩(spec §3.3):3 等值 → rank 2.0 → pct 2/3 → 66.67
    out = recommend._rel_strengths([("a", 0.01), ("b", 0.01), ("c", 0.01)])
    assert len(out) == 3 and all(abs(v - 66.67) < 0.01 for v in out.values())
    # 升序 → 33.33/66.67/100
    out2 = recommend._rel_strengths([("a", 0.01), ("b", 0.03), ("c", 0.05)])
    assert abs(out2["a"] - 33.33) < 0.01 and abs(out2["c"] - 100.0) < 0.01
    # 基数 <3 → 空(spec §3.3 回退 v3)
    assert recommend._rel_strengths([]) == {}
    assert recommend._rel_strengths([("a", 0.01)]) == {}
    assert recommend._rel_strengths([("a", 0.01), ("b", 0.02)]) == {}


def test_score_candidate_passes_high_low_open(monkeypatch):
    captured = {}
    def fake_score_stock(daily_df, quote, now):
        captured.update(quote)
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    row = {"code": "600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
           "volume": 100000, "amount": 2e8, "high": 5.5, "low": 4.5, "open": 5.1}
    daily = make_daily([10 + i for i in range(65)])
    out = recommend._score_candidate(row, daily, datetime.datetime(2026, 8, 11, 15, 0),
                                     sector_composite=78.0)
    assert out is not None
    assert captured["high"] == pytest.approx(5.5)
    assert captured["low"] == pytest.approx(4.5)
    assert captured["open"] == pytest.approx(5.1)


def test_score_candidate_missing_high_low_open_ok(monkeypatch):
    captured = {}
    def fake_score_stock(daily_df, quote, now):
        captured.update(quote)
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    row = {"code": "600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
           "volume": 100000, "amount": 2e8}              # 无 high/low/open
    daily = make_daily([10 + i for i in range(65)])
    out = recommend._score_candidate(row, daily, datetime.datetime(2026, 8, 11, 15, 0),
                                     sector_composite=78.0)
    assert out is not None
    assert captured["high"] is None
    assert captured["low"] is None
    assert captured["open"] is None
