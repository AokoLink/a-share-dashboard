"""Archive official retrospective classification records; no historical signals."""
import argparse
import io
import json
import uuid
from pathlib import Path

import pandas as pd

from core import daily_data as data, strategy_lifecycle as life
from core.sw_sector_sources import verified_get
from core.daily_runner import RunLock
from core.stage1_data import _write_json, sha256

SOURCE = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
VERSION = "official-sw-classification-archive-v1"


def normalize(frame, now):
    columns = {"股票代码": "code", "计入日期": "effective_at_source", "行业代码": "classification_code",
               "更新日期": "updated_at_source"}
    if not set(columns).issubset(frame) or frame.empty or len(frame) > 100000:
        raise ValueError("sw_classification_schema_changed")
    frame = frame[list(columns)].rename(columns=columns).copy()
    for key in ("code", "classification_code"):
        frame[key] = frame[key].astype(str)
        if not frame[key].str.fullmatch(r"\d{6}").all():
            raise ValueError("sw_classification_identity_invalid")
    for key in ("effective_at_source", "updated_at_source"):
        values = pd.to_datetime(frame[key], errors="raise")
        if values.isna().any() or values.dt.tz is not None or (values > now.replace(tzinfo=None)).any():
            raise ValueError("sw_classification_future_or_ambiguous_source_dates")
        # Source timestamps have no timezone/publication guarantee. Do not invent known_at.
        frame[key] = values.dt.strftime("%Y-%m-%dT%H:%M:%S")
    return frame


def run(root=data.ROOT, get=None):
    root = Path(root)
    with RunLock(root):
        clock = life.observation_clock()
        response = (get or verified_get)(SOURCE, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        response.raise_for_status()
        payload = response.content
        if not payload.startswith(bytes.fromhex("d0cf11e0a1b11ae1")) or len(payload) > 8 * 1024 * 1024:
            raise ValueError("sw_classification_not_supported_xls")
        work = root / "sector_classification_history" / "runs" / str(uuid.uuid4())
        work.mkdir(parents=True, exist_ok=False)
        raw_path = work / "original.xls"
        raw_path.write_bytes(payload)
        raw_ref = {"path": str(raw_path.resolve()), "sha256": sha256(raw_path), "source": SOURCE,
                   "collected_at": clock.isoformat(), "server_last_modified": response.headers.get("Last-Modified")}
        frame = normalize(pd.read_excel(io.BytesIO(payload), dtype=str), clock)
        records = data.archive_frame(work / "records.csv", frame, SOURCE, clock.isoformat(), dependencies=[raw_ref])
        report = {"version": VERSION, "received_at": clock.isoformat(), "status": "retrospective_records_archived",
                  "records": len(frame), "securities": frame["code"].nunique(),
                  "source_effective_first": frame["effective_at_source"].min(),
                  "source_effective_last": frame["effective_at_source"].max(), "records_ref": records,
                  "code_sha256": sha256(__file__), "historical_point_in_time_verified": False,
                  "limits": ["classification_codes_not_index_codes", "source_updates_not_proof_of_first_publication",
                             "no_backdating_current_constituents", "not_used_as_historical_screening_or_execution_input",
                             "historical_taxonomy_changes_and_removed_securities_require_further_verification"]}
        _write_json(work / "report.json", report)
        ref = {"path": str((work / "report.json").resolve()), "sha256": sha256(work / "report.json")}
        _write_json(root / "sector_classification_history" / "latest.json", ref)
        return {**report, "report_ref": ref}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(data.ROOT))
    args = parser.parse_args()
    result = run(args.root)
    print(json.dumps({k: result[k] for k in ("status", "records", "securities", "report_ref")}))


if __name__ == "__main__":
    main()
