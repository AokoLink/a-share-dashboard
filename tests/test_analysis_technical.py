# -*- coding: utf-8 -*-
"""技术指标 + 持有建议纯函数单测(设计 2026-08-16 swing-recommend-technical-detail)。"""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from core import analysis as an


def _daily(closes, highs=None, lows=None, opens=None, volume=None, turnover=None):
    n = len(closes)
    highs = highs if highs is not None else [c * 1.01 for c in closes]
    lows = lows if lows is not None else [c * 0.99 for c in closes]
    opens = opens if opens is not None else list(closes)
    data = {"close": list(closes), "high": list(highs), "low": list(lows), "open": list(opens)}
    if volume is not None:
        data["volume"] = list(volume)
    if turnover is not None:
        data["turnover"] = list(turnover)
    return pd.DataFrame(data)


def dt_now(h=15, m=0):
    return datetime(2026, 8, 14, h, m)


# ---- add_kdj ----

def test_add_kdj_exact_values():
    # close=10..19,high=close+1,low=close-1 → 每根 RSV 恒为 90。
    closes = [float(10 + i) for i in range(10)]
    df = an.add_kdj(_daily(closes, highs=[c + 1 for c in closes], lows=[c - 1 for c in closes]))
    # i=8(第9根)首次有效:RSV=90,K=(2*50+90)/3,D=(2*50+K)/3,J=3K-2D
    assert df["k"].iloc[8] == pytest.approx(190 / 3, abs=1e-9)
    assert df["d"].iloc[8] == pytest.approx(490 / 9, abs=1e-9)
    assert df["j"].iloc[8] == pytest.approx(730 / 9, abs=1e-9)
    # i=9 次根
    assert df["k"].iloc[9] == pytest.approx(650 / 9, abs=1e-9)
    assert df["d"].iloc[9] == pytest.approx(1630 / 27, abs=1e-9)
    assert df["j"].iloc[9] == pytest.approx(2590 / 27, abs=1e-9)
    # 前 8 根不足 9 窗口 → NaN
    assert np.isnan(df["k"].iloc[0]) and np.isnan(df["d"].iloc[0])


def test_add_kdj_one_line_board_is_nan():
    # 9 根一字板(High9==Low9)→ RSV NaN → K/D/J NaN(不落值)
    closes = [10.0] * 9
    df = an.add_kdj(_daily(closes, highs=closes, lows=closes))
    assert np.isnan(df["k"].iloc[8])
    assert np.isnan(df["d"].iloc[8])
    assert np.isnan(df["j"].iloc[8])


# ---- kdj_state ----

def test_kdj_state_golden_cross():
    assert an.kdj_state(60, 50, 80, 40, 50) == ("中性", "金叉")


def test_kdj_state_dead_cross():
    assert an.kdj_state(40, 50, 20, 60, 50) == ("中性", "死叉")


def test_kdj_state_bullish():
    assert an.kdj_state(60, 50, 80, 60, 50) == ("中性", "多头")


def test_kdj_state_bearish():
    assert an.kdj_state(40, 50, 20, 40, 50) == ("中性", "空头")


def test_kdj_state_equal():
    assert an.kdj_state(50, 50, 50, 40, 50) == ("中性", "—")


def test_kdj_state_overbought_oversold():
    assert an.kdj_state(85, 85, 85, 80, 80)[0] == "超买"
    assert an.kdj_state(15, 15, 15, 20, 20)[0] == "超卖"


def test_kdj_state_nan():
    assert an.kdj_state(None, 50, 80, 40, 50) == (None, None)
    assert an.kdj_state(60, 50, 80, float("nan"), 50) == (None, None)


# ---- macd_state ----

def test_macd_state_golden_cross_above_zero():
    assert an.macd_state(0.5, 0.3, 0.2, 0.3) == ("金叉", "零轴上")


def test_macd_state_dead_cross_above_zero():
    assert an.macd_state(0.3, 0.5, 0.5, 0.3) == ("死叉", "零轴上")


