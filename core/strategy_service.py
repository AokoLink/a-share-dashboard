"""后台策略生成与冻结：网页只读取持久化结果。"""
from core import data_source as ds, store, strategies, strategy_lifecycle as lifecycle
from core.stage1_data import sha256
from core import sector_relations, screening_engine, signal_replay, version_manifest, forward_validation
from pathlib import Path


def generate(db, pool, technical, execution, memberships, regime, now, *, allow_freeze=True,
             quality=None, shadow=False, record_time=None, sector_snapshot=None, sector_snapshot_ref=None,
             replay_root=None, source_mode="frozen_signal"):
    identity = lifecycle.screening_identity()
    statuses = store.get_strategy_statuses(db, identity["version"], strategies.HORIZONS)
    modes = lifecycle.run_modes(statuses, observe_paused=shadow)
    day = now.date().isoformat()
    decision = screening_engine.select(day, technical, pool, regime=regime, sector_snapshot=sector_snapshot)
    shadow_groups = {key: [] for key in strategies.HORIZONS}
    groups = {key: [] for key in strategies.HORIZONS}
    frozen_pool, errors = [], list(decision["scope"]["missing_codes"])
    state = decision["market_state"]
    for row in decision["pool"]:
        code = row["code"]
        raw, failure = screening_engine.day_view(execution.get(code), day)
        if failure:
            errors.append(code)
            continue
        raw_close = float(raw["close"].iloc[-1])
        tech, _ = screening_engine.day_view(technical[code], day)
        evidence = decision["sector_evidence"][code]
        frozen_pool.append({"code": code, "name": row["name"], "signal_date": day,
            "signal_close": raw_close, "technical_signal_close": float(tech["close"].iloc[-1]),
            "amount": row["amount"], "sectors": [r["name"] for r in evidence["relations"]],
            "sector_evidence": evidence})
    closes = {r["code"]: r for r in frozen_pool}
    for key, signals in decision["groups"].items():
        for signal in signals:
            code = signal["code"]
            if code not in closes:
                continue
            bar = closes[code]
            shadow_groups[key].append({**signal, "code": ds.with_prefix(code),
                "signal_close": bar["signal_close"], "technical_signal_close": bar["technical_signal_close"],
                "actionable": False, "freshness": {"status": "current", "as_of": day}, "run_mode": "shadow"})
    sealed_at = record_time() if record_time else now
    day_changed = sealed_at.date().isoformat() != day
    statuses = store.get_strategy_statuses(db, identity["version"], strategies.HORIZONS)
    modes = lifecycle.run_modes(statuses, observe_paused=shadow)
    for key in strategies.HORIZONS:
        if statuses[key] == "retired":
            shadow_groups[key] = []
        if day_changed:
            for item in shadow_groups[key]:
                item["freshness"]["status"] = "historical"
        groups[key] = [{**item, "run_mode": modes[key]} for item in shadow_groups[key]] if modes[key] != "disabled" else []
    ids = {}
    freeze_status = "skipped_quality" if not allow_freeze or errors else "skipped_status"
    if day_changed:
        freeze_status = "skipped_day_changed"
    if allow_freeze and not errors and not shadow and not day_changed:
        replay_ref = signal_replay.capture(replay_root or Path(db).resolve().parent / "signal_replay",
            pool=pool, technical=technical, execution=execution, regime=regime, now=now,
            sector_snapshot=sector_snapshot, decision=decision,
            groups={k: groups[k] for k in strategies.HORIZONS}, status_events=store.list_strategy_status_events(db),
            memberships=memberships, source=source_mode)
        common = {"data_as_of": day, "market_state": state, "pool": sorted(frozen_pool, key=lambda x: x["code"]),
                  "replay_ref": replay_ref, "version_manifest": version_manifest.manifest(),
                  **{key: value for key, value in identity.items() if key != "version"},
                  "strategy_definition_version": strategies.VERSION,
                  "service_code_sha256": sha256(__file__), "price_basis": "verified_unadjusted_raw",
                  "technical_price_basis": "source_qfq_archived",
                  "market_state_basis": (regime or {}).get("basis", "unknown"),
                  "market_state_quality": (regime or {}).get("status", "unavailable"),
                  "membership_basis": "current_source_snapshot_not_historical",
                  "sector_snapshot_ref": sector_snapshot_ref,
                  "forward_protocol_ref": forward_validation.binding(
                      Path(replay_root).parent if replay_root else Path(db).resolve().parent,
                      day, sealed_at, source_mode),
                  "sector_feature_version": (sector_snapshot or {}).get("feature_version"),
                  "screening_decision": decision,
                  "quality": {"daily_failed": len(errors), "pool_scanned": len(pool),
                              "sector_coverage": sum(bool(row["sectors"]) for row in frozen_pool),
                              "regime_current": state is not None, **(quality or {})}}
        for key in strategies.HORIZONS:
            if modes[key] != "normal":
                continue
            ids[key] = store.freeze_strategy_signal(db, day, key, identity["version"], sealed_at.isoformat(),
                {**common, "strategy": key, "horizon_sessions": strategies.HORIZONS[key],
                 "candidates": groups[key]})["id"]
        if ids:
            freeze_status = "frozen"
    return {"generated_at": sealed_at.isoformat(), "data_as_of": day,
            "version": strategies.VERSION, "frozen_version": identity["version"],
            "status": "suspended" if all(mode == "disabled" for mode in modes.values()) else "exploratory",
            "actionable": False, "cards": strategies.CARDS, "groups": groups, "shadow_groups": shadow_groups,
            "strategy_statuses": statuses, "run_modes": modes, "snapshot_ids": ids,
            "freeze_status": "shadow_not_frozen" if shadow else freeze_status,
            "market_state": state, "screening_decision": decision, "diagnostics": {"daily_failed": len(errors), "failed_codes": errors,
                "stale_source": not allow_freeze or bool(errors) or day_changed, "pool_scanned": len(pool)}}
