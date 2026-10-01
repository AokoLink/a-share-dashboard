# -*- coding: utf-8 -*-
"""第二阶段：资格共用、策略独立、信号因果与同日对照。"""
import numpy as np
import pandas as pd

from core import strategies, strategy_lifecycle as lifecycle
from pipeline.evaluate_strategies import evaluate_day


def frame(closes, volumes=None, highs=None):
    n = len(closes)
    days = pd.date_range("2025-01-02", periods=n, freq="B")
    return pd.DataFrame({"date": days, "open": closes, "high": highs or [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes,
                         "volume": volumes or [100000] * n, "amount": [2e8] * n})


def test_breakout_keeps_high_position_and_future_does_not_change_signal():
    closes = [10.0] * 65 + [10.8] + [10.8] * 9
    volumes = [100000] * 61 + [200000] * 14
    data = frame(closes, volumes, [10.1] * 65 + [10.9] * 10)
    today = data.iloc[:66]
    first = strategies.screen(today, "600001", "测试")
    assert [s["strategy"] for s in first["signals"]] == ["breakout"]
    assert "sector_relative_strength_unverified" in first["signals"][0]["caveats"]
    assert strategies.screen(today, "600001", "测试", sector_rel20=-0.01)["signals"] == []
    changed = data.copy()
    changed.loc[66, "open"] = 100.0
    assert strategies.screen(changed.iloc[:66], "600001", "测试") == first
    assert strategies.execution_check(10.8, 11.5) == (False, "gap_above_5pct")


def test_pullback_and_rebound_are_separate_definitions():
    closes = list(np.linspace(10, 14, 45)) + list(np.linspace(14, 15, 19)) + [14.2]
    volumes = [100000] * 60 + [60000] * 5
    pull = strategies.screen(frame(closes, volumes), "600001", "测试")
    assert "pullback" in [s["strategy"] for s in pull["signals"]]
    rebound = frame([10.0] * 59 + [9.9, 9.7, 9.4, 9.1, 8.9, 8.8])
    assert strategies.screen(rebound, "600001", "测试")["signals"] == []
    fear = strategies.screen(rebound, "600001", "测试", market_state="恐慌")
    assert [s["strategy"] for s in fear["signals"]] == ["rebound"]


def test_qualification_rejects_st_missing_amount_and_limit_up():
    data = frame([10.0] * 65)
    assert strategies.base_eligibility(data, "600001", "ST测试")[1] == "st"
    assert strategies.base_eligibility(data.drop(columns="amount"), "600001", "测试")[1] == "low_or_unknown_liquidity"
    data.loc[64, "close"] = 11.0
    assert strategies.base_eligibility(data, "600001", "测试")[1] == "limit_up"


def test_same_day_controls_share_pool_cost_and_execution():
    days = pd.date_range("2025-01-02", periods=75, freq="B")
    a = frame([10.0] * 65 + [10.8] + [10.8] * 9,
              [100000] * 61 + [200000] * 14, [10.1] * 65 + [10.9] * 10)
    b = frame([10.0] * 75)
    a.loc[66, "open"] = 10.9
    a.loc[71, "open"] = 12.0
    universe = {"600001": a, "600002": b}
    pos = {code: {day: i for i, day in enumerate(days)} for code in universe}
    result = evaluate_day(universe, pos, list(days), 65,
                          {"600001": ["行业"], "600002": ["行业"]}, pool_n=2, top_n=1,
                          random_draws=20)["breakout"]
    assert result["candidates"] == result["executed"] == 1
    selected_net = lifecycle.net_open_return(10.9, 12.0)
    flat_net = lifecycle.net_open_return(10.0, 10.0)
    assert result["full"] == selected_net
    assert result["market_equal"] == (selected_net + flat_net) / 2
    assert result["sector_equal"] is None  # 静态当前映射不能冒充该历史日归属
    assert result["gross_arms"]["full"] == 12.0 / 10.9 - 1
    assert result["net_arms"]["full"] == result["full"]
    assert result["cost_model_version"] == lifecycle.COST_MODEL_VERSION
