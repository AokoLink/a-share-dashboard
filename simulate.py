# -*- coding: utf-8 -*-
"""交易模拟引擎 + 风险收益评价(大任务 §20/§21)。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
决策复用 backtest.select_baskets(评分输入 <=T);结果只读 >T 行情。
把「篮子盲测」升级为「逐笔交易模拟 + 盈亏比/回撤/Profit Factor」。
"""
import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

import backtest as bt

MODULE_VERSION = "1.0.0"

NOTES = [
    "入场价=close[T](信号收盘后生成,次日成交近似 close[T],不虚构分钟成交)",
    "保守日内假设:open 跳空越过 stop/target 价位按 open 成交;同日内 low<=stop 且 high>=target 同时触及,保守取 stop 先(先损)",
    "max_fav/max_adv 为持有窗口全程路径极值(机会口径,与 compare.py 的 max_high3/min_low3 一致),与止损止盈退出价解耦",
    "净收益 = ret_gross - 2*cost_bps/10000(双边成本)",
    "profit_factor 无亏损笔时为 None(未定义,含 0 笔交易)",
    "max_drawdown 基于净值曲线逐笔复利;持续负期望下曲线衰减趋零、回撤饱和至 ~100%,经济含义以 expectancy/profit_factor 为准",
    "逐笔明细不落盘,仅聚合 summary 落盘(避免全市场基准 D 大量交易膨胀 JSON)",
]


def simulate_trade(d, bar, holding_days, stop_pct, take_pct):
    """单笔交易模拟(保守日内假设)。

    entry = close[bar];窗口 = holding_window(d, bar, holding_days)。
    逐日:open 跳空越过 stop/target 价 -> 按 open 成交;
         否则 low<=stop 且 high>=target 同触 -> 保守取 stop 先;
         否则 low<=stop -> stop 价平;high>=target -> target 价平;
         都未触及 -> 持有到窗口末根 close。

    返回 {entry, exit, exit_reason(hold/stop/target), holding_days_actual,
          ret_gross, max_fav, max_adv} 或 None(无 T+1 可交易)。
    """
    try:
        entry = float(d["close"].iloc[bar])
    except (TypeError, ValueError, IndexError):
        return None
    if entry <= 0:
        return None
    w = bt.holding_window(d, bar, holding_days)
    if w is None:
        return None
    stop_price = entry * (1.0 + stop_pct) if stop_pct is not None else None
    target_price = entry * (1.0 + take_pct) if take_pct is not None else None

    max_fav = max(w["high"]) / entry - 1.0
    max_adv = min(w["low"]) / entry - 1.0

    exit_price = None
    exit_reason = "hold"
    held = w["n"]
    for k in range(w["n"]):
        o = w["open"][k]; hi = w["high"][k]; lo = w["low"][k]; c = w["close"][k]
        if stop_price is not None and o <= stop_price:
            exit_price, exit_reason, held = o, "stop", k + 1
            break
        if target_price is not None and o >= target_price:
            exit_price, exit_reason, held = o, "target", k + 1
            break
        if stop_price is not None and lo <= stop_price:
            exit_price, exit_reason, held = stop_price, "stop", k + 1
            break
        if target_price is not None and hi >= target_price:
            exit_price, exit_reason, held = target_price, "target", k + 1
            break
    if exit_price is None:
        exit_price = w["close"][-1]

    return {
        "entry": entry,
        "exit": exit_price,
        "exit_reason": exit_reason,
        "holding_days_actual": held,
        "ret_gross": exit_price / entry - 1.0,
        "max_fav": max_fav,
        "max_adv": max_adv,
    }


def summarize(trades, cost_bps):
    """逐笔聚合风险收益指标(净收益扣双边 2*cost_bps)。

    返回 {n_trades, win_rate, avg_win, avg_loss, profit_factor, expectancy,
          max_drawdown, avg_max_fav, avg_max_adv, mean_ret_gross}。
    trades 须按时间序传入(净值曲线按此序)。"""
    cost = 2.0 * cost_bps / 10000.0
    nets = [t["ret_gross"] - cost for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    favs = [t["max_fav"] for t in trades]
    advs = [t["max_adv"] for t in trades]
    gross = [t["ret_gross"] for t in trades]
    n = len(nets)

    gross_win = sum(wins)
    gross_loss = sum(abs(x) for x in losses)

    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for x in nets:
        eq *= (1.0 + x)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)

    return {
        "n_trades": n,
        "win_rate": len(wins) / n if n else None,
        "avg_win": float(np.mean(wins)) if wins else None,
        "avg_loss": float(np.mean(losses)) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "expectancy": float(np.mean(nets)) if n else None,
        "max_drawdown": mdd if n else None,
        "avg_max_fav": float(np.mean(favs)) if favs else None,
        "avg_max_adv": float(np.mean(advs)) if advs else None,
        "mean_ret_gross": float(np.mean(gross)) if gross else None,
    }


