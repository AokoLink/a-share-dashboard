# -*- coding: utf-8 -*-
"""历史盲测基线模块(生产级固化 nxday_backtest.py 核心次日盲测路径)。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
严格禁止未来数据泄露:评分输入 <= T,验证只读 T+1。
"""
import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import analysis as an

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
PER_SECTOR = 5
EVAL_DAYS = 1200
B_SAMPLE_EVERY = 10
MIN_AMOUNT = 1e8
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)

RNG = np.random.default_rng(0)   # 模块级单实例,评估日按循环顺序复用(spec §5.6 C)


def load_daily(data_dir, code):
    path = os.path.join(data_dir, f"{code}.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing daily pkl: {path}")
    return pd.read_pickle(path)


def load_sector_map(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing sector map: {path}")
    with open(path, "r", encoding="gbk") as f:
        return json.load(f)


def build_universe(data_dir, sector_map):
    if isinstance(sector_map, str):
        sector_map = load_sector_map(sector_map)
    universe, codes = {}, []
    files = sorted(f for f in os.listdir(data_dir) if f.endswith(".pkl"))
    for fn in files:
        code = fn[:-4]
        if code not in sector_map:
            continue
        d = load_daily(data_dir, code)
        if len(d) < 1200:
            continue
        d = d.reset_index(drop=True).tail(1200).copy()
        d["code"] = code
        d["change_pct"] = d["close"].pct_change() * 100.0
        d.iloc[0, d.columns.get_loc("change_pct")] = 0.0
        universe[code] = d
        codes.append(code)
    return universe, codes


def build_calendar(universe, codes):
    all_days = sorted(set().union(*[set(universe[c]["date"].values) for c in codes]))
    pos_of = {c: {dt: int(idx) for idx, dt in enumerate(universe[c]["date"].values)} for c in codes}
    return all_days, pos_of


def build_sector_members(sector_map, universe):
    sector_members = {}
    for c, secs in sector_map.items():
        if c in universe:
            for s in secs:
                sector_members.setdefault(s, []).append(c)
    return sector_members
