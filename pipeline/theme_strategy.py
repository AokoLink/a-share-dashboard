# -*- coding: utf-8 -*-
"""主题动量 + regime 择时 + 回撤风控 策略回测(核心组)。

目标(用户诉求,须诚实对照,不预设立场):
  1. 把"无脑持有核心龙头"的 -60% 级回撤砍到 ~-30%,同时保住大部分收益。
  2. 每月末输出一个可执行动作:持有哪个主题的哪几只、还是空仓。

规则(全部因果,无未来函数):
  - 信号日 = 每月最后一个交易日,收盘后决策;执行在次日 open。
  - 动量:各主题核心组"过去 63 交易日(≈3 个月)等权 close 收益"排序,选 top-N。
  - regime 门(可配):「高潮/退潮」日 阻挡进场(gate)或 强制出场(exit)。
  - 阶梯止盈 = trailing stop:持仓组合 close 自入场以来峰值回落 > TRAIL 即次日 open 离场。
  - 市场止损(可配):全市场等权指数自「滚动 60 交易日峰值」回落 > X% → 降仓到现金,
    恢复前阻挡进场。用滚动峰值而非历史峰值,否则等权市场 2021 见顶后永久锁死。
  - 成本:双边 0.4%(买 0.2% + 卖 0.2%)。

口径差异(诚实标注):
  - regime 序列复用 _analysis/environment_report.json(已按因果口径预计算);
    此前「regime 仅验证于进场/回避,非退出信号」针对宽基 50 只抄底,
    此处主题篮子上的 regime 出场是独立新检验,结论分开看。
  - 市场指数用「全体 universe 等权日收益复合」(mean_nav),而非 regime 的
    「中位日收益复合 M」——后者是衰减函数(期末 ~0.07),水平无意义,仅短周期形态可用。
"""
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = "_analysis/daily"
REGIME_JSON = "_analysis/environment_report.json"
MARKET_INDEX = "_analysis/market_index.pkl"
START = "2022-01-01"
KPAST = 63          # 动量回看交易日(≈3 个月)
COST_SIDE = 0.002   # 单边成本(双边 0.4%)
EXIT_REGIMES = {"高潮", "退潮"}
MKT_PEAK_LB = 60    # 市场止损的滚动峰值窗口(交易日)

# 核心组(用户星级龙头);只含 2022 前上市者,避免 point-in-time 上市偏。
THEMES = {
    "光模块": ["300308", "300502", "300394"],
    "PCB":    ["002463", "300476", "002384"],
    "封测":   ["600584", "002156"],
    "MLCC":   ["300408", "000636", "002138"],
    "光纤":   ["601869", "600487"],
}
NAME = {
    "300308": "中际旭创", "300502": "新易盛", "300394": "天孚通信",
    "002463": "沪电股份", "300476": "胜宏科技", "002384": "东山精密",
    "600584": "长电科技", "002156": "通富微电",
    "300408": "三环集团", "000636": "风华高科", "002138": "顺络电子",
    "601869": "长飞光纤", "600487": "亨通光电",
}


def load_regime(path=REGIME_JSON):
    with open(path, "r", encoding="utf-8") as f:
        rep = json.load(f)
    days, labels = [], {}
    for row in rep["series"]:
        d = pd.Timestamp(row["date"])
        days.append(d)
        labels[d] = row["environment"]
    days = sorted(days)
    return days, labels


def load_market_mdd(days):
    mk = pd.read_pickle(MARKET_INDEX)
    mk = mk.reindex(pd.DatetimeIndex(days)).ffill()
    mdd = {}
    for proxy in ("mean_nav", "median_nav"):
        nav = mk[proxy]
        peak = nav.rolling(MKT_PEAK_LB, min_periods=1).max()
        mdd[proxy] = (nav / peak - 1.0)
    return mdd


def load_series(code, col, days, forward_fill=True):
    p = os.path.join(DATA_DIR, code + ".pkl")
    if not os.path.exists(p):
        return pd.Series(np.nan, index=days)
    d = pd.read_pickle(p)[["date", col]].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").drop_duplicates("date").set_index("date")
    series = d[col].reindex(days)
    return series.ffill() if forward_fill else series


