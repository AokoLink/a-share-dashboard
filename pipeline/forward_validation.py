"""Register and report independently frozen forward evidence; never backfill signals."""
import argparse
from collections import Counter
from pathlib import Path
import json
from datetime import time

import pandas as pd

from core import forward_validation as fv, daily_data as data, research_evaluation as ev
from core import strategies, strategy_lifecycle as life
from core.stage1_data import _write_json, sha256
from pipeline import account_report


def summarize(rows, key, horizon):
    result = ev.summarize(rows, key, horizon)
    selected = [r for r in rows if r["strategies"][key]["candidates"]]
    incomplete = [r for r in selected if any(r["strategies"][key]["fixed_weight_net_arms"][arm] is None
                                            for arm in ("full", "market_equal"))]
    result["incomplete_signal_days"] = len(incomplete)
    result["summary_status"] = "pending_or_missing_labels" if incomplete else "descriptive_complete_proxy"
    if incomplete:
        result["available_rows_descriptive_excess"] = result["mean_fixed_net_excess"]
        result["mean_fixed_net_excess"] = None
        result["mean_conditional_net_excess"] = None
        result["event_cluster_uncertainty"] = {"status": "blocked_incomplete_event_labels", "interval95": None}
    result["account_return_status"] = "see_separate_verified_account"
    return result


def run(db, root=data.ROOT, *, as_of=None):
    clock = life.observation_clock(as_of)
    active = fv.load(root)
    if not active:
        return {"status": "not_registered", "formal": False}
    policy, ref = active
    inputs, manifest = account_report.load_inputs(db, root, clock)
    if policy.get("calendar_ref"):
        from core.stage1_data import load_calendar
        inputs["calendar"] = load_calendar(policy["calendar_ref"]["path"])["dates"]
    accepted, rejected, seen = [], [], set()
    for snap in sorted(inputs["snapshots"], key=lambda s: (s["frozen_at"], s["id"])):
        reason = fv.eligibility(snap, root, policy, ref)
        key = (snap["signal_date"], snap["strategy"])
        if key in seen:
            reason = reason or "later_revision_not_independent"
        else:
            seen.add(key)  # first real snapshot wins even when it is ineligible
        if reason:
            rejected.append({"snapshot_id": snap["id"], "reason": reason})
        else:
            accepted.append(snap)
    unchanged = policy["identity"] == fv.code_identity()
    execution = {code: pd.DataFrame([{"date": d, **bar} for d, bar in bars.items()])
                 for code, bars in inputs["bars"].items()}
    daily = {}
    sensitivity = {}
    paused = []
    if unchanged:
        price_cutoff = clock.date().isoformat()
        if clock.time().replace(tzinfo=None) < time(9, 30):
            price_cutoff = max((d for d in inputs["calendar"] if d < price_cutoff), default="0001-01-01")
        for snap in accepted:
            decision = snap["payload"]["screening_decision"]
            sessions = inputs["calendar"]
            idx = sessions.index(snap["signal_date"]) if snap["signal_date"] in sessions else -1
            entry = sessions[idx + 1] if idx >= 0 and idx + 1 < len(sessions) else None
            if entry and life.status_at_entry(snap, entry, inputs["status_events"], clock) in ("paused", "retired"):
                paused.append({"snapshot_id": snap["id"], "reason": "paused_or_retired_before_entry",
                               "candidates_retained": len(snap["payload"].get("candidates", []))})
                continue
            result = ev.evaluate_selection(decision, execution, inputs["calendar"], as_of=price_cutoff,
                seed=policy["seed"], random_draws=policy["random_draws"])
            key, day = snap["strategy"], snap["signal_date"]
            row = daily.setdefault(day, {"date": day, "market_state": decision.get("market_state"), "strategies": {}})
            row["strategies"][key] = result[key]
            sensitivity[str(snap["id"])] = {str(scale): ev.evaluate_selection(decision, execution,
                inputs["calendar"], as_of=price_cutoff, seed=policy["seed"],
                random_draws=policy["random_draws"], cost_scale=scale)[key]["fixed_weight_net_arms"]
                for scale in policy["cost"]["sensitivity"]}
    summaries, monthly = {}, {}
    for key, horizon in strategies.HORIZONS.items():
        rows = [r for r in sorted(daily.values(), key=lambda r: r["date"]) if key in r["strategies"]]
        summaries[key] = summarize(rows, key, horizon)
        months = sorted({r["date"][:7] for r in rows})
        monthly[key] = {month: {"status": "closed_month" if month < clock.date().isoformat()[:7] else "provisional",
            "summary": summarize([r for r in rows if r["date"].startswith(month)], key, horizon)} for month in months}
    account_inputs = {**inputs, "snapshots": accepted}
    account = account_report.build(account_inputs, as_of=clock, settings=policy["account_config"],
                                   source="registered_forward_simulation") if unchanged else None
    account_ready = bool(account and account["execution_status_counts"].get("buy_fill")
        and account["metrics"]["evidence_status"] == "verified_input_simulation_not_actual_fills"
        and account["metrics"]["total_return"] is not None)
    report = {"version": fv.VERSION, "as_of": clock.isoformat(), "protocol_ref": ref,
        "status": "code_changed_requires_new_registration" if not unchanged else
                  "awaiting_prospective_signals" if not accepted else "forward_observation",
        "formal": False, "accepted_snapshots": len(accepted), "rejected": rejected,
        "validation_start": policy["validation_start"], "development_label_end": policy["development_label_end"],
        "rejection_counts": dict(Counter(r["reason"] for r in rejected)),
        "paused_before_entry": paused,
        "signal_days": len(daily), "daily": list(daily.values()), "summary": summaries,
        "monthly": monthly, "cost_sensitivity": sensitivity, "verified_account": account,
        "validated_account_return": account["metrics"]["total_return"] if account_ready else None,
        "account_performance_status": "verified_input_simulation_not_broker_fills" if account_ready else "awaiting_verified_execution_evidence",
        "input_manifest": manifest, "negative_results_retained": True,
        "limits": ["price_open_proxy_not_actual_execution", "sector_index_and_historical_membership_gates_remain",
                   "no_automatic_promotion", "monthly_descriptive_not_parameter_tuning",
                   "post_registration_live_signals_only; historical_date_split_not_independence"]}
    path = Path(root) / "forward_validation" / "reports" / (fv.ledger.digest(report) + ".json")
    if not path.exists():
        _write_json(path, report)
    report_ref = {"path": str(path.resolve()), "sha256": sha256(path), "status": report["status"]}
    _write_json(Path(root) / "forward_validation" / "latest.json", report_ref)
    return {**report, "report_ref": report_ref}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--db", default=str(data.ROOT.parents[1] / "data" / "market.db"))
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--as-of")
    args = parser.parse_args()
    if args.register:
        policy, ref = fv.register(args.root)
        print(json.dumps({"registered_at": policy["registered_at"], "protocol_ref": ref}))
    result = run(args.db, args.root, as_of=args.as_of)
    print(json.dumps({k: result.get(k) for k in ("status", "accepted_snapshots", "signal_days", "report_ref")}))


if __name__ == "__main__":
    main()
