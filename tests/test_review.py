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


def test_baseline_majority_class():
    # 正类率 < 0.5 时,无信息基线 = 多数类率 1-base_rate,而非 base_rate
    assert abs(rv._baseline("gap", {"base_rate": 0.37}) - 0.63) < 1e-12
    # 正类率 > 0.5 时,多数类即正类,基线 = base_rate
    assert abs(rv._baseline("direction", {"base_rate": 0.70}) - 0.70) < 1e-12
    # 无 base_rate → None
    assert rv._baseline("gap", {}) is None


def test_gap_majority_class_is_not_edge():
    # 复现真实 bug:gap base_rate=0.3694,hit_rate=0.6306=1-base_rate(恒押多数类)
    # → edge 应为 0 且 gap 入 no_edge_dims(而非报 +0.26 假 edge)
    p = _fake_payload()
    p["overall"]["gap"] = _cell("gap", hit_rate=0.6306, base_rate=0.3694, n=85235, n_hold=0)
    rows = {r["dim"]: r for r in rv._overall_summary(p)}
    assert rows["gap"]["edge"] is not None
    assert abs(rows["gap"]["edge"]) < 1e-9
    assert "gap" in rv._no_edge_dims(p)


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


def test_weak_layer_from_significant():
    p = _fake_payload()
    p["significant"] = [["趋势", "direction", "hit_rate", 0.42, 0.55, 0.02]]
    p["se_bounds"] = [["趋势", "direction", 0.04]]
    weak, strong = rv._layer_weak_strong(p)
    assert any(w["layer"] == "趋势" and w["dim"] == "direction" and w["value"] == 0.42
               for w in weak)
    assert strong == []
    # 反转:v_l > v_o → strong
    p["significant"] = [["趋势", "direction", "hit_rate", 0.68, 0.55, 0.02]]
    weak, strong = rv._layer_weak_strong(p)
    assert weak == [] and any(s["layer"] == "趋势" for s in strong)


def test_layer_weak_filters_nonprimary_metric():
    # significant 里 return 的 mae 显著但 PRIMARY=sign_agreement → 不产生 weak/strong
    p = _fake_payload()
    p["significant"] = [["趋势", "return", "mae", 0.05, 0.02, 0.01]]
    weak, strong = rv._layer_weak_strong(p)
    assert weak == [] and strong == []


def test_env_significance_self_computed():
    # env direction hit_rate=0.40/n=100 vs overall 0.55/n=1000:
    # se = sqrt(0.4*0.6/100 + 0.55*0.45/1000) ≈ 0.0514;2σ≈0.1029;|0.40-0.55|=0.15>0.1029 → 弱环境
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=0.55, n=1000, base_rate=0.50)
    p["environments"]["熊"]["direction"] = _cell("direction", hit_rate=0.40, n=100, base_rate=0.50)
    weak, strong = rv._env_weak_strong(p)
    assert any(w["env"] == "熊" and w["dim"] == "direction" and w["value"] == 0.40
               for w in weak)
    # 有效 n < 30 → 跳过
    p["environments"]["熊"]["direction"] = _cell("direction", hit_rate=0.40, n=10, base_rate=0.50)
    weak, strong = rv._env_weak_strong(p)
    assert not any(w["env"] == "熊" for w in weak)


def test_env_suppressed_cell_skipped():
    p = _fake_payload()
    p["environments"]["恐慌"]["direction"] = {"n": 10, "_suppressed": True}
    weak, strong = rv._env_weak_strong(p)
    assert not any(w["env"] == "恐慌" for w in weak)


def test_degenerate_thin():
    p = _fake_payload()
    p["env_n"]["退潮"] = 10          # < MIN_STATE_N=20
    p["layer_n"]["反抽"] = 25        # < MIN_LAYER_N=30
    deg, thin = rv._degenerate_thin(p)
    assert "退潮" in deg and "反抽" in thin


def test_degenerate_dims_direction_hold_and_path_zero():
    # direction 全 hold(hit_rate=None, n_hold=n) + path n=0 → 二者应显式报退化
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=None, base_rate=0.50, n_hold=100)
    p["overall"]["path"] = _cell("path", acc_path=None, n=0)
    dd = rv._degenerate_dims(p)
    by_dim = {d["dim"]: d for d in dd}
    assert set(by_dim) == {"direction", "path"}
    assert "n_hold" in by_dim["direction"]["reason"] and "100" in by_dim["direction"]["reason"]
    assert by_dim["path"]["reason"].startswith("主指标未定义:n=0")
    # 正常维度(gap 有 hit_rate)不入退化
    assert "gap" not in by_dim


def test_degenerate_dims_empty_when_clean():
    # 默认 fixture:六维 PRIMARY 均非 None → 无退化
    assert rv._degenerate_dims(_fake_payload()) == []


def test_review_shape():
    r = rv.review(_fake_payload())
    for key in ("overall_summary", "weak_layers", "strong_layers",
                "weak_environments", "strong_environments", "no_edge_dims",
                "undifferentiated", "degenerate_environments", "thin_layers",
                "degenerate_dims"):
        assert key in r
    assert len(r["overall_summary"]) == len(rv.ev.DIMS)
    assert r["undifferentiated"] is False          # 默认 all_undifferentiated=False
    # all_undifferentiated=True → undifferentiated=True 原样透传
    assert rv.review(_fake_payload(all_undifferentiated=True))["undifferentiated"] is True


