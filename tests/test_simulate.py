# -*- coding: utf-8 -*-
"""simulate.py 交易模拟引擎 + 风险收益评价测试。"""
import numpy as np
import pandas as pd
import pytest

from core import backtest as bt
from pipeline import simulate as sim


def _df(closes, opens, highs, lows):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(n)],
        "open": [float(x) for x in opens],
        "high": [float(x) for x in highs],
        "low": [float(x) for x in lows],
        "close": [float(x) for x in closes],
        "volume": [100000.0] * n,
    })


# ---- simulate_trade ----

def test_simulate_trade_hold_to_end():
    d = _df([100, 101, 103, 102], [100, 101, 103, 102],
            [100, 105, 104, 103], [100, 99, 98, 97])
    t = sim.simulate_trade(d, 0, 3, None, None)
    assert t["entry"] == pytest.approx(101.0)
    assert t["exit"] == pytest.approx(102.0)
    assert t["exit_reason"] == "hold"
    assert t["holding_days_actual"] == 2
    assert t["ret_gross"] == pytest.approx(102.0 / 101.0 - 1.0)
    assert t["max_fav"] == pytest.approx(105.0 / 101.0 - 1.0)
    assert t["max_adv"] == pytest.approx(97.0 / 101.0 - 1.0)


def test_simulate_trade_stop_hit_intraday():
    d = _df([100, 98, 99, 100], [100, 99, 99, 100],
            [100, 99, 99, 100], [100, 96, 94, 99])
    t = sim.simulate_trade(d, 0, 3, -0.03, None)
    assert t["entry"] == pytest.approx(99.0)
    assert t["exit"] == pytest.approx(99.0 * 0.97)
    assert t["exit_reason"] == "stop"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(-0.03)


def test_simulate_trade_target_hit_intraday():
    d = _df([100, 102, 104, 101], [100, 101, 103, 101],
            [100, 105, 106, 101], [100, 100, 102, 100])
    t = sim.simulate_trade(d, 0, 3, None, 0.04)
    assert t["exit"] == pytest.approx(101.0 * 1.04)
    assert t["exit_reason"] == "target"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(0.04)


def test_simulate_trade_gap_open_through_stop():
    d = _df([100, 95, 88, 97], [100, 94, 88, 97],
            [100, 96, 90, 97], [100, 92, 87, 96])
    t = sim.simulate_trade(d, 0, 3, -0.05, None)
    assert t["exit"] == pytest.approx(88.0)
    assert t["exit_reason"] == "stop"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(88.0 / 94.0 - 1.0)


def test_simulate_trade_same_day_stop_before_target():
    # 同日内 low<=stop 且 high>=target 双触,保守取 stop 先
    d = _df([100, 99, 100, 100], [100, 100, 100, 100],
            [100, 105, 105, 100], [100, 96, 96, 100])
    t = sim.simulate_trade(d, 0, 3, -0.03, 0.04)
    assert t["exit"] == pytest.approx(97.0)
    assert t["exit_reason"] == "stop"
    assert t["holding_days_actual"] == 1


def test_simulate_trade_none_at_last_bar():
    d = _df([100, 101, 102, 103], [100, 101, 102, 103],
            [100, 101, 102, 103], [100, 100, 101, 102])
    assert sim.simulate_trade(d, 3, 3, None, None) is None


def test_simulate_trade_truncated_at_tail():
    d = _df([100, 101, 102, 103, 104], [100, 101, 102, 103, 104],
            [100, 101, 102, 103, 104], [100, 100, 101, 102, 103])
    t = sim.simulate_trade(d, 2, 3, None, None)
    assert t["holding_days_actual"] == 1  # T+1 买入，T+2 收盘退出
    assert t["exit"] == pytest.approx(104.0)
    assert t["ret_gross"] == pytest.approx(104.0 / 103.0 - 1.0)


def test_simulate_trade_nonpositive_entry_none():
    d = _df([100, -5, 102], [100, -5, 102], [100, 101, 102], [99, 100, 101])
    assert sim.simulate_trade(d, 0, 3, None, None) is None


# ---- summarize ----

