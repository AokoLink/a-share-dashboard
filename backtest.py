# -*- coding: utf-8 -*-
"""历史盲测基线模块(生产级固化 nxday_backtest.py 核心次日盲测路径)。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
严格禁止未来数据泄露:评分输入 <= T,验证只读 T+1。
"""
import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import analysis as an

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
PER_SECTOR = 5
EVAL_DAYS = 1200
B_SAMPLE_EVERY = 10
MIN_AMOUNT = 1e8
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)

RNG = np.random.default_rng(0)   # 模块级单实例,评估日按循环顺序复用(spec §5.6 C)


def load_daily(data_dir, code):
    path = os.path.join(data_dir, f"{code}.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing daily pkl: {path}")
    return pd.read_pickle(path)


def load_sector_map(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing sector map: {path}")
    with open(path, "r", encoding="gbk") as f:
        return json.load(f)


def build_universe(data_dir, sector_map):
    universe, codes = {}, []
    files = sorted(f for f in os.listdir(data_dir) if f.endswith(".pkl"))
    for fn in files:
        code = fn[:-4]
        if code not in sector_map:
            continue
        d = load_daily(data_dir, code)
        if len(d) < 1200:
            continue
        d = d.reset_index(drop=True).tail(1200).copy()
        d["code"] = code
        d["change_pct"] = d["close"].pct_change() * 100.0
        d.iloc[0, d.columns.get_loc("change_pct")] = 0.0
        universe[code] = d
        codes.append(code)
    return universe, codes


def build_calendar(universe, codes):
    all_days = sorted(set().union(*[set(universe[c]["date"].values) for c in codes]))
    pos_of = {c: {dt: int(idx) for idx, dt in enumerate(universe[c]["date"].values)} for c in codes}
    return all_days, pos_of


def build_sector_members(sector_map, universe):
    sector_members = {}
    for c, secs in sector_map.items():
        if c in universe:
            for s in secs:
                sector_members.setdefault(s, []).append(c)
    return sector_members


def next_returns(d, i):
    if i + 1 >= len(d):
        return None
    c0 = float(d["close"].iloc[i]); o1 = float(d["open"].iloc[i + 1]); c1 = float(d["close"].iloc[i + 1])
    if c0 <= 0 or o1 <= 0 or c1 <= 0:
        return None
    return {"gap": o1 / c0 - 1, "close1": c1 / c0 - 1, "od": c1 / o1 - 1}


def holding_window(d, bar, h):
    """(bar, bar+h] 的真实 OHLCV 路径(不含 bar 本身,bar 为决策 bar)。

    返回 {"n": m, "open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]}
    - m 为实际可用根数(1 <= m <= h);越界或任一根 open/close 非正即在该根之前截断;
    - m == 0 返回 None。
    只向前读 > bar 的行情,无未来泄露。
    """
    lo = bar + 1
    if lo >= len(d):
        return None
    hi = min(bar + h, len(d) - 1)
    opens, highs, lows, closes, vols = [], [], [], [], []
    for j in range(lo, hi + 1):
        try:
            o = float(d["open"].iloc[j])
            c = float(d["close"].iloc[j])
        except (TypeError, ValueError):
            break
        if o <= 0 or c <= 0:
            break
        opens.append(o)
        highs.append(float(d["high"].iloc[j]))
        lows.append(float(d["low"].iloc[j]))
        closes.append(c)
        vols.append(float(d["volume"].iloc[j]))
    if not closes:
        return None
    return {"n": len(closes), "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": vols}


def _win_gain(universe, pos_of, all_days, code, i, w):
    j_hi = pos_of[code].get(all_days[i])
    if j_hi is None:
        return None
    j_lo = pos_of[code].get(all_days[max(0, i - (w - 1))])
    if j_lo is None or j_hi <= j_lo:
        return None
    c0 = float(universe[code]["close"].iloc[j_lo])
    c1 = float(universe[code]["close"].iloc[j_hi])
    if c0 <= 0:
        return None
    return c1 / c0 - 1


def sector_heat(universe, sector_members, pos_of, all_days, i):
    heat = {}
    for s, members in sector_members.items():
        gains = {}
        for c in members:
            g = _win_gain(universe, pos_of, all_days, c, i, 5)
            if g is not None:
                gains[c] = g
        if len(gains) >= 3:
            heat[s] = float(np.median(list(gains.values())))
    return heat


def score_at(d, i, now):
    df = d.iloc[: i + 1]
    if len(df) < 61:
        return None
    quote = {"price": float(df["close"].iloc[-1]),
             "change_pct": float(df["change_pct"].iloc[-1]),
             "volume": float(df["volume"].iloc[-1]),
             "amount": float(df["volume"].iloc[-1]) * float(df["close"].iloc[-1]),
             "high": float(df["high"].iloc[-1]),
             "low": float(df["low"].iloc[-1]),
             "open": float(df["open"].iloc[-1])}
    sc = an.score_stock(df, quote, now)
    return sc if sc["composite"] is not None else None


def _long_upper_shadow(high, low, open_, close):
    """「高位长上影」谓词,与 analysis.py:498-503 同式;输入取自时点 bar(≤T)。"""
    if None in (high, low, open_, close) or close <= 0:
        return False
    body = max(open_, close) - min(open_, close)
    upper = high - max(open_, close)
    amplitude = (high - low) / max(open_, close) * 100 if max(open_, close) > 0 else 0
    return body > 0 and upper > 2 * body and amplitude > 5


def build_buyable(universe, pos_of, all_days, i):
    dt = all_days[i]
    buy = set()
    od_m, gap_m, c1_m = {}, {}, {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None or bar + 1 >= len(universe[c]):
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        nr = next_returns(d, bar)
        if nr is None:
            continue
        th = an.limit_threshold(c)
        if chg >= th or chg <= -7.0:
            continue
        if float(d["volume"].iloc[bar]) * close < MIN_AMOUNT:
            continue
        buy.add(c)
        od_m[c] = nr["od"]; gap_m[c] = nr["gap"]; c1_m[c] = nr["close1"]
    return buy, od_m, gap_m, c1_m


def select_baskets(sector_members, pos_of, all_days, i, buyable, hot, get_score, start, rng):
    dt = all_days[i]
    hot_members = set()
    for s in hot:
        hot_members.update(sector_members.get(s, []))
    basket_a, pos_hi, pos_lo = [], [], []
    for s in hot:
        scored = []
        for c in sector_members.get(s, []):
            if c not in buyable:
                continue
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            bonus = 8 if sc["composite"] >= 68 else 4 if sc["composite"] >= 60 else 0
            fa = sc["composite"] + bonus
            scored.append((fa, c, sc["position"]))
        scored.sort(key=lambda x: -x[0])          # 先 -fa(spec §5.6 E 两段式)
        basket_a += [x[1] for x in scored[:PER_SECTOR]]
        scored.sort(key=lambda x: -x[2])          # 稳定重排 -position,并列按 fa 序
        pos_hi += [x[1] for x in scored[:PER_SECTOR]]
        pos_lo += [x[1] for x in scored[-PER_SECTOR:]]
    basket_b = None
    if (i - start) % B_SAMPLE_EVERY == 0:
        scored_b = []
        for c in sorted(buyable):
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            scored_b.append((sc["composite"], c))
        scored_b.sort(key=lambda x: -x[0])
        basket_b = [c for _, c in scored_b[:TOP_SECTORS * PER_SECTOR]]
    basket_c = sorted(hot_members & buyable)
    if len(basket_c) > TOP_SECTORS * PER_SECTOR:
        basket_c = list(rng.choice(basket_c, size=TOP_SECTORS * PER_SECTOR, replace=False))
    basket_d = list(buyable)
    return {"A": basket_a, "E_hi": pos_hi, "E_lo": pos_lo, "B": basket_b, "C": basket_c, "D": basket_d}


def stats(basket, m):
    return float(np.mean([m[c] for c in basket if c in m])) if basket else np.nan


def summ(arr):
    a = np.array([x for x in arr if x == x and x is not None])
    if len(a):
        return (round(float(np.nanmean(a) * 100), 3),
                round(float(np.mean(a > 0) * 100), 1),
                int(len(a)))
    return (np.nan, np.nan, 0)


def summ_year(arr, years):
    groups = {}
    for x, y in zip(arr, years):
        if x is None or x != x:
            continue
        groups.setdefault(y, []).append(x)
    out = []
    for y in sorted(groups):
        a = np.array(groups[y])
        if len(a):
            out.append((y, round(float(np.nanmean(a) * 100), 3),
                        round(float(np.mean(a > 0) * 100), 1), int(len(a))))
    return out


def _betacf(a, b, x):
    """不完全 beta 的连分式展开(Numerical Recipes 6.4)。"""
    MAXIT = 200
    EPS = 3.0e-14
    FPMIN = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a, b, x):
    """正则化不完全 beta I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t, df):
    """Student's t CDF(t >= 0)。"""
    x = df / (df + t * t)
    return 1.0 - 0.5 * _betainc(df / 2.0, 0.5, x)


def welch_t(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return {"t": None, "p": None}
    m1, m2 = a.mean(), b.mean()
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se = (v1 / n1 + v2 / n2) ** 0.5
    if se == 0.0:
        return {"t": 0.0, "p": 1.0}
    t = (m1 - m2) / se
    df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * (1.0 - _t_cdf(abs(t), df))
    return {"t": float(t), "p": float(p)}


def run(data_dir, sector_map_path):
    sector_map = load_sector_map(sector_map_path)
    universe, codes = build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    sector_members = build_sector_members(sector_map, universe)
    all_days, pos_of = build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)

    score_cache = {}
    diag = {"long_upper_shadow": 0, "pushed_over_70": 0}
    def get_score(code, bar):
        key = (code, bar)
        if key not in score_cache:
            sc = score_at(universe[code], bar, AFTER_CLOSE)
            score_cache[key] = sc
            if sc is not None:
                d = universe[code]
                hi = float(d["high"].iloc[bar])
                lo = float(d["low"].iloc[bar])
                op = float(d["open"].iloc[bar])
                cl = float(d["close"].iloc[bar])
                if _long_upper_shadow(hi, lo, op, cl):
                    diag["long_upper_shadow"] += 1
                    if sc["risk"] >= 70 and sc["risk"] - 20 < 70:
                        diag["pushed_over_70"] += 1
        return score_cache[key]

    A_od, A_gap, A_c1 = [], [], []
    E_hi_od, E_lo_od = [], []
    B_od, C_od, D_od = [], [], []
    eval_years = []
    n_eval = 0

    for i in range(start, len(all_days) - 1, step):
        heat = sector_heat(universe, sector_members, pos_of, all_days, i)
        if not heat:
            continue
        hot = sorted(heat, key=heat.get, reverse=True)[:TOP_SECTORS]
        buyable, od_m, gap_m, c1_m = build_buyable(universe, pos_of, all_days, i)
        baskets = select_baskets(sector_members, pos_of, all_days, i,
                                 buyable, hot, get_score, start, RNG)
        A_od.append(stats(baskets["A"], od_m))
        A_gap.append(stats(baskets["A"], gap_m))
        A_c1.append(stats(baskets["A"], c1_m))
        E_hi_od.append(stats(baskets["E_hi"], od_m))
        E_lo_od.append(stats(baskets["E_lo"], od_m))
        if baskets["B"] is not None:
            B_od.append(stats(baskets["B"], od_m))
        C_od.append(stats(baskets["C"], od_m))
        D_od.append(stats(baskets["D"], od_m))
        eval_years.append(all_days[i][:4])
        n_eval += 1

    rows = {
        "A 实际管线(热板块xtop5)  次日od": summ(A_od),
        "A 隔夜gap": summ(A_gap),
        "A close->next close": summ(A_c1),
        "E 板块内低位股(pos分top5)次日od": summ(E_hi_od),
        "E 板块内高位股(pos分bot5)次日od": summ(E_lo_od),
        "B 全市场top15  次日od": summ(B_od),
        "C 热板块随机  次日od": summ(C_od),
        "D 全市场基准  次日od": summ(D_od),
    }
    by_year = {
        "A 实际管线": summ_year(A_od, eval_years),
        "E 板块内低位股(pos top5)": summ_year(E_hi_od, eval_years),
        "E 板块内高位股(pos bot5)": summ_year(E_lo_od, eval_years),
        "C 热板块随机": summ_year(C_od, eval_years),
        "D 全市场基准": summ_year(D_od, eval_years),
    }
    welch = {
        "A 实际管线(热板块xtop5)  次日od": welch_t(A_od, D_od),
        "B 全市场top15  次日od": welch_t(B_od, D_od),
        "C 热板块随机  次日od": welch_t(C_od, D_od),
        "E 板块内低位股(pos分top5)次日od": welch_t(E_hi_od, D_od),
        "E 板块内高位股(pos分bot5)次日od": welch_t(E_lo_od, D_od),
    }
    data_range = {"start": str(all_days[0]), "end": str(all_days[-1])}
    window = {"start": str(all_days[start]), "end": str(all_days[len(all_days) - 2])}
    return {"rows": rows, "by_year": by_year, "welch": welch, "diag": diag,
            "n_eval": n_eval, "step": step, "window": window, "data_range": data_range}


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def _json_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def build_report(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    rows = {k: {"mean_pct": _json_num(v[0]), "win_rate": _json_num(v[1]), "n": v[2]}
            for k, v in results["rows"].items()}
    by_year = {k: [{"year": y, "mean_pct": _json_num(m), "win_rate": _json_num(w), "n": n}
                   for y, m, w, n in v] for k, v in results["by_year"].items()}
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "data_range": results["data_range"],
        "n_eval": results["n_eval"],
        "step": results["step"],
        "window": results["window"],
        "sector_heat_note": "median-5-day-gain proxy (daily pkl 无 amount,生产板块 composite 不可复现)",
        "rows": rows,
        "by_year": by_year,
        "welch": results["welch"],
        "diag": results.get("diag", {}),
    }


def render_markdown(payload):
    def fmt(x, nd=3):
        return "-" if x is None else f"{x:.{nd}f}"

    lines = ["# 历史盲测基线报告", ""]
    lines.append(f"- system_version: `{payload['system_version']}`")
    lines.append(f"- module_version: {payload['module_version']}")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    lines.append(f"- 评估窗口: {payload['window']['start']} -> {payload['window']['end']} (n_eval={payload['n_eval']}, step={payload['step']})")
    lines += ["", "## 整体(次日)", "", "| 篮子 | mean_pct% | win_rate% | n |", "|---|---|---|---|"]
    for k, v in payload["rows"].items():
        lines.append(f"| {k} | {fmt(v['mean_pct'])} | {fmt(v['win_rate'], 1)} | {v['n']} |")
    lines += ["", "## Welch(对 D 全市场基准,非配对且样本重叠,仅定性参考)", "", "| 篮子 | t | p |", "|---|---|---|"]
    for k, v in payload["welch"].items():
        lines.append(f"| {k} | {fmt(v['t'], 4)} | {fmt(v['p'], 4)} |")
    lines += ["", "## 按年", "", "| 篮子 | 年份 | mean_pct% | win_rate% | n |", "|---|---|---|---|---|"]
    for k, rows in payload["by_year"].items():
        for r in rows:
            lines.append(f"| {k} | {r['year']} | {fmt(r['mean_pct'])} | {fmt(r['win_rate'], 1)} | {r['n']} |")
    lines += ["", "## 篮子 A 代理声明", ""]
    lines.append(payload["sector_heat_note"])
    lines.append("")
    lines.append("因日线 pkl 无 amount(成交额),生产 collect_sector_metrics 所需 turnover_ratio(emotion 25%)与 activity(strength 30%)无法历史复现。篮子 A 与生产 recommend.py 管线存在以下代理差异:")
    lines += ["", "| 维度 | 生产 recommend.py | 基线篮 A(代理) |", "|---|---|---|",
              "| 板块选择 | select_sectors verdict∈{建议关注, 跟踪(热点延续)} + composite 降序 + top3 | hot = 板块中位数 5 日涨幅 top3 |",
              "| 个股加成 | sector_bonus(68/60/50,+8/+4/-5) | composite 分档 8/4/0(无 -5) |",
              "| 热权重重算 | _apply_hot_weights(composite>=68 改用 HOT_SIGNAL_WEIGHTS) | 无(恒 V3_WEIGHTS) |",
              "| 个股硬过滤 | filter_candidates ST/新股/停牌/涨停/<=-7%/amount<1e8 | buyable 涨停/<=-7%/volume*close>=1e8 |",
              "| 加成×风险折扣位置 | (quality+bonus)*(1-risk/100) 加成在折扣内 | quality*(1-risk/100)+bonus 加成在折扣外 |",
              "| verdict 过滤 | rank_candidates 剔 verdict==回避(<42) | 仅 risk<70 |",
              ""]
    lines.append("结论:基线篮 A 的准确率是「代理管线」的准确率,作为当前版本可复现的近似基线;真正的生产口径基线须待 amount 数据补齐(后续切片)。")
    diag = payload.get("diag", {})
    if diag:
        lines += ["", "## 诊断(P0-quote-highlowopen 生效计数)", ""]
        lines.append(f"- 高位长上影触发 stock-day: {diag.get('long_upper_shadow', 0)}")
        lines.append(f"- 因 +20 越 risk>=70 门槛 stock-day: {diag.get('pushed_over_70', 0)}")
        lines.append("")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="历史盲测基线(次日方向)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="backtest_baseline.json")
    args = parser.parse_args(argv)

    try:
        results = run(args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    payload = build_report(results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {args.out} and {md_path} (n_eval={payload['n_eval']}, step={payload['step']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
