# -*- coding: utf-8 -*-
"""快照对比器:冻结前向快照 vs 真实结果,严格时间隔离。

只读 _analysis/daily/*.pkl、_analysis/code2sector.json(GBK)、冻结快照 JSON,不 fetch。
验证只读 > as_of_date 的真实行情(锚点 close <= as_of_date 作 T+1 分母)。
预测字段是快照里冻结的,绝不重跑 score_at/校准器。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

import backtest as bt
import predict as pr

MODULE_VERSION = "1.0.0"


def _path_label(gap_up, od_up):
    """gap x od -> 四分类路径(镜像 predict._path_label,自实现不引私有)。"""
    if gap_up and od_up:
        return "高开高走"
    if gap_up and not od_up:
        return "高开低走"
    if not gap_up and od_up:
        return "低开高走"
    return "低开低走"


def _actual_outcomes(d, bar):
    """读 >bar 真实行情 -> {close1, gap, od, trend3};缺对应 bar 时该项为 None。"""
    if bar + 1 >= len(d):
        return {"close1": None, "gap": None, "od": None, "trend3": None}
    close0 = float(d["close"].iloc[bar])
    open1 = float(d["open"].iloc[bar + 1])
    close1 = float(d["close"].iloc[bar + 1])
    if close0 <= 0 or open1 <= 0 or close1 <= 0:
        return {"close1": None, "gap": None, "od": None, "trend3": None}
    out = {
        "close1": close1 / close0 - 1.0,
        "gap": open1 / close0 - 1.0,
        "od": close1 / open1 - 1.0,
        "trend3": None,
    }
    if bar + 3 < len(d):
        close3 = float(d["close"].iloc[bar + 3])
        if close3 > 0:
            out["trend3"] = close3 / close0 - 1.0
    return out


def _class_metric(rows):
    """分类维命中率(镜像 predict._metric 的 hit_rate/n_hold/base_rate,输入为已存预测)。

    rows = [(pred, label)];pred in {"up","down","hold"},label in {0,1}。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "base_rate": None, "hit_rate": None, "n_hold": 0, "n_bet": 0}
    n_hold = 0
    n_bet = 0
    hit = 0
    for pred, label in rows:
        if pred == "hold":
            n_hold += 1
            continue
        n_bet += 1
        if (pred == "up" and label == 1) or (pred == "down" and label == 0):
            hit += 1
    base_rate = float(sum(label for _, label in rows)) / n
    return {"n": n, "base_rate": base_rate,
            "hit_rate": hit / n_bet if n_bet else None,
            "n_hold": n_hold, "n_bet": n_bet}


def _path_metric(rows):
    """路径四分类命中率(镜像 predict._metric_path,输入为已存预测)。

    rows = [(pred_path, actual_path)];pred_path 为 None 者已在采集时剔除。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "acc_path": None}
    correct = sum(1 for p, a in rows if p == a)
    return {"n": n, "acc_path": correct / n}
