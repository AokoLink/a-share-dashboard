# -*- coding: utf-8 -*-
"""simulate.py 交易模拟引擎 + 风险收益评价测试。"""
import numpy as np
import pandas as pd
import pytest

import backtest as bt
import simulate as sim


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
    assert t["entry"] == pytest.approx(100.0)
    assert t["exit"] == pytest.approx(102.0)
    assert t["exit_reason"] == "hold"
    assert t["holding_days_actual"] == 3
    assert t["ret_gross"] == pytest.approx(0.02)
    assert t["max_fav"] == pytest.approx(105.0 / 100.0 - 1.0)
    assert t["max_adv"] == pytest.approx(97.0 / 100.0 - 1.0)


def test_simulate_trade_stop_hit_intraday():
    d = _df([100, 98, 99, 100], [100, 99, 99, 100],
            [100, 99, 99, 100], [100, 96, 98, 99])
    t = sim.simulate_trade(d, 0, 3, -0.03, None)
    assert t["exit"] == pytest.approx(97.0)
    assert t["exit_reason"] == "stop"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(97.0 / 100.0 - 1.0)


def test_simulate_trade_target_hit_intraday():
    d = _df([100, 102, 104, 101], [100, 101, 103, 101],
            [100, 105, 105, 101], [100, 100, 102, 100])
    t = sim.simulate_trade(d, 0, 3, None, 0.04)
    assert t["exit"] == pytest.approx(104.0)
    assert t["exit_reason"] == "target"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(0.04)


def test_simulate_trade_gap_open_through_stop():
    d = _df([100, 95, 96, 97], [100, 94, 96, 97],
            [100, 96, 97, 97], [100, 94, 95, 96])
    t = sim.simulate_trade(d, 0, 3, -0.05, None)
    assert t["exit"] == pytest.approx(94.0)  # open 跳空成交,非 95
    assert t["exit_reason"] == "stop"
    assert t["holding_days_actual"] == 1
    assert t["ret_gross"] == pytest.approx(94.0 / 100.0 - 1.0)


def test_simulate_trade_same_day_stop_before_target():
    # 同日内 low<=stop 且 high>=target 双触,保守取 stop 先
    d = _df([100, 99, 100, 100], [100, 100, 100, 100],
            [100, 105, 100, 100], [100, 96, 100, 100])
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
    assert t["holding_days_actual"] == 2  # 尾部只剩 2 根
    assert t["exit"] == pytest.approx(104.0)
    assert t["ret_gross"] == pytest.approx(104.0 / 102.0 - 1.0)


def test_simulate_trade_nonpositive_entry_none():
    d = _df([-5, 101, 102], [100, 101, 102], [100, 101, 102], [99, 100, 101])
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
