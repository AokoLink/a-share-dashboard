# -*- coding: utf-8 -*-
"""评分系统分层报告:只读预测引擎输出 + 自建 universe,打八层标签逐维报准确率。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
"""
import analysis as an
import backtest as bt
import predict as pr

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
AMOUNT_TOP_FRAC = 0.20
HIGH_POS_RATIO = 0.97
OVERSOLD_RET = -0.15
AMP_THRESHOLD = 15.0
MIN_LAYER_N = 30
LAYERS = ("大盘", "热门", "龙头", "趋势", "高位", "超跌", "反抽", "震荡")


def _amount_top(universe, pos_of, all_days, i, buyable):
    """当日 buyable 中 amount 前 top20%(NaN 排名前 dropna,不参与也不计分母)。"""
    dt = all_days[i]
    pairs = []
    for c in buyable:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        amt = float(universe[c]["amount"].iloc[bar])
        if amt != amt:
            continue
        pairs.append((c, amt))
    if not pairs:
        return set()
    pairs.sort(key=lambda x: -x[1])
    k = max(1, int(len(pairs) * AMOUNT_TOP_FRAC))
    return set(c for c, _ in pairs[:k])


def _hot_members(sector_members, heat):
    """中位数 5 日涨幅 top3 热板块的成员并集。"""
    ranked = sorted(heat, key=heat.get, reverse=True)
    out = set()
    for s in ranked[:TOP_SECTORS]:
        out.update(sector_members.get(s, []))
    return out


def _leaders(sector_map, buyable, composite_of):
    """每板块内 composite 最高的 buyable 成分 top1;并列取 code 最小。"""
    by_sector = {}
    for c in buyable:
        for s in sector_map.get(c, []):
            by_sector.setdefault(s, []).append(c)
    leaders = set()
    for members in by_sector.values():
        best_c, best_comp = None, None
        for c in members:
            comp = composite_of.get(c)
            if comp is None:
                continue
            if best_comp is None or comp > best_comp or (comp == best_comp and c < best_c):
                best_c, best_comp = c, comp
        if best_c is not None:
            leaders.add(best_c)
    return leaders


def _layer_trend(d, bar):
    df = an.add_ma(d.iloc[:bar + 1], (5, 20, 60))
    m5, m20, m60 = df["ma5"].iloc[-1], df["ma20"].iloc[-1], df["ma60"].iloc[-1]
    if any(x != x for x in (m5, m20, m60)):
        return False
    return m5 > m20 > m60


def _layer_high(d, bar):
    closes = d["close"].iloc[max(0, bar - 59): bar + 1]
    if len(closes) == 0:
        return False
    return float(d["close"].iloc[bar]) >= HIGH_POS_RATIO * float(closes.max())


def _layer_oversold(d, bar):
    if bar - 20 < 0:
        return False
    return float(d["close"].iloc[bar]) / float(d["close"].iloc[bar - 20]) - 1 < OVERSOLD_RET


def _layer_rebound(d, bar):
    if bar - 21 < 0:
        return False
    ret20_tm1 = float(d["close"].iloc[bar - 1]) / float(d["close"].iloc[bar - 21]) - 1
    return ret20_tm1 < OVERSOLD_RET and float(d["close"].iloc[bar]) > float(d["close"].iloc[bar - 1])


def _layer_oscillation(d, bar):
    window = d.iloc[max(0, bar - 19): bar + 1]
    close = float(d["close"].iloc[bar])
    if close <= 0:
        return False
    amp = (float(window["high"].max()) - float(window["low"].min())) / close * 100.0
    return amp < AMP_THRESHOLD
