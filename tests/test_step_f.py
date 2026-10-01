import json
from pathlib import Path

import pytest

from core import daily_data, report_cache, signal_replay, store, strategy_lifecycle, strategy_service, version_manifest
from core.stage1_data import _write_json
from pipeline.backup_workspace import backup, restore
from tests.test_daily_pipeline import NOW, Sources, runner


def freeze(tmp_path):
    db = str(tmp_path / "market.db")
    store.init_db(db)
    frame = daily_data.normalize_source_daily(Sources().daily("600001", "", NOW.date(), ""), "600001")
    result = strategy_service.generate(db, [{"code": "600001", "name": "测试股", "amount": 2e8}],
        {"600001": frame}, {"600001": frame}, {}, None, NOW, source_mode="test_fixture",
        replay_root=tmp_path / "daily" / "replay")
    return db, store.get_strategy_signal(db, result["snapshot_ids"]["breakout"])


def test_frozen_inputs_and_archived_code_replay_without_current_function(tmp_path, monkeypatch):
    db, snap = freeze(tmp_path)
    ref = snap["payload"]["replay_ref"]
    monkeypatch.setattr(strategy_service, "generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("live code must not run")))
    result = signal_replay.replay(ref, tmp_path / "work")
    assert result["identical"], result
    assert result["network"] == "disabled"
    assert len(store.list_strategy_signals(db)) == 3
    inputs = signal_replay.verified(signal_replay.verified(ref)["inputs"])
    assert len(inputs["technical"]["600001"]["data"]) == 66


def test_replay_tampering_and_legacy_missing_inputs_are_explicit(tmp_path):
    _, snap = freeze(tmp_path)
    ref = snap["payload"]["replay_ref"]
    bundle = signal_replay.verified(ref)
    Path(bundle["inputs"]["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        signal_replay.replay(ref, tmp_path / "work")
    assert signal_replay.replay(None, tmp_path / "work")["status"].startswith("legacy_missing")


def test_presentation_does_not_change_rule_identity_but_dependencies_do(monkeypatch):
    old = strategy_lifecycle.screening_identity()["version"]
    real = strategy_lifecycle.sha256
    monkeypatch.setattr(strategy_lifecycle, "sha256", lambda path: "page_change" if str(path).endswith("app.js") else real(path))
    assert strategy_lifecycle.screening_identity()["version"] == old
    monkeypatch.setattr(strategy_lifecycle, "sha256", lambda path: "analysis_change" if str(path).endswith("analysis.py") else real(path))
    assert strategy_lifecycle.screening_identity()["version"] != old
    assert set(version_manifest.manifest()["layers"]) == {"screening", "data_features", "execution_evaluation", "application"}


def test_monitor_pages_are_bounded_and_use_cached_summary(tmp_path, monkeypatch):
    from web import create_app
    app = create_app(str(tmp_path / "api.db"))
    app.config["DAILY_PIPELINE_ROOT"] = str(tmp_path / "daily")
    db = app.config["DB"]
    for i in range(5):
        row = store.freeze_strategy_signal(db, f"2026-09-{20+i:02d}", "breakout", "v1", NOW.isoformat(), {"candidates": []})
        store.save_strategy_observation(db, row["id"], "v1", str(i), NOW.isoformat(), {"status": "holding"})
        store.save_strategy_observation(db, row["id"], "v1", "new"+str(i), NOW.isoformat(), {"status": "observed"})
    root = Path(app.config["DAILY_PIPELINE_ROOT"])
    ref = daily_data.archive_json(root / "summary.json", {"versions": [], "watermark": store.monitor_watermark(db)}, "fixture", NOW.isoformat())
    _write_json(root / "monitor" / "latest.json", ref)
    monkeypatch.setattr(store, "list_strategy_signals", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no full history scan")))
    monkeypatch.setattr(store, "list_strategy_observations", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no full history scan")))
    client = app.test_client()
    page1 = client.get("/api/strategy-monitor?limit=2").get_json()["data"]
    assert len(page1["snapshots"]) == len(page1["observations"]) == 2
    assert page1["summary_status"] == "current"
    page2 = client.get("/api/strategy-monitor?limit=2&cursor="+str(page1["next_cursor"])).get_json()["data"]
    assert not set(s["id"] for s in page1["snapshots"]) & set(s["id"] for s in page2["snapshots"])
    assert all(r["payload"]["status"] == "observed" for r in page2["observations"])
    assert client.get("/api/strategy-monitor?date=2026-99-99").status_code == 400
    assert client.get("/api/strategy-monitor?limit=10000").status_code == 400


def test_background_health_has_elapsed_failures_and_preserves_last_success(tmp_path):
    source = Sources()
    job = runner(tmp_path, source)
    success = job.run()
    health = daily_data.read_json(job.root / "health.json")
    assert health["codes"]["600001"]["raw"]["elapsed_seconds"] >= 0
    source.fail.add(("600001", ""))
    job.refresh_prices = True  # 当前缓存完整时不会访问来源；强制刷新验证故障记录。
    failed = job.run()
    health = daily_data.read_json(job.root / "health.json")
    assert health["codes"]["600001"]["raw"]["error"]
    assert health["codes"]["600001"]["raw"]["last_success_date"] == "2026-09-30"
    assert failed["steps"]["prices"]["elapsed_seconds"] >= 0


def test_report_cache_is_copy_safe_and_invalidates_on_change(tmp_path):
    path = tmp_path / "saved.json"
    _write_json(path, {"date": "2026-09-30", "groups": []})
    result = report_cache.read(path)
    result["groups"].append("local")
    assert report_cache.read(path)["groups"] == []
    _write_json(path, {"date": "2026-10-01", "groups": ["changed"]})
    assert report_cache.read(path)["groups"] == ["changed"]
    with pytest.raises(ValueError, match="hash"):
        report_cache.read(path, "wrong")


def test_sqlite_backup_restore_preserves_snapshots_and_replay_relocations(tmp_path):
    db, snap = freeze(tmp_path)
    root = tmp_path / "daily"
    ref = backup(db, root, tmp_path / "backups")
    destination = tmp_path / "restored"
    checked = restore(ref["path"], destination)
    assert checked["database_counts"]["strategy_signal_snapshot"] == 3
    with pytest.raises(ValueError, match="must_be_new"):
        restore(ref["path"], destination)
    mappings = daily_data.read_json(destination / "restore-map.json")
    result = signal_replay.replay(snap["payload"]["replay_ref"], tmp_path / "work", mappings)
    assert result["identical"], result


def test_workflow_page_has_unique_controls_and_explicit_weight_input(tmp_path):
    from web import create_app
    app = create_app(str(tmp_path / "api.db"))
    text = app.test_client().get("/").get_data(as_text=True)
    for marker in ("btn-strategy-freeze", "strategy-candidate-evidence", "portfolio-weights", "btn-monitor-more"):
        assert text.count('id="'+marker+'"') == 1
    client = app.test_client()
    bad = client.post("/api/portfolio-insights", json={"holdings": [{"code": "600001", "weight": .5}], "cash_weight": .7})
    assert bad.status_code == 400
