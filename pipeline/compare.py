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

from core import backtest as bt
from pipeline import predict as pr

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


def _empty_outcomes():
    return {"close1": None, "gap": None, "od": None, "trend3": None,
            "high1": None, "low1": None, "volume1": None,
            "path3": None, "max_high3": None, "min_low3": None}


def _actual_outcomes(d, bar):
    """读 >bar 真实行情 -> {close1, gap, od, trend3, high1, low1, volume1,
    path3, max_high3, min_low3};缺对应 bar 时相应字段为 None。

    trend3(3 日累计)保持原口径;path3/max_high3/min_low3 复用 holding_window(3 日窗口)。
    """
    if bar + 1 >= len(d):
        return _empty_outcomes()
    close0 = float(d["close"].iloc[bar])
    open1 = float(d["open"].iloc[bar + 1])
    close1 = float(d["close"].iloc[bar + 1])
    if close0 <= 0 or open1 <= 0 or close1 <= 0:
        return _empty_outcomes()
    out = {
        "close1": close1 / close0 - 1.0,
        "gap": open1 / close0 - 1.0,
        "od": close1 / open1 - 1.0,
        "trend3": None,
        "high1": float(d["high"].iloc[bar + 1]) / close0 - 1.0,
        "low1": float(d["low"].iloc[bar + 1]) / close0 - 1.0,
        "volume1": float(d["volume"].iloc[bar + 1]),
        "path3": None, "max_high3": None, "min_low3": None,
    }
    if bar + 3 < len(d):
        close3 = float(d["close"].iloc[bar + 3])
        if close3 > 0:
            out["trend3"] = close3 / close0 - 1.0
    w = bt.holding_window(d, bar, 3)
    if w is not None and w["n"] >= 3:
        out["path3"] = [w["close"][0] / close0 - 1.0,
                        w["close"][1] / w["close"][0] - 1.0,
                        w["close"][2] / w["close"][1] - 1.0]
        out["max_high3"] = max(w["high"]) / close0 - 1.0
        out["min_low3"] = min(w["low"]) / close0 - 1.0
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


def _trend3_stats(actuals):
    """T+3 描述统计:3 日累计/期间最高/期间最大回撤的均值(样本 = trend3 非 None 的已验证条)。"""
    cum, fav, adv = [], [], []
    for a in actuals:
        if a["trend3"] is None:
            continue
        cum.append(a["trend3"])
        if a["max_high3"] is not None:
            fav.append(a["max_high3"])
        if a["min_low3"] is not None:
            adv.append(a["min_low3"])

    def _mean(xs):
        return float(np.mean(xs)) if xs else None

    return {"n": len(cum), "mean_cum3": _mean(cum),
            "mean_max_high3": _mean(fav), "mean_min_low3": _mean(adv)}


def verify(snapshot_path, data_dir, sector_map_path):
    with open(snapshot_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("mode") != "predict":
        raise RuntimeError(f"snapshot mode != predict: {snap.get('mode')}")
    as_of_date = snap.get("as_of_date")
    if as_of_date is None:
        raise RuntimeError("snapshot missing as_of_date")
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
    actuals = []
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
        actuals.append({"code": code, "date": date,
                        "close1": oc["close1"], "gap": oc["gap"], "od": oc["od"],
                        "trend3": oc["trend3"], "high1": oc["high1"], "low1": oc["low1"],
                        "volume1": oc["volume1"], "path": path_actual,
                        "path3": oc["path3"], "max_high3": oc["max_high3"],
                        "min_low3": oc["min_low3"]})

        t1 = pred.get("T+1") or {}
        t3 = pred.get("T+3") or {}
        rows["direction"].append((t1.get("direction") or "hold", close1_up))
        gap_pred = {"high": "up", "low": "down"}.get(t1.get("gap"), "hold")
        rows["gap"].append((gap_pred, gap_up))
        rows["od"].append((t1.get("od") or "hold", od_up))
        pred_path = t1.get("path")
        if pred_path is not None:
            rows["path"].append((pred_path, path_actual))
        if oc["trend3"] is not None:
            trend3_up = 1 if oc["trend3"] > 0 else 0
            n_trend3_verified += 1
            rows["trend3"].append((t3.get("direction") or "hold", trend3_up))
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
    metrics["trend3"].update(_trend3_stats(actuals))
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
        "actuals": actuals,
        "metrics": metrics,
    }


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


def _clean_actuals(actuals):
    """逐条真实结果落库,NaN -> None 保证 JSON 合法。"""
    out = []
    for a in actuals:
        out.append({
            "code": a["code"], "date": a["date"],
            "close1": _num(a["close1"]), "gap": _num(a["gap"]), "od": _num(a["od"]),
            "trend3": _num(a["trend3"]), "high1": _num(a["high1"]), "low1": _num(a["low1"]),
            "volume1": _num(a["volume1"]), "path": a["path"],
            "path3": None if a["path3"] is None else [_num(x) for x in a["path3"]],
            "max_high3": _num(a["max_high3"]), "min_low3": _num(a["min_low3"]),
        })
    return out


