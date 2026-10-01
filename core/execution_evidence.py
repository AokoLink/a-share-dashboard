"""Validated, immutable evidence imports. Verification flags never manufacture missing proof."""
import copy
import math
import re
from pathlib import Path

from core import account_ledger as ledger, daily_data as data, strategy_lifecycle as life
from core.stage1_data import _write_json, sha256

VERSION = "execution-evidence-v1"
KINDS = ("trade_status", "corporate_coverage", "corporate_actions", "execution_rules")


def validate(kind, row, clock):
    if not re.fullmatch(r"\d{6}", row.get("code", "")):
        raise ValueError("invalid_evidence_code")
    if row.get("verified") is not True or not isinstance(row.get("source"), str) or not row["source"].strip():
        raise ValueError("evidence_requires_source_and_verification")
    timestamp = row.get("known_at", "")
    if not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", timestamp) or life.observation_clock(timestamp) > clock:
        raise ValueError("evidence_requires_nonfuture_timezone_timestamp")
    if kind == "corporate_actions":
        if row.get("action_type") not in ("cash_dividend", "share_distribution", "combined_distribution"):
            raise ValueError("unsupported_or_unknown_corporate_action_type")
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("corporate_event_requires_id")
        for field in ("ex_date", "pay_date"):
            life.observation_clock(row[field])
        if row["pay_date"] < row["ex_date"]:
            raise ValueError("corporate_payment_before_ex_date")
        if not life._finite(row.get("share_factor")) or row["share_factor"] < 1:
            raise ValueError("unsupported_share_factor")
        if (type(row.get("net_cash_per_share")) not in (int, float)
                or not math.isfinite(row["net_cash_per_share"]) or row["net_cash_per_share"] < 0):
            raise ValueError("corporate_requires_verified_net_cash")
    else:
        life.observation_clock(row["date"])
    if kind == "trade_status":
        for field in ("halted", "buy_open_allowed", "sell_open_allowed"):
            if type(row.get(field)) is not bool:
                raise ValueError("execution_status_requires_explicit_booleans")
        for side in ("buy", "sell"):
            cap = row.get("max_" + side + "_shares")
            if cap is not None and (type(cap) is not int or cap < 0):
                raise ValueError("invalid_execution_capacity")
    elif kind == "corporate_coverage":
        if type(row.get("complete")) is not bool:
            raise ValueError("coverage_requires_explicit_completeness")
    elif kind == "execution_rules":
        for field in ("buy_min", "buy_step", "sell_min", "sell_step"):
            if type(row.get(field)) is not int or row[field] < 1:
                raise ValueError("invalid_order_quantity_rule")
        if type(row.get("odd_lot_sell_all")) is not bool:
            raise ValueError("odd_lot_rule_unknown")
        for field in ("commission_rate", "minimum_commission", "sell_tax_rate", "transfer_rate"):
            value = row.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid_fee_rule")
            if field != "minimum_commission" and value >= 1:
                raise ValueError("invalid_fee_rate")
    refs = row.get("evidence_refs", [])
    if not refs or not all(data.intact(ref) for ref in refs):
        raise ValueError("evidence_artifact_missing_or_changed")


def import_bundle(root, bundle, *, now=None):
    clock = life.observation_clock(now)
    if set(bundle) - set(KINDS):
        raise ValueError("unknown_execution_evidence_kind")
    base = Path(root) / "execution" / "evidence"
    payload, counts = {}, {}
    for kind in KINDS:
        rows, seen = [], set()
        for original in bundle.get(kind, []):
            row = copy.deepcopy(original)
            validate(kind, row, clock)
            key = row["id"] if kind == "corporate_actions" else (row["date"], row["code"])
            if key in seen:
                raise ValueError("duplicate_evidence_key")
            seen.add(key)
            # Archive source bytes as well as the normalized row.
            refs = []
            for ref in row["evidence_refs"]:
                source_bytes = Path(ref["path"]).read_bytes()
                path = base / "sources" / ref["sha256"]
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and sha256(path) != ref["sha256"]:
                    raise ValueError("archived_evidence_integrity_failed")
                if not path.exists():
                    path.write_bytes(source_bytes)
                refs.append({"path": str(path.resolve()), "sha256": sha256(path)})
            row["evidence_refs"] = refs
            row["received_at"] = clock.isoformat()
            rows.append(row)
        payload[kind], counts[kind] = rows, len(rows)
    existing, _ = load(root)
    for kind in KINDS:
        old = {r["id"] if kind == "corporate_actions" else (r["date"], r["code"]): r for r in existing[kind]}
        for row in payload[kind]:
            key = row["id"] if kind == "corporate_actions" else (row["date"], row["code"])
            if key in old and {k: v for k, v in old[key].items() if k != "received_at"} != {
                    k: v for k, v in row.items() if k != "received_at"}:
                raise ValueError("conflicting_execution_evidence_requires_resolution")
    value = {"version": VERSION, "imported_at": clock.isoformat(), "rows": payload,
             "verification_basis": "caller_attested_source_artifacts_schema_validated_not_authority_certification"}
    path = base / "blobs" / (ledger.digest(value) + ".json")
    if not path.exists():
        _write_json(path, value)
    ref = {"path": str(path.resolve()), "sha256": sha256(path), "counts": counts}
    # Preserve all packages. A later package cannot silently overwrite conflicting rows.
    _write_json(base / "imports" / (ref["sha256"] + ".json"), ref)
    return ref


def load(root):
    base = Path(root) / "execution" / "evidence"
    out, refs, seen = {k: [] for k in KINDS}, [], {k: {} for k in KINDS}
    for index in sorted((base / "imports").glob("*.json")):
        ref = data.read_json(index)
        path = Path(ref["path"]).resolve()
        if path.parent != (base / "blobs").resolve() or not data.intact(ref):
            raise ValueError("execution_import_integrity_failed")
        payload = data.read_json(path)
        if payload.get("version") != VERSION:
            raise ValueError("unsupported_execution_evidence_version")
        for kind, rows in payload["rows"].items():
            for row in rows:
                if not all(data.intact(r) for r in row["evidence_refs"]):
                    raise ValueError("execution_source_integrity_failed")
                key = row["id"] if kind == "corporate_actions" else (row["date"], row["code"])
                semantic = {k: v for k, v in row.items() if k != "received_at"}
                previous = seen[kind].get(key)
                if previous and {k: v for k, v in previous.items() if k != "received_at"} != semantic:
                    raise ValueError("conflicting_execution_evidence_requires_resolution")
                if not previous or row["received_at"] < previous["received_at"]:
                    seen[kind][key] = row
        refs.append(ref)
    for kind in KINDS:
        out[kind] = list(seen[kind].values())
    return out, refs


def quantity(maximum, minimum, step):
    maximum = int(maximum)
    return 0 if maximum < minimum else minimum + ((maximum - minimum) // step) * step


def charges(notional, side, rule):
    commission = max(rule["minimum_commission"], notional * rule["commission_rate"])
    transfer = notional * rule["transfer_rate"]
    tax = notional * rule["sell_tax_rate"] if side == "sell" else 0.
    return commission + transfer + tax, {"commission": commission, "transfer": transfer, "sell_tax": tax}
