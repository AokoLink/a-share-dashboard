"""双价格跨日缓存、复权校验和可恢复采集。缓存不改变不可变运行档案。"""
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from core import daily_data as data
from core.stage1_data import _write_json

VERSION = "price-collection-v1"


def checked_frame(item):
    if not all(data.intact(item.get(key, {})) for key in ("artifact", "response")):
        raise ValueError("price_cache_hash_mismatch")
    return pd.read_csv(item["artifact"]["path"], dtype={"date": str})


class PriceCollector:
    def __init__(self, root, work, sources, sessions, day, start, mode, timestamp,
                 *, refresh=False, retries=1, sleep=time.sleep):
        self.root, self.work = Path(root), Path(work)
        self.sources, self.sessions, self.day, self.start = sources, sessions, day, start
        self.mode, self.timestamp = mode, timestamp
        self.refresh, self.retries, self.sleep = refresh, retries, sleep

    def cache_path(self, code, kind):
        return self.root / "price_cache" / self.mode / kind / f"{code}.json"

    def cached(self, code, kind):
        try:
            row = data.read_json(self.cache_path(code, kind))
            if (row["version"] != VERSION or row["source_mode"] != self.mode
                    or row["code"] != code or row["kind"] != kind
                    or row["requested_start"] > self.start):
                return None
            checked_frame(row["item"])
            return row["item"]
        except (OSError, KeyError, ValueError):
            return None

    def remember(self, code, kind, item):
        if item.get("status") == "success":
            _write_json(self.cache_path(code, kind), {"version": VERSION, "source_mode": self.mode,
                "code": code, "kind": kind, "requested_start": self.start, "item": item})

    def publish_execution(self, code, item):
        frame = checked_frame(item)
        item = dict(item)
        item["execution_artifact"] = data.archive_frame(
            self.root / "execution" / "raw_daily" / f"{code}.csv", frame,
            item["artifact"]["source"], item["artifact"]["collected_at"],
            code=code, adjust="none", price_basis="unadjusted", **item["quality"])
        return item

    def request(self, code, kind, start, calls, directory):
        for attempt in range(self.retries + 1):
            record = {"start": start, "end": self.day, "attempt": attempt + 1}
            began = time.perf_counter()
            try:
                raw = self.sources.daily(code, start, self.day, "" if kind == "raw" else "qfq")
                source = raw.attrs.get("source", "akshare.stock_zh_a_hist")
                info = dict(raw.attrs)
                info.pop("source", None)
                evidence = info.pop("factor_evidence", None)
                dependencies = []
                if evidence:
                    dependencies.append(data.archive_json(
                        directory / f"response-{len(calls)}-factors.json",
                        evidence, "sina.qfq_factors", self.timestamp()))
                response = data.archive_frame(
                    directory / f"response-{len(calls)}.csv", raw,
                    source, self.timestamp(), adjust=kind, source_details=info, dependencies=dependencies)
                record["response"] = response
                frame = data.normalize_source_daily(raw, code)
                frame, _ = data.daily_quality(frame, self.day, self.sessions)
                if frame["date"].iloc[0] < start:
                    frame = frame[frame["date"] >= start].reset_index(drop=True)
                if frame.empty:
                    raise ValueError("requested_range_empty")
                record.update(status="success", elapsed_seconds=round(time.perf_counter() - began, 6))
                calls.append(record)
                return frame, response, source, info
            except Exception as exc:
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500],
                              elapsed_seconds=round(time.perf_counter() - began, 6))
                calls.append(record)
                if not isinstance(exc, (requests.RequestException, ConnectionError, TimeoutError)) or attempt == self.retries:
                    raise
                self.sleep(min(.5 * 2 ** attempt, 4))

    def collect(self, code, kind, checkpoint=None):
        began, calls = time.perf_counter(), []
        # 每次恢复独立写入，不覆盖作为增量父输入的上一次档案。
        self_path = self.work / "collection_attempts" / kind / code / str(uuid.uuid4())
        old = checkpoint or {}
        frame = None
        if not self.refresh:
            try:
                frame = checked_frame(old) if old.get("status") == "success" else None
            except (OSError, ValueError, KeyError):
                frame = None
            if frame is None:
                old = self.cached(code, kind) or {}
                if old:
                    frame = checked_frame(old)
        if frame is not None:
            # Future cache is never used for an earlier decision date, even after slicing.
            if str(frame["date"].iloc[-1]) > self.day:
                old, frame = {}, None
        try:
            if frame is not None:
                frame, q = data.daily_quality(frame, self.day, self.sessions)
                old = {**old, "quality": q}
            if frame is not None and str(frame["date"].iloc[-1]) == self.day and not old["quality"].get("missing_sessions"):
                item = {**old, "collection_mode": "cache_current", "requests": [],
                        "elapsed_seconds": round(time.perf_counter() - began, 6)}
                if kind == "raw":
                    item = self.publish_execution(code, item)
                self.remember(code, kind, item)
                return item
            mode, reason, parent = "history_backfill", None, None
            request_start = self.start
            if frame is not None:
                old_last = str(frame["date"].iloc[-1])
                prior_sessions = [d for d in self.sessions if d <= old_last]
                request_start = max(self.start, prior_sessions[-10] if len(prior_sessions) >= 10 else prior_sessions[0])
                gaps = old["quality"].get("missing_dates", [])
                if gaps:
                    request_start = min(request_start, max(self.start, min(gaps)))
                mode, parent = "incremental", old["artifact"]
            fresh, response, source, info = self.request(code, kind, request_start, calls, self_path)
            if frame is not None:
                overlap = sorted(set(frame["date"]) & set(fresh["date"]))
                same = bool(overlap) and np.allclose(
                    frame.set_index("date").loc[overlap, list(data.FIELDS[1:])].to_numpy(dtype=float),
                    fresh.set_index("date").loc[overlap, list(data.FIELDS[1:])].to_numpy(dtype=float),
                    rtol=1e-9, atol=1e-8)
                if not same or old["artifact"]["source"] != source:
                    # An adjusted-price rebase or vendor revision must never leave mixed historical bases.
                    reason = "overlap_or_source_changed" if overlap else "no_overlap"
                    mode = "full_refresh"
                    fresh, response, source, info = self.request(code, kind, self.start, calls, self_path)
                    frame = fresh
                else:
                    frame = pd.concat([frame, fresh], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
            else:
                frame = fresh
            frame, quality = data.daily_quality(frame, self.day, self.sessions)
            dependencies = [r["response"] for r in calls if r.get("response")]
            if parent:
                dependencies.append(parent)
            artifact = data.archive_frame(self_path / "prices.csv", frame,
                source, self.timestamp(), adjust="none" if kind == "raw" else "qfq",
                source_details=info, dependencies=dependencies, **quality)
            item = {"status": "success", "artifact": artifact, "response": response, "quality": quality,
                    "collection_mode": mode, "refresh_reason": reason, "requests": calls,
                    "elapsed_seconds": round(time.perf_counter() - began, 6), "execution_artifact": None}
            if kind == "raw":
                item = self.publish_execution(code, item)
            self.remember(code, kind, item)
            return item
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500], "requests": calls,
                    "elapsed_seconds": round(time.perf_counter() - began, 6),
                    "response": next((r["response"] for r in reversed(calls) if r.get("response")), None)}
