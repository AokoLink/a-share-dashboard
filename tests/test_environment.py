# -*- coding: utf-8 -*-
"""environment.py 市场环境分类测试。"""
from core import environment as env
import pandas as pd
import pytest
from core import backtest as bt


def _row(r5=None, r1=None, up=None, ld=0, lu=0, tr=None, r20=None, r60=0.0,
         ma5=1.0, ma20=1.0, ma60=1.0):
    """构造 classify 输入行;默认 r60=0.0/ma60=1.0 通过历史闸,ma 平坦无趋势。"""
    return {"r1": r1, "up_ratio": up, "limit_up": lu, "limit_down": ld,
            "turnover_ratio": tr, "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "r5": r5, "r20": r20, "r60": r60}


def test_insufficient_history():
    assert env.classify(_row(r60=None)) is None
    assert env.classify(_row(ma60=None)) is None


def test_panic_r5_boundary():
    assert env.classify(_row(r5=-0.08)) == "恐慌"
    assert env.classify(_row(r5=-0.079)) == "震荡"


def test_panic_breadth():
    assert env.classify(_row(r1=-0.04, up=0.15)) == "恐慌"


def test_panic_limit_down():
    assert env.classify(_row(ld=300)) == "恐慌"
    assert env.classify(_row(ld=299)) == "震荡"


def test_climax():
    assert env.classify(_row(r5=0.08, up=0.85)) == "高潮"
    assert env.classify(_row(r5=0.08, lu=100)) == "高潮"
    assert env.classify(_row(r5=0.08, tr=1.8)) == "高潮"
    assert env.classify(_row(r5=0.079, up=0.9)) == "震荡"


def test_bear():
    assert env.classify(_row(r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "熊"


def test_bull():
    assert env.classify(_row(r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "牛"


def test_recovery():
    assert env.classify(_row(r5=0.03, r20=-0.01, up=0.55)) == "恢复"


def test_receding():
    assert env.classify(_row(r20=0.01, r5=-0.03, up=0.45)) == "退潮"


def test_oscillation_fallback():
    assert env.classify(_row(r5=0.0, r20=0.0, up=0.5)) == "震荡"


def test_priority_panic_over_bear():
    assert env.classify(_row(r5=-0.08, r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "恐慌"


def test_priority_climax_over_bull():
    assert env.classify(_row(r5=0.08, up=0.85, r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "高潮"


def test_classify_none_limit_counts_do_not_crash():
    # 规格 §6:任一判定字段为 NaN/None → 该条不命中;limit_down/limit_up=None 不得 TypeError
    assert env.classify(_row(ld=None)) == "震荡"
    assert env.classify(_row(r5=0.08, up=None, lu=None, tr=None)) == "震荡"


def _mk_df(closes, amounts=None, change_pct=None):
    """构造 universe 单股 DataFrame(含 change_pct/amount,与 bt.build_universe 同口径)。"""
    n = len(closes)
    if amounts is None:
        amounts = [1e8] * n
    if change_pct is None:
        change_pct = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)]
        change_pct = [v * 100.0 for v in change_pct]
    return pd.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(n)],
        "close": [float(c) for c in closes],
        "amount": [float(a) for a in amounts],
        "change_pct": [float(v) for v in change_pct],
    })


def _universe(n=70):
    """单股单调涨(close=100..100+n-1)、amount 恒定。"""
    closes = [100.0 + i for i in range(n)]
    universe = {"000001": _mk_df(closes)}
    all_days = list(universe["000001"]["date"])
    pos_of = {"000001": {dt: i for i, dt in enumerate(all_days)}}
    return universe, all_days, pos_of


def test_build_series_columns():
    universe, all_days, pos_of = _universe(70)
    s = env.build_series(universe, pos_of, all_days)
    assert list(s.columns) == ["date", "r1", "up_ratio", "limit_up", "limit_down",
                               "turnover", "turnover_ratio", "M", "ma5", "ma20", "ma60",
                               "r5", "r20", "r60", "environment"]
    assert len(s) == 70


def test_build_series_r1_median():
    n = 10
    base = [0.0] * n
    c1 = base[:]; c1[5] = 1.0
    c2 = base[:]; c2[5] = 2.0
    c3 = base[:]; c3[5] = 3.0
    universe = {"000001": _mk_df([100.0] * n, change_pct=c1),
                "000002": _mk_df([100.0] * n, change_pct=c2),
                "000003": _mk_df([100.0] * n, change_pct=c3)}
    all_days = list(universe["000001"]["date"])
    pos_of = {c: {dt: i for i, dt in enumerate(all_days)} for c in universe}
    s = env.build_series(universe, pos_of, all_days)
    assert s["r1"].iloc[5] == pytest.approx(0.02)   # median(0.01,0.02,0.03)


def test_build_series_environment_causal():
    """反泄露:改未来(i+2)的 change_pct/amount 不影响 label[i](pandas 3.0 CoW 须用 .loc)。"""
    universe, all_days, pos_of = _universe(70)
    s1 = env.build_series(universe, pos_of, all_days)
    i = 65
    label_before = s1["environment"].iloc[i]
    d = universe["000001"]
    d.loc[i + 2, "change_pct"] = -10.0
    d.loc[i + 2, "amount"] = 0.0
    s2 = env.build_series(universe, pos_of, all_days)
    assert s2["environment"].iloc[i] == label_before


def test_build_series_le_sensitive():
    """<=T 敏感:改当日 change_pct 会改变 r1[i](证明序列确实消费 <=T 数据,非真空)。"""
    universe, all_days, pos_of = _universe(70)
    s1 = env.build_series(universe, pos_of, all_days)
    i = 65
    universe["000001"].loc[i, "change_pct"] = -20.0
    s2 = env.build_series(universe, pos_of, all_days)
    assert s2["r1"].iloc[i] != s1["r1"].iloc[i]


def _patch_bt(monkeypatch, universe, all_days, pos_of):
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: (universe, list(universe.keys())))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))


def test_run_distribution(tmp_path, monkeypatch):
    universe, all_days, pos_of = _universe(70)
    _patch_bt(monkeypatch, universe, all_days, pos_of)
    rep = env.run(str(tmp_path), "x")
    assert rep["n_days"] == 70
    assert rep["n_classified"] == 10
    assert rep["n_insufficient"] == 60
    assert sum(rep["distribution"].values()) == 10
    assert rep["distribution"]["牛"] == 10
    assert set(rep["state_stats"]) == set(env.LABELS)
    # 规格 §7:count < MIN_STATE_N(含 ==0)即退化;本 fixture 牛=10 < MIN_STATE_N=20,同为退化态
    # (brief 断言 set(env.LABELS)-{"牛"} 与 spec/代码冲突,按 spec 权威改为全 7 态)
    assert set(rep["degenerate_states"]) == set(env.LABELS)


def test_build_report_render():
    rep = {
        "data_range": {"first": "2026-01-01", "last": "2026-08-13"},
        "n_days": 100, "n_classified": 40, "n_insufficient": 60,
        "distribution": {L: (10 if L == "牛" else 5) for L in env.LABELS},
        "state_stats": {L: {"n": 5, "mean_r5": 0.01, "mean_r20": 0.02,
                            "mean_up_ratio": 0.5, "mean_turnover_ratio": 1.0}
                        for L in env.LABELS},
        "degenerate_states": ["恢复"],
        "series": [],
    }
    payload = env.build_report(rep)
    assert payload["mode"] == "environment"
    assert payload["distribution"]["牛"] == 10
    md = env.render_markdown(payload)
    assert "## 状态分布" in md
    assert "## 退化态提示" in md
    assert "恢复" in md


def test_main_end_to_end(tmp_path, monkeypatch):
    import json
    universe, all_days, pos_of = _universe(70)
    _patch_bt(monkeypatch, universe, all_days, pos_of)
    out = tmp_path / "env.json"
    rc = env.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "environment"
    assert payload["n_days"] == 70
    assert out.with_suffix(".md").exists()


# ---------- regime gate ----------

def test_regime_advice_mapping():
    assert env.regime_advice("恐慌")["action"] == "buy_dip"
    assert env.regime_advice("高潮")["action"] == "avoid"
    assert env.regime_advice("退潮")["action"] == "avoid"
    for lab in ("牛", "熊", "震荡", "恢复"):
        assert env.regime_advice(lab)["action"] == "neutral"
    assert env.regime_advice(None)["action"] == "unknown"
    assert env.regime_advice("未知态")["action"] == "unknown"
    assert isinstance(env.regime_advice("恐慌")["message"], str)
    assert "持有 2-3 天" in env.regime_advice("恐慌")["message"]
    assert "持有净胜率" in env.regime_advice("恐慌")["message"]   # 钉住"持有期净胜率",防"日级净胜率"歧义回归


def test_regime_swing_action_mapping():
    assert env.regime_swing_action("恐慌")["action"] == "opportunity"
    assert env.regime_swing_action("高潮")["action"] == "exit"
    assert env.regime_swing_action("退潮")["action"] == "exit"
    for lab in ("牛", "熊", "震荡", "恢复"):
        assert env.regime_swing_action(lab)["action"] == "hold"
    assert env.regime_swing_action(None)["action"] == "unknown"
    assert env.regime_swing_action("未知态")["action"] == "unknown"
    assert isinstance(env.regime_swing_action("恐慌")["message"], str)


def test_latest_state_bull(tmp_path, monkeypatch):
    universe, all_days, pos_of = _universe(70)   # 单股单调涨 -> 牛
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm, tail_n=None: (universe, list(universe.keys())))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))
    st = env.latest_state(str(tmp_path), "x", tail_n=None)
    assert st["as_of"] == all_days[-1]
    assert st["label"] == "牛"
    assert st["advice"]["action"] == "neutral"
    assert st["swing"]["action"] == "hold"
    assert st["metrics"]["r5"] is not None
    assert st["metrics"]["up_ratio"] is not None


def test_latest_state_cached(tmp_path, monkeypatch):
    universe, all_days, pos_of = _universe(70)
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm, tail_n=None: (universe, list(universe.keys())))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))
    cache = tmp_path / "regime_cache.json"
    calls = {"n": 0}
    real_build = env.build_series

    def counted(univ, po, ad):
        calls["n"] += 1
        return real_build(univ, po, ad)

    monkeypatch.setattr(env, "build_series", counted)
    st1 = env.latest_state(str(tmp_path), "x", tail_n=None, cache_path=str(cache))
    assert calls["n"] == 1
    assert st1["label"] == "牛"
    st2 = env.latest_state(str(tmp_path), "x", tail_n=None, cache_path=str(cache))
    assert calls["n"] == 1                      # 缓存命中,不重算 build_series
    assert st2["label"] == st1["label"]
    got = env.load_cached_regime(str(cache))
    assert got["label"] == "牛"
    assert got["advice"]["action"] == "neutral"
    assert got["swing"]["action"] == "hold"


def test_load_cached_regime_missing(tmp_path):
    assert env.load_cached_regime(str(tmp_path / "nope.json")) is None
