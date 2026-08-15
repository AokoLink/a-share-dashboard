# -*- coding: utf-8 -*-
"""波段候选(方向中性可操作性筛选)单测(设计 2026-08-16 §1)。"""
from datetime import datetime

import pandas as pd
import pytest

from core import recommend as rc


def _spot(rows):
    cols = ["code", "name", "price", "change_pct", "volume", "amount", "open", "high", "low"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


def _daily70(turnover_frac):
    # 平盘 70 根:pos60=(10−9.9)/(10.1−9.9)=0.5(中位,不触发 0.85 高位硬砍)
    n = 70
    closes = [10.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n),
        "open": closes, "high": [10.1] * n,
        "low": [9.9] * n, "close": closes,
        "volume": [1_000_000] * n, "amount": [1e8] * n,
        "outstanding_share": [1e9] * n, "turnover": [turnover_frac] * n,
    })


def dt_now():
    return datetime(2026, 8, 14, 15, 0)


# ---- swing_pool ----

def test_swing_pool_filters_and_sorts():
    df = _spot([
        ("600001", "正常A", 10.0, 1.0, 1e6, 3e8, 9.9, 10.1, 9.8),
        ("600002", "ST警示", 5.0, 1.0, 1e6, 5e8, 4.9, 5.1, 4.8),      # ST 排除
        ("600003", "停牌股", 0.0, 0.0, 0, 2e8, 0.0, 0.0, 0.0),        # 停牌(price/vol 0)
        ("600004", "涨停股", 11.0, 10.0, 1e6, 6e8, 10.0, 11.0, 10.0),  # 涨停(≥9.9)排除
        ("600005", "新股", 20.0, 1.0, 1e6, 4e8, 19.9, 20.1, 19.8),    # 新股排除
        ("600006", "低流动性", 8.0, 1.0, 1e6, 5e7, 7.9, 8.1, 7.8),     # amount<1e8 排除
    ])
    out = rc.swing_pool(df, {"600005"}, 1e8, 200)
    assert [x["code"] for x in out] == ["600001"]


def test_swing_pool_amount_desc():
    df = _spot([
        ("600001", "A", 10.0, 1.0, 1e6, 1e8, 9.9, 10.1, 9.8),
        ("600007", "B", 10.0, 1.0, 1e6, 9e8, 9.9, 10.1, 9.8),
    ])
    out = rc.swing_pool(df, set(), 1e8, 200)
    assert [x["code"] for x in out] == ["600007", "600001"]


def test_swing_pool_top_n():
    rows = [("60000%d" % i, "N%d" % i, 10.0, 1.0, 1e6, (1 + i) * 1e8, 9.9, 10.1, 9.8) for i in range(3)]
    out = rc.swing_pool(_spot(rows), set(), 1e8, 2)
    assert len(out) == 2
    assert [x["code"] for x in out] == ["600002", "600001"]  # 成交额 3e8 > 2e8


# ---- swing_eligible ----

def test_swing_eligible_branches():
    assert rc.swing_eligible(0.03, 30, 0.5) == (True, "")
    assert rc.swing_eligible(None, 30, 0.5) == (False, "换手不足")
    assert rc.swing_eligible(0.01, 30, 0.5) == (False, "换手不足")
    assert rc.swing_eligible(0.03, 60, 0.5) == (False, "风险尾部")
    assert rc.swing_eligible(0.03, 30, 0.85) == (False, "高位")


def test_swing_eligible_fail_open():
    # risk/pos60 None(历史不足)→ fail-open,不剔除
    assert rc.swing_eligible(0.03, None, None) == (True, "")
    assert rc.swing_eligible(0.03, 59, 0.84) == (True, "")


# ---- collect_swing_candidates ----

_REGIME = {"label": "震荡", "action": "hold", "message": "持有(中性,启发式,未回测)"}


def _collect(spot_rows, turnover_map, top_n=60, pool_n=200):
    def get_daily(code):
        return _daily70(turnover_map[code]), False

    def resolve(code):
        return ["板块X"]

    payload, stale = rc.collect_swing_candidates(
        _spot(spot_rows), get_daily, dt_now(), _REGIME,
        exclude_codes=set(), resolve_sectors_fn=resolve, top_n=top_n, pool_n=pool_n)
    return payload, stale


def test_collect_swing_candidates_contract_and_sort():
    spot = [
        ("600001", "甲", 10.0, 1.0, 1e6, 2e8, 9.9, 10.1, 9.8),   # turnover 0.05
        ("600002", "乙", 12.0, 1.0, 1e6, 3e8, 11.9, 12.1, 11.8), # turnover 0.10
        ("600003", "丙", 9.0, 1.0, 1e6, 4e8, 8.9, 9.1, 8.8),     # turnover 0.01 → 换手不足
    ]
    payload, stale = _collect(spot, {"600001": 0.05, "600002": 0.10, "600003": 0.01}, top_n=60)
    assert stale is False
    assert payload["regime"] is _REGIME
    assert payload["total"] == 2          # 丙被换手不足剔除
    codes = [it["code"] for it in payload["items"]]
    assert codes == ["sh600002", "sh600001"]   # 换手率降序 0.10 > 0.05
    item = payload["items"][0]
    for key in ("code", "name", "price", "change_pct", "amount",
                "turnover_pct", "amp20", "risk", "pos60", "sector_name"):
        assert key in item
    assert item["turnover_pct"] == pytest.approx(10.0)
    assert item["sector_name"] == "板块X"
    # 方向中性:无 composite/verdict/position
    for key in ("composite", "verdict", "position", "_turnover_frac"):
        assert key not in item


def test_collect_swing_candidates_top_n_truncate():
    spot = [
        ("600001", "甲", 10.0, 1.0, 1e6, 2e8, 9.9, 10.1, 9.8),
        ("600002", "乙", 12.0, 1.0, 1e6, 3e8, 11.9, 12.1, 11.8),
    ]
    payload, _ = _collect(spot, {"600001": 0.05, "600002": 0.10}, top_n=1)
    assert len(payload["items"]) == 1
    assert payload["items"][0]["code"] == "sh600002"


def test_collect_swing_candidates_dedup():
    # spot 含重复 code → 去重后只保留一份
    spot = [
        ("600001", "甲", 10.0, 1.0, 1e6, 2e8, 9.9, 10.1, 9.8),
        ("600001", "甲", 10.0, 1.0, 1e6, 2e8, 9.9, 10.1, 9.8),
    ]
    payload, _ = _collect(spot, {"600001": 0.05}, top_n=60)
    codes = [it["code"] for it in payload["items"]]
    assert len(codes) == len(set(codes)) == 1
