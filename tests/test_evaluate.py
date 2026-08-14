# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import pytest

import evaluate as ev


def mk(closes, opens=None, highs=None, lows=None, amounts=None, code="000001"):
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    highs = [float(h) for h in highs] if highs is not None else [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [float(l) for l in lows] if lows is not None else [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    amounts = [float(a) for a in amounts] if amounts is not None else [1e9] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    closes = [float(c) for c in closes]
    # change_pct 由 close 计算(与 bt.build_universe 同口径);bt.build_buyable 读该列
    change_pct = (pd.Series(closes).pct_change().fillna(0.0) * 100.0).tolist()
    return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": [1e6] * n,
                         "amount": amounts, "code": [code] * n, "change_pct": change_pct})


def test_amount_top_20pct_and_nan():
    # 5 只股,amount 10/8/6/4/NaN → top20% = max(1, int(4*0.2))=1 → 只有 amount=10 那只
    d = mk([10.0] * 100)
    universe = {"a": mk([10.0] * 100, amounts=[10.0] * 100, code="a"),
                "b": mk([10.0] * 100, amounts=[8.0] * 100, code="b"),
                "c": mk([10.0] * 100, amounts=[6.0] * 100, code="c"),
                "d": mk([10.0] * 100, amounts=[4.0] * 100, code="d"),
                "e": mk([10.0] * 100, amounts=[float("nan")] * 100, code="e")}
    all_days = sorted(set().union(*[set(v["date"]) for v in universe.values()]))
    pos_of = {c: {dt: i for i, dt in enumerate(v["date"])} for c, v in universe.items()}
    top = ev._amount_top(universe, pos_of, all_days, 99, {"a", "b", "c", "d", "e"})
    assert top == {"a"}


def test_hot_members_top3():
    sm = {"s1": ["a", "b"], "s2": ["c"], "s3": ["d"], "s4": ["e"]}
    heat = {"s1": 0.05, "s2": 0.03, "s3": 0.01, "s4": -0.02}
    assert ev._hot_members(sm, heat) == {"a", "b", "c", "d"}


def test_leaders_tie_smallest_code():
    # sector_map 为 code→sectors 映射(与 bt.load_sector_map 输出同向)
    sector_map = {"a": ["s1"], "b": ["s1"], "c": ["s2"]}
    comp = {"a": 70.0, "b": 70.0, "c": 50.0}
    # s1 内 a/b 并列 70 → 取 code 小者 a;c 是 s2 唯一成员 → 龙头
    assert ev._leaders(sector_map, {"a", "b", "c"}, comp) == {"a", "c"}


def test_layer_trend():
    d = mk(list(range(100, 200)))  # 单调涨 → MA5>MA20>MA60
    assert ev._layer_trend(d, 99)


def test_layer_high():
    closes = [10.0] * 100
    closes[99] = 15.0
    d = mk(closes)
    assert ev._layer_high(d, 99)
    closes[99] = 9.0
    assert not ev._layer_high(mk(closes), 99)


def test_layer_oversold_rebound_oscillation():
    closes = [100.0] * 100
    closes[80] = 60.0   # ret20 at bar=99 → close[99]/close[79]-1
    closes[99] = 62.0
    d = mk(closes)
    # ret20(99) = 62/100 - 1 = -0.38 < -0.15 → 超跌
    assert ev._layer_oversold(d, 99)
    # 反抽:ret20(98)=close[98]/close[78]-1=100/100-1=0 不< -0.15 → False
    assert not ev._layer_rebound(d, 99)
    # 震荡:构造窄幅
    narrow = mk([100.0 + 0.01 * i for i in range(100)],
                highs=[100.0 + 0.02 * i for i in range(100)],
                lows=[100.0 + 0.005 * i for i in range(100)])
    assert ev._layer_oscillation(narrow, 99)


