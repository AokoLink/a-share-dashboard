# -*- coding: utf-8 -*-
"""市场环境分类:七态(牛/熊/震荡/恐慌/高潮/退潮/恢复)决策树。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
合成市场指数 = universe 中位日收益复合,非真实指数(口径差异见 spec §3)。
分类纯因果:第 i 日标签只依赖 <= i 的数据。
"""

import numpy as np
import pandas as pd

import analysis as an

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
            th = an.limit_threshold(c)
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
    df["turnover_ratio"] = df["turnover"] / df["turnover"].shift(1).rolling(5).mean()
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
    df["environment"] = df.apply(classify, axis=1)
    return df
