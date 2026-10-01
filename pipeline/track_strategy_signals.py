# -*- coding: utf-8 -*-
"""追踪真实冻结的阶段二候选；不从历史回测反填前向信号。"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import store, strategy_lifecycle as lifecycle
from core.stage1_data import load_calendar


def run(db, raw_dir, legacy_dir, calendar_path, *, persist=True, as_of=None, recompute=False):
    calendar = load_calendar(calendar_path)
    snapshots = store.list_strategy_signals(db, limit=100000)
    if not calendar:
        return {"status": "missing_calendar", "snapshots": len(snapshots), "pending": len(snapshots),
                "progress": [], "versions": []}
    sessions = calendar["dates"]
    status_events = store.list_strategy_status_events(db)
    saved_before = store.list_strategy_observations(db, limit=100000)
    latest_before = {row["snapshot_id"]: row["payload"] for row in reversed(saved_before)}
    completed = {key: row for key, row in latest_before.items() if row.get("status") == "observed"
                 and row.get("cost_model_version") == lifecycle.COST_MODEL_VERSION} if not recompute else {}
    # 相同代码的日线只读取一次；结果本身仍按每个冻结版本单独存档。
    codes = {item["code"][-6:] for row in snapshots if row["id"] not in completed
             for item in row["payload"].get("pool", [])}
    loaded = {code: lifecycle.load_bars(code, raw_dir, legacy_dir) for code in codes}
    bars = {code: pair[0] for code, pair in loaded.items()}
    bases = {code: pair[1] for code, pair in loaded.items()}
    observations = []
    clock = lifecycle.observation_clock(as_of)
    now = clock.isoformat()
    for snapshot in snapshots:
        if snapshot["id"] in completed:
            observations.append({"snapshot_id": snapshot["id"], "payload": completed[snapshot["id"]]})
            continue
        result = lifecycle.evaluate_snapshot(snapshot, sessions, bars, bases,
                                             as_of=clock, status_events=status_events)
        if persist:
            store.save_strategy_observation(
                db, snapshot["id"], lifecycle.TRACKER_VERSION,
                lifecycle.observation_digest(snapshot, result), now, result)
        observations.append({"snapshot_id": snapshot["id"], "payload": result})
    saved = store.list_strategy_observations(db, limit=100000) if persist else observations
    return {"status": "exploratory", "generated_at": now,
            "tracker_version": lifecycle.TRACKER_VERSION,
            "calendar_basis": calendar.get("source"), "snapshots": len(snapshots),
            "observed_or_partial": len(observations),
            "pending": sum(row["payload"]["status"] != "observed" for row in observations),
            "progress": [{"snapshot_id": row["snapshot_id"],
                          "status": row["payload"]["status"],
                          "counts": row["payload"].get("lifecycle_counts", {})} for row in observations],
            "versions": lifecycle.review_versions(
                snapshots, saved, status_events),
            "notice": "仅冻结后真实信号；逐股开盘代理成交，停牌/涨跌停与公司行为尚未核实。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "market.db"))
    parser.add_argument("--raw-dir", default=str(ROOT / "_analysis" / "daily_pipeline" / "execution" / "raw_daily"))
    parser.add_argument("--legacy-dir", default=str(ROOT / "_analysis" / "daily"))
    parser.add_argument("--calendar", default=str(ROOT / "_analysis" / "daily_pipeline" / "calendar.json"))
    parser.add_argument("--out", default=str(ROOT / "_analysis" / "stage4_strategy_monitor.json"))
    parser.add_argument("--recompute", action="store_true", help="显式另存重算已完成的观察，原记录保留")
    args = parser.parse_args()
    store.init_db(args.db)
    report = run(args.db, args.raw_dir, args.legacy_dir, args.calendar, recompute=args.recompute)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "snapshots", "pending", "versions")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
