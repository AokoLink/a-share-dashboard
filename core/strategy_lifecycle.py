# -*- coding: utf-8 -*-
"""第四阶段：仅对真实冻结的前向信号做模拟执行与分层归因。"""
import hashlib
import inspect
import json
import math
import pickle
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pandas as pd

from core import strategies
from core.stage1_data import load_calendar, sha256

TRACKER_VERSION = "forward-observation-v3"
FEE_PER_SIDE = 0.001
SLIPPAGE_PER_SIDE = 0.001
COST_MODEL_VERSION = "fee10bp-slippage10bp-per-side-v1"
ALLOWED_STATES = ("exploratory", "historical_validated", "forward_observation",
                  "formal", "paused", "retired")
SCREENING_PIPELINE_VERSION = "shared-daily-screen-v5"


def screening_identity():
    """策略身份只依赖筛选规则及参数；接口展示改动不会重置观察期。"""
    from core import screening_engine, sector_relations, sector_insights, sector_service, analysis, environment
    parameters = {"top_n": 20, "pool_n": 200, "min_bars": strategies.MIN_BARS,
                  "min_amount": strategies.MIN_AMOUNT, "max_entry_gap": 0.05}
    strategy_code = sha256(Path(strategies.__file__))
    pool_code = sha256(screening_engine.__file__)
    manifest = {"pipeline": SCREENING_PIPELINE_VERSION, "strategy_code": strategy_code,
                "pool_code": pool_code, "parameters": parameters,
                "sector_relations_code": sha256(sector_relations.__file__),
                "sector_features_code": sha256(sector_insights.__file__),
                "sector_service_code": sha256(sector_service.__file__),
                "eligibility_dependency_code": sha256(analysis.__file__),
                "environment_code": sha256(environment.__file__)}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {"version": f"{strategies.VERSION}+{fingerprint}",
            "parameters": parameters, "code_sha256": strategy_code,
            "pool_code_sha256": pool_code, "pipeline_version": SCREENING_PIPELINE_VERSION,
            "sector_relations_code_sha256": manifest["sector_relations_code"],
            "sector_features_code_sha256": manifest["sector_features_code"],
            "sector_service_code_sha256": manifest["sector_service_code"]}


def run_modes(statuses, observe_paused=False):
    """相同策略状态规则供页面与命令行共用。"""
    return {key: ("shadow" if status == "paused" and observe_paused else
                  "disabled" if status in ("paused", "retired") else "normal")
            for key, status in statuses.items()}


def status_at_entry(snapshot, entry_day, events, as_of=None):
    """以次日开盘前最后一次状态事件决定是否新开仓；开仓后暂停不抹去旧仓。"""
    entry_at = datetime.combine(date.fromisoformat(entry_day), time(9, 30),
                                timezone(timedelta(hours=8)))
    cutoff = min(entry_at, observation_clock(as_of)) if as_of is not None else entry_at
    status, latest_change = "exploratory", None
    for event in events:
        if event["strategy"] != snapshot["strategy"] or event["version"] != snapshot["version"]:
            continue
        try:
            changed = datetime.fromisoformat(event["changed_at"])
        except ValueError:
            continue
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=entry_at.tzinfo)
        if changed <= cutoff and (latest_change is None or changed >= latest_change):
            status = event["status"]
            latest_change = changed
    return status


def _mean(values):
    return sum(values) / len(values) if values else None


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _bar(bars, day):
    return bars.get(day, {})


def net_open_return(entry_open, exit_open):
    """两端开盘代理成交，逐边计入固定费率和滑点。"""
    return (exit_open * (1 - SLIPPAGE_PER_SIDE) * (1 - FEE_PER_SIDE)
            / (entry_open * (1 + SLIPPAGE_PER_SIDE) * (1 + FEE_PER_SIDE)) - 1)


def load_bars(code, raw_root, legacy_dir):
    """优先可校验未复权原始日线；旧 pkl 仅作注明来源的研究观察。"""
    code = str(code)[-6:]
    path = Path(raw_root) / f"{code}.csv"
    meta_path = path.with_suffix(".json")
    if path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("sha256") != sha256(path) or meta.get("adjust") != "none":
                return {}, "raw_integrity_failed"
            frame = pd.read_csv(path)
        except (OSError, ValueError):
            return {}, "raw_load_failed"
        basis = "verified_unadjusted_raw"
    else:
        path = Path(legacy_dir) / f"{code}.pkl"
        if not path.exists():
            return {}, "missing"
        try:
            frame = pd.read_pickle(path)
        except (OSError, ValueError, EOFError, pickle.UnpicklingError):
            return {}, "legacy_load_failed"
        basis = "legacy_pkl_adjustment_unverified"
    if not {"date", "open", "close"}.issubset(frame.columns):
        return {}, "invalid_columns"
    bars = {}
    for _, row in frame.iterrows():
        day = str(row["date"])[:10]
        bars[day] = {"open": _finite(row["open"]), "close": _finite(row["close"])}
    return bars, basis


