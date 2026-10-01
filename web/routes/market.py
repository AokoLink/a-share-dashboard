# -*- coding: utf-8 -*-
"""大盘总览路由 /api/market。"""
from core import analysis as an
from core import data_source as ds
from core import store
from web import util


def register(app):
    db_path = app.config["DB"]

    @app.route("/api/market")
    def api_market():
        try:
            spot, spot_stale = ds.get_market_spot()
            indices, idx_stale = ds.get_index_realtime()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
        today = util.today()
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
        return util.ok({"indices": indices, "breadth": breadth,
                        "volume_vs_yesterday": {"pct": volume_pct}},
                       stale=spot_stale or idx_stale or bool(getattr(exclude, "stale", False)))
