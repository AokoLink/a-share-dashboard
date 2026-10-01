# -*- coding: utf-8 -*-
"""主题动量 vol-target 策略路由 /api/theme-vol(读预生成回测 JSON,输出当前仓位信号)。

策略口径(见 pipeline/theme_vol_strategy.py):每月末按动量选 top2 主题,每主题仓位
= clip(长250/短20 波动率, floor, 1) 的等权份,波动飙升自动降仓、余持现金。回测 2022-01→今,
因果无未来函数。信号为**月度重平衡**(月末收盘决策、次日 open 执行),非每日实时。

本路由只读离线回测产物 _analysis/theme_vol_strategy.json(同 report.py 读 trade_sim.json 模式),
数据更新须重跑 `python pipeline/theme_vol_strategy.py`。
"""
import json
import os

from core import freshness
from web import config
from web import util


def register(app):
    @app.route("/api/theme-vol")
    def api_theme_vol():
        path = app.config.get("THEME_VOL_REPORT", config.THEME_VOL_REPORT)
        if not os.path.exists(path):
            return util.err("NO_REPORT",
                            "主题策略回测未生成:请先运行 python pipeline/theme_vol_strategy.py", 404)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return util.err("REPORT_ERROR", str(e), 500)

        themes = data.get("themes") or {}
        latest = data.get("latest") or {}
        weights = latest.get("weights") or {}

        positions = []
        for t, wt in weights.items():
            theme = themes.get(t) or {}
            positions.append({
                "theme": t,
                "weight": wt,
                "stocks": [{"code": c, "name": n}
                           for c, n in zip(theme.get("core", []), theme.get("names", []))],
            })
        positions.sort(key=lambda x: -(x["weight"] or 0.0))

        total_pos = latest.get("total_pos")
        cash = None if total_pos is None else round(1.0 - float(total_pos), 4)
        state = freshness.assess(data.get("data_as_of") or latest.get("date"))
        current = (state["status"] == "current"
                   and data.get("accounting_version") == "shares-cash-v1"
                   and data.get("price_basis") == "raw_verified"
                   and data.get("baseline_status") == "formal")

        return util.ok({
            "generated_at": data.get("generated_at"),
            "signal_date": latest.get("date"),
            "freshness": state,
            "status": "current" if current else "historical",
            "data_as_of": data.get("data_as_of"),
            "accounting_version": data.get("accounting_version", "legacy_unverified"),
            "price_basis": data.get("price_basis", "unknown"),
            "baseline_status": data.get("baseline_status", "exploratory"),
            "input_readiness": data.get("input_readiness"),
            "total_pos": total_pos if current else None,
            "cash": cash if current else None,
            "positions": positions if current else [],
            "historical_positions": positions if not current else [],
            "vol_target": data.get("vol_target"),
            "full_baseline": data.get("full_baseline"),
            "params": {
                "topn": data.get("topn"),
                "kpast": data.get("kpast"),
                "long_vol": data.get("long_vol"),
                "short_vol": data.get("short_vol"),
                "floor": data.get("floor"),
                "cost_side": data.get("cost_side"),
            },
        })
