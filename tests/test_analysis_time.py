# tests/test_analysis_time.py
# -*- coding: utf-8 -*-
from datetime import datetime

import pandas as pd
import pytest

import analysis as an


def dt(h, m, weekday=0):
    d = datetime(2026, 8, 10, h, m)  # 2026-08-10 为周一
    while d.weekday() != weekday:
        d = datetime(2026, 8, 10 + (weekday - d.weekday()) % 7, h, m)
    return d


def test_trading_minutes_elapsed():
    assert an.trading_minutes_elapsed(dt(9, 20)) == 0            # 开盘前
    assert an.trading_minutes_elapsed(dt(9, 45)) == 15
    assert an.trading_minutes_elapsed(dt(10, 30)) == 60
    assert an.trading_minutes_elapsed(dt(11, 30)) == 120         # 午休起点
    assert an.trading_minutes_elapsed(dt(13, 0)) == 120          # 午休不计入
    assert an.trading_minutes_elapsed(dt(14, 0)) == 180          # 关键:非270
    assert an.trading_minutes_elapsed(dt(15, 0)) == 240          # 收盘


def test_is_after_close_and_trading_time():
    assert an.is_after_close(dt(14, 59)) is False
    assert an.is_after_close(dt(15, 0)) is True
    assert an.is_trading_time(dt(10, 0, weekday=0)) is True      # 周一盘中
    assert an.is_trading_time(dt(12, 0, weekday=0)) is True      # 午休仍算交易日
    assert an.is_trading_time(dt(9, 0, weekday=0)) is False      # 开盘前
    assert an.is_trading_time(dt(10, 0, weekday=5)) is False     # 周六


def test_day_adjusted_volume_and_ratio():
    assert an.day_adjusted_volume(100000, 60) == pytest.approx(400000)   # 100000*240/60
    assert an.day_adjusted_volume(100000, 14) is None                    # <15min 下限
    assert an.day_adjusted_volume(100000, 0) is None
    assert an.custom_volume_ratio(100000, 60, 200000) == pytest.approx(2.0)
    assert an.custom_volume_ratio(100000, 14, 200000) is None            # 开盘尖峰不计算
    assert an.custom_volume_ratio(100000, 60, 0) is None


def test_filter_active_and_limit_threshold():
    df = pd.DataFrame({"code": ["600000", "688981", "830000"],
                       "price": [10.0, 0.0, 5.0], "volume": [100, 200, 0]})
    act = an.filter_active(df)
    assert list(act["code"]) == ["600000"]
    assert an.limit_threshold("600000") == 9.9
    assert an.limit_threshold("688981") == 19.9
    assert an.limit_threshold("300750") == 19.9
    assert an.limit_threshold("830000") == 29.9
    assert an.limit_threshold("400001") == 29.9


def test_compute_breadth():
    df = pd.DataFrame({"code": ["600000", "600519", "000001", "688981", "830000", "300750"],
                       "price": [10, 20, 30, 40, 50, 60],
                       "volume": [1, 1, 1, 1, 1, 0],           # 300750 停牌
                       "change_pct": [10.0, -10.0, 0.0, 5.0, 30.0, 5.0],  # 688981 +5% 未触板(19.9)
                       "amount": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]})
    b = an.compute_breadth(df)
    assert b == {"up": 3, "down": 1, "flat": 1, "limit_up": 2, "limit_down": 1,
                 "total_turnover": pytest.approx(1500.0)}
    # 新股排除:830000 是北交所新股 → 剔除后 limit_up 少一个
    b2 = an.compute_breadth(df, exclude_codes={"830000"})
    assert b2["limit_up"] == 1


def test_ma_and_macd_columns():
    df = pd.DataFrame({"close": [float(i) for i in range(1, 65)]})
    out = an.add_ma(df)
    assert list(out.columns) == ["close", "ma5", "ma10", "ma20", "ma60"]
    assert out["ma20"].iloc[-1] == pytest.approx(54.5)          # (45..64 均值)
    macd = an.add_macd(df)
    assert "dif" in macd.columns and "dea" in macd.columns and "macd" in macd.columns
    assert not macd["dif"].isna().iloc[-1]


def test_weighted_renormalize():
    assert an._weighted([(50.0, 40), (80.0, 20), (None, 25), (100.0, 15)]) == pytest.approx(
        (50 * 40 + 80 * 20 + 100 * 15) / 75)                    # 缺失 25 权重被归一化
    assert an._weighted([(None, 40), (None, 20)]) is None
    assert an._weighted([]) is None
    assert an._weighted([(float("nan"), 40), (80.0, 60)]) == pytest.approx(80.0)  # NaN 视为缺失
