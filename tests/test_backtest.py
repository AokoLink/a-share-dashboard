# -*- coding: utf-8 -*-
import json

import numpy as np
import pandas as pd
import pytest

import backtest as bt
import analysis as an


def make_daily(closes, opens=None, volumes=None, start="2026-01-01"):
    """构造日线 DataFrame,date 递增。open 缺省 = close;volume 缺省 100000。"""
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    vols = [float(v) for v in volumes] if volumes is not None else [100000.0] * n
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": [float(c) for c in closes], "volume": vols,
    })


def test_load_sector_map_decodes_gbk(tmp_path):
    path = tmp_path / "code2sector.json"
    with open(path, "w", encoding="gbk") as f:
        json.dump({"600000": ["半导体", "白酒"]}, f, ensure_ascii=False)
    assert bt.load_sector_map(str(path)) == {"600000": ["半导体", "白酒"]}


def test_load_sector_map_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.load_sector_map(str(tmp_path / "nope.json"))


def test_load_daily_roundtrip(tmp_path):
    d = make_daily([10.0, 10.5, 11.0])
    d.to_pickle(tmp_path / "600000.pkl")
    loaded = bt.load_daily(str(tmp_path), "600000")
    assert list(loaded.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(loaded) == 3


def test_load_daily_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.load_daily(str(tmp_path), "999999")


def _write_universe(tmp_path, sector_map, stocks):
    dailydir = tmp_path / "daily"
    dailydir.mkdir()
    for code, df in stocks.items():
        df.to_pickle(dailydir / f"{code}.pkl")
    sm = tmp_path / "code2sector.json"
    with open(sm, "w", encoding="gbk") as f:
        json.dump(sector_map, f, ensure_ascii=False)
    return str(dailydir), str(sm)


def test_build_universe_filters_and_truncates(tmp_path):
    sector_map = {"600000": ["半导体"], "600001": ["半导体"], "999999": ["半导体"]}
    long_df = make_daily([10.0 + i * 0.01 for i in range(1300)])
    short_df = make_daily([10.0, 10.1])  # len < 1200 -> 剔除
    stocks = {"600000": long_df, "600001": short_df}  # 999999 无文件
    dailydir, sm = _write_universe(tmp_path, sector_map, stocks)
    universe, codes = bt.build_universe(dailydir, bt.load_sector_map(sm))
    assert codes == ["600000"]
    assert len(universe["600000"]) == 1200
    assert "change_pct" in universe["600000"].columns
    assert universe["600000"]["change_pct"].iloc[0] == 0.0


def test_build_calendar_sorted_union_and_pos():
    d1 = make_daily([10.0, 10.1], start="2026-01-01")          # 01-01, 01-02
    d2 = make_daily([20.0, 20.1, 20.2], start="2026-01-02")    # 01-02, 01-03, 01-04
    all_days, pos_of = bt.build_calendar({"a": d1, "b": d2}, ["a", "b"])
    assert all_days == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    assert pos_of["a"]["2026-01-01"] == 0
    assert pos_of["a"]["2026-01-02"] == 1
    assert pos_of["b"]["2026-01-02"] == 0
    assert pos_of["b"]["2026-01-04"] == 2


def test_build_sector_members_file_order_and_filter():
    sector_map = {"a": ["S2", "S1"], "b": ["S1"], "c": ["S1"]}
    universe = {"a": None, "c": None}  # b 不在 universe -> 剔除
    sm = bt.build_sector_members(sector_map, universe)
    assert sm["S2"] == ["a"]
    assert sm["S1"] == ["a", "c"]


def test_next_returns_exact():
    d = make_daily([100.0, 110.0], opens=[100.0, 105.0])
    r = bt.next_returns(d, 0)
    assert r["gap"] == pytest.approx(105.0 / 100.0 - 1)
    assert r["od"] == pytest.approx(110.0 / 105.0 - 1)
    assert r["close1"] == pytest.approx(110.0 / 100.0 - 1)


def test_next_returns_out_of_range():
    d = make_daily([100.0, 110.0])
    assert bt.next_returns(d, 1) is None


def test_next_returns_nonpositive():
    d = make_daily([0.0, 110.0], opens=[0.0, 105.0])
    assert bt.next_returns(d, 0) is None  # close[T] <= 0
    d2 = make_daily([100.0, 0.0], opens=[100.0, 0.0])
    assert bt.next_returns(d2, 0) is None  # close[T+1] <= 0


def test_win_gain():
    d = make_daily([10.0, 10.5, 11.0, 11.5, 12.0, 13.0], start="2026-01-01")
    all_days, pos_of = bt.build_calendar({"a": d}, ["a"])
    # i=5(w=5): end=all_days[5], start=all_days[1] -> close[5]/close[1]-1
    g = bt._win_gain({"a": d}, pos_of, all_days, "a", 5, 5)
    assert g == pytest.approx(13.0 / 10.5 - 1)


def test_win_gain_missing_day():
    d1 = make_daily([10.0, 11.0], start="2026-01-01")             # 01-01, 01-02
    d2 = make_daily([20.0, 21.0, 22.0, 23.0], start="2026-01-01")  # 01-01..01-04
    universe = {"a": d1, "b": d2}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b"])
    # all_days[2]=01-03,a 无此 bar -> None
    assert bt._win_gain(universe, pos_of, all_days, "a", 2, 5) is None


def test_sector_heat_median_and_min3():
    d = make_daily([10.0 + i * 0.5 for i in range(10)], start="2026-01-01")
    universe = {c: d for c in ["a", "b", "c"]}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b", "c"])
    heat = bt.sector_heat(universe, {"S": ["a", "b", "c"]}, pos_of, all_days, 9)
    assert set(heat) == {"S"}
    expected = d["close"].iloc[9] / d["close"].iloc[5] - 1  # 三只同序列 -> median = 该涨幅
    assert heat["S"] == pytest.approx(expected)


def test_sector_heat_under3_dropped():
    d = make_daily([10.0 + i * 0.5 for i in range(10)], start="2026-01-01")
    universe = {c: d for c in ["a", "b"]}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b"])
    heat = bt.sector_heat(universe, {"S": ["a", "b"]}, pos_of, all_days, 9)
    assert heat == {}


def test_score_at_reconstructs_quote(monkeypatch):
    closes = [10.0 + i * 0.1 for i in range(65)]
    d = make_daily(closes)
    d["change_pct"] = d["close"].pct_change() * 100.0
    d.iloc[0, d.columns.get_loc("change_pct")] = 0.0
    calls = []
    def fake(df, quote, now):
        calls.append((len(df), dict(quote)))
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake)
    i = 63
    s = bt.score_at(d, i, bt.AFTER_CLOSE)
    assert s["composite"] == 40
    assert calls[0][0] == 64                       # df 只含 0..63(<=T)
    assert calls[0][1]["price"] == float(closes[63])
    assert calls[0][1]["amount"] == float(d["volume"].iloc[63]) * float(closes[63])


