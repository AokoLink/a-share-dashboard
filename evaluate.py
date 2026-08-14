# -*- coding: utf-8 -*-
"""评分系统分层报告:只读预测引擎输出 + 自建 universe,打八层标签逐维报准确率。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np

import analysis as an
import backtest as bt
import predict as pr

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
AMOUNT_TOP_FRAC = 0.20
HIGH_POS_RATIO = 0.97
OVERSOLD_RET = -0.15
AMP_THRESHOLD = 15.0
MIN_LAYER_N = 30
LAYERS = ("大盘", "热门", "龙头", "趋势", "高位", "超跌", "反抽", "震荡")

DIMS = ("direction", "gap", "path", "trend3", "return", "risk")
PRIMARY = {"direction": "hit_rate", "gap": "hit_rate", "path": "acc_path",
           "trend3": "hit_rate", "return": "sign_agreement", "risk": "adverse_rate"}


def _binom_diff_se(p_l, n_l, p_o, n_o):
    if p_l is None or p_o is None or not n_l or not n_o:
        return None
    sl = (p_l * (1.0 - p_l) / n_l) ** 0.5
    so = (p_o * (1.0 - p_o) / n_o) ** 0.5
    return (sl * sl + so * so) ** 0.5


def _mean_diff_se(std_l, n_l, std_o, n_o):
    if std_l is None or std_o is None or not n_l or not n_o:
        return None
    return ((std_l * std_l) / n_l + (std_o * std_o) / n_o) ** 0.5


def _dim_metrics(cals, dim, records):
    """对一组记录算某维指标,复用 predict 的指标函数(单一真相源)。"""
    if dim == "path":
        samples = [(r["composite"], r["gap"], r["od"]) for r in records]
        return pr._metric_path(cals["gap"], cals["od"], samples)
    if dim == "return":
        samples = [(r["composite"], r["close1"]) for r in records]
        return pr._metric_return(cals["return"], cals["direction"], samples)
    if dim == "risk":
        samples = [(r["risk"], 1 if r["close1"] < pr.ADVERSE_THRESHOLD else 0)
                   for r in records if r["risk"] is not None]
        return pr._metric_risk(cals["risk"], samples)
    samples = []
    for r in records:
        if dim == "trend3" and r["trend3"] is None:
            continue
        label = r["direction_label"] if dim == "direction" else r[dim]
        samples.append((r["composite"], label))
    return pr._metric(dim, cals[dim], samples)


def _significance(overall, per_layer):
    """逐 (层, 维, 指标) 做 ±2σ 显著性;n<30 或值 None 跳过。
    返回 (significant, se_bounds, all_undifferentiated);se_bounds[(L, dim)] = 2*se(主指标)。
    """
    significant = []
    se_bounds = {}
    for dim, metric, kind in (
        ("direction", "hit_rate", "binom"),
        ("gap", "hit_rate", "binom"),
        ("path", "acc_path", "binom"),
        ("trend3", "hit_rate", "binom"),
        ("return", "sign_agreement", "binom"),
        ("return", "mae", "mean"),
        ("return", "rmse", "mean"),
        ("return", "mean_residual", "mean"),
        ("risk", "adverse_rate", "binom"),
    ):
        o = overall[dim]
        for L in LAYERS:
            m = per_layer[L][dim]
            if kind == "binom":
                if metric == "hit_rate":
                    n_l, n_o = m["n"] - m["n_hold"], o["n"] - o["n_hold"]
                elif metric == "sign_agreement":
                    n_l, n_o = m["sign_n"], o["sign_n"]
                else:  # adverse_rate / acc_path
                    n_l, n_o = m["n"], o["n"]
                v_l, v_o = m[metric], o[metric]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _binom_diff_se(v_l, n_l, v_o, n_o)
            else:  # mean
                std_key = {"mae": "mae_std", "rmse": "rmse_std",
                           "mean_residual": "residual_std"}[metric]
                v_l, v_o = m[metric], o[metric]
                n_l, n_o = m["n"], o["n"]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _mean_diff_se(m[std_key], n_l, o[std_key], n_o)
            if se is None:
                continue
            if metric == PRIMARY[dim]:
                se_bounds[(L, dim)] = 2.0 * se
            if abs(v_l - v_o) > 2.0 * se:
                significant.append((L, dim, metric, v_l, v_o, se))
    return significant, se_bounds, (len(significant) == 0)


def evaluate(records, universe, pos_of, all_days, sector_members, sector_map, cals):
    day_to_i = {dt: i for i, dt in enumerate(all_days)}
    by_date = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    layer_records = {L: [] for L in LAYERS}
    for date, recs in by_date.items():
        i = day_to_i[date]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        heat = bt.sector_heat(universe, sector_members, pos_of, all_days, i)
        amount_top = _amount_top(universe, pos_of, all_days, i, buyable)
        hot_members = _hot_members(sector_members, heat)
        composite_of = {r["code"]: r["composite"] for r in recs}
        leaders = _leaders(sector_map, buyable, composite_of)
        for r in recs:
            c, bar = r["code"], r["bar"]
            d = universe[c]
            tags = {
                "大盘": c in amount_top,
                "热门": c in hot_members,
                "龙头": c in leaders,
                "趋势": _layer_trend(d, bar),
                "高位": _layer_high(d, bar),
                "超跌": _layer_oversold(d, bar),
                "反抽": _layer_rebound(d, bar),
                "震荡": _layer_oscillation(d, bar),
            }
            for L, ok in tags.items():
                if ok:
                    layer_records[L].append(r)

    overall = {dim: _dim_metrics(cals, dim, records) for dim in DIMS}
    per_layer = {L: {dim: _dim_metrics(cals, dim, layer_records[L]) for dim in DIMS}
                 for L in LAYERS}
    significant, se_bounds, all_undifferentiated = _significance(overall, per_layer)
    return {"overall": overall, "layers": per_layer,
            "layer_n": {L: len(layer_records[L]) for L in LAYERS},
            "significant": significant, "se_bounds": se_bounds,
            "all_undifferentiated": all_undifferentiated}


def _amount_top(universe, pos_of, all_days, i, buyable):
    """当日 buyable 中 amount 前 top20%(NaN 排名前 dropna,不参与也不计分母)。"""
    dt = all_days[i]
    pairs = []
    for c in buyable:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        raw = universe[c]["amount"].iloc[bar]
        if raw is None:
            continue
        try:
            amt = float(raw)
        except (TypeError, ValueError):
            continue
        if amt != amt:
            continue
        pairs.append((c, amt))
    if not pairs:
        return set()
    pairs.sort(key=lambda x: -x[1])
    k = max(1, int(len(pairs) * AMOUNT_TOP_FRAC))
    return set(c for c, _ in pairs[:k])


def _hot_members(sector_members, heat):
    """中位数 5 日涨幅 top3 热板块的成员并集。"""
    ranked = sorted(heat, key=heat.get, reverse=True)
    out = set()
    for s in ranked[:TOP_SECTORS]:
        out.update(sector_members.get(s, []))
    return out


def _leaders(sector_map, buyable, composite_of):
    """每板块内 composite 最高的 buyable 成分 top1;并列取 code 最小。"""
    by_sector = {}
    for c in buyable:
        for s in sector_map.get(c, []):
            by_sector.setdefault(s, []).append(c)
    leaders = set()
    for members in by_sector.values():
        best_c, best_comp = None, None
        for c in members:
            comp = composite_of.get(c)
            if comp is None:
                continue
            if best_comp is None or comp > best_comp or (comp == best_comp and c < best_c):
                best_c, best_comp = c, comp
        if best_c is not None:
            leaders.add(best_c)
    return leaders


def _layer_trend(d, bar):
    df = an.add_ma(d.iloc[:bar + 1], (5, 20, 60))
    m5, m20, m60 = df["ma5"].iloc[-1], df["ma20"].iloc[-1], df["ma60"].iloc[-1]
    if any(x != x for x in (m5, m20, m60)):
        return False
    return m5 > m20 > m60


def _layer_high(d, bar):
    closes = d["close"].iloc[max(0, bar - 59): bar + 1]
    if len(closes) == 0:
        return False
    return float(d["close"].iloc[bar]) >= HIGH_POS_RATIO * float(closes.max())


def _layer_oversold(d, bar):
    if bar - 20 < 0:
        return False
    return float(d["close"].iloc[bar]) / float(d["close"].iloc[bar - 20]) - 1 < OVERSOLD_RET


def _layer_rebound(d, bar):
    if bar - 21 < 0:
        return False
    ret20_tm1 = float(d["close"].iloc[bar - 1]) / float(d["close"].iloc[bar - 21]) - 1
    return ret20_tm1 < OVERSOLD_RET and float(d["close"].iloc[bar]) > float(d["close"].iloc[bar - 1])


def _layer_oscillation(d, bar):
    window = d.iloc[max(0, bar - 19): bar + 1]
    close = float(d["close"].iloc[bar])
    if close <= 0:
        return False
    amp = (float(window["high"].max()) - float(window["low"].min())) / close * 100.0
    return amp < AMP_THRESHOLD


def run(data_dir, sector_map_path):
    results = pr.run_backtest(data_dir, sector_map_path)
    cals = results["calibrators"]
    valid_records = results["valid_records"]
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    for code, df in universe.items():
        if "amount" not in df.columns:
            raise RuntimeError(
                f"pkl {code} 缺 amount 列(实际 {list(df.columns)});请重拉 _analysis/daily 为 9 列")
    all_days, pos_of = bt.build_calendar(universe, codes)
    sector_members = bt.build_sector_members(sector_map, universe)
    report = evaluate(valid_records, universe, pos_of, all_days, sector_members, sector_map, cals)
    report["data_range"] = results["data_range"]
    report["valid_window"] = results["valid_window"]
    report["n_valid_records"] = len(valid_records)
    return report


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def build_report(report):
    def dim_payload(m):
        if "acc_path" in m:  # path
            return {"n": m["n"], "acc_path": _num(m["acc_path"])}
        if "hit_rate" in m:  # direction/gap/trend3
            return {"n": m["n"], "hit_rate": _num(m["hit_rate"]),
                    "base_rate": _num(m["base_rate"]), "n_hold": m["n_hold"]}
        if "mae" in m:  # return
            return {"n": m["n"], "mae": _num(m["mae"]), "rmse": _num(m["rmse"]),
                    "sign_agreement": _num(m["sign_agreement"]), "sign_n": m["sign_n"],
                    "mean_residual": _num(m["mean_residual"]),
                    "dir_cond_mae": {k: {"mae": _num(v["mae"]), "n": v["n"]}
                                     for k, v in m["dir_cond_mae"].items()}}
        return {"n": m["n"], "adverse_rate": _num(m["adverse_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "lift": _num(m["lift"])}

    def _effective_n(dim, m):
        metric = PRIMARY[dim]
        if metric == "hit_rate":
            return m["n"] - m["n_hold"]
        if metric == "sign_agreement":
            return m["sign_n"]
        return m["n"]  # acc_path (path) / adverse_rate (risk)

    def layer_dim_payload(dim, m):
        if _effective_n(dim, m) < MIN_LAYER_N:  # §5 薄层小 n:只报样本量
            return {"n": m["n"], "_suppressed": True}
        return dim_payload(m)

    overall = {dim: dim_payload(report["overall"][dim]) for dim in DIMS}
    layers = {L: {dim: layer_dim_payload(dim, report["layers"][L][dim]) for dim in DIMS}
              for L in LAYERS}
    return {
        "system_version": pr._git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "evaluate",
        "data_range": report["data_range"],
        "valid_window": report["valid_window"],
        "n_valid_records": report["n_valid_records"],
        "overall": overall,
        "layers": layers,
        "layer_n": report["layer_n"],
        "significant": [list(s) for s in report["significant"]],
        "se_bounds": [[L, dim, se] for (L, dim), se in sorted(report["se_bounds"].items())],
        "all_undifferentiated": report["all_undifferentiated"],
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 评分系统分层报告(六维 × 八层)", ""]
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- valid_window: {payload['valid_window']['start']} -> {payload['valid_window']['end']}")
    L.append(f"- n_valid_records: {payload['n_valid_records']}")
    L += ["", "## 总体(valid 段,out-of-sample)", "", "| 维度 | n | 主指标 |", "|---|---|---|"]
    for dim in DIMS:
        o = payload["overall"][dim]
        if dim == "path":
            L.append(f"| path | {o['n']} | acc_path={fmt(o['acc_path'])} |")
        elif dim in ("direction", "gap", "trend3"):
            L.append(f"| {dim} | {o['n']} | hit_rate={fmt(o['hit_rate'])} (base={fmt(o['base_rate'])}) |")
        elif dim == "return":
            L.append(f"| return | {o['n']} | mae={fmt(o['mae'])} rmse={fmt(o['rmse'])} "
                     f"sign={fmt(o['sign_agreement'])} resid={fmt(o['mean_residual'])} |")
        else:
            L.append(f"| risk | {o['n']} | adverse_rate={fmt(o['adverse_rate'])} "
                     f"ece={fmt(o['ece'])} lift={fmt(o['lift'])} |")
    se_map = {(L, dim): se for L, dim, se in payload["se_bounds"]}
    L += ["", "## 分层 × 维度(层值 vs 总体;n<30 只报样本量)", "", "| 层 | 维度 | n | 层值 | 总体值 | 2σ界 | 显著 |", "|---|---|---|---|---|---|---|"]
    for Ln in LAYERS:
        for dim in DIMS:
            cell = payload["layers"][Ln][dim]
            if cell.get("_suppressed"):
                L.append(f"| {Ln} | {dim} | {cell['n']} | (n<30) | - | - | - |")
                continue
            o = payload["overall"][dim]
            lv = cell.get(PRIMARY[dim])
            ov = o.get(PRIMARY[dim])
            bound = se_map.get((Ln, dim))
            sig = any(s[0] == Ln and s[1] == dim and s[2] == PRIMARY[dim] for s in payload["significant"])
            L.append(f"| {Ln} | {dim} | {cell['n']} | {fmt(lv)} | {fmt(ov)} "
                     f"| {fmt(bound)} | {'是' if sig else '否'} |")
    L += ["", "## 无分化判定", ""]
    if payload["all_undifferentiated"]:
        L.append("**八层无显著分化**:所有层与总体差异均在 ±2σ 噪声界内 → 触发分层定义重审(合并/换规则)。")
    else:
        L.append("存在显著分化层(见上表「显著」列),分层有区分度。")
    L += ["", "## 诚实声明", ""]
    L.append("1. expected_return 是 composite 分箱的阶梯函数(全宇宙当天 <=10 个取值),不是个股级回归预测。")
    L.append("2. expected_return 与 risk_p 分别是 composite/risk 的单特征边际,不是联合条件。")
    L.append("3. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数。")
    L.append("4. 大盘层用原始成交额(不复权)横截面排名;amount 取自 stock_zh_a_daily(adjust=qfq) 的 amount 列。")
    L.append("")
    return "\n".join(L)


def json_load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description="评分系统分层报告(六维 × 八层)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="evaluate_report.json")
    args = parser.parse_args(argv)
    try:
        report = run(args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = build_report(report)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {args.out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