def run(data_dir, sector_map_path, holding_days=3, stop_pct=-0.08,
        take_pct=None, cost_bps=20):
    """逐评估日选篮 -> 每篮逐笔模拟 -> 逐篮 summary。

    返回 {config, data_range, window, n_eval, step, baskets: {name: {trades, summary}}}。
    选篮与 backtest.run 完全一致(同 seed、同 start/step),篮 C 采样可复现。"""
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    sector_members = bt.build_sector_members(sector_map, universe)
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - bt.EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)

    score_cache = {}

    def get_score(code, bar):
        key = (code, bar)
        if key not in score_cache:
            score_cache[key] = bt.score_at(universe[code], bar, bt.AFTER_CLOSE)
        return score_cache[key]

    rng = np.random.default_rng(0)  # 与 backtest 同 seed,篮 C 采样一致
    trades = {name: [] for name in ("A", "E_hi", "E_lo", "B", "C", "D")}
    n_eval = 0

    for i in range(start, len(all_days) - 1, step):
        heat = bt.sector_heat(universe, sector_members, pos_of, all_days, i)
        if not heat:
            continue
        hot = sorted(heat, key=heat.get, reverse=True)[:bt.TOP_SECTORS]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        baskets = bt.select_baskets(sector_members, pos_of, all_days, i,
                                    buyable, hot, get_score, start, rng)
        dt = all_days[i]
        for name, codes_ in baskets.items():
            if codes_ is None:
                continue
            for c in codes_:
                bar = pos_of[c].get(dt)
                if bar is None:
                    continue
                t = simulate_trade(universe[c], bar, holding_days, stop_pct, take_pct)
                if t is not None:
                    trades[name].append({"code": c, "date": dt, **t})
        n_eval += 1

    baskets = {name: {"trades": trades[name], "summary": summarize(trades[name], cost_bps)}
               for name in trades}
    return {
        "config": {"holding_days": holding_days, "stop_pct": stop_pct,
                   "take_pct": take_pct, "cost_bps": cost_bps},
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "window": {"start": str(all_days[start]), "end": str(all_days[len(all_days) - 2])},
        "n_eval": n_eval,
        "step": step,
        "baskets": baskets,
        "notes": NOTES,
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
    return None if (math.isnan(f) or math.isinf(f)) else f


def build_report(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    baskets = {}
    for name, b in results["baskets"].items():
        s = b["summary"]
        baskets[name] = {
            "n_trades": s["n_trades"],
            "win_rate": _num(s["win_rate"]),
            "avg_win": _num(s["avg_win"]),
            "avg_loss": _num(s["avg_loss"]),
            "profit_factor": _num(s["profit_factor"]),
            "expectancy": _num(s["expectancy"]),
            "max_drawdown": _num(s["max_drawdown"]),
            "avg_max_fav": _num(s["avg_max_fav"]),
            "avg_max_adv": _num(s["avg_max_adv"]),
            "mean_ret_gross": _num(s["mean_ret_gross"]),
        }
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "config": results["config"],
        "data_range": results["data_range"],
        "window": results["window"],
        "n_eval": results["n_eval"],
        "step": results["step"],
        "baskets": baskets,
        "notes": results.get("notes", NOTES),
    }


def render_markdown(payload):
    def pct(x, nd=3):
        return "-" if x is None else f"{x * 100:.{nd}f}%"

    def num(x, nd=3):
        return "-" if x is None else f"{x:.{nd}f}"

    lines = ["# 交易模拟 + 风险收益评价报告", ""]
    lines.append(f"- system_version: `{payload['system_version']}`")
    lines.append(f"- module_version: {payload['module_version']}")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    lines.append(f"- 评估窗口: {payload['window']['start']} -> {payload['window']['end']} "
                 f"(n_eval={payload['n_eval']}, step={payload['step']})")
    cfg = payload["config"]
    lines.append(f"- 配置: holding_days={cfg['holding_days']}, stop_pct={cfg['stop_pct']}, "
                 f"take_pct={cfg['take_pct']}, cost_bps={cfg['cost_bps']}")
    lines += ["", "## 逐篮交易汇总(净收益扣双边成本)", ""]
    lines.append("| 篮子 | n | 胜率 | 盈亏比 | 期望 | 最大回撤 | 平均盈利 | 平均亏损 | 平均最高 | 平均回撤 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name in ("A", "E_hi", "E_lo", "B", "C", "D"):
        b = payload["baskets"].get(name)
        if b is None:
            continue
        lines.append(
            f"| {name} | {b['n_trades']} | {pct(b['win_rate'], 1)} | {num(b['profit_factor'])} | "
            f"{pct(b['expectancy'])} | {pct(b['max_drawdown'])} | {pct(b['avg_win'])} | "
            f"{pct(b['avg_loss'])} | {pct(b['avg_max_fav'])} | {pct(b['avg_max_adv'])} |")
    lines += ["", "## 声明与假设", ""]
    for n in payload["notes"]:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="交易模拟 + 风险收益评价(逐笔,§20/§21)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--holding-days", type=int, default=3)
    parser.add_argument("--stop-pct", type=float, default=-0.08)
    parser.add_argument("--take-pct", type=float, default=None)
    parser.add_argument("--cost-bps", type=float, default=20)
    parser.add_argument("--out", default="trade_sim.json")
    args = parser.parse_args(argv)

    try:
        results = run(args.data_dir, args.sector_map, holding_days=args.holding_days,
                      stop_pct=args.stop_pct, take_pct=args.take_pct, cost_bps=args.cost_bps)
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
