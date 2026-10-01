# -*- coding: utf-8 -*-
"""波动率目标仓位 + 主题动量研究回测(top2)。

动机(来自对抗复核):回撤的主机制是"抛物线顶部满仓持有"。用「缩仓」而非「清仓」——
每月末按动量选 top2 主题,每主题仓位 = clip(长周期vol / 短周期vol, floor, 1) 的等权份,
短周期波动飙升时自动降仓、剩余持现金。对比 market_stop 的二元空仓更平滑。

规则(时间轴已修正；原始价与历史股票池仍待验证):
  - 信号日 = 每月最后交易日,收盘决策,次日 open 执行。
  - 动量:各主题核心组过去 KPAST(=63 交易日)等权 close 收益,选 top2。
  - 仓位:每主题 base = clip(rolling250 std / rolling20 std, 0, 1),floor 兜底;
    组合权重 = base/2,现金 = 1 - 总权重。
  - 成本:单边 0.2%,按实际逐股买卖额收取；持有不动不收费。

输出:_analysis/theme_vol_strategy.json + 终端月度动作表(含仓位)。
"""
import json
import hashlib
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.theme_strategy import (load_regime, build_theme_series, build_theme_baskets, theme_navs,
                                     month_end_flags, THEMES, NAME, KPAST, START, DATA_DIR, REGIME_JSON)
from core.stage1_data import DATA_ROOT as STAGE1_DATA_ROOT, formal_gates, sha256

TOPN = 2
LONG_VOL = 250   # 长周期波动窗口(交易日)
SHORT_VOL = 20   # 短周期波动窗口
FLOOR = 0.2      # 单主题 base 下限(相对其 1/N 全额)
COST_SIDE = 0.002


def _sha256_files(paths):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(os.path.basename(path).encode("utf-8"))
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def vol_base(close):
    """每主题 base 权重 Series = clip(long_std / short_std, 0, 1),数据不足填 1(满仓)。"""
    w = {}
    for name, s in close.items():
        r = s.pct_change()
        short = r.rolling(SHORT_VOL).std()
        long_ = r.rolling(LONG_VOL).std()
        w[name] = (long_ / short.replace(0.0, np.nan)).clip(lower=0.0, upper=1.0).fillna(1.0)
    return w


