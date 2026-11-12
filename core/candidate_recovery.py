"""Explicit retrospective candidates; no database writes or live signal freezing."""
import base64
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from core import daily_data as data, screening_engine, strategies, strategy_lifecycle, version_manifest
from core import price_collection, report_cache, signal_replay, data_source
from core.stage1_data import _write_json, load_calendar


def universe_before(root, day):
    choices = []
    for path in (Path(root) / "runs").glob("*/universe.json"):
        value = report_cache.read(path)
        if value.get("day", "9999") <= day:
            choices.append((value["day"], path.stat().st_mtime_ns, path))
    if not choices:
        raise ValueError("no_archived_universe_before_requested_date")
    _, _, path = max(choices)
    ref = report_cache.read(path.parent / "run.json", project=lambda r:
        r["steps"]["universe"]["result"]["artifact"], projection_key="recovery-universe-ref-v1")
    if Path(ref["path"]).resolve() != path.resolve() or not data.intact(ref):
        raise ValueError("archived_universe_hash_mismatch")
    if ref.get("collected_at", "9999")[:10] > day:
        raise ValueError("universe_was_not_known_before_research_day")
    return report_cache.read(path, ref["sha256"]), ref


def run(root, day, *, workers=8, resume=None, sources=None, now=None):
    root = Path(root).resolve()
    clock = strategy_lifecycle.observation_clock(now)
    target = date.fromisoformat(day)
    if target >= clock.date():
        raise ValueError("retrospective_date_must_be_before_today")
    calendar = load_calendar(root / "calendar.json")
    if not calendar or not calendar["first"] <= day <= calendar["last"] or day not in calendar["dates"]:
        raise ValueError("requested_date_is_not_a_verified_trading_session")
    universe, universe_ref = universe_before(root, day)
    rows = [{"code": str(r["code"]), "name": r.get("name"), "name_as_of": universe["day"]}
            for r in universe["pool"]]
    if not rows or len({r["code"] for r in rows}) != len(rows):
        raise ValueError("invalid_archived_scope")
    identity = strategy_lifecycle.screening_identity()["version"]
    job_id = str(uuid.UUID(resume)) if resume else str(uuid.uuid4())
    work = root / "research_candidates" / "runs" / job_id
    work.mkdir(parents=True, exist_ok=True)
    job_path = work / "job.json"
    if resume:
        old = data.read_json(job_path)
        if old["day"] != day or old["screening_version"] != identity or old["universe_ref"] != universe_ref:
            raise ValueError("resume_requires_same_research_date_scope_and_rules")
    job = {"job_id": job_id, "day": day, "status": "collecting", "requested_at": clock.isoformat(),
           "screening_version": identity, "universe_ref": universe_ref, "requested": len(rows),
           "kind": "retrospective_research", "forward_signal": False}
    _write_json(job_path, job)
    timestamp = lambda: strategy_lifecycle.observation_clock(now).isoformat()
    source = sources or data.LiveSources()
    collector = price_collection.PriceCollector(work / "isolated_cache", work, source, calendar["dates"],
        day, (target - timedelta(days=400)).isoformat(), "retrospective_research", timestamp)
    checkpoint = work / "price_checkpoint.json"
    entries = data.read_json(checkpoint) if checkpoint.exists() else {}
    def fetch(code):
        old = entries.get(code)
        if not old:
            path = root / "price_cache" / "live" / "technical" / (code + ".json")
            try:
                old = data.read_json(path)["item"]
            except (OSError, ValueError, KeyError):
                old = None
        return collector.collect(code, "technical", old)
    written = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch, row["code"]): row["code"] for row in rows}
        for i, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            entries[code] = future.result()
            if time.monotonic() - written >= 5 or i == len(rows):
                _write_json(checkpoint, entries)
                job.update(completed=i, failed=sum(r.get("status") != "success" for r in entries.values()),
                           updated_at=timestamp())
                _write_json(job_path, job)
                print(json.dumps({"job_id": job_id, "day": day, "completed": i,
                                  "expected": len(rows), "failed": job["failed"]}), flush=True)
                written = time.monotonic()
    technical = {}
    for code, item in entries.items():
        if item.get("status") == "success":
            technical[code] = price_collection.checked_frame(item)
    # No historical environment or day-exact membership is inferred from later snapshots.
    decision = screening_engine.select(day, technical, rows, regime=None, sector_snapshot=None)
    groups = {key: [{**r, "code": data_source.with_prefix(r["code"]),
        "name_as_of": universe["day"], "actionable": False, "run_mode": "historical_research",
        "evidence_level": "retrospective_research", "freshness": {"status": "historical", "as_of": day},
        "earliest_execution": "历史补算，不生成订单", "invalidation": "未核实目标日ST状态、市场环境和同日板块证据",
        "existing_account_holding": None} for r in signals] for key, signals in decision["groups"].items()}
    manifest = {"day": day, "retrieved_at": timestamp(), "universe_ref": universe_ref,
                "prices": {code: item for code, item in entries.items()}, "rows": rows}
    input_ref = signal_replay.immutable(work / "input_manifests" / (version_manifest.digest(manifest) + ".json"), manifest)
    code = {str(p.relative_to(version_manifest.ROOT)).replace("\\", "/"):
            base64.b64encode(p.read_bytes()).decode("ascii")
            for folder in ("core", "pipeline") for p in sorted((version_manifest.ROOT / folder).glob("*.py"))}
    code_ref = signal_replay.immutable(work / "code" / (version_manifest.digest(code) + ".json"), code)
    missing = decision["scope"]["missing_codes"]
    report = {"generated_at": timestamp(), "data_as_of": day, "status": "retrospective_research_partial" if missing else "retrospective_research",
        "retrospective": True, "forward_signal": False, "actionable": False, "read_only": True,
        "version": strategies.VERSION, "frozen_version": identity, "cards": strategies.CARDS,
        "groups": groups, "snapshot_ids": {}, "freeze_status": "retrospective_not_frozen", "market_state": None,
        "input_manifest": input_ref, "code_ref": code_ref, "version_manifest": version_manifest.manifest(),
        "screening_decision": decision, "scope": {**decision["scope"], "universe_as_of": universe["day"]},
        "limits": ["历史补算于实际生成时间，不是目标日已封印的前向信号",
            "使用此前留档股票范围；目标日全市场股票池及ST变化未完整核实",
            "前复权行情为补算时来源版本，可能包含后来修订",
            "目标日市场环境及同日板块证据缺失，不推断环境，不加板块加分",
            "未核实未复权成交价格与可成交性；不生成订单，不记账户收益"],
        "diagnostics": {"daily_failed": len(missing), "failed_codes": missing, "stale_source": True,
            "pool_scanned": len(rows), "history_gap_inputs": sum(bool(i.get("quality", {}).get("missing_sessions")) for i in entries.values())}}
    digest = version_manifest.digest(report)
    ref = signal_replay.immutable(root / "research_candidates" / "blobs" / (digest + ".json"), report)
    _write_json(root / "research_candidates" / "days" / (day + ".json"), ref)
    job.update(status="complete", report_ref=ref, counts={k: len(v) for k,v in groups.items()}, finished_at=timestamp())
    _write_json(job_path, job)
    return {"job_id": job_id, "report_ref": ref, "status": report["status"], "counts": job["counts"],
            "scope": report["scope"], "forward_signal": False}
