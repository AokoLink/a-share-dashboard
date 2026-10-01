"""先登记实验，再执行条件逐项消融与同日对照；不发布策略升级。"""
import argparse
import json
import uuid
from collections import Counter
from pathlib import Path

import pandas as pd

from core import screening_engine as engine, research_evaluation as ev, strategies, sector_relations as sectors
from core import experiment_protocol, strategy_lifecycle as life
from core.stage1_data import _write_json, sha256, load_calendar

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "_analysis" / "strategy_experiments"


def load_universe(path):
    """不以静态板块映射或期末历史长度预先筛证券；这些资格交给 T 日引擎。"""
    universe, errors = {}, []
    for file in sorted(Path(path).glob("*.pkl")):
        code = file.stem
        if len(code) != 6 or not code.isdigit():
            continue
        try:
            frame = pd.read_pickle(file).copy()
            dates = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
            if dates.duplicated().any() or not dates.is_monotonic_increasing:
                raise ValueError("duplicate_or_reverse_dates")
            frame["date"] = dates
            universe[code] = frame.reset_index(drop=True)
        except Exception as exc:
            errors.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    return universe, errors


def sensitivity(record, multipliers):
    output = {}
    for scale in multipliers:
        arms = {}
        for name, original in record["arms"].items():
            if name == "sector_random":
                continue
            events = []
            for event in original["events"]:
                altered = dict(event)
                if event["status"] == "completed":
                    fee, slip = life.FEE_PER_SIDE * scale, life.SLIPPAGE_PER_SIDE * scale
                    altered["net_return"] = event["exit_price"] * (1 - fee) * (1 - slip) / (event["entry_price"] * (1 + fee) * (1 + slip)) - 1
                events.append(altered)
            arms[name] = ev.arm(events)["fixed_weight_net_proxy"]
        output[str(scale)] = arms["full"] - arms["market_equal"] if arms["full"] is not None and arms["market_equal"] is not None else None
    return output


