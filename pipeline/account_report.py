"""Replay frozen signals into immutable cash/share accounts; no broker orders or signal backfill."""
import argparse
import copy
import json
from pathlib import Path

import pandas as pd

from core import account_ledger as ledger, daily_data as data, store, sector_relations, strategy_lifecycle as life, execution_evidence
from core.stage1_data import _write_json, load_calendar, sha256

ROOT = Path(__file__).resolve().parents[1]


def _dated(rows):
    out = {}
    for row in rows:
        day, code = row["date"], row["code"]
        life.observation_clock(day)
        if len(code) != 6 or not code.isdigit() or code in out.get(day, {}):
            raise ValueError("duplicate_or_invalid_dated_evidence")
        out.setdefault(day, {})[code] = row
    return out


def _raw(code, root):
    path = Path(root) / "execution" / "raw_daily" / (code + ".csv")
    meta = data.read_json(path.with_suffix(".json"))
    if meta.get("adjust") != "none" or meta.get("sha256") != sha256(path):
        raise ValueError("raw_price_integrity_or_adjustment_failed")
    frame = pd.read_csv(path, dtype={"date": str})
    if not {"date", "open", "close", "amount"}.issubset(frame.columns):
        raise ValueError("raw_price_schema_incomplete")
    dates = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("duplicate_or_reverse_price_dates")
    out = {}
    for day, (_, row) in zip(dates, frame.iterrows()):
        out[day] = {key: life._finite(row.get(key)) for key in ("open", "close", "amount")}
    return out, {"path": str(path.resolve()), "sha256": meta["sha256"], "adjust": "none"}


def load_inputs(db, root, as_of):
    root = Path(root)
    clock = life.observation_clock(as_of)
    calendar = load_calendar(root / "calendar.json")
    if not calendar:
        raise ValueError("account_requires_archived_calendar")
    snapshots = [r for r in store.list_strategy_signals(db, limit=100000)
                 if life.observation_clock(r["frozen_at"]) <= clock and r["signal_date"] <= clock.date().isoformat()]
    codes = sorted({str(r["code"])[-6:] for s in snapshots for r in s["payload"].get("pool", [])}
                   | {str(r["code"])[-6:] for s in snapshots for r in s["payload"].get("candidates", [])})
    bars, bases, artifacts, errors = {}, {}, [], []
    for code in codes:
        try:
            bars[code], artifact = _raw(code, root)
            bases[code] = "verified_unadjusted_raw"
            artifacts.append(artifact)
        except (OSError, ValueError, KeyError) as exc:
            bars[code], bases[code] = {}, "missing_or_invalid_raw"
            errors.append({"code": code, "reason": str(exc)})
    status_path = root / "execution" / "trade_status.json"
    action_path = root / "execution" / "corporate_actions.json"
    status = data.read_json(status_path) if status_path.exists() else {"rows": []}
    actions = data.read_json(action_path) if action_path.exists() else {"events": [], "coverage": []}
    imported, evidence_refs = execution_evidence.load(root)
    # Legacy files remain readable research inputs. A mutable verification flag
    # cannot substitute for an archived, checked source document.
    if evidence_refs:
        status = {"rows": imported["trade_status"]}
        actions = {"events": imported["corporate_actions"], "coverage": imported["corporate_coverage"]}
    else:
        status = {"rows": [{**r, "verified": False} for r in status.get("rows", [])]}
        actions = {"events": [{**r, "verified": False} for r in actions.get("events", [])],
                   "coverage": [{**r, "verified": False} for r in actions.get("coverage", [])]}
    links, benchmark, sector_returns = {}, {}, {}
    first = min([s["signal_date"] for s in snapshots], default=clock.date().isoformat())
    sessions = calendar["dates"]
    first_idx = sessions.index(first) if first in sessions else 0
    for day in sessions[max(0, first_idx - 1):]:
        if day > clock.date().isoformat():
            break
        snapshot = sector_relations.load(root, day)
        if not snapshot:
            continue
        links[day] = {c: sector_relations.lookup(snapshot, c, day) for c in codes}
        # Only dated, archived sector-index frames are acceptable references.
        for row in snapshot.get("rows", []):
            artifact = row.get("history")
            if not artifact or not data.intact(artifact):
                continue
            frame = pd.read_csv(artifact["path"], dtype={"date": str})
            if "date" not in frame or "close" not in frame or frame["date"].duplicated().any():
                continue
            prices = dict(zip(frame["date"], frame["close"]))
            idx = sessions.index(day)
            previous = sessions[idx - 1] if idx else None
            if life._finite(prices.get(day)) and life._finite(prices.get(previous)):
                sector_returns.setdefault(day, {})[row["id"]] = prices[day] / prices[previous] - 1
            artifacts.append(artifact)
        for artifact in snapshot.get("input_artifacts", []):
            if artifact.get("source") in ("akshare.sh000001", "tencent.sh000001") and data.intact(artifact):
                frame = pd.read_csv(artifact["path"], dtype={"date": str})
                if "date" in frame and "close" in frame and not frame["date"].duplicated().any():
                    for d, price in zip(frame["date"], frame["close"]):
                        if d <= day and life._finite(price):
                            benchmark.setdefault(d, float(price))
                    artifacts.append(artifact)
    inputs = {"snapshots": snapshots, "bars": bars, "calendar": sessions, "bases": bases,
              "trade_status": _dated(status.get("rows", [])), "corporate_actions": actions.get("events", []),
              "corporate_coverage": _dated(actions.get("coverage", [])), "relations": links,
              "benchmark": benchmark, "sector_returns": sector_returns,
              "status_events": store.list_strategy_status_events(db)}
    inputs["execution_rules"] = _dated(imported["execution_rules"])
    return inputs, {"calendar_basis": calendar.get("source"), "price_artifacts": artifacts,
                    "source_errors": errors, "execution_status_file": str(status_path) if status_path.exists() else None,
                    "corporate_action_file": str(action_path) if action_path.exists() else None,
                    "execution_evidence_refs": evidence_refs,
                    "execution_evidence_counts": {k: len(v) for k, v in imported.items()},
                    "legacy_verification_policy": "mutable_legacy_rows_research_only"}


