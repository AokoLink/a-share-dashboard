import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from core import daily_jobs, store
from core.daily_runner import RunLock
from core.stage1_data import _write_json
from pipeline.run_daily_job import execute
from tests.test_daily_pipeline import NOW, Sources
from web.routes import daily_jobs as routes


def manager(tmp_path, now=NOW, **kwargs):
    _write_json(tmp_path / "calendar.json", {"first": "2026-09-01", "last": "2026-12-31",
        "dates": ["2026-09-30", "2026-10-08", "2026-10-09"]})
    launched = []
    def fake_launch(root, job_id, log):
        launched.append(job_id)
        return SimpleNamespace(pid=123)
    jobs = daily_jobs.DailyJobs(tmp_path / "db.sqlite", tmp_path, clock=lambda: now,
        launcher=kwargs.get("launcher", fake_launch), identity=kwargs.get("identity", lambda pid: "worker-birth"))
    return jobs, launched


@pytest.mark.parametrize("clock,expected", [
    (datetime(2026, 10, 9, 15, 4), "waiting_for_close"),
    (datetime(2026, 10, 10, 17), "market_closed"),
    (datetime(2026, 10, 9, 23), "collection_window_closed"),
    (datetime(2027, 1, 4, 17), "calendar_uncovered"),
])
def test_automatic_gate_does_not_start_ineligible_jobs(tmp_path, clock, expected):
    jobs, launched = manager(tmp_path, clock)
    assert jobs.request(automatic=True)["status"] == expected
    assert not launched
    assert jobs.gate()["server_time"].endswith("+08:00")


def test_automatic_catches_up_at_five_and_duplicate_manual_click_reuses_job(tmp_path):
    jobs, launched = manager(tmp_path, datetime(2026, 10, 9, 17))
    first = jobs.request(automatic=True)
    assert first["origin"] == "automatic" and first["day"] == "2026-10-09"
    second = jobs.request()
    assert second["job_id"] == first["job_id"] and second["reused"]
    assert len(launched) == 1
    assert jobs.status()["status"] == "queued"


def test_failed_job_displays_verified_recovery_without_rewriting_failure(tmp_path):
    from core.stage1_data import sha256
    jobs, launched = manager(tmp_path)
    job = jobs.request()
    jobs.update(job["job_id"], status="failed")
    before = jobs.job_path(job["job_id"]).read_bytes()
    blob = tmp_path / "research_candidates" / "blobs" / "saved.json"
    _write_json(blob, {"data_as_of": NOW.date().isoformat(), "retrospective": True,
        "forward_signal": False, "generated_at": "2026-10-01T10:00:00+08:00", "groups": {"breakout": [1, 2]}})
    _write_json(tmp_path / "research_candidates" / "days" / (NOW.date().isoformat() + ".json"),
                {"path": str(blob.resolve()), "sha256": sha256(blob)})
    status = jobs.status()
    assert status["status"] == "failed"
    assert status["historical_recovery"]["counts"]["breakout"] == 2
    assert jobs.job_path(job["job_id"]).read_bytes() == before
    blob.write_text('{}', encoding="utf-8")
    assert "historical_recovery" not in jobs.status()


