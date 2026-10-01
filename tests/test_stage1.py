# -*- coding: utf-8 -*-
"""第一阶段：成分等资金、时间因果、样本边界与过期状态。"""
import pandas as pd
import pytest

from core import forward, freshness, stage1_data
from pipeline import predict, theme_strategy, theme_vol_strategy


def test_theme_index_equal_initial_capital(monkeypatch):
    days = pd.date_range("2022-01-03", periods=2, freq="B")
    monkeypatch.setattr(theme_strategy, "THEMES", {"测试": ["A", "B"]})

    def series(code, field, index, forward_fill=True):
        values = [10.0, 11.0] if code == "A" else [100.0, 100.0]
        return pd.Series(values, index=index)

    monkeypatch.setattr(theme_strategy, "load_series", series)
    close, _ = theme_strategy.build_theme_series(days)
    assert close["测试"].iloc[1] / close["测试"].iloc[0] - 1 == pytest.approx(0.05)


def test_last_available_day_is_not_assumed_month_end():
    days = pd.DatetimeIndex(pd.to_datetime(["2026-07-31", "2026-08-03", "2026-08-14"]))
    assert theme_strategy.month_end_flags(days) == {0}


def test_theme_vol_future_open_cannot_change_past_nav(monkeypatch):
    days = pd.DatetimeIndex(pd.to_datetime(["2022-01-28", "2022-01-31",
                                          "2022-02-01", "2022-02-02"]))
    monkeypatch.setattr(theme_vol_strategy, "KPAST", 1)
    monkeypatch.setattr(theme_vol_strategy, "COST_SIDE", 0.0)

    def trial(next_open):
        a_open = pd.Series([10., 10., next_open, 10.], index=days)
        b_open = pd.Series([100., 100., 100., 100.], index=days)
        a_close = pd.Series([10., 11., next_open, next_open * 1.1], index=days)
        b_close = pd.Series([100., 100., 100., 100.], index=days)
        baskets = {"open": {"测试": pd.DataFrame({"A": a_open, "B": b_open})},
                   "close": {"测试": pd.DataFrame({"A": a_close, "B": b_close})}}
        navs = {"测试": pd.Series([1., 1.05, 1.05, 1.10], index=days)}
        vw = {"测试": pd.Series(1., index=days)}
        result = theme_vol_strategy.run({}, {}, navs, days, vw, topn=1, baskets=baskets)
        return result

    x, y = trial(10.), trial(20.)
    assert x["daily_equity"][:2] == y["daily_equity"][:2] == [1., 1.]
    assert x["daily_equity"][-1] == pytest.approx(1.05)
    assert y["daily_equity"][-1] == pytest.approx(1.05)
    assert x["trades"][0]["date"] == "2022-02-01"
    assert x["trades"][0]["shares"]["A"] == pytest.approx(0.05)


def test_legacy_theme_signal_day_nav_is_causal(monkeypatch):
    days = pd.DatetimeIndex(pd.to_datetime(["2022-01-28", "2022-01-31",
                                          "2022-02-01", "2022-02-02"]))
    monkeypatch.setattr(theme_strategy, "KPAST", 1)
    monkeypatch.setattr(theme_strategy, "COST_SIDE", 0.0)
    close = {"测试": pd.Series([1., 1.1, 1.1, 1.1], index=days)}
    navs = theme_strategy.theme_navs(close)

    def trial(next_open):
        opens = {"测试": pd.Series([1., 1., next_open, next_open], index=days)}
        return theme_strategy.run_strategy(close, opens, navs, days, {}, topn=1,
                                           regime_mode="none")

    a, b = trial(1.), trial(2.)
    assert a["daily_equity"][:2] == b["daily_equity"][:2] == [1., 1.]


def test_theme_vol_cash_shares_and_cost_reconcile(monkeypatch):
    days = pd.DatetimeIndex(pd.to_datetime(["2022-01-28", "2022-01-31", "2022-02-01"]))
    monkeypatch.setattr(theme_vol_strategy, "KPAST", 1)
    monkeypatch.setattr(theme_vol_strategy, "COST_SIDE", 0.002)
    a = pd.Series([10., 10., 10.], index=days)
    b = pd.Series([100., 100., 100.], index=days)
    prices = {"测试": pd.DataFrame({"A": a, "B": b})}
    navs = {"测试": pd.Series([1., 1.01, 1.01], index=days)}
    vw = {"测试": pd.Series(1., index=days)}
    result = theme_vol_strategy.run({}, {}, navs, days, vw, topn=1,
                                    baskets={"open": prices, "close": prices})
    trade = result["trades"][0]
    value = trade["cash"] + 10. * trade["shares"]["A"] + 100. * trade["shares"]["B"]
    assert value == pytest.approx(1. - trade["cost"], abs=1e-6)
    assert trade["shares"]["A"] * 10. == pytest.approx(trade["shares"]["B"] * 100., abs=1e-6)
    assert result["daily_equity"][1] == pytest.approx(1.)
    assert result["daily_equity"][2] == pytest.approx(1. / 1.002)


