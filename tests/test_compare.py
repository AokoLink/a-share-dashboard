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


def test_class_metric():
    rows = [("up", 1), ("down", 0), ("up", 0), ("hold", 1)]
    m = compare._class_metric(rows)
    assert m["n"] == 4
    assert m["n_hold"] == 1
    assert m["n_bet"] == 3
    assert m["hit_rate"] == pytest.approx(2.0 / 3.0)
    assert m["base_rate"] == pytest.approx(0.5)


def test_class_metric_all_hold():
    m = compare._class_metric([("hold", 1), ("hold", 0)])
    assert m["n_bet"] == 0
    assert m["hit_rate"] is None
    assert m["n_hold"] == 2
    assert m["base_rate"] == pytest.approx(0.5)


def test_path_metric():
    rows = [("高开高走", "高开高走"), ("高开高走", "低开低走"), ("低开低走", "低开低走")]
    m = compare._path_metric(rows)
    assert m["n"] == 3
    assert m["acc_path"] == pytest.approx(2.0 / 3.0)


def test_return_metric():
    rows = [(0.10, 0.10), (0.0, 0.05), (-0.05, -0.05)]
    m = compare._return_metric(rows)
    assert m["n"] == 3
    assert m["mae"] == pytest.approx((0.0 + 0.05 + 0.0) / 3.0)
    assert m["rmse"] == pytest.approx(((0.0 ** 2 + 0.05 ** 2 + 0.0 ** 2) / 3.0) ** 0.5)
    assert m["mean_residual"] == pytest.approx((0.0 + 0.05 + 0.0) / 3.0)
    assert m["sign_n"] == 3
    assert m["sign_agreement"] == pytest.approx(2.0 / 3.0)


def test_risk_metric():
    rows = [(0.1, 0), (0.2, 0), (0.3, 1), (0.4, 1)]
    m = compare._risk_metric(rows)
    assert m["n"] == 4
    assert m["adverse_rate"] == pytest.approx(0.5)
    assert m["brier"] == pytest.approx((0.01 + 0.04 + 0.49 + 0.36) / 4.0)
    assert m["ece"] == pytest.approx(abs(0.25 - 0.5))
    assert m["lift"] is None  # n=4 < N_BINS=10 降单箱


import backtest as bt

D1_CLOSES = [90.0, 92.0, 95.0, 100.0, 110.0, 121.0, 108.9, 108.9]
D1_OPENS = [90.0, 90.0, 93.0, 97.0, 105.0, 115.0, 105.0, 105.0]
# as_of bar=3(close0=100),open1=105,close1=110,close3=108.9


def _fixture(monkeypatch):
    universe = {
        "000001": _mk_df(D1_CLOSES, D1_OPENS),
        "000002": _mk_df([50.0, 55.0, 60.0, 65.0], [50.0, 55.0, 60.0, 65.0]),
    }
    codes = ["000001", "000002"]
    all_days = ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    pos_of = {
        "000001": {"2026-08-11": 3},
        "000002": {"2026-08-11": 3},  # d2 末根,bar+1 越界
    }
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: (universe, codes))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))
    return universe


def _snapshot():
    return {
        "mode": "predict", "system_version": "62c60a7", "module_version": "1.0.0",
        "generated_at": "2026-08-14T20:52:42", "as_of_date": "2026-08-11",
        "predictions": [
            {"code": "000001", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "up", "confidence": 0.6, "gap": "high", "od": "up", "path": "高开高走"},
             "T+3": {"direction": "up", "confidence": 0.6}},
            {"code": "000002", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "down", "confidence": 0.6, "gap": "low", "od": "down", "path": "低开低走"},
             "T+3": {"direction": "down", "confidence": 0.6}},
            {"code": "000003", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "hold", "confidence": 0.5, "gap": "hold", "od": "hold", "path": None},
             "T+3": {"direction": "hold", "confidence": 0.5}},
        ],
    }


