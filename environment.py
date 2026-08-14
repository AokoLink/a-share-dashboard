# -*- coding: utf-8 -*-
"""市场环境分类:七态(牛/熊/震荡/恐慌/高潮/退潮/恢复)决策树。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
合成市场指数 = universe 中位日收益复合,非真实指数(口径差异见 spec §3)。
分类纯因果:第 i 日标签只依赖 <= i 的数据。
"""

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
            or ld >= PANIC_LIMIT_DOWN:
        return "恐慌"
    if not _miss(r5) and r5 >= CLIMAX_R5 and (
            (not _miss(up) and up >= CLIMAX_UP_RATIO) or lu >= CLIMAX_LIMIT_UP
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
