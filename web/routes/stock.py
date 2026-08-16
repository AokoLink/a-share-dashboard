# -*- coding: utf-8 -*-
"""个股体检路由 /api/stock。"""
from flask import request

from core import analysis as an
from core import data_source as ds
from core import environment as env
from core import recommend
from core import store
from web import config
from web import util


def register(app):
    @app.route("/api/stock")
    def api_stock():
        raw = request.args.get("code")
        code6 = util._parse_stock_code(raw)
        if code6 is None:
            return util.err("BAD_PARAM", "code 必须为 6 位股票代码", 400)
        try:
            quote, stale1 = ds.get_stock_quote(code6)
            daily, stale2 = ds.get_stock_daily(code6)
            minute, stale3 = ds.get_stock_minute(code6)
        except ds.DataSourceError as e:
            return util.err("SOURCE_FAIL", str(e), 500)
        now = util.now()
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
                    need = set(names)
                    summary_f = summary[summary["name"].astype(str).isin(need)]
                    scored_sectors = recommend.score_all_sectors(
                        summary_f, app.config["DB"], "industry", store,
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
        indicators = an.compute_technical_indicators(daily, quote, now)
        regime = env.load_cached_regime(config.REGIME_CACHE)
        regime_block = None
        if regime:
            swing = regime.get("swing") or {}
            regime_block = {"label": regime.get("label"),
                            "action": swing.get("action"),
                            "message": swing.get("message")}
        hold = an.build_hold_advice(regime_block, scores["risk"], scores["pos60"])
        kline = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                  "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                 for x in daily.tail(250).to_dict("records")]
        intraday = [{"time": str(x["time"]), "price": util._num(x["price"]),
                     "avg": util._num(x["avg"]), "volume": util._num(x["volume"])}
                    for x in minute.to_dict("records")]
        return util.ok({"code": ds.with_prefix(code6), "name": quote["name"], "quote": quote,
                        "scores": scores, "position": scores["position"],
                        "composite": composite, "verdict": verdict, "tier": tier,
                        "sector_resolved": sector_resolved, "kline": kline,
                        "intraday": intraday, "indicators": indicators, "hold": hold},
                       stale=stale1 or stale2 or stale3)
