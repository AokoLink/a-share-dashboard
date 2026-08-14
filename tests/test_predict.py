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


def test_metric_hit_rate_and_ece():
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)  # p=[0,1]
    samples = [(1.0, 0)] * 5 + [(9.0, 1)] * 5
    m = pr._metric("direction", cal, samples)
    assert m["n"] == 10
    assert m["base_rate"] == pytest.approx(0.5)
    assert m["hit_rate"] == pytest.approx(1.0)
    assert m["ece"] == pytest.approx(0.0)
    assert m["brier"] == pytest.approx(0.0)
    assert m["n_hold"] == 0


def test_run_backtest_integration(monkeypatch):
    d = make_daily([10.0 + 0.1 * i for i in range(70)])
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_buyable", lambda univ, pos, ad, i: ({"000001"}, {}, {}, {}))
    monkeypatch.setattr(bt, "score_at", lambda dd, i, now: {"composite": 50.0})
    results = pr.run_backtest("/dummy", "/dummy")
    assert set(results["calibrators"]) == {"direction", "gap", "od", "trend3", "return", "risk"}
    assert set(results["metrics"]) == {"direction", "gap", "od", "trend3", "return", "risk", "path"}
    assert results["n_train"] + results["n_valid"] == results["n_eval"]
    assert results["n_train"] == int(0.8 * results["n_eval"])
    assert results["data_range"]["start"] == d["date"].iloc[0]
    assert isinstance(results["valid_records"], list)
    if results["valid_records"]:
        rec = results["valid_records"][0]
        for k in ("code", "date", "bar", "composite", "close1", "expected_return"):
            assert k in rec


