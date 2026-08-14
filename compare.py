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


def _return_metric(rows):
    """涨跌幅误差(镜像 predict._metric_return,输入为已存预测)。

    rows = [(er, close1)];er=预测 expected_return,close1=实际 T+1 收益。
    residual = close1 - er(正 = 系统性低估,与 predict 同向)。
    符号一致率:close1==0 样本剔除(不计分子分母)。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "sign_agreement": None,
                "sign_n": 0, "mean_residual": None}
    abs_errs = []
    sq_errs = []
    residuals = []
    sign_num = 0
    sign_den = 0
    for er, close1 in rows:
        e = er - close1
        abs_errs.append(abs(e))
        sq_errs.append(e * e)
        residuals.append(close1 - er)
        if close1 != 0:
            sign_den += 1
            if (er > 0 and close1 > 0) or (er < 0 and close1 < 0):
                sign_num += 1
    return {"n": n, "mae": float(np.mean(abs_errs)), "rmse": float(np.sqrt(np.mean(sq_errs))),
            "sign_agreement": sign_num / sign_den if sign_den else None,
            "sign_n": sign_den, "mean_residual": float(np.mean(residuals))}


def _risk_metric(rows):
    """风险概率校准度(镜像 predict._metric_risk,输入为已存 risk_p 直接分箱)。

    rows = [(risk_p, adverse)];adverse = 1[close1 < ADVERSE_THRESHOLD]。
    ECE 按 risk_p 升序等量分 N_BINS 箱(等样本数;n < N_BINS 降单箱),比箱内 mean_p vs realized。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "adverse_rate": None, "ece": None, "brier": None, "lift": None}
    adverse_rate = float(np.mean([a for _, a in rows]))
    brier = float(np.mean([(rp - a) ** 2 for rp, a in rows]))
    rows_sorted = sorted(rows, key=lambda x: x[0])
    if n < pr.N_BINS:
        bins = [rows_sorted]
    else:
        bins = []
        for b in range(pr.N_BINS):
            lo = b * n // pr.N_BINS
            hi = (b + 1) * n // pr.N_BINS
            bins.append(rows_sorted[lo:hi])
    ece = 0.0
    realized = []
    for chunk in bins:
        if not chunk:
            continue
        mean_p = float(np.mean([rp for rp, _ in chunk]))
        realized_bin = float(np.mean([a for _, a in chunk]))
        realized.append(realized_bin)
        ece += (len(chunk) / n) * abs(mean_p - realized_bin)
    lift = (realized[-1] - realized[0]) if len(realized) >= 2 else None
    return {"n": n, "adverse_rate": adverse_rate, "ece": ece, "brier": brier, "lift": lift}
