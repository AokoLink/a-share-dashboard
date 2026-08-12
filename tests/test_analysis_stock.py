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


def dt_now(h=15, m=0):
    return datetime(2026, 8, 11, h, m)  # 周二收盘后(默认)


def test_vol_health_tiers():
    assert an._vol_health(1.5) == pytest.approx(35)
    assert an._vol_health(1.51) == pytest.approx(26)
    assert an._vol_health(2.5) == pytest.approx(26)
    assert an._vol_health(2.51) == pytest.approx(12)
    assert an._vol_health(None) == pytest.approx(12)


def test_price_volume_tiers():
    assert an._price_volume(5, 1.5) == pytest.approx(35)
    assert an._price_volume(5, None) == pytest.approx(25)     # vr 缺失不误判
    assert an._price_volume(0.5, 0.5) == pytest.approx(15)
    assert an._price_volume(0.5, None) == pytest.approx(25)
    assert an._price_volume(-2, 1.5) == pytest.approx(5)
    assert an._price_volume(-2, 0.5) == pytest.approx(25)


def test_vol_sustain_sweet_zone():
    # r=0.7/1.0/1.5 边界 → 6/20/30/12(甜区 1.0~1.5 最高,≥1.5 回落)
    assert an._vol_sustain(0.69) == pytest.approx(6)
    assert an._vol_sustain(0.7) == pytest.approx(20)
    assert an._vol_sustain(0.99) == pytest.approx(20)
    assert an._vol_sustain(1.0) == pytest.approx(30)
    assert an._vol_sustain(1.49) == pytest.approx(30)
    assert an._vol_sustain(1.5) == pytest.approx(12)
    assert an._vol_sustain(None) == pytest.approx(0)


def test_macd_branch_priority():
    # 当根金叉零轴上 30 > 零轴下 15 > 维持多头 12 > 弱多头 8 > 其余 0
    assert an._macd_branch(1.0, 0.0, -1.0, 0.0) == pytest.approx(30)
    assert an._macd_branch(-1.0, -2.0, -3.0, -2.0) == pytest.approx(15)
    assert an._macd_branch(1.0, 0.0, 0.5, 0.3) == pytest.approx(12)   # 非当根,DIF>0
    assert an._macd_branch(-1.0, -2.0, -1.5, -2.5) == pytest.approx(8)
    assert an._macd_branch(-2.0, -1.0, -2.5, -1.5) == pytest.approx(0)


def test_rsi_and_momentum_tiers():
    assert an._rsi_score(30) == pytest.approx(25)   # r<30 落 ≤65 → 超卖修复加分
    assert an._rsi_score(65) == pytest.approx(25)
    assert an._rsi_score(65.1) == pytest.approx(12)
    assert an._rsi_score(80) == pytest.approx(12)
    assert an._rsi_score(80.1) == pytest.approx(5)
    assert an._momentum_score(0) == pytest.approx(15)
    assert an._momentum_score(8) == pytest.approx(15)
    assert an._momentum_score(8.1) == pytest.approx(3)
    assert an._momentum_score(-1) == pytest.approx(8)
    assert an._momentum_score(-3) == pytest.approx(8)
    assert an._momentum_score(-3.1) == pytest.approx(3)


def test_breakout_gated_by_pos60():
    # pos60≥0.6 时创新高不加分;pos60<0.6 时加 20(放量)或 10(不放量)
    assert an._breakout_score(0.7, 10.0, 9.5, 1.5) == pytest.approx(0)
    assert an._breakout_score(0.3, 10.0, 9.5, 1.5) == pytest.approx(20)
    assert an._breakout_score(0.3, 10.0, 9.5, 1.0) == pytest.approx(10)
    assert an._breakout_score(0.3, 9.0, 9.5, 1.5) == pytest.approx(0)


def test_volume_price_score_v3_exact():
    # 65 根平量 100000,收盘后 vr=1.0:分项一 35 + 分项二(涨0.5%,vr 1.0)→25 + 分项三 r=1.0→30 = 90
    df = make_daily([float(10 + i) for i in range(65)])
    q = quote(price=df["close"].iloc[-1], change_pct=0.5, volume=100000)
    assert an.compute_volume_price_score(df, q, dt_now(15, 0)) == pytest.approx(90.0)


def test_signal_score_v3_steady_rise():
    # 持续上升 65 根:MACD 已金叉维持(DIF>0)→12 + RSI≈100→5 + 突破被门控(高位)→0
    # + ret5=(74/69-1)=7.25% → 动量 15 = 32
    df = make_daily([float(10 + i) for i in range(65)])
    q = quote(price=df["close"].iloc[-1], change_pct=2.0, volume=100000)
    assert an.compute_signal_score(df, q, dt_now(15, 0)) == pytest.approx(32.0)


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


