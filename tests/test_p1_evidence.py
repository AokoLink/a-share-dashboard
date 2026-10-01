import copy
import json
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from core import daily_data as data, sector_sources, sector_service, sector_relations
from core import execution_evidence as evidence, account_ledger as ledger, forward_validation as fv
from core import store, strategy_service, strategy_lifecycle as life
from core.stage1_data import save_calendar, sha256
from pipeline import forward_validation as forward_report
from tests.test_account_ledger import inputs, settings, DAYS, CODES, fills
from tests.test_daily_pipeline import Sources, NOW


class Response:
    def __init__(self, value):
        self.text = value if isinstance(value, str) else json.dumps(value)
    def raise_for_status(self):
        pass
    def json(self):
        return json.loads(self.text)


@pytest.mark.parametrize("problem", [None, "wrong_symbol", "duplicate", "future", "bad_price"])
def test_benchmark_symbol_dates_and_raw_evidence(problem):
    rows = [["2026-09-29", "10", "11", "12", "9"], ["2026-09-30", "11", "12", "13", "10"]]
    if problem == "duplicate":
        rows[1][0] = rows[0][0]
    elif problem == "future":
        rows[1][0] = "2026-10-01"
    elif problem == "bad_price":
        rows[1][2] = "nan"
    payload = {"data": {"sz000001" if problem == "wrong_symbol" else "sh000001": {"day": rows}}}
    get = lambda *a, **kw: Response(payload)
    if problem:
        with pytest.raises(ValueError):
            data.tencent_benchmark("2026-09-30", get=get)
    else:
        frame = data.tencent_benchmark("2026-09-30", get=get)
        assert frame["date"].iloc[-1] == "2026-09-30"
        assert frame.attrs["raw_response"] == payload and frame.attrs["benchmark_symbol"] == "sh000001"


def test_native_catalog_and_paginated_members_preserve_ids_and_raw_evidence():
    requested = []
    def get(url, params, timeout):
        requested.append((url, params))
        if "eastmoney" in url:
            raise ConnectionError("fixture primary unavailable")
        if "getHQNodeStockCount" in url:
            return Response('"81"')
        if "getHQNodeData" in url:
            numbers = list(range(81))
            if params["asc"] == "0":
                numbers.reverse()
            page = int(params["page"])
            return Response([{"code": str(600000 + n), "symbol": "sh" + str(600000 + n)}
                             for n in numbers[(page - 1) * 80:page * 80]])
        return Response('var boards = {"native_id":"native_id,Board,81"};')
    native = sector_sources.NativeSectors(get=get, clock=lambda: NOW)
    frame, stale = native.catalog()
    assert not stale and frame["category"].tolist() == ["industry", "subindustry", "theme"]
    row = native.membership(frame.iloc[0].to_dict())
    assert row["match_type"] == "source_native" and row["complete"] and len(row["codes"]) == 81
    assert len(row["raw_pages"]) == 6 and len(row["traversals"]) == 2
    assert all(p["node"] == "native_id" for u, p in requested if "getHQNode" in u)


@pytest.mark.parametrize("problem", ["duplicate", "short", "invalid_symbol"])
def test_native_member_failures_never_verify_full_relation(problem):
    counts = iter(['"2"', '"3"' if problem == "count_changed" else '"2"'])
    entries = [{"code": "600001", "symbol": "sh600001"}, {"code": "600002", "symbol": "sh600002"}]
    if problem == "duplicate":
        entries[1] = entries[0]
    if problem == "invalid_symbol":
        entries[1]["symbol"] = "sz999999"
    def get(url, **kw):
        params = kw["params"]
        if "Count" in url:
            return Response(next(counts))
        if params["page"] != "1":
            return Response([])
        return Response(entries[:1] if problem == "short" and params["asc"] == "0" else entries)
    native = sector_sources.NativeSectors(get=get, clock=lambda: NOW)
    row = {"native_id": "test", "provider": "sina", "name": "Board", "expected_members": 2}
    if problem == "invalid_symbol":
        with pytest.raises(ValueError, match="invalid_sector_member"):
            native.membership(row)
    else:
        response = native.membership(row)
        assert not response["ok"] and response["raw_pages"]


