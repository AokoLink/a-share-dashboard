# -*- coding: utf-8 -*-
"""市场环境分类:七态(牛/熊/震荡/恐慌/高潮/退潮/恢复)决策树。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
合成市场指数 = universe 中位日收益复合,非真实指数(口径差异见 spec §3)。
分类纯因果:第 i 日标签只依赖 <= i 的数据。
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
MIN_HISTORY = 60
MIN_STATE_N = 20

PANIC_R5 = -0.08
PANIC_R1 = -0.04
PANIC_UP_RATIO = 0.15
PANIC_LIMIT_DOWN = 300
CLIMAX_R5 = 0.08
CLIMAX_UP_RATIO = 0.85
CLIMAX_LIMIT_UP = 100
CLIMAX_TURNOVER = 1.8
BEAR_R20 = -0.08
BULL_R20 = 0.08
RECOVERY_R5 = 0.03
RECOVERY_UP_RATIO = 0.55
RECEDE_R5 = -0.03
RECEDE_UP_RATIO = 0.45

LABELS = ("牛", "熊", "震荡", "恐慌", "高潮", "退潮", "恢复")


def _miss(v):
    """None 或 NaN 都视为缺失。"""
    return v is None or v != v


def classify(row):
    """单日七态决策;历史不足(r60/ma60 缺失)返回 None。row 为 dict 或 pandas Series。"""
    if _miss(row["r60"]) or _miss(row["ma60"]):
        return None
    r5 = row["r5"]; r1 = row["r1"]; up = row["up_ratio"]
    r20 = row["r20"]; ld = row["limit_down"]; lu = row["limit_up"]
    tr = row["turnover_ratio"]; ma5 = row["ma5"]; ma20 = row["ma20"]; ma60 = row["ma60"]
    if (not _miss(r5) and r5 <= PANIC_R5) \
            or (not _miss(r1) and not _miss(up) and r1 <= PANIC_R1 and up <= PANIC_UP_RATIO) \
            or (not _miss(ld) and ld >= PANIC_LIMIT_DOWN):
        return "恐慌"
    if not _miss(r5) and r5 >= CLIMAX_R5 and (
            (not _miss(up) and up >= CLIMAX_UP_RATIO) or (not _miss(lu) and lu >= CLIMAX_LIMIT_UP)
            or (not _miss(tr) and tr >= CLIMAX_TURNOVER)):
        return "高潮"
    if not _miss(r20) and r20 <= BEAR_R20 and not _miss(ma5) and not _miss(ma20) \
            and ma5 < ma20 < ma60:
        return "熊"
    if not _miss(r20) and r20 >= BULL_R20 and not _miss(ma5) and not _miss(ma20) \
            and ma5 > ma20 > ma60:
        return "牛"
    if not _miss(r5) and not _miss(r20) and not _miss(up) \
            and r5 >= RECOVERY_R5 and r20 < 0 and up >= RECOVERY_UP_RATIO:
        return "恢复"
    if not _miss(r20) and not _miss(r5) and not _miss(up) \
            and r20 >= 0 and r5 <= RECEDE_R5 and up <= RECEDE_UP_RATIO:
        return "退潮"
    return "震荡"


def build_series(universe, pos_of, all_days):
    """合成市场序列:行 = all_days 位置,列见 spec §5;末列 environment。"""
    codes = list(universe.keys())
    th_of = {c: an.limit_threshold(c) for c in codes}
    rows = []
    for dt in all_days:
        rets = []
        up = 0
        lu = 0
        ld = 0
        turnover = 0.0
        n = 0
        for c in codes:
            bar = pos_of[c].get(dt)
            if bar is None:
                continue
            d = universe[c]
            chg = d["change_pct"].iloc[bar]
            if _miss(chg):
                continue
            chg = float(chg)
            rets.append(chg / 100.0)
            n += 1
            if chg > 0:
                up += 1
            th = th_of[c]
            if chg >= th:
                lu += 1
            elif chg <= -th:
                ld += 1
            amt = d["amount"].iloc[bar]
            if not _miss(amt):
                turnover += float(amt)
        rows.append({"date": dt,
                     "r1": float(np.median(rets)) if rets else None,
                     "up_ratio": (up / n) if n else None,
                     "limit_up": lu, "limit_down": ld,
                     "turnover": turnover})
    df = pd.DataFrame(rows)
    # 规格 §5:turnover_ratio = turnover / mean(turnover[i-5..i-1]);i<5 或分母 0 → None。
    # 分母 0 直接除会得 inf(inf >= CLIMAX_TURNOVER 误触发高潮),故 0 替换为 NaN(规格 §72 NaN/None 同义)。
    denom = df["turnover"].shift(1).rolling(5).mean().replace(0.0, np.nan)
    df["turnover_ratio"] = df["turnover"] / denom
    r1 = df["r1"].fillna(0.0).to_numpy()
    M = np.empty(len(df))
    if len(df) > 0:
        M[0] = 1.0
        M[1:] = np.cumprod(1.0 + r1[1:])
    df["M"] = M
    df["ma5"] = df["M"].rolling(5).mean()
    df["ma20"] = df["M"].rolling(20).mean()
    df["ma60"] = df["M"].rolling(60).mean()
    df["r5"] = df["M"] / df["M"].shift(5) - 1.0
    df["r20"] = df["M"] / df["M"].shift(20) - 1.0
    df["r60"] = df["M"] / df["M"].shift(60) - 1.0
    # pandas 3.0 会把 apply 结果推断为 Arrow str dtype,把 classify 的 None(不足)转成 NaN;
    # 规格 §5 要求 environment 列值为「七态字符串或 None(不足)」,故显式 object Series + None。
    df["environment"] = pd.Series(
        [None if _miss(v) else v for v in df.apply(classify, axis=1)], dtype=object)
    return df


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
    return None if f != f else f


def run(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    series = build_series(universe, pos_of, all_days)
    dist = {L: 0 for L in LABELS}
    n_classified = 0
    n_insufficient = 0
    for lab in series["environment"]:
        if lab is None:
            n_insufficient += 1
        else:
            dist[lab] += 1
            n_classified += 1
    state_stats = {}
    for L in LABELS:
        sub = series[series["environment"] == L]
        if len(sub) == 0:
            state_stats[L] = {"n": 0, "mean_r5": None, "mean_r20": None,
                              "mean_up_ratio": None, "mean_turnover_ratio": None}
            continue
        state_stats[L] = {"n": int(len(sub)),
                          "mean_r5": _num(sub["r5"].mean()),
                          "mean_r20": _num(sub["r20"].mean()),
                          "mean_up_ratio": _num(sub["up_ratio"].mean()),
                          "mean_turnover_ratio": _num(sub["turnover_ratio"].mean())}
    degenerate = [L for L in LABELS if dist[L] < MIN_STATE_N]
    series_list = [{"date": str(dt), "environment": lab}
                   for dt, lab in zip(series["date"], series["environment"])]
    return {
        "data_range": {"first": str(all_days[0]), "last": str(all_days[-1])},
        "n_days": len(all_days),
        "n_classified": n_classified,
        "n_insufficient": n_insufficient,
        "distribution": dist,
        "state_stats": state_stats,
        "degenerate_states": degenerate,
        "series": series_list,
    }


# ---------- regime gate(择时门控) ----------

REGIME_ADVICE = {
    "恐慌": {"action": "buy_dip", "message": "市场恐慌,关注超跌反弹(全市场/宽基);建议持有 2-3 天(T+1 开盘买,T+3/4 开盘卖)。日级净胜率 73%(41 天样本,肥左尾,接飞刀风险,非稳赚)"},
    "高潮": {"action": "avoid", "message": "市场冲顶(高潮),建议空仓/回避追高"},
    "退潮": {"action": "avoid", "message": "市场杀跌延续(退潮),建议空仓"},
    "牛":   {"action": "neutral", "message": "牛市主升,无择时 edge(方向中性)"},
    "熊":   {"action": "neutral", "message": "熊市,无择时 edge"},
    "震荡": {"action": "neutral", "message": "震荡市(占多数天数),无择时 edge,建议降低交易频率"},
    "恢复": {"action": "neutral", "message": "恢复期,无择时 edge"},
}


def regime_advice(label):
    """标签 → 择时建议(纯函数)。None/未知 → unknown。"""
    if label in REGIME_ADVICE:
        return REGIME_ADVICE[label]
    return {"action": "unknown", "message": "历史不足或未知状态,无法给出择时建议"}


def latest_state(data_dir, sector_map_path, tail_n=120, cache_path=None):
    """当日 regime 标签 + 指标 + 建议(纯因果,读 pkl)。

    tail_n=120:分类只需最近 60 日(MIN_HISTORY);合成指数 M 为复利,ma/r 比较对
    起始基重定不变,故末日常标签与完整历史一致、但快约 30 倍。
    文件缓存:as_of(日历最后交易日)与 code 数不变则跳过 build_series 重算。
    """
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map, tail_n=tail_n)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    as_of = str(all_days[-1])
    if cache_path is not None and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("as_of") == as_of and cached.get("n_codes") == len(codes):
                return {"as_of": as_of, "label": cached.get("label"),
                        "metrics": cached.get("metrics", {}),
                        "advice": regime_advice(cached.get("label"))}
        except (OSError, ValueError):
            pass
    series = build_series(universe, pos_of, all_days)
    label = series["environment"].iloc[-1]
    if label is not None and label != label:     # NaN 防御(object 已用 None,但防 pandas 3.0)
        label = None
    metrics = {
        "r5": _num(series["r5"].iloc[-1]),
        "r1": _num(series["r1"].iloc[-1]),
        "r20": _num(series["r20"].iloc[-1]),
        "up_ratio": _num(series["up_ratio"].iloc[-1]),
        "limit_up": _num(series["limit_up"].iloc[-1]),
        "limit_down": _num(series["limit_down"].iloc[-1]),
        "turnover_ratio": _num(series["turnover_ratio"].iloc[-1]),
    }
    result = {"as_of": as_of, "label": label, "metrics": metrics,
              "advice": regime_advice(label)}
    if cache_path is not None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"as_of": as_of, "n_codes": len(codes),
                           "label": label, "metrics": metrics},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return result


def load_cached_regime(cache_path):
    """读文件缓存(instant);无缓存/损坏 → None。供 /api/recommend 快速注入,不触发重算。"""
    if cache_path is None or not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    return {"as_of": cached.get("as_of"), "label": cached.get("label"),
            "metrics": cached.get("metrics", {}),
            "advice": regime_advice(cached.get("label"))}


def build_report(report):
    return {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "environment",
        "data_range": report["data_range"],
        "n_days": report["n_days"],
        "n_classified": report["n_classified"],
        "n_insufficient": report["n_insufficient"],
        "distribution": report["distribution"],
        "state_stats": report["state_stats"],
        "degenerate_states": report["degenerate_states"],
        "series": report["series"],
        "notes": ["合成指数为 universe 中位收益复合,非真实指数(口径差异见 spec §3)"],
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 市场环境分类报告(七态)", ""]
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- 数据区间: {payload['data_range']['first']} -> {payload['data_range']['last']}")
    L.append(f"- 总交易日: {payload['n_days']};已分类: {payload['n_classified']};"
             f"不足: {payload['n_insufficient']}")
    L += ["", "## 状态分布", "", "| 状态 | 天数 | 占比 |", "|---|---|---|"]
    n = payload["n_classified"]
    for lab in LABELS:
        c = payload["distribution"][lab]
        pct = f"{c / n * 100:.1f}%" if n else "-"
        L.append(f"| {lab} | {c} | {pct} |")
    L += ["", "## 各态特征均值", "", "| 状态 | n | mean_r5 | mean_r20 | mean_up_ratio | mean_turnover_ratio |",
          "|---|---|---|---|---|---|"]
    for lab in LABELS:
        ss = payload["state_stats"][lab]
        L.append(f"| {lab} | {ss['n']} | {fmt(ss['mean_r5'])} | {fmt(ss['mean_r20'])} "
                 f"| {fmt(ss['mean_up_ratio'])} | {fmt(ss['mean_turnover_ratio'])} |")
    if payload["degenerate_states"]:
        L += ["", "## 退化态提示", "",
              f"- 以下状态样本数 < {MIN_STATE_N},分布可能不稳定:"
              f"{', '.join(payload['degenerate_states'])}"]
    L += ["", "## 诚实声明", ""]
    L.append("1. 合成市场指数 = universe 中位日收益复合,非真实上证指数(幸存者+大中盘口径)。")
    L.append("2. 每个交易日恰一标签(优先级决策树,互斥);标签只用 <= 当日数据(纯因果)。")
    L.append("3. 阈值是初值常量,本报告仅锚定分布、不自动调参;退化态如实标注,不编造。")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    parser = argparse.ArgumentParser(description="市场环境分类(七态)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="environment_report.json")
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