def test_score_at_short_history_returns_none(monkeypatch):
    d = make_daily([10.0 + i * 0.1 for i in range(60)])  # 60 根 < 61
    d["change_pct"] = 0.0
    assert bt.score_at(d, 59, bt.AFTER_CLOSE) is None


def test_score_at_no_future_leak(monkeypatch):
    closes = [10.0 + i * 0.1 for i in range(65)]
    d = make_daily(closes)
    d["change_pct"] = d["close"].pct_change() * 100.0
    d.iloc[0, d.columns.get_loc("change_pct")] = 0.0
    seen_prices = []
    def fake(df, quote, now):
        seen_prices.append(quote["price"])
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake)
    i = 63
    s1 = bt.score_at(d, i, bt.AFTER_CLOSE)
    # 把 T+1 的 open/close 改成极端正值(保持 >0,使 next_returns 仍有效)
    d.loc[i + 1, "open"] = 1e9
    d.loc[i + 1, "close"] = 1e9
    s2 = bt.score_at(d, i, bt.AFTER_CLOSE)
    assert s2 == s1                                  # 评分逐位不变
    assert seen_prices == [float(closes[63])] * 2     # 两次都只读到 T 的 close
    nr = bt.next_returns(d, i)
    assert nr is not None and nr["gap"] > 1.0         # 验证 next_returns 确实读了 T+1


def _with_change_pct(df, vals):
    df = df.copy()
    df["change_pct"] = [float(v) for v in vals]
    return df


def test_build_buyable_filters():
    d1 = _with_change_pct(make_daily([10.0, 11.0, 12.0]), [0.0, 10.0, 0.0])  # 600000 bar1 涨停(>=9.9)
    d2 = _with_change_pct(make_daily([10.0, 11.0, 12.0], volumes=[1000] * 3), [0.0, 1.0, 0.0])  # 量能不足
    d3 = _with_change_pct(make_daily([1000.0, 1001.0, 1002.0], volumes=[200000] * 3), [0.0, 1.0, 0.0])  # 合格
    universe = {"600000": d1, "000001": d2, "600519": d3}
    codes = ["600000", "000001", "600519"]
    all_days, pos_of = bt.build_calendar(universe, codes)
    buy, od_m, gap_m, c1_m = bt.build_buyable(universe, pos_of, all_days, 1)
    assert buy == {"600519"}
    assert set(od_m) == set(gap_m) == set(c1_m) == {"600519"}


