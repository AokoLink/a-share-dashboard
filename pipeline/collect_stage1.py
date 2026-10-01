# -*- coding: utf-8 -*-
"""采集第一阶段可验证输入。运行: python -m pipeline.collect_stage1"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd

from core.stage1_data import DATA_ROOT, _write_json, formal_gates, load_calendar, save_calendar, save_raw, sha256
from pipeline.theme_strategy import THEMES


def collect(start, end, codes, root=DATA_ROOT):
    root = Path(root)
    fetched_at = datetime.now().astimezone().isoformat()
    calendar_error = None
    try:
        calendar = save_calendar(ak.tool_trade_date_hist_sina(), root / "trading_calendar.json", fetched_at)
    except Exception as exc:
        calendar = load_calendar(root / "trading_calendar.json")
        if calendar is None:
            raise
        calendar_error = f"{type(exc).__name__}: {exc}"
    results = {}
    for code in codes:
        try:
            frame = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start.replace("-", ""),
                                       end_date=end.replace("-", ""), adjust="")
            price = save_raw(frame, code, root, fetched_at)
            actions = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
            action_path = root / "corporate_actions" / f"{code}.csv"
            action_path.parent.mkdir(parents=True, exist_ok=True)
            actions.to_csv(action_path, index=False, encoding="utf-8")
            results[code] = {"price": price, "corporate_actions": {
                "source": "akshare.stock_history_dividend_detail", "indicator": "分红",
                "collected_at": fetched_at, "rows": len(actions), "sha256": sha256(action_path),
                "status": "raw_events_not_yet_reconciled"}}
        except Exception as exc:
            prior_meta = root / "raw_daily" / f"{code}.json"
            prior_csv = prior_meta.with_suffix(".csv")
            if prior_meta.exists() and prior_csv.exists():
                prior = json.loads(prior_meta.read_text(encoding="utf-8"))
                if prior.get("sha256") == sha256(prior_csv):
                    action_csv = root / "corporate_actions" / f"{code}.csv"
                    results[code] = {"price": prior, "status": "cached_verified",
                                     "refresh_error": f"{type(exc).__name__}: {exc}"}
                    if action_csv.exists():
                        results[code]["corporate_actions"] = {
                            "source": "akshare.stock_history_dividend_detail", "indicator": "分红",
                            "sha256": sha256(action_csv), "status": "cached_raw_events_not_reconciled"}
                    continue
            results[code] = {"error": f"{type(exc).__name__}: {exc}"}
    delisted = {}
    for market, fetch in (("sh", ak.stock_info_sh_delist), ("sz", ak.stock_info_sz_delist)):
        csv = root / "delisted" / f"{market}.csv"
        try:
            frame = fetch()
            csv.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(csv, index=False, encoding="utf-8")
            status = "refreshed"
        except Exception as exc:
            if not csv.exists():
                delisted[market] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            frame = pd.read_csv(csv, dtype=str)
            status = "cached_after_refresh_error"
        code_col = "公司代码" if market == "sh" else "证券代码"
        known = {str(code).zfill(6) for code in frame[code_col]}
        cached = {p.stem for p in (DATA_ROOT.parent / "daily").glob("*.pkl")}
        delisted[market] = {"source": fetch.__name__, "rows": len(frame), "sha256": sha256(csv),
                            "status": status, "missing_from_legacy_daily": len(known - cached),
                            "missing_codes": sorted(known - cached)}
    audit = formal_gates(root, start, end, codes)
    sessions = {day for day in calendar["dates"] if start <= day <= end}
    coverage_by_code = {}
    for code in codes:
        csv = root / "raw_daily" / f"{code}.csv"
        if not csv.exists():
            continue
        observed = set(pd.read_csv(csv, usecols=["date"], dtype=str)["date"])
        missing = sorted(sessions - observed)
        coverage_by_code[code] = {"missing_calendar_sessions": len(missing),
                                  "missing_dates": missing,
                                  "interpretation": "unknown_suspension_or_missing_data" if missing else "complete_dates"}
    audit.update({"generated_at": fetched_at, "source_calendar": calendar["source"],
                  "calendar_sha256": sha256(root / "trading_calendar.json"),
                  "calendar_refresh_error": calendar_error, "codes": codes,
                  "code_sha256": sha256(__file__), "results": results, "delisted_audit": delisted,
                  "coverage_by_code": coverage_by_code,
                  "cost_side": 0.002, "signal": "monthly_close", "execution": "next_open"})
    _write_json(root / "audit.json", audit)
    return audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--codes", nargs="*", default=sorted({c for group in THEMES.values() for c in group}))
    args = parser.parse_args()
    report = collect(args.start, args.end, args.codes)
    print(json.dumps({"status": report["status"], "gates": report["gates"],
                      "retrieved": sum("price" in r for r in report["results"].values()),
                      "requested": len(args.codes)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
