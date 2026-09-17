# -*- coding: utf-8 -*-
"""可介入龙头路由 /api/actionable-leaders。"""
from core import data_source as ds
from core import recommend
from core import store
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
        # 快照日志:每个信号日一行(同日重建覆盖),供事后复盘「哪天推荐了什么」。
        # close_date 为 None 恰好意味着当天没有一只有效 → 那是数据失败不是信号,
        # 落库会用空行遮蔽当天真实快照,故不写。失败不拖垮接口(照抄 recommend 路由)。
        try:
            if payload["close_date"] is not None:
                store.upsert_actionable_snapshot(
                    db_path, payload["signal_date"], now.strftime("%Y-%m-%d %H:%M:%S"),
                    payload["close_date"], payload["total"], payload["items"])
        except Exception:
            pass
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
            "signal_date": payload["signal_date"],
            "close_date": payload["close_date"],
            "total": payload["total"],
            "items": payload["items"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})
