# -*- coding: utf-8 -*-
import pytest
from core import analysis as an


def test_emotion_score_weights_and_renormalize():
    # 全子项齐备:up 0.9, limit 0.1, turnover_ratio 1.5, leader 5.0
    e = an.sector_emotion(0.9, 0.1, 1.5, 5.0)
    # 0.9*100=90(40) + 0.1*100=10(20) + 1.5/1.5*100=100(25) + 5*20=100(15)
    assert e == pytest.approx((90 * 40 + 10 * 20 + 100 * 25 + 100 * 15) / 100)
    # 盘中:turnover_ratio=None → 25 权重归一化到剩余 75
    e2 = an.sector_emotion(0.9, 0.1, None, 5.0)
    assert e2 == pytest.approx((90 * 40 + 10 * 20 + 100 * 15) / 75)
    # 缺 leader → leader 15 权重归一化到剩余 85(降级口径下涨停占比恒 None,同样由调用方归一化)
    e3 = an.sector_emotion(0.9, 0.1, 1.5, None)
    assert e3 == pytest.approx((90 * 40 + 10 * 20 + 100 * 25) / 85)
    assert an.sector_emotion(None, None, None, None) is None


def test_strength_score():
    s = an.sector_strength(5.0, 4, 0.03)   # 指数5%→100,上榜4天→100,活跃3%→100
    assert s == pytest.approx(100.0)
    s2 = an.sector_strength(1.0, 1, None)  # 活跃度缺失 → 40/30 归一化
    assert s2 == pytest.approx((20 * 40 + 25 * 30) / 70)
    s3 = an.sector_strength(None, 2, 0.03)  # 指数涨幅缺失 → 该子项降级,其余归一化
    assert s3 == pytest.approx((50 * 30 + 100 * 30) / 60)


def test_risk_single_and_double():
    # 单个信号:仅 3日累计>10% → 40(中档)
    assert an.sector_risk(3.0, 12.0, None, None, 0.6) == pytest.approx(40)
    # 两个信号:3日累计>10% + 严重分化 → 80(高)
    assert an.sector_risk(3.0, 12.0, None, None, 0.3) == pytest.approx(80)
    # 单日涨幅>5% 单信号 → 40
    assert an.sector_risk(6.0, None, None, None, 0.6) == pytest.approx(40)


def test_risk_voluptake_only_after_close():
    # 盘中:turnover_ratio=None → 放量滞涨不评估
    r_daytime = an.sector_risk(0.5, None, None, 1.5, 0.6)
    assert r_daytime == pytest.approx(0)
    # 收盘后:放量滞涨(成交额1.8倍均值 + 指数涨0.5<前日1.5 且 <2%)→ +40
    r_close = an.sector_risk(0.5, None, 1.8, 1.5, 0.6)
    assert r_close == pytest.approx(40)
    # 指数涨幅≥2% → 不构成滞涨
    assert an.sector_risk(2.5, None, 1.8, 1.5, 0.6) == pytest.approx(0)
    # 指数涨幅 ≥ 前日 → 不构成滞涨
    assert an.sector_risk(2.0, None, 1.8, 1.0, 0.6) == pytest.approx(0)


def test_verdict_rule1_2_3():
    # P1:强度低 + 风险高 → 回避
    assert an.sector_verdict(20, 20, 80, 1, True) == "风险提示/回避"
    # P2:风险高 + 情绪≥中 → 谨慎追高(全中+高风险,规格 C5')
    assert an.sector_verdict(50, 50, 70, 1, True) == "谨慎追高(过热)"
    # P2:风险高 + 强度≥中
    assert an.sector_verdict(20, 65, 80, 3, True) == "谨慎追高(过热)"
    # P3:情绪高 + 强度高 + 风险非高 → 建议关注
    assert an.sector_verdict(75, 70, 30, 2, True) == "建议关注"


def test_verdict_rule4_5_6():
    # P4:情绪高 + 强度中 + 连续上榜≥2 → 跟踪
    assert an.sector_verdict(75, 55, 20, 2, True) == "跟踪(热点延续)"
    # P5:情绪高 + 强度中/低 + 连续=1 + data_complete → 警惕一日游
    assert an.sector_verdict(75, 55, 20, 1, True) == "警惕一日游"
    # 强度=低(30<35)同样触发 P5(规格"中/低",最终评审修复项)
    assert an.sector_verdict(75, 30, 20, 1, True) == "警惕一日游"
    # 冷启动 day1:data_complete=False → 不触发一日游 → 观望
    assert an.sector_verdict(75, 55, 20, 1, False) == "观望"
    # P6 其余 → 观望
    assert an.sector_verdict(50, 30, 50, 1, True) == "观望"


def test_composite_arithmetic():
    # 规格 §8 校验:0.4*82 + 0.35*78 + 0.25*(100-55) = 71.35(综合分=0.4强度+0.35情绪+0.25×(100−风险);规格示例 risk=55)
    assert an.composite_score(82, 78, 55) == pytest.approx(71.35)
    # 任一维度缺失 → None
    assert an.composite_score(None, 78, 55) is None


