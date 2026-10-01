"""同日、同池、同成本的研究核算；未知成交不删除、不当现金。"""
from collections import Counter

import numpy as np
import pandas as pd

from core import strategies, strategy_lifecycle as life

VERSION = "same-day-research-v1"


def _mean(values):
    values = [v for v in values if v is not None]
    return float(np.mean(values)) if values else None


def execution_index(execution):
    indexed = {}
    for code, frame in execution.items():
        if frame is None or not {"date", "open", "close"}.issubset(frame.columns):
            indexed[code] = None
            continue
        data = frame[["date", "open", "close"]].copy()
        data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
        indexed[code] = False if data["date"].duplicated().any() else data.set_index("date")
    return indexed


def execution_record(code, execution, day, calendar, horizon, as_of=None, cost_scale=1., indexed=None):
    calendar = [str(d)[:10] for d in calendar]
    as_of = as_of or calendar[-1]
    record = {"code": code, "signal_date": day, "gross_return": None, "net_return": None,
              "cost_scale": cost_scale, "entry_day": None, "exit_day": None, "label_end": None}
    if day not in calendar or calendar.index(day) + horizon + 1 >= len(calendar):
        return {**record, "status": "calendar_uncovered"}
    index = calendar.index(day)
    entry_day, exit_day = calendar[index + 1], calendar[index + horizon + 1]
    record.update(entry_day=entry_day, exit_day=exit_day, label_end=exit_day)
    if entry_day > as_of:
        return {**record, "status": "pending_entry"}
    frame = (indexed or execution_index({code: execution.get(code)})).get(code)
    if frame is None:
        return {**record, "status": "missing_execution_data"}
    if frame is False:
        return {**record, "status": "duplicate_execution_dates"}
    def price(date, field):
        return strategies._finite(frame.loc[date, field]) if date in frame.index and date <= as_of else None
    close, entered = price(day, "close"), price(entry_day, "open")
    allowed, reason = strategies.execution_check(close, entered)
    if not allowed:
        return {**record, "status": "cancelled_gap" if reason == "gap_above_5pct" else "missing_entry_price",
                "entry_price": entered, "signal_close": close}
    record.update(entry_price=entered, signal_close=close)
    if exit_day > as_of:
        return {**record, "status": "holding"}
    exited = price(exit_day, "open")
    if exited is None or exited <= 0:
        return {**record, "status": "missing_exit_price"}
    fee, slip = life.FEE_PER_SIDE * cost_scale, life.SLIPPAGE_PER_SIDE * cost_scale
    gross = exited / entered - 1
    net = exited * (1 - slip) * (1 - fee) / (entered * (1 + slip) * (1 + fee)) - 1
    return {**record, "status": "completed", "exit_price": exited, "gross_return": gross,
            "net_return": net, "cost_drag": gross - net}


def arm(records):
    completed = [r for r in records if r["status"] == "completed"]
    known = all(r["status"] in ("completed", "cancelled_gap") for r in records)
    gross = _mean([r["gross_return"] for r in completed])
    net = _mean([r["net_return"] for r in completed])
    return {"selected": len(records), "completed": len(completed), "status_counts": dict(Counter(r["status"] for r in records)),
            "conditional_gross": gross, "conditional_net": net,
            "fixed_weight_gross_proxy": sum(r["gross_return"] or 0 for r in records) / len(records) if known and records else None,
            "fixed_weight_net_proxy": sum(r["net_return"] or 0 for r in records) / len(records) if known and records else None,
            "data_complete": known, "events": records,
            "basis": "equal_slots_cash_if_gap_cancelled_unknown_not_zero_not_account_nav"}


