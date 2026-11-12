"""Recompute a past saved daily input set under current rules, without network or live signals."""
import argparse
import base64
from datetime import date
from pathlib import Path

from core import daily_data as data, price_collection, screening_engine, sector_relations
from core import strategies, strategy_lifecycle as life, signal_replay, version_manifest, data_source
from core.stage1_data import _write_json, sha256


def run(root, day, *, now=None):
    root = Path(root).resolve()
    day = date.fromisoformat(day).isoformat()
    clock = life.observation_clock(now)
    if day >= clock.date().isoformat():
        raise ValueError("revision_requires_a_past_session")
    index = data.read_json(root / "days" / (day + ".json"))
    run_path = (root / "runs" / index["run_id"] / "run.json").resolve()
    if run_path.parent.parent != root / "runs":
        raise ValueError("invalid_run_path")
    original = data.read_json(run_path)
    if original["day"] != day:
        raise ValueError("source_day_mismatch")
    source_ref = {"path": str(run_path), "sha256": sha256(run_path)}
    universe_ref = original["steps"]["universe"]["result"]["artifact"]
    if not data.intact(universe_ref) or universe_ref.get("collected_at", "9999")[:10] > day:
        raise ValueError("universe_missing_changed_or_later_than_decision_day")
    rows = data.read_json(universe_ref["path"])["pool"]
    prices = original["steps"]["prices"]["result"]["entries"]
    technical, archived_prices = {}, {}
    for row in rows:
        code = row["code"]
        item = prices.get(code, {}).get("technical", {})
        archived_prices[code] = item
        if item.get("status") != "success":
            continue
        if item["artifact"].get("collected_at", "9999")[:10] > day:
            raise ValueError("price_input_was_collected_after_decision_day")
        technical[code] = price_collection.checked_frame(item)
    features = original["steps"].get("features", {}).get("result", {})
    sector_ref = features.get("sector_snapshot")
    snapshot = sector_relations.load(root, day, sector_ref) if sector_ref else None
    if sector_ref and snapshot is None:
        raise ValueError("sector_input_missing_changed_or_wrong_date")
    regime = features.get("regime")
    decision = screening_engine.select(day, technical, rows, regime=regime, sector_snapshot=snapshot)
    inputs = {"day": day, "rows": rows, "prices": archived_prices, "universe_ref": universe_ref,
              "source_run_ref": source_ref, "sector_snapshot_ref": sector_ref, "regime": regime}
    base = root / "strategy_revision"
    input_ref = signal_replay.immutable(base / "inputs" / (version_manifest.digest(inputs) + ".json"), inputs)
    code = {str(p.relative_to(version_manifest.ROOT)).replace("\\", "/"):
            base64.b64encode(p.read_bytes()).decode("ascii")
            for folder in ("core", "pipeline") for p in sorted((version_manifest.ROOT / folder).glob("*.py"))}
    code_ref = signal_replay.immutable(base / "code" / (version_manifest.digest(code) + ".json"), code)
    groups = {key: [{**r, "code": data_source.with_prefix(r["code"]), "actionable": False,
        "run_mode": "historical_research", "evidence_level": "retrospective_research",
        "freshness": {"status": "historical", "as_of": day}} for r in signals]
        for key, signals in decision["groups"].items()}
    missing = decision["scope"]["missing_codes"]
    report = {"generated_at": clock.isoformat(), "data_as_of": day, "status": "retrospective_research",
        "research_kind": "momentum_rule_revision", "retrospective": True, "forward_signal": False,
        "actionable": False, "read_only": True, "version": strategies.VERSION,
        "frozen_version": life.screening_identity()["version"], "cards": strategies.CARDS,
        "groups": groups, "snapshot_ids": {}, "freeze_status": "retrospective_not_frozen",
        "market_state": decision["market_state"], "screening_decision": decision,
        "input_manifest": input_ref, "code_ref": code_ref, "version_manifest": version_manifest.manifest(),
        "scope": decision["scope"], "diagnostics": {"daily_failed": len(missing), "failed_codes": missing,
            "pool_scanned": len(rows), "stale_source": True},
        "limits": ["使用原日期留档输入按当前规则复核；不是当时已生成的信号",
            "该日期参与过策略讨论，仅作规则行为核验，不作为独立收益验证",
            "新闻、财报与龙虎榜未评分；公告及次日实际成交能力仍待核实",
            "同日行业热度只使用可核验留档；缺失时标注待核实，不据此认定热点",
            "原候选与旧快照保留，不补记订单或账户收益"]}
    ref = signal_replay.immutable(root / "research_candidates" / "blobs" /
        (version_manifest.digest(report) + ".json"), report)
    _write_json(base / "days" / (day + ".json"), ref)
    return {"report_ref": ref, "counts": {k: len(v) for k, v in groups.items()},
            "generated_at": report["generated_at"], "forward_signal": False}


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.date), ensure_ascii=False))


if __name__ == "__main__":
    main()
