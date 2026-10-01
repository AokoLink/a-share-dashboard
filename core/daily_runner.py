"""收盘每日任务：单实例、步骤留档、断点恢复和明确的数据门槛。"""
import hashlib
import json
import os
import uuid
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pandas as pd
import numpy as np

from core import analysis, data_source, daily_data as data, environment, recommend, sector_insights, sector_relations, sector_service, screening_engine, store
from core import strategies, strategy_lifecycle as lifecycle, strategy_service, account_ledger, operations, price_collection
from core.stage1_data import _write_json, load_calendar, save_calendar, sha256
from pipeline import track_strategy_signals, account_report

STEPS = ("calendar", "gate", "inputs", "universe", "prices", "quality", "features", "freeze", "track", "account")


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    """操作系统锁在进程退出后自动释放，不以残留 lock 文件推断正在运行。"""
    def __init__(self, root):
        self.path = Path(root) / ".run.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = open(self.path, "a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise AlreadyRunning("another_daily_run_is_active") from exc
        return self

    def __exit__(self, *args):
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


def _frame(artifact):
    if not data.intact(artifact):
        raise ValueError("input_hash_mismatch")
    return pd.read_csv(artifact["path"], dtype={"date": str, "code": str})


def _error(exc):
    return f"{type(exc).__name__}: {exc}"[:500]


def continuity(root, calendar):
    """只统计真实每日运行，不用注入测试或历史回放充当前向稳定性证据。"""
    reports = []
    for path in (Path(root) / "runs").glob("*/run.json"):
        try:
            row = data.read_json(path)
            if row.get("source_mode") == "live" and not row.get("check_only") and not row.get("observe_only"):
                reports.append(row)
        except (OSError, ValueError):
            continue
    by_day = {}
    for row in sorted(reports, key=lambda x: x["started_at"]):
        by_day[row["day"]] = row
    if not by_day or not calendar:
        return {"required_sessions": 10, "consecutive_complete_sessions": 0, "passed": False, "days": []}
    first, last = min(by_day), max(by_day)
    days = [day for day in calendar["dates"] if first <= day <= last]
    good = {"success", "no_candidates", "suspended"}
    streak = 0
    for day in reversed(days):
        if by_day.get(day, {}).get("status") not in good:
            break
        streak += 1
    price_streak = 0
    for day in reversed(days):
        row = by_day.get(day, {})
        if not all(row.get("steps", {}).get(key, {}).get("status") == "success"
                   for key in ("inputs", "universe", "prices", "quality")):
            break
        price_streak += 1
    return {"required_sessions": 10, "consecutive_complete_sessions": streak, "passed": streak >= 10,
            "price_coverage": {"consecutive_complete_sessions": price_streak,
                               "passed": price_streak >= 10, "required_sessions": 10},
            "days": [{"date": day, "status": by_day.get(day, {}).get("status", "missing_run"),
                      "run_id": by_day.get(day, {}).get("run_id")} for day in days[-10:]]}


