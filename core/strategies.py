# -*- coding: utf-8 -*-
"""第二阶段选股定义。信号只读取决策日及以前的日线。"""
import math

import numpy as np
import pandas as pd

from core.analysis import limit_threshold

VERSION = "stage2-technical-v2-experiments"
MIN_BARS = 61
MIN_AMOUNT = 1e8
HORIZONS = {"breakout": 5, "pullback": 10, "rebound": 3}
VARIANTS = ("base", "breakout_no_sector", "breakout_no_buffer", "breakout_no_volume",
            "pullback_no_depth", "pullback_no_shrink", "rebound_no_drop", "rebound_no_position")

CARDS = {
    "breakout": {
        "name": "趋势突破", "hypothesis": "强势方向的平台突破可能延续",
        "scope": "非ST、上市至少61根、成交额至少1亿元的股票",
        "ranking": "突破幅度、近5日量能相对前20日及已核实的板块20日超额，策略内排序",
        "entry": "T日收盘确认，最早T+1交易日开盘；跳空超过5%取消",
        "exit": "持有5个交易日后下一个开盘退出；不在同日补卖",
        "risk": "不追涨停，不把未知历史ST或缺失板块成分视为已核实",
        "evidence": "exploratory", "horizon_sessions": 5,
    },
    "pullback": {
        "name": "强势回调", "hypothesis": "中期趋势中的有序缩量回调可能恢复",
        "scope": "非ST、上市至少61根、成交额至少1亿元的股票",
        "ranking": "有序回调深度与近5日缩量程度，策略内排序",
        "entry": "T日收盘确认，最早T+1交易日开盘；跳空超过5%取消",
        "exit": "持有10个交易日后下一个开盘退出；不在同日补卖",
        "risk": "控制回调深度，不把高换手本身当收益信号",
        "evidence": "exploratory", "horizon_sessions": 10,
    },
    "rebound": {
        "name": "超跌反弹", "hypothesis": "恐慌环境下个别超跌股票可能短期修复，待与等权市场对照",
        "scope": "仅在已知且新鲜的恐慌状态研究，股票仍需基本流动性",
        "ranking": "5日下跌幅度与60日价格低位程度，策略内排序",
        "entry": "T日收盘确认，最早T+1交易日开盘；跳空超过5%取消",
        "exit": "持有3个交易日后下一个开盘退出；不在同日补卖",
        "risk": "旧低位因子已无选股增量证据，仅作新假设检验",
        "evidence": "exploratory", "horizon_sessions": 3,
    },
    "allocation": {
        "name": "中期配置", "hypothesis": "盈利质量与合理价格可能提供独立于动量的信息",
        "scope": "需要按实际披露日可用的财报与估值历史",
        "ranking": "待披露时点历史数据齐备后定义",
        "entry": "待数据齐备后定义", "exit": "待数据齐备后定义",
        "risk": "缺少披露时点历史，不产生候选",
        "evidence": "blocked_data", "horizon_sessions": None,
    },
}


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def base_eligibility(frame, code, name=None):
    """共用资格；历史缺名称时保留并标记未知ST，不伪装为已验证。"""
    if len(frame) < MIN_BARS:
        return False, "history_under_61", []
    if name is not None and "ST" in str(name).upper():
        return False, "st", []
    row = frame.iloc[-1]
    close = _finite(row.get("close"))
    prev = _finite(frame["close"].iloc[-2])
    volume = _finite(row.get("volume"))
    amount = _finite(row.get("amount"))
    if close is None or prev is None or volume is None or min(close, prev, volume) <= 0:
        return False, "no_trade", []
    if amount is None or amount < MIN_AMOUNT:
        return False, "low_or_unknown_liquidity", []
    change_pct = (close / prev - 1) * 100
    if change_pct >= limit_threshold(str(code)):
        return False, "limit_up", []
    return True, None, ["historical_st_unknown"] if name is None else []


