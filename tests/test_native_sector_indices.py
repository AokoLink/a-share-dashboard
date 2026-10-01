import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

from core import daily_data as data, sector_index_sources as native, sector_relations
from core.stage1_data import _write_json, sha256


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone(timedelta(hours=8)))


def response(rows, code="881272", year=2026):
    return f"quotebridge_v4_line_bk_{code}_01_{year}(" + json.dumps({"data": ";".join(rows)}) + ");"


@pytest.mark.parametrize("problem", ["identity", "year", "duplicate", "reverse", "nan", "ohlc", "volume", "fields", "suffix"])
def test_native_index_rejects_wrong_identity_and_invalid_prices(problem):
    rows = ["20260929,10,12,9,11,100,1000", "20260930,11,13,10,12,200,2000"]
    if problem == "year":
        rows[0] = rows[0].replace("20260929", "20250929")
    elif problem == "duplicate":
        rows[1] = rows[1].replace("20260930", "20260929")
    elif problem == "reverse":
        rows.reverse()
    elif problem == "nan":
        rows[1] = rows[1].replace(",12,200", ",NaN,200")
    elif problem == "ohlc":
        rows[1] = rows[1].replace(",13,10,12", ",8,10,12")
    elif problem == "volume":
        rows[1] = rows[1].replace(",200,", ",-200,")
    elif problem == "fields":
        rows[1] = "20260930,11,13"
    raw = response(rows, code="881111" if problem == "identity" else "881272")
    if problem == "suffix":
        raw += "untrusted_function();"
    with pytest.raises(ValueError):
        native.ThsIndices.decode(raw, "881272", 2026)


def test_native_history_keeps_raw_responses_and_filters_to_requested_cutoff():
    requested = []
    def get(url, **kwargs):
        requested.append(url)
        year = 2025 if "2025.js" in url else 2026
        class Result:
            text = response(["20250901,10,12,9,11,100,1000"] if year == 2025 else
                            ["20260929,10,12,9,11,100,1000", "20260930,11,13,10,12,200,2000"], year=year)
            def raise_for_status(self):
                pass
        return Result()
    source = native.ThsIndices(get=get, clock=lambda: NOW)
    frame, raw = source.history({"native_id": "881272"}, "2026-09-29")
    assert frame["date"].tolist() == ["2025-09-01", "2026-09-29"]
    assert len(raw) == 2 and all(r["raw_response"] for r in raw)
    assert "volume_native" in frame and "volume" not in frame
    with pytest.raises(ValueError, match="future"):
        source.history({"native_id": "881272"}, "2026-10-02")
    assert len(requested) == 2


@pytest.mark.parametrize("links", ["", '<a href="http://q.10jqka.com.cn/thshy/detail/code/881272/">A</a>' * 2,
                                   '<a href="https://other.invalid/881272">A</a>'])
def test_catalog_rejects_empty_duplicate_and_non_native_ids(links):
    class Result:
        text = f'<div class="cate_inner">{links}</div>'
        def raise_for_status(self):
            pass
    with pytest.raises(ValueError):
        native.ThsIndices(get=lambda *a, **kw: Result()).catalog()


def archived(tmp_path):
    base = tmp_path / "sector_indices" / "runs" / "fixture"
    raw = data.archive_json(base / "response.json", {"raw_response": "fixture"}, "ths", NOW.isoformat())
    prices = data.archive_frame(base / "history.csv", pd.DataFrame({"date": ["2026-09-30"], "close": [100]}),
                               "ths", NOW.isoformat(), dependencies=[raw])
    report = {"as_of": "2026-09-30", "status": "complete_prices", "total": 3, "current": 3,
              "rows": [{"code": f"ths_88127{i}", "name": f"Native{i}", "history": prices,
                        "relative": {"returns": {"20": {"excess_pct": gain}}},
                        "membership_status": "not_collected_no_cross_provider_join"}
                       for i, gain in enumerate((1, 7, 3))]}
    _write_json(base / "report.json", report)
    ref = {"path": str(base / "report.json"), "sha256": sha256(base / "report.json")}
    _write_json(tmp_path / "sector_indices" / "latest.json", ref)
    return ref, raw