def test_layer_high_boundary_097():
    # max(close[40..99])=100;close[99]=97.0 恰 =0.97*100 → True;96.9 → False
    closes = [100.0] * 100
    closes[99] = 97.0
    assert ev._layer_high(mk(closes), 99)
    closes[99] = 96.9
    assert not ev._layer_high(mk(closes), 99)


def test_layer_oversold_boundary():
    # ret20(99)=close[99]/close[79]-1;close[79]=100。恰 -0.15 不满足(< 严格);略低于 -0.15 → True
    # 注:85.0/100.0-1 在浮点下= -0.15000000000000002(因 0.85 不可二进制精确表示),会误判为 < -0.15,
    #     故边界侧用 85.000001(≈-0.14999999,严格不 < -0.15)以避开浮点噪声;84.9 → -0.151 → True
    closes = [100.0] * 100
    closes[99] = 85.000001
    assert not ev._layer_oversold(mk(closes), 99)
    closes[99] = 84.9
    assert ev._layer_oversold(mk(closes), 99)


def test_layer_oscillation_boundary_15():
    # amp=(max(high)-min(low))/close*100;窗口 80..99。恰 15 → False;14.9 → True
    closes = [100.0] * 100
    assert not ev._layer_oscillation(mk(closes, highs=[115.0] * 100, lows=[100.0] * 100), 99)
    assert ev._layer_oscillation(mk(closes, highs=[114.9] * 100, lows=[100.0] * 100), 99)


def test_layer_rebound_true_tm1():
    # ret20(T-1)=close[98]/close[78]-1=60/100-1=-0.4<-0.15;close[99]=62>close[98]=60 → True
    closes = [100.0] * 100
    closes[98] = 60.0
    closes[99] = 62.0
    assert ev._layer_rebound(mk(closes), 99)
    closes[99] = 60.0  # 不高于 T-1 → False
    assert not ev._layer_rebound(mk(closes), 99)


def _mk_records(n=200):
    # 全部落在「趋势」层:单调涨;composite 恒定 50,close1 交替 ±
    recs = []
    for k in range(n):
        recs.append({"code": "000001", "date": "2026-04-01", "bar": 99,
                     "composite": 50.0, "risk": 10.0,
                     "close1": 0.01 if k % 2 == 0 else -0.01,
                     "direction_label": 1 if k % 2 == 0 else 0,
                     "gap": 1, "od": 1, "trend3": 1})
    return recs


def test_evaluate_cores_and_undifferentiated_flag():
    import backtest as bt
    import predict as pr
    d = mk(list(range(100, 200)))  # 单调涨 → 趋势层 True
    universe = {"000001": d}
    all_days = list(d["date"])
    pos_of = {"000001": {dt: i for i, dt in enumerate(d["date"])}}
    sector_members = {}
    sector_map = {"000001": []}
    cals = {"direction": pr._fit_calibrator([(50.0, 0)] * 10 + [(50.0, 1)] * 10, 2),
            "gap": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "od": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "trend3": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "return": pr._fit_calibrator([(50.0, 0.0)] * 10, 2, monotone=False),
            "risk": pr._fit_calibrator([(10.0, 0)] * 10, 2)}
    rep = ev.evaluate(_mk_records(), universe, pos_of, all_days, sector_members, sector_map, cals)
    assert set(rep["layers"]) == set(ev.LAYERS)
    assert rep["layer_n"]["趋势"] == 200
    assert "direction" in rep["overall"] and "return" in rep["overall"] and "risk" in rep["overall"]
    # 大盘/趋势/高位/震荡四层均含全部 200 记录(单调涨 + 窄幅),与总体逐样本一致
    # → 各层指标 == 总体指标,|差| 恒 0,永不超过 2σ → 无显著分化
    assert rep["all_undifferentiated"] is True
    assert rep["significant"] == []


