# -*- coding: utf-8 -*-
"""regime 门控的宽基抄底策略回测:100k 本金,逐月/逐笔盈亏 + 净值曲线。

策略(项目里**唯一被回测验证为正**的 edge,见 regime-gate-momentum-inverted):
- 恐慌日(regime==恐慌,纯因果、只用 <=i 数据)在 T+1 开盘买入「全市场等权篮」(宽基代理);
- 持有 H 天,T+1+H 开盘卖出;
- 其余状态(牛/熊/震荡/高潮/退潮/恢复)空仓,不进场;
- 单仓位、不复利加仓、无杠杆。

严格口径:
- 买卖只读 >i 行情(信号收盘后生成,次日开盘成交),无未来泄露;
- 净收益 = 篮等权 open->open 收益 - 双边成本 COST;
- 篮 = 当日 build_buyable 过滤后(剔涨停/<=-7%/流动性不足)的等权市场,宽基代理。

只读 _analysis/daily/*.pkl 与 code2sector.json,不 fetch。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

from core import backtest as bt
from core import environment as env
from core import forward as fw

MODULE_VERSION = "1.0.0"

INITIAL_CAPITAL = 100_000.0
COST = 0.004                      # 双边成本(买卖各约 0.2%)
HOLD = 2                          # 默认持有交易日数(买 T+1 开盘 -> 卖 T+1+H 开盘)
MIN_BASKET_N = 50                 # 篮内有效样本低于此数视为不可交易


def _basket_return(universe, pos_of, all_days, i, h):
    """信号日 i 的宽基篮 open->open 净毛收益(买 T+1 开盘卖 T+1+h 开盘,等权)。"""
    buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
    dt = all_days[i]
    rets = []
    for c in buyable:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        r = fw.fwd_oo(universe[c], bar, h)
        if r is not None:
            rets.append(r)
    if len(rets) < MIN_BASKET_N:
        return None, 0
    return float(np.mean(rets)), len(rets)


def run(data_dir, sector_map_path, start_date="2026-01-01", end_date=None,
        hold=HOLD, cost=COST):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)   # tail_n=1200 默认,足够 regime 预热
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    series = env.build_series(universe, pos_of, all_days)
    label_of = {str(dt): lab for dt, lab in zip(series["date"], series["environment"])}
    # 合成指数 M(中位日收益复合)同期涨跌 = 市场基准(非可交易,仅供对照)
    m_of = {str(dt): m for dt, m in zip(series["date"], series["M"])}

    end_date = end_date or str(all_days[-1])
    # 信号日 = 在 [start,end] 内、且 T+1+H 仍可成交(需 H+1 根未来行情)
    win_idx = [i for i in range(len(all_days))
               if start_date <= str(all_days[i]) <= end_date]

    # regime 分布(窗口内,供理解市场语境)
    reg_dist = {}
    for i in win_idx:
        lab = label_of.get(str(all_days[i]))
        reg_dist[lab] = reg_dist.get(lab, 0) + 1

    capital = INITIAL_CAPITAL
    trades = []
    in_pos = False
    exit_i = -1
    for i in win_idx:
        dt = str(all_days[i])
        lab = label_of.get(dt)
        if in_pos:
            if i >= exit_i:
                in_pos = False
            else:
                continue
        if lab != "恐慌":
            continue
        # 恐慌进场:需 T+1+H 可成交
        if i + 1 + hold >= len(all_days):
            continue
        gross, n_stocks = _basket_return(universe, pos_of, all_days, i, hold)
        if gross is None:
            continue
        net = gross - cost
        entry_date = str(all_days[i + 1])          # 实际成交日 = T+1 开盘
        exit_date = str(all_days[i + 1 + hold])     # 实际卖出日 = T+1+H 开盘
        cap_before = capital
        capital = capital * (1.0 + net)
        trades.append({
            "signal_date": dt, "entry_date": entry_date, "exit_date": exit_date,
            "hold_days": hold, "n_stocks": n_stocks,
            "gross": round(gross, 6), "net": round(net, 6),
            "capital_before": round(cap_before, 2),
            "capital_after": round(capital, 2),
            "pnl": round(cap_before * net, 2),
        })
        in_pos = True
        exit_i = i + 1 + hold

    # 逐月盈亏(按卖出月实现)+ 月末净值
    monthly = []
    months = sorted({t["exit_date"][:7] for t in trades})
    if months:
        first_month = months[0]
        last_month = max(m[:7] for m in months)
    # 也覆盖窗口内所有自然月(含无交易的月份)
    all_months = sorted({str(all_days[i])[:7] for i in win_idx})
    running = INITIAL_CAPITAL
    monthly_by_exit = {}
    for t in trades:
        m = t["exit_date"][:7]
        monthly_by_exit[m] = monthly_by_exit.get(m, 0.0) + t["pnl"]
    for m in all_months:
        pnl_m = monthly_by_exit.get(m, 0.0)
        n_m = sum(1 for t in trades if t["exit_date"][:7] == m)
        running += pnl_m
        monthly.append({
            "month": m, "n_trades": n_m, "pnl": round(pnl_m, 2),
            "equity": round(running, 2),
        })

    # 基准 1(苹果比苹果):等权篮买入持有整窗(买首日 T+1 开盘、卖末日开盘,同篮口径)
    bh_gross, bh_n = _basket_return(universe, pos_of, all_days,
                                    win_idx[0], win_idx[-1] - win_idx[0] - 1)
    # 基准 2(参考):合成指数 M = 中位日收益复合,代表「典型股票」,不可交易,与均值篮不可比
    m0 = m_of.get(str(all_days[win_idx[0]]))
    m1 = m_of.get(str(all_days[win_idx[-1]]))
    benchmark = {
        "buyhold_gross": round(bh_gross, 6) if bh_gross is not None else None,
        "buyhold_n": bh_n,
        "index_m": round((m1 / m0 - 1.0) if (m0 and m1) else None, 6),
        "note": "buyhold=等权篮买入持有(同策略口径,苹果比苹果);index_m=中位日收益复合(典型股票,不可交易,与均值篮不可直接比)",
    }

    total_ret = capital / INITIAL_CAPITAL - 1.0
    return {
        "config": {"initial_capital": INITIAL_CAPITAL, "hold": hold, "cost": cost,
                   "min_basket_n": MIN_BASKET_N, "start_date": start_date,
                   "end_date": end_date},
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "window": {"start": str(all_days[win_idx[0]]), "end": str(all_days[win_idx[-1]]),
                   "n_days": len(win_idx)},
        "regime_distribution": {str(k): v for k, v in reg_dist.items()},
        "n_signals": len(trades),
        "final_capital": round(capital, 2),
        "total_return": round(total_ret, 6),
        "benchmark": benchmark,
        "trades": trades,
        "monthly": monthly,
    }


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_report(results, system_version=None, generated_at=None):
    return {
        "system_version": system_version or _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": generated_at or datetime.now().isoformat(),
        "config": results["config"],
        "data_range": results["data_range"],
        "window": results["window"],
        "regime_distribution": results["regime_distribution"],
        "n_signals": results["n_signals"],
        "final_capital": results["final_capital"],
        "total_return": results["total_return"],
        "benchmark": results["benchmark"],
        "trades": results["trades"],
        "monthly": results["monthly"],
        "notes": [
            "策略 = 恐慌日买入全市场等权篮(T+1 开盘),持有 H 天(T+1+H 开盘卖),其余空仓;这是项目唯一被回测验证为正的 edge(全历史 26 笔,胜率 80.8%,净均值 +1.49%,本身样本小)。",
            "篮 = build_buyable 过滤后的等权市场,是宽基代理,非真实指数 ETF;等权均值口径(均值>中位,正偏)。",
            "净收益 = 篮 open->open 毛收益 - 双边成本 COST(0.4%);单仓位、全仓进出、无杠杆、不复利加仓。",
            "⚠ 幸存者偏差:数据只含存续到数据末日的股票,信号日可买但之后退市/长期停牌的股票不在数据里,其(坏的)前向收益不进篮均值 → 策略与 buyhold 的收益都被系统性抬高(策略抬得更多)。",
            "⚠ 口径:策略篮用均值,buyhold 是苹果比苹果的对照;index_m 是中位日收益复合(典型股票),与均值篮不可直接比,勿拿策略跟 index_m 相减。",
            "⚠ n=1:本窗口通常只有 0~1 个恐慌日,结果仅描述性,不代表可复现 edge;全历史有效样本仅 26 笔。",
            "⚠ 可执行性:等权买 N 只小盘,10 万本金每股 ~10万/N 元,多数低于 1 手(100 股)门槛,小资金实盘买不出此等权篮;0.4% 双边成本对大额买小盘、恐慌次日追反弹开盘常被低估(滑点/冲击)。",
        ],
    }


def render_markdown(payload):
    def pct(x):
        return "-" if x is None else f"{x * 100:.2f}%"

    L = ["# regime 门控宽基抄底策略回测(100k 本金)", ""]
    L.append(f"- system_version: `{payload['system_version']}` / module_version {payload['module_version']}")
    L.append(f"- 回测窗口: {payload['window']['start']} -> {payload['window']['end']} ({payload['window']['n_days']} 个交易日)")
    cfg = payload["config"]
    L.append(f"- 本金 {cfg['initial_capital']:,.0f};持有 {cfg['hold']} 天;双边成本 {cfg['cost']*100:.1f}%")
    L.append(f"- **期末本金 {payload['final_capital']:,.2f}({pct(payload['total_return'])})**;"
             f"触发信号 {payload['n_signals']} 次")
    b = payload["benchmark"]
    L.append(f"- 对照 1(苹果比苹果)等权篮买入持有整窗: {pct(b['buyhold_gross'])}"
             f"(n={b['buyhold_n']});对照 2 中位股合成指数: {pct(b['index_m'])}(不可交易,口径不同)")
    L += ["", "## 窗口内市场环境分布(regime)", ""]
    for k in sorted(payload["regime_distribution"], key=lambda x: (x is None, str(x))):
        L.append(f"- {k}: {payload['regime_distribution'][k]} 天")
    L += ["", "## 逐月盈亏(按卖出月实现)", "",
          "| 月份 | 笔数 | 盈亏(¥) | 月末本金(¥) |", "|---|---|---|---|"]
    for m in payload["monthly"]:
        L.append(f"| {m['month']} | {m['n_trades']} | {m['pnl']:+,.2f} | {m['equity']:,.2f} |")
    L += ["", "## 逐笔明细", "",
          "| # | 信号日 | 买入日 | 卖出日 | 毛收益 | 净收益 | 盈亏(¥) | 本金后(¥) |",
          "|---|---|---|---|---|---|---|---|"]
    for i, t in enumerate(payload["trades"], 1):
        L.append(f"| {i} | {t['signal_date']} | {t['entry_date']} | {t['exit_date']} "
                 f"| {pct(t['gross'])} | {pct(t['net'])} | {t['pnl']:+,.2f} | {t['capital_after']:,.2f} |")
    L += ["", "## 声明", ""]
    for n in payload["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    parser = argparse.ArgumentParser(description="regime 门控宽基抄底策略回测")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--hold", type=int, default=HOLD)
    parser.add_argument("--cost", type=float, default=COST)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out = args.out or os.path.join("_analysis", "regime_trade.json")
    try:
        results = run(args.data_dir, args.sector_map, start_date=args.start,
                      end_date=args.end, hold=args.hold, cost=args.cost)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = build_report(results)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {out} and {md_path}")
    print(f"final_capital={payload['final_capital']}, total_return={payload['total_return']}, "
          f"n_signals={payload['n_signals']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
