# -*- coding: utf-8 -*-
"""core/forward.py 单元测试:合法窗口前向收益 + 诊断信心标签 + 序列化往返。"""
import pandas as pd
import pytest

from core import forward as fw
from core.calibration import Calibrator


def _frame(n=70):
    opens = [10.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame({
        "date": [f"2026-05-{i % 28 + 1:02d}" for i in range(n)],
        "open": opens,
        "high": [o * 1.01 for o in opens],
        "low": [o * 0.99 for o in opens],
        "close": opens,
        "volume": [1e6] * n,
        "code": ["600519"] * n,
    })


# ---------- 前向收益(精确值) ----------

def test_fwd_oo_exact():
    d = pd.DataFrame({"open": [10.0, 10.0, 11.0, 11.0, 12.1]})
    assert fw.fwd_oo(d, 0, 1) == pytest.approx(0.10)   # open[2]/open[1]-1 = 11/10-1
    assert fw.fwd_oo(d, 0, 2) == pytest.approx(0.10)   # open[3]/open[1]-1 = 11/10-1
    assert fw.fwd_oo(d, 0, 3) == pytest.approx(0.21)   # open[4]/open[1]-1 = 12.1/10-1
    assert fw.fwd_oo(d, 0, 5) is None                  # bar+1+5 越界


def test_fwd_oo_series_exact():
    d = pd.DataFrame({"open": [10.0, 10.0, 11.0, 11.0, 12.1]})
    s = fw.fwd_oo_series(d, 0, 3)
    assert len(s) == 3
    assert s[0] == pytest.approx(0.10)      # open[2]/open[1]-1
    assert s[1] == pytest.approx(0.0)       # open[3]/open[2]-1
    assert s[2] == pytest.approx(0.10)      # open[4]/open[3]-1
    assert fw.fwd_oo_series(d, 0, 4) is None


def test_left_tail_hit():
    d = pd.DataFrame({"open": [10.0, 10.0, 9.5, 9.5, 9.6]})
    # series = [-0.05, 0.0, ~0.0105];-5% 日 < -3% → 命中
    assert fw.left_tail_hit(d, 0, 2) == 1
    # 只看 j=1(单日 -5%):仍命中
    assert fw.left_tail_hit(d, 0, 1) == 1
    # 无深跌序列
    d2 = pd.DataFrame({"open": [10.0, 10.0, 10.1, 10.2]})
    assert fw.left_tail_hit(d2, 0, 2) == 0
    assert fw.left_tail_hit(d2, 0, 5) is None


# ---------- 信心标签 ----------

def _horizons(edge, net):
    h = {}
    for hh in fw.HORIZONS:
        h[str(hh)] = {"edge": edge, "net": net}
    return h


def test_confidence_insufficient_when_edge_below_band():
    label, best = fw._confidence(_horizons(0.01, 0.02))
    assert label == "信号不足,无法判断"
    assert best is None


def test_confidence_positive_expectation():
    label, best = fw._confidence(_horizons(0.05, 0.006))
    assert label == "有正向期望(超过成本)"
    assert best == fw.HORIZONS[0]


def test_confidence_weak_signal():
    label, best = fw._confidence(_horizons(0.05, -0.001))
    assert label == "弱方向信号,未超过成本"
    assert best == fw.HORIZONS[0]


def test_confidence_ignores_none_edge():
    h = {str(hh): {"edge": None, "net": 0.01} for hh in fw.HORIZONS}
    label, best = fw._confidence(h)
    assert label == "信号不足,无法判断"
    assert best is None


# ---------- diagnose_at ----------

def _const_cal(p):
    return Calibrator([(float("inf"), p)], degraded=True)


def _cals():
    return {h: {"direction": _const_cal(0.6), "return": _const_cal(0.01),
                "tail": _const_cal(0.05)} for h in fw.HORIZONS}


def _benches():
    return {h: {"base_up": 0.5, "median": 0.0, "mean": 0.0, "n": 1000} for h in fw.HORIZONS}


def test_diagnose_at_positive(monkeypatch):
    monkeypatch.setattr(fw.bt, "score_at", lambda d, i, now: {"composite": 70.0, "risk": 30.0})
    d = _frame()
    dg = fw.diagnose_at(d, len(d) - 1, _cals(), _benches())
    assert dg is not None
    assert dg["code"] == "600519"
    assert dg["confidence"] == "有正向期望(超过成本)"
    assert dg["best_horizon"] == 1
    assert dg["risk_high"] is False
    h1 = dg["horizons"]["1"]
    assert h1["p_up"] == pytest.approx(0.6)
    assert h1["edge"] == pytest.approx(0.10)
    assert h1["expected_return"] == pytest.approx(0.01)
    assert h1["exceeds_cost"] is True                      # 0.01 - 0.004 > 0
    assert h1["beats_benchmark"] is True                   # 0.01 > 0.0
    assert h1["left_tail"] == pytest.approx(0.05)


def test_diagnose_at_insufficient(monkeypatch):
    monkeypatch.setattr(fw.bt, "score_at", lambda d, i, now: {"composite": 50.0, "risk": 30.0})
    cals = {h: {"direction": _const_cal(0.51), "return": _const_cal(-0.01),
                "tail": _const_cal(0.05)} for h in fw.HORIZONS}
    d = _frame()
    dg = fw.diagnose_at(d, len(d) - 1, cals, _benches())
    assert dg["confidence"] == "信号不足,无法判断"
    assert dg["best_horizon"] is None


def test_diagnose_at_none_composite(monkeypatch):
    monkeypatch.setattr(fw.bt, "score_at", lambda d, i, now: None)
    assert fw.diagnose_at(_frame(), 69, _cals(), _benches()) is None


def test_diagnose_at_risk_high_flag(monkeypatch):
    monkeypatch.setattr(fw.bt, "score_at", lambda d, i, now: {"composite": 70.0, "risk": 70.0})
    d = _frame()
    dg = fw.diagnose_at(d, len(d) - 1, _cals(), _benches())
    assert dg["risk_high"] is True


# ---------- 序列化往返 ----------

def test_cal_to_from_json_roundtrip():
    cal = _const_cal(0.62)
    obj = fw._cal_to_json(cal)
    assert obj["bins"][0]["upper"] is None                 # inf → None
    back = fw._cal_from_json(obj)
    assert back.p_up(0.0) == pytest.approx(0.62)
    assert back.degraded is True


def test_prepare_frame_adds_code_and_change_pct():
    d = pd.DataFrame({
        "open": [10.0] * 70, "high": [10.1] * 70, "low": [9.9] * 70,
        "close": [10.0] * 69 + [11.0], "volume": [1e6] * 70,
    })
    f = fw.prepare_frame(d, "000001", tail_n=1200)
    assert f["code"].iloc[0] == "000001"
    assert "change_pct" in f.columns
    assert f["change_pct"].iloc[0] == 0.0
    # 末根 close 11.0 vs 前根 10.0 → +10%
    assert f["change_pct"].iloc[-1] == pytest.approx(10.0)


def test_prepare_frame_tail():
    d = pd.DataFrame({"open": [10.0] * 2000, "high": [10.1] * 2000,
                      "low": [9.9] * 2000, "close": [10.0] * 2000, "volume": [1e6] * 2000})
    f = fw.prepare_frame(d, "000001", tail_n=1200)
    assert len(f) == 1200
