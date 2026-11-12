"""Persistent background daily jobs shared by scheduler and manual fallback."""
import contextlib
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from core import report_cache, strategy_lifecycle
from core.daily_data import read_json
from core.daily_runner import AlreadyRunning, RunLock
from core.stage1_data import _write_json, load_calendar

PROJECT = Path(__file__).resolve().parents[1]
COMPLETE = {"success", "no_candidates", "suspended"}
ACTIVE = {"queued", "running"}
MAX_AUTOMATIC_ATTEMPTS = 3
RETRY_DELAYS = (300, 900)


def retry_basis(run):
    """Retain small failure facts; dependent skips are not new failure causes."""
    steps = run.get("steps", {})
    frozen = steps.get("freeze", {}).get("result", {})
    errors = []
    def collect(value, error_list=False):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"error", "errors"}:
                    if isinstance(item, str) and item:
                        errors.append(item[:1000])
                    else:
                        collect(item, True)
                elif isinstance(item, (dict, list)) and key not in {"attempt_history", "calendar"}:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                if error_list and isinstance(item, str) and item:
                    errors.append(item[:1000])
                else:
                    collect(item)
    for step in steps.values():
        if step.get("status") in {"failed", "partial"}:
            collect(step.get("result", {}))
    # A fresh final report can rule out retries without loading all price inputs again.
    return {"frozen": frozen.get("freeze_status") == "frozen" or bool(frozen.get("snapshot_ids")),
            "all_transient": bool(errors) and all(transient_error(error) for error in errors),
            "errors": sorted(set(errors))[:30]}


def transient_error(error):
    text = str(error).lower()
    if any(token in text for token in ("valueerror", "typeerror", "keyerror", "permissionerror", "invalid", "schema")):
        return False
    return any(token in text for token in ("readtimeout", "connecttimeout", "timeouterror", "connectionerror",
        "connectionreseterror", "connectionabortederror", "remotedisconnected", "newconnectionerror",
        "connection reset", "connection aborted", "read timed out", "connect timed out",
        "429 client error", "502 server error", "503 server error", "504 server error"))


def progress_view(run):
    steps = {}
    for name, step in run.get("steps", {}).items():
        row = {key: step[key] for key in ("status", "started_at", "elapsed_seconds") if key in step}
        result = step.get("result", {})
        for key in ("reason", "error"):
            if result.get(key):
                row[key] = str(result[key])[:500]
        steps[name] = row
    return {"run_id": run.get("run_id"), "day": run.get("day"), "status": run.get("status"), "steps": steps}


