# -*- coding: utf-8 -*-
"""分析层:时间口径 + 指标 + 打分。纯函数,输入可注入,便于单测。"""
from datetime import datetime

import numpy as np
import pandas as pd

# ---- 时间口径(规格 §7 P1/A/B) ----

AM_START = 9 * 60 + 30
AM_END = 11 * 60 + 30
PM_START = 13 * 60
PM_END = 15 * 60

# ---- P0 过热判定(规格 §4.1;decisions.md P0 定稿) ----

OVERHEAT_MIN_DAYS = 4          # 过热判定:连续上榜天数阈值(spec §4.1 默认;badge 路径仅作信息徽章)
P0_PATH = "badge"              # "intercept"(路径 A,独立标签排除)/ "badge"(路径 B,仅徽章,生产现状)


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
    if c.startswith(("8", "4", "920")):
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


def _high_flags(emotion, strength, risk):
    """三高旗标(阈值 70/60/65),供 sector_verdict 与 score_sector 共享。"""
    e_hi = emotion is not None and emotion >= 70
    s_hi = strength is not None and strength >= 60
    r_hi = risk is not None and risk >= 65
    return e_hi, s_hi, r_hi


def sector_verdict(emotion, strength, risk, consecutive_days, data_complete):
    e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)
    e_mid = emotion is not None and emotion >= 45
    s_mid = strength is not None and strength >= 35
    if strength is not None and strength < 35 and r_hi:
        return "风险提示/回避"                                  # P1
    if r_hi and (e_mid or s_mid):
        return "谨慎追高(过热)"                                  # P2
    if e_hi and s_hi and not r_hi:
        return "建议关注"                                        # P3
    if e_hi and s_mid and not s_hi and consecutive_days >= 2 and not r_hi:
        return "跟踪(热点延续)"                                  # P4
    if e_hi and strength is not None and consecutive_days == 1 and data_complete and not r_hi:
        return "警惕一日游"                                      # P5 强度=中/低(非高即 P3 分流,高风险低强度已被 P1/P2 分流)
    return "观望"                                                # P6


def composite_score(strength, emotion, risk):
    if strength is None or emotion is None or risk is None:
        return None
    return round(0.4 * strength + 0.35 * emotion + 0.25 * (100 - risk), 2)


def score_sector(metrics):
    """板块打分唯一入口:组合 emotion/strength/risk/composite/verdict/overheated。
    metrics 键见接口说明;与旧 app 路由内联口径完全一致。"""
    emotion = sector_emotion(metrics["up_ratio"], metrics["limit_ratio"],
                             metrics["turnover_ratio"], metrics["leader_change_pct"])
    strength = sector_strength(metrics["change_pct"], metrics["consecutive_days"],
                               metrics["activity"])
    risk = sector_risk(metrics["change_pct"], metrics["change_3d"],
                       metrics["turnover_ratio"], metrics["prev_change"], metrics["up_ratio"])
    composite = composite_score(strength, emotion, risk)
    verdict = sector_verdict(emotion, strength, risk, metrics["consecutive_days"],
                             metrics["data_complete"])
    e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)
    cd = metrics.get("consecutive_days")
    overheated = (cd is not None and cd >= OVERHEAT_MIN_DAYS and e_hi and s_hi and not r_hi
                  and metrics.get("data_complete", True))
    if overheated and P0_PATH == "intercept":
        verdict = "过热(连涨)"        # 独立标签,不在 QUALIFYING_VERDICTS → 排除(路径 A;生产 badge 惰性)
    return {"emotion": emotion, "strength": strength, "risk": risk,
            "composite": composite, "verdict": verdict, "overheated": overheated}


