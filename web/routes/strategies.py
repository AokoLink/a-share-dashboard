# -*- coding: utf-8 -*-
"""第二阶段独立策略的在线研究候选。"""
import uuid
from datetime import date
from pathlib import Path
from flask import request

from core import strategies, store, strategy_lifecycle
from core.daily_data import read_json
from core import report_cache
from web import util


def register(app):
    @app.route("/api/forward-validation")
    def api_forward_validation():
        root = Path(app.config["DAILY_PIPELINE_ROOT"]) / "forward_validation"
        index = root / "latest.json"
        if not index.exists():
            return util.ok({"status": "not_registered", "formal": False, "read_only": True})
        try:
            ref = report_cache.read(index)
            path = Path(ref["path"]).resolve()
            if path.parent != (root / "reports").resolve():
                raise ValueError("invalid_forward_report_path")
            report = report_cache.read(path, ref["sha256"])
            from core import forward_validation
            active = forward_validation.load(root.parent)
            if not active or report["protocol_ref"] != active[1]:
                raise ValueError("forward_report_does_not_match_active_protocol")
            fields = ("status", "as_of", "formal", "accepted_snapshots", "signal_days", "validation_start",
                      "development_label_end", "rejection_counts", "summary", "validated_account_return",
                      "account_performance_status", "negative_results_retained", "limits")
            return util.ok({k: report.get(k) for k in fields} | {"read_only": True,
                            "registered_at": active[0]["registered_at"], "report_ref": ref})
        except (OSError, ValueError, KeyError):
            return util.err("INVALID_FORWARD_REPORT", "独立前向报告或登记证据损坏，请检查后台验证任务", 500)

    @app.route("/api/strategy-monitor")
    def api_strategy_monitor():
        day = request.args.get("date")
        try:
            if day:
                date.fromisoformat(day)
            limit = int(request.args.get("limit", 30))
            before = int(request.args["cursor"]) if request.args.get("cursor") else None
            if not 1 <= limit <= 100 or before is not None and before <= 0:
                raise ValueError()
        except ValueError:
            return util.err("BAD_PARAM", "日期、游标或分页大小无效", 400)
        strategy, version = request.args.get("strategy"), request.args.get("version")
        page = store.monitor_page(app.config["DB"], day=day, strategy=strategy, version=version, before=before, limit=limit)
        snapshots, observations, events = page["snapshots"], page["observations"], page["status_events"]
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        summary, summary_status = {}, "not_run"
        try:
            index = root / "monitor" / "latest.json"
            if index.exists():
                ref = report_cache.read(index)
                if Path(ref["path"]).resolve().is_relative_to(root.resolve()):
                    summary = report_cache.read(ref["path"], ref["sha256"])
                    summary_status = "current" if summary["watermark"] == store.monitor_watermark(app.config["DB"]) else "stale"
        except (OSError, ValueError, KeyError):
            summary_status = "invalid"
        reviews = [r for r in summary.get("versions", []) if (not strategy or r["strategy"] == strategy)
                   and (not version or r["version"] == version)]
        return util.ok({"status": "exploratory", "versions": reviews,
                        "summary_status": summary_status, "next_cursor": page["next_cursor"], "limit": limit,
                        "snapshots": [{"id": row["id"], "signal_date": row["signal_date"],
                                       "strategy": row["strategy"], "version": row["version"],
                                       "frozen_at": row["frozen_at"], "sha256": row["content_sha256"],
                                       "candidates": len(row["payload"].get("candidates", []))}
                                      for row in snapshots],
                        "observations": [{"snapshot_id": row["snapshot_id"],
                                          "observed_at": row["observed_at"],
                                          "payload": row["payload"]} for row in observations],
                        "status_events": events})

    @app.route("/api/strategy-snapshot/<int:snapshot_id>")
    def api_strategy_snapshot(snapshot_id):
        row = store.get_strategy_signal(app.config["DB"], snapshot_id)
        return util.ok(row) if row else util.err("NOT_FOUND", "snapshot not found", 404)

    @app.route("/api/daily-runs")
    def api_daily_runs():
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        run_id = request.args.get("run_id")
        try:
            path = root / "runs" / str(uuid.UUID(run_id)) / "run.json" if run_id else root / "latest.json"
            return util.ok(read_json(path)) if path.exists() else util.ok({"status": "not_run", "steps": {}})
        except (OSError, ValueError):
            return util.err("BAD_RUN_OR_REPORT", "运行编号无效或运行记录无法读取", 400)

    @app.route("/api/pipeline-health")
    def api_pipeline_health():
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        try:
            result = report_cache.read(root / "health.json") if (root / "health.json").exists() else {"status": "not_run", "codes": {}}
            page, limit = int(request.args.get("page", 1)), int(request.args.get("limit", 30))
            if page < 1 or not 1 <= limit <= 100:
                raise ValueError()
            codes = sorted(result.pop("codes").items())
            if request.args.get("failed") == "1":
                codes = [(c, pair) for c,pair in codes if any(r["status"] != "success" or not r["current"] for r in pair.values())]
            return util.ok({**result, "codes": dict(codes[(page - 1)*limit:page*limit]), "total_codes": len(codes), "page": page})
        except (OSError, ValueError, KeyError):
            return util.err("BAD_HEALTH_REPORT", "监控记录或分页参数无效", 400)

    @app.route("/api/strategy-candidates")
    def api_strategy_candidates():
        now = util.now()
        empty = {"generated_at": now.isoformat(), "version": strategies.VERSION,
                 "actionable": False, "cards": strategies.CARDS,
                 "groups": {key: [] for key in strategies.HORIZONS},
                 "snapshot_ids": {}, "freeze_status": "not_run"}
        if (now.hour, now.minute) < (15, 5):
            return util.ok({**empty, "status": "waiting_for_close"})
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        try:
            paths = sorted((root / "candidates").glob("*.json"))
            paths = [path for path in paths if path.stem <= now.date().isoformat()]
            if not paths:
                return util.ok({**empty, "status": "not_run", "diagnostics": {
                    "reason": "run_daily_pipeline_first"}})
            result = report_cache.read(paths[-1])
        except (OSError, ValueError):
            return util.err("NO_REPORT", "每日候选记录损坏，请查看每日任务结果", 500)
        statuses = store.get_strategy_statuses(app.config["DB"], result["frozen_version"], strategies.HORIZONS)
        shadow = request.args.get("shadow") == "1"
        modes = strategy_lifecycle.run_modes(statuses, observe_paused=shadow)
        groups = {}
        for key, mode in modes.items():
            source = result.get("shadow_groups", {}) if mode == "shadow" else result["groups"]
            groups[key] = [{**item, "run_mode": mode} for item in source.get(key, [])] if mode != "disabled" else []
        historical = result.get("data_as_of") != now.date().isoformat()
        incomplete = result.get("diagnostics", {}).get("stale_source", False)
        stale = historical or incomplete
        result = {**result, "groups": groups, "strategy_statuses": statuses, "run_modes": modes,
                  "read_only": True, "data_freshness": "historical" if historical else "partial_data" if incomplete else "current"}
        # Account holdings come from the saved ledger, never a new quote request.
        held = None
        try:
            ref = report_cache.read(root / "accounts" / "latest.json")
            if Path(ref["path"]).resolve().parent == (root / "accounts" / "blobs").resolve():
                account = report_cache.read(ref["path"], ref["sha256"])
                held = {p["code"] for row in account.get("daily", [])[-1:] for p in row["positions"]}
        except (OSError, KeyError, ValueError):
            pass
        for key, items in groups.items():
            for item in items:
                item["evidence_level"] = "research_candidate"
                item["earliest_execution"] = strategies.CARDS[key]["entry"]
                item["invalidation"] = strategies.CARDS[key]["risk"] + "；数据过期、资格不符或策略暂停时取消新入场"
                item["existing_account_holding"] = None if held is None else str(item["code"])[-6:] in held
        result.pop("shadow_groups", None)
        if all(mode == "disabled" for mode in modes.values()):
            result["status"] = "suspended"
        return util.ok(result, stale=stale)