def test_summarize_metrics():
    trades = [
        {"ret_gross": 0.10, "max_fav": 0.15, "max_adv": -0.05},
        {"ret_gross": -0.05, "max_fav": 0.02, "max_adv": -0.08},
        {"ret_gross": 0.02, "max_fav": 0.04, "max_adv": -0.03},
    ]
    s = sim.summarize(trades, cost_bps=20)  # 双边 0.004
    nets = [0.096, -0.054, 0.016]
    assert s["n_trades"] == 3
    assert s["win_rate"] == pytest.approx(2.0 / 3.0)
    assert s["avg_win"] == pytest.approx(np.mean([0.096, 0.016]))
    assert s["avg_loss"] == pytest.approx(-0.054)
    assert s["profit_factor"] == pytest.approx((0.096 + 0.016) / 0.054)
    assert s["expectancy"] == pytest.approx(np.mean(nets))
    assert s["max_drawdown"] == pytest.approx(0.054)
    assert s["avg_max_fav"] == pytest.approx(np.mean([0.15, 0.02, 0.04]))
    assert s["avg_max_adv"] == pytest.approx(np.mean([-0.05, -0.08, -0.03]))
    assert s["mean_ret_gross"] == pytest.approx(np.mean([0.10, -0.05, 0.02]))


def test_summarize_no_losses_profit_factor_none():
    trades = [{"ret_gross": 0.10, "max_fav": 0.10, "max_adv": 0.0}]
    s = sim.summarize(trades, cost_bps=0)
    assert s["profit_factor"] is None
    assert s["win_rate"] == pytest.approx(1.0)
    assert s["max_drawdown"] == pytest.approx(0.0)


def test_summarize_empty():
    s = sim.summarize([], 20)
    assert s["n_trades"] == 0
    assert s["win_rate"] is None
    assert s["avg_win"] is None
    assert s["avg_loss"] is None
    assert s["profit_factor"] is None
    assert s["expectancy"] is None
    assert s["max_drawdown"] is None
    assert s["avg_max_fav"] is None
    assert s["avg_max_adv"] is None
    assert s["mean_ret_gross"] is None


# ---- run 布线(选篮 -> 逐笔 -> summary) ----

def test_run_wiring(tmp_path, monkeypatch):
    days = [f"2026-01-{i + 1:02d}" for i in range(63)]
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {"600000": ["半导体"]})
    monkeypatch.setattr(bt, "build_universe",
                        lambda d, sm: ({"600000": object()}, ["600000"]))
    monkeypatch.setattr(bt, "build_sector_members",
                        lambda sm, u: {"半导体": ["600000"]})
    monkeypatch.setattr(bt, "build_calendar",
                        lambda u, c: (days, {"600000": {dt: 0 for dt in days}}))
    monkeypatch.setattr(bt, "sector_heat", lambda *a: {"半导体": 0.05})
    monkeypatch.setattr(bt, "build_buyable", lambda *a: ({"600000"}, {}, {}, {}))

    def fake_baskets(*a, **k):
        return {"A": ["600000"], "E_hi": [], "E_lo": [], "B": None, "C": [], "D": []}

    monkeypatch.setattr(bt, "select_baskets", fake_baskets)

    calls = []

    def fake_trade(d, bar, holding_days, stop_pct, take_pct):
        calls.append((bar, holding_days, stop_pct, take_pct))
        return {"entry": 100.0, "exit": 110.0, "exit_reason": "hold",
                "holding_days_actual": 3, "ret_gross": 0.10,
                "max_fav": 0.15, "max_adv": -0.02}

    monkeypatch.setattr(sim, "simulate_trade", fake_trade)

    res = sim.run("x", "y", holding_days=3, stop_pct=-0.08, take_pct=None, cost_bps=20)
    assert res["n_eval"] == 1
    assert calls == [(0, 3, -0.08, None)]
    assert res["baskets"]["A"]["summary"]["n_trades"] == 1
    assert res["baskets"]["A"]["summary"]["expectancy"] == pytest.approx(0.10 - 0.004)
    assert res["baskets"]["A"]["summary"]["profit_factor"] is None  # 全胜无亏损


# ---- build_report / render_markdown ----