def run(close, open_, navs, days, vw, topn=TOPN, floor=FLOOR, baskets=None):
    """收盘信号、次日开盘成交、当日收盘估值的逐股现金/股数账本。

    baskets 缺省时把每个主题当作单一可交易指数，仅供小样本测试；
    正式回测必须传入 build_theme_baskets 的逐股价格。
    """
    if baskets is None:
        baskets = {"open": {t: pd.DataFrame({t: s}) for t, s in open_.items()},
                   "close": {t: pd.DataFrame({t: s}) for t, s in close.items()}}
    opens = {code: frame[code] for frame in baskets["open"].values() for code in frame}
    closes = {code: frame[code] for frame in baskets["close"].values() for code in frame}
    members = {t: list(frame.columns) for t, frame in baskets["open"].items()}
    start_idx = next(i for i, day in enumerate(days) if day >= pd.Timestamp(START))
    signals = month_end_flags(days)
    cash, shares, pending = 1.0, {}, None
    last_close = {}
    daily, monthly, trades = [], [], []

    def quote(series, i):
        value = float(series.iloc[i])
        return value if np.isfinite(value) and value > 0 else None

    def execute(target, i):
        nonlocal cash, shares
        codes = set(shares)
        for theme in target:
            codes.update(members[theme])
        prices = {code: quote(opens[code], i) for code in codes}
        if any(price is None for price in prices.values()):
            return False
        before = cash + sum(qty * prices[code] for code, qty in shares.items())
        weights = {}
        for theme, weight in target.items():
            for code in members[theme]:
                weights[code] = weights.get(code, 0.0) + weight / len(members[theme])
        after = before
        for _ in range(12):
            new_shares = {code: after * weight / prices[code] for code, weight in weights.items()}
            traded = sum(abs(new_shares.get(code, 0.0) - shares.get(code, 0.0)) * prices[code]
                         for code in codes)
            updated = before - COST_SIDE * traded
            if abs(updated - after) < 1e-12:
                after = updated
                break
            after = updated
        new_shares = {code: after * weight / prices[code] for code, weight in weights.items()}
        traded = sum(abs(new_shares.get(code, 0.0) - shares.get(code, 0.0)) * prices[code]
                     for code in codes)
        cost = COST_SIDE * traded
        cash = before - cost - sum(qty * prices[code] for code, qty in new_shares.items())
        shares = new_shares
        trades.append({"date": str(days[i].date()), "turnover": round(traded / before, 6),
                       "cost": round(cost, 8), "cash": round(cash, 8),
                       "shares": {c: round(q, 8) for c, q in shares.items()}})
        return True

    for i in range(start_idx, len(days)):
        # 前一交易日收盘生成的指令，只能在本日开盘执行。
        if pending is not None:
            if not execute(pending, i):
                trades.append({"date": str(days[i].date()), "status": "blocked_price"})
            pending = None
        for code in shares:
            price = quote(closes[code], i)
            if price is not None:
                last_close[code] = price
        equity = cash + sum(qty * last_close.get(code, quote(opens[code], i) or 0.0)
                            for code, qty in shares.items())
        daily.append(float(equity))

        if i in signals:
            past = {}
            for name in navs:
                if i < KPAST:
                    continue
                now, before = float(navs[name].iloc[i]), float(navs[name].iloc[i - KPAST])
                if np.isfinite(now) and np.isfinite(before) and before > 0:
                    past[name] = now / before - 1.0
            top = sorted(past, key=past.get, reverse=True)[:topn]
            base = {t: max(floor, float(vw[t].iloc[i])) for t in top}
            target = {t: base[t] / topn for t in top}
            pending = target
            monthly.append({"date": str(days[i].date()), "execution": "next_open",
                            "execution_date": str(days[i + 1].date()) if i + 1 < len(days) else None,
                            "themes": top, "total_pos": round(sum(target.values()), 4),
                            "weights": {t: round(w, 4) for t, w in target.items()},
                            "past_ret": {t: round(past[t], 4) for t in top}})

    eq = np.asarray(daily)
    peaks = np.maximum.accumulate(eq)
    drawdown = eq / peaks - 1
    trough = int(np.argmin(drawdown))
    peak_i = int(np.argmax(eq[:trough + 1]))
    years = (days[-1] - days[start_idx]).days / 365.25
    avg_pos = float(np.mean([m["total_pos"] for m in monthly])) if monthly else 0.0
    return {"final_nav": float(eq[-1]), "total": float(eq[-1] - 1),
            "cagr": float(eq[-1] ** (1 / max(years, 0.1)) - 1),
            "mdd": float(drawdown[trough]),
            "dd_peak": str(days[start_idx + peak_i].date()),
            "dd_trough": str(days[start_idx + trough].date()),
            "avg_pos": avg_pos, "n_trades": len(trades),
            "trades": trades, "monthly": monthly, "daily_equity": daily}


def main():
    days, labels = load_regime()
    close, open_ = build_theme_series(days)
    baskets = build_theme_baskets(days)
    navs = theme_navs(close)
    vw = vol_base(close)

    # 满仓对照(vw 全 1)验证口径与主策略 top2 一致
    full_vw = {t: pd.Series(1.0, index=vw[t].index) for t in vw}
    r_full = run(close, open_, navs, days, full_vw, baskets=baskets)
    r_vol = run(close, open_, navs, days, vw, baskets=baskets)
    print("=== 修正口径:逐股资金权重、股数、成本与次日执行 ===")
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

    print(f"\n=== 最近一次历史信号(截至数据末 {days[-1].date()}) ===")
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
        "data_as_of": str(days[-1].date()), "accounting_version": "shares-cash-v1",
        "baseline_status": "exploratory",
        "input_readiness": formal_gates(STAGE1_DATA_ROOT, START, str(days[-1].date()),
                                         sorted({code for group in THEMES.values() for code in group})),
        "stage1_audit_sha256": sha256(STAGE1_DATA_ROOT / "audit.json")
        if (STAGE1_DATA_ROOT / "audit.json").exists() else None,
        "price_basis": "daily_pkl_unverified_adjustment",
        "input_sha256": _sha256_files([os.path.join(DATA_DIR, c + ".pkl")
                                       for codes in THEMES.values() for c in codes] + [REGIME_JSON]),
        "code_sha256": _sha256_files([__file__, os.path.join(os.path.dirname(__file__), "theme_strategy.py")]),
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
