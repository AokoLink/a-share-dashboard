# -*- coding: utf-8 -*-
"""compare.py 快照对比器测试。"""
import pandas as pd
import pytest

import compare


def _mk_df(closes, opens):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(n)],
        "open": list(opens),
        "close": list(closes),
    })


def test_path_label_quadrants():
    assert compare._path_label(1, 1) == "高开高走"
    assert compare._path_label(1, 0) == "高开低走"
    assert compare._path_label(0, 1) == "低开高走"
    assert compare._path_label(0, 0) == "低开低走"


def test_actual_outcomes_values():
    d = _mk_df([100.0, 110.0, 121.0, 108.9], [100.0, 105.0, 115.0, 105.0])
    oc = compare._actual_outcomes(d, 0)
    assert oc["close1"] == pytest.approx(0.10)
    assert oc["gap"] == pytest.approx(0.05)
    assert oc["od"] == pytest.approx(110.0 / 105.0 - 1.0)
    assert oc["trend3"] == pytest.approx(108.9 / 100.0 - 1.0)


def test_actual_outcomes_edge_cases():
    d = _mk_df([100.0, 110.0, 121.0], [100.0, 105.0, 115.0])
    # bar+1 越界(末根)
    assert compare._actual_outcomes(d, 2) == {"close1": None, "gap": None, "od": None, "trend3": None}
    # bar+3 越界(bar=1: close1/gap/od 有值,trend3 None)
    oc = compare._actual_outcomes(d, 1)
    assert oc["close1"] == pytest.approx(121.0 / 110.0 - 1.0)
    assert oc["trend3"] is None
    # 非正价
    d2 = _mk_df([-5.0, 110.0], [100.0, 105.0])
    assert compare._actual_outcomes(d2, 0) == {"close1": None, "gap": None, "od": None, "trend3": None}
