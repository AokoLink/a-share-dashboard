# -*- coding: utf-8 -*-
"""板块路由 /api/sectors、/api/sector。"""
from datetime import date
from pathlib import Path

from flask import request

from core import analysis as an
from core import data_source as ds
from core import recommend
from core import sector_insights, sector_relations, sector_index_sources
from core import freshness
from core.stage1_data import load_calendar
from core.strategy_lifecycle import observation_clock
from core import store
from web import config
from web import util


def register(app):
    db_path = app.config["DB"]

    def evidence_day():
        day = request.args.get("as_of", util.today())
        date.fromisoformat(day)
        if day > util.today():
            raise ValueError("future_as_of")
        return day

    @app.route("/api/sector-relations")
    def api_sector_relations():
        code = ds.normalize_code(request.args.get("code", ""))
        try:
            day = evidence_day()
            if len(code) != 6 or not code.isdigit():
                raise ValueError("invalid_code")
        except ValueError:
            return util.err("BAD_PARAM", "需要6位代码及不晚于今天的日期", 400)
        snapshot = sector_relations.load(app.config["DAILY_PIPELINE_ROOT"], day)
        return util.ok(sector_relations.feature_for(snapshot, code, day))

    @app.route("/api/sector-rotation")
    def api_sector_rotation():
        try:
            top = max(1, min(200, int(request.args.get("top", "8"))))
            day = evidence_day()
            category = request.args.get("category", "all")
            if category not in ("all", *sector_relations.CATEGORIES):
                raise ValueError("category")
        except ValueError:
            return util.err("BAD_PARAM", "top、日期或板块类型无效", 400)
        snapshot = sector_relations.load(app.config["DAILY_PIPELINE_ROOT"], day)
        if snapshot is None:
            return util.ok({"status": "not_run", "as_of": day, "rows": [], "read_only": True,
                "benchmark": "sh000001", "version": sector_insights.VERSION,
                "selection_basis": "all_available_sectors_then_relative20_rank", "candidate_universe": {"total": 0}})
        rows = [r for r in snapshot["rows"] if category == "all" or r["category"] == category]
        return util.ok({**{k: v for k, v in snapshot.items() if k not in ("rows", "relations", "input_artifacts")},
                        "rows": [{**r, "code": r["id"]} for r in rows[:top]], "read_only": True,
                        "filtered_total": len(rows)})

    @app.route("/api/sector-index-rotation")
    def api_sector_index_rotation():
        try:
            top = max(1, min(200, int(request.args.get("top", "8"))))
            day = evidence_day() if "as_of" in request.args else None
            provider = request.args.get("provider", "ths")
            if provider not in ("ths", "sw"):
                raise ValueError("provider")
        except ValueError:
            return util.err("BAD_PARAM", "top 或日期无效", 400)
        try:
            report = sector_index_sources.load(app.config["DAILY_PIPELINE_ROOT"], day, provider)
        except (OSError, ValueError, KeyError, TypeError):
            return util.err("EVIDENCE_CHANGED", "原生指数留档校验失败，请重新采集", 500)
        if report is None:
            return util.ok({"status": "not_run", "as_of": day, "rows": [], "total": 0, "read_only": True})
        calendar = load_calendar(Path(app.config["DAILY_PIPELINE_ROOT"]) / "calendar.json")
        now = observation_clock(util.now())
        closed = [d for d in (calendar or {}).get("dates", []) if observation_clock(d + "T15:00:00+08:00") <= now]
        expected = max(closed) if closed and now.date().isoformat() <= calendar["last"] else None
        state = "unknown" if expected is None else "current" if report["as_of"] == expected else "historical"
        return util.ok({**{k: v for k, v in report.items() if k not in ("rows", "catalog_ref", "catalog_refs", "benchmark_ref")},
                        "freshness": {"status": state, "expected_closed_session": expected},
                        "rows": [{k: v for k, v in r.items() if k not in ("history", "membership_response")} for r in report["rows"][:top]]},
                       stale=state != "current")

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
            for rank, (_, r) in enumerate(ranked.iterrows(), start=1):
                up, down = util._num(r["up_count"]), util._num(r["down_count"])
                up_ratio = up / (up + down) if up is not None and down is not None and up + down > 0 else None
                store.upsert_sector_daily(
                    db_path, today, type_key, r["code"], r["name"], util._num(r["change_pct"]),
                    up_ratio, util._num(r["turnover"]), rank)

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
        snapshot = sector_relations.load(app.config["DAILY_PIPELINE_ROOT"], util.today())
        evidence = next((x for x in (snapshot or {}).get("rows", []) if x["id"] == f"{type_key}:{code}"), None)
        empty_roles = {"trading_core": [], "price_leaders": [], "industrial_core": [],
                       "industrial_core_status": "fundamentals_unavailable", "price_status": "unavailable"}
        relative = (evidence or {}).get("relative", {"benchmark": "sh000001", "returns": {}, "as_of": None})
        roles = (evidence or {}).get("roles", empty_roles)
        relation = next((x for x in (snapshot or {}).get("relations", []) if x["id"] == f"{type_key}:{code}"), None)
        spot_index = {str(x["code"]): x for x in spot.to_dict("records")}
        members = [spot_index[c] for c in (relation or {}).get("members", []) if c in spot_index]
        leaders = recommend.pick_leaders(members, total=5) if relation and relation["status"] == "verified_current" else []
        insights = {"version": sector_insights.VERSION, "generated_at": (snapshot or {}).get("generated_at"),
                    "relative_strength": relative, "breadth": (evidence or {}).get("breadth"),
                    "state": (evidence or {}).get("state", "证据不足"),
                    "state_basis": "descriptive_not_predictive", "roles": roles,
                    "observations": store.get_sector_observations(db_path, type_key, code),
                    "membership": relation, "membership_basis": "observed_day_only_not_historical",
                    "snapshot_sha256": (snapshot or {}).get("snapshot_sha256"),
                    "freshness": freshness.assess(relative.get("as_of"), now=now),
                    "status": (snapshot or {}).get("status", "not_run")}
        return util.ok({"code": f"{type_key}:{code}", "name": str(r["name"]),
                        "scores": {"emotion": scores["emotion"], "strength": scores["strength"],
                                   "risk": scores["risk"], "composite": scores["composite"]},
                        "verdict": verdict, "overheated": scores["overheated"], "index_history": hist_rows,
                        "insights": insights, "leaders": leaders,
                        "leaders_status": (relation or {}).get("status", "unknown"),
                        "leaders_source": (relation or {}).get("source")},
                       stale=stale1 or stale2 or stale3 or snapshot is None)