def _fake_run_backtest():
    import predict as pr
    return {
        "calibrators": {"direction": pr._fit_calibrator([(50.0, 0)] * 10 + [(50.0, 1)] * 10, 2),
                        "gap": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "od": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "trend3": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "return": pr._fit_calibrator([(50.0, 0.0)] * 10, 2, monotone=False),
                        "risk": pr._fit_calibrator([(10.0, 0)] * 10, 2)},
        "valid_records": _mk_records(),
        "metrics": {},
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "valid_window": {"start": "2026-01-01", "end": "2026-08-13"},
    }


def _default_metric(dim):
    if dim == "path":
        return {"n": 100, "acc_path": 0.6}
    if dim in ("direction", "gap", "trend3"):
        return {"n": 100, "hit_rate": 0.6, "base_rate": 0.5, "n_hold": 0}
    if dim == "return":
        return {"n": 100, "mae": 0.1, "rmse": 0.2, "sign_agreement": 0.6,
                "sign_n": 100, "mean_residual": 0.01, "dir_cond_mae": {}}
    return {"n": 100, "adverse_rate": 0.05, "ece": 0.1, "brier": 0.2, "lift": 1.0}


def _fake_report():
    return {
        "overall": {dim: _default_metric(dim) for dim in ev.DIMS},
        "layers": {L: {dim: _default_metric(dim) for dim in ev.DIMS} for L in ev.LAYERS},
        "layer_n": {L: 100 for L in ev.LAYERS},
        "significant": [],
        "se_bounds": {},
        "all_undifferentiated": True,
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "valid_window": {"start": "2026-01-01", "end": "2026-08-13"},
        "n_valid_records": 100,
    }


def test_layer_suppression_uses_effective_n():
    # direction 层维:n=35,n_hold=30 → 有效 n=5 < 30 → 抑制(总 n≥30 但主指标有效 n 不足)
    rep = _fake_report()
    rep["layers"]["趋势"]["direction"] = {"n": 35, "base_rate": 0.5, "hit_rate": 0.6,
                                          "ece": 0.1, "brier": 0.2, "n_hold": 30}
    payload = ev.build_report(rep)
    assert payload["layers"]["趋势"]["direction"] == {"n": 35, "_suppressed": True}
    # direction 层维:n=35,n_hold=3 → 有效 n=32 ≥ 30 → 不抑制(有 hit_rate 层值)
    rep2 = _fake_report()
    rep2["layers"]["趋势"]["direction"] = {"n": 35, "base_rate": 0.5, "hit_rate": 0.6,
                                           "ece": 0.1, "brier": 0.2, "n_hold": 3}
    payload2 = ev.build_report(rep2)
    assert "hit_rate" in payload2["layers"]["趋势"]["direction"]


def test_significant_flag_respects_primary():
    # return 维:mae 显著但 PRIMARY=sign_agreement 不显著 → 表显「否」
    rep = _fake_report()
    rep["significant"] = [["趋势", "return", "mae", 0.05, 0.02, 0.01]]
    payload = ev.build_report(rep)
    md = ev.render_markdown(payload)
    line = next(l for l in md.splitlines() if l.startswith("| 趋势 | return |"))
    assert line.endswith("| 否 |")
    # 切到 PRIMARY 指标 sign_agreement 显著 → 表显「是」
    rep["significant"] = [["趋势", "return", "sign_agreement", 0.05, 0.02, 0.01]]
    payload = ev.build_report(rep)
    md = ev.render_markdown(payload)
    line = next(l for l in md.splitlines() if l.startswith("| 趋势 | return |"))
    assert line.endswith("| 是 |")


def test_run_build_report_and_main(tmp_path, monkeypatch):
    import backtest as bt
    import predict as pr
    d = mk(list(range(100, 200)))
    monkeypatch.setattr(pr, "run_backtest", lambda dd, sm: _fake_run_backtest())
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {"000001": []})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_sector_members", lambda sm, univ: {})
    out = tmp_path / "ev.json"
    rc = ev.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = ev.json_load(out)
    assert payload["module_version"] == "1.0.0"
    assert "overall" in payload and "layers" in payload
    assert out.with_suffix(".md").exists()
