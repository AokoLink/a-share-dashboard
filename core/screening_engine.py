"""在线、留档重放和历史研究共用的 T 日筛选顺序。"""
import hashlib
import json

import numpy as np
import pandas as pd

from core import strategies, sector_relations

VERSION = "daily-screening-engine-v1"
POOL_N = 200
TOP_N = 20


def acquisition_scope(rows, excluded=()):
    """只做无需历史的必要过滤，不截断；资格确认后才取流动性前 N。"""
    excluded = set(excluded)
    accepted, rejected = [], {}
    for original in rows:
        row = dict(original)
        code = str(row.get("code", ""))
        reason = None
        if len(code) != 6 or not code.isdigit():
            reason = "invalid_code"
        elif code in excluded:
            reason = "excluded_new_listing"
        elif "ST" in str(row.get("name") or "").upper():
            reason = "st"
        elif strategies._finite(row.get("amount")) is None or float(row["amount"]) < strategies.MIN_AMOUNT:
            reason = "low_or_unknown_liquidity"
        if reason:
            rejected[code] = reason
        else:
            accepted.append(row)
    if len({r["code"] for r in accepted}) != len(accepted):
        raise ValueError("duplicate_universe_codes")
    return sorted(accepted, key=lambda r: (-float(r["amount"]), r["code"])), rejected


def day_view(frame, day):
    if frame is None or frame.empty:
        return None, "missing_daily"
    if "date" not in frame:
        return None, "missing_date_field"
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if dates.isna().any():
        return None, "invalid_dates"
    view = frame.loc[dates <= day].copy()
    view["date"] = dates[dates <= day]
    if view.empty or str(view["date"].iloc[-1]) != day:
        return None, "missing_decision_bar"
    if view["date"].duplicated().any() or not view["date"].is_monotonic_increasing:
        return None, "duplicate_or_reverse_dates"
    return view.reset_index(drop=True), None


def eligibility(view, code, name=None):
    if not {"close", "high", "volume", "amount"}.issubset(view.columns):
        return False, "missing_daily_fields", []
    ok, reason, caveats = strategies.base_eligibility(view, code, name)
    if not ok:
        return ok, reason, caveats
    tail = view.tail(65)
    for key in ("close", "high", "volume"):
        values = pd.to_numeric(tail[key], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any() or key != "volume" and (values <= 0).any():
            return False, "invalid_history", caveats
    return True, None, caveats


def prepare(day, technical, rows=None, *, excluded=(), pool_n=POOL_N):
    records = rows if rows is not None else [{"code": c, "name": None} for c in sorted(technical)]
    if len({r["code"] for r in records}) != len(records):
        raise ValueError("duplicate_universe_codes")
    decisions, views, eligible = {}, {}, []
    excluded = set(excluded)
    for original in sorted(records, key=lambda r: str(r["code"])):
        code, name = str(original["code"]), original.get("name")
        if original.get("name_as_of") and original["name_as_of"] > day:
            name = None
        view, failure = day_view(technical.get(code), day)
        if code in excluded:
            ok, reason, caveats = False, "excluded_new_listing", []
        elif failure:
            ok, reason, caveats = False, failure, []
        else:
            ok, reason, caveats = eligibility(view, code, name)
        record = {"code": code, "name": name, "eligible": ok, "reason": reason, "caveats": caveats,
                  "data_status": "unavailable" if failure or reason in ("invalid_history", "missing_daily_fields") else "available", "as_of": day,
                  "amount": float(view["amount"].iloc[-1]) if ok else None}
        record["vol20"] = float(pd.to_numeric(view["close"].tail(21)).pct_change().dropna().std(ddof=0)) if ok else None
        decisions[code] = record
        if ok:
            views[code] = view
            eligible.append(record)
    eligible.sort(key=lambda r: (-r["amount"], r["code"]))
    pool = eligible[:pool_n]
    return {"day": day, "pool_n": pool_n, "eligibility": decisions, "views": views, "eligible": eligible, "pool": pool, "records": records}


def select(day, technical, rows=None, *, excluded=(), regime=None, sector_snapshot=None,
           pool_n=POOL_N, top_n=TOP_N, variant="base", prepared=None):
    if pool_n < 1 or top_n < 1:
        raise ValueError("pool_n_and_top_n_must_be_positive")
    if variant not in strategies.VARIANTS:
        raise ValueError("unknown_variant")
    prepared = prepared or prepare(day, technical, rows, excluded=excluded, pool_n=pool_n)
    if prepared["day"] != day or prepared["pool_n"] != pool_n:
        raise ValueError("prepared_day_or_pool_mismatch")
    decisions = {c: dict(r) for c,r in prepared["eligibility"].items()}
    views, eligible, pool, records = prepared["views"], prepared["eligible"], prepared["pool"], prepared["records"]
    state = regime.get("label") if regime and regime.get("as_of") == day else None
    groups = {key: [] for key in strategies.HORIZONS}
    evidence = {}
    for record in pool:
        code = record["code"]
        evidence[code] = sector_relations.feature_for(sector_snapshot, code, day)
        result = strategies.screen(views[code], code, record["name"], state,
                                   evidence[code]["sector_rel20"], variant=variant)
        for signal in result["signals"]:
            groups[signal["strategy"]].append({**signal, "code": code, "name": record["name"],
                "sector_evidence": evidence[code], "sector_condition": "not_applicable"
                    if signal["strategy"] != "breakout" or variant == "breakout_no_sector" else
                    "applied" if evidence[code]["status"] == "ok" else "unverified"})
    for key, signals in groups.items():
        signals.sort(key=lambda r: (-r["score"], r["code"]))
        groups[key] = signals[:top_n]
    selected = {r["code"] for r in pool}
    for r in eligible:
        decisions[r["code"]]["pool_status"] = "selected" if r["code"] in selected else "below_pool_cutoff"
    payload = {"engine_version": VERSION, "strategy_version": strategies.VERSION, "day": day,
               "variant": variant, "parameters": {"pool_n": pool_n, "top_n": top_n},
               "market_state": state, "eligibility": decisions, "pool": pool, "groups": groups,
               "sector_evidence": evidence, "scope": {"expected": len(records), "eligible": len(eligible),
                   "selected": len(pool), "missing_codes": [c for c,r in decisions.items() if r["data_status"] == "unavailable"]}}
    payload["decision_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    return payload


def compare(left, right):
    differences = []
    for key in ("day", "variant", "parameters", "market_state", "eligibility", "pool", "groups", "sector_evidence"):
        if left.get(key) != right.get(key):
            differences.append({"field": key, "left": left.get(key), "right": right.get(key)})
    return {"status": "identical" if not differences else "different", "differences": differences,
            "left_sha256": left["decision_sha256"], "right_sha256": right["decision_sha256"],
            "missing_left": left["scope"]["missing_codes"], "missing_right": right["scope"]["missing_codes"]}