def test_stale_sector_counts_use_confirmed_terminal_lists_not_count_truncation():
    codes = ["600001", "600002", "600003"]
    def get(url, params, timeout):
        if "Count" in url:
            return Response('"2"')
        selected = codes if params["asc"] == "1" else list(reversed(codes))
        return Response([{"code": c, "symbol": "sh" + c} for c in selected] if params["page"] == "1" else [])
    native = sector_sources.NativeSectors(get=get, clock=lambda: NOW)
    row = native.membership({"native_id": "test", "provider": "sina", "name": "Board", "expected_members": 1})
    assert row["ok"] and row["complete"] and row["codes"] == codes
    assert row["expected_members"] == 3 and row["reported_counts"] == [1, 2, 2]
    assert row["count_discrepancy"] and row["completion_basis"] == "bidirectional_terminal_enumeration"


@pytest.mark.parametrize("problem", ["ignored_sort", "empty_positive_count"])
def test_terminal_enumeration_rejects_ignored_sort_and_false_empty(problem):
    def get(url, params, timeout):
        if "Count" in url:
            return Response('"2"')
        return Response([{"code": c, "symbol": "sh" + c} for c in ("600001", "600002")]
                        if params["page"] == "1" and problem == "ignored_sort" else [])
    native = sector_sources.NativeSectors(get=get, clock=lambda: NOW)
    with pytest.raises(ValueError, match="sort_not_honored|nonzero_reported_count"):
        native.membership({"native_id": "test", "provider": "sina", "name": "Board", "expected_members": 2})


def test_members_observed_later_are_not_backdated(tmp_path):
    class Source:
        def membership_for(self, row):
            return {"ok": True, "codes": ["600001"], "observed_at": "2026-10-01T10:00:00+08:00",
                    "source": "fixture", "match_type": "source_native", "complete": True}
        def sector_history_for(self, row):
            raise ValueError("missing")
    summary = pd.DataFrame([{"code": "native", "name": "Same", "category": "industry"}])
    snapshot = sector_service.collect("2026-09-30", summary, Source(), pd.DataFrame(columns=["code"]),
        {}, None, [], lambda *a, **kw: {}, lambda *a: {}, workers=1)
    assert snapshot["candidate_universe"]["membership_verified"] == 0
    assert "different_day" in snapshot["rows"][0]["errors"][0]


def test_complete_native_catalog_proves_theme_empty_only_for_covered_industry_code():
    rows = [{"id": "industry:a", "category": "industry", "members": ["600001"],
             "status": "verified_current", "valid_from": "2026-09-30", "valid_to": "2026-09-30"}]
    snapshot = {"as_of": "2026-09-30", "relations": rows, "categories_complete": ["industry", "theme"]}
    known = sector_relations.lookup(snapshot, "600001", "2026-09-30")
    assert known["categories_complete"] == ["industry", "theme"] and known["by_category"]["theme"] == []
    unknown = sector_relations.lookup(snapshot, "920001", "2026-09-30")
    assert unknown["categories_complete"] == [] and unknown["status"] == "unknown"


def proof(tmp_path):
    path = tmp_path / "fixture-source.txt"
    path.write_text("Synthetic evidence for tests only", encoding="utf-8")
    return {"path": str(path), "sha256": sha256(path)}


def rule(code=CODES[0], day=DAYS[1], **changes):
    return {"code": code, "date": day, "verified": True, "source": "test_fixture",
        "known_at": DAYS[0] + "T16:00:00+08:00", "buy_min": 100, "buy_step": 100,
        "sell_min": 100, "sell_step": 100, "odd_lot_sell_all": True,
        "commission_rate": .001, "minimum_commission": 5., "sell_tax_rate": .0005,
        "transfer_rate": .00001, **changes}