def build_report(results):
    payload = {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "compare",
        "snapshot": results["snapshot"],
        "data_range": results["data_range"],
        "verification": results["verification"],
        "actuals": _clean_actuals(results["actuals"]),
        "metrics": {},
    }
    for name, m in results["metrics"].items():
        if name in ("return", "risk") and m.get("available") is False:
            payload["metrics"][name] = {"available": False, "reason": m["reason"]}
            continue
        if name in ("direction", "gap", "od", "trend3"):
            entry = {"n": m["n"], "base_rate": _num(m["base_rate"]),
                     "hit_rate": _num(m["hit_rate"]), "n_hold": m["n_hold"],
                     "n_bet": m["n_bet"]}
            if name == "trend3":
                entry["mean_cum3"] = _num(m.get("mean_cum3"))
                entry["mean_max_high3"] = _num(m.get("mean_max_high3"))
                entry["mean_min_low3"] = _num(m.get("mean_min_low3"))
            payload["metrics"][name] = entry
        elif name == "path":
            payload["metrics"][name] = {"n": m["n"], "acc_path": _num(m["acc_path"])}
        elif name == "return":
            payload["metrics"][name] = {"available": True, "n": m["n"], "mae": _num(m["mae"]),
                                        "rmse": _num(m["rmse"]),
                                        "sign_agreement": _num(m["sign_agreement"]),
                                        "sign_n": m["sign_n"], "mean_residual": _num(m["mean_residual"])}
        else:
            payload["metrics"][name] = {"available": True, "n": m["n"],
                                        "adverse_rate": _num(m["adverse_rate"]),
                                        "ece": _num(m["ece"]), "brier": _num(m["brier"]),
                                        "lift": _num(m["lift"])}
    return payload


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    snap = payload["snapshot"]
    L = ["# 快照对比报告(预测 vs 真实结果)", ""]
    L.append(f"- 快照 as_of_date: {snap['as_of_date']}")
    L.append(f"- 快照引擎 system_version: `{snap['system_version']}` (module {snap['module_version']})")
    L.append(f"- 快照生成时间: {snap['generated_at']}")
    L.append(f"- comparator system_version: `{payload['system_version']}` (module {payload['module_version']})")
    L.append(f"- 数据区间: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    v = payload["verification"]
    L += ["", "## 验证计数", ""]
    L.append(f"- 预测数: {v['n_predictions']}")
    L.append(f"- T+1 可验证: {v['n_verified']} / 不可验证: {v['n_unverified']}"
             f"(无下日 bar {v['unverified_reasons']['no_next_bar']} / 不在宇宙 {v['unverified_reasons']['not_in_universe']}"
             f" / 非正价 {v['unverified_reasons']['non_positive_close']})")
    L.append(f"- T+3 可验证: {v['n_trend3_verified']}")
    L.append(f"- 真实结果快照: {len(payload['actuals'])} 条已落库(见 JSON actuals,含 high/low/volume/逐日 path3/期间最高/回撤)")
    L += ["", "## 六维指标", "", "| 维度 | n | 主指标 | 备注 |", "|---|---|---|---|"]
    m = payload["metrics"]
    for name, label in (("direction", "方向"), ("gap", "开盘"), ("od", "盘中"), ("trend3", "T+3趋势")):
        mm = m[name]
        L.append(f"| {label} | {mm['n']} | hit_rate={fmt(mm['hit_rate'])} (base={fmt(mm['base_rate'])})"
                 f" | n_hold={mm['n_hold']} n_bet={mm['n_bet']} |")
        if name == "trend3":
            L.append(f"| T+3 描述统计 | {mm['n']} | mean_cum3={fmt(mm.get('mean_cum3'))} "
                     f"mean_max_high3={fmt(mm.get('mean_max_high3'))} "
                     f"mean_min_low3={fmt(mm.get('mean_min_low3'))} | 3日累计/期间最高/回撤 |")
    pm = m["path"]
    L.append(f"| 路径 | {pm['n']} | acc_path={fmt(pm['acc_path'])} | - |")
    for name, label in (("return", "涨跌幅"), ("risk", "风险")):
        mm = m[name]
        if not mm.get("available"):
            L.append(f"| {label} | - | 不可验证 | {mm['reason']} |")
        elif name == "return":
            L.append(f"| {label} | {mm['n']} | mae={fmt(mm['mae'])} rmse={fmt(mm['rmse'])}"
                     f" sign={fmt(mm['sign_agreement'])} (n={mm['sign_n']}) | resid={fmt(mm['mean_residual'])} |")
        else:
            L.append(f"| {label} | {mm['n']} | adverse_rate={fmt(mm['adverse_rate'])}"
                     f" ece={fmt(mm['ece'])} brier={fmt(mm['brier'])} | lift={fmt(mm['lift'])} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. 验证读两类行情:<=as_of 锚点 close(分母)+ >as_of 的 open1/close1/close3;预测字段是快照里冻结的,comparator 不重跑评分/校准。")
    L.append("2. 快照不可变:快照引擎代码 / comparator 代码 / 验证数据区间三者独立,互不冒充。")
    L.append("3. 样本按「股票x时间」聚集、非 i.i.d.,n 为样本数;单份前向快照 n 较小,准确率波动大,只作单次前向验证。")
    L.append("4. return/risk 维若快照无对应字段,如实标「不可验证」,不回退、不编造。")
    L.append("5. risk 维 ECE 按已存 risk_p 直接分箱(非原始 risk 经校准器)。")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    parser = argparse.ArgumentParser(description="快照对比器(预测快照 vs 真实结果)")
    parser.add_argument("--snapshot", default="prediction_snapshot.json")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="snapshot_verification.json")
    args = parser.parse_args(argv)
    try:
        results = verify(args.snapshot, args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = build_report(results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {args.out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