def observation_clock(as_of=None):
    zone = timezone(timedelta(hours=8))
    if as_of is None:
        return datetime.now(zone)
    if isinstance(as_of, datetime):
        return as_of.replace(tzinfo=zone) if as_of.tzinfo is None else as_of.astimezone(zone)
    if isinstance(as_of, str) and len(as_of) > 10:
        return observation_clock(datetime.fromisoformat(as_of))
    return datetime.combine(date.fromisoformat(str(as_of)[:10]), time.max, zone)


def simulate(code, signal_close, bars, signal_day, entry_day, exit_day, as_of=None):
    """T 日收盘信息与下一交易日开盘分离；无开盘/过高跳空均不补单。"""
    clock = observation_clock(as_of)
    known_day = clock.date().isoformat()
    base = {"code": code, "entry_day": entry_day, "exit_day": exit_day,
            "events": []}
    if signal_day > known_day or (signal_day == known_day and clock.time().replace(tzinfo=None) < time(15)):
        return {**base, "status": "waiting_entry", "lifecycle_state": "waiting_entry"}
    observed_close = _finite(_bar(bars, signal_day).get("close"))
    frozen_close = _finite(signal_close)
    if not observed_close or not frozen_close:
        return {**base, "status": "pending_or_missing_signal_bar", "lifecycle_state": "data_error",
                "signal_day": signal_day, "reason": "signal_price_unavailable"}
    if abs(observed_close / frozen_close - 1) > 0.005:
        return {**base, "status": "price_basis_mismatch", "lifecycle_state": "data_error",
                "signal_day": signal_day, "frozen_close": frozen_close,
                "observed_close": observed_close}
    if entry_day > known_day or (entry_day == known_day and clock.time().replace(tzinfo=None) < time(9, 30)):
        return {**base, "status": "waiting_entry", "lifecycle_state": "waiting_entry"}
    opened = _finite(_bar(bars, entry_day).get("open"))
    if not opened:
        return {**base, "status": "pending_or_missing_entry", "lifecycle_state": "data_error",
                "reason": "missing_entry_or_halt_unverified"}
    allowed, reason = strategies.execution_check(signal_close, opened)
    if not allowed:
        return {**base, "status": "cancelled", "lifecycle_state": "cancelled", "reason": reason,
                "entry_open": opened, "events": [{"state": "cancelled", "date": entry_day}]}
    base.update({"entry_open": opened,
                 "events": [{"state": "simulated_entry", "date": entry_day}],
                 "fill_basis": "daily_open_proxy_limit_and_suspension_unverified"})
    if not exit_day or exit_day > known_day or (exit_day == known_day and
            clock.time().replace(tzinfo=None) < time(9, 30)):
        state = "waiting_exit" if exit_day == known_day else "holding"
        closes = [(day, _finite(bar.get("close"))) for day, bar in sorted(bars.items())
                  if entry_day <= day <= known_day and (day < known_day or
                     clock.time().replace(tzinfo=None) >= time(15))]
        closes = [(day, value) for day, value in closes if value]
        return {**base, "status": state, "lifecycle_state": state,
                "mark_date": closes[-1][0] if closes else None,
                "mark_close": closes[-1][1] if closes else None}
    exited = _finite(_bar(bars, exit_day).get("open"))
    if not exited:
        return {**base, "status": "pending_or_missing_exit", "lifecycle_state": "data_error",
                "reason": "missing_exit_or_halt_unverified"}
    gross = exited / opened - 1
    net = net_open_return(opened, exited)
    closes = [_finite(bars[day].get("close")) for day in sorted(bars)
              if entry_day <= day < exit_day]
    closes = [value for value in closes if value]
    path = [opened] + closes + [exited]
    peak = path[0]
    worst = 0.0
    for price in path:
        peak = max(peak, price)
        worst = min(worst, price / peak - 1)
    return {**base, "status": "simulated_fill", "lifecycle_state": "exited",
            "events": base["events"] + [{"state": "exited", "date": exit_day}], "entry_day": entry_day,
            "exit_day": exit_day, "entry_open": opened, "exit_open": exited,
            "gross_return": gross, "net_return": net, "path_drawdown": worst,
            "fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE,
            "cost_model_version": COST_MODEL_VERSION,
            "fill_basis": "daily_open_proxy_limit_and_suspension_unverified"}