def collect_sector_metrics(db, type_key, code, change_pct, turnover, up_count, down_count,
                           leader_change_pct, store_ctx, market_turnover, now, data_complete=True):
    """summary 行 + store 历史 + 市场成交额 → 打分输入 → 五维结果 + consecutive。
    与旧 api_sectors/api_sector 内联逻辑逐字段一致:limit_ratio 恒 None(无成分股聚合)。"""
    up_ratio = None
    if up_count is not None and down_count is not None and (up_count + down_count) > 0:
        up_ratio = up_count / (up_count + down_count)
    today = now.strftime("%Y-%m-%d")
    turnover_ratio = None
    if is_after_close(now) and turnover is not None:
        avg5 = store_ctx.get_sector_turnover_avg(db, type_key, code, today, 5)
        if avg5 and avg5 > 0:
            turnover_ratio = turnover / float(avg5)
    prev_change = store_ctx.get_sector_prev_change(db, type_key, code, today)
    change_3d = store_ctx.get_sector_change_3d(db, type_key, code, today)
    consecutive = store_ctx.get_consecutive_days(db, type_key, code, today, 20)
    activity = (turnover / market_turnover) if (turnover is not None and market_turnover) else None
    scores = score_sector({
        "change_pct": change_pct, "up_ratio": up_ratio, "limit_ratio": None,
        "leader_change_pct": leader_change_pct, "turnover_ratio": turnover_ratio,
        "consecutive_days": consecutive, "activity": activity,
        "change_3d": change_3d, "prev_change": prev_change, "data_complete": data_complete,
    })
    scores["consecutive_days"] = consecutive
    return scores


# ---- 个股评分 v3:位置/趋势 子助手(规格 §4.1/§4.3) ----

def _platform_score(amp):
    """近10日振幅平台分。amp:百分比;amp≤8→30;8<amp≤15→15;amp>15→0;缺失→0。"""
    if amp is None:
        return 0
    if amp <= 8:
        return 30
    if amp <= 15:
        return 15
    return 0


def _bias_sweet(bias):
    """乖离甜区(规格 §4.1),单位 %。0/3/8/15/20 处连续;−8/−3 残留小台阶。"""
    if bias < -8:
        return 0
    if bias < -3:
        return 15
    if bias < 0:
        return 35
    if bias <= 3:
        return 35 + 65 * min(1, bias / 3)
    if bias <= 8:
        return 100 - (bias - 3) * 12
    if bias <= 15:
        return 40 - (bias - 8) * 4
    if bias <= 20:
        return 12 - (bias - 15) * 1.4
    return 5


def _trend_fresh(df):
    """近5根内 MA5 上穿 MA10 →15;否则当前 MA5>MA10 →8;否则 0。df 需含 ma5/ma10 列。"""
    for i in range(max(0, len(df) - 5), len(df)):
        if i == 0:
            continue
        pm5, pm10 = df["ma5"].iloc[i - 1], df["ma10"].iloc[i - 1]
        cm5, cm10 = df["ma5"].iloc[i], df["ma10"].iloc[i]
        if (not _is_missing(pm5) and not _is_missing(pm10) and not _is_missing(cm5)
                and not _is_missing(cm10) and pm5 <= pm10 and cm5 > cm10):
            return 15
    m5, m10 = df["ma5"].iloc[-1], df["ma10"].iloc[-1]
    if not _is_missing(m5) and not _is_missing(m10) and m5 > m10:
        return 8
    return 0


def rsi14(closes):
    """Wilder RSI(14)。数据不足或全平 → 50;全涨 → 100;全跌 → 0。"""
    s = pd.Series(closes, dtype=float).dropna()
    if len(s) < 15:
        return 50.0
    delta = s.diff()
    avg_gain = delta.clip(lower=0.0).ewm(alpha=1.0 / 14, adjust=False).mean()
    avg_loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / 14, adjust=False).mean()
    g, l = float(avg_gain.iloc[-1]), float(avg_loss.iloc[-1])
    if g == 0 and l == 0:
        return 50.0
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + g / l)


def max_drawdown_20(daily_df):
    """近20根收盘最大回撤(%,负数)。不足 → 0。"""
    closes = daily_df["close"].tail(20)
    if len(closes) < 2:
        return 0.0
    peak = closes.cummax()
    return float((closes - peak).div(peak).min() * 100)


