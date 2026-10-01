"""Observe current native classifications even on holidays, without backdating or freezing."""
import argparse
import json
import uuid
from pathlib import Path

import pandas as pd

from core import daily_data as data, sector_service, sector_relations
from core.strategy_lifecycle import observation_clock
from core.stage1_data import _write_json, load_calendar
from core.daily_runner import RunLock


def run(root=data.ROOT, workers=6):
    root = Path(root)
    with RunLock(root):
        now = observation_clock()
        day, work = now.date().isoformat(), root / "sector_runs" / str(uuid.uuid4())
        source = data.LiveSources()
        summary, stale = source.sectors()
        if stale:
            raise ValueError("native_catalog_stale")
        def archive(name, value, provider, **meta):
            return data.archive_json(work / name, value, provider, observation_clock().isoformat(), **meta)
        def archive_frame(row, frame):
            return data.archive_frame(work / "history" / (row["code"] + ".csv"), frame,
                frame.attrs.get("source", "native_sector_index"), observation_clock().isoformat(),
                as_of=str(frame["date"].iloc[-1]), dependencies=[row["index_response"]] if row.get("index_response") else [],
                quantity_basis=frame.attrs.get("quantity_basis", "provider_native_not_stock_execution_units"))
        # A holiday inventory cannot claim to have today's prices or technical breadth.
        payload = sector_service.collect(day, summary, source, pd.DataFrame(columns=["code"]),
            {}, None, [], archive, archive_frame, workers=max(1, min(8, workers)),
            generated_at=lambda: observation_clock().isoformat())
        if observation_clock().date().isoformat() != day:
            raise ValueError("sector_inventory_crossed_observation_day")
        payload.update(source_mode="live_current_inventory_not_trading_day_features", sector_run_id=work.name)
        ref = sector_relations.save(root, payload)
        native_ref, native_error = None, None
        if any(r.get("taxonomy") == "sw_native_index" for r in payload["rows"]):
            try:
                from pipeline.collect_sector_indices import observed_reference
                calendar = load_calendar(root / "calendar.json")["dates"]
                closed = max(d for d in calendar if observation_clock(d + "T15:00:00+08:00") <= now)
                benchmark = data.tencent_benchmark(closed, get=source.get)
                native_ref = observed_reference(root, payload, benchmark, calendar)
            except Exception as exc:
                native_error = f"{type(exc).__name__}: {exc}"[:500]
        report = {"as_of": day, "status": payload["status"], "source_mode": payload["source_mode"],
                  "snapshot": ref, "candidate_universe": payload["candidate_universe"],
                  "categories_complete": payload["categories_complete"],
                  "native_index_reference": native_ref, "native_index_error": native_error,
                  "failed_memberships": [{"id": r["id"], "errors": r["errors"]} for r in payload["rows"]
                                          if r["membership_status"] != "verified_current"]}
        _write_json(work / "report.json", report)
        _write_json(root / "sector_evidence" / "inventory_latest.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    report = run(args.root, args.workers)
    print(json.dumps({k: report[k] for k in ("as_of", "status", "snapshot", "candidate_universe", "categories_complete")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
