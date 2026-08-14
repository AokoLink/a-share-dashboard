# -*- coding: utf-8 -*-
import json

import numpy as np
import pandas as pd
import pytest

import backtest as bt
import analysis as an
import predict as pr


def make_daily(closes, opens=None, volumes=None, start="2026-01-01", code="000001"):
    """构造日线 DataFrame,含 date/open/high/low/close/volume/code/change_pct。"""
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    vols = [float(v) for v in volumes] if volumes is not None else [100000.0] * n
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    closes = [float(c) for c in closes]
    change = [0.0] + [round((closes[i] / closes[i - 1] - 1) * 100, 6) if closes[i - 1] != 0 else 0.0
                      for i in range(1, n)]
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols, "code": [code] * n, "change_pct": change,
    })


def test_fwd_close_exact():
    d = make_daily([10.0, 11.0, 12.0, 13.0, 14.0])
    assert pr.fwd_close(d, 0, 1) == pytest.approx(0.1)
    assert pr.fwd_close(d, 0, 3) == pytest.approx(0.3)


def test_fwd_close_out_of_range_none():
    d = make_daily([10.0, 11.0])
    assert pr.fwd_close(d, 0, 2) is None
    assert pr.fwd_close(d, 1, 1) is None


def test_fwd_close_nonpositive_none():
    d = make_daily([10.0, 0.0, 11.0])
    assert pr.fwd_close(d, 0, 1) is None


def test_labels_all_four():
    closes = [10.0, 11.0, 12.0, 13.0]
    opens = [10.0, 10.5, 12.0, 13.0]
    d = make_daily(closes, opens=opens)
    lbl = pr._labels(d, 0)
    assert lbl["close1"] == 1
    assert lbl["gap"] == 1
    assert lbl["od"] == 1
    assert lbl["trend3"] == 1


def test_labels_trend3_none_when_short():
    d = make_daily([10.0, 11.0, 12.0])  # i=0 → i+3=3 >= len → trend3 None
    lbl = pr._labels(d, 0)
    assert lbl["close1"] in (0, 1)
    assert lbl["trend3"] is None


def test_labels_next_returns_none():
    d = make_daily([10.0])  # i=0 → i+1 >= len → next_returns None
    assert pr._labels(d, 0) is None


def test_fit_calibrator_bin_probabilities():
    pairs = [(1.0, 0)] * 10 + [(9.0, 1)] * 10
    cal = pr._fit_calibrator(pairs, 2)
    assert not cal.degraded
    assert cal.p_up(1.0) == pytest.approx(0.0)
    assert cal.p_up(9.0) == pytest.approx(1.0)


def test_fit_calibrator_pav_monotone():
    pairs = [(1.0, 1)] * 4 + [(2.0, 0)] * 4 + [(3.0, 1)] * 4
    cal = pr._fit_calibrator(pairs, 3)
    ps = [p for _, p in cal.bins]
    assert ps == sorted(ps)  # PAV 后单调不减
    assert cal.p_up(1.0) == pytest.approx(0.5)
    assert cal.p_up(2.0) == pytest.approx(0.5)
    assert cal.p_up(3.0) == pytest.approx(1.0)


def test_p_up_boundary_and_none():
    cal = pr._fit_calibrator([(1.0, 0)] * 5 + [(2.0, 1)] * 5, 2)
    assert cal.p_up(0.5) == pytest.approx(0.0)
    assert cal.p_up(1.0) == pytest.approx(0.0)
    assert cal.p_up(1.5) == pytest.approx(0.0)
    assert cal.p_up(2.0) == pytest.approx(1.0)
    assert cal.p_up(99.0) == pytest.approx(1.0)
    assert cal.p_up(None) is None
    assert cal.p_up(float("nan")) is None


def test_fit_calibrator_degraded_single_bin():
    cal = pr._fit_calibrator([(1.0, 1), (2.0, 0)], 10)
    assert cal.degraded
    assert len(cal.bins) == 1
    assert cal.p_up(1.0) == pytest.approx(0.5)


def test_forward_universe_filters():
    def mk(code, closes, vols):
        return make_daily(closes, volumes=vols, code=code)

    universe = {
        "000001": mk("000001", [10.0, 10.2, 10.4], [1e7] * 3),   # 正常(末 bar 无 T+1,仍保留)
        "000002": mk("000002", [5.0, 5.1], [1e7] * 2),          # 停牌:无 as_of_date bar
        "000003": mk("000003", [10.0, 10.0, 11.0], [1e7] * 3),  # 涨停:chg ≈ 10% >= 9.9
        "000004": mk("000004", [10.0, 10.0, 9.0], [1e7] * 3),   # 跌超 7%:-10%
        "000005": mk("000005", [10.0, 10.0, 10.1], [1e3] * 3),  # 低换手:vol*close < 1e8
    }
    pos_of = {c: {dt: i for i, dt in enumerate(d["date"])} for c, d in universe.items()}
    all_days = sorted(set().union(*[set(d["date"]) for d in universe.values()]))
    fwd = pr.forward_universe(universe, pos_of, all_days)
    assert set(fwd.keys()) == {"000001"}
    assert fwd["000001"] == 2  # 最后一天 index


def _fake_cals():
    up = pr._fit_calibrator([(5.0, 1)] * 10, 2)    # p=1
    down = pr._fit_calibrator([(5.0, 0)] * 10, 2)  # p=0
    return {"direction": up, "gap": down, "od": up, "trend3": up}


def test_predict_at_full_schema(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4})
    pred = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert pred["code"] == "000001"
    assert pred["date"] == d["date"].iloc[10]
    assert pred["composite"] == pytest.approx(61.4)
    t1 = pred["T+1"]
    assert t1["direction"] == "up"
    assert t1["confidence"] == pytest.approx(1.0)
    assert t1["gap"] == "low"
    assert t1["od"] == "up"
    assert t1["path"] == "低开高走"
    assert pred["T+3"]["direction"] == "up"
    assert pred["T+3"]["confidence"] == pytest.approx(1.0)


def test_predict_at_t3_no_future_bars(monkeypatch):
    # B2 修复:T+3 只由 calibrator 决定,不读 fwd_close;锚定最后一根仍产出
    d = make_daily([10.0] * 5)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 50.0})
    pred = pr.predict_at(d, 4, pr.AFTER_CLOSE, _fake_cals())  # i=4 最后一根
    assert pred["T+3"]["direction"] == "up"
    assert pred["T+3"]["confidence"] == pytest.approx(1.0)


def test_predict_at_composite_none_returns_none(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: None)
    assert pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals()) is None


def test_predict_at_no_future_leak(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4})
    before = pr.fwd_close(d, 10, 3)
    p1 = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    for col in ("open", "close"):
        d.iloc[11:14, d.columns.get_loc(col)] = 999.0
    after = pr.fwd_close(d, 10, 3)
    assert after != before  # 未来确实被改动
    p2 = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert p1 == p2  # 但预测逐位不变(不读未来)
