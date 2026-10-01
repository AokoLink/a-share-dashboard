"""后台计算完整板块集合；页面、选股和组合读取同一不可变证据。"""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from core import daily_data as data, sector_insights as insights, sector_relations as relations
from core.stage1_data import sha256

VERSION = "sector-service-v1"


def assemble(day, rows, spot, technical, benchmark, calendar, *, previous=None, generated_at=None,
             input_artifacts=None):
    indexed = {str(row["code"]): row for row in spot.to_dict("records")}
    out, links = [], []
    for original in rows:
        row = dict(original)
        row.setdefault("category", "unknown")
        row["code"] = str(row.get("code", ""))
        row["id"] = f"{row['category']}:{row['code']}"
        row.setdefault("observed_at", day)
        row.setdefault("valid_from", day)
        row.setdefault("valid_to", day)
        row["historical_provenance"] = False
        members = sorted(set(str(c) for c in row.get("members", [])))
        if any(len(c) != 6 or not c.isdigit() for c in members):
            members, row["status"] = [], "invalid_members"
        row["members"] = members
        rel = relations.relation(row, day)
        verified = rel["status"] == "verified_current"
        matched = [indexed[c] for c in members if c in indexed]
        row["breadth"] = insights.complete_breadth(matched, len(members), technical, day, calendar, verified)
        history = row.pop("history_frame", None)
        if history is not None:
            row["relative"] = insights.relative_strength(history, benchmark, as_of=day, calendar=calendar)
        else:
            row["relative"] = {"status": "sector_index_unavailable", "returns": {}, "as_of": None,
                               "benchmark": "sh000001"}
        return20 = (row["relative"].get("returns", {}).get("20") or {}).get("sector_pct")
        row["roles"] = insights.leader_roles(matched, {c: technical[c] for c in members if c in technical},
            return20, as_of=day, calendar=calendar, total_members=len(members))
        if not verified:
            row["roles"].update({"trading_core": [], "price_leaders": [], "price_status": "unverified_membership"})
        row["membership_status"] = rel["status"]
        row["as_of"] = day
        rel["coverage"] = row["breadth"]["coverage_ratio"]
        links.append(rel)
        out.append(row)
    previous_day = next((d for d in reversed(calendar) if d < day), None)
    ordered = insights.rotation(out, previous, consecutive=bool(previous and previous.get("as_of") == previous_day))
    current = sum(r["relative"].get("as_of") == day and r["relative"].get("status") == "ok" for r in ordered)
    membership = sum(r["membership_status"] == "verified_current" for r in ordered)
    return {"version": VERSION, "relation_version": relations.VERSION, "feature_version": insights.VERSION,
            "code_sha256": {"service": sha256(__file__), "relations": sha256(relations.__file__),
                            "features": sha256(insights.__file__)},
            "as_of": day, "generated_at": generated_at, "benchmark": "sh000001",
            "selection_basis": "all_available_sectors_then_relative20_rank",
            "candidate_universe": {"total": len(ordered), "index_current": current,
                "membership_verified": membership, "category_counts": {kind: sum(r["category"] == kind for r in ordered)
                   for kind in relations.CATEGORIES}, "category_missing": [kind for kind in relations.CATEGORIES
                    if not any(r["category"] == kind for r in ordered)]},
            "status": "complete" if ordered and current == len(ordered) and membership == len(ordered)
                and all(r["breadth"]["technical_status"] == "ok" for r in ordered) else "partial",
            "minimum_breadth_coverage": insights.MIN_COVERAGE, "rows": ordered, "relations": links,
            "input_artifacts": input_artifacts or [],
            "limits": ["current_members_not_historical", "available_archived_member_prices_coverage_reported",
                       "industrial_core_requires_fundamentals", "descriptive_not_validated_prediction"]}


