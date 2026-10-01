import json

import pandas as pd
import pytest

from core.sw_sector_sources import SwSectors
from core import sector_sources, sector_relations, sector_service
from pipeline.collect_sector_history import normalize
from tests.test_daily_pipeline import NOW


class Response:
    def __init__(self, item):
        self.text = json.dumps(item)
    def json(self):
        return json.loads(self.text)
    def raise_for_status(self):
        pass


def pages(rows, total=None, next_page=None):
    return Response({"code": "200", "data": {"count": len(rows) if total is None else total,
                                              "results": rows, "next": next_page}})


@pytest.mark.parametrize("problem", [None, "duplicate", "changed", "future_effective"])
def test_sw_membership_requires_two_complete_enumerations(problem):
    members = [{"stockcode": "600001", "beginningdate": "2025-01-01T08:00:00+08:00"},
               {"stockcode": "600002", "beginningdate": "2025-01-01T08:00:00+08:00"}]
    calls = []
    def get(url, **kw):
        calls.append(kw)
        rows = [dict(r) for r in members]
        if problem == "duplicate":
            rows[1] = rows[0]
        elif problem == "changed" and len(calls) == 2:
            rows[1]["stockcode"] = "600003"
        elif problem == "future_effective":
            rows[0]["beginningdate"] = "2026-10-08T08:00:00+08:00"
        return pages(rows)
    source = SwSectors(get=get, clock=lambda: NOW)
    row = {"native_id": "801010", "name": "SW"}
    if problem:
        with pytest.raises(ValueError):
            source.membership(row)
    else:
        result = source.membership(row)
        assert result["codes"] == ["600001", "600002"] and result["complete"]
        assert len(result["raw_pages"]) == 2 and all(c["params"]["swindexcode"] == "801010" for c in calls)


@pytest.mark.parametrize("problem", ["short", "missing_next", "extra_next", "changed_count"])
def test_sw_pagination_rejects_missing_and_changing_rows(problem):
    def get(url, **kw):
        page = kw["params"]["page"]
        total = 201 if problem != "extra_next" else 1
        if problem == "changed_count" and page == 2:
            total = 202
        n = 1 if total == 1 else 200 if page == 1 else 1
        if problem == "short":
            n -= 1
        nxt = "next" if page == 1 or problem == "extra_next" else None
        return pages([{}] * n, total, None if problem == "missing_next" else nxt)
    with pytest.raises(ValueError):
        SwSectors(get=get).pages("fixture/")


@pytest.mark.parametrize("problem", [None, "identity", "duplicate", "future", "bad_price"])
def test_sw_history_is_native_ohlc_with_date_and_identity_checks(problem):
    values = [{"swindexcode": "801010", "bargaindate": day, "openindex": 100, "maxindex": 102,
               "minindex": 99, "closeindex": 101, "bargainamount": 1, "bargainsum": 2}
              for day in ("2026-09-29", "2026-09-30")]
    if problem == "identity":
        values[0]["swindexcode"] = "801030"
    elif problem == "duplicate":
        values[1]["bargaindate"] = values[0]["bargaindate"]
    elif problem == "future":
        values[1]["bargaindate"] = "2026-10-08"
    elif problem == "bad_price":
        values[1]["closeindex"] = 500
    source = SwSectors(get=lambda *a, **kw: Response({"code": "200", "data": values}), clock=lambda: NOW)
    if problem:
        with pytest.raises(ValueError):
            source.history({"native_id": "801010"})
    else:
        frame, stale = source.history({"native_id": "801010"})
        assert not stale and frame.attrs["raw_response"] and frame["date"].iloc[-1] == "2026-09-30"
        assert "volume_native" in frame and "volume" not in frame


