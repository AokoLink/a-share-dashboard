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

from core import analysis as an
from core import backtest as bt
from core.calibration import N_BINS, Calibrator, _fit_calibrator, _bin_index

MODULE_VERSION = "1.1.0"
EVAL_DAYS = 1200
TRAIN_FRAC = 0.8
DIRECTION_BAND = 0.05
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)
ADVERSE_THRESHOLD = -0.03


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
    risk = sc.get("risk")
    risk = float(risk) if risk is not None else None
    p_dir = cals["direction"].p_up(composite)
    p_gap = cals["gap"].p_up(composite)
    p_od = cals["od"].p_up(composite)
    p_t3 = cals["trend3"].p_up(composite)
    expected_return = cals["return"].p_up(composite)
    risk_p = cals["risk"].p_up(risk) if risk is not None else None
    direction, confidence = _dir_conf(p_dir)
    gap_dir = _gap_dir(p_gap)
    od_dir, _ = _dir_conf(p_od)
    t3_dir, t3_conf = _dir_conf(p_t3)
    return {
        "code": str(d["code"].iloc[i]),
        "date": str(d["date"].iloc[i]),
        "composite": composite,
        "expected_return": expected_return,
        "risk_p": risk_p,
        "T+1": {
            "direction": direction,
            "confidence": confidence,
            "gap": gap_dir,
            "od": od_dir,
            "path": _path(gap_dir, od_dir),
        },
        "T+3": {"direction": t3_dir, "confidence": t3_conf},
    }


def _iter_scored(universe, pos_of, all_days, days):
    """逐评估日逐 buyable 股 yield record dict(≤T 特征 + T+1 标签)。"""
    for i in days:
        dt = all_days[i]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        for c in buyable:
            bar = pos_of[c][dt]
            sc = bt.score_at(universe[c], bar, AFTER_CLOSE)
            if sc is None:
                continue
            composite = float(sc["composite"])
            if composite != composite:  # NaN: 诚实不预测
                continue
            risk = sc.get("risk")
            risk = float(risk) if risk is not None else None
            nr = bt.next_returns(universe[c], bar)
            if nr is None:
                continue
            lbl = _labels(universe[c], bar)
            if lbl is None:
                continue
            yield {"code": c, "date": dt, "bar": bar, "composite": composite,
                   "risk": risk, "close1": nr["close1"], "lbl": lbl}


def _collect_samples(universe, pos_of, all_days, days):
    samples = {"direction": [], "gap": [], "od": [], "trend3": [], "return": [], "risk": []}
    for rec in _iter_scored(universe, pos_of, all_days, days):
        lbl = rec["lbl"]
        samples["direction"].append((rec["composite"], lbl["close1"]))
        samples["gap"].append((rec["composite"], lbl["gap"]))
        samples["od"].append((rec["composite"], lbl["od"]))
        if lbl["trend3"] is not None:
            samples["trend3"].append((rec["composite"], lbl["trend3"]))
        samples["return"].append((rec["composite"], rec["close1"]))
        if rec["risk"] is not None:
            samples["risk"].append((rec["risk"], 1 if rec["close1"] < ADVERSE_THRESHOLD else 0))
    return samples


def _fit_all(samples):
    cals = {}
    for name in ("direction", "gap", "od", "trend3", "risk"):
        cals[name] = _fit_calibrator(samples[name], N_BINS)
    cals["return"] = _fit_calibrator(samples["return"], N_BINS, monotone=False)
    return cals


def _metric(name, cal, samples):
    n = len(samples)
    if n == 0:
        return {"n": 0, "base_rate": None, "hit_rate": None, "ece": None, "brier": None, "n_hold": 0}
    base_rate = float(sum(l for _, l in samples)) / n
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    n_hold = 0
    hit = 0
    n_bet = 0
    brier_sum = 0.0
    brier_n = 0
    for composite, label in samples:
        p = cal.p_up(composite)
        direction, _ = _dir_conf(p)
        idx = _bin_index(cal, composite)
        counts[idx] += 1
        sums[idx] += label
        if direction == "hold":
            n_hold += 1
        else:
            n_bet += 1
            correct = (direction == "up" and label == 1) or (direction == "down" and label == 0)
            if correct:
                hit += 1
            brier_sum += (p - label) ** 2
            brier_n += 1
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    return {
        "n": n,
        "base_rate": base_rate,
        "hit_rate": hit / n_bet if n_bet else None,
        "ece": ece,
        "brier": brier_sum / brier_n if brier_n else None,
        "n_hold": n_hold,
    }


