# -*- coding: utf-8 -*-
"""低位透视 collect_low_position 单测(2026-09-17)。

Phase 0 预注册检验判负后,本模块定位为【透视工具】:不预测方向,只把 position 因子
背后的低位股显性化。测试重点:
  1. 把 position 的语义钉死(高 position 分 = 低 pos60 = 低位),防语义再被搞反;
  2. 契约/排序/去重/截断;
  3. composite/verdict/tier 为个股口径(未含板块共振)。
"""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from core import analysis as an
from core import recommend as rc


def _spot(rows):
    cols = ["code", "name", "price", "change_pct", "volume", "amount", "open", "high", "low"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


def _daily_trend(start, end, n=70):
    """线性走势 n 根。end<start → 收在 60 日区间底部(低位);end>start → 顶部(高位)。"""
    closes = np.linspace(start, end, n)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n),
        "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
        "volume": [1_000_000] * n, "amount": [1e8] * n,
        "outstanding_share": [1e9] * n, "turnover": [0.05] * n,
    })


def dt_now():
    return datetime(2026, 8, 14, 15, 0)


# ---- 语义钉死:position 分是 pos60 的减函数 ----

def test_position_score_is_decreasing_in_pos60():
    """低位股(收在60日区间底部)必须比高位股拿到【更高】的 position 分。

    这是本模块最容易搞反的地方:compute_position_score 里 pos_factor = 100*(1−pos60),
    权重 0.5。若此测试失败,说明语义被改反,collect_low_position 会选出一堆高位超买股。
    """
    low_pos = _daily_trend(20.0, 10.0)      # 收在区间底部
    high_pos = _daily_trend(10.0, 20.0)     # 收在区间顶部
    s_low = an.score_stock(low_pos, {"price": 10.0, "change_pct": 0.0, "volume": 1e6,
                                     "amount": 1e8, "open": 10.0, "high": 10.1, "low": 9.9},
                           dt_now())
    s_high = an.score_stock(high_pos, {"price": 20.0, "change_pct": 0.0, "volume": 1e6,
                                       "amount": 1e8, "open": 20.0, "high": 20.2, "low": 19.8},
                            dt_now())
    assert s_low["pos60"] < 0.15 < 0.85 < s_high["pos60"]
    assert s_low["position"] > s_high["position"]


# ---- _dd60 ----

def test_dd60_non_positive_and_missing():
    # 构造里 high = close*1.01,收盘恒 ≤ 最高价,故 dd60 永远 ≤ 0(收在最高也约 −1%)
    d = _daily_trend(10.0, 20.0)
    assert rc._dd60(d) == pytest.approx(-0.99, abs=0.05)
    d2 = _daily_trend(20.0, 10.0)            # 收在最低 → 回撤显著为负
    assert rc._dd60(d2) < -40.0
    assert rc._dd60(d.iloc[0:0]) is None      # 空帧 → None


# ---- collect_low_position ----

def _collect(spot_rows, daily_map, top_n=60, pool_n=300, resolve=None):
    def get_daily(code):
        return daily_map[code], False

    def _resolve(code):
        return ["板块X"]

    payload, stale = rc.collect_low_position(
        _spot(spot_rows), get_daily, dt_now(), None,
        exclude_codes=set(), resolve_sectors_fn=resolve or _resolve,
        top_n=top_n, pool_n=pool_n)
    return payload, stale


def _rows_simple():
    return [
        ("600001", "低位股", 10.0, -1.0, 1e6, 3e8, 9.9, 10.1, 9.8),
        ("600002", "高位股", 20.0, 1.0, 1e6, 3e8, 19.9, 20.1, 19.8),
    ]


def _daily_map_simple():
    return {"600001": _daily_trend(20.0, 10.0), "600002": _daily_trend(10.0, 20.0)}


def test_collect_low_position_contract_and_sort():
    payload, stale = _collect(_rows_simple(), _daily_map_simple())
    assert stale is False
    assert payload["total"] == 2
    # 按 position 降序 → 低位股在前
    codes = [it["code"] for it in payload["items"]]
    assert codes == ["sh600001", "sh600002"]
    item = payload["items"][0]
    for key in ("code", "name", "price", "change_pct", "amount", "position", "pos60",
                "dd60_pct", "bias_pct", "risk", "composite", "verdict", "tier", "sector_name"):
        assert key in item, key
    assert item["sector_name"] == "板块X"
    assert item["position"] > payload["items"][1]["position"]
    assert item["pos60"] < payload["items"][1]["pos60"]
    assert payload["basis"].startswith("个股因子口径")


def test_collect_low_position_tier_matches_verdict():
    """tier 必须与 tier_for_verdict(verdict) 一致(个股口径)。"""
    payload, _ = _collect(_rows_simple(), _daily_map_simple())
    for it in payload["items"]:
        assert it["tier"] == rc.tier_for_verdict(it["verdict"])
        if it["composite"] is not None:
            assert it["verdict"] == an.stock_verdict(it["composite"])


def test_collect_low_position_excludes_illiquid_and_st():
    rows = _rows_simple() + [
        ("600003", "ST警示", 5.0, 1.0, 1e6, 5e8, 4.9, 5.1, 4.8),      # ST
        ("600004", "低流动性", 8.0, 1.0, 1e6, 5e7, 7.9, 8.1, 7.8),     # amount < 1e8
    ]
    dm = _daily_map_simple()
    dm["600003"] = _daily_trend(20.0, 10.0)
    dm["600004"] = _daily_trend(20.0, 10.0)
    payload, _ = _collect(rows, dm)
    assert {it["code"] for it in payload["items"]} == {"sh600001", "sh600002"}


def test_collect_low_position_dedup_and_top_n():
    rows = [
        ("600001", "甲", 10.0, -1.0, 1e6, 3e8, 9.9, 10.1, 9.8),
        ("600001", "甲", 10.0, -1.0, 1e6, 3e8, 9.9, 10.1, 9.8),        # 重复
        ("600002", "乙", 20.0, 1.0, 1e6, 3e8, 19.9, 20.1, 19.8),
    ]
    dm = _daily_map_simple()
    payload, _ = _collect(rows, dm, top_n=60)
    codes = [it["code"] for it in payload["items"]]
    assert len(codes) == len(set(codes)) == 2
    payload1, _ = _collect(rows, dm, top_n=1)
    assert len(payload1["items"]) == 1
    assert payload1["items"][0]["code"] == "sh600001"


def test_collect_low_position_short_history_skipped():
    """日线不足 61 根 → score_stock 返回 position=None → 该股被跳过,不污染低位档。"""
    short = _daily_trend(20.0, 10.0, n=40)
    dm = {"600001": short, "600002": _daily_trend(10.0, 20.0)}
    payload, _ = _collect(_rows_simple(), dm)
    assert [it["code"] for it in payload["items"]] == ["sh600002"]


def test_collect_low_position_daily_failure_isolated():
    """单只日线抛异常 → 跳过该股,其余继续,并计入 diagnostics。"""
    def get_daily(code):
        if code == "600001":
            raise RuntimeError("boom")
        return _daily_trend(10.0, 20.0), False

    payload, _ = rc.collect_low_position(
        _spot(_rows_simple()), get_daily, dt_now(), None,
        exclude_codes=set(), resolve_sectors_fn=lambda c: ["板块X"])
    assert [it["code"] for it in payload["items"]] == ["sh600002"]
    assert payload["diagnostics"]["stocks_daily_failed"] == 1
