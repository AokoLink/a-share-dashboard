import datetime
import json

import pandas as pd
import pytest

import app as app_mod
from core import data_source as ds, environment as env, store, strategy_lifecycle as life
from core.stage1_data import sha256
from web import config


def _snapshot(snapshot_id=1):
    payload = {"horizon_sessions": 2, "membership_basis": "captured_now",
               "quality": {"daily_failed": 0},
               "pool": [
                   {"code": "600001", "signal_date": "2025-01-02", "signal_close": 10,
                    "sectors": ["A"]},
                   {"code": "600002", "signal_date": "2025-01-02", "signal_close": 20,
                    "sectors": ["A"]},
                   {"code": "600003", "signal_date": "2025-01-02", "signal_close": 30,
                    "sectors": ["B"]}],
               "candidates": [{"code": "sh600001"}, {"code": "sh600002"}]}
    return {"id": snapshot_id, "signal_date": "2025-01-02", "strategy": "breakout",
            "version": "v1", "content_sha256": "abc", "payload": payload}


def test_snapshot_is_immutable_and_revision_is_separate(tmp_path):
    db = str(tmp_path / "signals.db")
    store.init_db(db)
    first = store.freeze_strategy_signal(db, "2025-01-02", "breakout", "v1", "15:10",
                                         {"candidates": ["600001"]})
    again = store.freeze_strategy_signal(db, "2025-01-02", "breakout", "v1", "15:30",
                                         {"candidates": ["600001"]})
    revised = store.freeze_strategy_signal(db, "2025-01-02", "breakout", "v1", "16:00",
                                           {"candidates": ["600002"]})
    rows = store.list_strategy_signals(db)
    assert first == again and revised["id"] != first["id"]
    assert len(rows) == 2
    assert store.get_strategy_signal(db, first["id"])["payload"] == {"candidates": ["600001"]}
    assert store.get_strategy_signal(db, first["id"])["frozen_at"] == "15:10"


