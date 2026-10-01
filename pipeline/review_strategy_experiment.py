"""独立解释已登记实验的证据边界；保留原实验结果和负结果。"""
import argparse
import json
from pathlib import Path

from core.stage1_data import _write_json, sha256
from core import research_evaluation as ev, strategy_lifecycle as life


def review(report_path):
    report_path = Path(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    base = report["daily"]["base"]
    features = [v for r in base for v in r["strategies"]["breakout"]["sector_evidence"].values()]
    verified = sum(v["status"] == "ok" for v in features)
    contrasts = {}
    for key, contrast in report["contrasts"].items():
        status = "descriptive_examined_history" if key != "breakout_no_sector" or verified else "blocked_feature_coverage"
        contrasts[key] = {"status": status, "paired_mean": contrast["paired_mean"] if status != "blocked_feature_coverage" else None,
            "raw_paired_mean": contrast["paired_mean"], "missing_pairs": contrast["missing_pairs"],
            "candidate_count_changes": sum(p["base_candidates"] != p["variant_candidates"] for p in contrast["pairs"])}
    rows = {}
    for key, summary in report["summary"]["base"].items():
        rows[key] = {"signal_days": summary["signal_days"], "event_clusters": summary["event_clusters"],
            "fixed_net_excess_pp": summary["mean_fixed_net_excess"] * 100 if summary["mean_fixed_net_excess"] is not None else None,
            "conditional_net_excess_pp": summary["mean_conditional_net_excess"] * 100 if summary["mean_conditional_net_excess"] is not None else None,
            "event_cluster_uncertainty": summary["event_cluster_uncertainty"],
            "execution_status_counts": summary["execution_status_counts"], "style_exposure": summary["style_exposure"],
            "cost_sensitivity": summary["cost_sensitivity"]}
    result = {"generated_at": life.observation_clock().isoformat(), "report_path": str(report_path.resolve()),
        "report_sha256": sha256(report_path), "protocol": report["protocol"], "status": "no_upgrade_evidence",
        "base_results": rows, "contrasts": contrasts,
        "sector_factor_coverage": {"observations": len(features), "verified": verified,
                                   "coverage": verified / len(features) if features else None},
        "date_split_validation_rows": {k:len(v["validation"]) for k,v in report["validation_split"].items()},
        "independent_validation_rows": {k: 0 for k in report["validation_split"]},
        "independence_status": "historical_date_split_is_not_registered_live_forward_evidence",
        "negative_results_retained": True, "account_NAV": None,
        "account_NAV_status": "requires_same_protocol_verified_forward_account", "limits": report["limits"],
        "decision": "continue_exploratory; no_parameter_optimization_or_formal_upgrade"}
    _write_json(report_path.parent / "review.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    result = review(args.report)
    print(json.dumps({k:result[k] for k in ("status", "base_results", "sector_factor_coverage", "independent_validation_rows")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