def test_forward_purges_each_horizon_at_label_end(monkeypatch):
    days = [f"2022-01-{i + 1:02d}" for i in range(8)]

    def records(*args):
        for day in days[:4]:
            yield {"date": day, "composite": 50., "risk": 20.,
                   "fwd": {h: (0.01, 0) for h in forward.HORIZONS}}

    monkeypatch.setattr(forward, "_iter_forward", records)
    samples, _ = forward._collect_samples({}, {}, days, range(4), label_end_before=4)
    assert len(samples[1]["direction"]) == 2
    assert len(samples[3]["direction"]) == 0


def test_predict_purges_three_day_label_separately(monkeypatch):
    days = [f"2022-01-{i + 1:02d}" for i in range(8)]

    def records(*args):
        for day in days[:4]:
            yield {"date": day, "composite": 50., "risk": 20., "close1": 0.01,
                   "lbl": {"close1": 1, "gap": 1, "od": 1, "trend3": 1}}

    monkeypatch.setattr(predict, "_iter_scored", records)
    samples = predict._collect_samples({}, {}, days, range(4), label_end_before=4)
    assert len(samples["direction"]) == 3
    assert len(samples["trend3"]) == 1


def test_historical_regime_loses_action_advice():
    state = freshness.mark_regime({"as_of": "2026-08-14", "label": "退潮",
                                   "advice": {"action": "avoid"}, "swing": {"action": "avoid"}},
                                  now="2026-09-29")
    assert state["freshness"]["status"] == "historical"
    assert state["advice"] is None and state["swing"] is None


def test_stage1_calendar_holiday_and_coverage(monkeypatch, tmp_path):
    path = tmp_path / "calendar.json"
    stage1_data.save_calendar(pd.DataFrame({"trade_date": ["2026-09-29", "2026-10-09"]}),
                              path=path, collected_at="2026-09-29T20:00:00+08:00")
    monkeypatch.setattr(freshness, "load_calendar", lambda: stage1_data.load_calendar(path))
    state = freshness.assess("2026-09-29", now="2026-10-08", max_sessions=0)
    assert state["status"] == "current" and state["sessions_behind"] == 0
    assert state["calendar_basis"] == "akshare_sina"
    assert freshness.assess("2026-10-09", now="2027-01-01")["status"] == "unknown"


def test_stage1_raw_price_has_units_hash_and_window_gate(tmp_path):
    frame = pd.DataFrame({"日期": ["2022-01-04", "2022-01-03"], "开盘": [11, 10],
                          "最高": [12, 11], "最低": [10, 9], "收盘": [11, 10],
                          "成交量": [2, 1], "成交额": [2200, 1000], "换手率": [2, 1]})
    meta = stage1_data.save_raw(frame, "000001", path=tmp_path, collected_at="2022-01-05")
    saved = pd.read_csv(tmp_path / "raw_daily" / "000001.csv")
    assert saved["volume"].tolist() == [100, 200]
    assert saved["turnover"].tolist() == [0.01, 0.02]
    assert meta["sha256"] == stage1_data.sha256(tmp_path / "raw_daily" / "000001.csv")
    report = stage1_data.formal_gates(tmp_path, "2022-01-03", "2022-01-04", ["000001"])
    assert report["gates"]["unadjusted_prices_for_requested_codes"]
    assert report["status"] == "exploratory"


def test_stage1_membership_is_not_backdated(tmp_path):
    snap = stage1_data.save_membership_snapshot(["000001", "600000"], "测试", "industry",
                                                 "source", "2026-09-30", path=tmp_path)
    assert stage1_data.membership_at([snap], "2026-09-29", "测试", "industry") is None
    assert stage1_data.membership_at([snap], "2026-09-30", "测试", "industry") == ["000001", "600000"]
