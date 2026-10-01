"""Prospective validation: register before signal close, bind immutable live snapshots."""
import json
from pathlib import Path

from core import account_ledger as ledger, daily_data as data, strategy_lifecycle as life
from core import strategies, version_manifest
from core.stage1_data import _write_json, sha256, load_calendar

VERSION = "prospective-validation-v1"


def code_identity():
    manifest = version_manifest.manifest()
    return {"screening_version": life.screening_identity()["version"],
            "layers": {k: manifest["layers"][k]["sha256"] for k in
                       ("screening", "data_features", "execution_evaluation")},
            "validation_code": sha256(__file__)}


def register(root, *, now=None):
    clock = life.observation_clock(now)
    calendar = load_calendar(Path(root) / "calendar.json")
    sessions = (calendar or {}).get("dates", [])
    closed = [d for d in sessions if life.observation_clock(d + "T15:00:00+08:00") < clock]
    development_end = max(closed) if closed else None
    future = [d for d in sessions if development_end and d > development_end]
    start = future[10] if len(future) > 10 else None
    calendar_ref = None
    if calendar:
        content = (Path(root) / "calendar.json").read_bytes()
        calendar_path = Path(root) / "forward_validation" / "calendars" / (sha256(Path(root) / "calendar.json") + ".json")
        calendar_path.parent.mkdir(parents=True, exist_ok=True)
        if not calendar_path.exists():
            calendar_path.write_bytes(content)
        calendar_ref = {"path": str(calendar_path.resolve()), "sha256": sha256(calendar_path)}
    policy = {"version": VERSION, "registered_at": clock.isoformat(), "identity": code_identity(),
        "validation_start": start, "development_label_end": development_end,
        "calendar_ref": calendar_ref,
        "horizons": strategies.HORIZONS,
        "account_config": ledger.config({"mode": "verified_inputs"}),
        "cost": {"version": life.COST_MODEL_VERSION, "fee": life.FEE_PER_SIDE,
                 "slippage": life.SLIPPAGE_PER_SIDE, "sensitivity": [0., .5, 1., 1.5, 2.]},
        "random_draws": 50, "seed": 17,
        "primary_metric": "event_cluster_equal_weight_fixed_slot_net_excess_vs_same_day_pool",
        "controls": ["same_pool_equal", "same_sector_equal", "same_sector_random"],
        "release": "monthly_descriptive_mature_labels_only", "embargo_sessions": 10,
        "stop": ["no_tuning_on_validation", "retain_negative_and_missing", "no_automatic_promotion"],
        "missing_policy": "unknown_not_zero; frozen_choices_never_reselected"}
    base = Path(root) / "forward_validation"
    active = load(root)
    if active and active[0]["identity"] == policy["identity"]:
        return active
    path = base / "registry" / (ledger.digest(policy) + ".json")
    _write_json(path, policy)
    ref = {"path": str(path.resolve()), "sha256": sha256(path)}
    _write_json(base / "active.json", ref)
    return policy, ref


def load(root, ref=None):
    try:
        ref = ref or data.read_json(Path(root) / "forward_validation" / "active.json")
        path = Path(ref["path"]).resolve()
        if path.parent != (Path(root) / "forward_validation" / "registry").resolve() or not data.intact(ref):
            return None
        policy = data.read_json(path)
        if policy.get("version") != VERSION or (policy.get("calendar_ref") and not data.intact(policy["calendar_ref"])):
            return None
        return policy, ref
    except (OSError, KeyError, ValueError):
        return None


def binding(root, day, sealed_at, source_mode):
    active = load(root)
    if not active or source_mode != "live":
        return None
    policy, ref = active
    close = life.observation_clock(day + "T15:00:00+08:00")
    if (not policy["validation_start"] or day < policy["validation_start"]
            or policy["identity"] != code_identity() or life.observation_clock(policy["registered_at"]) >= close
            or life.observation_clock(sealed_at).date().isoformat() != day):
        return None
    return ref


def eligibility(snapshot, root, policy, ref):
    day, payload = snapshot["signal_date"], snapshot["payload"]
    frozen = life.observation_clock(snapshot["frozen_at"])
    if not policy["validation_start"] or day < policy["validation_start"]:
        return "validation_embargo_or_calendar_unavailable"
    if payload.get("forward_protocol_ref") != ref:
        return "not_bound_at_freeze"
    close = life.observation_clock(day + "T15:00:00+08:00")
    if life.observation_clock(policy["registered_at"]) >= close or frozen < close or frozen.date().isoformat() != day:
        return "not_prospective_same_day_freeze"
    if snapshot["version"] != policy["identity"]["screening_version"]:
        return "screening_version_changed"
    if ledger.digest(payload) != snapshot.get("content_sha256"):
        return "snapshot_content_changed"
    replay = payload.get("replay_ref")
    if not replay or not data.intact(replay):
        return "live_replay_archive_missing_or_changed"
    archive = data.read_json(replay["path"])
    if archive.get("source") != "live":
        return "not_live_archive"
    if not all(data.intact(archive.get(k, {})) for k in ("code", "inputs")):
        return "live_replay_dependencies_changed"
    versions = archive.get("versions", {}).get("layers", {})
    if any(versions.get(k, {}).get("sha256") != digest for k, digest in policy["identity"]["layers"].items()):
        return "live_replay_code_does_not_match_registered_policy"
    from core import signal_replay
    expected = signal_replay.decode(archive.get("expected", {}))
    if expected.get("decision") != payload.get("screening_decision"):
        return "frozen_decision_does_not_match_live_replay"
    if expected.get("groups", {}).get(snapshot["strategy"]) != payload.get("candidates"):
        return "frozen_candidates_differ_from_live_replay"
    return None
