# -*- coding: utf-8 -*-
"""交易回测报告路由 /api/report/trade-sim(读预生成的 JSON)。"""
import json
import os

from core import freshness
from pipeline import simulate
from web import config
from web import util


def register(app):
    @app.route("/api/report/trade-sim")
    def api_report_trade_sim():
        path = app.config.get("TRADE_SIM_REPORT", config.DEFAULT_TRADE_SIM_REPORT)
        if not os.path.exists(path):
            return util.err("NO_REPORT",
                            "交易回测报告未生成:请先运行 python simulate.py --out _analysis/trade_sim.json", 404)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return util.err("REPORT_ERROR", str(e), 500)
        data["freshness"] = freshness.assess((data.get("data_range") or {}).get("end"))
        data["evidence_status"] = (
            "invalid_legacy" if data.get("module_version") != simulate.MODULE_VERSION
            else "exploratory_unverified_inputs")
        return util.ok(data)
