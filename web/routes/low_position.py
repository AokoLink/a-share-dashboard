# -*- coding: utf-8 -*-
"""低位透视路由 /api/low-position(方向中性;把 position 因子背后的低位股显性化)。

定位:透视工具,非策略。Phase 0 预注册检验已判负(低位反转无选股 alpha),
详见 core/recommend.collect_low_position 文档串。
"""
from core import data_source as ds
from core import environment as env
from core import recommend
from web import config
from web import util


def register(app):
    @app.route("/api/low-position")
    def api_low_position():
        try:
            spot, spot_stale = ds.get_market_spot()
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
        regime = env.load_cached_regime(config.REGIME_CACHE)
        regime_block = None
        if regime:
            regime_block = {"as_of": regime.get("as_of"), "label": regime.get("label"),
                            "advice": regime.get("advice"), "swing": regime.get("swing")}
        payload, stale_cands = recommend.collect_low_position(
            spot, ds.get_stock_daily, now, regime_block, resolve_sectors_fn=ds.resolve_code_sectors)
        payload["generated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return util.ok(payload, stale=spot_stale or stale_cands)