def _metric_return(cal, direction_cal, samples):
    """return 维度:MAE/RMSE/符号一致率/平均残差/方向条件 MAE。

    samples = [(composite, close1), ...];cal=return 校准器(composite→mean close1);
    direction_cal 用于 dir_cond 分组(经 _dir_conf 得 up/down/hold)。
    符号一致率:close1==0 样本剔除(不计分子分母);residual = close1 - expected。
    方向条件 MAE:按预测方向分组各报 MAE,小 n(<30)只报 n 不报值。
    """
    n = len(samples)
    if n == 0:
        return {"n": 0, "mae": None, "mae_std": None, "rmse": None, "rmse_std": None,
                "sign_agreement": None, "sign_n": 0, "mean_residual": None,
                "residual_std": None,
                "dir_cond_mae": {"up": {"mae": None, "n": 0},
                                 "down": {"mae": None, "n": 0},
                                 "hold": {"mae": None, "n": 0}}}
    abs_errs = []
    sq_errs = []
    residuals = []
    sign_num = 0
    sign_den = 0
    by_dir = {"up": [], "down": [], "hold": []}
    for composite, close1 in samples:
        er = cal.p_up(composite)
        e = er - close1
        abs_errs.append(abs(e))
        sq_errs.append(e * e)
        residuals.append(close1 - er)
        if close1 != 0:
            sign_den += 1
            if (er > 0 and close1 > 0) or (er < 0 and close1 < 0):
                sign_num += 1
        d, _ = _dir_conf(direction_cal.p_up(composite))
        by_dir[d].append(abs(e))
    mae = float(np.mean(abs_errs))
    mae_std = float(np.std(abs_errs, ddof=1)) if n > 1 else 0.0
    rmse = float(np.sqrt(np.mean(sq_errs)))
    rmse_std = float(np.std(sq_errs, ddof=1)) if n > 1 else 0.0
    mean_residual = float(np.mean(residuals))
    residual_std = float(np.std(residuals, ddof=1)) if n > 1 else 0.0
    sign_agreement = sign_num / sign_den if sign_den else None
    dir_cond = {}
    for d in ("up", "down", "hold"):
        arr = by_dir[d]
        n_d = len(arr)
        dir_cond[d] = {"mae": float(np.mean(arr)) if n_d >= 30 else None, "n": n_d}
    return {"n": n, "mae": mae, "mae_std": mae_std, "rmse": rmse, "rmse_std": rmse_std,
            "sign_agreement": sign_agreement, "sign_n": sign_den,
            "mean_residual": mean_residual, "residual_std": residual_std,
            "dir_cond_mae": dir_cond}


def _metric_risk(cal, samples):
    """risk 维度:ECE/Brier/单调 lift/adverse_rate。samples = [(risk, adverse), ...]"""
    n = len(samples)
    if n == 0:
        return {"n": 0, "adverse_rate": None, "ece": None, "brier": None, "lift": None}
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    brier_sum = 0.0
    for risk, adverse in samples:
        p = cal.p_up(risk)
        idx = _bin_index(cal, risk)
        counts[idx] += 1
        sums[idx] += adverse
        brier_sum += (p - adverse) ** 2
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    realized = [sums[idx] / counts[idx] if counts[idx] else None for idx in range(len(cal.bins))]
    nonempty = [r for r in realized if r is not None]
    lift = (nonempty[-1] - nonempty[0]) if nonempty else None
    adverse_rate = float(sum(l for _, l in samples)) / n
    return {"n": n, "adverse_rate": adverse_rate, "ece": ece, "brier": brier_sum / n, "lift": lift}


def _metric_path(gap_cal, od_cal, samples):
    """path 维度:gap×od 四分类命中率。samples = [(composite, gap_up, od_up), ...]。"""
    path_n = 0
    path_correct = 0
    for composite, gap_up, od_up in samples:
        gap_dir = _gap_dir(gap_cal.p_up(composite))
        od_dir, _ = _dir_conf(od_cal.p_up(composite))
        pred_path = _path(gap_dir, od_dir)
        if pred_path is None:
            continue
        actual = _path_label(gap_up, od_up)
        path_n += 1
        if pred_path == actual:
            path_correct += 1
    return {"n": path_n, "acc_path": path_correct / path_n if path_n else None}


