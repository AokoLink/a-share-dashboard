# -*- coding: utf-8 -*-
"""多 horizon 前向分布引擎:从「单点 T+1」改为「拿多久都可以」的持仓诊断校准器。

对每个 horizon h ∈ {1,3,5,10,20},在**合法可执行窗口**(A股 T+1)上校准:
- 方向 P(涨):P(oo_h > 0),oo_h = open[bar+1+h]/open[bar+1] - 1(买 T+1 开盘、卖 T+1+h 开盘);
- 期望收益 E[oo_h];
- 左尾概率 P(持有 h 日内任一逐日 oo 收益 < TAIL_THRESHOLD),按 risk 分箱(风险是尾部信号)。

诚实原则:校准 P_up 接近 base_rate、E_ret 不超成本时,诊断必须输出「信号不足,无法判断」,
绝不硬给「会赚」。只读 _analysis/daily/*.pkl,不 fetch。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

from core import analysis as an
from core import backtest as bt
from core.calibration import N_BINS, Calibrator, _fit_calibrator, _bin_index

MODULE_VERSION = "1.1.0"
HORIZONS = (1, 3, 5, 10, 20)
MAX_H = max(HORIZONS)
EVAL_DAYS = 1200
TRAIN_FRAC = 0.8
COST = 0.004                      # 往返成本(买卖各约 0.2%,含冲击)
TAIL_THRESHOLD = -0.03            # 左尾定义:单日 oo 收益 < -3%
MIN_SIGNAL_BAND = 0.03            # |P_up - base_up| 低于此视为「测不到」
RISK_HIGH = 60                    # risk >= 此值标「风险偏高」
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)


# ---------- 合法窗口前向收益(纯函数,精确可测) ----------

def fwd_oo_series(d, bar, h):
    """逐日 oo 收益 open[bar+j+1]/open[bar+j]-1, j=1..h。数据不足或非正价返回 None。"""
    series = []
    for j in range(1, h + 1):
        if bar + j + 1 >= len(d):
            return None
        a = float(d["open"].iloc[bar + j])
        b = float(d["open"].iloc[bar + j + 1])
        if a <= 0 or b <= 0:
            return None
        series.append(b / a - 1.0)
    return series


def fwd_oo(d, bar, h):
    """买 T+1 开盘、卖 T+1+h 开盘的合法窗口收益 open[bar+1+h]/open[bar+1]-1。"""
    if bar + 1 + h >= len(d):
        return None
    a = float(d["open"].iloc[bar + 1])
    b = float(d["open"].iloc[bar + 1 + h])
    if a <= 0 or b <= 0:
        return None
    return b / a - 1.0


def left_tail_hit(d, bar, h):
    """持有 h 日内任一逐日 oo 收益 < TAIL_THRESHOLD → 1,否则 0;数据不足 None。"""
    series = fwd_oo_series(d, bar, h)
    if series is None:
        return None
    return 1 if any(r < TAIL_THRESHOLD for r in series) else 0


# ---------- 样本采集 ----------

def _forward_universe(universe, pos_of, all_days):
    """前向宇宙 {code: bar}:as_of_date 当日可交易股(不要求 T+1 bar 存在)。"""
    dt = all_days[-1]
    out = {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        if chg >= an.limit_threshold(c) or chg <= -7.0:
            continue
        amount = float(d["amount"].iloc[bar]) if "amount" in d else float(d["volume"].iloc[bar]) * close
        if not np.isfinite(amount) or amount < bt.MIN_AMOUNT:
            continue
        out[c] = bar
    return out


def _iter_forward(universe, pos_of, all_days, days):
    """逐评估日逐 buyable 股 yield (code,date,bar,composite,risk,fwd)。fwd = {h: (oo, tail)}。"""
    for i in days:
        dt = all_days[i]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        for c in buyable:
            bar = pos_of[c][dt]
            d = universe[c]
            sc = bt.score_at(d, bar, AFTER_CLOSE)
            if sc is None:
                continue
            composite = float(sc["composite"])
            if composite != composite:
                continue
            risk = sc.get("risk")
            risk = float(risk) if risk is not None else None
            fwd = {}
            ok = False
            for h in HORIZONS:
                oo = fwd_oo(d, bar, h)
                tail = left_tail_hit(d, bar, h) if oo is not None else None
                fwd[h] = (oo, tail)
                if oo is not None:
                    ok = True
            if not ok:
                continue
            yield {"code": c, "date": dt, "bar": bar, "composite": composite,
                   "risk": risk, "fwd": fwd}


def _collect_samples(universe, pos_of, all_days, days, label_end_before=None):
    """采样；验证时按各 h 的标签结束日清除跨边界训练样本。"""
    samples = {h: {"direction": [], "return": [], "tail": []} for h in HORIZONS}
    bench = {h: [] for h in HORIZONS}
    day_index = {day: i for i, day in enumerate(all_days)}
    for rec in _iter_forward(universe, pos_of, all_days, days):
        for h in HORIZONS:
            # 信号 T 的标签读到 T+1+h；边界当天及以后均属于验证区。
            if label_end_before is not None and day_index[rec["date"]] + 1 + h >= label_end_before:
                continue
            oo, tail = rec["fwd"][h]
            if oo is None:
                continue
            samples[h]["direction"].append((rec["composite"], 1 if oo > 0 else 0))
            samples[h]["return"].append((rec["composite"], oo))
            bench[h].append(oo)
            if rec["risk"] is not None and tail is not None:
                samples[h]["tail"].append((rec["risk"], tail))
    return samples, bench


def _fit_all(samples):
    """per-horizon 三个校准器:direction(单调)/return(非单调)/tail(risk→左尾,单调)。"""
    cals = {h: {} for h in HORIZONS}
    for h in HORIZONS:
        cals[h]["direction"] = _fit_calibrator(samples[h]["direction"], N_BINS)
        cals[h]["return"] = _fit_calibrator(samples[h]["return"], N_BINS, monotone=False)
        if samples[h]["tail"]:
            cals[h]["tail"] = _fit_calibrator(samples[h]["tail"], N_BINS)
        else:
            cals[h]["tail"] = Calibrator([(float("inf"), 0.0)], degraded=True)
    return cals


def _benchmarks(bench):
    """per-horizon 市场基准(全 buyable 股的 cross-sectional 汇总 = 宽基代理)。"""
    out = {}
    for h in HORIZONS:
        arr = [x for x in bench[h] if x is not None]
        if not arr:
            out[h] = {"n": 0, "median": None, "mean": None, "base_up": None}
        else:
            out[h] = {
                "n": len(arr),
                "median": float(np.median(arr)),
                "mean": float(np.mean(arr)),
                "base_up": float(np.mean([1 if x > 0 else 0 for x in arr])),
            }
    return out


# ---------- 诊断 ----------

def _confidence(horizons):
    """信心标签。best_edge = max_h(P_up_h - base_up_h);测不到则诚实输出「信号不足」。"""
    best_h, best_edge = None, None
    for h in HORIZONS:
        e = horizons[str(h)]["edge"]
        if e is None:
            continue
        if best_edge is None or e > best_edge:
            best_edge, best_h = e, h
    if best_h is None or best_edge is None or best_edge < MIN_SIGNAL_BAND:
        return "信号不足,无法判断", None
    net = horizons[str(best_h)]["net"]
    if net is not None and net > 0:
        return "有正向期望(超过成本)", best_h
    return "弱方向信号,未超过成本", best_h


def diagnose_at(d, bar, cals, benchmarks):
    """单股 ≤T → 持仓诊断 dict;composite 不可得返回 None。"""
    sc = bt.score_at(d, bar, AFTER_CLOSE)
    if sc is None:
        return None
    composite = float(sc["composite"])
    if composite != composite:
        return None
    risk = sc.get("risk")
    risk = float(risk) if risk is not None else None
    horizons = {}
    for h in HORIZONS:
        p_up = cals[h]["direction"].p_up(composite)
        base_up = benchmarks[h]["base_up"]
        er = cals[h]["return"].p_up(composite)
        bench = benchmarks[h]["median"]
        tail = cals[h]["tail"].p_up(risk) if risk is not None else None
        edge = (p_up - base_up) if (p_up is not None and base_up is not None) else None
        net = (er - COST) if er is not None else None
        horizons[str(h)] = {
            "p_up": p_up, "base_up": base_up, "edge": edge,
            "expected_return": er, "net": net, "benchmark": bench,
            "beats_benchmark": (er > bench) if (er is not None and bench is not None) else None,
            "exceeds_cost": (net > 0) if net is not None else None,
            "left_tail": tail,
        }
    label, best_h = _confidence(horizons)
    return {
        "code": str(d["code"].iloc[bar]),
        "date": str(d["date"].iloc[bar]),
        "composite": composite,
        "risk": risk,
        "confidence": label,
        "best_horizon": best_h,
        "risk_high": (risk >= RISK_HIGH) if risk is not None else False,
        "horizons": horizons,
    }


# ---------- 评估(out-of-sample,诚实校准质量) ----------

def _eval_direction(cal, samples):
    n = len(samples)
    if n == 0:
        return {"n": 0, "base_rate": None, "ece": None, "brier": None, "spread": None}
    base_rate = float(sum(l for _, l in samples)) / n
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    brier = 0.0
    for composite, label in samples:
        p = cal.p_up(composite)
        idx = _bin_index(cal, composite)
        counts[idx] += 1
        sums[idx] += label
        brier += (p - label) ** 2
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    ps = [p for _u, p in cal.bins]
    spread = max(ps) - min(ps)
    return {"n": n, "base_rate": base_rate, "ece": ece, "brier": brier / n, "spread": spread}


def _eval_return(cal, samples):
    n = len(samples)
    if n == 0:
        return {"n": 0, "mae": None, "mean_residual": None}
    errs = [cal.p_up(c) - oo for c, oo in samples]
    mae = float(np.mean([abs(e) for e in errs]))
    mean_residual = float(np.mean([oo - cal.p_up(c) for c, oo in samples]))
    return {"n": n, "mae": mae, "mean_residual": mean_residual}


def _eval_tail(cal, samples):
    n = len(samples)
    if n == 0:
        return {"n": 0, "adverse_rate": None, "ece": None, "brier": None}
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    brier = 0.0
    for risk, tail in samples:
        p = cal.p_up(risk)
        idx = _bin_index(cal, risk)
        counts[idx] += 1
        sums[idx] += tail
        brier += (p - tail) ** 2
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    adverse_rate = float(sum(l for _, l in samples)) / n
    return {"n": n, "adverse_rate": adverse_rate, "ece": ece, "brier": brier / n}


def _evaluate(universe, pos_of, all_days, days, cals):
    metrics = {h: {} for h in HORIZONS}
    val = {h: {"direction": [], "return": [], "tail": []} for h in HORIZONS}
    for rec in _iter_forward(universe, pos_of, all_days, days):
        for h in HORIZONS:
            oo, tail = rec["fwd"][h]
            if oo is None:
                continue
            val[h]["direction"].append((rec["composite"], 1 if oo > 0 else 0))
            val[h]["return"].append((rec["composite"], oo))
            if rec["risk"] is not None and tail is not None:
                val[h]["tail"].append((rec["risk"], tail))
    for h in HORIZONS:
        metrics[h] = {
            "direction": _eval_direction(cals[h]["direction"], val[h]["direction"]),
            "return": _eval_return(cals[h]["return"], val[h]["return"]),
            "tail": _eval_tail(cals[h]["tail"], val[h]["tail"]),
        }
    return metrics


def run_fit(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    n = len(eval_days)
    n_train = int(TRAIN_FRAC * n)
    train_days = eval_days[:n_train]
    valid_days = eval_days[n_train:]
    first_valid = valid_days[0]
    samples, bench = _collect_samples(universe, pos_of, all_days, train_days,
                                      label_end_before=first_valid)
    cals = _fit_all(samples)
    benchmarks = _benchmarks(bench)
    metrics = _evaluate(universe, pos_of, all_days, valid_days, cals)
    return {
        "calibrators": cals,
        "benchmarks": benchmarks,
        "metrics": metrics,
        "n_samples": {h: len(samples[h]["direction"]) for h in HORIZONS},
        "train_label_end_before": str(all_days[first_valid]),
        "train_signal_end_by_horizon": {
            h: str(all_days[max(0, first_valid - h - 2)]) for h in HORIZONS},
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "train_window": {"start": str(all_days[train_days[0]]), "end": str(all_days[train_days[-1]])},
        "valid_window": {"start": str(all_days[valid_days[0]]), "end": str(all_days[valid_days[-1]])},
        "n_eval": n, "step": step, "n_train": n_train, "n_valid": n - n_train,
    }


def predict_now(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    samples, bench = _collect_samples(universe, pos_of, all_days, eval_days)
    cals = _fit_all(samples)
    benchmarks = _benchmarks(bench)
    fwd = _forward_universe(universe, pos_of, all_days)
    diagnoses = []
    for c in sorted(fwd):
        dg = diagnose_at(universe[c], fwd[c], cals, benchmarks)
        if dg is not None:
            diagnoses.append(dg)
    return {"as_of_date": all_days[-1], "benchmarks": benchmarks, "diagnoses": diagnoses}


# ---------- 序列化 ----------

def _cal_to_json(cal):
    return {"bins": [{"upper": None if u == float("inf") else round(u, 4),
                      "p": round(p, 4)} for u, p in cal.bins],
            "degraded": cal.degraded}


def _cal_from_json(obj):
    bins = [(float("inf") if b["upper"] is None else float(b["upper"]), float(b["p"]))
            for b in obj["bins"]]
    return Calibrator(bins, degraded=bool(obj.get("degraded", False)))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def build_payload(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    calibrators = {str(h): {name: _cal_to_json(results["calibrators"][h][name])
                            for name in ("direction", "return", "tail")} for h in HORIZONS}
    metrics = {}
    for h in HORIZONS:
        m = results["metrics"][h]
        metrics[str(h)] = {
            "direction": {k: _num(v) if k != "n" else v for k, v in m["direction"].items()},
            "return": {k: _num(v) if k != "n" else v for k, v in m["return"].items()},
            "tail": {k: _num(v) if k != "n" else v for k, v in m["tail"].items()},
        }
    benchmarks = {str(h): {k: _num(v) if k != "n" else v for k, v in results["benchmarks"][h].items()}
                  for h in HORIZONS}
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "mode": "fit",
        "membership_basis": "static_snapshot_no_effective_dates",
        "universe_basis": "available_pkl_only_delisted_unverified",
        "horizons": list(HORIZONS),
        "cost": COST,
        "tail_threshold": TAIL_THRESHOLD,
        "min_signal_band": MIN_SIGNAL_BAND,
        "data_range": results["data_range"],
        "train_window": results["train_window"],
        "valid_window": results["valid_window"],
        "n_eval": results["n_eval"], "step": results["step"],
        "n_train": results["n_train"], "n_valid": results["n_valid"],
        "n_samples": results["n_samples"],
        "train_label_end_before": results.get("train_label_end_before"),
        "train_signal_end_by_horizon": results.get("train_signal_end_by_horizon"),
        "benchmarks": benchmarks,
        "calibrators": calibrators,
        "metrics": metrics,
    }


def build_snapshot(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "mode": "predict",
        "membership_basis": "static_snapshot_no_effective_dates",
        "horizons": list(HORIZONS),
        "cost": COST,
        "as_of_date": results["as_of_date"],
        "benchmarks": results["benchmarks"],
        "diagnoses": results["diagnoses"],
    }


def load_model(path):
    """读 forward_calib.json → (cals, benchmarks, meta)。cals = {h: {name: Calibrator}}。"""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    cals = {int(h): {name: _cal_from_json(obj) for name, obj in payload["calibrators"][str(h)].items()}
            for h in payload["horizons"]}
    benchmarks = {int(h): payload["benchmarks"][str(h)] for h in payload["horizons"]}
    return cals, benchmarks, payload


def prepare_frame(df, code, tail_n=1200):
    """单股 pkl → 可 score 的 frame(补 code + change_pct),与 build_universe 同口径。"""
    d = df.reset_index(drop=True)
    if tail_n is not None and len(d) > tail_n:
        d = d.tail(tail_n)
    d = d.copy()
    d["code"] = code
    d["change_pct"] = d["close"].pct_change() * 100.0
    d.iloc[0, d.columns.get_loc("change_pct")] = 0.0
    return d


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 多 horizon 前向分布校准报告", ""]
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- generated_at: {payload['generated_at']}")
    L.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    L.append(f"- train_window: {payload['train_window']['start']} -> {payload['train_window']['end']} (n_train={payload['n_train']})")
    L.append(f"- valid_window: {payload['valid_window']['start']} -> {payload['valid_window']['end']} (n_valid={payload['n_valid']})")
    L.append(f"- 成本 COST={payload['cost']}, 左尾阈值={payload['tail_threshold']}, 信号带={payload['min_signal_band']}")
    L += ["", "## 各 horizon 市场基准(全 buyable 股 = 宽基代理)", "",
          "| horizon | n | base_up(P涨) | 中位收益 | 均值收益 |", "|---|---|---|---|---|"]
    for h in payload["horizons"]:
        b = payload["benchmarks"][str(h)]
        L.append(f"| {h} | {b['n']} | {fmt(b['base_up'])} | {fmt(b['median'])} | {fmt(b['mean'])} |")
    L += ["", "## 校准质量(valid 段,out-of-sample)", "",
          "| horizon | dir_n | dir_base | dir_ece | dir_brier | dir_spread | ret_mae | tail_adverse | tail_ece |", "|---|---|---|---|---|---|---|---|---|"]
    for h in payload["horizons"]:
        m = payload["metrics"][str(h)]
        L.append(f"| {h} | {m['direction']['n']} | {fmt(m['direction']['base_rate'])} | {fmt(m['direction']['ece'])} "
                 f"| {fmt(m['direction']['brier'])} | {fmt(m['direction']['spread'])} "
                 f"| {fmt(m['return']['mae'])} | {fmt(m['tail']['adverse_rate'])} | {fmt(m['tail']['ece'])} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. dir_spread 是校准器 P_up 的跨箱极差;spread 越窄,方向区分度越弱(接近 base_rate 即无 edge)。")
    L.append("2. 诊断信心标签据此判定:|P_up - base_up| < MIN_SIGNAL_BAND 一律输出「信号不足,无法判断」。")
    L.append("3. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数。")
    L.append("4. benchmark 为校准窗口内全部 buyable 股的横截面汇总,是「全市场中位/宽基代理」,非指数本身。")
    L.append("")
    return "\n".join(L)


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(description="多 horizon 前向分布引擎(持仓诊断校准器)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--predict", action="store_true", help="前向诊断快照模式(默认拟合+评估)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    out = args.out or os.path.join("_analysis", "forward_snapshot.json" if args.predict else "forward_calib.json")
    try:
        if args.predict:
            payload = build_snapshot(predict_now(args.data_dir, args.sector_map))
        else:
            payload = build_payload(run_fit(args.data_dir, args.sector_map))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if args.predict:
        print(f"wrote {out} (n_diagnoses={len(payload['diagnoses'])})")
    else:
        md_path = os.path.splitext(out)[0] + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(payload))
        print(f"wrote {out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