class DailyRunner:
    def __init__(self, db, root=data.ROOT, *, sources=None, now=None, workers=6,
                 holdings=None, sector_map=None, shadow=False, account_config=None, refresh_prices=False):
        self.db, self.root = str(Path(db).resolve()), Path(root).resolve()
        self.sources = sources or data.LiveSources()
        self.now = lifecycle.observation_clock(now)
        self.day = self.now.date().isoformat()
        self.workers = max(1, min(int(workers), 8))
        self.holdings = sorted(set(holdings or []))
        if any(len(code) != 6 or not code.isdigit() for code in self.holdings):
            raise ValueError("holdings_require_six_digit_codes")
        self.holdings_supplied = holdings is not None
        self.shadow = shadow
        self.refresh_prices = refresh_prices
        self.account_config = account_ledger.config(account_config)
        self.sector_map = sector_map
        if self.sector_map is None:
            path = data.ROOT.parent / "code2sector.json"
            try:
                self.sector_map = json.loads(path.read_text(encoding="gbk"))
            except (OSError, ValueError):
                self.sector_map = {}

    def timestamp(self):
        return lifecycle.observation_clock().isoformat()

    def archive(self, name, value, source, **meta):
        return data.archive_json(self.work / name, value, source, self.timestamp(), **meta)

    def run(self, resume=None, check_only=False):
        store.init_db(self.db)
        with RunLock(self.root):
            signature = hashlib.sha256(json.dumps({"code": [sha256(__file__), sha256(data.__file__),
                sha256(strategy_service.__file__), sha256(lifecycle.__file__), sha256(environment.__file__),
                sha256(recommend.__file__), sha256(strategies.__file__), sha256(data_source.__file__),
                sha256(sector_relations.__file__), sha256(sector_service.__file__), sha256(sector_insights.__file__), sha256(screening_engine.__file__),
                sha256(account_ledger.__file__), sha256(account_report.__file__), sha256(price_collection.__file__)], "db": self.db, "holdings": self.holdings,
                "account_config": self.account_config,
                "holdings_supplied": self.holdings_supplied, "sectors": self.sector_map,
                "shadow": self.shadow, "check_only": check_only, "refresh_prices": self.refresh_prices,
                "source_mode": "live" if isinstance(self.sources, data.LiveSources) else "injected"}, sort_keys=True).encode()).hexdigest()
            if resume:
                run_id = str(uuid.UUID(resume))
                self.work = self.root / "runs" / run_id
                manifest = data.read_json(self.work / "run.json")
                if manifest["day"] != self.day or manifest["signature"] != signature:
                    raise ValueError("resume_requires_same_day_code_inputs_and_mode")
            else:
                run_id = str(uuid.uuid4())
                self.work = self.root / "runs" / run_id
                manifest = {"run_id": run_id, "day": self.day, "started_at": self.timestamp(),
                            "effective_at": self.now.isoformat(), "signature": signature, "db": self.db,
                            "source_mode": "live" if isinstance(self.sources, data.LiveSources) else "injected",
                            "check_only": check_only, "observe_only": self.shadow, "steps": {}}
            self.manifest = manifest
            manifest["steps"].pop("report", None)
            manifest["status"] = "running"
            manifest["resumed_at"] = self.timestamp() if resume else None
            self.save()
            self.results = {}
            invalidated = False
            for name in STEPS:
                prior = manifest["steps"].get(name)
                if name in ("gate", "account"):  # 账户恢复也重新读取冻结、状态与执行证据。
                    reuse = False
                else:
                    reuse = bool(resume and not invalidated and prior and prior["status"] == "success"
                                 and all(data.intact(a) for a in data.artifacts_of(prior["result"])))
                if reuse:
                    self.results[name] = prior["result"]
                    prior["reused"] = True
                    continue
                if name != "gate":
                    invalidated = True
                reason = self.skip_reason(name, check_only)
                history = list((prior or {}).get("attempt_history", []))
                if prior:
                    history.append({key: prior.get(key) for key in ("status", "started_at", "finished_at", "input_date")})
                    history[-1]["error"] = prior.get("result", {}).get("error")
                    history[-1]["failed_inputs"] = prior.get("result", {}).get("failed_inputs")
                    history[-1]["source_errors"] = [{"code": code, "kind": kind, "error": item.get("error")}
                        for code, pair in prior.get("result", {}).get("entries", {}).items()
                        for kind, item in pair.items() if item.get("error")]
                record = {"status": "skipped" if reason else "running", "started_at": self.timestamp(),
                          "input_date": self.day, "attempt": (prior or {}).get("attempt", 0) + 1,
                          "attempt_history": history}
                manifest["steps"][name] = record
                self.save()
                began = time.perf_counter()
                if reason:
                    result = {"status": "skipped", "reason": reason}
                else:
                    try:
                        result = getattr(self, "step_" + name)()
                    except Exception as exc:
                        result = {"status": "failed", "error": _error(exc)}
                record.update({"status": result["status"], "finished_at": self.timestamp(), "result": result,
                               "elapsed_seconds": round(time.perf_counter() - began, 6)})
                self.results[name] = result
                self.save()
            gate = self.results["gate"]
            required = [self.results[key]["status"] for key in ("inputs", "universe", "prices", "quality", "features", "freeze", "track", "account")]
            if gate["status"] != "success":
                status = gate.get("reason", "failed")
            elif check_only:
                status = "checked"
            elif "failed" in required:
                status = "failed"
            elif "partial" in required:
                status = "partial"
            else:
                counts = self.results.get("freeze", {}).get("candidate_counts", {})
                freeze_status = self.results.get("freeze", {}).get("freeze_status")
                status = ("shadow_observation" if self.shadow else "suspended" if freeze_status == "skipped_status"
                          else "success" if any(counts.values()) else "no_candidates")
            manifest.update({"status": status, "finished_at": self.timestamp()})
            self.save()
            operations.publish(self.root, manifest)
            stability = continuity(self.root, self.results.get("calendar", {}).get("calendar"))
            coverage = self.results.get("quality", {}).get("coverage", {})
            memberships = self.results.get("features", {}).get("memberships", {})
            by_code = self.results.get("quality", {}).get("by_code", {})
            for role, row in coverage.items():
                available = sum(bool(memberships.get(code)) for code in row["codes"])
                row["membership"] = {"available": available, "expected": row["expected"],
                    "missing_rate": 1 - available / row["expected"] if row["expected"] else None,
                    "basis": "current_snapshot_not_historical"}
                row["trade_status"] = {"current_name_volume_proxy": sum(
                    by_code.get(code, {}).get("trade_status", {}).get("name") is not None for code in row["codes"]),
                    "verified_halt_and_execution_limits": 0}
            report = {"run_id": run_id, "day": self.day, "status": status,
                      "generated_at": manifest["finished_at"], "steps": manifest["steps"],
                      "coverage": coverage,
                      "stability": stability, "notice": "十个实际交易日仅验收运行稳定性；不代表策略有效。"}
            report_meta = self.archive("report.json", report, "daily_pipeline")
            manifest["steps"]["report"] = {"status": "success", "input_date": self.day,
                "started_at": manifest["finished_at"], "finished_at": self.timestamp(), "result": report_meta}
            self.save()
            _write_json(self.root / "latest.json", {**report, "report": report_meta})
            _write_json(self.root / "days" / f"{self.day}.json", {"run_id": run_id, "status": status, "report": report_meta})
            return report

    def save(self):
        _write_json(self.work / "run.json", self.manifest)

    def skip_reason(self, name, check_only):
        if name in ("calendar", "gate"):
            return None
        if check_only:
            return "check_only"
        if self.results.get("gate", {}).get("status") != "success":
            return self.results.get("gate", {}).get("reason", "gate_failed")
        # 跟踪已冻结信号在采集失败时仍执行，并明确显示缺失，不混成无信号。
        if name in ("track", "account"):
            return None
        dependencies = {"inputs": (), "universe": ("inputs",), "prices": ("universe",),
                        "quality": ("inputs", "universe", "prices"), "features": ("quality",),
                        "freeze": ("quality", "features")}
        for key in dependencies[name]:
            if self.results.get(key, {}).get("status") in ("failed", "skipped"):
                return f"dependency_{key}_failed"
        return None

    def step_calendar(self):
        error = None
        try:
            calendar = save_calendar(self.sources.calendar(), self.root / "calendar.json", self.timestamp())
        except Exception as exc:
            error = _error(exc)
            calendar = load_calendar(self.root / "calendar.json") or load_calendar()
            if not calendar:
                raise ValueError(f"calendar_unavailable: {error}") from exc
            _write_json(self.root / "calendar.json", calendar)
        artifact = self.archive("calendar.json", calendar, calendar["source"])
        return {"status": "success", "calendar": calendar, "artifact": artifact, "refresh_error": error}

    def step_gate(self):
        calendar = self.results.get("calendar", {}).get("calendar")
        if not calendar:
            return {"status": "failed", "reason": "calendar_unavailable"}
        if not calendar["first"] <= self.day <= calendar["last"]:
            return {"status": "failed", "reason": "calendar_uncovered"}
        if self.day not in calendar["dates"]:
            return {"status": "skipped", "reason": "market_closed"}
        if (self.now.hour, self.now.minute) < (15, 5):
            return {"status": "skipped", "reason": "waiting_for_close"}
        return {"status": "success", "day": self.day, "source_close_confirmation": "requires_current_price_bars"}

    def step_inputs(self):
        spot, new_stocks = self.sources.market()
        columns = {"code", "name", "price", "change_pct", "volume", "amount"}
        if spot.empty or not columns.issubset(spot.columns) or spot["code"].duplicated().any():
            raise ValueError("invalid_market_schema_or_duplicate_codes")
        spot = spot.copy()
        spot["code"] = spot["code"].astype(str)
        if not spot["code"].str.fullmatch(r"\d{6}").all():
            raise ValueError("invalid_market_codes")
        numeric = spot[list(columns - {"code", "name"})].apply(pd.to_numeric, errors="raise")
        if not np.isfinite(numeric.to_numpy(dtype=float)).all() or (numeric[["price", "amount", "volume"]] < 0).any().any():
            raise ValueError("invalid_market_numeric_values")
        spot_meta = data.archive_frame(self.work / "spot.csv", spot, "akshare.stock_zh_a_spot",
                    self.timestamp(), as_of=self.day, date_confirmation="awaiting_daily_bars",
                    volume_unit="shares", amount_unit="CNY")
        new_meta = self.archive("new_stocks.json", new_stocks, "akshare.stock_zh_a_new", as_of=self.day)
        return {"status": "success", "spot": spot_meta, "new_stocks": new_meta}

    def step_universe(self):
        spot = _frame(self.results["inputs"]["spot"])
        excluded = data.read_json(self.results["inputs"]["new_stocks"]["path"])
        pool, preflight_rejected = screening_engine.acquisition_scope(spot.to_dict("records"), excluded)
        latest = {row["snapshot_id"]: row["payload"] for row in reversed(store.list_strategy_observations(self.db, limit=100000))}
        outstanding = [row for row in store.list_strategy_signals(self.db, limit=100000)
                       if latest.get(row["id"], {}).get("status") != "observed"]
        research = sorted({str(row["code"]) for row in pool})
        controls = sorted(set(research) | {item["code"][-6:] for row in outstanding for item in row["payload"].get("pool", [])})
        simulated = sorted({item["code"][-6:] for row in outstanding for item in row["payload"].get("candidates", [])})
        # Account exits may still be pending after the per-signal observer has finished.
        # Replay also needs closed lots' earlier bars; retain every frozen account candidate.
        account_codes = sorted({str(item["code"])[-6:] for row in store.list_strategy_signals(self.db, limit=100000)
                                for item in row["payload"].get("pool", []) + row["payload"].get("candidates", [])})
        manifest = {"day": self.day, "research": research, "controls": controls,
                    "holdings": self.holdings, "holdings_input": "supplied" if self.holdings_supplied else "not_supplied",
                    "simulated_candidates": simulated, "account_codes": account_codes,
                    "requested": sorted(set(controls) | set(self.holdings) | set(account_codes)),
                    "pool": pool, "preflight_rejected": preflight_rejected,
                    "research_basis": "acquisition_scope_before_historical_eligibility_no_truncation", "outstanding_snapshot_ids": [row["id"] for row in outstanding]}
        return {"status": "success", **manifest,
                "artifact": self.archive("universe.json", manifest, "current_spot_and_frozen_pools")}

    def step_prices(self):
        sessions = self.results["calendar"]["calendar"]["dates"]
        universe = self.results["universe"]
        start = (self.now.date() - timedelta(days=400)).isoformat()
        pending_days = [row["signal_date"] for row in store.list_strategy_signals(self.db, limit=100000)
                        if row["id"] in universe["outstanding_snapshot_ids"] or row["payload"].get("candidates")]
        # Include liquidity at the session preceding the first account signal.
        if pending_days and min(pending_days) in sessions:
            idx = sessions.index(min(pending_days))
            if idx:
                pending_days.append(sessions[idx - 1])
        start = min([start] + pending_days)
        prior = {}
        # 中断后价格 checkpoint 单独留档，成功的代码不再次访问来源。
        checkpoint = self.work / "price_checkpoint.json"
        if checkpoint.exists():
            prior = data.read_json(checkpoint)
        entries = {code: prior.get(code, {}) for code in universe["requested"]}
        collector = price_collection.PriceCollector(self.root, self.work, self.sources, sessions,
            self.day, start, self.manifest['source_mode'], self.timestamp, refresh=self.refresh_prices)
        def fetch(code, kind):
            return collector.collect(code, kind, entries[code].get(kind))
        completed, checkpoint_at = 0, time.monotonic()
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            jobs = {executor.submit(fetch, code, kind): (code, kind) for code in entries for kind in ("raw", "technical")}
            for future in as_completed(jobs):
                code, kind = jobs[future]
                entries[code][kind] = future.result()
                completed += 1
                if time.monotonic() - checkpoint_at >= 2 or completed == len(jobs):
                    _write_json(checkpoint, entries)
                    _write_json(self.work / 'price_progress.json', {'day': self.day,
                        'run_id': self.manifest['run_id'], 'requested': len(entries) * 2,
                        'completed': completed, 'updated_at': self.timestamp(),
                        'failed': sum(i.get('status') == 'failed' for p in entries.values() for i in p.values())})
                    checkpoint_at = time.monotonic()
        collection_modes = dict(Counter(i.get('collection_mode', 'failed') for p in entries.values() for i in p.values()))
        history_gaps = [{'code': c, 'kind': k, 'missing_dates': i['quality']['missing_dates'],
                         'interpretation': 'unknown_suspension_or_source_gap'}
                        for c,pair in entries.items() for k,i in pair.items()
                        if i.get('quality', {}).get('missing_sessions')]
        gaps_ref = self.archive('price_history_gaps.json', history_gaps, 'unresolved_historical_price_gaps')
        retry_queue = [{'code': c, 'kind': k, 'reason': i.get('error') or 'missing_current_bar',
                        'last_available': i.get('quality', {}).get('last')}
                       for c,pair in entries.items() for k,i in pair.items()
                       if i.get('status') != 'success' or not i.get('quality', {}).get('current')]
        retry_ref = self.archive('price_retry_queue.json', retry_queue, 'failed_or_stale_price_inputs')
        _write_json(self.root / 'price_retry_latest.json', {'day': self.day,
            'run_id': self.manifest['run_id'], 'artifact': retry_ref, 'pending': len(retry_queue)})
        failed = sum(item.get("status") != "success" for pair in entries.values() for item in pair.values())
        stale = sum(item.get("status") == "success" and not item.get("quality", {}).get("current")
                    for pair in entries.values() for item in pair.values())
        return {"status": "partial" if failed or stale else "success", "entries": entries,
                "requested": len(entries), "failed_inputs": failed,
                "stale_inputs": stale, "collection_modes": collection_modes, "retry_queue": retry_ref,
                "history_gaps": gaps_ref, "history_gap_inputs": len(history_gaps),
                "price_provider": getattr(self.sources, "price_provider", None),
                "primary_source_error": getattr(self.sources, "primary_error", None)}

    def step_quality(self):
        universe, prices = self.results["universe"], self.results["prices"]["entries"]
        spot = {str(row["code"]): row for row in _frame(self.results["inputs"]["spot"]).to_dict("records")}
        codes = {}
        for code in universe["requested"]:
            row = {}
            for kind in ("raw", "technical"):
                item = prices[code].get(kind, {})
                q = item.get("quality", {})
                row[kind] = {"available": item.get("status") == "success", "current": bool(q.get("current")),
                             "enough_history": item.get("artifact", {}).get("rows", 0) >= 61,
                             "missing_sessions": q.get("missing_sessions"), "missing_dates": q.get("missing_dates", []),
                             "error": item.get("error")}
            row["trade_status"] = {"source": "current_spot_name_and_volume_proxy", "as_of": self.day,
                "name": str(spot[code]["name"]) if code in spot else None,
                "st_proxy": "ST" in str(spot[code]["name"]).upper() if code in spot else None,
                "volume_positive": float(spot[code]["volume"]) > 0 if code in spot else None,
                "halt_status": "unverified", "limit_execution_status": "unverified"}
            current_prices = all(row[k]["available"] and row[k]["current"] for k in ("raw", "technical"))
            matches = False
            if current_prices and code in spot:
                raw_bar = _frame(prices[code]["raw"]["artifact"]).iloc[-1]
                tech_bar = _frame(prices[code]["technical"]["artifact"]).iloc[-1]
                raw_close, tech_close = float(raw_bar["close"]), float(tech_bar["close"])
                matches = abs(raw_close / float(spot[code]["price"]) - 1) <= .005 and abs(raw_close / tech_close - 1) <= .005
                volume = float(spot[code]["volume"])
                amount = float(spot[code]["amount"])
                row["volume_amount_units_match"] = (volume > 0 and amount > 0
                    and abs(float(raw_bar["volume"]) / volume - 1) <= .03
                    and abs(float(raw_bar["amount"]) / amount - 1) <= .03
                    and abs(float(tech_bar["volume"]) / volume - 1) <= .03
                    and abs(float(tech_bar["amount"]) / amount - 1) <= .03)
            row["spot_close_matches"] = matches
            codes[code] = row
        technical = {c: _frame(pair["technical"]["artifact"]) for c,pair in prices.items()
                     if pair.get("technical", {}).get("status") == "success"}
        prepared = screening_engine.prepare(self.day, technical, universe["pool"])
        selected_codes = [r["code"] for r in prepared["pool"]]
        unknown_codes = [c for c,r in prepared["eligibility"].items()
                         if r["data_status"] == "unavailable" or r["reason"] in ("invalid_history", "missing_daily_fields")]
        coverage = {}
        scopes = {**{role: universe[role] for role in ("controls", "holdings", "simulated_candidates")},
                  "research": selected_codes, "qualification_inputs": universe["research"]}
        for role, wanted in scopes.items():
            coverage[role] = {"codes": wanted, "expected": len(wanted),
                **{kind: {"available": sum(codes[c][kind]["available"] for c in wanted),
                          "current": sum(codes[c][kind]["current"] for c in wanted),
                          "missing_rate": sum(not codes[c][kind]["current"] for c in wanted) / len(wanted) if wanted else None}
                   for kind in ("raw", "technical")}}
        failed = sorted(set(unknown_codes) | {code for code in selected_codes if not codes[code]["spot_close_matches"]
                  or not codes[code].get("volume_amount_units_match")
                  or not all(codes[code][k]["enough_history"] for k in ("raw", "technical"))})
        ready = bool(universe["research"]) and not failed
        report = {"status": "success" if ready and all(codes[c][k]["current"] for c in codes for k in ("raw", "technical")) else "partial",
                  "freeze_ready": ready, "failed_research_codes": failed, "coverage": coverage,
                  "selected_pool_codes": selected_codes, "eligibility": prepared["eligibility"],
                  "qualification_unknown_codes": unknown_codes,
                  "by_code": codes, "holdings_input": universe["holdings_input"],
                  "missing_interpretation": "unknown_suspension_or_source_gap"}
        return {**report, "artifact": self.archive("quality.json", report, "daily_quality_checks")}

    def step_features(self):
        spot = _frame(self.results["inputs"]["spot"])
        ready = self.results["quality"]["freeze_ready"]
        regime = {"as_of": self.day, "label": None, "metrics": {}, "status": "insufficient_daily_history",
                  "basis": "full_market_daily_spot_median_return", "required_sessions": 61}
        if ready:
            active = spot[(spot["price"] > 0) & (spot["volume"] > 0)]
            daily = {"date": self.day, "r1": float(active["change_pct"].median()) / 100,
                     "up_ratio": float((active["change_pct"] > 0).mean()),
                     "limit_up": sum(float(r["change_pct"]) >= analysis.limit_threshold(r["code"]) for r in active.to_dict("records")),
                     "limit_down": sum(float(r["change_pct"]) <= -analysis.limit_threshold(r["code"]) for r in active.to_dict("records")),
                     "turnover": float(active["amount"].sum())}
            _write_json(self.root / "market_daily" / f"{self.day}.json", daily)
            history = [data.read_json(path) for path in sorted((self.root / "market_daily").glob("*.json"))
                       if path.stem <= self.day]
            recent = history[-61:]
            series = environment.classify_market_rows(recent)
            expected = [d for d in self.results["calendar"]["calendar"]["dates"] if d <= self.day][-61:]
            if len(recent) >= 61 and [row["date"] for row in recent] == expected:
                last = series.iloc[-1]
                regime.update({"label": last["environment"], "status": "current",
                    "metrics": {key: environment._num(last[key]) for key in ("r1", "r5", "r20", "up_ratio", "limit_up", "limit_down", "turnover_ratio")}})
            regime["observed_sessions"] = len(history)
        else:
            regime["status"] = "source_data_unconfirmed"
        regime_meta = self.archive("regime.json", regime, "daily_market_snapshots", as_of=self.day)
        # 页面旧入口也读取新版状态；来源不充分时 label=None，不延用历史市场状态。
        _write_json(self.root / "regime_cache.json", regime)
        summaries, errors, members, sector_rows = None, [], {}, []
        try:
            summaries, stale = self.sources.sectors()
            if stale:
                raise ValueError("sector_summary_stale")
            summary_meta = data.archive_frame(self.work / "sectors.csv", summaries, "native_sector_catalog", self.timestamp(), as_of=self.day)
        except Exception as exc:
            summary_meta = None
            errors.append({"source": "sector_summary", "error": _error(exc)})
        try:
            benchmark, stale = self.sources.benchmark()
            if stale:
                raise ValueError("benchmark_stale")
            benchmark = benchmark.copy()
            benchmark["date"] = pd.to_datetime(benchmark["date"]).dt.strftime("%Y-%m-%d")
            benchmark = benchmark[benchmark["date"] <= self.day]
            dependencies = []
            if benchmark.attrs.get("raw_response"):
                dependencies.append(self.archive("benchmark_response.json", benchmark.attrs["raw_response"],
                                                 benchmark.attrs.get("source", "sh000001")))
            benchmark_meta = data.archive_frame(self.work / "benchmark.csv", benchmark,
                benchmark.attrs.get("source", "akshare.sh000001"), self.timestamp(),
                as_of=str(benchmark["date"].iloc[-1]), benchmark_symbol="sh000001", dependencies=dependencies)
        except Exception as exc:
            benchmark, benchmark_meta = None, None
            errors.append({"source": "benchmark", "error": _error(exc)})
        entries = self.results["prices"]["entries"]
        technical = {c: _frame(pair["technical"]["artifact"]) for c, pair in entries.items()
                     if pair.get("technical", {}).get("status") == "success"}
        calendar = self.results["calendar"]["calendar"]["dates"]
        prev_day = next((d for d in reversed(calendar) if d < self.day), None)
        previous = sector_relations.load(self.root, prev_day) if prev_day else None
        def archive_history(row, frame):
            return data.archive_frame(self.work / "sector_history" / f"{row['category']}-{row['code']}.csv",
                frame, frame.attrs.get("source", "sector_index_source"), self.timestamp(),
                as_of=str(frame["date"].iloc[-1]), dependencies=[row["index_response"]] if row.get("index_response") else [],
                quantity_basis=frame.attrs.get("quantity_basis", "provider_native_not_stock_execution_units"))
        evidence = sector_service.collect(self.day, summaries, self.sources, spot, technical, benchmark,
            calendar, self.archive, archive_history, workers=self.workers, previous=previous,
            generated_at=self.timestamp)
        if benchmark_meta:
            evidence["input_artifacts"].append(benchmark_meta)
        for row in evidence["rows"]:
            if row["membership_status"] != "verified_current" or row["relative"].get("status") != "ok":
                errors.append({"source": row["id"], "error": row.get("errors") or "membership_or_index_unverified"})
        sector_ref = sector_relations.save(self.root, evidence)
        native_sector_ref = None
        if any(r.get("taxonomy") == "sw_native_index" for r in evidence["rows"]):
            try:
                from pipeline.collect_sector_indices import observed_reference
                native_sector_ref = observed_reference(self.root, evidence, benchmark, calendar)
            except Exception as exc:
                errors.append({"source": "native_sector_reference", "error": _error(exc)})
        snapshot = sector_relations.load(self.root, self.day, sector_ref)
        members = {c: [r["name"] for r in sector_relations.lookup(snapshot, c, self.day)["relations"]]
                   for c in self.results["universe"]["requested"]}
        unmapped = [c for c, names in members.items() if not names]
        if unmapped:
            errors.append({"source": "membership_coverage", "error": "current_membership_unknown", "codes": unmapped})
        sector_meta = self.archive("sector_features.json", evidence["rows"], "dated_sector_service", as_of=self.day)
        native_indices = None
        if hasattr(self.sources, "native_index_reference"):
            try:
                native_indices = self.sources.native_index_reference(self.day, self.root, self.workers, benchmark)
                if native_indices["status"] != "complete_prices":
                    errors.append({"source": "native_indices", "error": "native_index_prices_incomplete"})
            except Exception as exc:
                errors.append({"source": "native_indices", "error": _error(exc)})
        return {"status": "partial" if errors or evidence["status"] != "complete" else "success",
                "regime": regime, "regime_artifact": regime_meta, "memberships": members,
                "sector_features": sector_meta, "sector_snapshot": sector_ref,
                "native_sector_reference": native_sector_ref,
                "native_index_reference": native_indices,
                "sector_coverage": evidence["candidate_universe"], "sector_inputs": evidence["rows"],
                "summary": summary_meta, "benchmark": benchmark_meta, "errors": errors,
                "membership_coverage": sum(bool(members.get(c)) for c in self.results["universe"]["research"]),
                "membership_expected": len(self.results["universe"]["research"])}

    def step_freeze(self):
        prices = self.results["prices"]["entries"]
        technical = {c: _frame(pair["technical"]["artifact"]) for c, pair in prices.items()
                     if pair.get("technical", {}).get("status") == "success"}
        raw = {c: _frame(pair["raw"]["artifact"]) for c, pair in prices.items()
               if pair.get("raw", {}).get("status") == "success"}
        result = strategy_service.generate(self.db, self.results["universe"]["pool"], technical, raw,
                    self.results["features"]["memberships"], self.results["features"]["regime"], self.now,
                    allow_freeze=self.results["quality"]["freeze_ready"], shadow=self.shadow,
                    sector_snapshot=sector_relations.load(self.root, self.day, self.results["features"]["sector_snapshot"]),
                    sector_snapshot_ref=self.results["features"]["sector_snapshot"],
                    replay_root=self.root / "replay", source_mode=self.manifest["source_mode"],
                    record_time=lifecycle.observation_clock if self.manifest["source_mode"] == "live" else None,
                    quality={"price_input_sha256": {c: {kind: pair[kind]["artifact"]["sha256"]
                        for kind in ("raw", "technical") if pair.get(kind, {}).get("status") == "success"}
                        for c, pair in prices.items() if c in self.results["universe"]["research"]},
                        "price_sources": {c: {kind: {"source": pair[kind]["artifact"]["source"],
                            "adjust": pair[kind]["artifact"]["adjust"],
                            "response_price_keys": pair[kind]["artifact"].get("source_details", {}).get("response_price_keys")}
                            for kind in ("raw", "technical") if pair.get(kind, {}).get("status") == "success"}
                            for c, pair in prices.items() if c in self.results["universe"]["research"]},
                        "price_gap_codes": [c for c, row in self.results["quality"]["by_code"].items()
                            if c in self.results["universe"]["research"] and any(row[k].get("missing_sessions") for k in ("raw", "technical"))],
                        "trade_status_verified": False})
        artifact = self.archive("candidates.json", result, "daily_strategy_service", as_of=self.day)
        _write_json(self.root / "candidates" / f"{self.day}.json", result)
        return {"status": "success" if result["freeze_status"] in ("frozen", "skipped_status", "shadow_not_frozen") else "partial",
                "artifact": artifact, "freeze_status": result["freeze_status"], "snapshot_ids": result["snapshot_ids"],
                "replay_archives": [store.get_strategy_signal(self.db, sid)["payload"]["replay_ref"] for sid in result["snapshot_ids"].values()],
                "candidate_counts": {key: len(rows) for key, rows in result["groups"].items()}}

    def step_track(self):
        result = track_strategy_signals.run(self.db, self.root / "execution" / "raw_daily",
            self.root / "no_legacy_fallback", self.root / "calendar.json",
            as_of=lifecycle.observation_clock() if self.manifest["source_mode"] == "live" else self.now)
        return {"status": "success" if result["status"] != "missing_calendar" else "failed",
                "artifact": self.archive("monitor.json", result, "forward_tracking"),
                "snapshots": result.get("snapshots", 0), "pending": result.get("pending", 0),
                "progress": result.get("progress", [])}

    def step_account(self):
        result = account_report.run(self.db, self.root, settings=self.account_config,
            source="frozen_forward_signals" if self.manifest["source_mode"] == "live" else "injected_daily_pipeline_simulation",
            as_of=lifecycle.observation_clock() if self.manifest["source_mode"] == "live" else self.now)
        validation = None
        if self.manifest["source_mode"] == "live":
            from pipeline import forward_validation
            validation = forward_validation.run(self.db, self.root)
        summary = self.archive("monitor_summary.json", {"versions": result.get("versions", []),
            "watermark": store.monitor_watermark(self.db), "generated_at": result.get("generated_at")}, "background_strategy_review")
        _write_json(self.root / "monitor" / "latest.json", summary)
        return {"status": "partial" if result["status"] in ("valuation_incomplete", "execution_inputs_incomplete") else "success",
                "artifact": self.archive("account.json", result, "cash_share_account_simulation"),
                "account_id": result["account_id"], "account_status": result["status"],
                "mode": result["mode"], "metrics": result["metrics"], "report_ref": result["report_ref"],
                "forward_validation": {k: validation.get(k) for k in ("status", "accepted_snapshots", "report_ref")}
                    if validation else None}