def _pos60(daily_df):
    """60日位置(0..1)。分母≤0 → 0.5。"""
    lo_min = daily_df["low"].iloc[-60:].min()
    hi_max = daily_df["high"].iloc[-60:].max()
    if hi_max - lo_min <= 0:
        return 0.5
    return (daily_df["close"].iloc[-1] - lo_min) / (hi_max - lo_min)


def compute_position_score(daily_df):
    """位置分(规格 §4.1):低60日位置高分 + 乖离甜区 + 平台。"""
    if len(daily_df) < 61:
        return 0.0
    df = add_ma(daily_df, (20,))
    last_close = df["close"].iloc[-1]
    pos60 = _pos60(df)
    pos_factor = 100.0 * (1.0 - pos60)
    ma20 = df["ma20"].iloc[-1]
    bias = 0.0 if _is_missing(ma20) or ma20 <= 0 else (last_close - ma20) / ma20 * 100
    base = df["close"].iloc[-11] if len(df) >= 11 else 0
    amp = 0 if base is None or base <= 0 \
        else (df["high"].iloc[-10:].max() - df["low"].iloc[-10:].min()) / base * 100
    return min(100.0, 0.5 * pos_factor + 0.35 * _bias_sweet(bias) + 0.15 * _platform_score(amp))


# ---- 个股量价打分(规格 §6.3/§7) ----

def compute_trend_score(daily_df):
    """趋势结构分(规格 §4.3):order 三档 + fresh 金叉奖励;自然上限 55。"""
    if len(daily_df) < 61:
        return 0.0
    df = add_ma(daily_df)
    m5, m10, m20, m60 = (df["ma%d" % p].iloc[-1] for p in (5, 10, 20, 60))
    order = 0
    if not _is_missing(m5) and not _is_missing(m10) and m5 > m10:
        order += 1
    if not _is_missing(m10) and not _is_missing(m20) and m10 > m20:
        order += 1
    if not _is_missing(m20) and not _is_missing(m60) and m20 > m60:
        order += 1
    return 40.0 * order / 3.0 + _trend_fresh(df)


def _avg5_volume(daily_df):
    if len(daily_df) < 6:
        return None
    v = daily_df["volume"].iloc[-6:-1].mean()
    return None if pd.isna(v) or v <= 0 else v


# ---- 个股评分 v3:量价/信号 子助手(规格 §4.2/§4.4) ----

def _vol_health(vr):
    """分项一 量比健康度。vr≤1.5→35;1.5<vr≤2.5→26;vr>2.5→12;缺失→12。"""
    if vr is None:
        return 12
    if vr <= 1.5:
        return 35
    if vr <= 2.5:
        return 26
    return 12


def _price_volume(chg, vr):
    """分项二 价量一致。chg>1且vr>1.2→35;chg>0且vr<0.8→15;chg<−1且vr>1.2→5;其余→25。"""
    if chg > 1 and vr is not None and vr > 1.2:
        return 35
    if chg > 0 and vr is not None and vr < 0.8:
        return 15
    if chg < -1 and vr is not None and vr > 1.2:
        return 5
    return 25


def _vol_sustain(r):
    """分项三 量能持续(甜区)。r<0.7→6;0.7≤r<1.0→20;1.0≤r<1.5→30;r≥1.5→12;缺失→0。"""
    if r is None:
        return 0
    if r < 0.7:
        return 6
    if r < 1.0:
        return 20
    if r < 1.5:
        return 30
    return 12


def _rsi_score(rsi):
    if rsi <= 65:
        return 25
    if rsi <= 80:
        return 12
    return 5


def _momentum_score(ret5):
    if 0 <= ret5 <= 8:
        return 15
    if -3 <= ret5 < 0:
        return 8
    return 3


def _macd_branch(dif, dea, prev_dif, prev_dea):
    """MACD 互斥分支(规格 §4.4):当根金叉优先,零轴上严格更高。"""
    if prev_dif <= prev_dea and dif > dea:      # 当根金叉
        return 30 if dif > 0 else 15
    if dif > dea:                               # 已金叉维持
        return 12 if dif > 0 else 8
    return 0


