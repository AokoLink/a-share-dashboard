"""Rebuild an environment archive from verified saved market inputs; no signal writes."""
import argparse
import uuid
from pathlib import Path

from core import daily_data as data, strategy_lifecycle, version_manifest
from core.daily_runner import DailyRunner, RunLock
from core.stage1_data import _write_json, sha256


def run(root, run_id, *, now=None):
    root = Path(root).resolve()
    original_path = root / "runs" / str(uuid.UUID(run_id)) / "run.json"
    original = data.read_json(original_path)
    clock = strategy_lifecycle.observation_clock(now)
    day = original["day"]
    if day >= clock.date().isoformat():
        raise ValueError("repair_requires_past_saved_session")
    inputs = original["steps"]["inputs"]["result"]
    calendar = original["steps"]["calendar"]["result"]
    if (inputs["status"] != "success" or calendar["status"] != "success"
            or day not in calendar["calendar"]["dates"]
            or original["steps"]["inputs"].get("input_date") != day
            or not all(data.intact(ref) for result in (inputs, calendar) for ref in data.artifacts_of(result))):
        raise ValueError("repair_inputs_unconfirmed_or_hash_mismatch")
    repair_id = str(uuid.uuid4())
    work = root / "runs" / repair_id
    worker = object.__new__(DailyRunner)
    worker.root, worker.work, worker.day = root, work, day
    worker.results = {"inputs": inputs, "calendar": calendar}
    worker.manifest = {"run_id": repair_id, "day": day, "source_mode": "archived_environment_repair",
        "started_at": clock.isoformat(), "status": "running", "retrospective": True,
        "forward_signal": False, "origin_run": {"path": str(original_path), "sha256": sha256(original_path)},
        "version_manifest": version_manifest.manifest(), "steps": {"features": {"status": "running"}}}
    with RunLock(root):
        worker.save()
        regime, ref = worker.build_environment()
        worker.manifest["steps"]["features"].update(status="partial", result={
            "status": "partial", "regime": regime, "regime_artifact": ref,
            "reason": "environment_only_sector_features_not_replayed"})
        worker.manifest.update(status="environment_repaired", finished_at=worker.timestamp())
        worker.save()
        _write_json(work / "repair.json", {"origin_run_id": run_id, "repair_run_id": repair_id,
            "regime_artifact": ref, "generated_at": worker.timestamp(), "forward_signal": False})
    return {"run_id": repair_id, "day": day, "regime_artifact": ref, "regime": regime,
            "forward_signal": False}


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.run_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
