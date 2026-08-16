# -*- coding: utf-8 -*-
"""可介入龙头路由 /api/actionable-leaders。"""
from core import data_source as ds
from core import recommend
from web import util


def register(app):
    db_path = app.config["DB"]

    @app.route("/api/actionable-leaders")
    def api_actionable_leaders():
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
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
        return util.ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "total": payload["total"],
            "items": payload["items"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})