def test_collect_samples_risk_label_strict_threshold(monkeypatch):
    # 风险标签 = 1[close1 < -0.03](单一阈值,无 base-rate 兜底);边界 -0.03 不判不利
    recs = [
        {"composite": 50.0, "risk": 30.0, "close1": -0.031,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
        {"composite": 50.0, "risk": 30.0, "close1": -0.03,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
        {"composite": 50.0, "risk": 30.0, "close1": -0.029,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
    ]
    monkeypatch.setattr(pr, "_iter_scored", lambda u, p, a, d: iter(recs))
    samples = pr._collect_samples(None, None, None, [0])
    assert samples["risk"] == [(30.0, 1), (30.0, 0), (30.0, 0)]


def test_predict_now_integration(monkeypatch):
    d = make_daily([10.0 + 0.1 * i for i in range(70)], volumes=[1e7] * 70)
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_buyable", lambda univ, pos, ad, i: ({"000001"}, {}, {}, {}))
    monkeypatch.setattr(bt, "score_at", lambda dd, i, now: {"composite": 50.0})
    res = pr.predict_now("/dummy", "/dummy")
    assert res["as_of_date"] == d["date"].iloc[-1]
    assert len(res["predictions"]) == 1
    pred = res["predictions"][0]
    assert pred["code"] == "000001"
    assert pred["date"] == d["date"].iloc[-1]
    assert pred["T+3"]["direction"] is not None


def _fake_results():
    return {
        "calibrators": {"direction": pr._fit_calibrator([(5.0, 1)] * 10, 2)},
        "n_samples": {"direction": 10},
        "metrics": {
            "direction": {"n": 10, "base_rate": 0.5, "hit_rate": 0.6, "ece": 0.1, "brier": 0.2, "n_hold": 1},
            "path": {"n": 8, "acc_path": 0.5},
        },
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "train_window": {"start": "2026-01-01", "end": "2026-06-01"},
        "valid_window": {"start": "2026-06-02", "end": "2026-08-13"},
        "n_eval": 300, "step": 3, "n_train": 240, "n_valid": 60,
    }


def test_main_backtest_writes_output(tmp_path, monkeypatch):
    out = tmp_path / "pb.json"
    monkeypatch.setattr(pr, "run_backtest", lambda dd, sm: _fake_results())
    rc = pr.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "backtest"
    assert payload["system_version"]
    assert "calibrators" in payload and "metrics" in payload
    assert out.with_suffix(".md").exists()


def test_main_predict_writes_snapshot(tmp_path, monkeypatch):
    out = tmp_path / "snap.json"
    monkeypatch.setattr(pr, "predict_now",
                        lambda dd, sm: {"as_of_date": "2026-08-13", "predictions": [{"code": "000001"}]})
    rc = pr.main(["--predict", "--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "predict"
    assert payload["as_of_date"] == "2026-08-13"
    assert payload["predictions"] == [{"code": "000001"}]
    assert not out.with_suffix(".md").exists()


def test_main_missing_data_dir_exits_nonzero():
    rc = pr.main(["--data-dir", "/nonexistent/xyz", "--sector-map", "/nope"])
    assert rc == 1


def test_main_end_to_end_gbk(tmp_path, monkeypatch):
    # 真实全链路:tmp pkl 集 + GBK 板块映射,monkeypatch 仅固定 score_stock 的 composite
    dailydir = tmp_path / "daily"
    dailydir.mkdir()
    closes = [2000.0 + 0.1 * k for k in range(1200)]
    make_daily(closes, start="2020-01-01", code="000001").to_pickle(dailydir / "000001.pkl")
    sm = tmp_path / "code2sector.json"
    with open(sm, "w", encoding="gbk") as f:
        json.dump({"000001": ["半导体"]}, f, ensure_ascii=False)
    monkeypatch.setattr(an, "score_stock",
                        lambda df, quote, now: {"position": 50.0, "trend": 50.0, "volume_price": 50.0,
                                                "signal": 50.0, "risk": 10.0, "composite": 70.0})
    out = tmp_path / "pb.json"
    rc = pr.main(["--data-dir", str(dailydir), "--sector-map", str(sm), "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "backtest"
    assert payload["system_version"]
    assert set(payload["calibrators"]) == {"direction", "gap", "od", "trend3", "return", "risk"}
    assert "ece" in payload["metrics"]["direction"]
    assert out.with_suffix(".md").exists()


def test_fit_calibrator_monotone_false_keeps_nonmonotone():
    # 非单调输入:PAV 会把它压平为单调,monotone=False 应保留原始非单调箱序
    pairs = [(1.0, 0)] * 4 + [(2.0, 1)] * 4 + [(3.0, 0)] * 4
    cal = pr._fit_calibrator(pairs, 3, monotone=False)
    ps = [p for _, p in cal.bins]
    assert ps != sorted(ps)  # 非单调(1.0→p≈0, 2.0→p≈1, 3.0→p≈0)


def test_fit_calibrator_default_still_pav():
    pairs = [(1.0, 0)] * 4 + [(2.0, 1)] * 4 + [(3.0, 0)] * 4
    cal = pr._fit_calibrator(pairs, 3)
    ps = [p for _, p in cal.bins]
    assert ps == sorted(ps)  # 默认 PAV 单调不减


def test_metric_return_mae_rmse_sign_residual():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)  # 单箱 mean=0.02
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    samples = [(5.0, 0.0), (5.0, 0.04), (5.0, -0.02)]
    m = pr._metric_return(ret, dircal, samples)
    # er=0.02 恒定:abs err = |0.02-close1| = [0.02, 0.02, 0.04];
    # residual = close1-er = [-0.02, 0.02, -0.04]
    assert m["n"] == 3
    assert m["mae"] == pytest.approx((0.02 + 0.02 + 0.04) / 3)
    assert m["rmse"] == pytest.approx(((0.02 ** 2 + 0.02 ** 2 + 0.04 ** 2) / 3) ** 0.5)
    assert m["mean_residual"] == pytest.approx((-0.02 + 0.02 - 0.04) / 3)
    # sign_agreement:close1==0 样本剔除(不计分子分母);非零样本 0.04、-0.02 中
    # er>0 且 close1>0 的只有 0.04 → 1/2
    assert m["sign_n"] == 2
    assert m["sign_agreement"] == pytest.approx(1 / 2)
    # 全部 up,但 n=3 < 30 → dir_cond_mae["up"] 只报 n 不报值
    assert m["dir_cond_mae"]["up"]["n"] == 3
    assert m["dir_cond_mae"]["up"]["mae"] is None
    assert m["dir_cond_mae"]["down"]["n"] == 0
    assert m["dir_cond_mae"]["down"]["mae"] is None


def test_metric_return_zero_close1_excluded_from_sign():
    ret = pr._fit_calibrator([(5.0, 0.0)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)
    m = pr._metric_return(ret, dircal, [(5.0, 0.0)])
    assert m["sign_n"] == 0
    assert m["sign_agreement"] is None


def test_metric_return_empty():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)
    m = pr._metric_return(ret, dircal, [])
    assert m["n"] == 0
    assert m["mae"] is None


def test_metric_return_dir_cond_large_n_reports_mae():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    m = pr._metric_return(ret, dircal, [(5.0, 0.03)] * 30)
    assert m["dir_cond_mae"]["up"]["n"] == 30
    assert m["dir_cond_mae"]["up"]["mae"] == pytest.approx(0.01)


def test_metric_risk_ece_brier_lift():
    # 两箱:risk 低→P(adverse)=0,risk 高→P(adverse)=1
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)
    samples = [(1.0, 0)] * 5 + [(9.0, 1)] * 5
    m = pr._metric_risk(cal, samples)
    assert m["n"] == 10
    assert m["adverse_rate"] == pytest.approx(0.5)
    assert m["ece"] == pytest.approx(0.0)
    assert m["brier"] == pytest.approx(0.0)
    assert m["lift"] == pytest.approx(1.0)  # top bin realized=1 - bottom=0


def test_metric_risk_empty():
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)
    m = pr._metric_risk(cal, [])
    assert m["n"] == 0
    assert m["ece"] is None
    assert m["lift"] is None


def test_metric_path_four_class():
    gap_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)   # p=1 → high
    od_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)    # p=1 → up
    # 预测恒「高开高走」;4 样本各命中其一 → 1/4
    samples = [(5.0, 1, 1), (5.0, 1, 0), (5.0, 0, 1), (5.0, 0, 0)]
    m = pr._metric_path(gap_cal, od_cal, samples)
    assert m["n"] == 4
    assert m["acc_path"] == pytest.approx(0.25)


def test_metric_path_hold_yields_none():
    gap_cal = pr._fit_calibrator([(5.0, 0)] * 5 + [(5.0, 1)] * 5, 1)  # p=0.5 → hold
    od_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    m = pr._metric_path(gap_cal, od_cal, [(5.0, 1, 1)])
    assert m["n"] == 0
    assert m["acc_path"] is None