def process_identity(pid):
    """Identify a live process by creation time, so reused PIDs are not jobs."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = dll.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            stamps = [wintypes.FILETIME() for _ in range(4)]
            if not dll.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            if not dll.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in stamps)):
                return None
            return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)
        finally:
            dll.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rpartition(")")[2].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def launch(root, job_id, log):
    args = [sys.executable, "-u", "-m", "pipeline.run_daily_job", "--root", str(root), "--job-id", job_id]
    with open(log, "ab") as output:
        return subprocess.Popen(args, cwd=PROJECT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt")


class DailyJobs:
    def __init__(self, db, root, *, clock=None, launcher=None, identity=None):
        self.db, self.root = str(Path(db).resolve()), Path(root).resolve()
        self.base = self.root / "automation"
        self.clock = clock or strategy_lifecycle.observation_clock
        self.launcher, self.identity = launcher or launch, identity or process_identity

    @contextlib.contextmanager
    def locked(self):
        deadline = time.monotonic() + 2
        while True:
            try:
                lock = RunLock(self.base)
                lock.__enter__()
                break
            except AlreadyRunning:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def job_path(self, job_id):
        return self.base / "jobs" / str(uuid.UUID(job_id)) / "job.json"

    def gate(self):
        now = strategy_lifecycle.observation_clock(self.clock())
        day = now.date().isoformat()
        try:
            calendar = load_calendar(self.root / "calendar.json") or load_calendar()
            if not calendar:
                reason = "calendar_unavailable"
            elif not calendar["first"] <= day <= calendar["last"]:
                reason = "calendar_uncovered"
            elif day not in calendar["dates"]:
                reason = "market_closed"
            else:
                reason = None
        except (OSError, ValueError, KeyError, TypeError):
            reason = "calendar_unavailable"
        if (now.hour, now.minute) < (15, 5):
            if reason in (None, "calendar_unavailable", "calendar_uncovered"):
                reason = "waiting_for_close"
        elif now.hour >= 23:
            reason = "collection_window_closed"
        return {"day": day, "server_time": now.isoformat(), "gate_reason": reason, "schedule": "15:05", "timezone": "Asia/Shanghai",
                "catch_up_until": "23:00"}

    def pipeline_busy(self):
        try:
            with RunLock(self.root):
                return False
        except AlreadyRunning:
            return True

    def _latest(self):
        path = self.base / "latest.json"
        if not path.exists():
            return None
        ref = read_json(path)
        return read_json(self.job_path(ref["job_id"]))

    def _save(self, job):
        _write_json(self.job_path(job["job_id"]), job)

    def _day_attempt(self, day):
        path = self.base / "days" / (day + ".json")
        if not path.exists():
            return {"job_ids": [], "automatic_attempts": 0}, None
        index = read_json(path)
        prior = read_json(self.job_path(index["job_id"]))
        index.setdefault("job_ids", [index["job_id"]])
        index.setdefault("automatic_attempts", int(prior.get("origin") == "automatic"))
        return index, prior

    def retry_state(self, job, automatic_attempts, gate=None):
        gate = gate or self.gate()
        state = {"status": "not_needed", "reason": None, "automatic_attempts": automatic_attempts,
                 "max_automatic_attempts": MAX_AUTOMATIC_ATTEMPTS}
        if not job or job.get("status") not in {"failed", "partial", "interrupted"}:
            return state
        state["status"] = "suppressed"
        if job.get("day") != gate["day"]:
            return {**state, "reason": "different_day"}
        try:
            basis = job.get("retry_basis")
            if basis is None and job.get("run_id"):
                path = self.root / "runs" / str(uuid.UUID(job["run_id"])) / "run.json"
                if path.exists():
                    basis = report_cache.read(path, project=retry_basis, projection_key="job-retry-basis-v1")
            basis = basis or {"frozen": False, "errors": []}
            candidate = self.root / "candidates" / (job["day"] + ".json")
            if candidate.exists():
                saved = report_cache.read(candidate, project=lambda r: {
                    "day": r.get("data_as_of"), "frozen": r.get("freeze_status") == "frozen" or bool(r.get("snapshot_ids"))},
                    projection_key="job-candidate-sealed-v1")
                if saved["day"] == job["day"] and saved["frozen"]:
                    basis = {**basis, "frozen": True}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {**state, "reason": "unknown_failure"}
        if basis["frozen"]:
            return {**state, "reason": "candidate_already_frozen"}
        errors = basis["errors"] + ([job["error"]] if job.get("error") and job["status"] != "interrupted" else [])
        if job["status"] != "interrupted" and (not errors or basis.get("all_transient") is False or
                                               not all(transient_error(e) for e in errors)):
            return {**state, "reason": "not_transient" if errors else "unknown_failure"}
        if automatic_attempts >= MAX_AUTOMATIC_ATTEMPTS:
            return {**state, "status": "exhausted", "reason": "daily_retry_limit"}
        if gate["gate_reason"]:
            return {**state, "reason": "retry_window_closed"}
        try:
            finished = strategy_lifecycle.observation_clock(job["finished_at"])
        except (KeyError, ValueError, TypeError):
            return {**state, "reason": "unknown_failure"}
        retry_at = finished + timedelta(seconds=RETRY_DELAYS[min(max(automatic_attempts - 1, 0), len(RETRY_DELAYS) - 1)])
        return {**state, "status": "eligible" if strategy_lifecycle.observation_clock(self.clock()) >= retry_at else "waiting",
                "reason": "process_interrupted" if job["status"] == "interrupted" else "transient_failure",
                "next_retry_at": retry_at.isoformat()}

    def gaps(self, sessions=20):
        """Read-only coverage view. A recovery never counts as an original daily signal."""
        now = strategy_lifecycle.observation_clock(self.clock())
        day = now.date().isoformat()
        result = {"status": "calendar_unavailable", "as_of": day, "checked_sessions": 0,
                  "missing_original_dates": [], "missing_environment_dates": [],
                  "missing_market_snapshot_dates": [], "incomplete_dates": [], "rows": []}
        try:
            calendar = load_calendar(self.root / "calendar.json") or load_calendar()
            if not calendar:
                return result
            if not calendar["first"] <= day <= calendar["last"]:
                return {**result, "status": "calendar_uncovered"}
            closed = sorted(set(d for d in calendar["dates"] if d < day or
                                d == day and (now.hour, now.minute) >= (15, 5)))[-sessions:]
        except (OSError, ValueError, KeyError, TypeError):
            return result
        for saved_day in closed:
            row = {"day": saved_day, "candidate_status": "missing", "market_snapshot_status": "missing",
                   "environment_status": "missing", "job_status": "not_run", "run_status": "not_run"}
            recovery_index = None
            path = self.root / "candidates" / (saved_day + ".json")
            if path.exists():
                try:
                    candidate = report_cache.read(path, project=lambda r: {
                        "day": r.get("data_as_of"), "retrospective": r.get("retrospective", False),
                        "market_state": r.get("market_state"), "groups_valid": isinstance(r.get("groups"), dict)},
                        projection_key="daily-gap-candidate-v1")
                    if candidate["day"] != saved_day or not candidate["groups_valid"] or candidate["retrospective"]:
                        raise ValueError("invalid_daily_candidate")
                    row["candidate_status"] = "original"
                    row["environment_status"] = "available" if candidate["market_state"] else "unavailable"
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    row["candidate_status"] = "invalid"
            else:
                recovery_indices = [self.root / folder / "days" / (saved_day + ".json") for folder in
                                    ("research_candidates", "risk_revision", "strategy_revision")]
                recovery_index = next((p for p in recovery_indices if p.exists()), None)
            if row["candidate_status"] == "missing" and recovery_index:
                try:
                    ref = read_json(recovery_index)
                    if Path(ref["path"]).resolve().parent != (self.root / "research_candidates" / "blobs").resolve():
                        raise ValueError("invalid_recovery_path")
                    recovered = report_cache.read(ref["path"], ref["sha256"], project=lambda r: {
                        "day": r.get("data_as_of"), "retrospective": r.get("retrospective"), "forward": r.get("forward_signal")},
                        projection_key="daily-gap-recovery-v1")
                    if recovered != {"day": saved_day, "retrospective": True, "forward": False}:
                        raise ValueError("invalid_recovery_kind")
                    row["candidate_status"] = "recovered_only"
                except (OSError, ValueError, KeyError, TypeError):
                    row["candidate_status"] = "invalid"
            market = self.root / "market_daily" / (saved_day + ".json")
            if market.exists():
                try:
                    row["market_snapshot_status"] = "available" if read_json(market).get("date") == saved_day else "invalid"
                except (OSError, ValueError, TypeError, AttributeError):
                    row["market_snapshot_status"] = "invalid"
            try:
                _, job = self._day_attempt(saved_day)
                if job:
                    row["job_status"] = job["status"]
            except (OSError, ValueError, KeyError, TypeError):
                row["job_status"] = "invalid"
            run_index = self.root / "days" / (saved_day + ".json")
            if run_index.exists():
                try:
                    row["run_status"] = read_json(run_index)["status"]
                except (OSError, ValueError, KeyError, TypeError):
                    row["run_status"] = "invalid"
            result["rows"].append(row)
        result["missing_original_dates"] = [r["day"] for r in result["rows"] if r["candidate_status"] != "original"]
        result["missing_environment_dates"] = [r["day"] for r in result["rows"] if r["environment_status"] != "available"]
        result["missing_market_snapshot_dates"] = [r["day"] for r in result["rows"] if r["market_snapshot_status"] != "available"]
        result["incomplete_dates"] = [r["day"] for r in result["rows"] if any(
            r[key] in {"failed", "partial", "interrupted", "running", "queued", "invalid"} for key in ("job_status", "run_status"))]
        result["checked_sessions"] = len(closed)
        result["status"] = "gaps_detected" if any(r["candidate_status"] != "original" or
            r["market_snapshot_status"] != "available" or r["environment_status"] != "available"
            for r in result["rows"]) or result["incomplete_dates"] else "ok"
        return result

    def _recover(self, job):
        if not job or job["status"] not in ACTIVE:
            return job
        live = job.get("pid") and self.identity(job["pid"])
        if live and live == job.get("process_identity"):
            return job
        if self.pipeline_busy():
            return {**job, "status": "running", "reason": "daily_pipeline_active"}
        job = {**job, "status": "interrupted", "error": "后台进程已停止；可点按钮重试",
               "finished_at": self.clock().isoformat()}
        self._save(job)
        return job

    def status(self):
        gate = self.gate()
        with self.locked():
            job = self._recover(self._latest())
            result = dict(job or {"status": "idle", "day": gate["day"]})
            if not job or job["status"] not in ACTIVE:
                if self.pipeline_busy():
                    result = {"status": "running", "day": gate["day"], "reason": "external_daily_pipeline"}
                else:
                    path = self.root / "days" / (gate["day"] + ".json")
                    if path.exists():
                        finished = read_json(path)
                        if finished.get("status") in COMPLETE and (not job or job.get("day") != gate["day"] or job["status"] not in COMPLETE):
                            result = {"status": "already_completed", "day": gate["day"], "run_id": finished["run_id"]}
        if job and job.get("run_id"):
            path = self.root / "runs" / str(uuid.UUID(job["run_id"])) / "run.json"
            if path.exists():
                try:
                    result["progress"] = report_cache.read(path, project=progress_view, projection_key="job-progress-v1")
                except (OSError, ValueError, KeyError, TypeError):
                    result["progress_error"] = "运行记录暂时不可读"
        result["automation"] = gate
        index, today_job = self._day_attempt(gate["day"])
        if job and today_job and job.get("job_id") == today_job.get("job_id"):
            today_job = job
        result["retry"] = self.retry_state(today_job, index["automatic_attempts"], gate)
        result["gaps"] = self.gaps()
        if job and job.get("status") in ACTIVE:
            heartbeat = {"status": "unknown", "last_seen": job.get("heartbeat_at"), "age_seconds": None}
            if job.get("heartbeat_at"):
                try:
                    age = (strategy_lifecycle.observation_clock(self.clock()) -
                           strategy_lifecycle.observation_clock(job["heartbeat_at"])).total_seconds()
                    heartbeat.update(age_seconds=max(0, age), status="delayed" if age > 60 else "current")
                except (ValueError, TypeError):
                    pass
            result["heartbeat"] = heartbeat
        try:
            scheduler = read_json(self.base / "scheduler.json")
            checked = strategy_lifecycle.observation_clock(scheduler["checked_at"])
            age = (strategy_lifecycle.observation_clock(self.clock()) - checked).total_seconds()
            result["scheduler"] = {**{k: scheduler[k] for k in
                ("checked_at", "status", "scheduler", "job_id", "day", "error") if k in scheduler},
                "age_seconds": max(0, age), "fresh": 0 <= age <= 600}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if job and job.get("status") in {"failed", "partial", "interrupted"}:
            try:
                ref = read_json(self.root / "research_candidates" / "days" / (job["day"] + ".json"))
                if Path(ref["path"]).resolve().parent != (self.root / "research_candidates" / "blobs").resolve():
                    raise ValueError("invalid_recovery_path")
                recovered = report_cache.read(ref["path"], ref["sha256"],
                    project=lambda report: {"day": report["data_as_of"], "retrospective": report.get("retrospective"),
                        "generated_at": report["generated_at"], "forward_signal": report.get("forward_signal"),
                        "counts": {k: len(v) for k, v in report["groups"].items()}},
                    projection_key="daily-job-recovery-v1")
                if recovered["day"] == job["day"] and recovered["retrospective"] and recovered["forward_signal"] is False:
                    result["historical_recovery"] = recovered
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return result

    def request(self, *, automatic=False):
        gate = self.gate()
        # Manual launch may refresh an unavailable calendar; the runner still gates before prices.
        blocked = gate["gate_reason"]
        if blocked and (automatic or blocked not in {"calendar_unavailable", "calendar_uncovered"}):
            return {"status": blocked, **gate}
        with self.locked():
            current = self._recover(self._latest())
            if current and current["status"] in ACTIVE:
                return {**current, "reused": True}
            if self.pipeline_busy():
                return {"status": "running", "day": gate["day"], "reason": "external_daily_pipeline", "reused": True}
            day_path = self.root / "days" / (gate["day"] + ".json")
            if day_path.exists():
                completed = read_json(day_path)
                if completed.get("status") in COMPLETE:
                    return {"status": "already_completed", "day": gate["day"], "run_id": completed["run_id"]}
            attempt = self.base / "days" / (gate["day"] + ".json")
            index, previous = self._day_attempt(gate["day"])
            if previous and current and previous["job_id"] == current.get("job_id"):
                previous = current
            if automatic and previous:
                retry = self.retry_state(previous, index["automatic_attempts"], gate)
                if retry["status"] != "eligible":
                    return {**previous, "reused": True, "retry": retry}
            job_id = str(uuid.uuid4())
            job = {"job_id": job_id, "day": gate["day"], "db": self.db, "status": "queued",
                   "origin": "automatic" if automatic else "button", "requested_at": self.clock().isoformat(),
                   "attempt_number": len(index["job_ids"]) + 1,
                   "automatic_attempt_number": index["automatic_attempts"] + int(automatic)}
            if previous:
                job["retry_of"] = previous["job_id"]
            self._save(job)
            _write_json(self.base / "latest.json", {"job_id": job_id})
            _write_json(attempt, {"job_id": job_id, "job_ids": index["job_ids"] + [job_id],
                "automatic_attempts": index["automatic_attempts"] + int(automatic)})
            try:
                proc = self.launcher(self.root, job_id, self.job_path(job_id).with_name("worker.log"))
                job.update(pid=proc.pid, process_identity=self.identity(proc.pid))
                if not job["process_identity"]:
                    raise RuntimeError("无法确认后台进程身份，请检查 worker.log")
            except Exception as exc:
                job.update(status="failed", error=str(exc)[:500], finished_at=self.clock().isoformat())
            self._save(job)
            return job

    def update(self, job_id, **fields):
        with self.locked():
            job = read_json(self.job_path(job_id))
            job.update(fields)
            self._save(job)
            return job


def controller(app):
    key = (str(Path(app.config["DB"]).resolve()), str(Path(app.config["DAILY_PIPELINE_ROOT"]).resolve()))
    registry = app.extensions.setdefault("daily_jobs", {})
    if key not in registry:
        registry[key] = DailyJobs(*key)
    return registry[key]


def start_scheduler(app):
    """Only the real app entrypoint opts in; importing/creating test apps is inert."""
    if app.extensions.get("daily_scheduler"):
        return
    stop = threading.Event()
    app.config["DAILY_AUTO_RUN_ENABLED"] = True
    def loop():
        while not stop.is_set():
            try:
                controller(app).request(automatic=True)
            except Exception:
                app.logger.exception("daily automatic launch failed")
            stop.wait(30)
    thread = threading.Thread(target=loop, name="daily-candidate-scheduler", daemon=True)
    app.extensions["daily_scheduler"] = {"thread": thread, "stop": stop}
    thread.start()
