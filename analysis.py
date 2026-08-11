# -*- coding: utf-8 -*-
"""分析层:时间口径 + 指标 + 打分。纯函数,输入可注入,便于单测。"""
from datetime import datetime

import pandas as pd

# ---- 时间口径(规格 §7 P1/A/B) ----

AM_START = 9 * 60 + 30
AM_END = 11 * 60 + 30
PM_START = 13 * 60
PM_END = 15 * 60


def trading_minutes_elapsed(now: datetime) -> int:
    m = now.hour * 60 + now.minute
    if m < AM_START:
        return 0
    total = 0
    if m <= AM_END:
        total += m - AM_START
    else:
        total += AM_END - AM_START
    if m >= PM_END:
        total += PM_END - PM_START
    elif m >= PM_START:
        total += m - PM_START
    return total


def is_after_close(now: datetime) -> bool:
    return now.hour >= 15


def is_trading_time(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return AM_START <= m <= PM_END


def day_adjusted_volume(cum_volume: float, elapsed_min: int):
    if elapsed_min <= 0 or elapsed_min < 15:
        return None
    return cum_volume * 240.0 / elapsed_min


def custom_volume_ratio(cum_volume: float, elapsed_min: int, avg_5d_volume):
    if avg_5d_volume is None or avg_5d_volume <= 0:
        return None
    adj = day_adjusted_volume(cum_volume, elapsed_min)
    if adj is None:
        return None
    return adj / avg_5d_volume


# ---- 停牌过滤与涨停近似(规格 §7) ----

def filter_active(df):
    return df[(df["volume"] > 0) & (df["price"] > 0)]


def limit_threshold(code: str) -> float:
    c = str(code)
    if c.startswith(("30", "688")):
        return 19.9
    if c.startswith(("8", "4")):
        return 29.9
    return 9.9


def compute_breadth(spot_df, exclude_codes=None):
    df = filter_active(spot_df)
    if exclude_codes:
        df = df[~df["code"].isin(exclude_codes)]
    if len(df) == 0:
        return {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "total_turnover": 0.0}
    up = int((df["change_pct"] > 0).sum())
    down = int((df["change_pct"] < 0).sum())
    flat = int((df["change_pct"] == 0).sum())
    limit_up = 0
    limit_down = 0
    for r in df.itertuples(index=False):
        th = limit_threshold(r.code)
        if r.change_pct >= th:
            limit_up += 1
        elif r.change_pct <= -th:
            limit_down += 1
    return {"up": up, "down": down, "flat": flat, "limit_up": limit_up,
            "limit_down": limit_down, "total_turnover": float(df["amount"].sum())}


# ---- 指标(规格 §6.3/§7) ----

def add_ma(df, periods=(5, 10, 20, 60)):
    out = df.copy()
    for p in periods:
        out["ma%d" % p] = out["close"].rolling(p).mean()
    return out


def add_macd(df):
    out = df.copy()
    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = 2 * (out["dif"] - out["dea"])
    return out


def _is_missing(v):
    """None 或 NaN 都视为缺失。"""
    return v is None or v != v


def _weighted(items):
    """items: [(value|None|NaN, weight)];缺失项移除,剩余按原比例归一化到 100%。"""
    vals = [(v, w) for v, w in items if not _is_missing(v)]
    if not vals:
        return None
    total_w = sum(w for _, w in vals)
    return sum(v * w / total_w for v, w in vals)