def screen(frame, code, name=None, market_state=None, sector_rel20=None, *, variant="base"):
    """返回每个命中策略的解释卡；frame 末根即决策日，不读取下一根。"""
    if variant not in VARIANTS:
        raise ValueError("unknown_variant")
    ok, reason, caveats = base_eligibility(frame, code, name)
    if not ok:
        return {"eligible": False, "reason": reason, "signals": []}
    tail = frame.iloc[-65:]
    close = pd.to_numeric(tail["close"], errors="coerce").to_numpy(dtype=float)
    high = pd.to_numeric(tail["high"], errors="coerce").to_numpy(dtype=float)
    volume = pd.to_numeric(tail["volume"], errors="coerce").to_numpy(dtype=float)
    if not (np.isfinite(close).all() and np.isfinite(high).all() and np.isfinite(volume).all()
            and np.min(close) > 0 and np.min(high) > 0):
        return {"eligible": False, "reason": "invalid_history", "signals": []}
    c = close[-1]
    ma20, ma60 = float(close[-20:].mean()), float(close[-60:].mean())
    prev20 = float(close[-40:-20].mean())
    high60 = float(high[-61:-1].max())
    peak20 = float(high[-21:-1].max())
    low60 = float(close[-60:].min())
    pos60 = (c - low60) / max(high60 - low60, 1e-9)
    vol_ratio = float(volume[-5:].mean() / max(volume[-25:-5].mean(), 1.0))
    ret5 = c / close[-6] - 1
    date = str(frame["date"].iloc[-1])[:10]
    signals = []

    def add(key, score, reasons, extra=None):
        signals.append({"strategy": key, "version": VERSION, "score": round(min(100, max(0, score)), 2),
                        "signal_date": date, "entry": "next_session_open", "horizon_sessions": HORIZONS[key],
                        "reasons": reasons, "caveats": caveats + (extra or []),
                        "evidence": "exploratory", "variant": variant})

    rel = _finite(sector_rel20)
    # 20/60日趋势 + 60日平台突破；板块20日超额缺失时只形成待核实研究候选。
    buffer = 1.0 if variant == "breakout_no_buffer" else 1.001
    if c > high60 * buffer and ma20 > ma60 and ma20 > prev20 and (variant == "breakout_no_volume" or vol_ratio >= 1.1):
        if variant == "breakout_no_sector" or rel is None or rel > 0:
            add("breakout", 45 + min(25, (c / high60 - 1) * 500)
                + (0 if variant == "breakout_no_volume" else min(20, (vol_ratio - 1) * 20))
                + (10 if rel is not None and variant != "breakout_no_sector" else 0),
                ["60日平台突破", "20日均线高于60日均线"] + ([] if variant == "breakout_no_volume" else ["近5日成交参与放大"]),
                ["sector_condition_removed_for_research"] if variant == "breakout_no_sector" else
                ["sector_relative_strength_unverified"] if rel is None else [])
    drawdown = c / peak20 - 1
    if ma20 > ma60 and ma20 > prev20 and (variant == "pullback_no_depth" or -0.12 <= drawdown <= -0.03) and c >= ma20 * 0.97 and (variant == "pullback_no_shrink" or vol_ratio <= 0.85):
        add("pullback", 50 + (0 if variant == "pullback_no_depth" else min(25, -drawdown * 200))
            + (0 if variant == "pullback_no_shrink" else min(25, (1 - vol_ratio) * 50)),
            ["20日均线高于60日均线", "结构未破"] + ([] if variant == "pullback_no_depth" else ["距近期高点回调3%至12%"])
            + ([] if variant == "pullback_no_shrink" else ["近5日缩量"]))
    if market_state == "恐慌" and (variant == "rebound_no_position" or pos60 <= 0.2) and (variant == "rebound_no_drop" or ret5 <= -0.08) and c >= low60 * 0.98:
        add("rebound", 45 + (0 if variant == "rebound_no_drop" else min(30, -ret5 * 150))
            + (0 if variant == "rebound_no_position" else min(25, (0.2 - pos60) * 100)),
            ["市场状态为恐慌"] + ([] if variant == "rebound_no_position" else ["60日价格低位"])
            + ([] if variant == "rebound_no_drop" else ["5日明显下跌"]),
            ["regime_event_evidence_limited"])
    return {"eligible": True, "reason": None, "signals": signals}


def execution_check(signal_close, next_open, max_gap=0.05):
    """执行日才判断跳空；该值绝不能进入T日选股排序。"""
    close, opened = _finite(signal_close), _finite(next_open)
    if not close or not opened:
        return False, "missing_open"
    if opened / close - 1 > max_gap:
        return False, "gap_above_5pct"
    return True, None
