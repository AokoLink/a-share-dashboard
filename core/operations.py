"""Coverage and recovery diagnostics derived from archived daily runs."""
from pathlib import Path
from collections import Counter
from core import daily_data, strategy_lifecycle
from core.stage1_data import _write_json


def publish(root, manifest):
    root = Path(root)
    old_path = root / "health.json"
    try:
        previous = daily_data.read_json(old_path) if old_path.exists() else {}
    except (OSError, ValueError):
        previous = {}
    codes = previous.get("codes", {})
    steps = manifest.get("steps", {})
    requested = set(steps.get("prices", {}).get("result", {}).get("entries", {}))
    for code, pair in steps.get("prices", {}).get("result", {}).get("entries", {}).items():
        history = codes.setdefault(code, {})
        for kind, item in pair.items():
            last = history.get(kind, {})
            artifact = item.get("artifact", {})
            good = item.get("status") == "success" and daily_data.intact(artifact)
            success_date = item.get("quality", {}).get("last") if good else None
            advances = good and success_date and success_date >= (last.get("last_success_date") or "")
            history[kind] = {"status": item.get("status"), "error": item.get("error"),
                "elapsed_seconds": item.get("elapsed_seconds"), "source": artifact.get("source"),
                "last_success_date": success_date if advances else last.get("last_success_date"),
                "last_success_at": artifact.get("collected_at") if advances else last.get("last_success_at"),
                "current": item.get("quality", {}).get("current", False),
                "collection_mode": item.get("collection_mode"), "request_attempts": len(item.get("requests", []))}
    for code, pair in codes.items():
        for item in pair.values():
            item["requested_this_run"] = code in requested
    failures = [{"step": key, "status": value["status"], "reason": value.get("result", {}).get("error")
                  or value.get("result", {}).get("reason"), "elapsed_seconds": value.get("elapsed_seconds")}
                for key,value in steps.items() if value["status"] in ("failed", "partial", "skipped")]
    result = {"day": manifest["day"], "run_id": manifest["run_id"], "source_mode": manifest["source_mode"],
              "generated_at": strategy_lifecycle.observation_clock().isoformat(), "codes": codes, "steps": failures,
              "price_errors": dict(Counter(item.get("error") or "missing_current_bar"
                  for code,pair in codes.items() if code in requested for item in pair.values()
                  if item.get("status") != "success" or not item.get("current"))),
              "retry_queue": steps.get("prices", {}).get("result", {}).get("retry_queue"),
              "recovery": {"resume": "python -m pipeline.run_daily --resume " + manifest["run_id"],
                  "resume_requires": "same_trade_day_code_budget_and_inputs", "changed_code_or_day": "start_a_new_run",
                  "no_candidates_is_not_source_failure": True}}
    _write_json(old_path, result)
    return result
