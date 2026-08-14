# -*- coding: utf-8 -*-
"""预测引擎(多维结构化预测):在评分之上叠校准概率层。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
严格禁止未来数据泄露:预测输入 <= T,验证只读 > T 的真实数据。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import analysis as an
import backtest as bt

MODULE_VERSION = "1.0.0"
EVAL_DAYS = 1200
TRAIN_FRAC = 0.8
N_BINS = 10
DIRECTION_BAND = 0.05
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)


def fwd_close(d, i, k):
    """close[i+k]/close[i]-1;越界或非正价返回 None。"""
    if i + k >= len(d):
        return None
    c0 = float(d["close"].iloc[i])
    ck = float(d["close"].iloc[i + k])
    if c0 <= 0 or ck <= 0:
        return None
    return ck / c0 - 1


def _labels(d, i):
    """四 horizon 二分类标签 {close1, gap, od, trend3};next_returns None 则整体 None。"""
    nr = bt.next_returns(d, i)
    if nr is None:
        return None
    out = {
        "close1": 1 if nr["close1"] > 0 else 0,
        "gap": 1 if nr["gap"] > 0 else 0,
        "od": 1 if nr["od"] > 0 else 0,
        "trend3": None,
    }
    f3 = fwd_close(d, i, 3)
    if f3 is not None:
        out["trend3"] = 1 if f3 > 0 else 0
    return out


class Calibrator:
    """composite → 单调经验概率。bins = [(upper, p), ...],upper 升序,末箱 upper=inf。"""

    def __init__(self, bins, degraded=False):
        self.bins = bins
        self.degraded = degraded

    def p_up(self, composite):
        if composite is None:
            return None
        try:
            f = float(composite)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        for upper, p in self.bins:
            if f <= upper:
                return p
        return self.bins[-1][1]


def _fit_calibrator(pairs, n_bins):
    """等量分箱 + PAV 单调池化。n < n_bins 降为单箱(degraded)。"""
    clean = []
    for c, l in pairs:
        try:
            fc, fl = float(c), float(l)
        except (TypeError, ValueError):
            continue
        if fc != fc or fl != fl:
            continue
        clean.append((fc, fl))
    if len(clean) < n_bins:
        if not clean:
            return Calibrator([(float("inf"), 0.5)], degraded=True)
        p = sum(l for _, l in clean) / len(clean)
        return Calibrator([(float("inf"), p)], degraded=True)
    clean.sort(key=lambda x: x[0])
    n = len(clean)
    bins = []
    for b in range(n_bins):
        lo = b * n // n_bins
        hi = (b + 1) * n // n_bins
        chunk = clean[lo:hi]
        p = sum(l for _, l in chunk) / len(chunk)
        if hi < n:
            upper = (chunk[-1][0] + clean[hi][0]) / 2.0
        else:
            upper = chunk[-1][0]
        bins.append([upper, p, len(chunk)])
    while True:
        merged = False
        out = []
        i = 0
        while i < len(bins):
            if i + 1 < len(bins) and bins[i][1] > bins[i + 1][1]:
                n_m = bins[i][2] + bins[i + 1][2]
                p_m = (bins[i][1] * bins[i][2] + bins[i + 1][1] * bins[i + 1][2]) / n_m
                out.append([bins[i + 1][0], p_m, n_m])
                i += 2
                merged = True
            else:
                out.append(bins[i])
                i += 1
        bins = out
        if not merged:
            break
    finite = [(u, p) for u, p, _ in bins]
    finite[-1] = (float("inf"), finite[-1][1])
    return Calibrator(finite, degraded=False)


def forward_universe(universe, pos_of, all_days):
    """前向宇宙 {code: bar}:as_of_date 当日可交易股,不要求 T+1 bar 存在。"""
    dt = all_days[-1]
    out = {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        th = an.limit_threshold(c)
        if chg >= th or chg <= -7.0:
            continue
        if float(d["volume"].iloc[bar]) * close < bt.MIN_AMOUNT:
            continue
        out[c] = bar
    return out


def _dir_conf(p):
    """二分类校准概率 → (direction, confidence);direction ∈ {up, down, hold}。"""
    if p is None:
        return None, None
    if p > 0.5 + DIRECTION_BAND:
        return "up", p
    if p < 0.5 - DIRECTION_BAND:
        return "down", 1.0 - p
    return "hold", max(p, 1.0 - p)


def _gap_dir(p):
    d, _ = _dir_conf(p)
    if d == "up":
        return "high"
    if d == "down":
        return "low"
    return "hold"


def _path(gap_dir, od_dir):
    """gap×od → 四分类路径;任一方观望返回 None。"""
    if gap_dir == "hold" or od_dir == "hold":
        return None
    if gap_dir == "high" and od_dir == "up":
        return "高开高走"
    if gap_dir == "high" and od_dir == "down":
        return "高开低走"
    if gap_dir == "low" and od_dir == "up":
        return "低开高走"
    return "低开低走"


def predict_at(d, i, now, cals):
    """单股 ≤T → 结构化预测 dict;composite 不可得返回 None。"""
    sc = bt.score_at(d, i, now)
    if sc is None:
        return None
    composite = float(sc["composite"])
    p_dir = cals["direction"].p_up(composite)
    p_gap = cals["gap"].p_up(composite)
    p_od = cals["od"].p_up(composite)
    p_t3 = cals["trend3"].p_up(composite)
    direction, confidence = _dir_conf(p_dir)
    gap_dir = _gap_dir(p_gap)
    od_dir, _ = _dir_conf(p_od)
    t3_dir, t3_conf = _dir_conf(p_t3)
    return {
        "code": str(d["code"].iloc[i]),
        "date": str(d["date"].iloc[i]),
        "composite": composite,
        "T+1": {
            "direction": direction,
            "confidence": confidence,
            "gap": gap_dir,
            "od": od_dir,
            "path": _path(gap_dir, od_dir),
        },
        "T+3": {"direction": t3_dir, "confidence": t3_conf},
    }