def _breakout_score(pos60, last_close, prev_high, vr):
    """低位门控突破(规格 §4.4):仅 pos60<0.6 时创新高加分。"""
    if pos60 is None or pos60 >= 0.6:
        return 0
    if last_close > prev_high:
        return 20 if vr is not None and vr > 1.2 else 10
    return 0


def compute_volume_price_score(daily_df, quote, now):
    """量价分(规格 §4.2):量比健康 + 价量一致 + 量能持续(甜区)。"""
    if len(daily_df) < 25:
        return 0.0
    vr = custom_volume_ratio(quote.get("volume", 0), trading_minutes_elapsed(now), _avg5_volume(daily_df))
    chg = quote.get("change_pct", 0.0) or 0.0
    b20 = daily_df["volume"].iloc[-25:-5].mean()
    a5 = daily_df["volume"].iloc[-5:].mean()
    r = a5 / b20 if b20 and b20 > 0 else None
    return min(100.0, _vol_health(vr) + _price_volume(chg, vr) + _vol_sustain(r))


def compute_signal_score(daily_df, quote, now):
    """信号分(规格 §4.4):MACD 优先级 + RSI + 低位门控突破 + 动量。"""
    if len(daily_df) < 26:
        return 0.0
    df = add_macd(daily_df)
    last, prev = df.iloc[-1], df.iloc[-2]
    dif, dea = last["dif"], last["dea"]
    if (_is_missing(dif) or _is_missing(dea) or _is_missing(prev["dif"]) or _is_missing(prev["dea"])):
        macd = 0
    else:
        macd = _macd_branch(dif, dea, prev["dif"], prev["dea"])
    rsi = _rsi_score(rsi14(daily_df["close"]))
    vr = custom_volume_ratio(quote.get("volume", 0), trading_minutes_elapsed(now), _avg5_volume(daily_df))
    prev_high = df["high"].iloc[-21:-1].max() if len(df) > 21 else None
    breakout = _breakout_score(_pos60(daily_df), last["close"], prev_high, vr)
    ret5 = (last["close"] / df["close"].iloc[-6] - 1) * 100 if len(df) >= 6 else 0.0
    return macd + rsi + breakout + _momentum_score(ret5)


def _bias_risk(bias):
    """乖离风险梯度(规格 §4.5),单位 %。15/20/25/30 处连续。"""
    if bias <= 15:
        return 0
    if bias <= 20:
        return 40 * (bias - 15) / 5
    if bias <= 25:
        return 40
    if bias <= 30:
        return 40 + 30 * (bias - 25) / 5
    return 70


def compute_stock_risk(daily_df, quote, now):
    """个股风险(规格 §4.5):乖离梯度 + 放量跌破 + 放量滞涨 + 高位长上影 + 近20日回撤,累加 cap 100。"""
    items = 0.0
    df = add_ma(daily_df, (20,))
    ma20 = df["ma20"].iloc[-1]
    price = quote.get("price", df["close"].iloc[-1])
    change = quote.get("change_pct", 0.0) or 0.0
    bias = 0.0 if _is_missing(ma20) or ma20 <= 0 else (price - ma20) / ma20 * 100
    items += _bias_risk(bias)
    vr = custom_volume_ratio(quote.get("volume", 0), trading_minutes_elapsed(now), _avg5_volume(daily_df))
    if vr is not None and vr > 1.5 and not _is_missing(ma20) and price < ma20 and change < 0:
        items += 10                                   # 放量跌破 MA20(§9.3 校准:命中组 fwd5 +0.58% 反超未命中 −0.10% 共 0.68pp,+40 反效 → 降权至 +10)
    if vr is not None and vr > 1.5 and len(daily_df) >= 3:
        prev_change = (daily_df["close"].iloc[-2] / daily_df["close"].iloc[-3] - 1) * 100
        if change < prev_change and change < 2:
            items += 25                               # 放量滞涨
    high = quote.get("high"); low = quote.get("low"); op = quote.get("open")
    if high is not None and low is not None and op is not None and price and price > 0:
        body = max(op, price) - min(op, price)
        upper = high - max(op, price)
        amplitude = (high - low) / max(op, price) * 100 if max(op, price) > 0 else 0
        if body > 0 and upper > 2 * body and amplitude > 5:
            items += 20                               # 高位长上影
    if max_drawdown_20(daily_df) < -25:
        items += 20                                   # 近20日最大回撤>25%
    return min(100.0, items)