def evaluate_selection(selection, execution, calendar, *, as_of=None, random_draws=50, seed=17,
                       cost_scale=1., benchmark=None, indexed=None, event_cache=None):
    day = selection["day"]
    pool = [r["code"] for r in selection["pool"]]
    sectors = {c: {r["id"] for r in selection["sector_evidence"][c]["relations"]} for c in pool}
    results = {}
    for key, horizon in strategies.HORIZONS.items():
        candidates = selection["groups"][key]
        selected = [r["code"] for r in candidates]
        cache_key = (day, horizon, as_of, cost_scale, tuple(pool))
        event = event_cache.get(cache_key) if event_cache is not None else None
        if event is None:
            event = {c: execution_record(c, execution, day, calendar, horizon, as_of, cost_scale, indexed) for c in pool}
            if event_cache is not None:
                event_cache[cache_key] = event
        selected_sectors = set().union(*(sectors[c] for c in selected)) if selected else set()
        sector_codes = [c for c in pool if sectors[c] & selected_sectors]
        rng = np.random.default_rng(seed + int(day.replace("-", "")) + horizon)
        # 随机名单在 T 日的板块池上抽取；不先剔除事后无法成交或缺少退出价的股票。
        draws = [sorted(rng.choice(sector_codes, size=len(selected), replace=False).tolist())
                 for _ in range(random_draws)] if selected and len(sector_codes) >= len(selected) else []
        arms = {"full": arm([event[c] for c in selected]), "market_equal": arm(list(event.values())),
                "sector_equal": arm([event[c] for c in sector_codes])}
        if key == "pullback":
            arms["same_environment_breakout"] = arm([event[r["code"]] for r in selection["groups"]["breakout"]])
        random_arms = [arm([event[c] for c in draw]) for draw in draws]
        arms["sector_random"] = {"draws": len(draws), "sampled_codes": draws,
            "conditional_gross": _mean([a["conditional_gross"] for a in random_arms]),
            "conditional_net": _mean([a["conditional_net"] for a in random_arms]),
            "fixed_weight_gross_proxy": _mean([a["fixed_weight_gross_proxy"] for a in random_arms])
                if random_arms and all(a["data_complete"] for a in random_arms) else None,
            "fixed_weight_net_proxy": _mean([a["fixed_weight_net_proxy"] for a in random_arms])
                if random_arms and all(a["data_complete"] for a in random_arms) else None,
            "data_complete": bool(random_arms) and all(a["data_complete"] for a in random_arms),
            "status_counts": dict(Counter(r["status"] for a in random_arms for r in a["events"]))}
        def exposure(codes):
            return {"amount": _mean([selection["eligibility"][c]["amount"] for c in codes]),
                    "vol20": _mean([selection["eligibility"][c].get("vol20") for c in codes]),
                    "count": len(codes)}
        wide = execution_record("sh000001", {"sh000001": benchmark}, day, calendar, horizon, as_of, cost_scale) if benchmark is not None else None
        results[key] = {"candidates": len(candidates), "ranked_codes": selected,
            "selected_sectors": len(selected_sectors), "executed": arms["full"]["completed"],
            "eligible_pool": len(pool), "arms": arms, "label_end": event[pool[0]]["label_end"] if pool else None,
            "sector_evidence": selection["sector_evidence"], "sector_snapshot_sha256": next(
                (r.get("snapshot_sha256") for r in selection["sector_evidence"].values()), None),
            "exposure": {"selected": exposure(selected), "pool": exposure(pool), "market_cap": "unavailable"},
            "benchmark_reference": {"record": wide, "basis": "sh000001_index_proxy_not_investable_all_A"},
            "gross_arms": {name: a["conditional_gross"] for name,a in arms.items()},
            "net_arms": {name: a["conditional_net"] for name,a in arms.items()},
            "fixed_weight_net_arms": {name: a["fixed_weight_net_proxy"] for name,a in arms.items()},
            "cost_model_version": life.COST_MODEL_VERSION, "cost_scale": cost_scale,
            **{name: a["conditional_net"] for name,a in arms.items()}}
    return results


def uncertainty(values, seed=17, draws=1000):
    values = np.asarray([v for v in values if v is not None], dtype=float)
    if len(values) < 2:
        return {"n": len(values), "mean": _mean(values.tolist()), "interval95": None, "status": "insufficient_independent_events"}
    rng = np.random.default_rng(seed)
    means = np.mean(rng.choice(values, size=(draws, len(values)), replace=True), axis=1)
    return {"n": len(values), "mean": float(values.mean()),
            "interval95": [float(v) for v in np.quantile(means, [.025, .975])],
            "status": "descriptive_event_cluster_bootstrap_not_significance_proof"}