def test_bias_sweet_boundaries():
    # 0/3/8/15/20 处连续;−8/−3 残留台阶存在但偏转 < 一个档位宽(5)
    assert an._bias_sweet(0) == pytest.approx(35)
    assert an._bias_sweet(0.01) == pytest.approx(35 + 65 * (0.01 / 3))
    assert an._bias_sweet(3) == pytest.approx(100)
    assert an._bias_sweet(3.1) == pytest.approx(100 - 0.1 * 12)
    assert an._bias_sweet(8) == pytest.approx(40)
    assert an._bias_sweet(15) == pytest.approx(12)
    assert an._bias_sweet(15.1) == pytest.approx(12 - 0.1 * 1.4)
    assert an._bias_sweet(20) == pytest.approx(5)
    assert an._bias_sweet(20.1) == pytest.approx(5)
    assert an._bias_sweet(-8.01) == pytest.approx(0)   # 台阶落在 −8 处(规格 §4.1:−8 起入 15 档)
    assert an._bias_sweet(-8) == pytest.approx(15)
    assert an._bias_sweet(-3) == pytest.approx(35)
    assert an._bias_sweet(-3.01) == pytest.approx(15)


def test_platform_score_three_tiers():
    assert an._platform_score(8) == pytest.approx(30)
    assert an._platform_score(8.1) == pytest.approx(15)
    assert an._platform_score(15) == pytest.approx(15)
    assert an._platform_score(15.1) == pytest.approx(0)
    assert an._platform_score(None) == pytest.approx(0)


def test_position_score_flat_zero_bias():
    # 65 根全平:pos60=0.5→pos_factor=50;bias=0→甜区35;platform amp=2%→30
    # position = 0.5*50 + 0.35*35 + 0.15*30 = 41.75
    assert an.compute_position_score(make_daily([10.0] * 65)) == pytest.approx(41.75)


def test_position_score_ma20_zero_guard():
    # 全部价格为 0:MA20=0 → bias 按 0 处理,不抛异常;pos60 分母 0 → 0.5
    assert an.compute_position_score(make_daily([0.0] * 65)) == pytest.approx(41.75)
    assert an.compute_position_score(make_daily([1.0] * 10)) == pytest.approx(0.0)  # 数据不足 → 0


def test_rsi14_wilder():
    # 全涨 → 100;全跌 → 0;全平 → 50
    assert an.rsi14(pd.Series([10.0 + i for i in range(20)])) == pytest.approx(100)
    assert an.rsi14(pd.Series([30.0 - i for i in range(20)])) == pytest.approx(0)
    assert an.rsi14(pd.Series([10.0] * 20)) == pytest.approx(50)


def test_max_drawdown_20():
    closes = [10.0] * 10 + [10.0, 9.0, 8.0, 7.0, 8.0, 9.0]   # 峰值 10 → 谷底 7 → −30%
    assert an.max_drawdown_20(make_daily(closes)) == pytest.approx(-30)
    assert an.max_drawdown_20(make_daily([10.0] * 20)) == pytest.approx(0)


def _ma_df(ma5, ma10):
    return pd.DataFrame({"ma5": [float(x) for x in ma5], "ma10": [float(x) for x in ma10]})


def test_trend_fresh_cross():
    # 近 5 根内 MA5 上穿 MA10 → 15(构造:末尾两根前 MA5≤MA10,末根 MA5>MA10)
    assert an._trend_fresh(_ma_df([10, 9, 11], [10, 10, 10])) == pytest.approx(15)
    # 当前 MA5>MA10 但无新交叉 → 8
    assert an._trend_fresh(_ma_df([9, 10, 11], [8, 9, 10])) == pytest.approx(8)
    # MA5<MA10 → 0
    assert an._trend_fresh(_ma_df([10, 10, 9], [10, 11, 11])) == pytest.approx(0)


def test_trend_score_v3():
    # 持续上升:order3(40)+ fresh8 = 48;持续下跌:order0 + fresh0 = 0
    assert an.compute_trend_score(make_daily([float(10 + i * 0.3) for i in range(65)])) == pytest.approx(48.0)
    assert an.compute_trend_score(make_daily([float(100 - i * 0.5) for i in range(65)])) == pytest.approx(0.0)
    assert an.compute_trend_score(make_daily([1.0, 2.0])) == pytest.approx(0.0)  # 数据不足