V3_WEIGHTS = (0.55, 0.15, 0.10, 0.20)          # (position, vp, trend, signal)
HOT_REL_WEIGHTS = (0.40, 0.15, 0.10, 0.20)     # 配置 c: + rel_strength×0.15
HOT_SIGNAL_WEIGHTS = (0.40, 0.15, 0.10, 0.35)  # 配置 b(decisions.md 定稿 HOT_WEIGHT_MODE="signal")


def stock_composite_v3(position, vp, trend, signal, risk, sector_bonus=0,
                       rel_strength=None, weights=None):
    """综合分(规格 §4.6):质量 = Σ(weights×因子) + (rel_strength×0.15 若非 None),
    再加板块加成,× 风险折扣。weights 缺省:rel_strength 非 None → HOT_REL_WEIGHTS(配置 c),
    否则 V3_WEIGHTS;显式 weights(配置 b 等)覆盖。未舍入。"""
    if None in (position, vp, trend, signal, risk):
        return None
    if weights is None:
        weights = HOT_REL_WEIGHTS if rel_strength is not None else V3_WEIGHTS
    quality = weights[0] * position + weights[1] * vp + weights[2] * trend + weights[3] * signal
    if rel_strength is not None:
        quality += 0.15 * rel_strength
    return (quality + sector_bonus) * max(0.0, 1.0 - risk / 100.0)


def stock_verdict(composite_raw):
    """五档 verdict(规格 §4.6):≥67 强烈关注;≥62 关注;≥52 持有/跟踪;≥42 观望;<42 回避。用未舍入值判定。

    阈值经 Task 8 校准(§9.5)整组上移 +4:原 63/58/48/38 下可介入率 40.2% 超出
    top 20–30% 目标,上移后 ≈26%。
    """
    if composite_raw is None:
        return None
    if composite_raw >= 67:
        return "强烈关注"
    if composite_raw >= 62:
        return "关注"
    if composite_raw >= 52:
        return "持有/跟踪"
    if composite_raw >= 42:
        return "观望"
    return "回避"


def score_stock(daily_df, quote, now):
    """个股打分(规格 §4.0/§4.6):≥61 根守卫;返回四因子 + 风险 + 未含板块的 composite;无 verdict。"""
    if len(daily_df) < 61:
        return {"position": None, "trend": None, "volume_price": None,
                "signal": None, "risk": None, "composite": None, "pos60": None}
    position = compute_position_score(daily_df)
    vp = compute_volume_price_score(daily_df, quote, now)
    trend = compute_trend_score(daily_df)
    signal = compute_signal_score(daily_df, quote, now)
    risk = compute_stock_risk(daily_df, quote, now)
    return {"position": position, "trend": trend, "volume_price": vp,
            "signal": signal, "risk": risk,
            "pos60": _pos60(daily_df),
            "composite": stock_composite_v3(position, vp, trend, signal, risk)}


# ---- 波段候选 + 个股技术详情(设计 2026-08-16;描述性,不预测方向) ----

