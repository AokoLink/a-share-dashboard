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