def summarize(daily, strategy, horizon):
    rows = [r for r in daily if r["strategies"][strategy]["candidates"]]
    clusters = []
    for row in rows:
        label_end = row["strategies"][strategy]["label_end"]
        if not clusters or row["date"] > clusters[-1]["end"]:
            clusters.append({"end": label_end or row["date"], "rows": []})
        clusters[-1]["rows"].append(row)
        clusters[-1]["end"] = max(clusters[-1]["end"], label_end or row["date"])
    def spread(row, metric="fixed_weight_net_arms"):
        arms = row["strategies"][strategy][metric]
        return arms["full"] - arms["market_equal"] if arms["full"] is not None and arms["market_equal"] is not None else None
    values = [_mean([spread(r) for r in c["rows"]]) for c in clusters]
    grouped = {}
    for state in sorted({str(r.get("market_state")) for r in rows}):
        group = [r for r in rows if str(r.get("market_state")) == state]
        grouped[state] = {"signal_days": len(group), "mean_fixed_net_excess": _mean([spread(r) for r in group])}
    return {"signal_days": len(rows), "event_clusters": len(clusters),
            "candidate_count": sum(r["strategies"][strategy]["candidates"] for r in rows),
            "execution_status_counts": dict(Counter(e["status"] for r in rows for e in r["strategies"][strategy]["arms"]["full"]["events"])),
            "mean_fixed_net_excess": _mean([spread(r) for r in rows]),
            "mean_conditional_net_excess": _mean([spread(r, "net_arms") for r in rows]),
            "event_cluster_uncertainty": uncertainty(values), "by_environment": grouped,
            "mean_gross": {name: _mean([r["strategies"][strategy]["gross_arms"][name] for r in rows])
                           for name in (rows[0]["strategies"][strategy]["gross_arms"] if rows else [])},
            "mean_net": {name: _mean([r["strategies"][strategy]["net_arms"][name] for r in rows])
                         for name in (rows[0]["strategies"][strategy]["net_arms"] if rows else [])},
            "mean_fixed_net": {name: _mean([r["strategies"][strategy]["fixed_weight_net_arms"][name] for r in rows])
                               for name in (rows[0]["strategies"][strategy]["fixed_weight_net_arms"] if rows else [])},
            "cost_sensitivity": {scale: _mean([r["strategies"][strategy].get("cost_sensitivity", {}).get(scale) for r in rows])
                                 for scale in ("0.0", "0.5", "1.0", "1.5", "2.0")},
            "style_exposure": {key: _mean([r["strategies"][strategy]["exposure"]["selected"][key] /
                r["strategies"][strategy]["exposure"]["pool"][key] for r in rows
                if r["strategies"][strategy]["exposure"]["pool"][key] and r["strategies"][strategy]["exposure"]["selected"][key] is not None])
                for key in ("amount", "vol20")},
            "event_groups": [{"first": c["rows"][0]["date"], "label_end": c["end"],
                              "days": len(c["rows"]), "mean_fixed_net_excess": values[i]} for i,c in enumerate(clusters)],
            "account_return": None, "account_return_status": "requires_step_E_cash_share_ledger"}


def purged_split(rows, validation_start, embargo_sessions=0, calendar=None):
    """按标签结束日隔离，不能只按信号日划分训练和验证。"""
    calendar = calendar or []
    earlier = [d for d in calendar if d < validation_start]
    cutoff = earlier[-embargo_sessions - 1] if len(earlier) > embargo_sessions else validation_start
    return {"development": [r for r in rows if r["date"] < validation_start and r.get("label_end") and r["label_end"] <= cutoff],
            "purged": [r for r in rows if r["date"] < validation_start and (not r.get("label_end") or r["label_end"] > cutoff)],
            "validation": [r for r in rows if r["date"] >= validation_start], "development_label_cutoff": cutoff}


def rolling_validation(rows, validation_start, calendar, embargo_sessions=10):
    months = sorted({r["date"][:7] for r in rows if r["date"] >= validation_start})
    windows = []
    for month in months:
        first = max(validation_start, month + "-01")
        last = str((pd.Timestamp(month + "-01") + pd.offsets.MonthEnd(0)).date())
        available = [r for r in rows if r["date"] <= last]
        split = purged_split(available, first, embargo_sessions, calendar)
        validation = [r for r in split["validation"] if r.get("label_end") and r["label_end"] <= last]
        windows.append({"month": month, "start": first, "end": last,
            "development": split["development"], "purged": split["purged"], "validation": validation,
            "incomplete_validation_labels": [r for r in split["validation"] if r not in validation]})
    return windows