def run(start="2022-01-01", end="2026-08-14", step=20, sector_root=None, *, data_dir=None,
        output_root=None, universe=None, regimes=None, calendar=None):
    root = Path(output_root or DEFAULT_OUT)
    protocol, protocol_ref = experiment_protocol.register(root, start, end, step)
    work = root / "runs" / str(uuid.uuid4())
    started = life.observation_clock().isoformat()
    _write_json(work / "run.json", {"status": "running", "started_at": started, "protocol": protocol_ref})
    source_mode = "injected_test" if universe is not None else "historical_pkl_research_proxy"
    errors = []
    if universe is None:
        universe, errors = load_universe(data_dir or ROOT / "_analysis" / "daily")
    if not universe:
        raise ValueError("empty_research_universe")
    universe = {c: f.assign(date=pd.to_datetime(f["date"]).dt.strftime("%Y-%m-%d")) for c,f in universe.items()}
    available_end = max(f["date"].iloc[-1] for f in universe.values())
    saved_calendar = load_calendar(ROOT / "_analysis" / "daily_pipeline" / "calendar.json") if calendar is None else None
    calendar_basis = "provided" if calendar is not None else "archived_sina_calendar" if saved_calendar else "available_daily_union_proxy"
    calendar = calendar or (saved_calendar or {}).get("dates") or sorted(set().union(*(set(f["date"]) for f in universe.values())))
    if regimes is None:
        regime_path = ROOT / "_analysis" / "environment_report.json"
        payload = json.loads(regime_path.read_text(encoding="utf-8"))
        regimes = {r["date"]: r["environment"] for r in payload["series"]}
    indexes = {c: {d: i for i,d in enumerate(f["date"])} for c,f in universe.items()}
    indexed_execution = ev.execution_index(universe)
    window = [d for d in calendar if start <= d <= end]
    regular = set(window[::step])
    sampled = sorted(regular | {d for d in window if regimes.get(d) == "恐慌"})
    if not sampled:
        raise ValueError("no_research_sessions")
    variants = list(protocol["variants"])
    daily = {variant: [] for variant in variants}
    benchmark = None
    benchmark_path = ROOT / "_analysis" / "market_index.pkl"
    if source_mode != "injected_test" and benchmark_path.exists():
        candidate = pd.read_pickle(benchmark_path)
        if {"date", "open", "close"}.issubset(candidate.columns):
            benchmark = candidate
    rows = [{"code": c, "name": None} for c in sorted(universe)]
    for day in sampled:
        prefixes = {c: f.iloc[max(0, indexes[c][day] - 64):indexes[c][day] + 1] for c,f in universe.items() if day in indexes[c]}
        prepared = engine.prepare(day, prefixes, rows)
        snapshot = sectors.load(sector_root or ROOT / "_analysis" / "daily_pipeline", day)
        event_cache = {}
        for variant in variants:
            selection = engine.select(day, prefixes, rows, regime={"as_of": day, "label": regimes.get(day)},
                sector_snapshot=snapshot, variant=variant, prepared=prepared)
            results = ev.evaluate_selection(selection, universe, calendar, as_of=available_end,
                indexed=indexed_execution, event_cache=event_cache, benchmark=benchmark)
            for key, record in results.items():
                record["cost_sensitivity"] = sensitivity(record, protocol["cost"]["sensitivity_multipliers"])
                if day not in regular and key != "rebound":
                    # 非定期恐慌日仍计算引擎，但不加入突破、回调的样本统计。
                    record["included_in_summary"] = False
                else:
                    record["included_in_summary"] = True
            row = {"date": day, "market_state": regimes.get(day), "scope": selection["scope"],
                   "decision_sha256": selection["decision_sha256"], "strategies": results}
            daily[variant].append(row)
            # 逐日保存可审计决定；结果主文件保留事件，避免只剩平均收益。
            _write_json(work / "decisions" / variant / f"{day}.json", selection)
        _write_json(work / "progress.json", {"completed_day": day, "completed_sessions": len(daily["base"]), "expected_sessions": len(sampled)})
    summary = {variant: {key: ev.summarize([r for r in records if r["strategies"][key]["included_in_summary"]], key, horizon)
                        for key,horizon in strategies.HORIZONS.items()} for variant,records in daily.items()}
    contrasts = {}
    for variant in variants[1:]:
        target = variant.split("_no_")[0]
        pairs = []
        for base, changed in zip(daily["base"], daily[variant]):
            if not base["strategies"][target]["included_in_summary"]:
                continue
            a, b = base["strategies"][target]["fixed_weight_net_arms"]["full"], changed["strategies"][target]["fixed_weight_net_arms"]["full"]
            pairs.append({"date": base["date"], "base_candidates": base["strategies"][target]["candidates"],
                "variant_candidates": changed["strategies"][target]["candidates"], "base_net": a, "variant_net": b,
                "base_minus_removed": a - b if a is not None and b is not None else None})
        contrasts[variant] = {"target": target, "pairs": pairs,
            "paired_mean": ev._mean([p["base_minus_removed"] for p in pairs]),
            "missing_pairs": sum(p["base_minus_removed"] is None for p in pairs),
            "interpretation": "previously_examined_history_no_optimization_or_promotion"}
    split = {key: ev.purged_split([{"date": r["date"], "label_end": r["strategies"][key]["label_end"]} for r in daily["base"]],
        protocol["validation"]["start"], protocol["validation"]["embargo_sessions"], calendar) for key in strategies.HORIZONS}
    report = {"run_id": work.name, "generated_at": life.observation_clock().isoformat(), "status": "exploratory_data_limited",
        "evaluation_version": ev.VERSION, "engine_version": engine.VERSION, "strategy_version": strategies.VERSION,
        "protocol": protocol_ref, "protocol_sha256": protocol["protocol_sha256"], "source_mode": source_mode,
        "window": {"start": start, "end": end, "step": step}, "sampled_sessions": len(sampled), "universe_files": len(universe),
        "source_errors": errors, "summary": summary, "contrasts": contrasts, "daily": daily,
        "validation_split": split, "rolling_validation": {key: ev.rolling_validation(
            [{"date": r["date"], "label_end": r["strategies"][key]["label_end"]} for r in daily["base"]],
            protocol["validation"]["start"], calendar) for key in strategies.HORIZONS},
        "calendar_basis": calendar_basis, "observation_as_of": available_end,
        "allocation_status": "blocked_publication_date_fundamentals",
        "input_manifest": {"data_dir": str(Path(data_dir or ROOT / "_analysis" / "daily").resolve()),
            "data_files": {c: sha256(Path(data_dir or ROOT / "_analysis" / "daily") / f"{c}.pkl") for c in universe}
                if source_mode != "injected_test" else {},
            "regime_sha256": sha256(ROOT / "_analysis" / "environment_report.json") if source_mode != "injected_test" else None},
        "limits": ["historical_adjustment_unverified_execution_proxy", "historical_ST_halt_limits_unknown",
            "surviving_available_pkl_universe_not_delisted_complete", "historical_sector_evidence_missing",
            "historical_environment_proxy_not_archived_as_known_then", "examined_history_not_independent_validation",
            "account_NAV_requires_step_E", "unresolved_market_cap_style_exposure"],
        "conclusion": "no_strategy_upgrade; await verified inputs and untouched forward validation"}
    _write_json(work / "report.json", report)
    _write_json(work / "run.json", {"status": "complete_research_proxy", "started_at": started,
        "finished_at": report["generated_at"], "protocol": protocol_ref,
        "report": {"path": str((work / "report.json").resolve()), "sha256": sha256(work / "report.json")}})
    _write_json(root / "latest.json", {"run_id": work.name, "report_path": str((work / "report.json").resolve()),
        "report_sha256": sha256(work / "report.json"), "status": report["status"], "summary": summary})
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--step", type=int, default=20)
    parser.add_argument("--data-dir")
    parser.add_argument("--output-root")
    parser.add_argument("--sector-root")
    args = parser.parse_args()
    report = run(args.start, args.end, args.step, args.sector_root, data_dir=args.data_dir, output_root=args.output_root)
    print(json.dumps({k:report[k] for k in ("run_id", "status", "sampled_sessions", "universe_files", "summary")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
