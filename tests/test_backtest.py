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