def test_import_keeps_source_bytes_and_received_time_no_retroactive_verification(tmp_path):
    row = rule(evidence_refs=[proof(tmp_path)])
    ref = evidence.import_bundle(tmp_path, {"execution_rules": [row]}, now="2026-10-01T10:00:00+08:00")
    saved, refs = evidence.load(tmp_path)
    imported = saved["execution_rules"][0]
    assert data.intact(ref) and len(refs) == 1 and data.intact(imported["evidence_refs"][0])
    assert not ledger._known(imported, life.observation_clock(DAYS[1] + "T09:30:00+08:00"))
    assert ledger._known(imported, life.observation_clock("2026-10-01T11:00:00+08:00"))
    original = row["evidence_refs"][0]["path"]
    from pathlib import Path
    Path(original).write_text("changed original", encoding="utf-8")
    assert evidence.load(tmp_path)[0]["execution_rules"]


@pytest.mark.parametrize("field,value", [("buy_step", 0), ("buy_min", True), ("sell_tax_rate", float("nan")),
                                        ("known_at", "2026-10-02T10:00:00+08:00")])
def test_evidence_invalid_rules_rejected(tmp_path, field, value):
    row = rule(evidence_refs=[proof(tmp_path)], **{field: value})
    with pytest.raises(ValueError):
        evidence.import_bundle(tmp_path, {"execution_rules": [row]}, now="2026-10-01T10:00:00+08:00")
    assert evidence.load(tmp_path)[1] == []


def test_conflicting_import_is_rejected_and_previous_evidence_stays_readable(tmp_path):
    row = rule(evidence_refs=[proof(tmp_path)])
    evidence.import_bundle(tmp_path, {"execution_rules": [row]}, now="2026-10-01T10:00:00+08:00")
    row["buy_min"] = 200
    with pytest.raises(ValueError, match="conflicting"):
        evidence.import_bundle(tmp_path, {"execution_rules": [row]}, now="2026-10-01T11:00:00+08:00")
    assert evidence.load(tmp_path)[0]["execution_rules"][0]["buy_min"] == 100


