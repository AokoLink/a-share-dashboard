import copy
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from core import sector_insights as si, sector_relations as sr, sector_service as ss, store
from core.strategy_service import generate
from pipeline.evaluate_strategies import evaluate_day

DAY = "2026-09-30"
DATES = pd.date_range(end=DAY, periods=75, freq="B").strftime("%Y-%m-%d").tolist()
CODES = [f"600{i:03d}" for i in range(1, 16)]


def frame(gain=8):
    closes = [10.] * 65 + [10 * (1 + gain / 100)] * 10
    return pd.DataFrame({"date": DATES, "open": closes, "close": closes,
        "high": [10.1] * 65 + [10.9] * 10, "low": [9.9] * 65 + [10.7] * 10,
        "volume": [100000] * 61 + [200000] * 14, "amount": [2e8] * 75})


def row(code="A", members=None, category="industry", gain=20, match="exact"):
    return {"code": code, "name": code, "category": category,
            "members": members or CODES[:5], "status": "current_snapshot", "match_type": match,
            "source": "verified_test_source", "source_name": code,
            "history_frame": pd.DataFrame({"date": DATES, "close": np.linspace(100, 100 + gain, 75)})}


def snapshot(rows=None, day=DAY, technical=None, spot=None, previous=None):
    spot = spot if spot is not None else pd.DataFrame([{"code": c, "name": c, "price": 10.8,
        "volume": 200000, "amount": (16 - i) * 1e8, "change_pct": -1 if i else 3}
        for i, c in enumerate(CODES)])
    technical = technical if technical is not None else {c: frame() for c in CODES}
    benchmark = pd.DataFrame({"date": DATES, "close": [100.] * 75})
    return ss.assemble(day, rows or [row()], spot, technical, benchmark, DATES,
                       previous=previous, generated_at=day + "T17:00:00+08:00")


def test_dated_relation_revision_is_immutable_and_repeat_idempotent(tmp_path):
    original = snapshot()
    first = sr.save(tmp_path, original)
    repeated = copy.deepcopy(original)
    repeated["generated_at"] = DAY + "T18:00:00+08:00"
    assert sr.save(tmp_path, repeated) == first
    changed = snapshot([row(members=CODES[1:5])])
    second = sr.save(tmp_path, changed)
    assert first["sha256"] != second["sha256"]
    assert sr.lookup(sr.load(tmp_path, DAY, first), CODES[0], DAY)["status"] == "ok"
    assert sr.lookup(sr.load(tmp_path, DAY), CODES[0], DAY)["status"] == "unknown"
    assert sr.lookup(sr.load(tmp_path, DAY, first), CODES[0], "2026-09-29")["relations"] == []
    assert sr.load(tmp_path, "2026-09-29") is None


@pytest.mark.parametrize("match", ["manual", "keyword", "ambiguous"])
def test_approximate_relations_never_become_verified_or_full_breadth(match):
    payload = snapshot([row(match=match)])
    evidence = sr.feature_for(payload, CODES[0], DAY)
    assert evidence["sector_rel20"] is None and evidence["status"] == "unknown"
    assert evidence["uncertain_relations"] and not evidence["relations"]
    assert payload["rows"][0]["breadth"]["up_ratio"] is None
    assert payload["rows"][0]["roles"]["price_leaders"] == []


def test_corrupt_snapshot_is_unavailable(tmp_path):
    from pathlib import Path
    ref = sr.save(tmp_path, snapshot())
    Path(ref["path"]).write_text("corrupt", encoding="utf-8")
    assert sr.load(tmp_path, DAY) is None


def test_feature_aggregation_requires_all_selected_relations_and_separates_categories():
    payload = snapshot([row("A", gain=20), row("B", gain=-20), row("T", category="theme", gain=90)])
    evidence = sr.feature_for(payload, CODES[0], DAY)
    expected = np.mean([r["relative20_pct"] for r in payload["rows"] if r["category"] == "industry"])
    assert evidence["sector_rel20"] == pytest.approx(expected)
    assert set(evidence["selected_sector_ids"]) == {"industry:A", "industry:B"}
    assert len(evidence["by_category"]["theme"]) == 1
    next(r for r in payload["rows"] if r["id"] == "industry:B")["relative"]["as_of"] = "2026-09-29"
    assert sr.feature_for(payload, CODES[0], DAY)["sector_rel20"] is None


