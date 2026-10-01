"""有证据的当日板块关系；当前成分不外推为历史成分。"""
import hashlib
import json
from datetime import date
from pathlib import Path

from core.stage1_data import _write_json, sha256

VERSION = "dated-sector-relations-v1"
CATEGORIES = ("industry", "subindustry", "theme")
EXACT_MATCHES = {"exact", "exact_cross_provider", "source_native"}


def relation(row, day):
    category = row.get("category", "unknown")
    members = sorted(set(str(c) for c in row.get("members", [])))
    known = (row.get("status") == "current_snapshot" and category in CATEGORIES
             and row.get("match_type") in EXACT_MATCHES
             and (bool(members) or (row.get("complete") is True and row.get("expected_members") == 0))
             and row.get("valid_from") == day and row.get("valid_to") == day)
    return {"id": row.get("id", f"{category}:{row.get('code', '')}"),
            "category": category, "code": str(row.get("code", "")), "name": row.get("name"),
            "source": row.get("source"), "source_name": row.get("source_name"),
            "taxonomy": row.get("taxonomy"), "parent_mapping": row.get("parent_mapping"),
            "membership_scope": row.get("membership_scope"),
            "completion_basis": row.get("completion_basis"),
            "count_discrepancy": row.get("count_discrepancy", False),
            "observed_at": row.get("observed_at"), "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"), "historical_provenance": False,
            "match_type": row.get("match_type"), "members": members,
            "status": "verified_current" if known else "unverified",
            "member_count": len(members), "coverage": row.get("breadth", {}).get("coverage_ratio")}


def lookup(snapshot, code, as_of):
    """只返回指定观察日关系；近似映射单独返回，不能成为已验证归属。"""
    date.fromisoformat(as_of)
    valid = snapshot and snapshot.get("as_of") == as_of
    rows = [row for row in (snapshot or {}).get("relations", [])
            if valid and code in row.get("members", []) and isinstance(row.get("valid_from"), str)
            and isinstance(row.get("valid_to"), str) and row["valid_from"] <= as_of <= row["valid_to"]]
    rows = [{k: v for k, v in row.items() if k != "members"} for row in rows]
    verified = [row for row in rows if row["status"] == "verified_current"]
    complete = ((snapshot or {}).get("categories_complete", []) if valid and
                any(r["category"] == "industry" for r in verified) else [])
    return {"code": code, "as_of": as_of, "status": "ok" if verified or complete else "unknown",
            "categories_complete": complete,
            "relations": verified, "uncertain_relations": [r for r in rows if r not in verified],
            "by_category": {kind: [r for r in verified if r["category"] == kind] for kind in CATEGORIES},
            "snapshot_sha256": (snapshot or {}).get("snapshot_sha256"),
            "basis": "observed_day_only_not_historical"}


def feature_for(snapshot, code, as_of):
    evidence = lookup(snapshot, code, as_of)
    choices = next((evidence["by_category"][kind] for kind in ("subindustry", "industry", "theme")
                    if evidence["by_category"][kind]), [])
    features = {row["id"]: row for row in (snapshot or {}).get("rows", [])}
    used, values = [], []
    for rel in choices:
        item = features.get(rel["id"], {})
        relative = item.get("relative", {})
        value = (relative.get("returns", {}).get("20") or {}).get("excess_pct")
        if relative.get("status") == "ok" and relative.get("as_of") == as_of and value is not None:
            values.append(float(value))
            used.append(rel["id"])
    ready = bool(choices) and len(values) == len(choices)
    return {**evidence, "status": "ok" if ready else "unverified" if choices else "unknown",
            "sector_rel20": sum(values) / len(values) if ready else None,
            "selected_sector_ids": [r["id"] for r in choices], "verified_feature_ids": used,
            "aggregation": "subindustry_then_industry_then_theme_equal_mean_all_required",
            "benchmark": "sh000001", "missing_policy": "research_only_no_sector_bonus"}


def _semantic(value):
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            return {"sha256": value["sha256"], "source": value.get("source"), "as_of": value.get("as_of")}
        return {k: _semantic(v) for k, v in value.items()
                if k not in {"generated_at", "collected_at", "input_artifacts", "snapshot_sha256",
                             "sector_run_id", "origin_run_id"}}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def save(root, payload):
    """按内容保存不可变版本；同日新观察只更新索引，旧引用始终可读取。"""
    root = Path(root) / "sector_evidence"
    semantic = hashlib.sha256(json.dumps(_semantic(payload), sort_keys=True, ensure_ascii=False,
                                        allow_nan=False).encode("utf-8")).hexdigest()
    index_path = root / "days" / f"{payload['as_of']}.json"
    old = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else None
    if old and old.get("semantic_sha256") == semantic and Path(old["path"]).exists() and sha256(old["path"]) == old["sha256"]:
        return old
    content = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    blob = root / "blobs" / f"{digest}.json"
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        blob.write_bytes(content)
    ref = {"path": str(blob.resolve()), "sha256": digest, "semantic_sha256": semantic,
           "as_of": payload["as_of"], "source": VERSION}
    _write_json(index_path, ref)
    return ref


def load(root, as_of, ref=None):
    date.fromisoformat(as_of)
    try:
        index = ref or json.loads((Path(root) / "sector_evidence" / "days" / f"{as_of}.json").read_text(encoding="utf-8"))
        path = Path(index["path"])
        expected_root = (Path(root) / "sector_evidence" / "blobs").resolve()
        if path.resolve().parent != expected_root or sha256(path) != index["sha256"]:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {**payload, "snapshot_sha256": index["sha256"]} if payload["as_of"] == as_of else None
    except (OSError, ValueError, KeyError):
        return None