def test_verify_core(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    snap = tmp_path / "s.json"
    import json as _json
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r = compare.verify(str(snap), "dd", "sm")
    v = r["verification"]
    assert v["n_predictions"] == 3
    assert v["n_verified"] == 1
    assert v["n_trend3_verified"] == 1
    assert v["n_unverified"] == 2
    assert v["unverified_reasons"] == {"not_in_universe": 1, "no_next_bar": 1, "non_positive_close": 0}
    m = r["metrics"]
    for name in ("direction", "gap", "od", "trend3"):
        assert m[name]["n"] == 1 and m[name]["hit_rate"] == pytest.approx(1.0)
    assert m["path"]["n"] == 1 and m["path"]["acc_path"] == pytest.approx(1.0)
    assert m["return"]["available"] is False
    assert m["risk"]["available"] is False


def test_verify_field_missing(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r = compare.verify(str(snap), "dd", "sm")
    assert r["metrics"]["return"]["available"] is False
    assert "reason" in r["metrics"]["return"]
    assert r["metrics"]["risk"]["available"] is False


def test_verify_anti_leak_snapshot(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    snap2 = _snapshot()
    snap2["predictions"][0]["T+1"]["direction"] = "down"
    snap.write_text(_json.dumps(snap2, ensure_ascii=False), encoding="utf-8")
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r1["metrics"]["direction"]["hit_rate"] != r2["metrics"]["direction"]["hit_rate"]


def test_verify_anti_leak_past(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"].loc[2, "close"] = 9999.0  # close[bar-1], <= as_of
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"] == r1["metrics"]


def test_verify_anti_leak_anchor(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"].loc[3, "close"] = 120.0  # close[bar] 锚点分母,翻转 close1 符号
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"]["direction"]["hit_rate"] != r1["metrics"]["direction"]["hit_rate"]


def test_verify_anti_leak_future(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"].loc[4, "close"] = 5.0  # close1, > as_of
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"]["direction"]["hit_rate"] != r1["metrics"]["direction"]["hit_rate"]


def test_main_end_to_end(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "v.json"
    rc = compare.main(["--snapshot", str(snap), "--data-dir", "dd",
                       "--sector-map", "sm", "--out", str(out)])
    assert rc == 0
    payload = _json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "compare"
    assert payload["snapshot"]["system_version"] == "62c60a7"
    assert set(payload["metrics"]) == {"direction", "gap", "od", "trend3", "path", "return", "risk"}
    assert payload["metrics"]["return"]["available"] is False
    assert out.with_suffix(".md").exists()


def test_main_missing_snapshot(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    rc = compare.main(["--snapshot", str(tmp_path / "nope.json"), "--data-dir", "dd",
                       "--sector-map", "sm", "--out", str(tmp_path / "v.json")])
    assert rc == 1


def test_verify_return_field_none_excluded(tmp_path, monkeypatch):
    import json as _json
    universe = {
        "000001": _mk_df(D1_CLOSES, D1_OPENS),
        "000004": _mk_df(D1_CLOSES, D1_OPENS),
    }
    codes = ["000001", "000004"]
    all_days = ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    pos_of = {"000001": {"2026-08-11": 3}, "000004": {"2026-08-11": 3}}
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: (universe, codes))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))
    snap = {
        "mode": "predict", "system_version": "62c60a7", "module_version": "1.1.0",
        "generated_at": "2026-08-14T20:52:42", "as_of_date": "2026-08-11",
        "predictions": [
            {"code": "000001", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "up", "confidence": 0.6, "gap": "high", "od": "up", "path": "高开高走"},
             "T+3": {"direction": "up", "confidence": 0.6},
             "expected_return": 0.05, "risk_p": 0.2},
            {"code": "000004", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "up", "confidence": 0.6, "gap": "high", "od": "up", "path": "高开高走"},
             "T+3": {"direction": "up", "confidence": 0.6},
             "expected_return": None, "risk_p": None},
        ],
    }
    snap_path = tmp_path / "s.json"
    snap_path.write_text(_json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    r = compare.verify(str(snap_path), "dd", "sm")
    assert r["metrics"]["return"]["available"] is True
    assert r["metrics"]["return"]["n"] == 1
    assert r["metrics"]["risk"]["available"] is True
    assert r["metrics"]["risk"]["n"] == 1
