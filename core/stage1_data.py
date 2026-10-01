# -*- coding: utf-8 -*-
"""第一阶段研究输入：保留来源、观察时间和可审计的覆盖边界。"""
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "_analysis" / "stage1"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _day(value):
    return date.fromisoformat(str(value)[:10])


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def save_calendar(frame, path=DATA_ROOT / "trading_calendar.json", collected_at=None):
    """Sina 交易日表的日期集合；不把覆盖外的日期推断为休市。"""
    days = sorted({_day(v).isoformat() for v in frame["trade_date"]})
    if not days:
        raise ValueError("empty trading calendar")
    payload = {"source": "akshare.tool_trade_date_hist_sina", "collected_at": collected_at or datetime.now().astimezone().isoformat(),
               "first": days[0], "last": days[-1], "dates": days}
    _write_json(Path(path), payload)
    return payload


def load_calendar(path=DATA_ROOT / "trading_calendar.json"):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def normalize_raw(frame, code):
    """东财未复权日线；成交量原单位为手，规范化为股。"""
    fields = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
              "成交量": "volume", "成交额": "amount", "换手率": "turnover"}
    missing = set(fields) - set(frame.columns)
    if missing:
        raise ValueError(f"raw OHLC missing fields: {sorted(missing)}")
    out = frame.rename(columns=fields)[list(fields.values())].copy()
    out["date"] = pd.to_datetime(out["date"], errors="raise").dt.strftime("%Y-%m-%d")
    for field in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        out[field] = pd.to_numeric(out[field], errors="raise")
    out["volume"] *= 100  # 东财接口的成交量为手
    out["turnover"] /= 100  # 百分数转小数
    out = out.sort_values("date").reset_index(drop=True)
    if out.empty or out["date"].duplicated().any() or not out["date"].is_monotonic_increasing:
        raise ValueError("empty, duplicate, or unsorted raw OHLC")
    if (out[list(fields.values())].isna().any().any()
            or (out[["open", "high", "low", "close"]] <= 0).any().any()
            or (out["high"] < out[["open", "close", "low"]].max(axis=1)).any()
            or (out["low"] > out[["open", "close", "high"]].min(axis=1)).any()
            or (out[["volume", "amount", "turnover"]] < 0).any().any()):
        raise ValueError(f"invalid raw OHLC for {code}")
    return out


def save_raw(frame, code, path=DATA_ROOT, collected_at=None):
    if not (len(code) == 6 and code.isdigit()):
        raise ValueError("six-digit code required")
    path = Path(path) / "raw_daily" / f"{code}.csv"
    out = normalize_raw(frame, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    out.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(path)
    meta = {"code": code, "source": "akshare.stock_zh_a_hist", "adjust": "none", "price_basis": "unadjusted",
            "volume_unit": "shares", "amount_unit": "CNY", "turnover_unit": "fraction",
            "collected_at": collected_at or datetime.now().astimezone().isoformat(),
            "first": out["date"].iloc[0], "last": out["date"].iloc[-1], "rows": len(out),
            "sha256": sha256(path)}
    _write_json(path.with_suffix(".json"), meta)
    return meta


def save_membership_snapshot(members, sector, category, source, observed_at, path=DATA_ROOT):
    """只保存观察日的当前成分；不能反推过去的生效日。"""
    day = _day(observed_at).isoformat()
    codes = sorted(set(str(code).zfill(6) for code in members))
    if not codes or any(not (len(c) == 6 and c.isdigit()) for c in codes):
        raise ValueError("invalid or empty sector membership")
    payload = {"sector": sector, "category": category, "source": source, "observed_at": day,
               "valid_from": day, "valid_to": day, "historical_provenance": False,
               "members": codes}
    name = hashlib.sha256(f"{category}:{sector}:{source}".encode()).hexdigest()[:12]
    _write_json(Path(path) / "membership" / f"{day}-{name}.json", payload)
    return payload


def membership_at(snapshots, day, sector, category):
    """仅按当时已知的快照返回成员；缺少完整历史时返回 None。"""
    day = _day(day)
    eligible = [row for row in snapshots if row["sector"] == sector and row["category"] == category
                and _day(row["observed_at"]) <= day and _day(row["valid_from"]) <= day
                and (not row.get("valid_to") or day <= _day(row["valid_to"]))]
    return max(eligible, key=lambda row: row["observed_at"])["members"] if eligible else None


def formal_gates(data_root=DATA_ROOT, start="2022-01-01", end="2026-08-14", codes=None):
    """只要历史股票池或公司行为无来源，就不将探索性结果升级。"""
    root = Path(data_root)
    codes = sorted(set(codes or []))
    calendar = load_calendar(root / "trading_calendar.json")
    calendar_ok = bool(calendar and calendar["first"] <= start and calendar["last"] >= end)
    first_session = next((d for d in calendar["dates"] if d >= start), start) if calendar_ok else start
    last_session = next((d for d in reversed(calendar["dates"]) if d <= end), end) if calendar_ok else end
    def raw_covers(code):
        csv = root / "raw_daily" / f"{code}.csv"
        meta_path = csv.with_suffix(".json")
        if not csv.exists() or not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return (meta.get("adjust") == "none" and meta.get("first", "9999") <= first_session
                and meta.get("last", "0000") >= last_session and meta.get("sha256") == sha256(csv))
    raw_ok = bool(codes) and all(raw_covers(code) for code in codes)
    coverage = {"calendar": calendar_ok, "unadjusted_prices_for_requested_codes": raw_ok,
                "historical_membership": (root / "historical_membership_complete.json").exists(),
                "delisted_and_listing_universe": (root / "historical_universe_complete.json").exists(),
                "historical_st_suspension_limits": (root / "historical_trade_status_complete.json").exists(),
                "corporate_actions": (root / "corporate_actions_complete.json").exists()}
    # 只接受有来源、覆盖窗口和输入哈希的证明；当前采集器不会生成这些证明。
    for key, filename in (("historical_membership", "historical_membership_complete.json"),
                          ("delisted_and_listing_universe", "historical_universe_complete.json"),
                          ("historical_st_suspension_limits", "historical_trade_status_complete.json"),
                          ("corporate_actions", "corporate_actions_complete.json")):
        path = root / filename
        if path.exists():
            proof = json.loads(path.read_text(encoding="utf-8"))
            coverage[key] = bool(proof.get("verified") is True and proof.get("source")
                                 and proof.get("sha256") and proof.get("start", "9999") <= start
                                 and proof.get("end", "0000") >= end)
    return {"status": "formal" if all(coverage.values()) else "exploratory",
            "window": {"start": start, "end": end}, "gates": coverage,
            "missing": [key for key, valid in coverage.items() if not valid]}