def controls(inputs, kind):
    output = copy.deepcopy(inputs)
    blocked = []
    for snap in output["snapshots"]:
        payload = snap["payload"]
        if not payload.get("candidates"):
            continue  # controls activate only on the same strategy signal dates
        original = payload["candidates"]
        if kind == "pool_equal":
            payload["candidates"] = payload.get("pool", [])
        elif kind == "sector_equal":
            codes = [str(c["code"])[-6:] for c in payload["candidates"]]
            relations = inputs.get("relations", {}).get(snap["signal_date"], {})
            ids = set()
            for code in codes:
                row = relations.get(code, {})
                if row.get("status") != "ok" or row.get("as_of") != snap["signal_date"]:
                    blocked.append({"snapshot_id": snap["id"], "code": code, "reason": "selected_sector_unknown"})
                else:
                    ids.update(r["id"] for r in row["relations"])
            pool = payload.get("pool", [])
            if any(relations.get(str(r["code"])[-6:], {}).get("status") != "ok" for r in pool):
                blocked.append({"snapshot_id": snap["id"], "reason": "pool_sector_coverage_incomplete"})
            payload["candidates"] = [r for r in pool if ids.intersection(
                x["id"] for x in relations.get(str(r["code"])[-6:], {}).get("relations", []))]
        if {str(c["code"])[-6:] for c in original} == {str(c["code"])[-6:] for c in payload["candidates"]}:
            payload["candidates"] = original  # identical basket retains identical deterministic execution priority
    return output, blocked


