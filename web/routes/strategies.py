# -*- coding: utf-8 -*-
"""第二阶段独立策略的在线研究候选。"""
import uuid
from datetime import date
from pathlib import Path
from flask import request

from core import strategies, store, strategy_lifecycle
from core import report_cache
from core.stage1_data import load_calendar
from web import util, report_views, candidate_review


def candidate_dates(root, today):
    """Availability is metadata, not permission to substitute a different view."""
    dates = {}
    for view, folders in {"original": ("candidates",), "revised": (
            "candidates", "research_candidates/days", "risk_revision/days", "strategy_revision/days")}.items():
        days = set()
        for folder in folders:
            for path in (root / folder).glob("*.json"):
                try:
                    day = date.fromisoformat(path.stem).isoformat()
                except ValueError:
                    continue
                if day <= today:
                    days.add(day)
        dates[view] = sorted(days)
    return dates


def candidate_files(root, today, view="original", requested=None):
    """Original archives never depend on current rules or retrospective revisions."""
    files = []
    for path in (root / "candidates").glob("*.json"):
        day = date.fromisoformat(path.stem).isoformat()
        if day <= today and (not requested or requested == day):
            files.append((day, 1, path, None))
    if view == "original":
        return sorted(files, key=lambda item: item[0])
    for index in (root / "research_candidates" / "days").glob("*.json"):
        day = date.fromisoformat(index.stem).isoformat()
        if day > today or requested and requested != day:
            continue
        ref = report_cache.read(index)
        path = Path(ref["path"]).resolve()
        if path.parent != (root / "research_candidates" / "blobs").resolve():
            raise ValueError("invalid_research_candidate_path")
        files.append((day, 0, path, ref["sha256"]))
    for index in (root / "risk_revision" / "days").glob("*.json"):
        day = date.fromisoformat(index.stem).isoformat()
        if day > today or requested and requested != day:
            continue
        ref = report_cache.read(index)
        path = Path(ref["path"]).resolve()
        if path.parent != (root / "research_candidates" / "blobs").resolve():
            raise ValueError("invalid_revised_candidate_path")
        files.append((day, 2, path, ref["sha256"]))
    for index in (root / "strategy_revision" / "days").glob("*.json"):
        day = date.fromisoformat(index.stem).isoformat()
        if day > today or requested and requested != day:
            continue
        ref = report_cache.read(index)
        path = Path(ref["path"]).resolve()
        if path.parent != (root / "research_candidates" / "blobs").resolve():
            raise ValueError("invalid_strategy_revision_path")
        files.append((day, 3, path, ref["sha256"]))
    return sorted(files, key=lambda item: (item[0], item[1]))


