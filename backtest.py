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


def next_returns(d, i):
    if i + 1 >= len(d):
        return None
    c0 = float(d["close"].iloc[i]); o1 = float(d["open"].iloc[i + 1]); c1 = float(d["close"].iloc[i + 1])
    if c0 <= 0 or o1 <= 0 or c1 <= 0:
        return None
    return {"gap": o1 / c0 - 1, "close1": c1 / c0 - 1, "od": c1 / o1 - 1}


def _win_gain(universe, pos_of, all_days, code, i, w):
    j_hi = pos_of[code].get(all_days[i])
    if j_hi is None:
        return None
    j_lo = pos_of[code].get(all_days[max(0, i - (w - 1))])
    if j_lo is None or j_hi <= j_lo:
        return None
    c0 = float(universe[code]["close"].iloc[j_lo])
    c1 = float(universe[code]["close"].iloc[j_hi])
    if c0 <= 0:
        return None
    return c1 / c0 - 1


def sector_heat(universe, sector_members, pos_of, all_days, i):
    heat = {}
    for s, members in sector_members.items():
        gains = {}
        for c in members:
            g = _win_gain(universe, pos_of, all_days, c, i, 5)
            if g is not None:
                gains[c] = g
        if len(gains) >= 3:
            heat[s] = float(np.median(list(gains.values())))
    return heat


def score_at(d, i, now):
    df = d.iloc[: i + 1]
    if len(df) < 61:
        return None
    quote = {"price": float(df["close"].iloc[-1]),
             "change_pct": float(df["change_pct"].iloc[-1]),
             "volume": float(df["volume"].iloc[-1]),
             "amount": float(df["volume"].iloc[-1]) * float(df["close"].iloc[-1])}
    sc = an.score_stock(df, quote, now)
    return sc if sc["composite"] is not None else None


def build_buyable(universe, pos_of, all_days, i):
    dt = all_days[i]
    buy = set()
    od_m, gap_m, c1_m = {}, {}, {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None or bar + 1 >= len(universe[c]):
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        nr = next_returns(d, bar)
        if nr is None:
            continue
        th = an.limit_threshold(c)
        if chg >= th or chg <= -7.0:
            continue
        if float(d["volume"].iloc[bar]) * close < MIN_AMOUNT:
            continue
        buy.add(c)
        od_m[c] = nr["od"]; gap_m[c] = nr["gap"]; c1_m[c] = nr["close1"]
    return buy, od_m, gap_m, c1_m


def select_baskets(sector_members, pos_of, all_days, i, buyable, hot, get_score, start, rng):
    dt = all_days[i]
    hot_members = set()
    for s in hot:
        hot_members.update(sector_members.get(s, []))
    basket_a, pos_hi, pos_lo = [], [], []
    for s in hot:
        scored = []
        for c in sector_members.get(s, []):
            if c not in buyable:
                continue
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            bonus = 8 if sc["composite"] >= 68 else 4 if sc["composite"] >= 60 else 0
            fa = sc["composite"] + bonus
            scored.append((fa, c, sc["position"]))
        scored.sort(key=lambda x: -x[0])          # 先 -fa(spec §5.6 E 两段式)
        basket_a += [x[1] for x in scored[:PER_SECTOR]]
        scored.sort(key=lambda x: -x[2])          # 稳定重排 -position,并列按 fa 序
        pos_hi += [x[1] for x in scored[:PER_SECTOR]]
        pos_lo += [x[1] for x in scored[-PER_SECTOR:]]
    basket_b = None
    if (i - start) % B_SAMPLE_EVERY == 0:
        scored_b = []
        for c in sorted(buyable):
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            scored_b.append((sc["composite"], c))
        scored_b.sort(key=lambda x: -x[0])
        basket_b = [c for _, c in scored_b[:TOP_SECTORS * PER_SECTOR]]
    basket_c = sorted(hot_members & buyable)
    if len(basket_c) > TOP_SECTORS * PER_SECTOR:
        basket_c = list(rng.choice(basket_c, size=TOP_SECTORS * PER_SECTOR, replace=False))
    basket_d = list(buyable)
    return {"A": basket_a, "E_hi": pos_hi, "E_lo": pos_lo, "B": basket_b, "C": basket_c, "D": basket_d}


def stats(basket, m):
    return float(np.mean([m[c] for c in basket if c in m])) if basket else np.nan


def summ(arr):
    a = np.array([x for x in arr if x == x and x is not None])
    if len(a):
        return (round(float(np.nanmean(a) * 100), 3),
                round(float(np.mean(a > 0) * 100), 1),
                int(len(a)))
    return (np.nan, np.nan, 0)


def summ_year(arr, years):
    groups = {}
    for x, y in zip(arr, years):
        if x is None or x != x:
            continue
        groups.setdefault(y, []).append(x)
    out = []
    for y in sorted(groups):
        a = np.array(groups[y])
        if len(a):
            out.append((y, round(float(np.nanmean(a) * 100), 3),
                        round(float(np.mean(a > 0) * 100), 1), int(len(a))))
    return out


def _betacf(a, b, x):
    """不完全 beta 的连分式展开(Numerical Recipes 6.4)。"""
    MAXIT = 200
    EPS = 3.0e-14
    FPMIN = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a, b, x):
    """正则化不完全 beta I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t, df):
    """Student's t CDF(t >= 0)。"""
    x = df / (df + t * t)
    return 1.0 - 0.5 * _betainc(df / 2.0, 0.5, x)


def welch_t(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return {"t": None, "p": None}
    m1, m2 = a.mean(), b.mean()
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se = (v1 / n1 + v2 / n2) ** 0.5
    if se == 0.0:
        return {"t": 0.0, "p": 1.0}
    t = (m1 - m2) / se
    df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * (1.0 - _t_cdf(abs(t), df))
    return {"t": float(t), "p": float(p)}
