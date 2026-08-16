# -*- coding: utf-8 -*-
"""持仓诊断路由 /api/diagnose:多 horizon 前向分布(诚实输出,可判「信号不足」)。

数据依赖:先运行 `python -m core.forward` 生成 _analysis/forward_calib.json;
再读个股日线 pkl,用校准器对最新一根做诊断。离线只读,不 fetch 行情。
"""
from flask import request

from core import backtest as bt
from core import data_source as ds
from core import forward as fw
from web import config
from web import util


def register(app):
    @app.route("/api/diagnose")
    def api_diagnose():
        raw = (request.args.get("codes") or request.args.get("code") or "").strip()
        codes = [c for c in raw.split(",") if c.strip()]
        if not codes:
            return util.err("BAD_PARAM", "需提供 code 或 codes(逗号分隔 6 位代码)", 400)
        parsed = []
        for c in codes:
            c6 = util._parse_stock_code(c)
            if c6 is None:
                return util.err("BAD_PARAM", f"非法代码: {c}", 400)
            parsed.append(c6)

        try:
            cals, benchmarks, meta = fw.load_model(config.FORWARD_CALIB)
        except (FileNotFoundError, OSError, ValueError) as e:
            return util.err("NO_CALIB",
                            "持仓诊断校准器未生成:请先运行 `python -m core.forward` "
                            f"生成 {config.FORWARD_CALIB} ({e})", 503)

        results = []
        errors = []
        for c6 in parsed:
            try:
                daily = bt.load_daily(config.DAILY_DIR, c6)
            except FileNotFoundError:
                errors.append({"code": c6, "error": "no_pkl"})
                continue
            frame = fw.prepare_frame(daily, c6)
            if len(frame) < 61:
                errors.append({"code": c6, "error": "history_too_short"})
                continue
            dg = fw.diagnose_at(frame, len(frame) - 1, cals, benchmarks)
            if dg is None:
                errors.append({"code": c6, "error": "no_signal"})
                continue
            name = None
            try:
                name = ds.get_stock_quote(c6)[0]["name"]
            except Exception:
                name = None
            dg["name"] = name
            results.append(dg)

        return util.ok({
            "as_of": meta.get("data_range", {}).get("end"),
            "horizons": meta.get("horizons", list(fw.HORIZONS)),
            "cost": meta.get("cost", fw.COST),
            "min_signal_band": meta.get("min_signal_band", fw.MIN_SIGNAL_BAND),
            "benchmarks": {str(h): benchmarks[h] for h in benchmarks},
            "results": results,
            "errors": errors,
        })
