"""Review archived candidates with current risk rules; never rewrite archived decisions."""
from core import daily_data as data, price_collection, report_cache, screening_engine, signal_replay
from core import store, strategies, strategy_risk, strategy_lifecycle


def apply(report, source_path, db):
    if report.get("frozen_version") == strategy_lifecycle.screening_identity()["version"]:
        report["risk_review"] = {"status": "current", "blocked": [], "unknown": [],
                                 "policy": strategy_risk.POLICY, "policy_version": strategy_risk.VERSION}
        return report
    codes = {row["code"][-6:] for rows in report.get("groups", {}).values() for row in rows}
    frames, failed = {}, set()
    # Only archived target-day inputs are eligible. No live/future quotes are used.
    ref = report_cache.read(source_path, project=lambda r: r.get("input_manifest"),
                            projection_key="risk-input-ref-v1")
    try:
        if ref:
            inputs = report_cache.read(ref["path"], ref["sha256"],
                project=lambda r: {c: r.get("prices", {}).get(c) for c in codes},
                projection_key=("risk-prices-v1", tuple(sorted(codes))))
            for code, item in inputs.items():
                try:
                    if item and item.get("status") == "success":
                        frames[code] = price_collection.checked_frame(item)
                except (OSError, ValueError, KeyError, TypeError):
                    failed.add(code)
        else:
            ids = report.get("snapshot_ids", {})
            if ids:
                snapshot = store.get_strategy_signal(db, next(iter(ids.values())))
                bundle_ref = snapshot["payload"]["replay_ref"]
                bundle = report_cache.read(bundle_ref["path"], bundle_ref["sha256"])
                ref = bundle["inputs"]
                encoded = report_cache.read(ref["path"], ref["sha256"],
                    project=lambda r: {c: r["technical"][c] for c in codes if c in r["technical"]},
                    projection_key=("risk-frames-v1", tuple(sorted(codes))))
                frames = signal_replay.restore_frames(encoded)
    except (OSError, ValueError, KeyError, TypeError):
        failed.update(codes)
    reviews, blocked, unknown = {}, [], []
    for code in codes:
        view, failure = screening_engine.day_view(frames.get(code), report["data_as_of"])
        risk = strategy_risk.assess(None if failure or code in failed else view, code)
        reviews[code] = risk
    for key, rows in report.get("groups", {}).items():
        kept = []
        for row in rows:
            risk = reviews[row["code"][-6:]]
            annotated = {**row, "risk": risk, "risk_messages": strategy_risk.describe(risk),
                         "score_basis": "archived_score_not_recomputed", "announcement_status": "unverified",
                         "entry_review_required": True, "actionable": False}
            if risk["blocked"]:
                target = unknown if risk["status"] == "unavailable" else blocked
                target.append({**annotated, "strategy": key})
            else:
                kept.append(annotated)
        report["groups"][key] = kept
    report["risk_review"] = {"status": "archived_candidates_reviewed", "blocked": blocked, "unknown": unknown,
        "policy": strategy_risk.POLICY, "policy_version": strategy_risk.VERSION,
        "basis": "current_risk_filter_on_original_candidates_no_pool_reselection",
        "original_version": report.get("version")}
    report["actionable"] = False
    return report