def _path_label(gap_up, od_up):
    if gap_up and od_up:
        return "高开高走"
    if gap_up and not od_up:
        return "高开低走"
    if not gap_up and od_up:
        return "低开高走"
    return "低开低走"


def _evaluate(universe, pos_of, all_days, days, cals):
    val = {"direction": [], "gap": [], "od": [], "trend3": [], "path": [], "return": [], "risk": []}
    records = []
    for rec in _iter_scored(universe, pos_of, all_days, days):
        lbl = rec["lbl"]
        composite = rec["composite"]
        val["direction"].append((composite, lbl["close1"]))
        val["gap"].append((composite, lbl["gap"]))
        val["od"].append((composite, lbl["od"]))
        if lbl["trend3"] is not None:
            val["trend3"].append((composite, lbl["trend3"]))
        val["path"].append((composite, lbl["gap"], lbl["od"]))
        val["return"].append((composite, rec["close1"]))
        if rec["risk"] is not None:
            val["risk"].append((rec["risk"], 1 if rec["close1"] < ADVERSE_THRESHOLD else 0))
        direction, _ = _dir_conf(cals["direction"].p_up(composite))
        records.append({
            "code": rec["code"], "date": rec["date"], "bar": rec["bar"],
            "composite": composite, "risk": rec["risk"], "close1": rec["close1"],
            "direction_label": lbl["close1"], "gap": lbl["gap"], "od": lbl["od"],
            "trend3": lbl["trend3"],
            "expected_return": cals["return"].p_up(composite),
            "risk_p": cals["risk"].p_up(rec["risk"]) if rec["risk"] is not None else None,
            "direction": direction,
        })
    metrics = {name: _metric(name, cals[name], val[name]) for name in ("direction", "gap", "od", "trend3")}
    metrics["return"] = _metric_return(cals["return"], cals["direction"], val["return"])
    metrics["risk"] = _metric_risk(cals["risk"], val["risk"])
    metrics["path"] = _metric_path(cals["gap"], cals["od"], val["path"])
    return metrics, records


