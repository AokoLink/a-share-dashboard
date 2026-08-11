# -*- coding: utf-8 -*-
"""推荐层:板块→个股双层推荐。select/filter/rank 为纯函数,build_recommend 编排。"""
from concurrent.futures import ThreadPoolExecutor, as_completed

import analysis as an
import data_source as ds
import store

MAX_WORKERS = 8
MIN_AMOUNT = 1e8            # 流动性下限:1 亿元
BIG_DROP_PCT = -7.0         # 大跌排除线(不设对称跌停,创业板/科创板 -7 非跌停)
QUALIFYING_VERDICTS = ("建议关注", "跟踪(热点延续)")


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


def select_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """对全部板块用公共打分函数打分,返回强势板块(verdict 入选)按 composite 降序。"""
    rows = []
    for _, r in summary_df.iterrows():
        scores = an.collect_sector_metrics(
            db, type_key, str(r["code"]), _num(r["change_pct"]), _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
            store_ctx, market_turnover, now, True)
        rows.append({"code": str(r["code"]), "name": str(r["name"]),
                     "composite": scores["composite"], "verdict": scores["verdict"],
                     "scores": scores})
    strong = [x for x in rows if x["verdict"] in QUALIFYING_VERDICTS]
    strong.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
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
    """剔除规避/高风险,按 composite 降序取前 per_sector。"""
    kept = [x for x in scored if x["verdict"] != "规避" and x["scores"]["risk"] < 70]
    kept.sort(key=lambda x: x["scores"]["composite"], reverse=True)
    return kept[:per_sector]


def _score_candidate(row, daily_df, now):
    quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
             "volume": _num(row["volume"]), "amount": _num(row["amount"])}
    scores = an.score_stock(daily_df, quote, now)
    return {"code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
            "price": quote["price"], "change_pct": quote["change_pct"],
            "scores": scores, "verdict": scores["verdict"]}


def _score_sector_stocks(spot_rows, get_daily, now, per_sector):
    """并发拉日线并打分。返回 (ranked, daily_failed, any_stale)。"""
    ranked, daily_failed, any_stale = [], 0, False
    if not spot_rows:
        return [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily, str(r["code"])): r for r in spot_rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                any_stale = any_stale or bool(stale)
                ranked.append(_score_candidate(row, daily, now))
            except Exception:
                daily_failed += 1                # 单只失败 → 跳过,同板块其余继续
    return rank_candidates(ranked, per_sector), daily_failed, any_stale


def build_recommend(summary_df, spot_df, db, type_key, now, top_sectors=3, per_sector=5):
    """编排:选板块 → 解析成分股 → 并发打分 → 组装。返回 (payload, stale_any)。"""
    strong = select_sectors(summary_df, db, type_key, store, _market_turnover(spot_df), now)
    new_codes = ds.get_new_stocks()
    sectors, skipped = [], []
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
        ranked, daily_failed, any_stale = _score_sector_stocks(kept, ds.get_stock_daily, now, per_sector)
        diagnostics["stocks_daily_failed"] += daily_failed
        stale_any = stale_any or any_stale
        if not ranked:
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": "too_few"})
            continue
        sectors.append({
            "code": "%s:%s" % (type_key, s["code"]), "name": s["name"],
            "verdict": s["verdict"], "composite_score": s["composite"],
            "match_type": res["match_type"], "constituent_source": res["source_name"],
            "stocks": [{"code": x["code"], "name": x["name"], "price": x["price"],
                        "change_pct": x["change_pct"], "scores": x["scores"],
                        "verdict": x["verdict"]} for x in ranked],
        })
    return ({"strong_count": len(strong), "sectors": sectors,
             "skipped_sectors": skipped, "diagnostics": diagnostics},
            stale_any)