def test_full_rotation_universe_and_thresholded_breadth():
    payload = snapshot([row("Slow", gain=40), row("Hot", gain=2), row("Weak", gain=-10)])
    assert payload["candidate_universe"]["total"] == 3
    assert payload["rows"][0]["id"] == "industry:Slow"
    assert payload["rows"][0]["rank20"] == 1
    partial = snapshot([row(members=CODES)], technical={c: frame() for c in CODES[:10]})["rows"][0]
    assert partial["breadth"]["technical_coverage"] == pytest.approx(10 / 15)
    assert partial["breadth"]["above_ma20_ratio"] is None
    assert partial["roles"]["price_universe_valid"] == 10
    sufficient = snapshot([row()], technical={c: frame() for c in CODES[:4]})["rows"][0]
    assert sufficient["breadth"]["technical_status"] == "ok"
    assert sufficient["breadth"]["technical_coverage"] == .8
    assert sufficient["breadth"]["above_ma60_ratio"] == 1


def test_relative_and_member_metrics_use_common_cutoff_and_ignore_future():
    rows = [row()]
    before = snapshot(rows)
    rows[0]["history_frame"] = pd.concat([rows[0]["history_frame"],
        pd.DataFrame({"date": ["2026-10-01"], "close": [999999.]})], ignore_index=True)
    after = snapshot(rows)
    assert before["rows"][0]["relative"] == after["rows"][0]["relative"]
    rows[0]["history_frame"] = rows[0]["history_frame"].drop(index=65)
    missing = snapshot(rows)["rows"][0]
    assert missing["relative"]["returns"]["20"] is None
    assert sr.feature_for(snapshot(rows), CODES[0], DAY)["status"] == "unverified"
    technical = {c: frame() for c in CODES}
    technical[CODES[0]] = technical[CODES[0]].iloc[:-1]
    roles = snapshot(technical=technical)["rows"][0]["roles"]
    assert roles["price_universe_valid"] == 4 and all(x["as_of"] == DAY for x in roles["price_leaders"])


def test_coverage_below_threshold_hides_spot_metrics():
    spot = pd.DataFrame([{"code": c, "price": 10, "volume": 100, "amount": 100, "change_pct": 1}
                         for c in CODES[:3]])
    breadth = snapshot(spot=spot)["rows"][0]["breadth"]
    assert breadth["coverage_ratio"] == .6
    assert breadth["up_ratio"] is None and breadth["top3_amount_share"] is None


def test_all_member_price_leaders_extend_beyond_ten_and_keep_cutoff():
    technical = {c: frame(5) for c in CODES}
    technical[CODES[-1]] = frame(30)
    roles = snapshot([row(members=CODES)], technical=technical)["rows"][0]["roles"]
    assert roles["price_universe_valid"] == 15 and roles["price_leaders"][0]["code"] == CODES[-1]
    assert roles["price_coverage"] == 1 and roles["industrial_core"] == []


def test_background_and_historical_paths_share_sector_condition_and_provenance(tmp_path):
    universe = {CODES[0]: frame()}
    decision = DATES[65]
    days = list(pd.to_datetime(DATES))
    pos = {CODES[0]: {day: i for i, day in enumerate(days)}}
    payload = snapshot(day=decision)
    for value, expected in ((3., True), (-3., False), (None, True)):
        current = copy.deepcopy(payload)
        current["rows"][0]["relative"]["returns"]["20"] = {"excess_pct": value}
        current["rows"][0]["relative"]["as_of"] = decision
        current["snapshot_sha256"] = "test-hash"
        db = str(tmp_path / f"{value}.sqlite")
        store.init_db(db)
        prefix = frame().iloc[:66]
        online = generate(db, [{"code": CODES[0], "name": "测试", "amount": 2e8}],
            {CODES[0]: prefix}, {CODES[0]: prefix}, {CODES[0]: ["A"]}, None,
            dt.datetime.fromisoformat(decision + "T16:00:00"), sector_snapshot=current)
        offline = evaluate_day(universe, pos, days, 65, {}, sector_snapshot=current)["breakout"]
        assert bool(online["groups"]["breakout"]) is expected
        assert bool(offline["ranked_codes"]) is expected
        assert offline["sector_evidence"][CODES[0]]["sector_rel20"] == value
        if expected:
            candidate = online["groups"]["breakout"][0]
            assert candidate["sector_evidence"] == offline["sector_evidence"][CODES[0]]
            assert ("sector_relative_strength_unverified" in candidate["caveats"]) is (value is None)


