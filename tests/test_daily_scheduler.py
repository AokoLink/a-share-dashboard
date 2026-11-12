from datetime import datetime
from types import SimpleNamespace

from core.daily_runner import AlreadyRunning
from core.daily_data import read_json
from pipeline.daily_scheduler import tick
from pipeline.daily_scheduler import wait_for_worker
from core.stage1_data import _write_json


def test_external_trigger_uses_same_automatic_gate_and_saves_heartbeat(tmp_path):
    calls = []
    def request(**kw):
        calls.append(kw)
        return {"status": "waiting_for_close", "day": "2026-11-11"}
    result = tick("unused", tmp_path, clock=lambda: datetime(2026, 11, 11, 14),
                  manager=SimpleNamespace(request=request))
    assert calls == [{"automatic": True}]
    assert result["status"] == "waiting_for_close"
    assert result["checked_at"].endswith("+08:00")
    assert read_json(tmp_path / "automation/scheduler.json") == result


def test_busy_and_failed_trigger_leave_diagnostic_without_launching_alternative(tmp_path):
    for error, expected in [(AlreadyRunning("locked"), "busy"), (ValueError("broken_calendar"), "error")]:
        def request(**kw):
            raise error
        result = tick("unused", tmp_path, manager=SimpleNamespace(request=request))
        assert result["status"] == expected
        assert read_json(tmp_path / "automation/scheduler.json")["status"] == expected
    assert result["error"] == "ValueError: broken_calendar"


def test_task_waits_only_for_own_worker_and_not_reused_pid(tmp_path):
    job_id = "00000000-0000-0000-0000-000000000001"
    _write_json(tmp_path / "automation/jobs" / job_id / "job.json", {"pid": 123, "process_identity": "birth"})
    checks = iter(["birth", "different-process"])
    slept = []
    result = {"job_id": job_id, "status": "queued"}
    wait_for_worker(result, tmp_path, identity=lambda pid: next(checks), sleep=slept.append)
    assert slept == [30]
    wait_for_worker({**result, "reused": True}, tmp_path,
                    identity=lambda pid: (_ for _ in ()).throw(AssertionError("must not inspect reused worker")))