def build_theme_series(days):
    """用于动量的等初始资金主题指数；不同股价不影响初始权重。"""
    idx = pd.DatetimeIndex(days)
    close, open_ = {}, {}
    for name, codes in THEMES.items():
        c = pd.concat({code: load_series(code, "close", idx) for code in codes}, axis=1)
        o = pd.concat({code: load_series(code, "open", idx) for code in codes}, axis=1)
        # 只在所有成员都有合法价格后建指数；避免缺失成员令权重意外变化。
        valid = (c.gt(0) & o.gt(0)).all(axis=1)
        if not valid.any():
            close[name] = pd.Series(np.nan, index=idx)
            open_[name] = pd.Series(np.nan, index=idx)
            continue
        first = valid[valid].index[0]
        close[name] = c.div(c.loc[first]).mean(axis=1).where(valid)
        open_[name] = o.div(o.loc[first]).mean(axis=1).where(valid)
    return close, open_


def build_theme_baskets(days):
    """逐股价格面板，供股数与现金账本核算；不替缺失开盘价虚构成交。"""
    idx = pd.DatetimeIndex(days)
    return {
        field: {name: pd.concat({code: load_series(code, field, idx, forward_fill=False)
                                  for code in codes}, axis=1)
                for name, codes in THEMES.items()}
        for field in ("open", "close")
    }


def theme_navs(close):
    return {n: (1 + s.pct_change().fillna(0.0)).cumprod() for n, s in close.items()}


def month_end_flags(days):
    flags = set()
    # 数据末日不等于已确认的月末；只有观察到下月交易日才可确认。
    for i in range(len(days) - 1):
        if days[i].month != days[i + 1].month:
            flags.add(i)
    return flags


def port_val(series, themes, i):
    vals = [series[t].iloc[i] for t in themes]
    vals = [v for v in vals if v == v]
    return float(np.mean(vals)) if vals else np.nan