def test_suggest_r1_antisignal():
    p = _fake_payload()
    p["layers"]["趋势"]["direction"] = _cell("direction", hit_rate=0.30, base_rate=0.55)
    p["significant"] = [["趋势", "direction", "hit_rate", 0.30, 0.55, 0.05]]
    p["se_bounds"] = [["趋势", "direction", 0.10]]
    s = rv.suggest(p, rv.review(p))
    kinds = [x["kind"] for x in s]
    assert "layer_antisignal" in kinds
    r1 = next(x for x in s if x["kind"] == "layer_antisignal")
    assert "0.30" in r1["evidence"] and "0.55" in r1["evidence"]


def test_suggest_r3_no_edge():
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=0.51, base_rate=0.50, n=400)
    s = rv.suggest(p, rv.review(p))
    r3 = next(x for x in s if x["kind"] == "direction_no_edge")
    assert r3["knob"] == "DIRECTION_BAND"
    assert r3["confidence"] == "high"


def test_suggest_r4_ece():
    p = _fake_payload()
    p["overall"]["risk"] = _cell("risk", ece=0.15)
    s = rv.suggest(p, rv.review(p))
    assert any(x["kind"] == "risk_miscalibration" and x["knob"] == "N_BINS" for x in s)


def test_suggest_r5_residual_bias():
    p = _fake_payload()
    p["overall"]["return"] = _cell("return", mean_residual=0.05)
    s = rv.suggest(p, rv.review(p))
    assert any(x["kind"] == "return_bias" for x in s)


def test_suggest_empty_when_clean():
    # 默认 fixture:方向有 edge、无显著弱层/弱环境、ece=0.10(不>0.10)、mean_residual=0.01(不>0.02)
    assert rv.suggest(_fake_payload(), rv.review(_fake_payload())) == []


def test_suggest_r2_env_antisignal():
    p = _fake_payload()
    p["overall"]["direction"] = _cell("direction", hit_rate=0.55, n=1000, base_rate=0.50)
    p["environments"]["熊"]["direction"] = _cell("direction", hit_rate=0.40, n=100, base_rate=0.50)
    s = rv.suggest(p, rv.review(p))
    r2 = [x for x in s if x["kind"] == "environment_antisignal"]
    assert len(r2) == 1
    assert r2[0]["knob"] == "DIRECTION_BAND"
    assert "0.40" in r2[0]["evidence"] and "0.50" in r2[0]["evidence"]


def test_main_end_to_end(tmp_path):
    p = _fake_payload()
    in_path = tmp_path / "ev.json"
    import json as _json
    in_path.write_text(_json.dumps(p, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "review.json"
    rc = rv.main(["--in", str(in_path), "--out", str(out)])
    assert rc == 0
    rep = rv.json_load(out)
    assert rep["mode"] == "review"
    assert rep["module_version"] == "1.0.0"
    assert "review" in rep and "suggestions" in rep and "knobs" in rep
    assert rep["source"]["path"] == str(in_path)
    md = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "## 复盘" in md and "## 优化建议" in md and "## 可调旋钮" in md


def test_render_empty_suggestions_honest():
    p = _fake_payload()
    r = rv.review(p)
    s = rv.suggest(p, r)
    rep = rv.build_report(p, r, s, "ev.json")
    md = rv.render_markdown(rep)
    assert "零建议" in md or "无建议" in md


def test_review_no_utf8_minus():
    src = open("review.py", encoding="utf-8").read()
    assert "−" not in src


def test_layer_risk_lower_is_better():
    # adverse_rate 越低越好:0.03 < 0.05 → 应入 strong 而非 weak
    p = _fake_payload()
    p["significant"] = [["趋势", "risk", "adverse_rate", 0.03, 0.05, 0.02]]
    p["se_bounds"] = [["趋势", "risk", 0.04]]
    weak, strong = rv._layer_weak_strong(p)
    assert not any(w["layer"] == "趋势" for w in weak)
    assert any(s["layer"] == "趋势" and s["dim"] == "risk" and s["value"] == 0.03
               for s in strong)


def test_env_risk_higher_is_weaker():
    # 熊 risk adverse_rate 0.20 高于总体 0.05 → 应入 weak(跑输);总体/环境 n=10000 保证显著
    p = _fake_payload()
    p["overall"]["risk"] = _cell("risk", adverse_rate=0.05, n=10000)
    p["environments"]["熊"]["risk"] = _cell("risk", adverse_rate=0.20, n=10000)
    weak, strong = rv._env_weak_strong(p)
    assert any(w["env"] == "熊" and w["dim"] == "risk" and w["value"] == 0.20
               for w in weak)
    assert not any(s["env"] == "熊" and s["dim"] == "risk" for s in strong)


def test_suggest_r1_skips_return():
    # return 维 sign_agreement 0.40 < 0.50 的弱层,不应触发 R1(return 不在 R1 授权范围)
    p = _fake_payload()
    p["significant"] = [["趋势", "return", "sign_agreement", 0.40, 0.60, 0.05]]
    p["se_bounds"] = [["趋势", "return", 0.10]]
    s = rv.suggest(p, rv.review(p))
    assert not any(x["kind"] == "layer_antisignal" for x in s)
