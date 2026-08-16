# -*- coding: utf-8 -*-
"""波动率目标仓位 + 主题动量 正式策略(top2)。

动机(来自对抗复核):回撤的主机制是"抛物线顶部满仓持有"。用「缩仓」而非「清仓」——
每月末按动量选 top2 主题,每主题仓位 = clip(长周期vol / 短周期vol, floor, 1) 的等权份,
短周期波动飙升时自动降仓、剩余持现金。对比 market_stop 的二元空仓更平滑。

规则(全部因果):
  - 信号日 = 每月最后交易日,收盘决策,次日 open 执行。
  - 动量:各主题核心组过去 KPAST(=63 交易日)等权 close 收益,选 top2。
  - 仓位:每主题 base = clip(rolling250 std / rolling20 std, 0, 1),floor 兜底;
    组合权重 = base/2,现金 = 1 - 总权重。
  - 成本:双边 0.4%,按换手 |Δ权重| 收取(0.2% × sum|Δw|),持有不动不收费。

输出:_analysis/theme_vol_strategy.json + 终端月度动作表(含仓位)。
"""
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.theme_strategy import (load_regime, build_theme_series, theme_navs,
                                     month_end_flags, THEMES, NAME, KPAST, START)

TOPN = 2
LONG_VOL = 250   # 长周期波动窗口(交易日)
SHORT_VOL = 20   # 短周期波动窗口
FLOOR = 0.2      # 单主题 base 下限(相对其 1/N 全额)
COST_SIDE = 0.002


def vol_base(close):
    """每主题 base 权重 Series = clip(long_std / short_std, 0, 1),数据不足填 1(满仓)。"""
    w = {}
    for name, s in close.items():
        r = s.pct_change()
        short = r.rolling(SHORT_VOL).std()
        long_ = r.rolling(LONG_VOL).std()
        w[name] = (long_ / short.replace(0.0, np.nan)).clip(lower=0.0, upper=1.0).fillna(1.0)
    return w


def run(close, open_, navs, days, vw, topn=TOPN, floor=FLOOR):
    n = len(days)
    start_idx = next(i for i in range(n) if days[i] >= pd.Timestamp(START))
    end_idx = n - 1
    signals = month_end_flags(days)

    equity = 1.0
    daily = []
    monthly = []
    trades = []
    pos = None  # {themes, w:dict theme->组合权重, entry_open:dict, entry_equity, entry_idx}

    def mark(pos_, k):
        """持仓在日 k 的市值(现金 + 各主题市值)。"""
        e = pos_["entry_equity"]
        s = 0.0
        for t, wt in pos_["w"].items():
            o0 = pos_["entry_open"][t]
            o1 = open_[t].iloc[k]
            if o0 == o0 and o1 == o1 and o0 > 0:
                s += wt * (o1 / o0 - 1.0)
        return e * (1.0 + s)

    for i in range(start_idx, end_idx):
        if i in signals:
            past = {}
            for name in navs:
                lv = navs[name].iloc[i]
                lv0 = navs[name].iloc[i - KPAST] if i - KPAST >= 0 else lv
                past[name] = lv / lv0 - 1
            top = sorted(past, key=past.get, reverse=True)[:topn]
            base = {t: max(floor, float(vw[t].iloc[i])) for t in top}
            new_w = {t: base[t] / topn for t in top}
            # 换仓:按 open[i+1] 结算旧仓、收换手成本、重建新仓
            if pos is None:
                old_w = {}
                equity_before = equity
            else:
                old_w = pos["w"]
                equity_before = mark(pos, i + 1)
            turnover = sum(abs(new_w.get(t, 0.0) - old_w.get(t, 0.0))
                           for t in set(new_w) | set(old_w))
            equity = equity_before * (1.0 - COST_SIDE * turnover)
            entry_open = {}
            for t in top:
                o = open_[t].iloc[i + 1]
                entry_open[t] = float(o) if o == o else np.nan
            pos = {"themes": top, "w": new_w, "entry_open": entry_open,
                   "entry_equity": equity, "entry_idx": i}
            if turnover > 1e-9:
                trades.append({"date": str(days[i].date()), "themes": top,
                               "turnover": round(float(turnover), 3)})
            monthly.append({
                "date": str(days[i].date()),
                "themes": top,
                "total_pos": round(float(sum(new_w.values())), 4),
                "weights": {t: round(wt, 4) for t, wt in new_w.items()},
                "past_ret": {t: round(float(past[t]), 4) for t in top},
            })
        if pos is not None:
            e = mark(pos, i)
        else:
            e = equity
        daily.append(float(e))

    # 期末按最后一日 open 平仓
    if pos is not None:
        equity = mark(pos, end_idx) * (1.0 - COST_SIDE * sum(pos["w"].values()))

    eq = np.array(daily)
    run_peak = np.maximum.accumulate(eq)
    dd = eq / run_peak - 1
    trough = int(np.argmin(dd))
    peak_i = int(np.argmax(eq[:trough + 1]))
    mdd = float(dd[trough])
    yrs = (days[end_idx] - days[start_idx]).days / 365.25
    avg_pos = float(np.mean([m["total_pos"] for m in monthly]))
    return {"final_nav": float(equity), "total": float(equity - 1),
            "cagr": float(equity ** (1 / max(yrs, 0.1)) - 1), "mdd": mdd,
            "dd_peak": str(days[start_idx + peak_i].date()),
            "dd_trough": str(days[start_idx + trough].date()),
            "avg_pos": avg_pos, "n_trades": len(trades),
            "trades": trades, "monthly": monthly, "daily_equity": eq.tolist()}


