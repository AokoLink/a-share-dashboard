import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import stage1_data


def test_windows_reader_conflict_retries_without_exposing_partial_json(tmp_path, monkeypatch):
    target = tmp_path / "job.json"
    stage1_data._write_json(target, {"status": "old"})
    original = Path.replace
    attempts = []
    def replace(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(target.read_text()) == {"status": "old"}
            error = PermissionError("reader still holds document")
            error.winerror = 32
            raise error
        return original(source, destination)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(stage1_data.time, "sleep", lambda duration: None)
    stage1_data._write_json(target, {"status": "complete", "rows": list(range(1000))})
    assert json.loads(target.read_text())["status"] == "complete"
    assert len(attempts) == 3 and not list(tmp_path.glob("*.tmp"))


def test_overlapping_writers_have_independent_complete_temporary_documents(tmp_path, monkeypatch):
    target = tmp_path / "checkpoint.json"
    barrier = threading.Barrier(2)
    original = Path.replace
    seen = set()
    def replace(source, destination):
        if source not in seen:
            seen.add(source)
            barrier.wait(timeout=5)
        return original(source, destination)
    monkeypatch.setattr(Path, "replace", replace)
    values = [{"day": day, "rows": [day] * 1000} for day in (13, 14)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda value: stage1_data._write_json(target, value), values))
    assert json.loads(target.read_text()) in values
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_windows_denial_keeps_previous_document_and_is_reported(tmp_path, monkeypatch):
    target = tmp_path / "job.json"
    stage1_data._write_json(target, {"status": "old"})
    calls = []
    def denied(source, destination):
        calls.append(source)
        error = PermissionError("denied")
        error.winerror = 5
        raise error
    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(stage1_data.time, "sleep", lambda duration: None)
    with pytest.raises(PermissionError):
        stage1_data._write_json(target, {"status": "new"})
    assert len(calls) == 8
    assert json.loads(target.read_text()) == {"status": "old"}
    assert not list(tmp_path.glob("*.tmp"))