def test_build_report_serializes_and_sanitizes():
    res = {
        "config": {"holding_days": 3, "stop_pct": -0.08, "take_pct": None, "cost_bps": 20},
        "data_range": {"start": "2026-01-01", "end": "2026-08-01"},
        "window": {"start": "2026-01-05", "end": "2026-07-31"},
        "n_eval": 2, "step": 1,
        "baskets": {
            "A": {"summary": sim.summarize(
                [{"ret_gross": 0.10, "max_fav": 0.15, "max_adv": -0.05},
                 {"ret_gross": -0.05, "max_fav": 0.02, "max_adv": -0.08}], 20)},
            "D": {"summary": sim.summarize([], 20)},
        },
        "notes": ["n1"],
    }
    payload = sim.build_report(res, system_version="abc123", generated_at="t")
    assert payload["system_version"] == "abc123"
    assert payload["module_version"] == sim.MODULE_VERSION
    assert payload["config"]["cost_bps"] == 20
    assert payload["baskets"]["A"]["n_trades"] == 2
    assert payload["baskets"]["A"]["profit_factor"] == pytest.approx(
        (0.096) / 0.054)
    assert payload["baskets"]["D"]["n_trades"] == 0
    assert payload["baskets"]["D"]["win_rate"] is None


def test_render_markdown_contains_table():
    res = {
        "config": {"holding_days": 3, "stop_pct": -0.08, "take_pct": None, "cost_bps": 20},
        "data_range": {"start": "2026-01-01", "end": "2026-08-01"},
        "window": {"start": "2026-01-05", "end": "2026-07-31"},
        "n_eval": 2, "step": 1,
        "baskets": {"A": {"summary": sim.summarize(
            [{"ret_gross": 0.10, "max_fav": 0.15, "max_adv": -0.05}], 20)}},
        "notes": ["保守日内假设", "双边成本"],
    }
    payload = sim.build_report(res, system_version="abc", generated_at="t")
    md = sim.render_markdown(payload)
    assert "交易模拟 + 风险收益评价" in md
    assert "profit_factor" in md or "盈亏比" in md
    assert "保守日内假设" in md
    assert "A" in md


# ---- risk_tiered_stop ----

def test_risk_tiered_stop_endpoints():
    assert sim.risk_tiered_stop(0.0) == pytest.approx(-0.12)
    assert sim.risk_tiered_stop(1.0) == pytest.approx(-0.04)
    assert sim.risk_tiered_stop(0.5) == pytest.approx(-0.08)
    assert sim.risk_tiered_stop(None) == pytest.approx(-0.08)


def test_risk_tiered_stop_custom_params():
    assert sim.risk_tiered_stop(0.0, tight=-0.02, loose=-0.10) == pytest.approx(-0.10)
    assert sim.risk_tiered_stop(1.0, tight=-0.02, loose=-0.10) == pytest.approx(-0.02)
    assert sim.risk_tiered_stop(0.25, tight=-0.02, loose=-0.10) == pytest.approx(-0.08)


# ---- run_risk_ab 布线(选篮一次 -> flat/tiered 各模拟一次 -> mean_diff) ----

