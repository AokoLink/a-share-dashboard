"""Native THS index evidence, kept separate from other providers' memberships."""
import json
import re
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

from core.strategy_lifecycle import observation_clock
from core import daily_data as data

VERSION = "native-sector-index-v1"


class ThsIndices:
    def __init__(self, get=None, clock=None):
        self.get = get or requests.get
        self.clock = clock or observation_clock

    def request(self, url):
        response = self.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        response.raise_for_status()
        response.encoding = "gb18030"
        if not response.text:
            raise ValueError("empty_native_index_response")
        return response.text

    def catalog(self):
        url = "https://q.10jqka.com.cn/thshy/detail/code/881272/"
        raw = self.request(url)
        soup = BeautifulSoup(raw, "html.parser")
        rows = []
        for link in soup.select(".cate_inner a"):
            match = re.fullmatch(r"https?://q\.10jqka\.com\.cn/thshy/detail/code/(881\d{3})/", link.get("href", ""))
            name = link.get_text(strip=True)
            if not match or not name:
                raise ValueError("native_index_catalog_schema_changed")
            rows.append({"code": "ths_" + match[1], "native_id": match[1], "name": name,
                         "category": "industry", "provider": "ths"})
        if not rows or len({r["code"] for r in rows}) != len(rows):
            raise ValueError("empty_or_duplicate_native_index_catalog")
        return rows, {"source": url, "raw_response": raw, "observed_at": self.clock().isoformat()}

    @staticmethod
    def decode(raw, code, year):
        prefix = f"quotebridge_v4_line_bk_{code}_01_{year}("
        if not raw.strip().startswith(prefix):
            raise ValueError("native_index_callback_identity_mismatch")
        obj, end = json.JSONDecoder().raw_decode(raw.strip()[len(prefix):])
        if raw.strip()[len(prefix):][end:].strip() not in (")", ");"):
            raise ValueError("invalid_native_index_callback_suffix")
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), str) or not obj["data"]:
            raise ValueError("native_index_price_schema_changed")
        values = [s.split(",") for s in obj["data"].split(";") if s]
        if any(len(v) < 7 for v in values):
            raise ValueError("native_index_price_schema_changed")
        frame = pd.DataFrame([v[:7] for v in values],
            columns=["date", "open", "high", "low", "close", "volume_native", "amount_native"])
        dates = pd.to_datetime(frame["date"], format="%Y%m%d", errors="raise")
        if not frame["date"].str.fullmatch(r"\d{8}").all() or not (dates.dt.year == int(year)).all():
            raise ValueError("native_index_year_identity_mismatch")
        if dates.duplicated().any() or not dates.is_monotonic_increasing:
            raise ValueError("native_index_duplicate_or_reverse_dates")
        frame["date"] = dates.dt.strftime("%Y-%m-%d")
        for key in frame.columns[1:]:
            frame[key] = pd.to_numeric(frame[key], errors="raise")
        prices = frame[["open", "high", "low", "close"]]
        if (not np.isfinite(frame.iloc[:, 1:].to_numpy()).all() or (prices <= 0).any().any()
                or (frame["high"] < prices.max(axis=1)).any() or (frame["low"] > prices.min(axis=1)).any()
                or (frame[["volume_native", "amount_native"]] < 0).any().any()):
            raise ValueError("invalid_native_index_prices")
        return frame

    def history(self, row, end):
        code = row["native_id"]
        if not re.fullmatch(r"881\d{3}", code):
            raise ValueError("invalid_native_index_id")
        until = pd.Timestamp(end)
        if until.strftime("%Y-%m-%d") != end or end > self.clock().date().isoformat():
            raise ValueError("invalid_or_future_native_index_cutoff")
        start = (until - timedelta(days=400)).strftime("%Y-%m-%d")
        frames, responses = [], []
        for year in range(int(start[:4]), int(end[:4]) + 1):
            url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
            raw = self.request(url)
            frames.append(self.decode(raw, code, year))
            responses.append({"source": url, "raw_response": raw})
        frame = pd.concat(frames, ignore_index=True)
        if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
            raise ValueError("native_index_year_overlap_or_order_error")
        frame = frame[(frame["date"] >= start) & (frame["date"] <= end)].reset_index(drop=True)
        if frame.empty:
            raise ValueError("native_index_requested_window_empty")
        return frame, responses


def load(root, day=None, provider="ths"):
    """Read archived prices only; never imply corresponding constituent evidence."""
    if provider not in ("ths", "sw"):
        raise ValueError("unsupported_native_index_provider")
    base = Path(root) / ("sector_indices" if provider == "ths" else "sector_native_indices")
    ref = data.read_json(base / "latest.json") if (base / "latest.json").exists() else None
    if ref is None:
        return None
    path = Path(ref["path"]).resolve()
    if path.name != "report.json" or path.parent.parent != (base / "runs").resolve() or not data.intact(ref):
        raise ValueError("native_index_report_changed_or_outside_archive")
    report = data.read_json(path)
    if day is not None and report["as_of"] != day:
        return None
    if not all(data.intact(r) for r in data.artifacts_of(report)):
        raise ValueError("native_index_dependency_changed")
    rows = [dict(r) for r in report["rows"]]
    for period in (5, 20, 60):
        metric = f"relative{period}_pct"
        for row in rows:
            row[metric] = (row.get("relative", {}).get("returns", {}).get(str(period)) or {}).get("excess_pct")
            row[f"rank{period}"] = None
        valid = sorted((r for r in rows if r[metric] is not None), key=lambda r: (-r[metric], r["code"]))
        for rank, row in enumerate(valid, 1):
            row[f"rank{period}"] = rank
    rows.sort(key=lambda r: (r["relative20_pct"] is None, -(r["relative20_pct"] or 0), r["code"]))
    return {**report, "rows": rows, "report_ref": ref, "read_only": True,
            "benchmark": "sh000001", "selection_basis": "all_native_indices_then_relative20_rank"}
