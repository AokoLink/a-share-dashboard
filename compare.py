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


def verify(snapshot_path, data_dir, sector_map_path):
    with open(snapshot_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("mode") != "predict":
        raise RuntimeError(f"snapshot mode != predict: {snap.get('mode')}")
    as_of_date = snap["as_of_date"]
    predictions = snap.get("predictions", [])
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)

    unverified = {"not_in_universe": 0, "no_next_bar": 0, "non_positive_close": 0}
    n_verified = 0
    n_trend3_verified = 0
    rows = {"direction": [], "gap": [], "od": [], "trend3": [], "path": [], "return": [], "risk": []}
    has_expected_return = False
    has_risk_p = False

    for pred in predictions:
        code = str(pred["code"])
        date = str(pred.get("date") or as_of_date)
        bar = pos_of.get(code, {}).get(date)
        if bar is None:
            unverified["not_in_universe"] += 1
            continue
        d = universe[code]
        if bar + 1 >= len(d):
            unverified["no_next_bar"] += 1
            continue
        close0 = float(d["close"].iloc[bar])
        open1 = float(d["open"].iloc[bar + 1])
        close1 = float(d["close"].iloc[bar + 1])
        if close0 <= 0 or open1 <= 0 or close1 <= 0:
            unverified["non_positive_close"] += 1
            continue
        oc = _actual_outcomes(d, bar)
        n_verified += 1
        close1_up = 1 if oc["close1"] > 0 else 0
        gap_up = 1 if oc["gap"] > 0 else 0
        od_up = 1 if oc["od"] > 0 else 0
        path_actual = _path_label(gap_up, od_up)

        t1 = pred.get("T+1") or {}
        t3 = pred.get("T+3") or {}
        rows["direction"].append((t1.get("direction"), close1_up))
        gap_pred = {"high": "up", "low": "down"}.get(t1.get("gap"), "hold")
        rows["gap"].append((gap_pred, gap_up))
        rows["od"].append((t1.get("od"), od_up))
        pred_path = t1.get("path")
        if pred_path is not None:
            rows["path"].append((pred_path, path_actual))
        if oc["trend3"] is not None:
            trend3_up = 1 if oc["trend3"] > 0 else 0
            n_trend3_verified += 1
            rows["trend3"].append((t3.get("direction"), trend3_up))
        er = pred.get("expected_return")
        if er is not None:
            has_expected_return = True
            rows["return"].append((float(er), oc["close1"]))
        rp = pred.get("risk_p")
        if rp is not None:
            has_risk_p = True
            rows["risk"].append((float(rp), 1 if oc["close1"] < pr.ADVERSE_THRESHOLD else 0))

    metrics = {}
    for name in ("direction", "gap", "od", "trend3"):
        metrics[name] = _class_metric(rows[name])
    metrics["path"] = _path_metric(rows["path"])
    if has_expected_return:
        metrics["return"] = _return_metric(rows["return"])
        metrics["return"]["available"] = True
    else:
        metrics["return"] = {"available": False, "reason": "快照无 expected_return 字段(1.0.0 引擎生成)"}
    if has_risk_p:
        metrics["risk"] = _risk_metric(rows["risk"])
        metrics["risk"]["available"] = True
    else:
        metrics["risk"] = {"available": False, "reason": "快照无 risk_p 字段(1.0.0 引擎生成)"}

    return {
        "snapshot": {
            "path": snapshot_path,
            "system_version": snap.get("system_version"),
            "module_version": snap.get("module_version"),
            "generated_at": snap.get("generated_at"),
            "as_of_date": as_of_date,
        },
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "verification": {
            "n_predictions": len(predictions),
            "n_verified": n_verified,
            "n_trend3_verified": n_trend3_verified,
            "n_unverified": sum(unverified.values()),
            "unverified_reasons": unverified,
        },
        "metrics": metrics,
    }