def evaluate_snapshot(snapshot, sessions, bars_by_code, basis_by_code=None, as_of=None,
                      status_events=None):
    payload = snapshot["payload"]
    day = snapshot["signal_date"]
    horizon = payload["horizon_sessions"]
    clock = observation_clock(as_of)
    known_day = clock.date().isoformat()
    if day not in sessions:
        return {"status": "pending_calendar", "reason": "signal_day_not_in_calendar",
                "snapshot_id": snapshot["id"], "signal_date": day}
    index = sessions.index(day)
    if index + 1 >= len(sessions):
        return {"status": "pending_calendar", "reason": "entry_day_not_in_calendar",
                "snapshot_id": snapshot["id"], "signal_date": day}
    entry_day = sessions[index + 1]
    exit_day = sessions[index + horizon + 1] if index + horizon + 1 < len(sessions) else None
    pool = {row["code"][-6:]: row for row in payload.get("pool", [])
            if row.get("signal_date") == day}
    outcomes = {code: simulate(code, row.get("signal_close"), bars_by_code.get(code, {}),
                               day, entry_day, exit_day, clock) for code, row in pool.items()}
    selected_codes = [row["code"][-6:] for row in payload.get("candidates", [])]
    entry_status = status_at_entry(snapshot, entry_day, status_events or [], clock)
    entry_reached = entry_day < known_day or (entry_day == known_day and
                     clock.time().replace(tzinfo=None) >= time(9, 30))
    if entry_reached and entry_status in ("paused", "retired"):
        selected = [{"code": code, "status": "cancelled",
                     "lifecycle_state": "cancelled", "entry_day": entry_day,
                     "events": [{"state": "cancelled", "date": entry_day}],
                     "reason": "strategy_suspended_before_entry"} for code in selected_codes]
    else:
        selected = [outcomes.get(code, {"code": code, "status": "candidate_not_in_pool"})
                    for code in selected_codes]
    filled = [row for row in selected if row["status"] == "simulated_fill"]
    pool_filled = [row for row in outcomes.values() if row["status"] == "simulated_fill"]
    selected_sectors = {sector for code in selected_codes for sector in pool.get(code, {}).get("sectors", [])}
    sector_filled = [outcomes[code] for code, member in pool.items()
                     if code in outcomes and outcomes[code]["status"] == "simulated_fill"
                     and selected_sectors.intersection(member.get("sectors", []))]
    selected_gross = _mean([row["gross_return"] for row in filled])
    selected_net = _mean([row["net_return"] for row in filled])
    market_gross = _mean([row["gross_return"] for row in pool_filled])
    market_net = _mean([row["net_return"] for row in pool_filled])
    sector_gross = _mean([row["gross_return"] for row in sector_filled])
    sector_net = _mean([row["net_return"] for row in sector_filled])
    attribution = {"market": market_gross,
                   "sector_selection": sector_gross - market_gross if sector_gross is not None and market_gross is not None else None,
                   "stock_selection": selected_gross - sector_gross if selected_gross is not None and sector_gross is not None else None,
                   "execution_cost": selected_net - selected_gross if selected_net is not None else None,
                   "position_adjustment": None,
                   "note": "等权候选；仓位变化未建模。各项仅在对照池覆盖时可加总。"}
    missing = [code for code in pool if code not in bars_by_code or not bars_by_code[code]]
    basis = sorted({(basis_by_code or {}).get(code, "unknown") for code in pool})
    incomplete = {"pending_or_missing_signal_bar", "pending_or_missing_entry",
                  "pending_or_missing_exit", "price_basis_mismatch", "candidate_not_in_pool"}
    entered_count = sum(row["status"] in ("holding", "waiting_exit", "simulated_fill",
                        "pending_or_missing_exit") for row in selected)
    exit_reached = bool(exit_day and (exit_day < known_day or (exit_day == known_day and
                         clock.time().replace(tzinfo=None) >= time(9, 30))))
    phase = ("pending_entry" if not entry_reached else "waiting_exit" if exit_day == known_day
             else "holding")
    if entry_reached and selected and all(row["status"] == "cancelled" for row in selected):
        phase = "cancelled"
    return {"status": ("observed" if not missing and all(row["status"] not in incomplete
            for row in list(outcomes.values()) + selected)
            else "partial_data") if exit_reached else ("partial_data" if any(
                row["status"] in incomplete for row in selected + list(outcomes.values())) else phase),
            "observation_as_of": clock.isoformat(), "signal_date": day, "entry_day": entry_day,
            "exit_day": exit_day, "strategy": snapshot["strategy"],
            "version": snapshot["version"], "snapshot_id": snapshot["id"],
            "entry_strategy_status": entry_status,
            "candidates": len(selected_codes), "filled": entered_count,
            "entered": entered_count,
            "lifecycle_counts": dict(Counter(
                row.get("lifecycle_state", "data_error") for row in selected)),
            "cancelled": sum(row["status"] == "cancelled" for row in selected),
            "fill_rate": entered_count / len(selected) if selected else None,
            "filled_share_of_pool": entered_count / len(pool) if pool else None,
            "selected_data_coverage": sum(row["status"] not in incomplete for row in selected) / len(selected)
            if selected else None,
            "pool_data_coverage": sum(row["status"] not in incomplete for row in outcomes.values()) / len(pool)
            if pool else None,
            "mean_gross_return": selected_gross, "mean_net_return": selected_net,
            "mean_path_drawdown": _mean([row["path_drawdown"] for row in filled]),
            "pool_control_gross": market_gross, "pool_control_net": market_net,
            "sector_control_gross": sector_gross, "sector_control_net": sector_net,
            "cost_model_version": COST_MODEL_VERSION,
            "attribution": attribution, "selected": selected,
            "pool_count": len(pool), "pool_missing": missing, "price_bases": basis,
            "sector_membership_basis": payload.get("membership_basis"),
            "execution_limits": ["daily_open_proxy", "historical_limit_and_halt_unverified",
                                 "corporate_actions_unverified"]}


