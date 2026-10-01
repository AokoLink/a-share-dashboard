import copy
import datetime as dt
import json

import pandas as pd
import pytest

from core import screening_engine as engine, research_evaluation as ev, store, strategies
from core.strategy_service import generate
from pipeline.evaluate_strategies import evaluate_day
from pipeline.strategy_experiments import run as experiments

DATES = pd.date_range("2026-01-02", periods=80, freq="B").strftime("%Y-%m-%d").tolist()
DAY = DATES[65]


def frame(amount=2e8, breakout=True):
    close = [10.] * 65 + ([10.8] * 15 if breakout else [10.] * 15)
    return pd.DataFrame({"date": DATES, "open": close, "high": [10.1] * 65 + [10.9] * 15,
        "low": [9.9] * 65 + [10.7] * 15, "close": close,
        "volume": [100000] * 61 + [200000] * 19, "amount": [amount] * 80})


def test_eligibility_precedes_pool_cutoff_and_ties_have_fixed_order():
    universe = {"600001": frame(4e8).iloc[-20:], "600003": frame(), "600002": frame()}
    decision = engine.select(DAY, universe, pool_n=2, top_n=1)
    assert decision["eligibility"]["600001"]["reason"] == "history_under_61"
    assert [r["code"] for r in decision["pool"]] == ["600002", "600003"]
    assert decision["groups"]["breakout"][0]["code"] == "600002"
    scope, _ = engine.acquisition_scope([{"code": c, "amount": 2e8} for c in universe])
    assert len(scope) == 3  # 不在采集资格前限制为池大小


def test_shared_online_historical_full_decision_and_future_invariance(tmp_path):
    universe = {f"600{i:03d}": frame(2e8 + i) for i in range(1, 204)}
    universe["600203"] = frame(1e10).iloc[60:]
    rows = [{"code": c, "name": "测试", "amount": 2e8} for c in universe]
    prefix = {c: f[f["date"] <= DAY] for c,f in universe.items()}
    db = str(tmp_path / "signals.sqlite")
    store.init_db(db)
    online = generate(db, rows, prefix, prefix, {}, None, dt.datetime.fromisoformat(DAY + "T16:00:00"),
                      allow_freeze=False)["screening_decision"]
    pos = {c: {d:i for i,d in enumerate(f["date"])} for c,f in universe.items()}
    offline = evaluate_day(universe, pos, DATES, 65, {}, rows=rows)["breakout"]["screening_decision"]
    assert engine.compare(online, offline)["status"] == "identical"
    assert len(online["pool"]) == 200 and online["eligibility"]["600203"]["reason"] == "history_under_61"
    changed = {c: f.copy() for c,f in universe.items()}
    for f in changed.values():
        f.loc[f["date"] > DAY, ["open", "high", "close", "amount"]] = 99999999.
    assert engine.compare(online, engine.select(DAY, changed, rows))["status"] == "identical"
    assert not store.list_strategy_signals(db)


def test_missing_inputs_have_explicit_difference_report():
    first = engine.select(DAY, {"600001": frame()}, rows=[{"code": "600001", "name": "测试"}])
    missing = engine.select(DAY, {}, rows=[{"code": "600001", "name": "测试"}])
    report = engine.compare(first, missing)
    assert report["status"] == "different" and report["missing_right"] == ["600001"]
    assert missing["eligibility"]["600001"]["reason"] == "missing_daily"


def test_invalid_history_does_not_consume_a_pool_slot():
    bad = frame(1e10)
    bad.loc[65, "high"] = float("nan")
    result = engine.select(DAY, {"600001": bad, "600002": frame()}, pool_n=1)
    assert result["eligibility"]["600001"]["reason"] == "invalid_history"
    assert result["pool"][0]["code"] == "600002"


def test_historical_name_after_decision_day_is_not_used():
    result = engine.select(DAY, {"600001": frame()}, [{"code": "600001", "name": "ST未来", "name_as_of": "2099-01-01"}])
    assert result["pool"][0]["name"] is None
    assert result["groups"]["breakout"][0]["caveats"][0] == "historical_st_unknown"


def test_unknown_variants_fail_and_single_condition_removal_is_identified():
    with pytest.raises(ValueError, match="unknown_variant"):
        engine.select(DAY, {"600001": frame()}, variant="optimized")
    data = frame()
    data["volume"] = 100000
    assert not strategies.screen(data.iloc[:66], "600001")["signals"]
    removed = strategies.screen(data.iloc[:66], "600001", variant="breakout_no_volume")
    assert removed["signals"][0]["variant"] == "breakout_no_volume"
    assert "近5日成交参与放大" not in removed["signals"][0]["reasons"]


