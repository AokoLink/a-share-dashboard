import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask

from core import environment, store, strategies
from core.stage1_data import _write_json
from web import util
from web.routes import regime, strategies as strategy_routes


NOW = datetime(2026, 10, 8, 10, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = Flask(__name__)
    db = str(tmp_path / "api.db")
    store.init_db(db)
    app.config.update(DB=db, DAILY_PIPELINE_ROOT=str(tmp_path))
    monkeypatch.setattr(util, "now", lambda: NOW)
    regime.register(app)
    strategy_routes.register(app)
    return app.test_client()


def saved_regime(root, name, day):
    path = root / "runs" / name / "regime.json"
    _write_json(path, {"as_of": day, "label": None, "metrics": {}, "status": "insufficient_daily_history",
                       "observed_sessions": 1, "required_sessions": 61})
    ref = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    _write_json(path.parent / "run.json", {"steps": {"features": {"result": {"regime_artifact": ref}}}})
    return path


def test_primary_environment_reads_verified_daily_report_not_legacy_cache(client, tmp_path, monkeypatch):
    path = saved_regime(tmp_path, "recent", "2026-09-30")
    # Later filesystem write must not make an older data date the current view.
    saved_regime(tmp_path, "older", "2026-09-29")
    saved_regime(tmp_path, "future", "2026-10-09")
    _write_json(tmp_path / "regime_cache.json", {"as_of": "2026-08-14", "label": "震荡"})
    cache_before = (tmp_path / "regime_cache.json").read_bytes()
    _write_json(tmp_path / "market_daily" / "2026-09-30.json", {
        "date": "2026-09-30", "r1": -.001, "limit_up": 57, "turnover": 1e12})
    monkeypatch.setattr(environment, "latest_state", lambda *a, **k: pytest.fail("no legacy calculation"))
    data = client.get("/api/market-environment").json["data"]
    assert data["as_of"] == "2026-09-30"
    assert data["observed_sessions"] == 1 and data["required_sessions"] == 61
    assert data["label"] is None and data["read_only"]
    assert data["saved_observation"]["limit_up"] == 57
    assert data["metrics"] == {}  # One-day observations cannot fill multi-day classification metrics.
    assert (tmp_path / "regime_cache.json").read_bytes() == cache_before
    path.write_text('{}', encoding="utf-8")
    assert client.get("/api/market-environment").status_code == 500


def test_environment_no_saved_report_is_explicit(client):
    data = client.get("/api/market-environment").json["data"]
    assert data["status"] == "not_run" and data["observed_sessions"] == 0


def test_environment_reads_checkpoint_when_sector_step_failed(client, tmp_path):
    path = saved_regime(tmp_path, "partial", "2026-09-30")
    ref = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    _write_json(path.parent / "run.json", {"steps": {"features": {
        "status": "failed", "environment_checkpoint": {"regime_artifact": ref},
        "result": {"status": "failed", "error": "sector failure"}}}})
    response = client.get("/api/market-environment")
    assert response.status_code == 200 and response.json["data"]["observed_sessions"] == 1
    path.write_text('{}', encoding="utf-8")
    assert client.get("/api/market-environment").status_code == 500


def test_environment_reads_independent_step_when_prices_failed(client, tmp_path):
    path = saved_regime(tmp_path, "independent", "2026-09-30")
    ref = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    _write_json(path.parent / "run.json", {"steps": {
        "environment": {"status": "success", "result": {"regime_artifact": ref}},
        "prices": {"status": "failed"}, "features": {"status": "skipped"}}})
    response = client.get("/api/market-environment")
    assert response.status_code == 200
    assert response.json["data"]["observed_sessions"] == 1
    assert response.json["data"]["readiness"]["status"] == "calendar_unavailable"


def test_closed_session_candidates_do_not_promise_generation_at_1505(client, tmp_path, monkeypatch):
    monkeypatch.setattr(util, "now", lambda: NOW.replace(day=10))
    _write_json(tmp_path / "calendar.json", {"first": "2026-10-01", "last": "2026-10-31", "dates": ["2026-10-09"]})
    response = client.get("/api/strategy-candidates")
    assert response.status_code == 200
    assert response.json["data"]["status"] == "market_closed"


def test_legacy_environment_cannot_overwrite_daily_pipeline_cache(client, monkeypatch):
    from web import config
    seen = []
    def legacy(*args, **kwargs):
        seen.append(kwargs["cache_path"])
        return {"label": None, "as_of": "2026-08-14", "metrics": {}}
    monkeypatch.setattr(environment, "latest_state", legacy)
    assert client.get("/api/regime").status_code == 200
    assert seen == [config.REGIME_CACHE + ".legacy.json"]


def test_before_close_candidates_keep_empty_today_but_allow_read_only_history(client, tmp_path):
    path = tmp_path / "candidates" / "2026-09-30.json"
    saved = {"status": "exploratory", "frozen_version": "saved-v1", "data_as_of": "2026-09-30",
        "freeze_status": "skipped_day_changed", "generated_at": "2026-10-01T00:07:16+08:00",
        "cards": strategies.CARDS, "actionable": True,
        "groups": {"breakout": [{"code": "600001", "name": "测试股", "score": 80}], "pullback": [], "rebound": []}}
    _write_json(path, saved)
    before = path.read_bytes()
    data = client.get("/api/strategy-candidates").json["data"]
    assert data["status"] == "waiting_for_close" and all(not v for v in data["groups"].values())
    assert data["previous_report"]["counts"]["breakout"] == 1
    old = client.get(data["previous_report"]["url"]).json
    assert old["data"]["groups"]["breakout"][0]["code"] == "600001"
    assert old["data"]["risk_review"]["status"] == "not_applied_original_archive"
    assert old["data"]["view"] == "original" and old["data"]["read_only"]
    assert old["data"]["strategy_blockers"]["momentum"] == "not_evaluated_in_archive"
    assert "momentum" in old["data"]["cards"]
    assert old["data"]["awaiting_current_close"]
    assert old["meta"]["stale"] and not old["data"]["actionable"]
    assert not store.list_strategy_signals(client.application.config["DB"])
    assert path.read_bytes() == before
    assert client.get("/api/strategy-candidates?history=invalid").status_code == 400


def test_waiting_without_history_is_explained(client):
    data = client.get("/api/strategy-candidates").json["data"]
    assert data["status"] == "waiting_for_close" and data["previous_report"] is None
    assert client.get("/api/strategy-candidates?history=1").json["data"]["status"] == "not_run"
