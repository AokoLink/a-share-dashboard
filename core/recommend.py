# -*- coding: utf-8 -*-
"""推荐层:板块→个股双层推荐。select/filter/rank 为纯函数,build_recommend 编排。"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import analysis as an
from core import data_source as ds
from core import store
import pandas as pd

MAX_WORKERS = 8
MIN_AMOUNT = 1e8            # 流动性下限:1 亿元
BIG_DROP_PCT = -7.0         # 大跌排除线(不设对称跌停,创业板/科创板 -7 非跌停)
QUALIFYING_VERDICTS = ("建议关注", "跟踪(热点延续)")
HOT_COMPOSITE_THRESHOLD = 68.0      # 热板块谓词(spec §3.1);decisions.md P0b 定稿维持 68
HOT_WEIGHT_MODE = "signal"          # P0b 权重模式(decisions.md 定稿):"signal"(b)/"rel_strength"(c)/"off"
BONUS_GE75 = False           # P3: +8 门槛提到 ≥75;decisions.md 定稿
BONUS_QUALITY_GATE = False   # P3: quality<50 不给加成;decisions.md 定稿
PRICE_FLOOR = None           # P4: 低于此价的候选排除(None=关);decisions.md 定稿

# 风险尾部 + 高位硬过滤(spec 2026-08-15-risk-first-recommendation-design §3.2)
# 探针(risk_pos60_probe,318k stock-day)证伪「风险优先排序」:risk 是尾部信号非均值信号——
# [0,20) 净 -0.433 最差(84.6% 的"横盘死水"股)、[20,60) 净 -0.11 最优、[60,101) 净 -0.66~-1.33 崩溃尾。
# 故 risk 只做硬尾砍(≥60),不做升序排序;pos60 单调(越高越差),做硬砍(≥0.85)。
RISK_HARD_CUT = 60.0         # 风险尾部硬过滤:risk ≥ 60 剔除(原 70;40 会误砍最优均值段 [20,60))
EXT_HARD_CUT = 0.85          # 高位硬过滤:pos60 ≥ 0.85 剔除(单调,越高越差)


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _market_turnover(spot_df):
    return float(spot_df["amount"].sum()) if len(spot_df) else 0.0


def score_all_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """对全部板块用公共打分函数打分,按 composite 降序返回(不过滤)。"""
    rows = []
    for _, r in summary_df.iterrows():
        scores = an.collect_sector_metrics(
            db, type_key, str(r["code"]), _num(r["change_pct"]), _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
            store_ctx, market_turnover, now, True)
        rows.append({"code": str(r["code"]), "name": str(r["name"]),
                     "composite": scores["composite"], "verdict": scores["verdict"],
                     "overheated": bool(scores.get("overheated", False)),
                     "scores": scores})
    rows.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
    return rows


def select_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """返回强势板块(verdict 入选)按 composite 降序。"""
    strong = [x for x in score_all_sectors(summary_df, db, type_key, store_ctx, market_turnover, now)
              if x["verdict"] in QUALIFYING_VERDICTS]
    return strong


def filter_candidates(codes, spot_df, exclude_codes, min_amount=MIN_AMOUNT):
    """用全市场 spot 硬过滤成分股(零额外请求)。返回 (kept_spot_rows, not_in_spot_count)。"""
    spot = {str(r["code"]): r for r in spot_df.to_dict("records")}
    kept, not_in_spot = [], 0
    for code in codes:
        row = spot.get(str(code))
        if row is None:
            not_in_spot += 1                      # 北交所/新上市/退市整理 → 跳过并计数
            continue
        if "ST" in str(row["name"] or "").upper():
            continue
        if str(code) in exclude_codes:
            continue
        price = _num(row["price"])
        vol = _num(row["volume"])
        chg = _num(row["change_pct"])
        if not price or not vol or chg is None:
            continue                              # 停牌
        if PRICE_FLOOR is not None and price < PRICE_FLOOR:
            continue                              # 低价护栏(spec §8)
        if chg >= an.limit_threshold(str(code)):
            continue                              # 涨停买不进
        if chg <= BIG_DROP_PCT:
            continue                              # 大跌
        amt = _num(row["amount"])
        if amt is not None and amt < min_amount:
            continue                              # 流动性不足
        kept.append(row)
    return kept, not_in_spot


def rank_candidates(scored, per_sector):
    """风险尾部硬过滤 + 高位硬过滤;composite 降序取前 per_sector。

    探针证伪「风险升序排序」(risk 是尾部信号非均值信号,最低风险档 [0,20) 反而是最差
    均值),故排序维持 composite 降序;风险只作硬尾砍(≥RISK_HARD_CUT)。
    """
    def keep(x):
        s = x["scores"]
        risk = s.get("risk")
        pos60 = s.get("pos60")
        if risk is None or risk >= RISK_HARD_CUT:
            return False
        if pos60 is not None and pos60 >= EXT_HARD_CUT:
            return False
        return x["verdict"] != "回避"
    kept = [x for x in scored if keep(x)]
    kept.sort(key=lambda x: -(x["composite"] or 0))
    return kept[:per_sector]


def _score_candidate(row, daily_df, now, sector_composite=None):
    quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
             "volume": _num(row["volume"]), "amount": _num(row["amount"]),
             "high": _num(row.get("high")), "low": _num(row.get("low")),
             "open": _num(row.get("open"))}
    scores = an.score_stock(daily_df, quote, now)
    if scores["composite"] is None:            # 数据不足(历史<61根)→ 跳过
        return None
    bonus = sector_bonus(sector_composite)
    final = an.stock_composite_v3(scores["position"], scores["volume_price"],
                                  scores["trend"], scores["signal"], scores["risk"], bonus)
    return {"code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
            "price": quote["price"], "change_pct": quote["change_pct"],
            "scores": scores, "composite": final,   # scores["composite"] 为加成前分;消费方一律读顶层 composite(加成后)
            "verdict": an.stock_verdict(final),
            "signal_close": round(float(daily_df["close"].iloc[-1]), 2),
            "close_date": str(daily_df["date"].iloc[-1])}


def _g5_of(daily_df):
    """个股 5 日区间涨幅 = 末根/6根前 − 1(spec §3.3 rel 分位基数)。不足 6 根 → None。"""
    if daily_df is None or len(daily_df) < 6:
        return None
    try:
        return float(daily_df["close"].iloc[-1]) / float(daily_df["close"].iloc[-6]) - 1.0
    except Exception:
        return None


def _rel_strengths(base_g5):
    """板块内 5 日涨幅分位(spec §3.3):均值秩 ×100。base_g5=[(code, g5), ...];基数<3 → {}。"""
    if len(base_g5) < 3:
        return {}
    pcts = pd.Series([g for _, g in base_g5]).rank(pct=True, method="average") * 100.0
    return {code: float(p) for (code, _), p in zip(base_g5, pcts)}


def _apply_hot_weights_one(x, sector_composite, rel=None):
    """单只个股 P0b 权重重算(spec §3.2)。non-hot 或模式 off → 原样返回;
    rel 非 None 用 HOT_REL_WEIGHTS,signal 模式用 HOT_SIGNAL_WEIGHTS,否则回退 v3 权重(weights=None)。"""
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return x
    if rel is not None:
        w = an.HOT_REL_WEIGHTS
    elif HOT_WEIGHT_MODE == "signal":
        w = an.HOT_SIGNAL_WEIGHTS
    else:
        w = None
    scores = x["scores"]
    bonus = sector_bonus(sector_composite)   # 自加成前 composite 重算,无重复加成
    final = an.stock_composite_v3(scores["position"], scores["volume_price"],
                                  scores["trend"], scores["signal"], scores["risk"],
                                  bonus, rel_strength=rel, weights=w)
    x["composite"] = final
    x["verdict"] = an.stock_verdict(final)
    if rel is not None:
        x["rel_strength"] = round(rel, 2)
    return x


def _apply_hot_weights(ranked, base_g5, sector_composite):
    """热板块内 P0b 权重重算(spec §3.2/§3.3)。non-hot 或模式 off → 原样返回。"""
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return ranked
    rel_map = _rel_strengths(base_g5) if HOT_WEIGHT_MODE == "rel_strength" else {}
    for x in ranked:
        rel = rel_map.get(x["code"][-6:]) if rel_map else None   # x["code"] 带 sh/sz 前缀 → 截 6 位
        _apply_hot_weights_one(x, sector_composite, rel=rel)
    return ranked


def _score_sector_stocks(spot_rows, get_daily, now, per_sector, sector_composite=None):
    """并发拉日线并打分;热板块内按 P0b 权重两遍重算 final/verdict。返回 (ranked, daily_failed, any_stale, dates)。"""
    ranked, daily_failed, any_stale, dates = [], 0, False, set()
    base_g5 = []          # (code, g5) — 分位基数 = 过滤后成分股 + ≥6根(spec §3.3)
    if not spot_rows:
        return [], 0, False, set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily, str(r["code"])): r for r in spot_rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                any_stale = any_stale or bool(stale)
                if len(daily):
                    dates.update(str(x) for x in daily["date"])
                g5 = _g5_of(daily)
                if g5 is not None:
                    base_g5.append((str(row["code"]), g5))
                scored = _score_candidate(row, daily, now, sector_composite)
                if scored is not None:
                    ranked.append(scored)
            except Exception:
                daily_failed += 1                # 单只失败 → 跳过,同板块其余继续
    ranked = _apply_hot_weights(ranked, base_g5, sector_composite)
    return rank_candidates(ranked, per_sector), daily_failed, any_stale, dates


def _signal_date(now, close_date):
    """推荐生成交易日:工作日盘中/收盘后 → now.date();否则回退 close_date(开盘前/周末/节假日)。"""
    if now.weekday() < 5 and (an.is_trading_time(now) or an.is_after_close(now)):
        return now.date().isoformat()
    return close_date


def pick_leaders(spot_rows, total=5, exclude_codes=frozenset()):
    """板块龙头/强势股:龙头池(成交额 top3)+ 强势池(涨幅 top3)合并去重取前 total。

    纯函数,无网络。轻过滤:排除新股(exclude_codes)、ST、停牌(price/volume 空);保留涨停股。
    """
    rows = []
    for r in spot_rows:
        code = str(r["code"])
        if code in exclude_codes or "ST" in str(r.get("name") or "").upper():
            continue
        price, vol = _num(r.get("price")), _num(r.get("volume"))
        if not price or not vol:              # 停牌(价格/成交量缺失)
            continue
        rows.append(r)
    leader_pool = sorted((x for x in rows if _num(x.get("amount")) is not None),
                         key=lambda x: -(_num(x["amount"]) or 0))[:min(3, total)]
    strong_pool = sorted((x for x in rows if _num(x.get("change_pct")) is not None),
                         key=lambda x: -(_num(x["change_pct"]) or 0))[:min(3, total)]
    leader_codes = {str(x["code"]) for x in leader_pool}
    strong_codes = {str(x["code"]) for x in strong_pool}
    out, seen = [], set()
    for x in leader_pool + strong_pool:
        code = str(x["code"])
        if code in seen:
            continue
        seen.add(code)
        tag = ("龙头+强势" if (code in leader_codes and code in strong_codes)
               else ("龙头" if code in leader_codes else "强势"))
        out.append({"code": code, "name": str(x.get("name") or ""),
                    "price": _num(x.get("price")), "change_pct": _num(x.get("change_pct")),
                    "amount": _num(x.get("amount")), "tag": tag})
        if len(out) >= total:
            break
    return out


def tier_for_verdict(verdict):
    """五档 verdict → 档位(规格 §4.6):强烈关注/关注→可介入;持有/跟踪→观察;其余→None。"""
    if verdict in ("强烈关注", "关注"):
        return "可介入"
    if verdict == "持有/跟踪":
        return "观察"
    return None


def sector_bonus(sector_composite, quality=None):
    """板块共振加成(规格 §5):≥68→+8;60≤x<68→+4;x<50→−5;其余→+0;缺失→0。

    阈值经 Task 8 校准(§9.4)按 live 板块 composite 分布重锚:原 70/55/40 下
    +4 档覆盖约 61% 板块(近乎恒触发)、−5 仅 3%;重锚后 +8≈21%、+4≈29%、0≈42%、−5≈8%。
    −5 边界刻意保留规格 <50 而非对齐 P30 锚(55.26):对底部 30% 板块统一 −5 过激进,
    只惩罚极端板块(实测约 8%),这是有意的不对称,非疏漏。

    P3 决策(BONUS_GE75/BONUS_QUALITY_GATE)可加门槛:GE75 时 +8 档提到 ≥75;
    QUALITY_GATE 时 quality<50 不给正加成。quality=None → 不 gate。
    """
    if sector_composite is None:
        return 0
    if quality is not None and BONUS_QUALITY_GATE and quality < 50:
        return 0
    ge75 = BONUS_GE75
    hi = 75.0 if ge75 else 68.0
    if sector_composite >= hi:
        return 8
    if sector_composite >= 60:
        return 4
    if sector_composite < 50:
        return -5
    return 0


def bias_pct(daily_df, price):
    """现价偏离 MA20 百分比;(price - ma20)/ma20*100。MA20 缺失 → None。"""
    price = _num(price)
    if price is None:
        return None
    if not len(daily_df):
        return None
    df = an.add_ma(daily_df, (20,))
    ma20 = _num(df["ma20"].iloc[-1])
    if not ma20:
        return None
    return round((price - ma20) / ma20 * 100, 2)


def build_recommend(summary_df, spot_df, db, type_key, now, top_sectors=3, per_sector=5):
    """编排:选板块 → 解析成分股 → 并发打分 → 组装。返回 (payload, stale_any)。"""
    strong = select_sectors(summary_df, db, type_key, store, _market_turnover(spot_df), now)
    new_codes = ds.get_new_stocks()
    sectors, skipped = [], []
    all_dates, close_dates = set(), []
    stale_any = False
    diagnostics = {"stocks_not_in_spot": 0, "stocks_daily_failed": 0}
    for s in strong:
        if len(sectors) >= top_sectors:
            break
        try:
            res = ds.resolve_sector_constituents(s["name"])
        except Exception:                        # 网络/模式失败 → 降级跳过
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": "source_fail"})
            continue
        if not res["ok"]:
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": res["reason"]})
            continue
        kept, not_in_spot = filter_candidates(res["codes"], spot_df, new_codes)
        diagnostics["stocks_not_in_spot"] += not_in_spot
        ranked, daily_failed, any_stale, s_dates = _score_sector_stocks(
            kept, ds.get_stock_daily, now, per_sector, s["composite"])
        all_dates.update(s_dates)
        close_dates.extend(x["close_date"] for x in ranked)
        diagnostics["stocks_daily_failed"] += daily_failed
        stale_any = stale_any or any_stale
        if not ranked:
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": "too_few"})
            continue
        sectors.append({
            "code": "%s:%s" % (type_key, s["code"]), "name": s["name"],
            "verdict": s["verdict"], "composite_score": s["composite"],
            "overheated": bool(s.get("overheated", False)),
            "match_type": res["match_type"], "constituent_source": res["source_name"],
            "stocks": [{"code": x["code"], "name": x["name"], "price": x["price"],
                        "change_pct": x["change_pct"], "scores": x["scores"],
                        "composite": round(x["composite"], 2),
                        "verdict": x["verdict"], "signal_close": x["signal_close"],
                        "rel_strength": x.get("rel_strength")} for x in ranked],
        })
    payload = {"strong_count": len(strong), "sectors": sectors,
               "skipped_sectors": skipped, "diagnostics": diagnostics}
    # P1 快照出参:两日期语义分离(§5.2)
    close_date = max(close_dates, key=close_dates.count) if close_dates else None   # 众数;停牌缺末根时取多数
    signal_date = _signal_date(now, close_date)
    dates_sorted = sorted(all_dates)
    # prev_trading_date 需 signal_date 锚点;全板块无得分(close_dates 空 → signal_date=None)时直接置 None,避免 d < None 崩溃
    prev_trading_date = (max((d for d in dates_sorted if d < signal_date), default=None)
                         if signal_date is not None else None)
    payload["signal_date"] = signal_date
    payload["close_date"] = close_date
    payload["prev_trading_date"] = prev_trading_date
    payload["trading_dates"] = dates_sorted[-20:]    # 最近 20 个交易日,供 Task 4 算 gap_days
    return (payload, stale_any)


def collect_actionable_leaders(summary_df, spot_df, db, type_key, now, resolve_fn, get_daily_fn):
    """汇总全部已映射板块的龙头/强势股,过滤为可介入/观察两档。返回 (payload, stale_any)。

    resolve_fn / get_daily_fn 可注入(测试 mock),生产传 ds.resolve_sector_constituents / ds.get_stock_daily。
    板块评价仅作展示列(不过滤板块);跨板块同 code 去重按板块 composite 降序先到先得。
    """
    sectors = score_all_sectors(summary_df, db, type_key, store, _market_turnover(spot_df), now)
    new_codes = ds.get_new_stocks()
    spot_index = {str(r["code"]): r for r in spot_df.to_dict("records")}
    tasks, skipped = [], []
    for i, s in enumerate(sectors):
        try:
            res = resolve_fn(s["name"])
        except Exception:
            skipped.append({"name": s["name"], "reason": "source_fail"})
            continue
        if not res["ok"]:
            skipped.append({"name": s["name"], "reason": res.get("reason", "source_fail")})
            continue
        leaders = pick_leaders([spot_index[c] for c in res.get("codes", []) if c in spot_index],
                               total=5, exclude_codes=new_codes)
        for L in leaders:
            tasks.append((i, s, L, spot_index[L["code"]]))

    def work(i, s, L, row):
        daily, stale = get_daily_fn(str(L["code"]))
        scored = _score_candidate(row, daily, now, s["composite"])
        if scored is None:
            return None, stale
        _apply_hot_weights_one(scored, s["composite"], rel=None)   # signal 模式 rel 恒 None
        tier = tier_for_verdict(scored["verdict"])
        if tier is None:
            return None, stale
        return {
            "_rank": i,
            "code": scored["code"], "name": scored["name"],
            "price": scored["price"], "change_pct": scored["change_pct"],
            "tag": L["tag"], "tier": tier,
            "sector_code": "%s:%s" % (type_key, s["code"]),
            "sector_name": s["name"], "sector_verdict": s["verdict"],
            "sector_composite": s["composite"],
            "position": scored["scores"]["position"],
            "trend": scored["scores"]["trend"], "volume_price": scored["scores"]["volume_price"],
            "signal": scored["scores"]["signal"], "composite": round(scored["composite"], 2),
            "risk": scored["scores"]["risk"],
            "bias_pct": bias_pct(daily, scored["price"]),
        }, stale

    items, daily_failed, stale_any = [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(work, i, s, L, row): 1 for i, s, L, row in tasks}
        for fut in as_completed(futures):
            try:
                item, stale = fut.result()
                stale_any = stale_any or bool(stale)
                if item is not None:
                    items.append(item)
            except Exception:
                daily_failed += 1              # 单只日线/打分失败 → 跳过该股,其余继续

    # 跨板块去重:score_all_sectors 已按 composite 降序 → _rank 即板块序,先到先得
    items.sort(key=lambda x: (x["_rank"], -(x["composite"] or 0)))
    seen, unique = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        it.pop("_rank")                        # 内部字段,出参前移除
        unique.append(it)
    # 排序:可介入在前,观察在后;组内按综合分降序
    unique.sort(key=lambda x: (x["tier"] != "可介入", -(x["composite"] or 0)))
    return ({"sectors_scanned": len(sectors), "total": len(unique), "items": unique,
             "skipped_sectors": skipped,
             "diagnostics": {"stocks_daily_failed": daily_failed}},
            stale_any)


# ---- 波段候选(方向中性可操作性筛选;设计 2026-08-16 §1) ----

def swing_pool(spot_df, exclude_codes, min_amount=MIN_AMOUNT, top_n=200):
    """波段候选池(方向中性):排除 ST/停牌(price/volume 缺失)/新股(exclude_codes)/
    涨停(change_pct ≥ limit_threshold);amount ≥ min_amount;按成交额降序取前 top_n。

    与 pick_leaders 不同:不用「涨幅 top3」追涨,不保留涨停(买不进),纯流动性池。"""
    rows = []
    for r in spot_df.to_dict("records"):
        code = str(r["code"])
        if code in exclude_codes:
            continue
        if "ST" in str(r.get("name") or "").upper():
            continue
        price, vol, chg = _num(r.get("price")), _num(r.get("volume")), _num(r.get("change_pct"))
        if not price or not vol or chg is None:
            continue                              # 停牌
        if chg >= an.limit_threshold(code):
            continue                              # 涨停买不进
        amt = _num(r.get("amount"))
        if amt is None or amt < min_amount:
            continue                              # 流动性不足
        rows.append(r)
    rows.sort(key=lambda x: -(_num(x["amount"]) or 0))
    return rows[:top_n]


def swing_eligible(turnover_frac, risk, pos60):
    """波段可操作性硬过滤(方向中性)→ (ok, reason)。命中即剔除:
    1. turnover_frac 缺失或 <0.02 → 换手不足;
    2. risk 非 None 且 ≥ RISK_HARD_CUT → 风险尾部;
    3. pos60 非 None 且 ≥ EXT_HARD_CUT → 高位。
    risk/pos60 为 None(历史 <61 根)时 fail-open(不因无法评估而剔除,与 filter_candidates 一致)。"""
    tf = _num(turnover_frac)
    if tf is None or tf < 0.02:
        return False, "换手不足"
    r = _num(risk)
    if r is not None and r >= RISK_HARD_CUT:
        return False, "风险尾部"
    p = _num(pos60)
    if p is not None and p >= EXT_HARD_CUT:
        return False, "高位"
    return True, ""


def collect_swing_candidates(spot_df, get_daily_fn, now, regime,
                             exclude_codes=None, resolve_sectors_fn=None,
                             top_n=60, pool_n=200):
    """编排波段候选(方向中性,无收益预测)。返回 (payload, stale_any)。

    regime 由调用方注入(env.load_cached_regime 的 swing 块组装);item 只含
    流动性/波动/风险尾/高位等方向中性字段,无 composite/verdict/position。
    排序:换手率降序 → 振幅降序 → 成交额降序;跨代码去重;截断 top_n。"""
    if exclude_codes is None:
        exclude_codes = ds.get_new_stocks()
    if resolve_sectors_fn is None:
        resolve_sectors_fn = ds.resolve_code_sectors
    pool = swing_pool(spot_df, exclude_codes, MIN_AMOUNT, pool_n)
    items, daily_failed, stale_any = [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily_fn, str(r["code"])): r for r in pool}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                stale_any = stale_any or bool(stale)
                if len(daily) and "turnover" in daily.columns:
                    turnover_frac = _num(daily["turnover"].iloc[-1])
                else:
                    turnover_frac = None
                amp = an.amp20(daily)
                quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
                         "volume": _num(row["volume"]), "amount": _num(row["amount"]),
                         "high": _num(row.get("high")), "low": _num(row.get("low")),
                         "open": _num(row.get("open"))}
                scores = an.score_stock(daily, quote, now)
                risk, pos60 = scores["risk"], scores["pos60"]
                ok, _reason = swing_eligible(turnover_frac, risk, pos60)
                if not ok:
                    continue
                secs = resolve_sectors_fn(str(row["code"]))
                items.append({
                    "code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
                    "price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
                    "amount": _num(row["amount"]),
                    "turnover_pct": round(turnover_frac * 100, 2) if turnover_frac is not None else None,
                    "amp20": amp, "risk": risk,
                    "pos60": round(float(pos60), 4) if pos60 is not None else None,
                    "sector_name": secs[0] if secs else None,
                    "_turnover_frac": turnover_frac,   # 排序键,出参前移除
                })
            except Exception:
                daily_failed += 1                # 单只日线失败 → 跳过,其余继续
    items.sort(key=lambda x: (x["_turnover_frac"] is None, -(x["_turnover_frac"] or 0),
                              x["amp20"] is None, -(x["amp20"] or 0),
                              -(x["amount"] or 0)))
    seen, unique = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        it.pop("_turnover_frac")
        unique.append(it)
    return ({"regime": regime, "total": len(unique), "items": unique[:top_n],
             "diagnostics": {"stocks_daily_failed": daily_failed}},
            stale_any)


# ---- 低位透视(方向中性;2026-09-17) ----

def _dd60(daily_df):
    """距 60 日最高价的回撤(%),≤0;缺失→None。"""
    if not len(daily_df):
        return None
    hi = _num(daily_df["high"].iloc[-60:].max())
    c = _num(daily_df["close"].iloc[-1])
    if not hi or not c:
        return None
    return round((c / hi - 1.0) * 100.0, 2)


def collect_low_position(spot_df, get_daily_fn, now, regime=None, exclude_codes=None,
                         resolve_sectors_fn=None, top_n=60, pool_n=300):
    """低位透视:把 position 因子背后的「60日位置最低」的股票显性化。

    【定位:透视工具,不是策略;不预测方向、不排收益序】
    Phase 0 预注册检验已判负(_analysis/lowpos_panic_probe.py,gitignored):
    恐慌日低位组 vs 同日等权全市场,主口径 oo1 spread −0.071%(单边 p=0.666,n=41,
    80% 功效下可检出 0.407%),oo/oc × h1/3/5/10 全部噪声,非恐慌日无交互(p=0.678),
    子期间前后半符号相反 → 低位反转【无选股 alpha】。故本函数只回答
    「模型为什么选/不选它」,即 position 分与 52 分门槛的关系。

    易错点:compute_position_score 是 pos60 的减函数(0.5*100*(1−pos60)),
    故 position 分【高】= pos60【低】= 60日区间【低位】。本函数按 position 降序
    = 越低位越靠前。

    regime 由调用方注入;排序 position 降序;跨代码去重;截断 top_n。
    返回 item 的 composite/verdict/tier 均为【未含板块共振加成与热权重】的个股口径。

    底池沿用 swing_pool(方向中性),故本表**看不到**当日涨停股(买不进,被排除)、
    ST、停牌、成交额 < MIN_AMOUNT 者,以及日线不足 61 根的新股。即「低位透视」是
    在这套可交易池内的透视,不是全市场普查 —— 一只今日涨停的低位股不会出现在这里。
    """
    if exclude_codes is None:
        exclude_codes = ds.get_new_stocks()
    if resolve_sectors_fn is None:
        resolve_sectors_fn = ds.resolve_code_sectors
    pool = swing_pool(spot_df, exclude_codes, MIN_AMOUNT, pool_n)
    items, daily_failed, stale_any = [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily_fn, str(r["code"])): r for r in pool}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                stale_any = stale_any or bool(stale)
                price = _num(row["price"])
                quote = {"price": price, "change_pct": _num(row["change_pct"]),
                         "volume": _num(row["volume"]), "amount": _num(row["amount"]),
                         "high": _num(row.get("high")), "low": _num(row.get("low")),
                         "open": _num(row.get("open"))}
                sc = an.score_stock(daily, quote, now)
                if sc["position"] is None:
                    continue
                verdict = an.stock_verdict(sc["composite"])
                secs = resolve_sectors_fn(str(row["code"]))
                items.append({
                    "code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
                    "price": price, "change_pct": _num(row["change_pct"]),
                    "amount": _num(row["amount"]),
                    "position": round(float(sc["position"]), 2),
                    "pos60": round(float(sc["pos60"]), 4) if sc["pos60"] is not None else None,
                    "dd60_pct": _dd60(daily),
                    "bias_pct": bias_pct(daily, price),
                    "risk": round(float(sc["risk"]), 2) if sc["risk"] is not None else None,
                    "composite": (round(float(sc["composite"]), 2)
                                  if sc["composite"] is not None else None),
                    "verdict": verdict,
                    "tier": tier_for_verdict(verdict),
                    "sector_name": secs[0] if secs else None,
                })
            except Exception:
                daily_failed += 1                # 单只日线失败 → 跳过,其余继续
    items.sort(key=lambda x: (-(x["position"] or 0.0), x["code"]))
    seen, unique = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        unique.append(it)
    return ({"regime": regime, "total": len(unique), "items": unique[:top_n],
             "basis": "个股因子口径:composite/verdict/tier 未含板块共振加成与热权重",
             "diagnostics": {"stocks_daily_failed": daily_failed}}, stale_any)
