"""从每日任务的已校验留档重建板块证据，不采集新数据或冻结信号。"""
import argparse
import json
import uuid
from pathlib import Path

from core import daily_data as data, sector_relations, sector_service
from core.daily_runner import RunLock, _frame
from core.strategy_lifecycle import observation_clock
from core.stage1_data import _write_json


def run(root, run_id, refresh=False, workers=6):
    root = Path(root).resolve()
    run_id = str(uuid.UUID(run_id))
    with RunLock(root):
        manifest = data.read_json(root / "runs" / run_id / "run.json")
        day = manifest["day"]
        if day > observation_clock().date().isoformat():
            raise ValueError("future_archive_date")
        steps = manifest["steps"]
        features = steps["features"]["result"]
        artifact = features["sector_features"]
        if not data.intact(artifact):
            raise ValueError("sector_archive_hash_mismatch")
        archived = data.read_json(artifact["path"])
        archived_rows = {(row.get("category", "industry"), str(row["code"])): row for row in archived}
        summary = _frame(features["summary"])
        rows = []
        for original in summary.to_dict("records"):
            old = archived_rows.get((original.get("category", "industry"), str(original["code"])), {})
            row = {**old, "code": str(original["code"]), "name": str(original["name"]),
                   "category": original.get("category", "industry")}
            if not old:
                row.update({"status": "unobserved", "errors": ["not_collected_in_original_run"]})
            if old.get("history"):
                row["history_frame"] = _frame(old["history"])
            rows.append(row)
        spot = _frame(steps["inputs"]["result"]["spot"])
        entries = steps["prices"]["result"]["entries"]
        technical = {c: _frame(pair["technical"]["artifact"]) for c, pair in entries.items()
                     if pair.get("technical", {}).get("status") == "success"}
        benchmark = _frame(features["benchmark"]) if features.get("benchmark") else None
        calendar = steps["calendar"]["result"]["calendar"]["dates"]
        previous_day = next((d for d in reversed(calendar) if d < day), None)
        previous = sector_relations.load(root, previous_day) if previous_day else None
        provenance = [artifact, features["summary"], steps["inputs"]["result"]["spot"]]
        if refresh:
            now = observation_clock()
            if day != now.date().isoformat() or (now.hour, now.minute) < (15, 5) or day not in calendar:
                raise ValueError("refresh_requires_today_after_close_and_archived_trading_day")
            work = root / "sector_runs" / str(uuid.uuid4())
            started = now.isoformat()
            _write_json(work / "run.json", {"status": "running", "started_at": started, "as_of": day,
                                          "origin_run_id": run_id})
            source = data.LiveSources()
            def archive(name, value, provider, **meta):
                return data.archive_json(work / name, value, provider, observation_clock().isoformat(), **meta)
            def archive_frame(row, frame):
                return data.archive_frame(work / "history" / f"{row['category']}-{row['code']}.csv",
                    frame, "sector_index_source", observation_clock().isoformat(), as_of=day)
            try:
                summary, stale = source.sectors()
                if stale:
                    raise ValueError("sector_summary_stale")
                provenance.append(data.archive_frame(work / "summary.csv", summary, "native_sector_catalog",
                                                     observation_clock().isoformat(), as_of=day))
                try:
                    benchmark, stale = source.benchmark()
                    if stale:
                        raise ValueError("benchmark_stale")
                    provenance.append(data.archive_frame(work / "benchmark.csv", benchmark, "sh000001",
                                                         observation_clock().isoformat(), as_of=day))
                except Exception as exc:
                    benchmark = None
                    archive("benchmark_error.json", {"error": f"{type(exc).__name__}: {exc}"}, "sh000001")
                payload = sector_service.collect(day, summary, source, spot, technical, benchmark,
                    calendar, archive, archive_frame, workers=max(1, min(workers, 8)), previous=previous,
                    generated_at=lambda: observation_clock().isoformat())
                if day != observation_clock().date().isoformat():
                    raise ValueError("refresh_crossed_day")
                payload["sector_run_id"] = work.name
                payload["source_mode"] = "live_sector_refresh_with_archived_close_prices"
            except Exception as exc:
                _write_json(work / "run.json", {"status": "failed", "started_at": started, "as_of": day,
                    "finished_at": observation_clock().isoformat(), "error": f"{type(exc).__name__}: {exc}"})
                raise
        else:
            payload = sector_service.assemble(day, rows, spot, technical, benchmark, calendar,
            previous=previous, generated_at=observation_clock().isoformat(),
            input_artifacts=provenance)
            payload["source_mode"] = "archive_rebuild_not_new_live_run"
        payload["input_artifacts"] = payload.get("input_artifacts", []) + provenance + ([features["benchmark"]] if features.get("benchmark") else []) + [
            pair["technical"]["artifact"] for pair in entries.values() if pair.get("technical", {}).get("status") == "success"]
        payload["origin_run_id"] = run_id
        ref = sector_relations.save(root, payload)
        report = {"as_of": day, "status": payload["status"], "snapshot": ref, "origin_run_id": run_id,
                "source_mode": payload["source_mode"],
                "candidate_universe": payload["candidate_universe"],
                "technical_breadth_verified": sum(r["breadth"]["technical_status"] == "ok" for r in payload["rows"])}
        _write_json(root / "sector_evidence" / "latest_report.json", report)
        if refresh:
            _write_json(work / "run.json", {**report, "started_at": started,
                                          "finished_at": observation_clock().isoformat()})
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--refresh", action="store_true", help="当日收盘后更新完整板块集合，复用已留档股票价格")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    result = run(args.root, args.run_id, args.refresh, args.workers)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
