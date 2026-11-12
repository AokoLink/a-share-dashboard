"""Independent daily-job trigger; shares the app's persistent gate and locks."""
import argparse
import json
from pathlib import Path
import time

from core import daily_data, strategy_lifecycle
from core.daily_jobs import DailyJobs, process_identity
from core.daily_runner import AlreadyRunning
from core.stage1_data import _write_json


def tick(db, root=daily_data.ROOT, *, clock=None, manager=None):
    clock = clock or strategy_lifecycle.observation_clock
    manager = manager or DailyJobs(db, root, clock=clock)
    record = {"checked_at": strategy_lifecycle.observation_clock(clock()).isoformat(),
              "scheduler": "windows_task_or_cli"}
    try:
        result = manager.request(automatic=True)
        record.update({key: result[key] for key in ("status", "job_id", "day", "reused") if key in result})
    except AlreadyRunning:
        record["status"] = "busy"
    except Exception as exc:
        record.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
    _write_json(Path(root) / "automation" / "scheduler.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(daily_data.ROOT))
    parser.add_argument("--db", default=str(daily_data.ROOT.parents[1] / "data" / "market.db"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="One check, suitable for a system scheduled task")
    mode.add_argument("--loop", action="store_true", help="Poll every 30 seconds, independently of Flask")
    args = parser.parse_args()
    while True:
        result = tick(args.db, args.root)
        # pythonw (Windows scheduled task) has no console streams.
        import sys
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if not args.loop:
            wait_for_worker(result, args.root)
            return 1 if result["status"] == "error" else 0
        time.sleep(30)


def wait_for_worker(result, root, *, identity=process_identity, sleep=time.sleep):
    """Keep a scheduled task alive for its own worker, never attach to reused work."""
    if not result.get("job_id") or result.get("reused") or result.get("status") not in {"queued", "running"}:
        return
    manager = DailyJobs("unused", root)
    job = daily_data.read_json(manager.job_path(result["job_id"]))
    pid, birth = job.get("pid"), job.get("process_identity")
    while pid and birth and identity(pid) == birth:
        sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
