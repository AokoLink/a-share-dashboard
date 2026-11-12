import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from flask import Flask

from core import candidate_recovery as recovery, daily_data, screening_engine, store
from core.stage1_data import _write_json
from tests.test_daily_pipeline import Sources
from web import util
from web.routes import strategies as routes

DAY = "2026-10-08"
NOW = datetime(2026, 10, 9, 10)


def fixture_root(root):
    days = pd.bdate_range("2025-01-01", "2026-12-31").strftime("%Y-%m-%d").tolist()
    _write_json(root / "calendar.json", {"first": days[0], "last": days[-1], "dates": days})
    path = root / "runs" / "prior" / "universe.json"
    value = {"day": "2026-09-30", "pool": [{"code": "600001", "name": "测试股"}]}
    _write_json(path, value)
    ref = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
           "collected_at": "2026-10-01T00:00:00+08:00"}
    _write_json(path.parent / "run.json", {"steps": {"universe": {"result": {"artifact": ref}}}})
    return path


def client(root, monkeypatch):
    db = str(root / "production.db")
    store.init_db(db)
    app = Flask(__name__)
    app.config.update(DB=db, DAILY_PIPELINE_ROOT=str(root))
    monkeypatch.setattr(util, "now", lambda: NOW)
    routes.register(app)
    return app.test_client()


def test_recovery_collects_target_date_without_live_market_or_database_writes(tmp_path, monkeypatch):
    original = fixture_root(tmp_path)
    before = original.read_bytes()
    source = Sources()
    source.market = lambda: pytest.fail("never use today's market quotes for historical scope")
    result = recovery.run(tmp_path, DAY, workers=1, sources=source, now=NOW)
    report = daily_data.read_json(result["report_ref"]["path"])
    assert report["data_as_of"] == DAY
    assert report["generated_at"][:10] != DAY
    assert report["retrospective"] and not report["forward_signal"] and not report["actionable"]
    assert report["snapshot_ids"] == {} and report["freeze_status"] == "retrospective_not_frozen"
    assert original.read_bytes() == before
    assert not (tmp_path / "candidates" / (DAY + ".json")).exists()
    assert not (tmp_path / "production.db").exists()
    assert result["counts"]["breakout"] == 1
    http = client(tmp_path, monkeypatch)
    missing = http.get("/api/strategy-candidates?date=" + DAY).json["data"]
    assert missing["status"] == "missing_original" and not missing["groups"]["breakout"]
    assert missing["alternative_view"]["view"] == "revised"
    data = http.get(missing["alternative_view"]["url"]).json["data"]
    assert data["retrospective"] and data["groups"]["breakout"][0]["evidence_level"] == "retrospective_research"
    assert data["groups"]["breakout"][0]["earliest_execution"] == "历史复核／补算，不生成订单"
    assert data["view"] == "revised" and data["origin"] == "historical_recovery"
    assert data["groups"]["breakout"][0]["existing_account_holding"] is None
    assert "screening_decision" not in data
    assert not store.list_strategy_signals(http.application.config["DB"])
    waiting = http.get("/api/strategy-candidates").json["data"]
    assert waiting["status"] == "waiting_for_close"
    assert waiting["previous_report"] is None
    assert waiting["available_views"]["revised"] == [DAY]


@pytest.mark.parametrize("day", ["2026-10-09", "2026-10-10"])
def test_recovery_rejects_today_and_future(tmp_path, day):
    fixture_root(tmp_path)
    with pytest.raises(ValueError, match="before_today"):
        recovery.run(tmp_path, day, sources=Sources(), now=NOW)


def test_recovery_rejects_knowledge_after_target_and_tampered_scope(tmp_path):
    path = fixture_root(tmp_path)
    manifest = path.parent / "run.json"
    value = daily_data.read_json(manifest)
    value["steps"]["universe"]["result"]["artifact"]["collected_at"] = "2026-10-09T00:00:00+08:00"
    _write_json(manifest, value)
    with pytest.raises(ValueError, match="not_known"):
        recovery.run(tmp_path, DAY, sources=Sources(), now=NOW)
    path.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError):
        recovery.run(tmp_path, DAY, sources=Sources(), now=NOW)


def test_api_date_lookup_never_substitutes_other_dates(tmp_path, monkeypatch):
    fixture_root(tmp_path)
    http = client(tmp_path, monkeypatch)
    old = {"data_as_of": "2026-09-30", "frozen_version": "prior", "groups": {}, "status": "exploratory"}
    _write_json(tmp_path / "candidates" / "2026-09-30.json", old)
    data = http.get("/api/strategy-candidates?date=" + DAY).json["data"]
    assert data["status"] == "missing_date" and data["requested_date"] == DAY
    assert data["available_dates"] == ["2026-09-30"]
    assert http.get("/api/strategy-candidates?date=2026-10-10").status_code == 400
    assert http.get("/api/strategy-candidates?date=bad").status_code == 400