def test_native_api_ranks_full_universe_and_checks_dependencies_without_fetch(tmp_path, monkeypatch):
    from web import create_app, util
    from core import data_source
    from core.stage1_data import save_calendar
    monkeypatch.setattr(data_source, "validate_sector_map", lambda: {"ok": True})
    monkeypatch.setattr(native.ThsIndices, "request", lambda *a: pytest.fail("read-only route fetched source"))
    ref, raw = archived(tmp_path / "daily")
    save_calendar(pd.DataFrame({"trade_date": ["2026-09-29", "2026-09-30", "2026-10-08"]}),
                  tmp_path / "daily" / "calendar.json")
    monkeypatch.setattr(util, "now", lambda: NOW)
    app = create_app(str(tmp_path / "market.sqlite"))
    app.config.update(TESTING=True, DAILY_PIPELINE_ROOT=str(tmp_path / "daily"))
    client = app.test_client()
    result = client.get("/api/sector-index-rotation?top=1").get_json()["data"]
    assert result["total"] == 3 and len(result["rows"]) == 1
    assert result["rows"][0]["rank20"] == 1 and result["rows"][0]["code"] == "ths_881271"
    assert result["report_ref"] == ref and result["read_only"]
    assert result["freshness"] == {"status": "current", "expected_closed_session": "2026-09-30"}
    assert result["rows"][0]["membership_status"] == "not_collected_no_cross_provider_join"
    assert sector_relations.load(tmp_path / "daily", "2026-09-30") is None
    assert client.get("/api/sector-index-rotation?as_of=2026-09-29").get_json()["data"]["status"] == "not_run"
    assert client.get("/api/sector-index-rotation?as_of=2099-01-01").status_code == 400
    monkeypatch.setattr(util, "now", lambda: NOW.replace(day=8, hour=16))
    assert client.get("/api/sector-index-rotation").get_json()["meta"]["stale"]
    Path(raw["path"]).write_text("tampered", encoding="utf-8")
    assert client.get("/api/sector-index-rotation").status_code == 500


def test_parallel_taxonomy_and_inactive_scope_survive_relation_lookup():
    row = {"code": "sina_hangye_ZB06", "name": "Detail", "category": "subindustry", "members": ["600001"],
           "status": "current_snapshot", "match_type": "source_native", "valid_from": "2026-09-30",
           "valid_to": "2026-09-30", "taxonomy": "sina_industry_detail", "parent_mapping": "not_asserted",
           "membership_scope": "provider_members_including_inactive_not_current_tradeable_universe",
           "completion_basis": "bidirectional_terminal_enumeration", "count_discrepancy": True}
    relation = sector_relations.relation(row, "2026-09-30")
    result = sector_relations.lookup({"as_of": "2026-09-30", "relations": [relation]}, "600001", "2026-09-30")
    saved = result["by_category"]["subindustry"][0]
    assert saved["taxonomy"] == "sina_industry_detail" and saved["parent_mapping"] == "not_asserted"
    assert saved["membership_scope"] == row["membership_scope"] and saved["count_discrepancy"]


def test_daily_native_prices_keep_member_evidence_separate_and_use_existing_lock(tmp_path):
    from tests.test_daily_pipeline import Sources, runner
    from core.daily_runner import RunLock, AlreadyRunning
    class Source(Sources):
        def native_index_reference(self, day, root, workers, benchmark):
            with pytest.raises(AlreadyRunning):
                with RunLock(root):
                    pass
            assert day == "2026-09-30" and benchmark is not None
            return {"status": "complete_prices", "total": 90, "current": 90, "as_of": day}
    job = runner(tmp_path, Source())
    result = job.run()
    feature = result["steps"]["features"]["result"]
    assert feature["native_index_reference"]["current"] == 90
    assert feature["sector_coverage"]["total"] == 1
    assert feature["membership_coverage"] == 1