def test_tracker_checks_raw_source_integrity(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    csv = raw / "600001.csv"
    csv.write_text("date,open,close\n2025-01-02,10,10\n", encoding="utf-8")
    csv.with_suffix(".json").write_text(json.dumps({"sha256": sha256(csv),
                                                    "adjust": "none"}), encoding="utf-8")
    bars, basis = life.load_bars("600001", raw, tmp_path / "legacy")
    assert basis == "verified_unadjusted_raw" and bars["2025-01-02"]["close"] == 10
    csv.write_text("date,open,close\n2025-01-02,12,12\n", encoding="utf-8")
    assert life.load_bars("600001", raw, tmp_path / "legacy")[1] == "raw_integrity_failed"


def test_forward_execution_and_attribution_use_only_frozen_pool():
    sessions = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
    bars = {
        "600001": {"2025-01-02": {"open": 10, "close": 10},
                   "2025-01-03": {"open": 10, "close": 10.5},
                   "2025-01-06": {"open": 10.5, "close": 10.7},
                   "2025-01-07": {"open": 11, "close": 11}},
        "600002": {"2025-01-02": {"open": 20, "close": 20},
                   "2025-01-03": {"open": 22, "close": 22},
                   "2025-01-07": {"open": 23, "close": 23}},
        "600003": {"2025-01-02": {"open": 30, "close": 30},
                   "2025-01-03": {"open": 30, "close": 30},
                   "2025-01-07": {"open": 33, "close": 33}},
    }
    result = life.evaluate_snapshot(_snapshot(), sessions, bars, as_of="2025-01-07")
    assert result["status"] == "observed"
    assert result["filled"] == 1 and result["cancelled"] == 1
    assert result["selected"][1]["reason"] == "gap_above_5pct"
    assert result["mean_gross_return"] == pytest.approx(0.1)
    assert result["pool_control_gross"] == pytest.approx(0.1)
    assert result["pool_control_net"] == pytest.approx(result["mean_net_return"])
    assert result["sector_control_net"] == pytest.approx(result["mean_net_return"])
    assert result["filled_share_of_pool"] == pytest.approx(1 / 3)
    assert "turnover_one_way" not in result
    assert result["attribution"]["sector_selection"] == pytest.approx(0)
    assert result["attribution"]["stock_selection"] == pytest.approx(0)
    assert result["mean_net_return"] < result["mean_gross_return"]
    assert result["mean_path_drawdown"] <= 0
    reviewed = life.review_versions([_snapshot()], [{"id": 1, "snapshot_id": 1,
                                                     "payload": result}], [])
    assert reviewed[0]["mean_net_excess_vs_pool"] == pytest.approx(0)
    assert reviewed[0]["mean_gross_excess_vs_pool"] == pytest.approx(0)
    assert reviewed[0]["comparable_observed_days"] == 1
    legacy = {key: value for key, value in result.items()
              if key not in ("cost_model_version", "pool_control_net", "sector_control_net",
                             "pool_control_gross", "sector_control_gross")}
    old_review = life.review_versions([_snapshot()], [{"id": 1, "snapshot_id": 1,
                                                       "payload": legacy}], [])
    assert old_review[0]["observed_days"] == 1
    assert old_review[0]["comparable_observed_days"] == 0
    assert old_review[0]["mean_net_excess_vs_pool"] is None
    assert old_review[0]["comparison_basis"] == "no_v2_comparable_observations"
    pause = {"strategy": "breakout", "version": "v1", "status": "paused",
             "changed_at": "2025-01-02T16:00:00+08:00"}
    blocked = life.evaluate_snapshot(_snapshot(), sessions, bars, as_of="2025-01-07",
                                     status_events=[pause])
    assert blocked["filled"] == 0 and blocked["cancelled"] == 2
    assert all(item["reason"] == "strategy_suspended_before_entry"
               for item in blocked["selected"])
    pause["changed_at"] = "2025-01-03T10:00:00+08:00"
    existing = life.evaluate_snapshot(_snapshot(), sessions, bars, as_of="2025-01-07",
                                      status_events=[pause])
    assert existing["filled"] == 1 and existing["entry_strategy_status"] == "exploratory"
    assert life.evaluate_snapshot(_snapshot(), sessions, bars, as_of="2025-01-06")[
        "status"] == "holding"
    bad_basis = dict(bars)
    bad_basis["600001"] = {**bars["600001"],
                           "2025-01-02": {"open": 12, "close": 12}}
    mismatched = life.evaluate_snapshot(_snapshot(), sessions, bad_basis, as_of="2025-01-07")
    assert mismatched["status"] == "partial_data"
    assert mismatched["selected"][0]["status"] == "price_basis_mismatch"


def test_review_keeps_sparse_forward_data_exploratory_and_gates_formal(tmp_path):
    db = str(tmp_path / "signals.db")
    store.init_db(db)
    snap = _snapshot()
    frozen = store.freeze_strategy_signal(db, snap["signal_date"], snap["strategy"],
                                          snap["version"], "15:10", snap["payload"])
    rows = store.list_strategy_signals(db)
    review = life.review_versions(rows, [], [])
    assert review[0]["status"] == "exploratory"
    assert "forward_days_under_60" in review[0]["blockers_to_formal"]
    store.add_strategy_status_event(db, "breakout", "v1", "paused", "数据故障", "16:00")
    assert life.review_versions(rows, [], store.list_strategy_status_events(db))[0]["status"] == "paused"
    with pytest.raises(ValueError, match="invalid status transition"):
        store.add_strategy_status_event(db, "breakout", "v1", "formal", "尝试越级", "16:01")
    assert store.get_strategy_signal(db, frozen["id"])["payload"] == snap["payload"]


def test_background_freeze_enforces_status_and_shadow(tmp_path):
    from core.strategy_service import generate
    db = str(tmp_path / "service.db")
    store.init_db(db)
    now = datetime.datetime(2026, 9, 30, 16)
    closes = [10.] * 65 + [10.8]
    frame = pd.DataFrame({"date": pd.date_range(end=now.date(), periods=66, freq="B").strftime("%Y-%m-%d"),
        "open": closes, "high": [10.1] * 65 + [10.9], "low": [9.9] * 65 + [10.7],
        "close": closes, "volume": [100000] * 61 + [200000] * 5, "amount": [2e8] * 66})
    pool = [{"code": "600001", "name": "测试股", "amount": 2e8}]
    def produce(shadow=False, allowed=True, clock=None):
        return generate(db, pool, {"600001": frame}, {"600001": frame}, {"600001": ["A"]},
                        None, now, shadow=shadow, allow_freeze=allowed, record_time=clock)
    sealed = now.replace(hour=17)
    first = produce(clock=lambda: sealed)
    assert first["freeze_status"] == "frozen"
    assert first["generated_at"] == sealed.isoformat()
    assert all(row["frozen_at"] == sealed.isoformat() for row in store.list_strategy_signals(db))
    assert produce()["snapshot_ids"] == first["snapshot_ids"]
    assert len(store.list_strategy_signals(db)) == 3
    version = first["frozen_version"]
    def pause_while_processing():
        store.add_strategy_status_event(db, "breakout", version, "paused", "暂停研究", now.isoformat())
        return sealed
    paused = produce(clock=pause_while_processing)
    assert paused["groups"]["breakout"] == []
    assert "breakout" not in paused["snapshot_ids"]
    shadow = produce(shadow=True)
    assert shadow["groups"]["breakout"][0]["run_mode"] == "shadow"
    assert shadow["snapshot_ids"] == {}
    store.add_strategy_status_event(db, "breakout", version, "retired", "退役", now.isoformat())
    assert produce(shadow=True)["groups"]["breakout"] == []
    assert produce(allowed=False)["freeze_status"] == "skipped_quality"
    count = len(store.list_strategy_signals(db))
    crossed_day = produce(clock=lambda: sealed + datetime.timedelta(days=1))
    assert crossed_day["freeze_status"] == "skipped_day_changed"
    assert crossed_day["snapshot_ids"] == {} and crossed_day["diagnostics"]["stale_source"]
    assert len(store.list_strategy_signals(db)) == count
