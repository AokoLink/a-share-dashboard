# -*- coding: utf-8 -*-
"""自选组合的描述性暴露与相关性，输入为账户净值权重。"""
from pathlib import Path
import math
from flask import request

from core import data_source as ds
from core import freshness, portfolio_insights, sector_relations, sector_service, report_cache
from core.daily_data import read_json, intact
from web import util


def register(app):
    @app.route("/api/account-ledger")
    def api_account_ledger():
        root = Path(app.config["DAILY_PIPELINE_ROOT"]) / "accounts"
        index = root / "latest.json"
        if not index.exists():
            return util.ok({"status": "not_run", "daily": [], "metrics": {}, "formal": False})
        try:
            ref = report_cache.read(index)
            path = Path(ref["path"])
            if path.resolve().parent != (root / "blobs").resolve():
                raise ValueError("invalid_account_report_reference")
            report = report_cache.read(path, ref["sha256"])
            # The page gets a compact view; orders/events and full controls remain in the archive.
            return util.ok({k: v for k, v in report.items() if k not in ("events", "orders", "lots", "receivables", "comparisons")} | {
                "daily": report["daily"][-120:], "total_sessions": len(report["daily"]), "report_ref": ref,
                "comparisons": {k: {f: v[f] for f in ("status", "total_net_excess", "basis") if f in v}
                                for k,v in report.get("comparisons", {}).items()}})
        except (OSError, ValueError, KeyError):
            return util.err("NO_ACCOUNT_REPORT", "账户账本记录损坏，请查看后台账户任务", 500)

    @app.route("/api/portfolio-insights", methods=["POST"])
    def api_portfolio_insights():
        body = request.get_json(silent=True) or {}
        raw = body.get("holdings")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 20 or any(not isinstance(item, dict) for item in raw):
            return util.err("BAD_PARAM", "holdings 需为1至20只股票列表", 400)
        codes = [ds.normalize_code(item.get("code", "")) for item in raw]
        if any(len(code) != 6 or not code.isdigit() for code in codes):
            return util.err("BAD_PARAM", "代码需为6位股票代码", 400)
        explicit = ["weight" in item for item in raw]
        if any(explicit) and not all(explicit):
            return util.err("BAD_PARAM", "请为全部持仓填写权重，或全部留空以按等权分析", 400)
        try:
            unique_codes = list(dict.fromkeys(codes))
            weights = {code: 0.0 for code in unique_codes}
            strategy_tags = {code: set() for code in unique_codes}
            for code, item in zip(codes, raw):
                if all(explicit):
                    weight = float(item["weight"])
                    if weight < 0:
                        raise ValueError("weight must be nonnegative")
                    weights[code] += weight
                if item.get("strategy"):
                    strategy_tags[code].add(str(item["strategy"]))
            if not all(explicit):
                weights = {code: 1 / len(unique_codes) for code in unique_codes}
            if "cash_weight" in body:
                cash = float(body["cash_weight"])
                if not all(explicit) or not math.isfinite(cash) or cash < 0 or cash > 1 or abs(sum(weights.values()) + cash - 1) > 1e-6:
                    raise ValueError("实际股票权重与现金权重之和必须为1")
            holdings = [{"code": code, "weight": weights[code]} for code in unique_codes]
            daily, sectors, stale_any = {}, {}, False
            for code in unique_codes:
                frame, stale = ds.get_stock_daily(code)
                daily[code] = frame
                stale_any = stale_any or bool(stale)
            day = util.today()
            snapshot = sector_relations.load(app.config["DAILY_PIPELINE_ROOT"], day)
            sector_exposure = sector_service.exposure(holdings, snapshot, day)
            sectors = {code: [r["id"] for r in item["relations"]]
                       for code, item in sector_exposure["per_code"].items()}
            result = portfolio_insights.analyze(holdings, daily, sectors)
        except (ValueError, TypeError) as exc:
            return util.err("BAD_PARAM", str(exc), 400)
        except ds.DataSourceError as exc:
            return util.err("SOURCE_FAIL", str(exc), 500)
        result["weight_assumption"] = "provided_by_user" if all(explicit) else "equal_weight_watchlist"
        result["version"] = portfolio_insights.VERSION
        result["generated_at"] = util.now().isoformat()
        result["deduplicated_entries"] = len(raw) - len(unique_codes)
        result["cross_strategy_codes"] = [code for code in unique_codes if len(strategy_tags[code]) > 1]
        result["sector_basis"] = "observed_day_only_not_historical"
        result["sector_exposure"] = sector_exposure
        result["freshness"] = freshness.assess(result["data_as_of"], now=util.now())
        result["status"] = "exploratory"
        return util.ok(result, stale=stale_any)
