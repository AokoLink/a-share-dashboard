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
    """个股评价 → 可介入档位:关注→可介入,持有/跟踪→观察,其余→None(规避/回调风险/观望排除)。"""
    if verdict == "关注":
        return "可介入"
    if verdict == "持有/跟踪":
        return "观察"
    return None


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
        scored = _score_candidate(row, daily, now)
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
            "trend": scored["scores"]["trend"], "volume_price": scored["scores"]["volume_price"],
            "signal": scored["scores"]["signal"], "composite": scored["scores"]["composite"],
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
