# tests/test_analysis_stock.py
# -*- coding: utf-8 -*-
from datetime import datetime

import pandas as pd
import pytest

import analysis as an


def quote(**kw):
    q = {"name": "X", "code": "sh600000", "price": 10.0, "prev_close": 9.5,
         "open": 9.6, "high": 10.2, "low": 9.5, "volume": 1_000_000,
         "turnover": 5e7, "change_pct": 5.0}
    q.update(kw)
    return q


def make_daily(closes, volumes=None):
    """构造日线 DataFrame,date 递增。"""
    n = len(closes)
    vols = volumes or [100000] * n
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    opens = [c for c in closes]
    return pd.DataFrame({
        "date": [f"2026-01-{i+1:02d}" for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})


def dt_now(h=10, m=30):
    return datetime(2026, 8, 11, h, m)  # 周二盘中


def test_trend_score_bull_alignment():
    # 持续上升 → 多头排列 + 站上 MA20/60 → 高分
    closes = [float(10 + i * 0.3) for i in range(65)]
    df = make_daily(closes)
    assert an.compute_trend_score(df) == pytest.approx(100.0)
    # 持续下跌 → 破位 → 低分(基础40)
    df2 = make_daily([float(100 - i * 0.5) for i in range(65)])
    assert an.compute_trend_score(df2) == pytest.approx(40.0)
    # 数据不足 → 基础分
    assert an.compute_trend_score(make_daily([1.0, 2.0])) == pytest.approx(50.0)


def test_volume_price_score_volume_surge():
    # 量比2.0 + 涨幅5% → 放量上攻 +30
    df = make_daily([float(10 + i) for i in range(30)])
    df["volume"] = [100000] * 29 + [200000]   # 今日显著放量(但 avg5 用 -6:-1,今日计入后 5 日均量被抬高)
    # 注:avg5 取 [-6:-1] 不含最后一根 → 5日均量=100000
    q = quote(volume=400000, change_pct=5.0)  # 10:30 → 折算 400000*240/60=1600000 → 量比16
    s = an.compute_volume_price_score(df, q, dt_now())
    assert s == pytest.approx(70.0)  # 40+30(放量上攻);未突破平台


def test_volume_price_shrink_healthy_pullback():
    # 缩量(量比<0.7)+ 跌幅≤3% + 站上 MA20 → 缩量健康回踩
    closes = [float(10 + i) for i in range(30)]
    df = make_daily(closes)
    df["volume"] = [100000] * 29 + [100000]
    q = quote(volume=10000, change_pct=-1.0, price=30.0)  # 10:30 折算量比≈0.4
    s = an.compute_volume_price_score(df, q, dt_now())
    # 站上 MA20(close=30 > ma20≈24.5) + 缩量 + 跌幅≤3 → +30
    assert s == pytest.approx(70.0)


def test_signal_score_macd_golden_cross_and_breakout():
    # 构造 V 型:前段下跌后急升 → 金叉
    closes = [float(50 - i) for i in range(30)] + [float(20 + i * 2) for i in range(30)]
    df = make_daily(closes)
    q = quote(price=closes[-1] * 1.1)  # 突破前20日平台
    s = an.compute_signal_score(df, q)
    assert s > 50.0  # 金叉 +40,突破 +30 → 120 封顶 100


def test_stock_risk_max_semantics():
    # 乖离率>15% → 70
    closes = [10.0] * 30
    closes[-1] = 30.0   # MA20≈11 → 乖离≈173%
    df = make_daily(closes)
    q = quote(price=30.0, change_pct=10.0)
    assert an.compute_stock_risk(df, q, dt_now()) == pytest.approx(70)
    # 放量跌破 MA20 → 70(风险分=max,两风险项取最大值 70)
    closes2 = [10.0] * 30
    df2 = make_daily(closes2)
    q2 = quote(price=5.0, change_pct=-5.0, volume=500000)  # 10:30 量比20>1.5,破MA20
    assert an.compute_stock_risk(df2, q2, dt_now()) == pytest.approx(70)
    # 无风险项 → 0(温和上涨、缩量、价格≈MA20、无长上影)
    df3 = make_daily([float(10 + i) for i in range(30)])
    q3 = quote(price=19.5, change_pct=1.0, volume=20000,
               high=19.6, low=19.4, open=19.5)   # 量比≈0.8<1.5,乖离≈0,无上影
    assert an.compute_stock_risk(df3, q3, dt_now()) == pytest.approx(0)


def test_stock_composite_and_verdict():
    assert an.stock_composite(70, 65, 60) == pytest.approx(65.75)  # 规格 §8 校验
    # 综合分表:≥70 无重大→关注;≥70 有重大→规避
    assert an.stock_verdict(80, 30) == "关注"
    assert an.stock_verdict(80, 70) == "规避"
    # 55-69 无重大→持有/跟踪;有重大→回调风险
    assert an.stock_verdict(60, 20) == "持有/跟踪"
    assert an.stock_verdict(60, 70) == "回调风险"
    # <55 无重大→观望;有重大→规避
    assert an.stock_verdict(50, 30) == "观望"
    assert an.stock_verdict(50, 70) == "规避"


def test_score_stock_wrapper():
    closes = [float(10 + i * 0.2) for i in range(65)]
    df = make_daily(closes)
    q = quote(price=closes[-1], change_pct=2.0, volume=300000)
    out = an.score_stock(df, q, dt_now(15, 0))  # 收盘后
    assert set(out) == {"trend", "volume_price", "signal", "risk", "composite", "verdict"}
    assert 0 <= out["risk"] <= 100