def main():
    days, labels = load_regime()
    close, open_ = build_theme_series(days)
    navs = theme_navs(close)
    vw = vol_base(close)

    # 满仓对照(vw 全 1)验证口径与主策略 top2 一致
    full_vw = {t: pd.Series(1.0, index=vw[t].index) for t in vw}
    r_full = run(close, open_, navs, days, full_vw)
    r_vol = run(close, open_, navs, days, vw)
    print("=== 口径校验:满仓 top2 应 ≈ 主策略 top2(净值20.28 / 回撤-41.5%) ===")
    print(f"  满仓top2(本模块): 总 {r_full['total']*100:+.1f}%  回撤 {r_full['mdd']*100:.1f}%  平均仓位 {r_full['avg_pos']*100:.0f}%")
    print(f"  vol-target top2:   总 {r_vol['total']*100:+.1f}%  回撤 {r_vol['mdd']*100:.1f}%  平均仓位 {r_vol['avg_pos']*100:.0f}%")
    print(f"  vol-target 回撤区间: {r_vol['dd_peak']} -> {r_vol['dd_trough']}")

    print(f"\n=== 月度动作表(vol-target top2, 2022-01 起) ===")
    print(f"{'日期':12s} {'总仓位':>5s}  主题(个股)与各自仓位")
    for m in r_vol["monthly"]:
        parts = []
        for t, wt in m["weights"].items():
            stocks = "、".join(NAME.get(c, c) for c in THEMES[t])
            parts.append(f"{t} {wt*100:.0f}%({stocks})")
        print(f"  {m['date']:12s} {m['total_pos']*100:4.0f}%  {' + '.join(parts)}")

    print(f"\n=== 最近一次信号(截至数据末 2026-08-14) ===")
    last = r_vol["monthly"][-1]
    parts = []
    for t, wt in last["weights"].items():
        stocks = "、".join(NAME.get(c, c) for c in THEMES[t])
        parts.append(f"{t}({stocks}) 仓位 {wt*100:.0f}%")
    print(f"  信号日 {last['date']}:总仓位 {last['total_pos']*100:.0f}%, 现金 {100-last['total_pos']*100:.0f}%")
    for p in parts:
        print(f"    - {p}")

    out = {
        "generated_at": datetime.now().isoformat(),
        "start": START, "kpast": KPAST, "topn": TOPN,
        "long_vol": LONG_VOL, "short_vol": SHORT_VOL, "floor": FLOOR, "cost_side": COST_SIDE,
        "themes": {n: {"core": v, "names": [NAME.get(c, c) for c in v]} for n, v in THEMES.items()},
        "full_baseline": {k: v for k, v in r_full.items() if k not in ("trades", "monthly", "daily_equity")},
        "vol_target": {k: v for k, v in r_vol.items() if k not in ("trades", "monthly", "daily_equity")},
        "monthly": r_vol["monthly"],
        "latest": last,
    }
    with open(os.path.join("_analysis", "theme_vol_strategy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\nwrote _analysis/theme_vol_strategy.json")


if __name__ == "__main__":
    main()