def read_candidate(item):
    day, _, path, expected = item
    value = report_cache.read(path, expected, project=report_views.candidate_view,
                             projection_key="candidate-view-v1")
    if value.get("data_as_of") != day:
        raise ValueError("candidate_date_mismatch")
    return value


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
            matches = active[0]["identity"] == forward_validation.code_identity()
            result = {k: report.get(k) for k in fields} | {"read_only": True,
                      "registered_at": active[0]["registered_at"], "report_ref": ref,
                      "identity_matches": matches, "report_status": report.get("status")}
            if not matches:
                result.update(status="code_changed_requires_new_registration", formal=False,
                              validated_account_return=None, account_performance_status="stale_code_identity")
            return util.ok(result, stale=not matches)
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
            detail = request.args.get("detail", "0")
            if detail not in ("0", "1"):
                raise ValueError("invalid_detail")
            path = root / "runs" / str(uuid.UUID(run_id)) / "run.json" if run_id else root / "latest.json"
            if not path.exists():
                return util.ok({"status": "not_run", "steps": {}, "read_only": True})
            result = report_cache.read(path) if detail == "1" else report_cache.read(
                path, project=report_views.run_summary, projection_key="daily-summary-v1")
            if detail != "1":
                result["detail_url"] = "/api/daily-runs?detail=1" + ("&run_id=" + run_id if run_id else "")
            return util.ok(result)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return util.err("BAD_RUN_OR_REPORT", "运行编号无效或运行记录无法读取", 400)

    @app.route("/api/pipeline-health")
    def api_pipeline_health():
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        try:
            page, limit = int(request.args.get("page", 1)), int(request.args.get("limit", 30))
            if page < 1 or not 1 <= limit <= 100:
                raise ValueError()
            failed = request.args.get("failed") == "1"
            path = root / "health.json"
            project = lambda report: report_views.health_page(report, page, limit, failed)
            result = report_cache.read(path, project=project, projection_key=("health-v1", page, limit, failed)) if path.exists() else project({"status": "not_run", "codes": {}})
            return util.ok(result)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return util.err("BAD_HEALTH_REPORT", "监控记录或分页参数无效", 400)

    @app.route("/api/strategy-candidates")
    def api_strategy_candidates():
        now = util.now()
        empty = {"generated_at": now.isoformat(), "version": strategies.VERSION,
                 "actionable": False, "cards": strategies.CARDS,
                 "groups": {key: [] for key in strategies.HORIZONS},
                 "snapshot_ids": {}, "freeze_status": "not_run", "read_only": True}
        root = Path(app.config["DAILY_PIPELINE_ROOT"])
        history = request.args.get("history", "0")
        if history not in ("0", "1"):
            return util.err("BAD_PARAM", "历史查看参数无效", 400)
        view = request.args.get("view", "original")
        if view not in ("original", "revised"):
            return util.err("BAD_PARAM", "候选口径必须为 original 或 revised", 400)
        before_close = (now.hour, now.minute) < (15, 5)
        market_closed = False
        try:
            calendar = load_calendar(root / "calendar.json")
            today = now.date().isoformat()
            market_closed = bool(calendar and calendar["first"] <= today <= calendar["last"]
                                 and today not in calendar["dates"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        requested = request.args.get("date")
        try:
            if requested:
                requested = date.fromisoformat(requested).isoformat()
                if requested > now.date().isoformat():
                    raise ValueError()
                if requested < now.date().isoformat():
                    history = "1"
        except ValueError:
            return util.err("BAD_PARAM", "候选日期无效或晚于今天", 400)
        try:
            available_views = candidate_dates(root, now.date().isoformat())
            available = available_views[view]
            empty.update(available_dates=available, available_views=available_views,
                         requested_date=requested, view=view, origin=None)
            files = candidate_files(root, now.date().isoformat(), view, requested)
            paths = files
            if (before_close or market_closed) and history != "1":
                previous = None
                if files:
                    saved = read_candidate(files[-1])
                    previous = {
                        "data_as_of": saved.get("data_as_of"), "generated_at": saved.get("generated_at"),
                        "freeze_status": saved.get("freeze_status"),
                        "retrospective": saved.get("retrospective", False),
                        "counts": {key: len(rows) for key, rows in saved.get("groups", {}).items()},
                        "view": view,
                        "url": "/api/strategy-candidates?history=1&view=" + view + "&date=" + files[-1][0]}
                return util.ok({**empty, "status": "market_closed" if market_closed else "waiting_for_close",
                                "previous_report": previous})
            if not paths:
                revised_only = view == "original" and (requested in available_views["revised"] if requested else bool(available_views["revised"]))
                alternative_day = requested or (available_views["revised"][-1] if revised_only else None)
                return util.ok({**empty, "status": "missing_original" if revised_only else "missing_date" if requested else "not_run",
                    "alternative_view": {"view": "revised", "date": alternative_day,
                        "url": "/api/strategy-candidates?history=1&view=revised&date=" + alternative_day} if revised_only else None,
                    "diagnostics": {
                    "reason": "no_saved_candidates_for_requested_date" if requested else "run_daily_pipeline_first"}})
            result = read_candidate(paths[-1])
            origin = {0: "historical_recovery", 1: "original_daily", 2: "risk_revision", 3: "strategy_revision"}[paths[-1][1]]
            archived_snapshot_ids = dict(result.get("snapshot_ids", {}))
            if view == "revised":
                if origin == "original_daily":
                    result = candidate_review.apply(result, paths[-1][2], app.config["DB"])
                    origin = "original_risk_review"
                    result["reviewed_at"] = now.isoformat()
                    result["review_version"] = result.get("risk_review", {}).get("policy_version")
                result.update(snapshot_ids={}, forward_signal=False, actionable=False,
                              source_snapshot_ids=archived_snapshot_ids)
            else:
                result["risk_review"] = {"status": "not_applied_original_archive", "blocked": [], "unknown": []}
        except (OSError, ValueError, KeyError, TypeError):
            return util.err("NO_REPORT", "每日候选记录损坏，请查看每日任务结果", 500)
        statuses = store.get_strategy_statuses(app.config["DB"], result["frozen_version"], strategies.HORIZONS)
        shadow = request.args.get("shadow") == "1"
        retrospective = bool(result.get("retrospective"))
        blockers = dict(result.get("strategy_blockers", {}))
        for key in strategies.HORIZONS:
            if key not in result.get("groups", {}):
                blockers[key] = "not_evaluated_in_archive"
        result["cards"] = {**strategies.CARDS, **result.get("cards", {})}
        if result.get("market_state") is None:
            blockers.setdefault("rebound", "missing_dated_market_environment")
        result["strategy_blockers"] = blockers
        archive_view = history == "1" or result.get("data_as_of") != now.date().isoformat()
        preserved = archive_view or view == "revised"
        modes = {key: "historical_research" if retrospective or view == "revised" else "original_archive"
                 for key in strategies.HORIZONS} if preserved else strategy_lifecycle.run_modes(statuses, observe_paused=shadow)
        groups = {}
        for key, mode in modes.items():
            source = result.get("shadow_groups", {}) if mode == "shadow" else result["groups"]
            groups[key] = [{**item, "run_mode": mode} for item in source.get(key, [])] if mode != "disabled" else []
        historical = result.get("data_as_of") != now.date().isoformat()
        incomplete = result.get("diagnostics", {}).get("stale_source", False)
        stale = historical or incomplete
        result = {**result, "groups": groups, "strategy_statuses": statuses, "run_modes": modes,
                  "read_only": True, "data_freshness": "historical" if historical else "partial_data" if incomplete else "current",
                  "view": view, "view_context": "archive" if archive_view else "latest", "origin": origin,
                  "source_generated_at": result.get("generated_at"),
                  "source_version": result.get("version"), "source_frozen_version": result.get("frozen_version"),
                  "awaiting_current_close": before_close and not market_closed,
                  "available_dates": available, "available_views": available_views, "requested_date": requested}
        if history == "1" or stale:
            result["actionable"] = False
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
                snapshot_id = result.get("snapshot_ids", {}).get(key) if view == "original" else None
                item["snapshot_id"] = snapshot_id
                item["snapshot_url"] = "/api/strategy-snapshot/" + str(snapshot_id) if isinstance(snapshot_id, int) else None
                item["evidence_level"] = "retrospective_research" if retrospective else "research_candidate"
                card = result["cards"].get(key, {})
                item["earliest_execution"] = "历史复核／补算，不生成订单" if view == "revised" or retrospective else card.get("entry", "原始执行条件未留档")
                item["invalidation"] = "历史股票范围、ST变化、环境及同日板块证据未完整核实" if retrospective else card.get("risk", "原始风险条件未留档")
                item["existing_account_holding"] = None if preserved or retrospective or held is None else str(item["code"])[-6:] in held
        result.pop("shadow_groups", None)
        if all(mode == "disabled" for mode in modes.values()):
            result["status"] = "suspended"
        return util.ok(result, stale=stale)
