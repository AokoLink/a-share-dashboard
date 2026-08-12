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


def test_bias_risk_gradient_continuous():
    # 断点 15/20/25/30 两侧极限一致(分段线性,无台阶):
    # 15⁻=15⁺=0;20⁻=40⁺=40;25⁻=25⁺=40;30⁻=30⁺=70;中段线性
    assert an._bias_risk(15) == pytest.approx(0)
    assert an._bias_risk(15.0001) == pytest.approx(40 * 0.0001 / 5)
    assert an._bias_risk(19) == pytest.approx(40 * 4 / 5)        # 19 → 32
    assert an._bias_risk(20) == pytest.approx(40)
    assert an._bias_risk(20.0001) == pytest.approx(40)            # 20-25 平段
    assert an._bias_risk(25) == pytest.approx(40)
    assert an._bias_risk(25.0001) == pytest.approx(40 + 30 * 0.0001 / 5)
    assert an._bias_risk(30) == pytest.approx(70)
    assert an._bias_risk(30.0001) == pytest.approx(70)            # >30 恒 70
    assert an._bias_risk(100) == pytest.approx(70)


def test_stock_risk_v3_bias_only():
    # 65 根平量,价格高出 MA20 约 18% → 仅乖离项:40*3/5 = 24
    df = make_daily([9.8] * 63 + [11.8] * 2)      # MA20≈10.0 → bias≈18%
    q = quote(price=11.8, change_pct=0.5, volume=100000)
    assert an.compute_stock_risk(df, q, dt_now(15, 0)) == pytest.approx(24.0)


def test_stock_risk_v3_additive_cap100():
    # 乖离82%→70 + 放量滞涨→25 + 高位长上影→20 = 115 → cap 100(近20日回撤项此 fixture 为 0,未触发;见 max_drawdown_20 单测)
    df = make_daily([10.0] * 24 + [19.8, 20.0])
    q = quote(price=20.0, change_pct=1.0, volume=500000,
              high=21.0, low=19.5, open=19.8)
    assert an.compute_stock_risk(df, q, dt_now(15, 0)) == pytest.approx(100.0)


def test_stock_risk_v3_none():
    df = make_daily([float(10 + i) for i in range(65)])
    q = quote(price=df["close"].iloc[-1], change_pct=1.0, volume=100000,
              high=df["close"].iloc[-1] * 1.01, low=df["close"].iloc[-1] * 0.99,
              open=df["close"].iloc[-1])
    assert an.compute_stock_risk(df, q, dt_now(15, 0)) == pytest.approx(0.0)


def test_composite_v3_risk_discount_and_bonus_before_discount():
    # quality=50:bonus=0 → 50*(1-0.5)=25;bonus=10 → (50+10)*(1-0.5)=30(加成在折扣前)
    base = dict(position=50.0, vp=50.0, trend=50.0, signal=50.0)
    assert an.stock_composite_v3(risk=50.0, sector_bonus=0, **base) == pytest.approx(25.0)
    assert an.stock_composite_v3(risk=50.0, sector_bonus=10, **base) == pytest.approx(30.0)
    assert an.stock_composite_v3(risk=0.0, sector_bonus=-5, **base) == pytest.approx(45.0)
    assert an.stock_composite_v3(position=None, vp=50, trend=50, signal=50, risk=10) is None


def test_verdict_v3_five_tiers_unrounded():
    assert an.stock_verdict(63) == "强烈关注"
    assert an.stock_verdict(58) == "关注"
    assert an.stock_verdict(57.996) == "持有/跟踪"   # 未舍入判定,避免两次 round 跨档
    assert an.stock_verdict(58.004) == "关注"
    assert an.stock_verdict(48) == "持有/跟踪"
    assert an.stock_verdict(47.99) == "观望"
    assert an.stock_verdict(38) == "观望"
    assert an.stock_verdict(37.99) == "回避"
    assert an.stock_verdict(None) is None


def test_score_stock_v3_guard_and_no_verdict():
    short = make_daily([float(10 + i) for i in range(40)])   # <61 根
    out = an.score_stock(short, quote(), dt_now(15, 0))
    assert set(out) == {"position", "trend", "volume_price", "signal", "risk", "composite"}
    assert out == {"position": None, "trend": None, "volume_price": None,
                   "signal": None, "risk": None, "composite": None}
    full = make_daily([float(10 + i * 0.2) for i in range(65)])
    out2 = an.score_stock(full, quote(price=full["close"].iloc[-1], change_pct=2.0,
                                      volume=100000), dt_now(15, 0))
    assert set(out2) == {"position", "trend", "volume_price", "signal", "risk", "composite"}
    assert "verdict" not in out2                              # 无 verdict 泄漏(第二轮#2)
    assert 0 <= out2["composite"] <= 100
    # composite = stock_composite_v3(bonus=0)
    expected = an.stock_composite_v3(out2["position"], out2["volume_price"],
                                     out2["trend"], out2["signal"], out2["risk"])
    assert out2["composite"] == pytest.approx(expected)


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