def test_same_source_members_and_index_enter_feature_without_name_join():
    from tests.test_sector_service import DATES
    members = {"code": "sw_801010", "name": "SW", "category": "industry", "taxonomy": "sw_native_index",
               "status": "current_snapshot", "match_type": "source_native", "members": ["600001"],
               "history_frame": pd.DataFrame({"date": DATES, "close": range(100, 175)})}
    snapshot = sector_service.assemble("2026-09-30", [members], pd.DataFrame(columns=["code"]), {},
                                      pd.DataFrame({"date": DATES, "close": [100] * len(DATES)}), DATES)
    feature = sector_relations.feature_for(snapshot, "600001", "2026-09-30")
    assert feature["status"] == "ok" and feature["sector_rel20"] > 0
    assert feature["selected_sector_ids"] == ["industry:sw_801010"]
    assert sector_relations.feature_for(snapshot, "600001", "2026-09-29")["status"] == "unknown"


def test_observed_native_reference_preserves_holiday_date_mismatch_and_dependency_hash(tmp_path):
    from core import daily_data as data, sector_index_sources
    from pipeline.collect_sector_indices import observed_reference
    from tests.test_sector_service import DATES
    history = pd.DataFrame({"date": DATES, "close": range(100, 175)})
    original = data.archive_json(tmp_path / "raw.json", {"source": "fixture"}, "sw", NOW.isoformat())
    ref = data.archive_frame(tmp_path / "prices.csv", history, "sw", NOW.isoformat(), dependencies=[original])
    row = {"code": "sw_801010", "name": "SW", "category": "industry", "taxonomy": "sw_native_index",
           "observed_at": "2026-10-01T10:00:00+08:00", "history": ref, "membership_status": "verified_current"}
    snapshot = {"as_of": "2026-10-01", "rows": [row]}
    reference = observed_reference(tmp_path, snapshot, pd.DataFrame({"date": DATES, "close": [100] * len(DATES)}), DATES)
    report = sector_index_sources.load(tmp_path, provider="sw")
    assert report["as_of"] == "2026-09-30" and report["membership_observation_day"] == "2026-10-01"
    assert report["rows"][0]["feature_binding_status"] == "member_price_date_mismatch"
    assert report["rows"][0]["relative20_pct"] > 0 and report["report_ref"] == reference
    assert "relative" not in snapshot["rows"][0]
    from pathlib import Path
    Path(original["path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="dependency_changed"):
        sector_index_sources.load(tmp_path, provider="sw")


def test_sw_and_sina_catalog_keep_native_integer_counts_in_mixed_frame():
    def get(url, **kw):
        if "swsresearch" in url:
            category = kw["params"]["indextype"]
            return pages([{"swindexcode": "801010" if category.startswith("一") else "801012", "swindexname": "SW"}])
        if "eastmoney" in url:
            raise ConnectionError("offline")
        return Response(None) if "Count" in url else type("R", (), {"text": 'var boards={"test":"test,Theme,2"};',
                                                                         "raise_for_status": lambda self: None})()
    source = sector_sources.NativeSectors(get=get, clock=lambda: NOW)
    frame, _ = source.catalog()
    assert frame["provider"].tolist() == ["sw", "sw", "sina"]
    assert type(frame.iloc[-1]["expected_members"]) is int


@pytest.mark.parametrize("problem", [None, "future", "invalid_code", "missing_column"])
def test_classification_history_preserves_source_times_without_invented_known_at(problem):
    frame = pd.DataFrame({"股票代码": ["000001"], "计入日期": ["2021-12-13"], "行业代码": ["480101"],
                          "更新日期": ["2024-09-27 09:08:00"]})
    if problem == "future":
        frame.loc[0, "更新日期"] = "2026-10-08"
    elif problem == "invalid_code":
        frame.loc[0, "股票代码"] = "1"
    elif problem == "missing_column":
        frame = frame.drop(columns="计入日期")
    if problem:
        with pytest.raises(ValueError):
            normalize(frame, NOW)
    else:
        result = normalize(frame, NOW)
        assert result["code"].iloc[0] == "000001" and "known_at" not in result
        assert result["updated_at_source"].iloc[0] == "2024-09-27T09:08:00"
