import json

import pandas as pd

from core import daily_data as data
from core.market_environment_inputs import market_history_readiness
from core.stage1_data import _write_json


def observation(root, day, *, verified=True):
    row = {"date": day, "r1": .001, "up_ratio": .55, "limit_up": 5,
           "limit_down": 1, "turnover": 1e10}
    if verified:
        source = data.archive_json(root / "source" / (day + ".json"), {"date": day},
                                   "test_saved_spot", day + "T15:05:00+08:00", as_of=day)
        row["source_artifact"] = source
        proof = data.archive_json(root / "proof" / (day + ".json"), row,
                                  "test_observation", day + "T15:05:00+08:00", dependencies=[source])
        row["observation_artifact"] = proof
    _write_json(root / "market_daily" / (day + ".json"), row)
    return row


def calendar():
    dates = pd.date_range("2026-01-01", periods=65, freq="B").strftime("%Y-%m-%d").tolist()
    return {"first": dates[0], "last": dates[-1], "dates": dates}


def test_readiness_requires_exact_window_not_total_observation_count(tmp_path):
    cal = calendar()
    for day in cal["dates"]:
        observation(tmp_path, day)
    day = cal["dates"][-1]
    ready, rows = market_history_readiness(tmp_path, day, cal)
    assert ready["ready"] and len(rows) == 61
    assert ready["observed_sessions"] == 65 and ready["window_verified_sessions"] == 61
    missing = cal["dates"][-3]
    (tmp_path / "market_daily" / (missing + ".json")).unlink()
    after, _ = market_history_readiness(tmp_path, day, cal)
    assert not after["ready"] and after["status"] == "history_gaps"
    assert after["observed_sessions"] == 64 and after["contiguous_sessions"] == 2
    assert after["missing_dates"] == [missing]
    assert after["window_available_sessions"] == 60


def test_old_observations_do_not_gain_source_proof_by_being_present(tmp_path):
    cal = calendar()
    for day in cal["dates"][-61:]:
        observation(tmp_path, day, verified=False)
    before = {p: p.read_bytes() for p in (tmp_path / "market_daily").glob("*.json")}
    result, rows = market_history_readiness(tmp_path, cal["dates"][-1], cal)
    assert result["observed_sessions"] == 61 and result["window_available_sessions"] == 61
    assert result["status"] == "unverified_history" and not result["ready"]
    assert result["window_verified_sessions"] == 0 and len(result["unverified_source_dates"]) == 61
    assert all(p.read_bytes() == body for p, body in before.items())


def test_corrupt_source_and_nonfinite_observation_are_missing_not_zero(tmp_path):
    cal = calendar()
    days = cal["dates"][-3:]
    first = observation(tmp_path, days[0])
    observation(tmp_path, days[1])
    observation(tmp_path, days[2])
    from pathlib import Path
    Path(first["source_artifact"]["path"]).write_text('{}', encoding="utf-8")
    path = tmp_path / "market_daily" / (days[1] + ".json")
    row = json.loads(path.read_text(encoding="utf-8"))
    row["r1"] = float("nan")
    path.write_text(json.dumps(row), encoding="utf-8")
    result, rows = market_history_readiness(tmp_path, days[2], cal)
    assert result["status"] == "invalid_history"
    assert result["invalid_dates"] == days[:2]
    assert result["observed_sessions"] == 1
    assert [r["date"] for r in rows] == days[2:]


def test_future_rows_and_closed_day_records_do_not_count(tmp_path):
    cal = calendar()
    target = cal["dates"][-2]
    observation(tmp_path, target)
    observation(tmp_path, cal["dates"][-1])
    observation(tmp_path, "2026-01-03")
    result, rows = market_history_readiness(tmp_path, target, cal)
    assert result["observed_sessions"] == 1 and result["contiguous_sessions"] == 1
    assert [r["date"] for r in rows] == [target]


def test_complete_verified_window_reuses_existing_classifier(tmp_path):
    from core.daily_runner import DailyRunner
    from core import environment
    cal = calendar()
    for day in cal["dates"][:-1]:
        observation(tmp_path, day)
    worker = object.__new__(DailyRunner)
    worker.root, worker.work, worker.day = tmp_path, tmp_path / "run", cal["dates"][-1]
    spot = pd.DataFrame([{"code": "600001", "price": 10., "volume": 100.,
                          "amount": 1e10, "change_pct": .1}])
    ref = data.archive_frame(worker.work / "spot.csv", spot, "test_saved_spot",
                             worker.day + "T15:05:00+08:00", as_of=worker.day)
    worker.results = {"inputs": {"status": "success", "spot": ref},
                      "calendar": {"calendar": cal}}
    regime, artifact = worker.build_environment()
    _, rows = market_history_readiness(tmp_path, worker.day, cal)
    expected = environment.classify_market_rows(rows).iloc[-1]["environment"]
    assert regime["status"] == "current" and regime["label"] == expected
    assert regime["readiness"]["window_verified_sessions"] == 61
    assert data.intact(artifact)