class FakeStore:
    """最小 store_ctx:无历史 → 各 getter 返回 None;consecutive 固定 2(模拟已连续上榜 ≥2 天)。"""
    def get_sector_turnover_avg(self, db, t, c, d, days=5): return None
    def get_sector_prev_change(self, db, t, c, d): return None
    def get_sector_change_3d(self, db, t, c, d): return None
    def get_consecutive_days(self, db, t, c, d, top_n=20): return 2


def test_score_sector_composes_five():
    # 与单函数口径一致:emotion/strength 全高、risk 0 → 建议关注
    r = an.score_sector({
        "change_pct": 5.0, "up_ratio": 0.9, "limit_ratio": None,
        "leader_change_pct": 5.0, "turnover_ratio": None,
        "consecutive_days": 2, "activity": 0.03,
        "change_3d": None, "prev_change": None, "data_complete": True,
    })
    # emotion: up 90(40) + leader 100(15),limit/turnover 缺失 → 权重归一化到 55
    assert r["emotion"] == pytest.approx((90 * 40 + 100 * 15) / 55)
    # strength: 指数5%→100(40) + 连续2天→50(30) + 活跃3%→100(30)
    assert r["strength"] == pytest.approx((100 * 40 + 50 * 30 + 100 * 30) / 100)
    assert r["risk"] == pytest.approx(0)                    # 5.0 非 >5,无前日/3d/turnover_ratio
    assert r["composite"] == pytest.approx(91.45)           # round(0.4*85 + 0.35*92.73 + 0.25*100, 2)
    assert r["verdict"] == "建议关注"


def test_collect_sector_metrics_full_pipeline():
    import datetime
    now = datetime.datetime(2026, 8, 11, 15, 0)
    # 无历史:turnover_ratio=None, prev=None, 3d=None;consecutive=2(FakeStore)
    r = an.collect_sector_metrics(
        ":db:", "industry", "885887", 5.0, 1e10, 90.0, 5.0, 5.0,
        FakeStore(), 1e12, now)
    assert set(r) == {"emotion", "strength", "risk", "composite", "verdict", "consecutive_days", "overheated"}
    assert r["consecutive_days"] == 2
    # up 94.7% → 情绪 96.17;强度=(100*40+50*30+33.3*30)/100=65 ≥ 60 → P3 建议关注;risk 0
    assert r["verdict"] == "建议关注"


def test_collect_sector_metrics_after_close_uses_turnover_ratio():
    import datetime
    now = datetime.datetime(2026, 8, 11, 15, 0)

    class StoreWithHistory(FakeStore):
        def get_sector_turnover_avg(self, db, t, c, d, days=5): return 5e9

    r = an.collect_sector_metrics(
        ":db:", "industry", "885887", 0.5, 1e10, 50.0, 50.0, 1.0,
        StoreWithHistory(), 1e12, now)
    # 收盘后 turnover=1e10 / avg5=5e9 → turnover_ratio=2.0,计入情绪
    assert r["emotion"] == pytest.approx((50 * 40 + 100 * 25 + 20 * 15) / 80)  # 60.0
    # prev=None → 放量滞涨分支需 prev_change,不触发;指数涨0.5<5 → risk 0
    assert r["risk"] == pytest.approx(0)


def test_sector_verdict_overheat_distinct_label(monkeypatch):
    from core import analysis as an
    # 连涨≥4 + e_hi + s_hi + not r_hi → overheated;路径 A → 独立标签
    monkeypatch.setattr(an, "OVERHEAT_MIN_DAYS", 4)
    monkeypatch.setattr(an, "P0_PATH", "intercept")
    out = an.score_sector({
        "up_ratio": 0.8, "limit_ratio": None, "turnover_ratio": 1.0,
        "leader_change_pct": 5.0, "change_pct": 2.0, "consecutive_days": 4,
        "change_3d": 6.0, "prev_change": 2.0, "activity": 0.05, "data_complete": True})
    assert out["overheated"] is True
    assert out["verdict"] == "过热(连涨)"
    assert out["verdict"] != "谨慎追高(过热)"      # 不与风险驱动标签撞名
    assert out["composite"] == an.composite_score(out["strength"], out["emotion"], out["risk"])  # composite 未变


def test_sector_verdict_overheat_badge_path(monkeypatch):
    from core import analysis as an
    monkeypatch.setattr(an, "OVERHEAT_MIN_DAYS", 4)
    monkeypatch.setattr(an, "P0_PATH", "badge")
    out = an.score_sector({
        "up_ratio": 0.8, "limit_ratio": None, "turnover_ratio": 1.0,
        "leader_change_pct": 5.0, "change_pct": 2.0, "consecutive_days": 4,
        "change_3d": 6.0, "prev_change": 2.0, "activity": 0.05, "data_complete": True})
    assert out["overheated"] is True
    assert out["verdict"] == "建议关注"           # 路径 B 保留推荐,徽章在前端