def test_shared_file_lock_prevents_concurrent_requests_across_controllers(tmp_path):
    jobs, launched = manager(tmp_path)
    other = daily_jobs.DailyJobs(jobs.db, jobs.root, clock=jobs.clock, launcher=jobs.launcher, identity=jobs.identity)
    barrier = threading.Barrier(2)
    results = []
    def click(controller):
        barrier.wait()
        results.append(controller.request())
    threads = [threading.Thread(target=click, args=(controller,)) for controller in (jobs, other)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(results) == 2
    assert results[0]["job_id"] == results[1]["job_id"]
    assert len(launched) == 1


def test_automatic_attempt_persists_across_restart_and_manual_retry_is_explicit(tmp_path):
    jobs, launched = manager(tmp_path)
    first = jobs.request(automatic=True)
    jobs.update(first["job_id"], status="partial")
    restarted = daily_jobs.DailyJobs(jobs.db, jobs.root, clock=jobs.clock, launcher=jobs.launcher, identity=jobs.identity)
    assert restarted.request(automatic=True)["job_id"] == first["job_id"]
    assert len(launched) == 1
    retry = restarted.request()
    assert retry["job_id"] != first["job_id"] and retry["origin"] == "button"
    assert len(launched) == 2


def test_finished_day_is_not_regenerated_by_auto_or_button(tmp_path):
    jobs, launched = manager(tmp_path)
    run_id = str(uuid.uuid4())
    _write_json(tmp_path / "days" / (NOW.date().isoformat() + ".json"), {"status": "no_candidates", "run_id": run_id})
    assert jobs.request()["status"] == "already_completed"
    assert jobs.request(automatic=True)["run_id"] == run_id
    assert jobs.status()["status"] == "already_completed"
    assert not launched


def test_external_cli_lock_is_respected_and_visible(tmp_path):
    jobs, launched = manager(tmp_path)
    with RunLock(tmp_path):
        assert jobs.request()["reason"] == "external_daily_pipeline"
        assert jobs.status()["status"] == "running"
    assert not launched


def test_pid_reuse_is_interruption_not_a_live_job(tmp_path):
    jobs, launched = manager(tmp_path)
    job = jobs.request()
    jobs.identity = lambda pid: "different-process-birth"
    assert jobs.status()["status"] == "interrupted"
    assert jobs.request()["job_id"] != job["job_id"]


def test_launch_failure_retained_and_does_not_retry_automatically(tmp_path):
    def failed(*args): raise OSError("cannot start Python")
    jobs, _ = manager(tmp_path, launcher=failed)
    result = jobs.request(automatic=True)
    assert result["status"] == "failed" and "cannot start" in result["error"]
    assert jobs.request(automatic=True)["job_id"] == result["job_id"]


def test_worker_runs_existing_pipeline_with_fixture_inputs_and_preserves_status(tmp_path):
    from core.daily_runner import DailyRunner
    jobs, _ = manager(tmp_path)
    job = jobs.request()
    result = execute(tmp_path, job["job_id"], clock=lambda: NOW,
        runner_factory=lambda db, root, now: DailyRunner(db, root, now=now, sources=Sources(), sector_map={"600001": ["A"]}))
    assert result["status"] == "success"
    state = jobs.status()
    assert state["status"] == "success" and state["run_id"] == result["run_id"]
    assert state["progress"]["steps"]["freeze"]["status"] == "success"
    assert len(store.list_strategy_signals(jobs.db)) == 4  # Injected fixtures, never production evidence.
    assert jobs.request()["status"] == "already_completed"


def test_worker_failure_is_visible_and_clock_change_cannot_backdate(tmp_path):
    jobs, _ = manager(tmp_path)
    job = jobs.request()
    result = execute(tmp_path, job["job_id"], clock=lambda: datetime(2026, 10, 9, 17),
                     runner_factory=lambda *a, **k: pytest.fail("must not backdate"))
    assert result["status"] == "failed" and "date_changed" in result["error"]
    assert jobs.status()["status"] == "failed"


def test_real_detached_worker_imports_and_returns_guard_error_without_any_acquisition(tmp_path):
    job_id = str(uuid.uuid4())
    jobs = daily_jobs.DailyJobs(tmp_path / "unused.db", tmp_path)
    path = jobs.job_path(job_id)
    # Cannot equal any actual calendar date, so the worker rejects before constructing a data source.
    _write_json(path, {"job_id": job_id, "day": "invalid-date-for-test", "status": "queued", "db": jobs.db})
    proc = daily_jobs.launch(tmp_path, job_id, path.with_name("worker.log"))
    assert proc.wait(timeout=30) == 1
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["status"] == "failed" and "date_changed" in result["error"]
    assert not (tmp_path / "runs").exists()
    assert not Path(jobs.db).exists()


def test_process_identity_is_live_birth_stamp():
    assert daily_jobs.process_identity(os.getpid())
    assert daily_jobs.process_identity(999999999) is None


def test_api_poll_never_launches_and_post_returns_quickly(tmp_path, monkeypatch):
    jobs, launched = manager(tmp_path)
    app = Flask(__name__)
    app.config["DAILY_AUTO_RUN_ENABLED"] = True
    monkeypatch.setattr(routes, "controller", lambda app: jobs)
    routes.register(app)
    client = app.test_client()
    assert client.get("/api/daily-job").json["data"]["status"] == "idle"
    assert not launched
    assert client.post("/api/daily-job", json={}).json["data"]["status"] == "queued"
    assert len(launched) == 1
    assert client.post("/api/daily-job", json={}).json["data"]["reused"]
    assert client.get("/api/daily-job").json["data"]["automation"]["enabled"]
    assert client.post("/api/daily-job", json={"date": "2026-10-08"}).status_code == 400
    assert client.post("/api/daily-job", json={}, headers={"Origin": "https://outside.example"}).status_code == 403


def test_plain_app_factory_does_not_start_scheduler_or_jobs(tmp_path, monkeypatch):
    from web import create_app
    monkeypatch.setattr("core.data_source.validate_sector_map", lambda: {"ok": True})
    app = create_app(str(tmp_path / "web.db"))
    assert "daily_scheduler" not in app.extensions
    assert not app.config["DAILY_AUTO_RUN_ENABLED"]


def test_scheduler_entrypoint_starts_once_and_can_stop(tmp_path, monkeypatch):
    app = Flask(__name__)
    called = threading.Event()
    count = []
    fake = SimpleNamespace(request=lambda **kw: (count.append(kw), called.set()))
    monkeypatch.setattr(daily_jobs, "controller", lambda app: fake)
    daily_jobs.start_scheduler(app)
    daily_jobs.start_scheduler(app)
    assert called.wait(2)
    app.extensions["daily_scheduler"]["stop"].set()
    app.extensions["daily_scheduler"]["thread"].join(2)
    assert count == [{"automatic": True}]
    assert app.config["DAILY_AUTO_RUN_ENABLED"]


def test_background_application_files_are_captured_and_offline_replay_still_matches(tmp_path):
    from core import signal_replay
    from tests.test_step_f import freeze
    _, snapshot = freeze(tmp_path)
    ref = snapshot["payload"]["replay_ref"]
    code = signal_replay.verified(signal_replay.verified(ref)["code"])
    assert "app.py" in code and "core/daily_jobs.py" in code and "pipeline/run_daily_job.py" in code
    assert signal_replay.replay(ref, tmp_path / "replay") ["identical"]


def test_transient_auto_retries_are_delayed_bounded_and_keep_each_attempt(tmp_path):
    now = datetime(2026, 10, 9, 17)
    jobs, launched = manager(tmp_path, now)
    jobs.clock = lambda: now
    first = jobs.request(automatic=True)
    jobs.update(first["job_id"], status="failed", error="ReadTimeout: server timed out", finished_at=now.isoformat())
    original = jobs.job_path(first["job_id"]).read_bytes()
    assert jobs.request(automatic=True)["retry"]["status"] == "waiting"
    now += timedelta(minutes=5)
    second = jobs.request(automatic=True)
    assert second["retry_of"] == first["job_id"]
    jobs.update(second["job_id"], status="failed", error="ConnectionError: connection reset", finished_at=now.isoformat())
    now += timedelta(minutes=14)
    assert jobs.request(automatic=True)["retry"]["status"] == "waiting"
    now += timedelta(minutes=1)
    third = jobs.request(automatic=True)
    jobs.update(third["job_id"], status="failed", error="TimeoutError: unavailable", finished_at=now.isoformat())
    now += timedelta(minutes=20)
    restarted = daily_jobs.DailyJobs(jobs.db, jobs.root, clock=jobs.clock, launcher=jobs.launcher, identity=jobs.identity)
    assert restarted.request(automatic=True)["retry"]["status"] == "exhausted"
    assert len(launched) == 3
    manual = restarted.request()
    assert manual["origin"] == "button" and manual["attempt_number"] == 4
    index = json.loads((jobs.base / "days/2026-10-09.json").read_text(encoding="utf-8"))
    assert index["automatic_attempts"] == 3 and len(index["job_ids"]) == 4
    assert jobs.job_path(first["job_id"]).read_bytes() == original


@pytest.mark.parametrize("error", ["ValueError: Out of range float values nan", "insufficient_daily_history", "cannot start Python"])
def test_permanent_or_unknown_failures_are_not_automatically_retried(tmp_path, error):
    jobs, launched = manager(tmp_path)
    job = jobs.request(automatic=True)
    jobs.update(job["job_id"], status="failed", error=error, finished_at=NOW.isoformat())
    jobs.clock = lambda: NOW + timedelta(hours=1)
    result = jobs.request(automatic=True)
    assert result["job_id"] == job["job_id"]
    assert result["retry"]["status"] == "suppressed" and len(launched) == 1


def test_partial_report_with_frozen_candidates_never_auto_regenerates(tmp_path):
    jobs, launched = manager(tmp_path)
    job = jobs.request(automatic=True)
    run_id = str(uuid.uuid4())
    _write_json(tmp_path / "runs" / run_id / "run.json", {"steps": {
        "features": {"status": "partial", "result": {"errors": [{"error": "ReadTimeout: slow"}]}},
        "freeze": {"status": "success", "result": {"freeze_status": "frozen", "snapshot_ids": {"breakout": 1}}}}})
    jobs.update(job["job_id"], status="partial", run_id=run_id, finished_at=NOW.isoformat())
    jobs.clock = lambda: NOW + timedelta(hours=1)
    assert jobs.request(automatic=True)["retry"]["reason"] == "candidate_already_frozen"
    assert len(launched) == 1


def test_worker_partial_network_errors_produce_retry_basis_without_counting_data_values(tmp_path):
    jobs, _ = manager(tmp_path)
    job = jobs.request()
    run_id = str(uuid.uuid4())
    result = {"run_id": run_id, "status": "partial", "steps": {
        "prices": {"status": "partial", "result": {"codes": ["600001"], "entries": {
            "600001": {"technical": {"error": "ReadTimeout: network"}}}}},
        "quality": {"status": "partial", "result": {"failed_codes": ["600001"]}},
        "freeze": {"status": "skipped", "result": {"reason": "dependency_prices_failed"}}}}
    execute(tmp_path, job["job_id"], clock=lambda: NOW,
        runner_factory=lambda *a, **kw: SimpleNamespace(run=lambda: result))
    saved = json.loads(jobs.job_path(job["job_id"]).read_text(encoding="utf-8"))
    assert saved["retry_basis"] == {"frozen": False, "all_transient": True, "errors": ["ReadTimeout: network"]}
    assert saved["heartbeat_at"]
    jobs.clock = lambda: NOW + timedelta(minutes=6)
    assert jobs.status()["retry"]["status"] == "eligible"


def test_interrupted_worker_waits_before_retry_and_stale_heartbeat_never_kills_live_worker(tmp_path):
    jobs, launched = manager(tmp_path)
    job = jobs.request(automatic=True)
    jobs.update(job["job_id"], status="running", heartbeat_at=(NOW - timedelta(minutes=3)).isoformat())
    assert jobs.status()["heartbeat"]["status"] == "delayed"
    assert jobs.request(automatic=True)["job_id"] == job["job_id"]
    jobs.identity = lambda pid: None
    assert jobs.status()["retry"]["status"] == "waiting"
    jobs.clock = lambda: NOW + timedelta(minutes=6)
    jobs.identity = lambda pid: "new-worker"
    assert jobs.request(automatic=True)["retry_of"] == job["job_id"]
    assert len(launched) == 2


def test_gap_audit_excludes_open_session_and_never_counts_backfill_as_original(tmp_path):
    from core.stage1_data import sha256
    jobs, launched = manager(tmp_path, datetime(2026, 10, 9, 12))
    _write_json(tmp_path / "candidates/2026-09-30.json", {
        "data_as_of": "2026-09-30", "groups": {}, "market_state": None})
    _write_json(tmp_path / "market_daily/2026-09-30.json", {"date": "2026-09-30"})
    blob = tmp_path / "research_candidates/blobs/recovered.json"
    _write_json(blob, {"data_as_of": "2026-10-08", "retrospective": True, "forward_signal": False})
    _write_json(tmp_path / "research_candidates/days/2026-10-08.json", {"path": str(blob.resolve()), "sha256": sha256(blob)})
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    gaps = jobs.gaps()
    assert gaps["missing_original_dates"] == ["2026-10-08"]
    assert [r["candidate_status"] for r in gaps["rows"]] == ["original", "recovered_only"]
    assert gaps["rows"][0]["environment_status"] == "unavailable"
    assert gaps["rows"][0]["market_snapshot_status"] == "available"
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert not launched
    blob.write_text('{}', encoding="utf-8")
    assert jobs.gaps()["rows"][-1]["candidate_status"] == "invalid"


def test_saved_scheduler_heartbeat_is_reported_without_starting_any_work(tmp_path):
    jobs, launched = manager(tmp_path)
    _write_json(jobs.base / "scheduler.json", {"checked_at": NOW.isoformat(),
        "scheduler": "windows_task_or_cli", "status": "checked", "day": NOW.date().isoformat()})
    result = jobs.status()
    assert result["scheduler"]["fresh"] and result["scheduler"]["age_seconds"] == 0
    assert not launched


def test_audit_reports_incomplete_cli_run_and_unknown_calendar(tmp_path):
    jobs, _ = manager(tmp_path)
    _write_json(tmp_path / "days/2026-09-30.json", {"status": "partial", "run_id": str(uuid.uuid4())})
    assert "2026-09-30" in jobs.gaps()["incomplete_dates"]
    jobs.clock = lambda: datetime(2027, 1, 4, 17)
    assert jobs.gaps()["status"] == "calendar_uncovered"
    assert jobs.gaps()["rows"] == []


def test_no_retry_when_any_error_is_permanent_even_after_error_summary_cap():
    run = {"steps": {"prices": {"status": "partial", "result": {"errors":
        [f"ConnectionError: server {i}" for i in range(40)] + ["ValueError: corrupted payload"]}}}}
    basis = daily_jobs.retry_basis(run)
    assert len(basis["errors"]) == 30 and basis["all_transient"] is False
