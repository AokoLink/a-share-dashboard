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


# ---- 板块打分(规格 §6.2) ----

def sector_emotion(up_ratio, limit_ratio, turnover_ratio, leader_change_pct):
    return _weighted([
        (up_ratio * 100 if up_ratio is not None else None, 40),
        (min(100.0, limit_ratio * 100) if limit_ratio is not None else None, 20),
        (min(100.0, turnover_ratio / 1.5 * 100) if turnover_ratio is not None else None, 25),
        (min(100.0, leader_change_pct * 20) if leader_change_pct is not None else None, 15),
    ])


def sector_strength(index_change, consecutive_days, activity):
    return _weighted([
        (min(100.0, index_change * 20) if index_change is not None else None, 40),
        (min(100.0, consecutive_days * 25) if consecutive_days is not None else None, 30),
        (min(100.0, activity / 0.03 * 100) if activity is not None else None, 30),
    ])


def sector_risk(index_change, change_3d, turnover_ratio, prev_change, up_ratio):
    """加性条件,cap 100;缺失项计 0(不归一化)。"""
    score = 0
    if index_change is not None and index_change > 5:
        score += 40
    if change_3d is not None and change_3d > 10:
        score += 40
    if (turnover_ratio is not None and prev_change is not None and index_change is not None
            and turnover_ratio > 1.5 and index_change < 2 and index_change < prev_change):
        score += 40  # 板块放量滞涨(仅收盘后:调用方盘中传 turnover_ratio=None)
    if up_ratio is not None and up_ratio < 0.35:
        score += 40  # 严重分化
    return min(100.0, score)


def sector_verdict(emotion, strength, risk, consecutive_days, data_complete):
    e_hi = emotion is not None and emotion >= 70
    e_mid = emotion is not None and emotion >= 45
    s_hi = strength is not None and strength >= 60
    s_mid = strength is not None and strength >= 35
    r_hi = risk is not None and risk >= 65
    if strength is not None and strength < 35 and r_hi:
        return "风险提示/回避"                                  # P1
    if r_hi and (e_mid or s_mid):
        return "谨慎追高(过热)"                                  # P2
    if e_hi and s_hi and not r_hi:
        return "建议关注"                                        # P3
    if e_hi and s_mid and not s_hi and consecutive_days >= 2 and not r_hi:
        return "跟踪(热点延续)"                                  # P4
    if e_hi and s_mid and consecutive_days == 1 and data_complete and not r_hi:
        return "警惕一日游"                                      # P5
    return "观望"                                                # P6


def composite_score(strength, emotion, risk):
    if strength is None or emotion is None or risk is None:
        return None
    return round(0.4 * strength + 0.35 * emotion + 0.25 * (100 - risk), 2)
