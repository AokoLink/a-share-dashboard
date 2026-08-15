# -*- coding: utf-8 -*-
import pytest

import review as rv


def test_binom_diff_se():
    # p=0.5,n=100 两侧相等 → se = sqrt(2 * 0.5*0.5/100) = sqrt(0.005) ≈ 0.0707106...
    se = rv._binom_diff_se(0.5, 100, 0.5, 100)
    assert abs(se - (0.005 ** 0.5)) < 1e-12
    # 任一侧 None 或 n<=0 → None
    assert rv._binom_diff_se(None, 100, 0.5, 100) is None
    assert rv._binom_diff_se(0.5, 0, 0.5, 100) is None


def test_effective_n():
    # hit_rate 维(direction/gap/trend3):n - n_hold
    assert rv._effective_n("direction", {"n": 100, "n_hold": 30}) == 70
    # sign_agreement 维(return):sign_n
    assert rv._effective_n("return", {"n": 100, "sign_n": 80}) == 80
    # path / risk:n
    assert rv._effective_n("path", {"n": 100}) == 100
    assert rv._effective_n("risk", {"n": 100}) == 100


def test_baseline():
    assert rv._baseline("direction", {"base_rate": 0.5}) == 0.5
    assert rv._baseline("path", {}) == 0.25
    assert rv._baseline("return", {}) == 0.5
    assert rv._baseline("risk", {}) is None


def test_knob_registry_unique_and_typed():
    names = [k["name"] for k in rv.KNOBS]
    assert len(names) == len(set(names))          # name 唯一
    allowed = {"backtest-calibrated", "provisional", "manual-ruling"}
    for k in rv.KNOBS:
        assert k["evidence_status"] in allowed
        loc = k["locator"]
        assert loc["module"]                       # locator 有 module
        assert ("attr" in loc) or ("func" in loc)  # attr(命名常量)或 func(内联)
        assert isinstance(k["validation_recipe"], str) and k["validation_recipe"]
