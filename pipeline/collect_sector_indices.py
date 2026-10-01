"""Collect native sector price references without inventing membership joins."""
import argparse
import json
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pandas as pd

from core import daily_data as data, sector_index_sources, sector_insights, strategy_lifecycle as life
from core.daily_runner import RunLock
from core.stage1_data import _write_json, load_calendar, sha256


def observed_reference(root, snapshot, benchmark, calendar):
    """Reuse archived same-source SW prices; keep member and price dates distinct."""
    records = [r for r in snapshot["rows"] if r.get("taxonomy") == "sw_native_index"]
    if not records:
        return None
    if benchmark is None or benchmark.empty:
        raise ValueError("native_observed_reference_requires_benchmark")
    now = life.observation_clock()
    day = max(d for d in calendar if life.observation_clock(d + "T15:00:00+08:00") <= now)
    work = Path(root) / "sector_native_indices" / "runs" / str(uuid.uuid4())
    raw_refs = []
    if benchmark.attrs.get("raw_response"):
        raw_refs.append(data.archive_json(work / "benchmark_response.json", benchmark.attrs["raw_response"],
                                         benchmark.attrs.get("source", "sh000001"), now.isoformat()))
    benchmark_ref = data.archive_frame(work / "benchmark.csv", benchmark,
        benchmark.attrs.get("source", "sh000001"), now.isoformat(), as_of=str(benchmark["date"].iloc[-1]),
        dependencies=raw_refs)
    rows = []
    for original in records:
        row = {k: original[k] for k in ("code", "name", "category")}
        row.update(provider="sw", membership_observed_at=original["observed_at"],
                   membership_status=original["membership_status"], membership_response=original.get("membership_response"),
                   feature_binding_status="same_observation_day" if snapshot["as_of"] == day else "member_price_date_mismatch")
        try:
            ref = original.get("history")
            if not ref or not data.intact(ref):
                raise ValueError("native_observed_price_missing_or_changed")
            frame = pd.read_csv(ref["path"], dtype={"date": str})
            row.update(history=ref, status="current" if frame["date"].iloc[-1] == day else "stale",
                       relative=sector_insights.relative_strength(frame, benchmark, as_of=day, calendar=calendar))
        except Exception as exc:
            row.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
        rows.append(row)
    report = {"version": "observed-native-index-reference-v1", "provider": "sw", "observed_at": now.isoformat(),
        "as_of": day, "membership_observation_day": snapshot["as_of"], "total": len(rows),
        "current": sum(r["status"] == "current" for r in rows),
        "relative20_verified": sum(r.get("relative", {}).get("returns", {}).get("20") is not None for r in rows),
        "status": "complete_prices" if all(r["status"] == "current" for r in rows) else "partial",
        "benchmark_ref": benchmark_ref, "catalog_refs": snapshot.get("input_artifacts", []), "rows": rows,
        "code_sha256": sha256(__file__), "limits": ["observed_members_not_historical_constituents",
            "price_reference_only_when_members_and_price_dates_differ", "native_units_not_execution_capacity",
            "descriptive_not_prediction_evidence"]}
    _write_json(work / "report.json", report)
    ref = {"path": str((work / "report.json").resolve()), "sha256": sha256(work / "report.json"), "as_of": day}
    _write_json(Path(root) / "sector_native_indices" / "latest.json", ref)
    return ref


def run(root=data.ROOT, workers=6, *, lock_held=False, as_of=None, get=None, benchmark=None):
    """DailyRunner already holds RunLock; standalone callers acquire it here."""
    root = Path(root)
    with (nullcontext() if lock_held else RunLock(root)):
        now = life.observation_clock()
        calendar = load_calendar(root / "calendar.json")
        if not calendar:
            raise ValueError("native_indices_require_calendar")
        day = max(d for d in calendar["dates"] if life.observation_clock(d + "T15:00:00+08:00") <= now)
        if as_of is not None and as_of != day:
            raise ValueError("native_indices_require_latest_closed_session")
        work = root / "sector_indices" / "runs" / str(uuid.uuid4())
        live = data.LiveSources()
        get = get or live.get
        source = sector_index_sources.ThsIndices(get=get)
        catalog, raw = source.catalog()
        catalog_ref = data.archive_json(work / "catalog.json", raw, "ths.native_catalog", now.isoformat())
        benchmark = benchmark if benchmark is not None else data.tencent_benchmark(day, get=get)
        dependencies = []
        if benchmark.attrs.get("raw_response"):
            dependencies.append(data.archive_json(work / "benchmark_response.json", benchmark.attrs["raw_response"],
                                benchmark.attrs.get("source", "tencent.sh000001"), now.isoformat()))
        benchmark_ref = data.archive_frame(work / "benchmark.csv", benchmark,
            benchmark.attrs.get("source", "sh000001"), now.isoformat(),
            as_of=str(benchmark["date"].iloc[-1]), dependencies=dependencies)
        def inspect(row):
            try:
                frame, raw = source.history(row, day)
                refs = [data.archive_json(work / row["code"] / f"response-{i}.json", value,
                    value["source"], life.observation_clock().isoformat()) for i, value in enumerate(raw)]
                ref = data.archive_frame(work / row["code"] / "history.csv", frame,
                    "ths.native_sector_index", life.observation_clock().isoformat(), as_of=frame["date"].iloc[-1],
                    dependencies=refs, quantity_basis="provider_native_not_stock_execution_units")
                relative = sector_insights.relative_strength(frame, benchmark, as_of=day, calendar=calendar["dates"])
                return {**row, "status": "current" if frame["date"].iloc[-1] == day else "stale", "history": ref,
                        "relative": relative, "membership_status": "not_collected_no_cross_provider_join"}
            except Exception as exc:
                return {**row, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
        with ThreadPoolExecutor(max_workers=max(1, min(8, workers))) as executor:
            rows = list(executor.map(inspect, catalog))
        report = {"version": sector_index_sources.VERSION, "observed_at": now.isoformat(), "as_of": day,
            "status": "complete_prices" if all(r["status"] == "current" for r in rows) else "partial",
            "total": len(rows), "current": sum(r["status"] == "current" for r in rows),
            "relative20_verified": sum(r.get("relative", {}).get("returns", {}).get("20") is not None for r in rows),
            "catalog_ref": catalog_ref, "benchmark_ref": benchmark_ref, "rows": rows,
            "code_sha256": sha256(sector_index_sources.__file__),
            "limits": ["native_index_prices_not_historical_membership", "no_name_matching_to_sina_members",
                       "not_stock_execution_inputs", "descriptive_not_prediction_evidence"]}
        _write_json(work / "report.json", report)
        ref = {"path": str((work / "report.json").resolve()), "sha256": sha256(work / "report.json"), "as_of": day}
        _write_json(root / "sector_indices" / "latest.json", ref)
        return {k: report[k] for k in ("status", "total", "current", "relative20_verified", "as_of")} | {"report_ref": ref}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.workers)))


if __name__ == "__main__":
    main()
