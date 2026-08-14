# -*- coding: utf-8 -*-
"""A股三层分析看板 —— Flask 入口 + API 路由。"""
import logging
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import analysis as an
import data_source as ds
import store
import recommend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "market.db")
SECTOR_TYPES = ("industry",)


def ok(data, stale=False, extra_meta=None):
    meta = {"stale": stale, "updated_at": ds.last_updated_at}
    if extra_meta:
        meta.update(extra_meta)
    return jsonify({"ok": True, "meta": meta, "data": data})


def err(code, message, http):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), http


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _num(v):
    """None/NaN/非数值 → None,其余 → float。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sort_key(v):
    """数值排序键:None/非数值排最后。"""
    n = _num(v)
    return float("-inf") if n is None else n


def _parse_sector_code(raw):
    if not raw or ":" not in raw:
        return None
    t, c = raw.split(":", 1)
    if t not in SECTOR_TYPES or not (c.isdigit() and len(c) == 6):
        return None
    return t, c


def _parse_stock_code(raw):
    if not raw:
        return None
    c = ds.normalize_code(raw)
    if not (c.isdigit() and len(c) == 6):
        return None
    return c


# ---------- API ----------

def register_routes(app):
    db_path = app.config["DB"]

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/market")
    def api_market():
        try:
            spot, spot_stale = ds.get_market_spot()
            indices, idx_stale = ds.get_index_realtime()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        exclude = ds.get_new_stocks()
        breadth = an.compute_breadth(spot, exclude)
        volume_pct = None
        if an.is_after_close(now):
            prev = store.get_market_daily_prev(db_path, today)
            if prev and prev["total_turnover"]:
                volume_pct = round(
                    (breadth["total_turnover"] - prev["total_turnover"]) / prev["total_turnover"] * 100, 2)
        if an.is_trading_time(now):
            index_close = indices[0]["price"] if indices else None
            store.upsert_market_daily(
                db_path, today, breadth["up"], breadth["down"], breadth["flat"],
                breadth["limit_up"], breadth["limit_down"], breadth["total_turnover"],
                index_close, now.strftime("%Y-%m-%d %H:%M:%S"))
        return ok({"indices": indices, "breadth": breadth,
                   "volume_vs_yesterday": {"pct": volume_pct}}, stale=spot_stale or idx_stale)

    @app.route("/api/sectors")
    def api_sectors():
        type_key = request.args.get("type", "industry")
        if type_key not in SECTOR_TYPES:
            return err("BAD_PARAM", "type 必须为 industry", 400)
        try:
            top = int(request.args.get("top", "60"))
        except ValueError:
            return err("BAD_PARAM", "top 必须为整数", 400)
        top = max(1, min(200, top))
        search = (request.args.get("search") or "").strip()
        try:
            summary, stale = ds.get_sector_summary(type_key)
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        today = _today()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0

        # 计算当日 rank 并持久化(仅交易日);change_pct 可能为 None → _sort_key 排序
        if an.is_trading_time(now):
            ranked = summary.iloc[summary["change_pct"].map(_sort_key).sort_values(ascending=False).index]
            for i, r in ranked.iterrows():
                store.upsert_sector_daily(
                    db_path, today, type_key, r["code"], r["name"], _num(r["change_pct"]),
                    None, _num(r["turnover"]), i + 1)

        rows = []
        for _, r in summary.iterrows():
            code = str(r["code"])
            chg = _num(r["change_pct"])
            scores = an.collect_sector_metrics(
                db_path, type_key, code, chg, _num(r["turnover"]),
                _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
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
        return ok({"type": type_key, "total": total, "sectors": rows[:top]}, stale=stale or spot_stale)

    @app.route("/api/sector")
    def api_sector():
        parsed = _parse_sector_code(request.args.get("code"))
        if parsed is None:
            return err("BAD_PARAM", "code 必须形如 industry:885887", 400)
        type_key, code = parsed
        try:
            summary, stale1 = ds.get_sector_summary(type_key)
            hist, stale2 = ds.get_sector_index_history(code, type_key)
            spot, stale3 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        row = summary[summary["code"] == code]
        if row.empty:
            return err("BAD_PARAM", "未找到该板块", 400)
        r = row.iloc[0]
        chg = _num(r["change_pct"])
        now = datetime.now()
        market_turnover = float(spot["amount"].sum()) if len(spot) else 0.0
        scores = an.collect_sector_metrics(
            app.config["DB"], type_key, code, chg, _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
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
        return ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                   "scores": {"emotion": scores["emotion"], "strength": scores["strength"],
                              "risk": scores["risk"], "composite": scores["composite"]},
                   "verdict": verdict, "overheated": scores["overheated"],
                   "index_history": hist_rows,
                   "leaders": leaders, "leaders_status": leaders_status,
                   "leaders_source": leaders_source},
                  stale=stale1 or stale2 or stale3)

    @app.route("/api/stock")
    def api_stock():
        raw = request.args.get("code")
        code6 = _parse_stock_code(raw)
        if code6 is None:
            return err("BAD_PARAM", "code 必须为 6 位股票代码", 400)
        try:
            quote, stale1 = ds.get_stock_quote(code6)
            daily, stale2 = ds.get_stock_daily(code6)
            minute, stale3 = ds.get_stock_minute(code6)
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        scores = an.score_stock(daily, quote, now)
        composite = verdict = tier = None
        sector_resolved = False
        sector_bonus_val = 0.0
        if scores["composite"] is not None:
            names = []
            comps = []
            try:                                   # 尽力而为:板块映射/打分失败不拖垮个股详情
                names = ds.resolve_code_sectors(code6)
                if names:
                    summary, _ = ds.get_sector_summary("industry")
                    spot, _ = ds.get_market_spot()
                    scored_sectors = recommend.score_all_sectors(
                        summary, app.config["DB"], "industry", store,
                        float(spot["amount"].sum()) if len(spot) else 0.0, now)
                    by_name = {s["name"]: s["composite"] for s in scored_sectors}
                    comps = [by_name[n] for n in names if n in by_name and by_name[n] is not None]
            except Exception:
                comps = []
            if comps:
                sector_resolved = True
                sector_bonus_val = recommend.sector_bonus(max(comps))
            final = an.stock_composite_v3(scores["position"], scores["volume_price"],
                                          scores["trend"], scores["signal"],
                                          scores["risk"], sector_bonus_val)
            composite = round(final, 2)
            verdict = an.stock_verdict(final)
            tier = recommend.tier_for_verdict(verdict)
        kline = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                  "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                 for x in daily.tail(250).to_dict("records")]
        intraday = [{"time": str(x["time"]), "price": float(x["price"]),
                     "avg": float(x["avg"]), "volume": float(x["volume"])}
                    for x in minute.to_dict("records")]
        return ok({"code": ds.with_prefix(code6), "name": quote["name"], "quote": quote,
                   "scores": scores, "position": scores["position"],
                   "composite": composite, "verdict": verdict, "tier": tier,
                   "sector_resolved": sector_resolved, "kline": kline,
                   "intraday": intraday},
                  stale=stale1 or stale2 or stale3)

    @app.route("/api/recommend")
    def api_recommend():
        try:
            top_sectors = int(request.args.get("top_sectors", "3"))
            per_sector = int(request.args.get("per_sector", "5"))
        except ValueError:
            return err("BAD_PARAM", "top_sectors/per_sector 必须为整数", 400)
        top_sectors = max(1, min(5, top_sectors))
        per_sector = max(1, min(10, per_sector))
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        payload, stale_cands = recommend.build_recommend(
            summary, spot, db_path, "industry", now, top_sectors, per_sector)
        # P1 快照:持久化本期,出参上一期对比(§5.2)
        prev_snapshot = None
        try:
            prev = store.get_recommend_snapshot_before(db_path, payload["signal_date"])
            if payload["close_date"] is not None:    # 退化快照(close_date=None)不落库,不遮蔽上一期真实快照
                store.upsert_recommend_snapshot(
                    db_path, payload["signal_date"], now.strftime("%Y-%m-%d %H:%M:%S"),
                    payload["close_date"], payload["prev_trading_date"],
                    [{"code": x["code"], "name": x["name"], "signal_close": x["signal_close"]}
                     for sec in payload["sectors"] for x in sec["stocks"]])
            if prev and prev["signal_date"] is not None:
                is_next_day = (prev["signal_date"] == payload["prev_trading_date"])
                td = payload.get("trading_dates") or []
                gap_days = sum(1 for d in td if prev["signal_date"] < d <= payload["signal_date"])
                spot_open = {}
                for _, r in spot.iterrows():
                    code = str(r["code"])
                    o = r.get("open")
                    if o is not None and o == o:      # 非 NaN
                        spot_open[code] = float(o)
                stocks = []
                for s in prev.get("stocks", []):
                    code6 = s.get("code", "")
                    if len(code6) >= 2 and code6[:2] in ("sh", "sz", "bj"):
                        code6 = code6[2:]
                    o = spot_open.get(code6)
                    sc = s.get("signal_close")
                    if o is None or sc is None or not sc:
                        continue
                    stocks.append({"code": s["code"], "name": s.get("name"),
                                   "signal_close": sc, "today_open": o,
                                   "gap_pct": round((o / sc - 1.0) * 100.0, 2)})
                prev_snapshot = {"signal_date": prev["signal_date"],
                                 "close_date": prev["close_date"],
                                 "is_next_day": is_next_day, "gap_days": gap_days,
                                 "stocks": stocks}
        except Exception:
            prev_snapshot = None          # 快照失败不拖垮推荐
        coverage = {
            "strong_candidates": payload["strong_count"],
            "mapped": len(payload["sectors"]),
            "skipped": len(payload["skipped_sectors"]),
            "skipped_by_reason": {},
        }
        for s in payload["skipped_sectors"]:
            r = s["reason"]
            coverage["skipped_by_reason"][r] = coverage["skipped_by_reason"].get(r, 0) + 1
        return ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sectors": payload["sectors"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
            "signal_date": payload["signal_date"],
            "close_date": payload["close_date"],
            "prev_trading_date": payload["prev_trading_date"],
            "prev_snapshot": prev_snapshot,
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})

    @app.route("/api/actionable-leaders")
    def api_actionable_leaders():
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        payload, stale_cands = recommend.collect_actionable_leaders(
            summary, spot, db_path, "industry", now,
            ds.resolve_sector_constituents, ds.get_stock_daily)
        coverage = {
            "scanned": payload["sectors_scanned"],
            "total": payload["total"],
            "skipped": len(payload["skipped_sectors"]),
            "skipped_by_reason": {},
        }
        for s in payload["skipped_sectors"]:
            r = s["reason"]
            coverage["skipped_by_reason"][r] = coverage["skipped_by_reason"].get(r, 0) + 1
        return ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "total": payload["total"],
            "items": payload["items"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB"] = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB"])), exist_ok=True)
    store.init_db(app.config["DB"])
    register_routes(app)
    health = ds.validate_sector_map()
    app.config["SECTOR_MAP_HEALTH"] = health
    if not health.get("ok") or health.get("stale") or health.get("renamed"):
        logging.getLogger(__name__).warning("sector map health: %s", health)
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8000)
