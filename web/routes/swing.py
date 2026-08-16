# -*- coding: utf-8 -*-
"""波段候选路由 /api/swing-candidates(方向中性 swing pool + regime 启发式指引)。"""
from core import data_source as ds
from core import environment as env
from core import recommend
from web import config
from web import util


def register(app):
    @app.route("/api/swing-candidates")
    def api_swing_candidates():
        try:
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
        regime = env.load_cached_regime(config.REGIME_CACHE)
        regime_block = None
        if regime:
            regime_block = {"as_of": regime.get("as_of"), "label": regime.get("label"),
                            "swing": regime.get("swing")}
        payload, stale_cands = recommend.collect_swing_candidates(
            spot, ds.get_stock_daily, now, regime_block)
        payload["generated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return util.ok(payload, stale=spot_stale or stale_cands)