def test_run_risk_ab_wiring(tmp_path, monkeypatch):
    days = [f"2026-01-{i + 1:02d}" for i in range(63)]
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {"600000": ["半导体"]})
    monkeypatch.setattr(bt, "build_universe", lambda d, sm: ({"600000": object()}, ["600000"]))
    monkeypatch.setattr(bt, "build_sector_members", lambda sm, u: {"半导体": ["600000"]})
    monkeypatch.setattr(bt, "build_calendar", lambda u, c: (days, {"600000": {dt: 0 for dt in days}}))
    monkeypatch.setattr(bt, "sector_heat", lambda *a: {"半导体": 0.05})
    monkeypatch.setattr(bt, "build_buyable", lambda *a: ({"600000"}, {}, {}, {}))
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"risk": 0.0, "composite": 60.0, "position": 50.0})

    def fake_baskets(*a, **k):
        return {"A": ["600000"], "E_hi": [], "E_lo": [], "B": None, "C": [], "D": []}

    monkeypatch.setattr(bt, "select_baskets", fake_baskets)

    class FakeRiskCal:
        def p_up(self, risk):
            return 0.0   # risk_p=0 -> tiered stop = loose = -0.12

    monkeypatch.setattr(sim.predict, "run_backtest",
                        lambda d, sm: {"calibrators": {"risk": FakeRiskCal()},
                                       "n_train": 0, "n_valid": 1})

    calls = []

    def fake_trade(d, bar, holding_days, stop_pct, take_pct):
        calls.append((bar, holding_days, stop_pct, take_pct))
        ret = 0.10 if stop_pct == -0.08 else 0.06
        return {"entry": 100.0, "exit": 100.0 * (1.0 + ret), "exit_reason": "hold",
                "holding_days_actual": 3, "ret_gross": ret,
                "max_fav": 0.15, "max_adv": -0.02}

    monkeypatch.setattr(sim, "simulate_trade", fake_trade)

    res = sim.run_risk_ab("x", "y", holding_days=3, cost_bps=20,
                          base_stop=-0.08, tight=-0.04, loose=-0.12)
    # 同一笔交易 flat 先、tiered 后,止损分别为 base_stop 与 loose
    assert calls == [(0, 3, -0.08, None), (0, 3, -0.12, None)]
    b = res["baskets"]["A"]
    assert b["n"] == 1
    assert b["flat"]["n_trades"] == 1
    assert b["tiered"]["n_trades"] == 1
    # flat net=0.10-0.004;tiered net=0.06-0.004;diff=-0.04
    assert b["mean_diff"] == pytest.approx((0.06 - 0.004) - (0.10 - 0.004))
    assert res["n_valid_eval"] == 1


# ---- build_ab_report / render_ab_markdown ----

def test_build_ab_report_serializes():
    flat_s = sim.summarize([{"ret_gross": 0.10, "max_fav": 0.15, "max_adv": -0.05}], 20)
    tiered_s = sim.summarize([{"ret_gross": 0.06, "max_fav": 0.15, "max_adv": -0.05}], 20)
    res = {
        "config": {"holding_days": 3, "cost_bps": 20, "base_stop": -0.08,
                   "tight": -0.04, "loose": -0.12},
        "data_range": {"start": "2026-01-01", "end": "2026-08-01"},
        "window": {"start": "2026-01-05", "end": "2026-07-31"},
        "n_eval": 2, "n_train": 1, "n_valid": 1, "n_valid_eval": 1, "step": 1,
        "baskets": {"A": {"n": 1, "flat": flat_s, "tiered": tiered_s, "mean_diff": -0.04}},
        "notes": ["n1"],
    }
    payload = sim.build_ab_report(res, system_version="abc", generated_at="t")
    assert payload["system_version"] == "abc"
    assert payload["module_version"] == sim.MODULE_VERSION
    assert payload["baskets"]["A"]["flat"]["n_trades"] == 1
    assert payload["baskets"]["A"]["tiered"]["n_trades"] == 1
    assert payload["baskets"]["A"]["mean_diff"] == pytest.approx(-0.04)
    assert payload["baskets"]["A"]["flat"]["expectancy"] == pytest.approx(0.10 - 0.004)


def test_render_ab_markdown_contains_tables():
    flat_s = sim.summarize([{"ret_gross": 0.10, "max_fav": 0.15, "max_adv": -0.05}], 20)
    tiered_s = sim.summarize([{"ret_gross": 0.06, "max_fav": 0.15, "max_adv": -0.05}], 20)
    res = {
        "config": {"holding_days": 3, "cost_bps": 20, "base_stop": -0.08,
                   "tight": -0.04, "loose": -0.12},
        "data_range": {"start": "2026-01-01", "end": "2026-08-01"},
        "window": {"start": "2026-01-05", "end": "2026-07-31"},
        "n_eval": 2, "n_train": 1, "n_valid": 1, "n_valid_eval": 1, "step": 1,
        "baskets": {"A": {"n": 1, "flat": flat_s, "tiered": tiered_s, "mean_diff": -0.04}},
        "notes": ["risk_p 线性内插", "保守日内假设"],
    }
    payload = sim.build_ab_report(res, system_version="abc", generated_at="t")
    md = sim.render_ab_markdown(payload)
    assert "flat vs tiered" in md
    assert "A" in md
    assert "risk_p 线性内插" in md
