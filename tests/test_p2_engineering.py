import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from flask import Flask

from core import report_cache, store, version_manifest
from core.stage1_data import _write_json
from web.routes import strategies as routes


@pytest.fixture
def client(tmp_path):
    app = Flask(__name__)
    db = str(tmp_path / "market.db")
    store.init_db(db)
    app.config.update(DB=db, DAILY_PIPELINE_ROOT=str(tmp_path))
    routes.register(app)
    return app.test_client()


def test_collapsed_tools_do_not_hide_any_content_panel():
    page = BeautifulSoup(Path("templates/index.html").read_text(encoding="utf-8"), "html.parser")
    tools = page.select_one("details.research-tools")
    assert tools and len(tools.select("button.tab")) == 7
    for panel in page.select("#sector-view, #workflow-market, [id$='-panel']"):
        assert panel.find_parent("details") is None, panel.get("id")
    scripts = [tag.get("src") for tag in page.select("script[src]")]
    assert scripts.index("/static/request_runtime.js") < scripts.index("/static/app.js")


def test_daily_summary_keeps_counts_and_full_detail_remains_read_only(client, tmp_path):
    code = "600001"
    run_id = str(uuid.uuid4())
    report = {"run_id": run_id, "day": "2026-09-30", "status": "partial",
              "coverage": {"research": {"expected": 3, "codes": [code],
                  "raw": {"current": 2, "missing": [code]}, "technical": {"current": 1}}},
              "stability": {"consecutive_complete_sessions": 0, "days": ["many"]},
              "steps": {"prices": {"status": "partial", "elapsed_seconds": 2,
                  "result": {"entries": [{"code": code}] * 5000}}}}
    path = tmp_path / "runs" / run_id / "run.json"
    _write_json(path, report)
    before = path.read_bytes()
    url = "/api/daily-runs?run_id=" + run_id
    response = client.get(url)
    small = response.json["data"]
    assert small["read_only"] and small["view"] == "summary"
    assert small["steps"]["prices"] == {"status": "partial", "elapsed_seconds": 2}
    assert small["coverage"]["research"]["raw"] == {"current": 2}
    assert small["coverage"]["research"]["expected"] == 3
    full = client.get(url + "&detail=1")
    assert full.json["data"] == report
    assert len(response.data) < len(full.data) / 20
    assert path.read_bytes() == before
    assert client.get(url + "&detail=other").status_code == 400


def test_latest_summary_invalidates_after_background_update(client, tmp_path):
    path = tmp_path / "latest.json"
    _write_json(path, {"status": "partial", "steps": {}})
    assert client.get("/api/daily-runs").json["data"]["status"] == "partial"
    _write_json(path, {"status": "success", "steps": {}})
    assert client.get("/api/daily-runs").json["data"]["status"] == "success"


def test_health_pages_keep_failure_filter_and_count(client, tmp_path):
    codes = {f"{i:06d}": {"raw": {"status": "success", "current": i % 2 == 0,
                                           "error": "past_date" if i % 2 else None}} for i in range(50)}
    _write_json(tmp_path / "health.json", {"status": "partial", "codes": codes})
    first = client.get("/api/pipeline-health?failed=1&limit=7").json["data"]
    second = client.get("/api/pipeline-health?failed=1&limit=7&page=2").json["data"]
    assert first["total_codes"] == second["total_codes"] == 25
    assert len(first["codes"]) == len(second["codes"]) == 7
    assert not first["codes"].keys() & second["codes"].keys()
    assert client.get("/api/pipeline-health?limit=101").status_code == 400
    assert client.get("/api/pipeline-health?page=0").status_code == 400


def test_hash_verification_survives_same_size_same_mtime_tampering(tmp_path):
    path = tmp_path / "signed.json"
    path.write_bytes(b'{"value":1}')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert report_cache.read(path, expected)["value"] == 1
    stat = path.stat()
    path.write_bytes(b'{"value":2}')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(ValueError, match="report_hash_mismatch"):
        report_cache.read(path, expected)


def test_large_reports_cache_only_projection_and_copies_are_isolated(tmp_path, monkeypatch):
    path = tmp_path / "large.json"
    _write_json(path, {"rows": ["large row"] * 5000, "status": "partial"})
    monkeypatch.setattr(report_cache, "ITEM_BYTES", 128)
    monkeypatch.setattr(report_cache, "CACHE_BYTES", 512)
    count = []
    def project(report):
        count.append(1)
        return {"status": report["status"], "rows": []}
    view = report_cache.read(path, project=project, projection_key="small")
    view["rows"].append("local mutation")
    assert report_cache.read(path, project=project, projection_key="small")["rows"] == []
    assert count == [1]
    for i in range(30):
        other = tmp_path / f"{i}.json"
        _write_json(other, {"value": "x" * 50})
        report_cache.read(other)
    info = report_cache.cache_info()
    assert info["serialized_bytes"] <= 512


def test_projection_keys_isolate_pages(tmp_path):
    path = tmp_path / "source.json"
    _write_json(path, {"rows": [1, 2]})
    for i in range(2):
        assert report_cache.read(path, project=lambda data: data["rows"][i],
                                 projection_key=("page", i)) == i + 1
    with pytest.raises(ValueError, match="projection_key_required"):
        report_cache.read(path, project=lambda data: data)


def test_forward_page_detects_current_code_change_without_rewriting_protocol(tmp_path, monkeypatch):
    from core import forward_validation as fv
    from pipeline import forward_validation as forward_report
    from tests.test_p1_evidence import frozen_fixture, NOW
    db, _, _, _ = frozen_fixture(tmp_path)
    report = forward_report.run(db, tmp_path, as_of=NOW)
    app = Flask(__name__)
    app.config.update(DB=db, DAILY_PIPELINE_ROOT=str(tmp_path))
    routes.register(app)
    before = (tmp_path / "forward_validation" / "active.json").read_bytes()
    old_report = Path(report["report_ref"]["path"]).read_bytes()
    monkeypatch.setattr(fv, "code_identity", lambda: {"changed": True})
    response = app.test_client().get("/api/forward-validation")
    assert response.json["meta"]["stale"]
    assert response.json["data"]["status"] == "code_changed_requires_new_registration"
    assert not response.json["data"]["formal"]
    assert not response.json["data"]["identity_matches"]
    assert response.json["data"]["validated_account_return"] is None
    assert (tmp_path / "forward_validation" / "active.json").read_bytes() == before
    assert Path(report["report_ref"]["path"]).read_bytes() == old_report


def test_application_manifest_tracks_request_and_report_helpers():
    files = version_manifest.manifest()["layers"]["application"]["files"]
    for name in ("static/request_runtime.js", "core/report_cache.py", "web/report_views.py"):
        assert name in files
