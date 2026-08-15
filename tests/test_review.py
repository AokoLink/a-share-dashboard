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


def _cell(dim, **over):
    if dim == "path":
        c = {"n": 100, "acc_path": 0.60}
    elif dim in ("direction", "gap", "trend3"):
        c = {"n": 100, "hit_rate": 0.60, "base_rate": 0.50, "n_hold": 0}
    elif dim == "return":
        c = {"n": 100, "mae": 0.10, "rmse": 0.20, "sign_agreement": 0.60,
             "sign_n": 100, "mean_residual": 0.01, "dir_cond_mae": {}}
    else:
        c = {"n": 100, "adverse_rate": 0.05, "ece": 0.10, "brier": 0.20, "lift": 1.0}
    c.update(over)
    return c


def _fake_payload(**over):
    p = {
        "system_version": "test", "module_version": "1.0.0",
        "generated_at": "x", "mode": "evaluate",
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "valid_window": {"start": "2026-01-01", "end": "2026-08-13"},
        "n_valid_records": 100,
        "overall": {d: _cell(d) for d in rv.ev.DIMS},
        "layers": {L: {d: _cell(d) for d in rv.ev.DIMS} for L in rv.ev.LAYERS},
        "environments": {s: {d: _cell(d) for d in rv.ev.DIMS} for s in rv.env.LABELS},
        "env_n": {s: 100 for s in rv.env.LABELS},
        "layer_n": {L: 100 for L in rv.ev.LAYERS},
        "significant": [], "se_bounds": [],
        "all_undifferentiated": False,
    }
    p.update(over)
    return p


def test_overall_summary_edge():
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=0.55, base_rate=0.50)
    p["overall"]["path"] = _cell("path", acc_path=0.30)
    rows = {r["dim"]: r for r in rv._overall_summary(p)}
    assert abs(rows["direction"]["edge"] - 0.05) < 1e-12      # 0.55 - 0.50
    assert abs(rows["path"]["edge"] - 0.05) < 1e-12           # 0.30 - 0.25
    assert rows["return"]["baseline"] == 0.5
    assert rows["risk"]["baseline"] is None and rows["risk"]["edge"] is None


def test_no_edge_direction():
    # n=400,n_hold=0 -> se=sqrt(0.5*0.5/400)=0.025;|0.51-0.50|=0.01 <= 0.025 -> 无 edge
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=0.51, base_rate=0.50, n=400)
    assert "direction" in rv._no_edge_dims(p)
    # hit_rate=0.60 -> |0.10| > 0.025 -> 有 edge,不入
    p["overall"]["direction"] = _cell("direction", hit_rate=0.60, base_rate=0.50, n=400)
    assert "direction" not in rv._no_edge_dims(p)