def test_macd_state_bullish():
    assert an.macd_state(0.5, 0.3, 0.5, 0.3) == ("多头", "零轴上")


def test_macd_state_bearish_below_zero():
    assert an.macd_state(-0.5, -0.3, -0.5, -0.3) == ("空头", "零轴下")


def test_macd_state_nan():
    assert an.macd_state(None, 0.3, 0.2, 0.3) == ("—", None)


# ---- volume_price_state ----

def test_volume_price_state_branches():
    assert an.volume_price_state(2.0, 1.5) == "放量上涨"
    assert an.volume_price_state(0.5, 0.5) == "缩量上涨"
    assert an.volume_price_state(-2.0, 1.5) == "放量下跌"
    assert an.volume_price_state(-0.5, 0.5) == "缩量回调"
    assert an.volume_price_state(0.5, 1.0) == "平量"
    assert an.volume_price_state(0.5, None) == "数据不足"


# ---- amp20 ----

def test_amp20_exact_mean():
    # close 恒 10 → prev_close=10;high=11,low=9 → 每根振幅 20.0%
    closes = [10.0] * 25
    df = _daily(closes, highs=[11.0] * 25, lows=[9.0] * 25)
    assert an.amp20(df) == 20.0


def test_amp20_insufficient():
    assert an.amp20(_daily([10.0] * 20)) is None


# ---- compute_technical_indicators ----

def test_compute_technical_indicators_limited():
    assert an.compute_technical_indicators(_daily([10.0] * 60), {}, dt_now()) == {"history_limited": True}


def test_compute_technical_indicators_full():
    closes = [10.0 + i * 0.1 for i in range(70)]
    df = _daily(closes, volume=[1_000_000] * 70, turnover=[0.05] * 70)
    quote = {"price": closes[-1], "volume": 1_000_000, "change_pct": 1.5}
    out = an.compute_technical_indicators(df, quote, dt_now())
    assert out["history_limited"] is False
    assert set(out["ma"]) == {"ma5", "ma10", "ma20", "ma60"}
    assert out["ma"]["ma60"] is not None
    assert out["macd"]["cross"] in {"金叉", "死叉", "多头", "空头", "—"}
    assert out["macd"]["zero"] in {"零轴上", "零轴下"}
    assert set(out["kdj"]) == {"k", "d", "j", "state", "cross"}
    assert out["kdj"]["k"] is not None
    assert out["rsi"]["state"] in {"超买", "超卖", "中性"}
    assert out["bias"]["ma20"] is not None
    assert out["pos60"] is not None
    assert out["turnover_pct"] == pytest.approx(5.0)
    assert set(out["volume_price"]) == {"vr", "vol_ratio", "state"}


# ---- build_hold_advice ----

_REGIME = {"label": "震荡", "action": "hold", "message": "持有(中性,启发式,未回测)"}


def test_build_hold_advice_normal():
    r = an.build_hold_advice(_REGIME, 30, 0.62)
    assert r["stock"]["risk"] == 30
    assert r["stock"]["risk_note"] == "个股风险可控"
    assert r["stock"]["pos_note"] == "位置中性"
    assert "个股方向无算法 edge(历史回测证伪)" in r["summary"]
    assert r["regime"] is _REGIME


def test_build_hold_advice_tail_risk():
    r = an.build_hold_advice(_REGIME, 70, 0.9)
    assert r["stock"]["risk_note"] == "个股风险尾部,注意回撤"
    assert r["stock"]["pos_note"] == "60 日高位,注意追高风险"


def test_build_hold_advice_none():
    r = an.build_hold_advice(_REGIME, None, None)
    assert r["stock"]["risk_note"] == "历史不足,无法评估风险"
    assert r["stock"]["pos_note"] == "历史不足,无法评估位置"
    assert r["stock"]["risk"] is None
    assert r["stock"]["pos60"] is None


def test_build_hold_advice_no_directional_keys():
    r = an.build_hold_advice(_REGIME, 30, 0.62)
    for key in ("verdict", "tier", "composite"):
        assert key not in r
        assert key not in r["stock"]