def test_archived_source_tampering_blocks_account_evidence(tmp_path):
    row = rule(evidence_refs=[proof(tmp_path)])
    evidence.import_bundle(tmp_path, {"execution_rules": [row]}, now="2026-10-01T10:00:00+08:00")
    archived = evidence.load(tmp_path)[0]["execution_rules"][0]["evidence_refs"][0]
    from pathlib import Path
    Path(archived["path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        evidence.load(tmp_path)


@pytest.mark.parametrize("minimum,step,cap,expected", [(200, 1, 199, 0), (200, 1, 203, 203),
                                                     (100, 1, 123, 123), (100, 100, 123, 100)])
def test_account_dated_quantity_rules(minimum, step, cap, expected):
    inp = inputs()
    for day in DAYS:
        inp["execution_rules"][day][CODES[0]] = rule(day=day, buy_min=minimum, buy_step=step,
                                                     sell_min=minimum, sell_step=step)
    inp["trade_status"][DAYS[1]][CODES[0]]["max_buy_shares"] = cap
    report = ledger.replay(**inp, as_of=DAYS[-1], settings=settings(mode="verified_inputs"))
    actual = fills(report, "buy")
    assert (actual[0]["shares"] if actual else 0) == expected
    assert all(abs(r["pnl_reconciliation_error"]) < 1e-6 for r in report["daily"])


def test_minimum_commission_sell_tax_and_cash_reserve_reconcile():
    inp = inputs()
    for day in DAYS:
        inp["execution_rules"][day][CODES[0]] = rule(day=day, commission_rate=0., minimum_commission=500.)
    report = ledger.replay(**inp, as_of=DAYS[-1], settings=settings(max_stock_weight=.8))
    buy, sell = fills(report, "buy")[0], fills(report, "sell")[0]
    assert buy["fee_components"]["commission"] == 500
    assert sell["fee_components"]["sell_tax"] == pytest.approx(sell["gross"] * .0005)
    assert report["cash"] == pytest.approx(100000 - buy["fee"] - sell["fee"])
    assert not report["daily"][1]["risk_breaches"]
    assert sum(report["fee_components"].values()) == pytest.approx(report["fees"])


def test_missing_quantity_rules_strictly_blocks_entry():
    inp = inputs()
    inp["execution_rules"] = {}
    report = ledger.replay(**inp, as_of=DAYS[-1], settings=settings(mode="verified_inputs"))
    assert not fills(report, "buy")
    assert report["metrics"]["total_return"] is None
    assert "order_quantity_and_fee_rules_unknown" in report["evidence_summary"]


def frozen_fixture(tmp_path):
    save_calendar(Sources().calendar(), tmp_path / "calendar.json")
    policy, ref = fv.register(tmp_path, now="2026-09-10T16:00:00+08:00")
    db = str(tmp_path / "market.db")
    store.init_db(db)
    frame = data.normalize_source_daily(Sources().daily("600001", "", NOW.date(), ""), "600001")
    data.archive_frame(tmp_path / "execution" / "raw_daily" / "600001.csv", frame, "test_fixture", NOW.isoformat(), adjust="none")
    result = strategy_service.generate(db, [{"code": "600001", "name": "Test", "amount": 2e8}],
        {"600001": frame}, {"600001": frame}, {}, None, NOW, source_mode="live", replay_root=tmp_path / "replay")
    return db, policy, ref, store.get_strategy_signal(db, result["snapshot_ids"]["breakout"])


def test_prospective_registry_is_immutable_and_live_snapshot_bound_before_results(tmp_path):
    db, policy, ref, snap = frozen_fixture(tmp_path)
    assert policy["validation_start"] == "2026-09-25"
    assert snap["payload"]["forward_protocol_ref"] == ref
    assert fv.eligibility(snap, tmp_path, policy, ref) is None
    later_policy, later_ref = fv.register(tmp_path, now="2026-10-01T10:00:00+08:00")
    assert (later_policy, later_ref) == (policy, ref)
    report = forward_report.run(db, tmp_path, as_of=NOW)
    assert report["accepted_snapshots"] == 3 and not report["formal"]
    assert report["validated_account_return"] is None
    assert report["negative_results_retained"]


@pytest.mark.parametrize("change,expected", [("no_binding", "not_bound"), ("backdate", "not_prospective"),
                                          ("version", "version_changed"), ("content", "content_changed")])
def test_retrospective_or_changed_snapshots_never_become_independent(tmp_path, change, expected):
    _, policy, ref, snap = frozen_fixture(tmp_path)
    if change == "no_binding":
        snap["payload"].pop("forward_protocol_ref")
    elif change == "backdate":
        snap["frozen_at"] = "2026-10-01T10:00:00+08:00"
    elif change == "version":
        snap["version"] = "changed"
    else:
        snap["payload"]["candidates"].append({"code": "600009"})
    assert expected in fv.eligibility(snap, tmp_path, policy, ref)


def test_injected_source_and_embargo_do_not_bind(tmp_path):
    _, policy, ref, snap = frozen_fixture(tmp_path)
    assert fv.binding(tmp_path, "2026-09-30", NOW, "injected_test") is None
    assert fv.binding(tmp_path, "2026-09-15", life.observation_clock("2026-09-15T16:00:00+08:00"), "live") is None
    snap["signal_date"] = "2026-09-15"
    assert "embargo" in fv.eligibility(snap, tmp_path, policy, ref)


def test_changed_evaluation_code_requires_new_registration(tmp_path, monkeypatch):
    db, _, _, _ = frozen_fixture(tmp_path)
    monkeypatch.setattr(fv, "code_identity", lambda: {"changed": True})
    report = forward_report.run(db, tmp_path, as_of=NOW)
    assert report["status"] == "code_changed_requires_new_registration"
    assert report["verified_account"] is None


def test_forward_api_is_read_only_and_rejects_changed_report(tmp_path):
    from flask import Flask
    from web.routes import strategies as routes
    db, _, _, _ = frozen_fixture(tmp_path)
    report = forward_report.run(db, tmp_path, as_of=NOW)
    app = Flask(__name__)
    app.config.update(DB=db, DAILY_PIPELINE_ROOT=str(tmp_path))
    routes.register(app)
    client = app.test_client()
    before = store.monitor_watermark(db)
    result = client.get("/api/forward-validation")
    assert result.status_code == 200 and result.json["data"]["read_only"]
    assert result.json["data"]["accepted_snapshots"] == 3
    assert store.monitor_watermark(db) == before
    from pathlib import Path
    Path(report["report_ref"]["path"]).write_text('{}', encoding="utf-8")
    assert client.get("/api/forward-validation").status_code == 500