def _num(v):
    """None/NaN → None,否则 float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def add_kdj(df, n=9, k0=50.0, d0=50.0):
    """KDJ(n=9)。RSV=(C−Low9)/(High9−Low9)×100;K=(2K_prev+RSV)/3;D=(2D_prev+K)/3;J=3K−2D。

    K0=D0=50;不足 n 根或 High9==Low9(一字)→ RSV=NaN → K/D/J 保持 NaN(不落值,
    由 kdj_state 转 None)。"""
    out = df.copy()
    low_n = out["low"].rolling(n).min()
    high_n = out["high"].rolling(n).max()
    denom = (high_n - low_n).replace(0.0, np.nan)
    rsv = (out["close"] - low_n) / denom * 100.0
    k = np.full(len(out), np.nan)
    d = np.full(len(out), np.nan)
    pk, pd = k0, d0
    for i in range(len(out)):
        r = rsv.iloc[i]
        if r != r:                       # NaN(不足 n 根或一字平盘)
            continue
        pk = pk * 2.0 / 3.0 + r / 3.0
        pd = pd * 2.0 / 3.0 + pk / 3.0
        k[i] = pk
        d[i] = pd
    out["k"] = k
    out["d"] = d
    out["j"] = 3.0 * k - 2.0 * d
    return out


def kdj_state(k, d, j, prev_k, prev_d):
    """KDJ 描述状态 → (state, cross)。任一输入 None/NaN → (None, None)。
    state: K>80 且 D>80 → 超买;K<20 且 D<20 → 超卖;否则 中性。
    cross: prev_k≤prev_d 且 k>d → 金叉;prev_k≥prev_d 且 k<d → 死叉;否则 k>d → 多头 / k<d → 空头 / 相等 → —。"""
    if any(_is_missing(v) for v in (k, d, j, prev_k, prev_d)):
        return None, None
    if k > 80 and d > 80:
        state = "超买"
    elif k < 20 and d < 20:
        state = "超卖"
    else:
        state = "中性"
    if prev_k <= prev_d and k > d:
        cross = "金叉"
    elif prev_k >= prev_d and k < d:
        cross = "死叉"
    elif k > d:
        cross = "多头"
    elif k < d:
        cross = "空头"
    else:
        cross = "—"
    return state, cross


def macd_state(dif, dea, prev_dif, prev_dea):
    """MACD 描述状态 → (cross, zero)。与打分助手 _macd_branch 解耦(其返回分值)。
    cross: 当根金叉(prev_dif≤prev_dea 且 dif>dea)→ 金叉;当根死叉(prev_dif≥prev_dea 且 dif<dea)→ 死叉;
    否则 dif>dea → 多头 / dif<dea → 空头 / 相等或 NaN → —。
    zero: dif>0 → 零轴上;dif<0 → 零轴下;NaN → None。"""
    if any(_is_missing(v) for v in (dif, dea, prev_dif, prev_dea)):
        return "—", None
    if prev_dif <= prev_dea and dif > dea:
        cross = "金叉"
    elif prev_dif >= prev_dea and dif < dea:
        cross = "死叉"
    elif dif > dea:
        cross = "多头"
    elif dif < dea:
        cross = "空头"
    else:
        cross = "—"
    zero = "零轴上" if dif > 0 else ("零轴下" if dif < 0 else None)
    return cross, zero


def volume_price_state(chg, vr):
    """量价状态(描述性)。chg=当日涨跌幅%,vr=量比。
    vr None(盘中前 15 分钟/avg5 缺失)→ 数据不足;否则按 涨跌幅×量比 分类。"""
    if vr is None:
        return "数据不足"
    if chg > 1 and vr > 1.2:
        return "放量上涨"
    if chg > 0 and vr < 0.8:
        return "缩量上涨"
    if chg < -1 and vr > 1.2:
        return "放量下跌"
    if chg < 0 and vr < 0.8:
        return "缩量回调"
    return "平量"


def amp20(daily_df):
    """近20日日均振幅%(方向中性):(high−low)/prev_close×100 均值。不足 21 根 → None。"""
    if len(daily_df) < 21:
        return None
    prev_close = daily_df["close"].shift(1)
    amp = (daily_df["high"] - daily_df["low"]) / prev_close.replace(0.0, np.nan) * 100.0
    tail = amp.tail(20).dropna()
    if len(tail) == 0:
        return None
    return round(float(tail.mean()), 2)


def compute_technical_indicators(daily_df, quote, now):
    """个股技术指标(描述性,不预测方向)。历史 <61 根 → history_limited=True(全字段省略)。"""
    if len(daily_df) < 61:
        return {"history_limited": True}
    df = add_ma(daily_df)
    macd_df = add_macd(daily_df)
    kdj_df = add_kdj(daily_df)
    last, prev = df.iloc[-1], df.iloc[-2]
    ml, mp = macd_df.iloc[-1], macd_df.iloc[-2]
    kl, kp = kdj_df.iloc[-1], kdj_df.iloc[-2]
    ma = {("ma%d" % p): _num(last["ma%d" % p]) for p in (5, 10, 20, 60)}
    cross, zero = macd_state(ml["dif"], ml["dea"], mp["dif"], mp["dea"])
    kstate, kcross = kdj_state(kl["k"], kl["d"], kl["j"], kp["k"], kp["d"])
    rsi = rsi14(daily_df["close"])
    rsi_state = "超买" if rsi > 70 else ("超卖" if rsi < 30 else "中性")
    ma20 = _num(last["ma20"])
    price = _num(quote.get("price"))
    if price is None or price <= 0:
        price = _num(last["close"])
    bias_pct = round((price - ma20) / ma20 * 100.0, 2) if (ma20 is not None and price is not None) else None
    vr = custom_volume_ratio(quote.get("volume", 0), trading_minutes_elapsed(now), _avg5_volume(daily_df))
    b20 = daily_df["volume"].iloc[-25:-5].mean()
    a5 = daily_df["volume"].iloc[-5:].mean()
    vol_ratio = round(float(a5 / b20), 3) if (b20 and b20 > 0) else None
    chg = quote.get("change_pct", 0.0) or 0.0
    vp_state = volume_price_state(chg, _num(vr))
    turnover_pct = None
    if "turnover" in daily_df.columns:
        tv = _num(daily_df["turnover"].iloc[-1])
        if tv is not None:
            turnover_pct = round(tv * 100.0, 2)
    return {
        "history_limited": False,
        "ma": ma,
        "macd": {"dif": _num(ml["dif"]), "dea": _num(ml["dea"]), "macd": _num(ml["macd"]),
                 "cross": cross, "zero": zero},
        "kdj": {"k": _num(kl["k"]), "d": _num(kl["d"]), "j": _num(kl["j"]),
                "state": kstate, "cross": kcross},
        "rsi": {"rsi14": round(float(rsi), 2), "state": rsi_state},
        "bias": {"ma20": ma20, "pct": bias_pct},
        "pos60": round(float(_pos60(daily_df)), 4),
        "turnover_pct": turnover_pct,
        "volume_price": {"vr": _num(vr), "vol_ratio": vol_ratio, "state": vp_state},
    }


def build_hold_advice(regime_block, risk, pos60):
    """「适不适合持有」分层(诚实)。regime 层(唯一 edge)+ 个股风险/位置尾层(非方向)。

    regime_block 由 app 层组装(env.regime_swing_action + label)后注入;不 import environment(成环)。
    返回不含 verdict/tier/composite 等方向性结论。"""
    if risk is None:
        risk_note = "历史不足,无法评估风险"
    elif risk >= 60:
        risk_note = "个股风险尾部,注意回撤"
    else:
        risk_note = "个股风险可控"
    if pos60 is None:
        pos_note = "历史不足,无法评估位置"
    elif pos60 >= 0.85:
        pos_note = "60 日高位,注意追高风险"
    else:
        pos_note = "位置中性"
    rb = regime_block or {}
    label = rb.get("label")
    action_label = {"hold": "持有", "exit": "卖出/回避", "opportunity": "等机会"}.get(rb.get("action"), "未知")
    summary = ("大盘:%s(%s);个股:%s、%s。个股方向无算法 edge(历史回测证伪),仅供研究参考。"
               % (label or "未知", action_label, risk_note, pos_note))
    return {
        "regime": rb,
        "stock": {"risk": _num(risk), "risk_note": risk_note,
                  "pos60": _num(pos60), "pos_note": pos_note},
        "summary": summary,
    }