def collect(day, summaries, sources, spot, technical, benchmark, calendar, archive, archive_frame,
            *, workers=6, previous=None, generated_at=None):
    records = summaries.to_dict("records") if summaries is not None else []
    catalog = summaries.attrs.get("catalog_evidence", {}) if summaries is not None else {}
    catalog_refs = [archive(f"sector_catalog/{kind}.json", item, "native_sector_catalog", as_of=day)
                    for kind, item in catalog.items()]
    ids = [f"{r.get('category', 'industry')}:{r['code']}" for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_sector_ids")

    def inspect(original):
        row = {"code": str(original["code"]), "name": str(original["name"]),
               "category": original.get("category", "industry"), "status": "unavailable",
               "taxonomy": original.get("taxonomy"), "parent_mapping": original.get("parent_mapping"),
               "observed_at": day, "valid_from": day, "valid_to": day, "errors": []}
        if row["category"] not in relations.CATEGORIES:
            row["errors"].append("unknown_category")
            return row
        try:
            resolved = (sources.membership_for(original) if hasattr(sources, "membership_for")
                        else sources.membership(row["name"]))
            row["membership_response"] = archive(f"sector_members/{row['category']}-{row['code']}.json",
                resolved, resolved.get("source", "membership_provider"), as_of=day)
            if not resolved.get("ok") or resolved.get("stale"):
                raise ValueError(resolved.get("reason", "membership_stale_or_unknown"))
            observed = resolved.get("observed_at", day)
            if observed[:10] != day:
                raise ValueError("membership_observed_on_different_day")
            codes = [str(c) for c in resolved.get("codes", [])]
            if any(len(c) != 6 or not c.isdigit() for c in codes) or len(set(codes)) != len(codes):
                raise ValueError("invalid_or_duplicate_member_codes")
            row.update({"members": sorted(set(str(c)[-6:] for c in resolved.get("codes", []))),
                        "source": resolved.get("source", "membership_provider"),
                        "source_name": resolved.get("source_name"), "match_type": resolved.get("match_type"),
                        "status": "current_snapshot", "observed_at": observed,
                        "complete": resolved.get("complete", False),
                        "expected_members": resolved.get("expected_members"),
                        "membership_scope": resolved.get("scope"),
                        "completion_basis": resolved.get("completion_basis", "source_count_verified"),
                        "reported_counts": resolved.get("reported_counts"),
                        "count_discrepancy": resolved.get("count_discrepancy", False)})
        except Exception as exc:
            row["errors"].append(f"membership: {type(exc).__name__}: {exc}"[:500])
        try:
            frame, stale = (sources.sector_history_for(original) if hasattr(sources, "sector_history_for")
                            else sources.sector_history(row["code"]))
            if stale:
                raise ValueError("sector_history_stale")
            if frame.attrs.get("raw_response"):
                row["index_response"] = archive(f"sector_index/{row['category']}-{row['code']}.json",
                    {"raw_response": frame.attrs["raw_response"]},
                    frame.attrs.get("source", "native_sector_index"), as_of=day)
            row["history"] = archive_frame(row, frame)
            row["history_frame"] = frame
        except Exception as exc:
            row["errors"].append(f"index: {type(exc).__name__}: {exc}"[:500])
        return row
    with ThreadPoolExecutor(max_workers=workers) as executor:
        rows = list(executor.map(inspect, records))
    payload = assemble(day, rows, spot, technical, benchmark, calendar, previous=previous,
                       input_artifacts=catalog_refs,
                       generated_at=generated_at() if callable(generated_at) else generated_at)
    payload["categories_complete"] = [kind for kind, item in catalog.items()
        if item.get("status") == "ok" and item.get("observed_at", "")[:10] == day
        and item.get("board_count") == sum(r["category"] == kind for r in rows)
        and all(r.get("status") == "current_snapshot" and r.get("complete")
                and r.get("expected_members") == len(r.get("members", []))
                and r.get("match_type") == "source_native" for r in rows if r["category"] == kind)]
    return payload


def exposure(holdings, snapshot, day):
    queried = {h["code"]: relations.lookup(snapshot, h["code"], day) for h in holdings}
    grouped = {kind: {} for kind in relations.CATEGORIES}
    for h in holdings:
        for kind in relations.CATEGORIES:
            for rel in queried[h["code"]]["by_category"][kind]:
                record = grouped[kind].setdefault(rel["id"], {"id": rel["id"], "name": rel["name"], "weight": 0.})
                record["weight"] += h["weight"]
    unknown = [h["code"] for h in holdings if queried[h["code"]]["status"] != "ok"]
    return {"by_category": {kind: sorted(rows.values(), key=lambda r: (-r["weight"], r["id"]))
                            for kind, rows in grouped.items()},
            "per_code": queried, "unknown_codes": unknown,
            "unknown_weight": sum(h["weight"] for h in holdings if h["code"] in unknown),
            "code_coverage": (len(holdings) - len(unknown)) / len(holdings) if holdings else None,
            "as_of": day, "snapshot_sha256": (snapshot or {}).get("snapshot_sha256"),
            "basis": "observed_day_only_not_historical"}