def build(inputs, *, as_of, settings=None, source="frozen_forward_signals"):
    result = ledger.replay(**inputs, as_of=as_of, settings=settings)
    result["source"] = source
    result["comparisons"] = {}
    for kind in ("pool_equal", "sector_equal"):
        control_inputs, blocked = controls(inputs, kind)
        if blocked:
            result["comparisons"][kind] = {"status": "blocked_membership_coverage", "blockers": blocked,
                                            "total_net_excess": None, "daily": []}
            continue
        control = ledger.replay(**control_inputs, as_of=as_of, settings=settings)
        returns = control["metrics"]["total_return"]
        selected_return = result["metrics"]["total_return"]
        by_day = {r["date"]: r for r in control["daily"]}
        paired = [{"date": r["date"], "selected_unit_nav": r["unit_nav"],
                   "control_unit_nav": by_day.get(r["date"], {}).get("unit_nav"),
                   "daily_net_excess": r["daily_return"] - by_day[r["date"]]["daily_return"]
                     if r["performance_evidence_complete"] and by_day.get(r["date"], {}).get("performance_evidence_complete")
                        and r["daily_return"] is not None and by_day.get(r["date"], {}).get("daily_return") is not None else None}
                  for r in result["daily"]]
        result["comparisons"][kind] = {"status": control["status"], "total_net_excess":
            selected_return - returns if selected_return is not None and returns is not None else None,
            "metrics": control["metrics"], "daily": paired, "account": control,
            "basis": "same_signal_days_capital_strategy_budgets_horizons_and_execution_policy"}
    return result


def _immutable(path, value):
    if path.exists():
        if ledger.digest(data.read_json(path)) != ledger.digest(value):
            raise ValueError("immutable_account_artifact_collision")
    else:
        _write_json(path, value)


def run(db, root=data.ROOT, *, as_of=None, settings=None, supplied_inputs=None,
        source="frozen_forward_signals", persist=True):
    root = Path(root)
    cfg = ledger.config(settings)
    clock = life.observation_clock(as_of)
    if supplied_inputs is not None and source == "frozen_forward_signals":
        raise ValueError("injected_inputs_require_explicit_research_source")
    identity = ledger.digest({"version": ledger.VERSION, "config": cfg, "source": source})
    account_root = root / "accounts"
    # Register the account budget before loading prices or calculating returns.
    registration = {"version": ledger.VERSION, "config": cfg, "source": source,
                    "account_id": identity, "policy": "fixed_slots_no_redistribution_first_frozen_snapshot_per_day_strategy"}
    if persist:
        _immutable(account_root / "configs" / (identity + ".json"), registration)
    if supplied_inputs is None:
        inputs, manifest = load_inputs(db, root, clock)
    else:
        inputs, manifest = supplied_inputs, {"source_errors": [], "calendar_basis": "injected_research_fixture"}
    result = build(inputs, as_of=clock, settings=cfg, source=source)
    result.update({"account_id": identity, "inputs_sha256": ledger.digest(inputs), "input_manifest": manifest,
                   "account_code_sha256": sha256(ledger.__file__), "runner_code_sha256": sha256(__file__),
                   "registration": registration, "benchmark_basis": "sh000001_reference_not_investable_all_A"})
    if persist:
        input_path = account_root / "inputs" / (result["inputs_sha256"] + ".json")
        _immutable(input_path, inputs)
        result["inputs_ref"] = {"path": str(input_path.resolve()), "sha256": sha256(input_path)}
        report_path = account_root / "blobs" / (ledger.digest(result) + ".json")
        _immutable(report_path, result)
        ref = {"account_id": identity, "path": str(report_path.resolve()), "sha256": sha256(report_path),
               "as_of": result["as_of"], "status": result["status"], "source": source, "mode": cfg["mode"]}
        _write_json(account_root / "by_account" / (identity + ".json"), ref)
        # Injected/research replay cannot replace the live forward report seen in the UI.
        if source == "frozen_forward_signals":
            _write_json(account_root / "latest.json", ref)
        result["report_ref"] = ref
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "market.db"))
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--as-of", help="Shanghai time or closing date; excludes future observations")
    parser.add_argument("--config", help="Account budget JSON; changed budgets create a separate account")
    parser.add_argument("--inputs", help="Explicit research input bundle; never writes forward latest")
    args = parser.parse_args(argv)
    store.init_db(args.db)
    settings = data.read_json(args.config) if args.config else None
    result = run(args.db, args.root, as_of=args.as_of, settings=settings,
                 supplied_inputs=data.read_json(args.inputs) if args.inputs else None,
                 source="injected_research_replay" if args.inputs else "frozen_forward_signals")
    print(json.dumps({k: result[k] for k in ("account_id", "status", "mode", "metrics", "report_ref")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