def review_versions(snapshots, observations, events):
    """预先声明的观察门槛；不会仅凭离线代理收益自动升为正式使用。"""
    latest = {}
    for event in events:
        latest[(event["strategy"], event["version"])] = event
    obs = {}
    for row in sorted(observations, key=lambda item: item.get("id", 0)):
        obs[row["snapshot_id"]] = row["payload"]
    versions = {}
    for row in snapshots:
        key = (row["strategy"], row["version"])
        versions.setdefault(key, []).append(row)
    result = []
    for (strategy, version), rows in sorted(versions.items()):
        # 同日内容修订保持可审计，评估只以首次冻结为前向基线。
        first = {}
        for row in sorted(rows, key=lambda item: item["id"]):
            first.setdefault(row["signal_date"], row)
        measured = [obs[row["id"]] for row in first.values() if row["id"] in obs]
        complete = [row for row in measured if row.get("status") == "observed"]
        comparable = [row for row in complete if row.get("cost_model_version") == COST_MODEL_VERSION
                      and "pool_control_net" in row and "sector_control_net" in row]
        comparable_ids = {row.get("snapshot_id") for row in comparable}
        candidates = sum(row.get("candidates", 0) for row in comparable)
        spreads = [row["mean_net_return"] - row["pool_control_net"] for row in comparable
                   if row.get("mean_net_return") is not None and row.get("pool_control_net") is not None]
        sector_spreads = [row["mean_net_return"] - row["sector_control_net"] for row in comparable
                          if row.get("mean_net_return") is not None
                          and row.get("sector_control_net") is not None]
        gross_spreads = [row["mean_gross_return"] - row["pool_control_gross"] for row in comparable
                         if row.get("mean_gross_return") is not None
                         and row.get("pool_control_gross") is not None]
        gross_sector_spreads = [row["mean_gross_return"] - row["sector_control_gross"] for row in comparable
                                if row.get("mean_gross_return") is not None
                                and row.get("sector_control_gross") is not None]
        calendar = load_calendar()
        dates = calendar["dates"] if calendar else []
        positions = {day: index for index, day in enumerate(dates)}
        ordered = sorted((row for row in first.values()
                          if row["signal_date"] in positions and row["id"] in comparable_ids
                          and obs[row["id"]].get("filled", 0) > 0),
                         key=lambda row: row["signal_date"])
        clusters = 0
        last_position = None
        for row in ordered:
            position = positions[row["signal_date"]]
            if last_position is None or position - last_position > row["payload"].get("horizon_sessions", 1):
                clusters += 1
            last_position = position
        last_day = max(first) if first else None
        today = date.today().isoformat()
        last_session = max((day for day in dates if day <= today), default=None)
        sessions_since = (positions[last_session] - positions[last_day]
                          if last_day in positions and last_session in positions else None)
        candidate_counts = [len(row["payload"].get("candidates", [])) for row in first.values()]
        scores = [float(item["score"]) for row in first.values()
                  for item in row["payload"].get("candidates", []) if item.get("score") is not None]
        by_day = sorted(first.values(), key=lambda row: row["signal_date"])
        def mean_score(day_rows):
            return _mean([float(item["score"]) for row in day_rows
                          for item in row["payload"].get("candidates", [])
                          if item.get("score") is not None])
        recent_score = mean_score(by_day[-20:])
        previous_score = mean_score(by_day[-40:-20])
        score_shift = (recent_score - previous_score if recent_score is not None
                       and previous_score is not None else None)
        daily_failure_days = sum(bool(row["payload"].get("quality", {}).get("daily_failed"))
                                 for row in first.values())
        pool_count = sum(len(row["payload"].get("pool", [])) for row in first.values())
        sector_covered = sum(row["payload"].get("quality", {}).get("sector_coverage", 0)
                             for row in first.values())
        blockers = []
        if len(comparable) < 60:
            blockers.append("forward_days_under_60")
        if clusters < 30:
            blockers.append("independent_event_clusters_under_30")
        if not spreads or _mean(spreads) <= 0:
            blockers.append("cost_after_market_excess_not_positive")
        if not sector_spreads or _mean(sector_spreads) <= 0:
            blockers.append("cost_after_sector_excess_not_positive")
        if daily_failure_days / len(first) > 0.05:
            blockers.append("source_failure_days_above_5pct")
        if any("legacy_pkl_adjustment_unverified" in row.get("price_bases", []) for row in measured):
            blockers.append("price_adjustment_unverified")
        if any("unverified" in row["payload"].get("price_basis", "") for row in first.values()):
            blockers.append("signal_price_basis_unverified")
        if any(row["payload"].get("membership_basis") != "historical_verified"
               or not row["payload"].get("corporate_actions_verified") for row in first.values()):
            blockers.append("historical_membership_and_corporate_actions_unverified")
        if any(row.get("execution_limits") for row in measured) or not measured:
            blockers.append("execution_halt_and_limit_status_unverified")
        current = latest.get((strategy, version), {}).get("status", "exploratory")
        result.append({"strategy": strategy, "version": version, "status": current,
                       "frozen_days": len(first), "revisions": len(rows) - len(first),
                       "observed_days": len(complete), "selected_candidates": candidates,
                       "comparable_observed_days": len(comparable),
                       "event_clusters": clusters, "latest_signal_date": last_day,
                       "sessions_since_latest": sessions_since,
                       "mean_candidates_per_day": _mean(candidate_counts),
                       "mean_candidate_score": _mean(scores),
                       "candidate_score_shift_last_20_vs_previous_20": score_shift,
                       "daily_failure_days": daily_failure_days,
                       "sector_coverage": sector_covered / pool_count if pool_count else None,
                       "mean_net_excess_vs_pool": _mean(spreads),
                       "mean_net_excess_vs_sector": _mean(sector_spreads),
                       "mean_gross_excess_vs_pool": _mean(gross_spreads),
                       "mean_gross_excess_vs_sector": _mean(gross_sector_spreads),
                       "comparison_basis": "matched_gross_and_net" if comparable else "no_v2_comparable_observations",
                       "cross_source_disagreement": None,
                       "blockers_to_formal": blockers,
                       "recommendation": "review_or_pause" if daily_failure_days or (
                           score_shift is not None and abs(score_shift) > 10) or (
                           sessions_since is not None and sessions_since > 2) or any(
                           row.get("status") == "partial_data" for row in measured)
                           else "continue_observation"})
    return result


def observation_digest(snapshot, outcome):
    stable = {key: value for key, value in outcome.items() if key != "observation_as_of"}
    body = json.dumps({"snapshot": snapshot["content_sha256"], "outcome": stable},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
