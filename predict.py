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
