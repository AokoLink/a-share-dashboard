import copy
import datetime as dt

import pandas as pd
import pytest

from core import momentum_strategy as momentum, screening_engine as engine, strategies
from core import research_evaluation, strategy_service, store, signal_replay

DAY = "2026-11-02"


def bars(last=11., previous=10., amount=2e8):
    dates = pd.bdate_range(end=DAY, periods=66).strftime("%Y-%m-%d")
    close = [12.] * 45 + [10.5] * 15 + [previous] * 5 + [last]
    frame = pd.DataFrame({"date": dates, "open": close, "high": [c * 1.01 for c in close],
        "low": [c * .99 for c in close], "close": close, "volume": [1e6] * 65 + [2e6],
        "amount": amount, "turnover": .03})
    frame.loc[65, ["open", "low", "high"]] = [previous, previous * .99, last]
    return frame


def sector(day=DAY, up=.8, median=3., category="industry", status="verified_current", coverage=1.):
    return {"as_of": day, "relations": [{"id": "sector:a", "category": category, "name": "行业A",
        "status": status, "members": ["600001"], "valid_from": day, "valid_to": day}],
        "rows": [{"id": "sector:a", "as_of": day, "breadth": {"status": "ok",
            "active_coverage": coverage, "active_members": 10, "up_ratio": up, "median_change_pct": median}}]}


def select(frame=None, snapshot=None):
    return engine.select(DAY, {"600001": bars() if frame is None else frame},
        [{"code": "600001", "name": "测试"}], sector_snapshot=snapshot)


def test_limit_up_can_be_observed_without_changing_legacy_pool_or_execution_rules():
    result = select(snapshot=sector())
    assert result["eligibility"]["600001"]["reason"] == "limit_up"
    assert result["pool"] == []
    assert result["momentum_eligibility"]["600001"]["eligible"]
    assert len(result["strategy_pools"]["momentum"]) == 1
    signal = result["groups"]["momentum"][0]
    assert signal["momentum_metrics"]["near_limit_up"]
    assert signal["candidate_kind"] == "observation" and not signal["actionable"]
    assert signal["announcement_status"] == "unverified"
    assert signal["momentum_sector"]["status"] == "confirmed"
    assert strategies.execution_check(11., 11.56) == (False, "gap_above_5pct")
    assert strategies.execution_check(11., 10.67) == (False, "gap_below_down_limit")


def test_repair_needs_neither_new_60day_high_nor_panic_state():
    result = select(bars(last=10.8))
    assert result["market_state"] is None and not result["groups"]["breakout"]
    signal = result["groups"]["momentum"][0]
    assert signal["momentum_metrics"]["setup"] == "repair"
    assert signal["momentum_sector"]["status"] == "unknown"
    assert "momentum_sector_unverified" in signal["caveats"]


@pytest.mark.parametrize("snapshot", [sector(day="2026-11-03"), sector(day="2026-10-30"),
    sector(category="theme"), sector(status="unverified"), sector(coverage=.5)])
def test_future_stale_approximate_and_technical_cohorts_never_supply_hot_industry_evidence(snapshot):
    signal = select(snapshot=snapshot)["groups"]["momentum"][0]
    assert signal["momentum_sector"]["status"] == "unknown"
    assert signal["sector_condition"] == "unverified"


def test_known_weak_industry_excludes_while_missing_is_explicit():
    assert not select(snapshot=sector(up=.2, median=-2.))["groups"]["momentum"]
    assert select()["groups"]["momentum"][0]["sector_condition"] == "unverified"


@pytest.mark.parametrize("change", ["turnover", "bad_high", "no_volume", "weak_close"])
def test_limit_up_is_not_a_bypass_for_risk_or_invalid_inputs(change):
    frame = bars()
    if change == "turnover":
        frame.loc[65, "turnover"] = .3
    elif change == "bad_high":
        frame.loc[65, "high"] = 10.
    elif change == "no_volume":
        frame.loc[65, "volume"] = 1e6
    else:
        frame.loc[65, "high"] = 12.
    assert not select(frame)["groups"]["momentum"]


def test_new_observation_does_not_displace_existing_strategy_liquidity_pool():
    data = {"600001": bars(amount=1e10), "600002": bars(last=10.8)}
    result = engine.select(DAY, data, pool_n=1)
    assert [r["code"] for r in result["pool"]] == ["600002"]
    assert {r["code"] for r in result["groups"]["momentum"]} == set(data)
    assert {r["code"] for r in engine.input_pool(result)} == set(data)


