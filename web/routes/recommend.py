# -*- coding: utf-8 -*-
"""股票推荐路由 /api/recommend(板块→个股双层 + 上一期跳空对比)。"""
from flask import request

from core import data_source as ds
from core import environment as env
from core import freshness
from core import recommend
from core import store
from web import config
from web import util


def register(app):
    db_path = app.config["DB"]

    @app.route("/api/recommend")
    def api_recommend():
        try:
            top_sectors = int(request.args.get("top_sectors", "3"))
            per_sector = int(request.args.get("per_sector", "5"))
        except ValueError:
            return util.err("BAD_PARAM", "top_sectors/per_sector 必须为整数", 400)
        top_sectors = max(1, min(5, top_sectors))
        per_sector = max(1, min(10, per_sector))
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
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
        signal_state = freshness.assess(payload.get("close_date") or payload.get("signal_date"), now=now)
        historical = (signal_state["status"] != "current" or stale1 or stale2 or stale_cands)
        return util.ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "historical" if historical else "current",
            "signal_freshness": signal_state,
            "sectors": payload["sectors"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
            "signal_date": payload["signal_date"],
            "close_date": payload["close_date"],
            "prev_trading_date": payload["prev_trading_date"],
            "prev_snapshot": prev_snapshot,
            "regime": freshness.mark_regime(env.load_cached_regime(config.REGIME_CACHE), now=now),
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})
