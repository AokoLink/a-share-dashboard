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

DIMS = ("direction", "gap", "path", "trend3", "return", "risk")
PRIMARY = {"direction": "hit_rate", "gap": "hit_rate", "path": "acc_path",
           "trend3": "hit_rate", "return": "sign_agreement", "risk": "adverse_rate"}


def _binom_diff_se(p_l, n_l, p_o, n_o):
    if p_l is None or p_o is None or not n_l or not n_o:
        return None
    sl = (p_l * (1.0 - p_l) / n_l) ** 0.5
    so = (p_o * (1.0 - p_o) / n_o) ** 0.5
    return (sl * sl + so * so) ** 0.5


def _mean_diff_se(std_l, n_l, std_o, n_o):
    if std_l is None or std_o is None or not n_l or not n_o:
        return None
    return ((std_l * std_l) / n_l + (std_o * std_o) / n_o) ** 0.5


def _dim_metrics(cals, dim, records):
    """对一组记录算某维指标,复用 predict 的指标函数(单一真相源)。"""
    if dim == "path":
        samples = [(r["composite"], r["gap"], r["od"]) for r in records]
        return pr._metric_path(cals["gap"], cals["od"], samples)
    if dim == "return":
        samples = [(r["composite"], r["close1"]) for r in records]
        return pr._metric_return(cals["return"], cals["direction"], samples)
    if dim == "risk":
        samples = [(r["risk"], 1 if r["close1"] < pr.ADVERSE_THRESHOLD else 0)
                   for r in records if r["risk"] is not None]
        return pr._metric_risk(cals["risk"], samples)
    samples = []
    for r in records:
        if dim == "trend3" and r["trend3"] is None:
            continue
        label = r["direction_label"] if dim == "direction" else r[dim]
        samples.append((r["composite"], label))
    return pr._metric(dim, cals[dim], samples)


def _significance(overall, per_layer):
    """逐 (层, 维, 指标) 做 ±2σ 显著性;n<30 或值 None 跳过。
    返回 (significant, se_bounds, all_undifferentiated);se_bounds[(L, dim)] = 2*se(主指标)。
    """
    significant = []
    se_bounds = {}
    for dim, metric, kind in (
        ("direction", "hit_rate", "binom"),
        ("gap", "hit_rate", "binom"),
        ("path", "acc_path", "binom"),
        ("trend3", "hit_rate", "binom"),
        ("return", "sign_agreement", "binom"),
        ("return", "mae", "mean"),
        ("return", "rmse", "mean"),
        ("return", "mean_residual", "mean"),
        ("risk", "adverse_rate", "binom"),
    ):
        o = overall[dim]
        for L in LAYERS:
            m = per_layer[L][dim]
            if kind == "binom":
                if metric == "hit_rate":
                    n_l, n_o = m["n"] - m["n_hold"], o["n"] - o["n_hold"]
                elif metric == "sign_agreement":
                    n_l, n_o = m["sign_n"], o["sign_n"]
                else:  # adverse_rate / acc_path
                    n_l, n_o = m["n"], o["n"]
                v_l, v_o = m[metric], o[metric]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _binom_diff_se(v_l, n_l, v_o, n_o)
            else:  # mean
                std_key = {"mae": "mae_std", "rmse": "rmse_std",
                           "mean_residual": "residual_std"}[metric]
                v_l, v_o = m[metric], o[metric]
                n_l, n_o = m["n"], o["n"]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _mean_diff_se(m[std_key], n_l, o[std_key], n_o)
            if se is None:
                continue
            if metric == PRIMARY[dim]:
                se_bounds[(L, dim)] = 2.0 * se
            if abs(v_l - v_o) > 2.0 * se:
                significant.append((L, dim, metric, v_l, v_o, se))
    return significant, se_bounds, (len(significant) == 0)


def evaluate(records, universe, pos_of, all_days, sector_members, sector_map, cals):
    day_to_i = {dt: i for i, dt in enumerate(all_days)}
    by_date = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    layer_records = {L: [] for L in LAYERS}
    for date, recs in by_date.items():
        i = day_to_i[date]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        heat = bt.sector_heat(universe, sector_members, pos_of, all_days, i)
        amount_top = _amount_top(universe, pos_of, all_days, i, buyable)
        hot_members = _hot_members(sector_members, heat)
        composite_of = {r["code"]: r["composite"] for r in recs}
        leaders = _leaders(sector_map, buyable, composite_of)
        for r in recs:
            c, bar = r["code"], r["bar"]
            d = universe[c]
            tags = {
                "大盘": c in amount_top,
                "热门": c in hot_members,
                "龙头": c in leaders,
                "趋势": _layer_trend(d, bar),
                "高位": _layer_high(d, bar),
                "超跌": _layer_oversold(d, bar),
                "反抽": _layer_rebound(d, bar),
                "震荡": _layer_oscillation(d, bar),
            }
            for L, ok in tags.items():
                if ok:
                    layer_records[L].append(r)

    overall = {dim: _dim_metrics(cals, dim, records) for dim in DIMS}
    per_layer = {L: {dim: _dim_metrics(cals, dim, layer_records[L]) for dim in DIMS}
                 for L in LAYERS}
    significant, se_bounds, all_undifferentiated = _significance(overall, per_layer)
    return {"overall": overall, "layers": per_layer,
            "layer_n": {L: len(layer_records[L]) for L in LAYERS},
            "significant": significant, "se_bounds": se_bounds,
            "all_undifferentiated": all_undifferentiated}


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