def test_future_prices_cannot_change_watch_candidates_or_their_ranking():
    first = select(snapshot=sector())
    frame = bars()
    future = frame.iloc[-1:].assign(date="2026-11-03", close=100., high=100., volume=1e10)
    after = select(pd.concat([frame, future]), sector())
    assert engine.compare(first, after)["status"] == "identical"


def test_independent_control_pool_and_cancelled_gap_are_preserved():
    decision = select(snapshot=sector())
    frame = bars()
    future = pd.DataFrame([{**frame.iloc[-1].to_dict(), "date": d, "open": 12., "close": 12.}
                          for d in ("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")])
    execution = pd.concat([frame, future])
    result = research_evaluation.evaluate_selection(decision, {"600001": execution}, execution.date.tolist())
    assert result["breakout"]["eligible_pool"] == 0
    assert result["momentum"]["eligible_pool"] == 1
    event = result["momentum"]["arms"]["full"]["events"][0]
    assert event["status"] == "cancelled_gap"
    assert result["momentum"]["arms"]["full"]["fixed_weight_net_proxy"] == 0.


def test_watch_candidates_freeze_with_their_own_pool_and_replay(tmp_path):
    db = str(tmp_path / "db.sqlite")
    store.init_db(db)
    frame = bars()
    result = strategy_service.generate(db, [{"code": "600001", "name": "测试"}],
        {"600001": frame}, {"600001": frame}, {}, None,
        dt.datetime.fromisoformat(DAY + "T16:00:00+08:00"), sector_snapshot=sector(),
        replay_root=tmp_path / "replay", source_mode="test_fixture")
    watch = store.get_strategy_signal(db, result["snapshot_ids"]["momentum"])
    old = store.get_strategy_signal(db, result["snapshot_ids"]["breakout"])
    assert len(watch["payload"]["pool"]) == 1 and old["payload"]["pool"] == []
    assert watch["payload"]["candidates"][0]["signal_close"] == 11.
    assert signal_replay.replay(watch["payload"]["replay_ref"], tmp_path / "verify")["identical"]


def test_past_rule_review_keeps_originals_and_never_freezes_or_fetches(tmp_path, monkeypatch):
    from core import daily_data, strategy_lifecycle
    from pipeline.review_momentum_candidates import run as review
    from tests.test_daily_pipeline import runner, NOW
    from web.routes.strategies import candidate_files, read_candidate
    job = runner(tmp_path)
    monkeypatch.setattr(job, "timestamp", lambda: NOW.isoformat())
    job.run()
    originals = [job.work / "run.json", job.root / "candidates" / "2026-09-30.json"]
    before = {p: p.read_bytes() for p in originals}
    snapshots = store.list_strategy_signals(job.db)
    monkeypatch.setattr(job.sources, "daily", lambda *a, **k: pytest.fail("review must be offline"))
    result = review(job.root, "2026-09-30", now=NOW + dt.timedelta(days=1))
    assert all(p.read_bytes() == content for p, content in before.items())
    assert store.list_strategy_signals(job.db) == snapshots
    assert not result["forward_signal"]
    report = signal_replay.verified(result["report_ref"])
    assert report["retrospective"] and not report["actionable"] and report["snapshot_ids"] == {}
    assert set(report["groups"]) == set(strategies.HORIZONS)
    assert signal_replay.verified(report["input_manifest"])["day"] == "2026-09-30"
    assert "core/momentum_strategy.py" in signal_replay.verified(report["code_ref"])
    # Original snapshots stay the default even after a rule change; revisions require an explicit view.
    files = candidate_files(job.root, "2026-10-01")
    assert files[-1][1] == 1 and not read_candidate(files[-1]).get("retrospective")
    monkeypatch.setattr(strategy_lifecycle, "screening_identity", lambda: {"version": "new-rules"})
    files = candidate_files(job.root, "2026-10-01")
    assert files[-1][1] == 1 and not read_candidate(files[-1]).get("retrospective")
    files = candidate_files(job.root, "2026-10-01", view="revised")
    assert files[-1][1] == 3 and read_candidate(files[-1])["retrospective"]
    with pytest.raises(ValueError, match="past_session"):
        review(job.root, "2026-09-30", now=NOW)
    ref = daily_data.read_json(job.work / "run.json")["steps"]["prices"]["result"]["entries"]["600001"]["technical"]["artifact"]
    from pathlib import Path
    Path(ref["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash_mismatch"):
        review(job.root, "2026-09-30", now=NOW + dt.timedelta(days=1))