def test_select_baskets_exact():
    sector_members = {"S1": ["a", "b", "c", "d", "e", "f", "g"], "S2": ["h", "i"], "S3": ["j"]}
    buyable = {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j"}
    hot = ["S1", "S2", "S3"]
    scores = {
        "a": {"composite": 80, "risk": 10, "position": 30},
        "b": {"composite": 70, "risk": 10, "position": 80},
        "c": {"composite": 65, "risk": 10, "position": 50},
        "d": {"composite": 55, "risk": 10, "position": 20},
        "e": {"composite": 50, "risk": 80, "position": 90},  # risk>=70 -> 排除
        "f": {"composite": 40, "risk": 10, "position": 10},
        "g": {"composite": 30, "risk": 10, "position": 70},
        "h": {"composite": 75, "risk": 10, "position": 40},
        "i": {"composite": 60, "risk": 10, "position": 60},
        "j": {"composite": 90, "risk": 10, "position": 25},
    }
    def get_score(code, bar):
        return scores[code]
    all_days = ["2026-01-01"]
    dt = all_days[0]
    pos_of = {c: {dt: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    assert b["A"] == ["a", "b", "c", "d", "f", "h", "i", "j"]
    assert b["E_hi"] == ["b", "g", "c", "a", "d", "i", "h", "j"]
    assert b["E_lo"] == ["g", "c", "a", "d", "f", "i", "h", "j"]
    assert b["B"] == ["j", "a", "h", "b", "c", "i", "d", "f", "g"]  # (0-0)%10==0 采样
    assert b["C"] == ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]  # 10<=15 不抽样
    assert set(b["D"]) == buyable


def test_select_baskets_B_not_sampled():
    sector_members = {"S1": ["a", "b", "c"]}
    buyable = {"a", "b", "c"}
    hot = ["S1"]
    def get_score(code, bar):
        return {"composite": 60, "risk": 10, "position": 50}
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 3, np.random.default_rng(0))
    assert b["B"] is None  # (0-3)%10 != 0 -> 未采样


def test_select_baskets_E_position_tie_breaks_by_fa():
    sector_members = {"S1": ["x", "y"]}
    buyable = {"x", "y"}
    hot = ["S1"]
    scores = {
        "x": {"composite": 70, "risk": 10, "position": 50},
        "y": {"composite": 80, "risk": 10, "position": 50},  # 同 position,fa 更高
    }
    def get_score(code, bar):
        return scores[code]
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    # 先 -fa 排序(y 前),稳定 -position 重排,并列按 fa 序 -> y 仍在前
    assert b["E_hi"] == ["y", "x"]


def test_select_baskets_C_rng_sampling_deterministic():
    members = [f"c{i:02d}" for i in range(20)]
    sector_members = {"S1": members}
    buyable = set(members)
    hot = ["S1"]
    def get_score(code, bar):
        return {"composite": 50, "risk": 10, "position": 50}
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in members}
    b1 = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    b2 = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    assert len(b1["C"]) == 15
    assert b1["C"] == b2["C"]
    assert set(b1["C"]) <= buyable


def test_stats_mean_and_empty():
    m = {"a": 0.1, "b": 0.2, "c": 0.3}
    assert bt.stats(["a", "b", "c"], m) == pytest.approx(0.2)
    assert bt.stats(["a", "zz"], m) == pytest.approx(0.1)
    assert np.isnan(bt.stats([], m))


def test_summ():
    arr = [0.01, 0.02, -0.01, 0.03, 0.0]
    m, w, n = bt.summ(arr)
    assert m == pytest.approx((0.01 + 0.02 - 0.01 + 0.03 + 0.0) / 5 * 100)
    assert w == pytest.approx(60.0)  # 0.01/0.02/0.03 正 -> 3/5
    assert n == 5


def test_summ_nan_and_none_filtered():
    m, w, n = bt.summ([0.01, float("nan"), None, -0.02])
    assert n == 2
    assert m == pytest.approx((0.01 - 0.02) / 2 * 100)


def test_summ_empty():
    m, w, n = bt.summ([])
    assert np.isnan(m) and np.isnan(w) and n == 0


def test_summ_year_groups():
    out = bt.summ_year([0.01, 0.02, 0.03], ["2023", "2023", "2024"])
    assert out[0][0] == "2023" and out[0][3] == 2
    assert out[1][0] == "2024" and out[1][3] == 1


def test_welch_t_formula():
    a = [1.0 + 0.1 * i for i in range(10)]
    b = [5.0 + 0.1 * i for i in range(10)]
    r = bt.welch_t(a, b)
    m1, m2 = np.mean(a), np.mean(b)
    v1, v2 = np.var(a, ddof=1), np.var(b, ddof=1)
    n1 = n2 = 10
    expected_t = (m1 - m2) / ((v1 / n1 + v2 / n2) ** 0.5)
    assert r["t"] == pytest.approx(expected_t)
    assert 0.0 <= r["p"] <= 1.0
    assert r["p"] < 0.001  # 差异极大 -> 极显著


def test_welch_t_identical():
    a = [1.0, 1.1, 1.2, 1.3, 1.4]
    r = bt.welch_t(a, a)
    assert r["t"] == 0.0 and r["p"] == 1.0


def test_welch_t_too_small():
    r = bt.welch_t([1.0], [2.0])
    assert r["t"] is None and r["p"] is None