def test_missing_exit_retained_and_not_reallocated_as_cash():
    universe = {"600001": frame(), "600002": frame()}
    universe["600001"].loc[71, "open"] = 12.
    universe["600002"] = universe["600002"].drop(index=71)
    decision = engine.select(DAY, universe)
    result = ev.evaluate_selection(decision, universe, DATES)["breakout"]
    assert result["arms"]["full"]["selected"] == 2
    assert result["arms"]["full"]["status_counts"]["missing_exit_price"] == 1
    assert result["arms"]["full"]["conditional_net"] is not None
    assert result["arms"]["full"]["fixed_weight_net_proxy"] is None


def test_gap_cancelled_slot_retains_cash_and_equal_controls_have_zero_excess():
    universe = {"600001": frame(), "600002": frame()}
    universe["600001"].loc[71, "open"] = 12.
    universe["600002"].loc[66, "open"] = 12.
    decision = engine.select(DAY, universe)
    result = ev.evaluate_selection(decision, universe, DATES)["breakout"]
    full = result["arms"]["full"]
    assert full["fixed_weight_net_proxy"] == pytest.approx(full["conditional_net"] / 2)
    assert result["fixed_weight_net_arms"]["full"] == result["fixed_weight_net_arms"]["market_equal"]
    assert full["status_counts"] == {"completed": 1, "cancelled_gap": 1}


def test_pending_horizon_and_future_execution_do_not_create_completed_return():
    selected = engine.select(DAY, {"600001": frame()})
    result = ev.evaluate_selection(selected, {"600001": frame()}, DATES, as_of=DATES[66])["breakout"]
    assert result["arms"]["full"]["status_counts"] == {"holding": 1}
    assert result["full"] is None and result["arms"]["full"]["fixed_weight_net_proxy"] is None


def test_random_controls_are_selected_before_missing_execution_is_known():
    universe = {"600001": frame(), "600002": frame(), "600003": frame()}
    decision = engine.select(DAY, universe, top_n=1)
    relation = {"id": "industry:A"}
    for c in universe:
        decision["sector_evidence"][c]["relations"] = [relation]
    before = ev.evaluate_selection(decision, universe, DATES)["breakout"]
    universe["600002"] = universe["600002"].drop(index=71)
    after = ev.evaluate_selection(decision, universe, DATES)["breakout"]
    assert before["arms"]["sector_random"]["sampled_codes"] == after["arms"]["sector_random"]["sampled_codes"]
    assert after["arms"]["sector_random"]["status_counts"]["missing_exit_price"] > 0
    assert after["arms"]["sector_random"]["fixed_weight_net_proxy"] is None


def test_label_end_purge_and_monthly_validation_windows():
    records = [{"date": "2026-09-20", "label_end": "2026-09-25"},
               {"date": "2026-09-29", "label_end": "2026-10-08"},
               {"date": "2026-10-02", "label_end": "2026-10-12"},
               {"date": "2026-10-30", "label_end": "2026-11-10"}]
    calendar = pd.date_range("2026-09-01", "2026-11-30", freq="B").strftime("%Y-%m-%d").tolist()
    split = ev.purged_split(records, "2026-10-01", 0, calendar)
    assert split["development"] == records[:1]
    assert split["purged"] == records[1:2]
    rolling = ev.rolling_validation(records, "2026-10-01", calendar, 0)[0]
    assert rolling["validation"] == records[2:3]
    assert rolling["incomplete_validation_labels"] == records[3:4]


def test_registered_experiment_preserves_protocol_and_negative_or_missing_results(tmp_path):
    report = experiments(DAY, DATES[67], step=2, output_root=tmp_path,
        universe={"600001": frame(), "600002": frame(breakout=False)}, regimes={DAY: "震荡"}, calendar=DATES)
    registry = json.loads(open(report["protocol"]["path"], encoding="utf-8").read())
    assert registry["registered_at"] <= report["generated_at"]
    assert registry["development"]["previously_examined"]
    assert len(report["daily"]) == len(strategies.VARIANTS)
    assert report["allocation_status"] == "blocked_publication_date_fundamentals"
    assert report["validation_split"]["breakout"]["validation"] == []
    assert report["contrasts"]["breakout_no_volume"]["pairs"]
    assert report["summary"]["base"]["breakout"]["account_return"] is None
    assert report["source_mode"] == "injected_test"
    from pathlib import Path
    from pipeline.review_strategy_experiment import review
    reviewed = review(Path(tmp_path) / "runs" / report["run_id"] / "report.json")
    assert reviewed["contrasts"]["breakout_no_sector"]["status"] == "blocked_feature_coverage"
    assert reviewed["contrasts"]["breakout_no_sector"]["paired_mean"] is None
    assert reviewed["status"] == "no_upgrade_evidence"