def test_collector_attempts_all_summary_sectors_and_keeps_failed_rows(tmp_path):
    from core import daily_data
    seen = []
    class Source:
        def membership(self, name):
            seen.append(name)
            if name == "Broken":
                raise ValueError("provider down")
            return {"ok": True, "codes": CODES[:5], "source": "test", "match_type": "exact"}
        def sector_history(self, code):
            return row()["history_frame"], False
    def archive(name, value, source, **meta):
        return daily_data.archive_json(tmp_path / name, value, source, DAY, **meta)
    def archive_frame(r, frame):
        return daily_data.archive_frame(tmp_path / f"{r['code']}.csv", frame, "test", DAY)
    payload = ss.collect(DAY, pd.DataFrame([{"code": str(i), "name": n} for i,n in
        enumerate(("Quiet", "Hot", "Broken"))]), Source(),
        pd.DataFrame([{"code": c, "price": 10, "amount": 100, "volume": 100, "change_pct": 1} for c in CODES]),
        {c: frame() for c in CODES}, pd.DataFrame({"date": DATES, "close": [100.] * 75}),
        DATES, archive, archive_frame)
    assert set(seen) == {"Quiet", "Hot", "Broken"}
    assert payload["candidate_universe"]["total"] == 3
    assert payload["status"] == "partial"
    assert next(r for r in payload["rows"] if r["name"] == "Broken")["errors"]


def test_rotation_and_portfolio_read_same_snapshot_without_membership_fetch(tmp_path, monkeypatch):
    from web import create_app, util
    from core import data_source as ds
    monkeypatch.setattr(ds, "validate_sector_map", lambda: {"ok": True})
    monkeypatch.setattr(util, "today", lambda: DAY)
    monkeypatch.setattr(ds, "resolve_sector_constituents", lambda *a: pytest.fail("page fetched membership"))
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda *a: pytest.fail("page used static map"))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (frame(), False))
    app = create_app(str(tmp_path / "api.sqlite"))
    app.config.update(TESTING=True, DAILY_PIPELINE_ROOT=str(tmp_path / "daily"))
    ref = sr.save(tmp_path / "daily", snapshot([row(), row("T", category="theme")]))
    client = app.test_client()
    matrix = client.get("/api/sector-rotation?top=1").get_json()["data"]
    assert len(matrix["rows"]) == 1 and matrix["candidate_universe"]["total"] == 2
    assert matrix["snapshot_sha256"] == ref["sha256"] and matrix["read_only"]
    queried = client.get(f"/api/sector-relations?code={CODES[0]}").get_json()["data"]
    assert queried["snapshot_sha256"] == ref["sha256"]
    portfolio = client.post("/api/portfolio-insights", json={"holdings": [{"code": CODES[0], "weight": .4},
        {"code": "000001", "weight": .2}]}).get_json()["data"]
    exposure = portfolio["sector_exposure"]
    assert exposure["snapshot_sha256"] == ref["sha256"]
    assert exposure["by_category"]["industry"][0]["weight"] == .4
    assert exposure["by_category"]["theme"][0]["weight"] == .4
    assert exposure["unknown_weight"] == .2
    assert client.get("/api/sector-relations?code=600001&as_of=2026-09-29").get_json()["data"]["status"] == "unknown"
    assert client.get("/api/sector-rotation?as_of=2099-01-01").status_code == 400


def test_rank_change_and_leadership_persistence_require_previous_trading_day():
    previous_day = DATES[-2]
    previous = snapshot([row("A", gain=40), row("B", gain=10)], day=previous_day)
    current = snapshot([row("A", gain=2), row("B", gain=50)], previous=previous)
    assert next(r for r in current["rows"] if r["id"] == "industry:B")["rank20_change"] == 1
    assert all(r["leadership_persistence_sessions"] == 2 for r in current["rows"])
    previous["as_of"] = DATES[-3]
    missed = snapshot([row("A", gain=2), row("B", gain=50)], previous=previous)
    assert all(r["rank20_change"] is None and r["leadership_persistence_sessions"] == 1 for r in missed["rows"])


def test_frozen_snapshot_retains_original_sector_revision(tmp_path):
    day = DATES[65]
    original = snapshot(day=day)
    ref = sr.save(tmp_path, original)
    original = sr.load(tmp_path, day, ref)
    db = str(tmp_path / "signals.sqlite")
    store.init_db(db)
    prefix = frame().iloc[:66]
    result = generate(db, [{"code": CODES[0], "name": "测试", "amount": 2e8}],
        {CODES[0]: prefix}, {CODES[0]: prefix}, {CODES[0]: ["未核实静态题材"]}, None,
        dt.datetime.fromisoformat(day + "T16:00:00"), sector_snapshot=original, sector_snapshot_ref=ref)
    saved = store.get_strategy_signal(db, result["snapshot_ids"]["breakout"])
    assert saved["payload"]["sector_snapshot_ref"] == ref
    assert saved["payload"]["pool"][0]["sectors"] == ["A"]
    sr.save(tmp_path, snapshot([row(members=CODES[1:5])], day=day))
    assert store.get_strategy_signal(db, saved["id"]) == saved
    assert sr.lookup(sr.load(tmp_path, day, saved["payload"]["sector_snapshot_ref"]), CODES[0], day)["status"] == "ok"
