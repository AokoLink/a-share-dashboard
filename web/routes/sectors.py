# -*- coding: utf-8 -*-
"""板块路由 /api/sectors、/api/sector。"""
from flask import request

from core import analysis as an
from core import data_source as ds
from core import recommend
from core import store
from web import config
from web import util


def register(app):
    db_path = app.config["DB"]

    @app.route("/api/sectors")
    def api_sectors():
        type_key = request.args.get("type", "industry")
        if type_key not in config.SECTOR_TYPES:
            return util.err("BAD_PARAM", "type 必须为 industry", 400)
        try:
            top = int(request.args.get("top", "60"))
        except ValueError:
            return util.err("BAD_PARAM", "top 必须为整数", 400)
        top = max(1, min(200, top))
        search = (request.args.get("search") or "").strip()
        try:
            summary, stale = ds.get_sector_summary(type_key)
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
        today = util.today()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0

        # 计算当日 rank 并持久化(仅交易日);change_pct 可能为 None → _sort_key 排序
        if an.is_trading_time(now):
            ranked = summary.iloc[summary["change_pct"].map(util._sort_key).sort_values(ascending=False).index]
            for i, r in ranked.iterrows():
                store.upsert_sector_daily(
                    db_path, today, type_key, r["code"], r["name"], util._num(r["change_pct"]),
                    None, util._num(r["turnover"]), i + 1)

        rows = []
        for _, r in summary.iterrows():
            code = str(r["code"])
            chg = util._num(r["change_pct"])
            scores = an.collect_sector_metrics(
                db_path, type_key, code, chg, util._num(r["turnover"]),
                util._num(r["up_count"]), util._num(r["down_count"]), util._num(r["leader_change_pct"]),
                store, market_turnover, now, True)
            rows.append({
                "code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                "index_change_pct": chg,
                "emotion_score": scores["emotion"], "strength_score": scores["strength"],
                "risk_score": scores["risk"], "composite_score": scores["composite"],
                "verdict": scores["verdict"], "overheated": scores["overheated"],
                "consecutive_days": scores["consecutive_days"],
                "data_complete": True,
            })

        if search:
            kw = search.lower()
            matched = [x for x in rows if kw in str(x["name"]).lower()]
            total = len(matched)
            rows = matched
        else:
            total = len(rows)
            rows = sorted(rows, key=lambda x: (x["composite_score"] is None, -(x["composite_score"] or 0)))
        return util.ok({"type": type_key, "total": total, "sectors": rows[:top]}, stale=stale or spot_stale)

    @app.route("/api/sector")
    def api_sector():
        parsed = util._parse_sector_code(request.args.get("code"))
        if parsed is None:
            return util.err("BAD_PARAM", "code 必须形如 industry:885887", 400)
        type_key, code = parsed
        try:
            summary, stale1 = ds.get_sector_summary(type_key)
            hist, stale2 = ds.get_sector_index_history(code, type_key)
            spot, stale3 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        row = summary[summary["code"] == code]
        if row.empty:
            return util.err("BAD_PARAM", "未找到该板块", 400)
        r = row.iloc[0]
        chg = util._num(r["change_pct"])
        now = util.now()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0
        scores = an.collect_sector_metrics(
            app.config["DB"], type_key, code, chg, util._num(r["turnover"]),
            util._num(r["up_count"]), util._num(r["down_count"]), util._num(r["leader_change_pct"]),
            store, market_turnover, now, True)
        verdict = scores["verdict"]
        hist_rows = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                      "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                     for x in hist.to_dict("records")]
        leaders, leaders_status, leaders_source = [], "ok", None
        try:
            res = ds.resolve_sector_constituents(str(r["name"]))
        except Exception:
            leaders_status = "source_fail"
        else:
            if not res["ok"]:
                leaders_status = res.get("reason", "source_fail")    # no_mapping | ambiguous
            else:
                spot_index = {str(x["code"]): x for x in spot.to_dict("records")}
                rows = [spot_index[c] for c in res.get("codes", []) if c in spot_index]
                leaders = recommend.pick_leaders(rows, total=5,
                                                 exclude_codes=ds.get_new_stocks())
                leaders_source = res.get("source_name")
        return util.ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                        "scores": {"emotion": scores["emotion"], "strength": scores["strength"],
                                   "risk": scores["risk"], "composite": scores["composite"]},
                        "verdict": verdict, "overheated": scores["overheated"],
                        "index_history": hist_rows,
                        "leaders": leaders, "leaders_status": leaders_status,
                        "leaders_source": leaders_source},
                       stale=stale1 or stale2 or stale3)