def run_strategy(close, open_, navs, days, labels, topn=1, regime_mode="exit",
                 trail=None, mkt_mdd=None, market_stop=None):
    n = len(days)
    start_idx = next(i for i in range(n) if days[i] >= pd.Timestamp(START))
    end_idx = n - 1
    signals = month_end_flags(days)

    equity = 1.0
    daily_equity = []
    trades = []
    monthly = []
    pos = None

    for i in range(start_idx, end_idx):
        # 先记 i 日开盘时的旧仓权益，再处理收盘后生成、i+1 开盘执行的指令。
        # 此后任何 i+1 行情变化都不能反写 i 日净值。
        if pos is not None:
            cur = port_val(open_, pos["themes"], i)
            e = pos["entry_equity"] * (cur / pos["entry_open"]) if cur == cur else equity
        else:
            e = equity
        daily_equity.append(float(e))
        exited_today = False
        # ---- 1. 出场检查(close of i 决策, open of i+1 执行) ----
        if pos is not None:
            cur_close = port_val(close, pos["themes"], i)
            if cur_close == cur_close:
                pos["peak"] = max(pos["peak"], cur_close)
            reason = None
            if regime_mode == "exit" and labels.get(days[i]) in EXIT_REGIMES:
                reason = "regime"
            elif trail is not None and pos["peak"] > 0 and cur_close < pos["peak"] * (1 - trail):
                reason = "trail"
            elif market_stop is not None and mkt_mdd[market_stop[0]].iloc[i] < -market_stop[1]:
                reason = "mkt"
            if reason is not None:
                xopen = port_val(open_, pos["themes"], i + 1)
                ret = (xopen / pos["entry_open"]) * (1 - COST_SIDE) - 1
                equity = pos["entry_equity"] * (xopen / pos["entry_open"]) * (1 - COST_SIDE)
                trades.append({"themes": pos["themes"], "entry": str(days[pos["entry_idx"]].date()),
                               "exit": str(days[i].date()), "reason": reason,
                               "ret": round(float(ret), 4)})
                pos = None
                exited_today = True
        # ---- 2. 月末 rebalance(收盘决策, 次日 open 执行) ----
        if i in signals and not exited_today:
            in_bad = labels.get(days[i]) in EXIT_REGIMES and regime_mode in ("gate", "exit")
            if not in_bad and market_stop is not None and mkt_mdd[market_stop[0]].iloc[i] < -market_stop[1]:
                in_bad = True
            past = {}
            for name in navs:
                lv = navs[name].iloc[i]
                lv0 = navs[name].iloc[i - KPAST] if i - KPAST >= 0 else lv
                past[name] = lv / lv0 - 1
            ranking = sorted(past, key=past.get, reverse=True)
            target = None if in_bad else ranking[:topn]
            if pos is None:
                if target:
                    eopen = port_val(open_, target, i + 1)
                    if eopen == eopen and eopen > 0:
                        pos = {"themes": target, "entry_open": float(eopen),
                               "entry_equity": equity * (1 - COST_SIDE),
                               "peak": float(eopen), "entry_idx": i}
            elif target and sorted(target) != sorted(pos["themes"]):
                oopen = port_val(open_, pos["themes"], i + 1)
                equity = pos["entry_equity"] * (oopen / pos["entry_open"]) * (1 - COST_SIDE)
                trades.append({"themes": pos["themes"], "entry": str(days[pos["entry_idx"]].date()),
                               "exit": str(days[i].date()), "reason": "switch",
                               "ret": round(float((oopen / pos["entry_open"]) * (1 - COST_SIDE) - 1), 4)})
                nopen = port_val(open_, target, i + 1)
                pos = {"themes": target, "entry_open": float(nopen),
                       "entry_equity": equity * (1 - COST_SIDE),
                       "peak": float(nopen), "entry_idx": i}
            held = pos["themes"] if pos else (target or [])
            monthly.append({
                "date": str(days[i].date()),
                "action": ("flat" if not held else
                           ("hold" if pos and sorted(held) == sorted(pos["themes"]) else
                            ("enter" if not pos else "switch"))),
                "themes": held,
                "top3_mom": ranking[:3],
                "past_ret": {k: round(float(past[k]), 4) for k in ranking[:3]},
            })
    if pos is not None:
        xopen = port_val(open_, pos["themes"], end_idx)
        equity = pos["entry_equity"] * (xopen / pos["entry_open"]) * (1 - COST_SIDE)
        trades.append({"themes": pos["themes"], "entry": str(days[pos["entry_idx"]].date()),
                       "exit": str(days[end_idx].date()), "reason": "end",
                       "ret": round(float((xopen / pos["entry_open"]) * (1 - COST_SIDE) - 1), 4)})
    daily_equity.append(float(equity))

    eq = np.array(daily_equity)
    run_peak = np.maximum.accumulate(eq)
    dd = eq / run_peak - 1
    trough = int(np.argmin(dd))
    peak_i = int(np.argmax(eq[:trough + 1]))
    mdd = float(dd[trough])
    yrs = (days[end_idx] - days[start_idx]).days / 365.25
    # 空仓占比(市场止损/门控的有效性)
    flat_frac = float(sum(1 for m in monthly if m["action"] == "flat") / max(len(monthly), 1))
    return {"final_nav": float(equity), "total": float(equity - 1),
            "cagr": float(equity ** (1 / max(yrs, 0.1)) - 1), "mdd": mdd,
            "dd_peak": str(days[start_idx + peak_i].date()),
            "dd_trough": str(days[start_idx + trough].date()),
            "flat_frac": flat_frac, "n_trades": len(trades),
            "trades": trades, "monthly": monthly, "daily_equity": eq.tolist()}


def buy_hold(s, start=START):
    s = s.loc[start:].dropna()
    if len(s) < 2:
        return None
    nav = s / s.iloc[0]
    mdd = float((nav / nav.cummax() - 1).min())
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    return {"total": float(nav.iloc[-1] - 1),
            "cagr": float(nav.iloc[-1] ** (1 / max(yrs, 0.1)) - 1), "mdd": mdd}


