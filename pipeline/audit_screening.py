"""留档重放的在线／历史决定差异报告；不采集、不冻结，不算前向样本。"""
import argparse
import json
import uuid
from pathlib import Path

import pandas as pd

from core import daily_data, screening_engine as engine, sector_relations, strategy_service, store
from core.daily_runner import _frame
from core.stage1_data import _write_json
from core.strategy_lifecycle import observation_clock
from pipeline.evaluate_strategies import evaluate_day


def run(root, run_id):
    root = Path(root).resolve()
    run_id = str(uuid.UUID(run_id))
    manifest = daily_data.read_json(root / "runs" / run_id / "run.json")
    steps, day = manifest["steps"], manifest["day"]
    spot = _frame(steps["inputs"]["result"]["spot"])
    excluded = daily_data.read_json(steps["inputs"]["result"]["new_stocks"]["path"])
    scope, removed = engine.acquisition_scope(spot.to_dict("records"), excluded)
    entries = steps["prices"]["result"]["entries"]
    technical = {c: _frame(pair["technical"]["artifact"]) for c,pair in entries.items()
                 if pair.get("technical", {}).get("status") == "success"}
    execution = {c: _frame(pair["raw"]["artifact"]) for c,pair in entries.items()
                 if pair.get("raw", {}).get("status") == "success"}
    snapshot = sector_relations.load(root, day)
    regime = steps["features"]["result"].get("regime")
    work = root / "screening_audits" / str(uuid.uuid4())
    work.mkdir(parents=True, exist_ok=True)
    db = str(work / "replay.sqlite")
    store.init_db(db)
    now = observation_clock(day + "T17:00:00+08:00")
    online = strategy_service.generate(db, scope, technical, execution, {}, regime, now,
        allow_freeze=False, sector_snapshot=snapshot)["screening_decision"]
    dates = sorted(set().union(*(set(f["date"]) for f in technical.values())))
    if day not in dates:
        raise ValueError("no_decision_day_in_archive")
    pos = {c: {d:i for i,d in enumerate(f["date"])} for c,f in technical.items()}
    offline = evaluate_day(technical, pos, dates, dates.index(day), {},
        (regime or {}).get("label"), rows=scope, sector_snapshot=snapshot, execution=execution)["breakout"]["screening_decision"]
    comparison = engine.compare(online, offline)
    # 改变决策日之后的数据，重放在线决定必须保持原内容。
    changed = {}
    future = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    for c,f in technical.items():
        extra = {key: f[key].iloc[-1] for key in f.columns}
        extra.update(date=future, close=999999., high=999999., volume=999999999.)
        changed[c] = pd.concat([f, pd.DataFrame([extra])], ignore_index=True)
    future_decision = engine.select(day, changed, scope, regime=regime, sector_snapshot=snapshot)
    report = {"run_id": work.name, "origin_run_id": run_id, "day": day, "source_mode": "archive_replay_not_forward",
        "engine_version": engine.VERSION, "generated_at": observation_clock().isoformat(),
        "comparison": comparison, "future_data_comparison": engine.compare(online, future_decision),
        "coverage": online["scope"], "preflight_rejections": removed,
        "selected_pool_codes": [r["code"] for r in online["pool"]],
        "freeze_status": "not_attempted_replay", "limits": ["identical_available_inputs_not_complete_market_coverage"]}
    _write_json(work / "online.json", online)
    _write_json(work / "historical.json", offline)
    _write_json(work / "report.json", report)
    _write_json(root / "screening_audits" / "latest.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(daily_data.ROOT))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run(args.root, args.run_id)
    print(json.dumps({"run_id": result["run_id"], "comparison": result["comparison"]["status"],
        "future_data_comparison": result["future_data_comparison"]["status"],
        "expected_inputs": result["coverage"]["expected"], "eligible": result["coverage"]["eligible"],
        "selected": result["coverage"]["selected"], "missing_inputs": len(result["coverage"]["missing_codes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
