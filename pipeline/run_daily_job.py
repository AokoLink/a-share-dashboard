"""Detached worker for the app's automatic and button-launched daily jobs."""
import argparse
import os
import threading
import traceback

from core import strategy_lifecycle, store
from core.daily_data import read_json
from core.daily_jobs import DailyJobs, process_identity, retry_basis
from core.daily_runner import DailyRunner


def execute(root, job_id, *, clock=None, runner_factory=None):
    clock = clock or strategy_lifecycle.observation_clock
    initial = DailyJobs("unused", root, clock=clock)
    job = read_json(initial.job_path(job_id))
    manager = DailyJobs(job["db"], root, clock=clock)
    stop = threading.Event()
    heartbeat = None
    try:
        if job["day"] != strategy_lifecycle.observation_clock(clock()).date().isoformat():
            raise ValueError("date_changed_before_worker_start")
        manager.update(job_id, status="running", started_at=clock().isoformat(),
                       heartbeat_at=clock().isoformat(), pid=os.getpid(), process_identity=process_identity(os.getpid()))
        runner = (runner_factory or DailyRunner)(job["db"], root, now=clock())
        def progress():
            while not stop.wait(2):
                try:
                    fields = {"heartbeat_at": clock().isoformat()}
                    if getattr(runner, "work", None):
                        fields["run_id"] = runner.work.name
                    manager.update(job_id, **fields)
                except Exception:
                    traceback.print_exc()
        heartbeat = threading.Thread(target=progress, daemon=True)
        heartbeat.start()
        result = runner.run()
        stop.set()
        heartbeat.join()
        manager.update(job_id, status=result["status"], run_id=result["run_id"], finished_at=clock().isoformat(),
                       heartbeat_at=clock().isoformat(), retry_basis=retry_basis(result))
        return result
    except Exception as exc:
        stop.set()
        if heartbeat:
            heartbeat.join()
        traceback.print_exc()
        manager.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500], finished_at=clock().isoformat())
        return {"status": "failed", "error": str(exc)}
    finally:
        store.close_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    result = execute(args.root, args.job_id)
    return 1 if result["status"] in {"failed", "partial"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