def main():
    days, labels = load_regime()
    close, open_ = build_theme_series(days)
    navs = theme_navs(close)
    mkt_mdd = load_market_mdd(days)

    print("=== 基准:买入持有(2022-01 起, close 口径) ===")
    all_core = pd.concat([close[n] for n in THEMES], axis=1).mean(axis=1, skipna=True)
    bh = {"all_core": buy_hold(all_core)}
    for n in THEMES:
        bh[n] = buy_hold(close[n])
    print(f"  5主题核心等权: 总 {bh['all_core']['total']*100:+.1f}%  年化 {bh['all_core']['cagr']*100:+.1f}%  回撤 {bh['all_core']['mdd']*100:.1f}%")
    for n in THEMES:
        b = bh[n]
        print(f"  {n}核心: 总 {b['total']*100:+.1f}%  年化 {b['cagr']*100:+.1f}%  回撤 {b['mdd']*100:.1f}%")

    print("\n=== 策略矩阵(2022-01 起) ===")
    # (label, topn, regime_mode, trail, market_stop)
    combos = [
        ("纯动量top1",              1, "none", None, None),
        ("纯动量top2",              2, "none", None, None),
        ("纯动量top3",              3, "none", None, None),
        ("纯动量top4",              4, "none", None, None),
        ("纯动量top5",              5, "none", None, None),
        ("top1+市场止损15%",         1, "none", None, ("mean_nav", 0.15)),
        ("top2+市场止损15%",         2, "none", None, ("mean_nav", 0.15)),
        ("top3+市场止损15%",         3, "none", None, ("mean_nav", 0.15)),
        ("top1+市场止损10%",         1, "none", None, ("mean_nav", 0.10)),
        ("top1+市场止损20%",         1, "none", None, ("mean_nav", 0.20)),
        ("top2+regime+止盈25%+市场15%", 2, "exit", 0.25, ("mean_nav", 0.15)),
    ]
    results = {}
    for label, topn, rmode, trail, mst in combos:
        r = run_strategy(close, open_, navs, days, labels, topn, rmode, trail, mkt_mdd, mst)
        results[label] = r
        print(f"  [{label:28s}] 净值 {r['final_nav']:6.2f}  总 {r['total']*100:+8.1f}%  "
              f"年化 {r['cagr']*100:+6.1f}%  回撤 {r['mdd']*100:6.1f}%  空仓 {r['flat_frac']*100:4.0f}%  "
              f"({r['dd_peak']}->{r['dd_trough']}) 交易{r['n_trades']}")

    pick_label = "top3+市场止损15%"
    pick = results[pick_label]
    print(f"\n=== 月度动作表({pick_label}) ===")
    act_label = {"flat": "空仓", "hold": "持有", "enter": "进场", "switch": "换仓"}
    for m in pick["monthly"]:
        th = "、".join(m["themes"]) if m["themes"] else "-"
        stocks = "、".join(NAME.get(c, c) for t in m["themes"] for c in THEMES[t]) if m["themes"] else "-"
        print(f"  {m['date']}  {act_label[m['action']]:3s}  {th}  ({stocks})")
    print("\n=== 成交明细(含出场原因) ===")
    for t in pick["trades"]:
        th = "、".join(t["themes"])
        print(f"  {t['entry']} -> {t['exit']}  {th}  原因 {t['reason']:6s}  收益 {t['ret']*100:+7.1f}%")

    out = {
        "generated_at": datetime.now().isoformat(),
        "start": START, "kpast": KPAST, "cost_side": COST_SIDE,
        "exit_regimes": sorted(EXIT_REGIMES), "mkt_peak_lb": MKT_PEAK_LB,
        "themes": {n: {"core": v, "names": [NAME.get(c, c) for c in v]} for n, v in THEMES.items()},
        "buy_hold": bh,
        "strategy": {k: {kk: vv for kk, vv in v.items()
                         if kk not in ("trades", "monthly", "daily_equity")}
                     for k, v in results.items()},
        "pick_label": pick_label,
        "pick": {"trades": pick["trades"], "monthly": pick["monthly"]},
    }
    with open(os.path.join("_analysis", "theme_strategy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print("\nwrote _analysis/theme_strategy.json")


if __name__ == "__main__":
    main()