def test_historical_report_hash_is_checked_even_after_cache_hit(tmp_path, monkeypatch):
    fixture_root(tmp_path)
    result = recovery.run(tmp_path, DAY, workers=1, sources=Sources(), now=NOW)
    http = client(tmp_path, monkeypatch)
    assert http.get("/api/strategy-candidates?view=revised&date=" + DAY).status_code == 200
    Path(result["report_ref"]["path"]).write_text('{}', encoding="utf-8")
    assert http.get("/api/strategy-candidates?view=revised&date=" + DAY).status_code == 500
    assert http.get("/api/strategy-candidates?date=" + DAY).json["data"]["status"] == "missing_original"


def test_recovery_keeps_missing_inputs_and_resume_uses_its_checkpoint(tmp_path):
    fixture_root(tmp_path)
    source = Sources()
    source.fail.add(("600001", "qfq"))
    first = recovery.run(tmp_path, DAY, workers=1, sources=source, now=NOW)
    assert first["status"] == "retrospective_research_partial"
    assert first["scope"]["missing_codes"] == ["600001"]
    source.fail.clear()
    second = recovery.run(tmp_path, DAY, workers=1, sources=source, now=NOW, resume=first["job_id"])
    assert second["counts"]["breakout"] == 1
    assert Path(first["report_ref"]["path"]).exists()


def test_selection_slices_future_bars_out_of_target_day():
    source = Sources()
    frame = daily_data.normalize_source_daily(source.daily("600001", "2025-01-01", DAY, "qfq"), "600001")
    first = screening_engine.select(DAY, {"600001": frame}, [{"code": "600001", "name": "测试股"}])
    future = frame.iloc[-1:].copy()
    future["date"] = "2026-10-09"
    future["close"] = 10000.
    second = screening_engine.select(DAY, {"600001": pd.concat([frame, future], ignore_index=True)}, [{"code": "600001", "name": "测试股"}])
    assert first["decision_sha256"] == second["decision_sha256"]


def test_original_and_revision_are_explicit_and_independent_of_active_rules(tmp_path, monkeypatch):
    from core import strategy_lifecycle, signal_replay
    http = client(tmp_path, monkeypatch)
    normal = {"data_as_of": DAY, "frozen_version": "old", "groups": {}, "version": "old"}
    _write_json(tmp_path / 'candidates' / (DAY + '.json'), normal)
    revised = {**normal, "frozen_version": strategy_lifecycle.screening_identity()['version'],
               "revision_marker": True}
    ref = signal_replay.immutable(tmp_path / 'research_candidates/blobs/revised.json', revised)
    _write_json(tmp_path / 'risk_revision/days' / (DAY + '.json'), ref)
    original = http.get('/api/strategy-candidates?date=' + DAY).json['data']
    assert original['view'] == 'original' and original['source_version'] == 'old'
    assert not original.get('revision_marker')
    assert http.get('/api/strategy-candidates?view=revised&date=' + DAY).json['data']['revision_marker']
    _write_json(tmp_path / 'candidates' / (DAY + '.json'), {**revised, 'revision_marker': False})
    assert not http.get('/api/strategy-candidates?date=' + DAY).json['data']['revision_marker']
    assert http.get('/api/strategy-candidates?view=revised&date=' + DAY).json['data']['revision_marker']
    assert http.get('/api/strategy-candidates?view=invalid&date=' + DAY).status_code == 400


def test_original_preserves_names_scores_and_snapshot_after_pause_or_broken_revision(tmp_path, monkeypatch):
    http = client(tmp_path, monkeypatch)
    snapshot = store.freeze_strategy_signal(http.application.config['DB'], DAY, 'breakout', 'old',
        DAY + 'T16:00:00+08:00', {'candidates': [{'code': '600001', 'name': '原始股', 'score': 90}]})
    report = {'data_as_of': DAY, 'frozen_version': 'old', 'version': 'old',
        'generated_at': DAY + 'T16:00:00+08:00', 'snapshot_ids': {'breakout': snapshot['id']},
        'groups': {'breakout': [{'code': '600001', 'name': '原始股', 'score': 90}]}}
    path = tmp_path / 'candidates' / (DAY + '.json')
    _write_json(path, report)
    before = path.read_bytes()
    store.add_strategy_status_event(http.application.config['DB'], 'breakout', 'old', 'paused',
                                    'later pause', '2026-10-09T10:00:00+08:00')
    _write_json(tmp_path / 'risk_revision/days' / (DAY + '.json'), {'path': 'invalid', 'sha256': 'broken'})
    monkeypatch.setattr(routes.candidate_review, 'apply', lambda *a: pytest.fail('original must never be risk-filtered'))
    out = http.get('/api/strategy-candidates?shadow=1&date=' + DAY).json['data']
    row = out['groups']['breakout'][0]
    assert (row['name'], row['score'], row['snapshot_id']) == ('原始股', 90, snapshot['id'])
    assert row['snapshot_url'] == '/api/strategy-snapshot/' + str(snapshot['id'])
    assert out['risk_review']['status'] == 'not_applied_original_archive'
    assert out['source_generated_at'] == report['generated_at']
    assert out['origin'] == 'original_daily' and not out['actionable']
    assert path.read_bytes() == before
    assert http.get('/api/strategy-candidates?view=revised&date=' + DAY).status_code == 500