def run_backtest(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    n = len(eval_days)
    n_train = int(TRAIN_FRAC * n)
    train_days = eval_days[:n_train]
    valid_days = eval_days[n_train:]
    samples = _collect_samples(universe, pos_of, all_days, train_days)
    cals = _fit_all(samples)
    metrics, valid_records = _evaluate(universe, pos_of, all_days, valid_days, cals)
    return {
        "calibrators": cals,
        "n_samples": {name: len(samples[name]) for name in samples},
        "metrics": metrics,
        "valid_records": valid_records,
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "train_window": {"start": str(all_days[train_days[0]]), "end": str(all_days[train_days[-1]])},
        "valid_window": {"start": str(all_days[valid_days[0]]), "end": str(all_days[valid_days[-1]])},
        "n_eval": n, "step": step, "n_train": n_train, "n_valid": n - n_train,
    }


def predict_now(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    samples = _collect_samples(universe, pos_of, all_days, eval_days)
    cals = _fit_all(samples)
    fwd = forward_universe(universe, pos_of, all_days)
    predictions = []
    for c in sorted(fwd):
        pred = predict_at(universe[c], fwd[c], AFTER_CLOSE, cals)
        if pred is not None:
            predictions.append(pred)
    return {"as_of_date": all_days[-1], "predictions": predictions}


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def build_report(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    calibrators = {}
    for name, cal in results["calibrators"].items():
        bins = [{"upper": None if upper == float("inf") else round(upper, 4), "p": round(p, 4)}
                for upper, p in cal.bins]
        calibrators[name] = {"bins": bins, "n": results["n_samples"].get(name), "degraded": cal.degraded}
    metrics = {}
    for name, m in results["metrics"].items():
        if name == "path":
            metrics[name] = {"n": m["n"], "acc_path": _num(m["acc_path"])}
        elif name == "return":
            metrics[name] = {
                "n": m["n"], "mae": _num(m["mae"]), "rmse": _num(m["rmse"]),
                "sign_agreement": _num(m["sign_agreement"]), "sign_n": m["sign_n"],
                "mean_residual": _num(m["mean_residual"]),
                "dir_cond_mae": {k: {"mae": _num(v["mae"]), "n": v["n"]}
                                 for k, v in m["dir_cond_mae"].items()},
            }
        elif name == "risk":
            metrics[name] = {
                "n": m["n"], "adverse_rate": _num(m["adverse_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "lift": _num(m["lift"]),
            }
        else:
            metrics[name] = {
                "n": m["n"], "base_rate": _num(m["base_rate"]), "hit_rate": _num(m["hit_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "n_hold": m["n_hold"],
            }
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "mode": "backtest",
        "data_range": results["data_range"],
        "train_window": results["train_window"],
        "valid_window": results["valid_window"],
        "n_eval": results["n_eval"], "step": results["step"],
        "n_train": results["n_train"], "n_valid": results["n_valid"],
        "calibrators": calibrators,
        "metrics": metrics,
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 预测引擎基线报告", ""]
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- generated_at: {payload['generated_at']}")
    L.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    L.append(f"- train_window: {payload['train_window']['start']} -> {payload['train_window']['end']} (n_train={payload['n_train']})")
    L.append(f"- valid_window: {payload['valid_window']['start']} -> {payload['valid_window']['end']} (n_valid={payload['n_valid']})")
    L.append(f"- n_eval={payload['n_eval']}, step={payload['step']}")
    L += ["", "## 校准表(composite 分箱 → P(up))", "", "| 校准器 | 箱上界 | P | degraded | 训练样本 n |", "|---|---|---|---|---|"]
    for name, cal in payload["calibrators"].items():
        for b in cal["bins"]:
            upper = "-" if b["upper"] is None else f"{b['upper']:.4f}"
            L.append(f"| {name} | {upper} | {b['p']:.4f} | {cal['degraded']} | {cal['n']} |")
    L += ["", "## 指标(valid 段,out-of-sample)", "", "| 校准器 | n | base_rate | hit_rate | ece | brier | n_hold |", "|---|---|---|---|---|---|---|"]
    for name, m in payload["metrics"].items():
        if name == "path":
            L.append(f"| path | {m['n']} | - | acc_path={fmt(m['acc_path'])} | - | - | - |")
        elif name == "return":
            L.append(f"| return | {m['n']} | - | mae={fmt(m['mae'])} | rmse={fmt(m['rmse'])} "
                     f"| sign={fmt(m['sign_agreement'])} (n={m['sign_n']}) | resid={fmt(m['mean_residual'])} |")
            L.append(f"|  dir_cond_mae | up {fmt(m['dir_cond_mae']['up']['mae'])}/{m['dir_cond_mae']['up']['n']} "
                     f"| down {fmt(m['dir_cond_mae']['down']['mae'])}/{m['dir_cond_mae']['down']['n']} "
                     f"| hold {fmt(m['dir_cond_mae']['hold']['mae'])}/{m['dir_cond_mae']['hold']['n']} | - | - | - |")
        elif name == "risk":
            L.append(f"| risk | {m['n']} | adverse_rate={fmt(m['adverse_rate'])} "
                     f"| ece={fmt(m['ece'])} | brier={fmt(m['brier'])} | lift={fmt(m['lift'])} | - |")
        else:
            L.append(f"| {name} | {m['n']} | {fmt(m['base_rate'])} | {fmt(m['hit_rate'])} "
                     f"| {fmt(m['ece'])} | {fmt(m['brier'])} | {m['n_hold']} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. 校准概率基于「代理管线」评分(日线 pkl 无 amount,生产板块 composite 不可复现)。")
    L.append("2. 回测模式指标仅在 valid 窗口有效(out-of-sample)。")
    L.append("3. 分钟级路径未做(数据缺口);path 为日线 OHLC 的 gap×od 四分类。")
    L.append("4. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数,ece/brier 不可按 n 直接推置信区间。")
    L.append("5. n_eval 为完整评估日 range,与基线 backtest(跳无热板块日)的 n_eval 可能不同,非同日口径。")
    L.append("")
    return "\n".join(L)


def build_snapshot(results):
    return {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "predict",
        "as_of_date": results["as_of_date"],
        "predictions": results["predictions"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="预测引擎(校准概率 + 多维 horizon)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--predict", action="store_true", help="前向模式(默认回测)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    out = args.out or ("prediction_snapshot.json" if args.predict else "prediction_baseline.json")
    try:
        if args.predict:
            payload = build_snapshot(predict_now(args.data_dir, args.sector_map))
        else:
            payload = build_report(run_backtest(args.data_dir, args.sector_map))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if args.predict:
        print(f"wrote {out}")
    else:
        md_path = os.path.splitext(out)[0] + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(payload))
        print(f"wrote {out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
