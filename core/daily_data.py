"""每日研究数据：双价格口径、输入留档与可核查的覆盖统计。"""
import json
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from core.stage1_data import _write_json, normalize_raw, sha256

ROOT = Path(__file__).resolve().parents[1] / "_analysis" / "daily_pipeline"
FIELDS = ("date", "open", "high", "low", "close", "volume", "amount", "turnover")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def archive_json(path, data, source, collected_at, **metadata):
    path = Path(path)
    _write_json(path, data)
    return {"path": str(path.resolve()), "sha256": sha256(path), "source": source,
            "collected_at": collected_at, **metadata}


def archive_frame(path, frame, source, collected_at, **metadata):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(path)
    meta = {"path": str(path.resolve()), "sha256": sha256(path), "source": source,
            "collected_at": collected_at, "rows": len(frame),
            "columns": list(frame.columns), **metadata}
    _write_json(path.with_suffix(".json"), meta)
    return meta


def intact(artifact):
    try:
        return sha256(artifact["path"]) == artifact["sha256"] and all(intact(ref) for ref in artifact.get("dependencies", []))
    except (OSError, KeyError):
        return False


def artifacts_of(value):
    if isinstance(value, dict):
        if "path" in value and "sha256" in value:
            yield value
        else:
            for item in value.values():
                yield from artifacts_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from artifacts_of(item)


def daily_quality(frame, day, sessions, min_bars=1):
    """缺日只报告未知缺失，不推断停牌；反序与重复在排序前拒绝。"""
    if not set(FIELDS).issubset(frame.columns) or frame.empty:
        raise ValueError("empty_or_changed_daily_schema")
    out = frame[list(FIELDS)].copy()
    dates = pd.to_datetime(out["date"], errors="raise")
    out["date"] = dates.dt.strftime("%Y-%m-%d")
    if out["date"].duplicated().any() or not out["date"].is_monotonic_increasing:
        raise ValueError("duplicate_or_reverse_dates")
    numeric = out[list(FIELDS[1:])].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("nonfinite_daily_values")
    out[list(FIELDS[1:])] = numeric
    if ((numeric[["open", "high", "low", "close"]] <= 0).any().any()
            or (numeric[["volume", "amount", "turnover"]] < 0).any().any()
            or (out["high"] < out[["open", "close", "low"]].max(axis=1)).any()
            or (out["low"] > out[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("invalid_daily_prices_or_units")
    if out["date"].iloc[-1] > day or not set(out["date"]).issubset(set(sessions)):
        raise ValueError("future_or_noncalendar_daily_dates")
    expected = [d for d in sessions if out["date"].iloc[0] <= d <= day]
    missing = sorted(set(expected) - set(out["date"]))
    return out, {"first": out["date"].iloc[0], "last": out["date"].iloc[-1],
                 "current": out["date"].iloc[-1] == day,
                 "enough_history": len(out) >= min_bars,
                 "missing_dates": missing, "missing_sessions": len(missing),
                 "missing_interpretation": "unknown_suspension_or_source_gap",
                 "volume_unit": "shares", "amount_unit": "CNY", "turnover_unit": "fraction"}


def normalize_source_daily(frame, code):
    if set(FIELDS).issubset(frame.columns):
        return frame[list(FIELDS)].copy()  # 已规范化的备选来源，仍由 daily_quality 完整校验。
    dates = pd.to_datetime(frame["日期"], errors="raise")
    if dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("duplicate_or_reverse_source_dates")
    return normalize_raw(frame, code)


class LiveSources:
    """来源调用在后台执行；规范化数据及采集时间单独留档。"""
    def __init__(self):
        self.price_provider = None
        self.primary_error = None
        self._provider_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_request = 0.

    def native_index_reference(self, day, root, workers, benchmark=None):
        from pipeline import collect_sector_indices
        return collect_sector_indices.run(root, workers, lock_held=True, as_of=day,
                                          get=self.get, benchmark=benchmark)

    def throttle(self):
        with self._request_lock:
            now = time.monotonic()
            delay = max(0., self._next_request - now)
            self._next_request = max(now, self._next_request) + .1
        if delay:
            time.sleep(delay)

    def get(self, *args, **kwargs):
        self.throttle()
        return requests.get(*args, **kwargs)

    def sw_get(self, *args, **kwargs):
        from core.sw_sector_sources import verified_get
        self.throttle()
        return verified_get(*args, **kwargs)

    def calendar(self):
        import akshare as ak
        return ak.tool_trade_date_hist_sina()

    def market(self):
        from core import data_source as ds
        spot, stale = ds.get_market_spot()
        new, new_stale = ds.get_new_stocks_with_status()
        if stale or new_stale:
            raise ValueError("market_or_new_stocks_source_stale")
        return spot, sorted(new)

    def daily(self, code, start, end, adjust):
        import akshare as ak
        def eastmoney():
            self.throttle()
            frame = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start.replace("-", ""),
                                     end_date=end.replace("-", ""), adjust=adjust, timeout=15)
            if frame.empty:
                raise ValueError("empty_primary_daily_response")
            frame.attrs["source"] = "akshare.stock_zh_a_hist"
            return frame
        # 首次探测失败后，该次运行改用备选来源，避免全池重复请求不可用端点。
        if self.price_provider is None:
            with self._provider_lock:
                if self.price_provider is None:
                    try:
                        result = eastmoney()
                        self.price_provider = "eastmoney"
                        return result
                    except Exception as exc:
                        self.price_provider = "tencent"
                        self.primary_error = f"{type(exc).__name__}: {exc}"[:500]
        if self.price_provider == "eastmoney":
            try:
                return eastmoney()
            except Exception as exc:
                with self._provider_lock:
                    self.price_provider = "tencent"
                    self.primary_error = f"{type(exc).__name__}: {exc}"[:500]
        try:
            result = tencent_daily(code, start, end, adjust, get=self.get)
        except ValueError as exc:
            if adjust != "qfq" or str(exc) != "requested_price_basis_not_available":
                raise
            # 原始价仅在独立、完整且可解析的因子证据支持下构造技术价。
            raw = tencent_daily(code, start, end, "", get=self.get)
            result = sina_factor_adjusted(raw, code, end, get=self.get)
        result.attrs["primary_source_error"] = self.primary_error
        return result

    def sectors(self):
        from core.sector_sources import NativeSectors
        self._native_sectors = NativeSectors(get=self.get, sw_get=self.sw_get)
        return self._native_sectors.catalog()

    def membership_for(self, row):
        return self._native_sectors.membership(row)

    def sector_history_for(self, row):
        return self._native_sectors.history(row)

    def membership(self, name):
        from core import data_source as ds
        return ds.resolve_sector_constituents(name)

    def sector_history(self, code):
        from core import data_source as ds
        return ds.get_sector_index_history(code, "industry")

    def benchmark(self):
        from core.strategy_lifecycle import observation_clock
        end = observation_clock().date().isoformat()
        return tencent_benchmark(end, get=self.get), False


def tencent_benchmark(end, get=None):
    """Explicit sh000001 identity; reference OHLC only, no stock-volume conversion."""
    get = get or requests.get
    response = get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        params={"param": f"sh000001,day,,{end},640,"}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    item = payload.get("data", {}).get("sh000001") if isinstance(payload, dict) else None
    rows = item.get("day") if isinstance(item, dict) else None
    if not isinstance(rows, list) or not rows or any(len(row) < 5 for row in rows):
        raise ValueError("benchmark_identity_or_schema_failed")
    frame = pd.DataFrame([row[:5] for row in rows], columns=["date", "open", "close", "high", "low"])
    dates = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
    if dates.duplicated().any() or not dates.is_monotonic_increasing or dates.iloc[-1] > end:
        raise ValueError("benchmark_dates_invalid")
    frame["date"] = dates
    for key in ("open", "close", "high", "low"):
        frame[key] = pd.to_numeric(frame[key], errors="raise")
    prices = frame[["open", "close", "high", "low"]]
    if (not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any()
            or (frame["high"] < prices.max(axis=1)).any() or (frame["low"] > prices.min(axis=1)).any()):
        raise ValueError("benchmark_prices_invalid")
    frame.attrs.update(source="tencent.sh000001", benchmark_symbol="sh000001", raw_response=payload)
    return frame


def tencent_daily(code, start, end, adjust, get=None):
    """腾讯原始价格标识须匹配请求；量为股、额为元、换手率为小数。"""
    from core.data_source import with_prefix
    symbol = with_prefix(code)
    get = get or requests.get
    chunks, keys = [], []
    # 640 条足以覆盖常规 400 日初始化与短区间更新，避免同一窗口按年重复请求。
    if (date.fromisoformat(end) - date.fromisoformat(start)).days <= 700:
        windows = [(start, end)]
    else:
        windows = [(f"{year}-01-01", f"{year + 1}-12-31")
                   for year in range(date.fromisoformat(start).year, date.fromisoformat(end).year + 1)]
    for first, last in windows:
        response = get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            params={"param": f"{symbol},day,{first},{last},640,{adjust}"}, timeout=15)
        response.raise_for_status()
        body = response.json()
        content = body.get("data") if isinstance(body, dict) else None
        payload = content.get(symbol) if isinstance(content, dict) else None
        if not isinstance(payload, dict):
            raise ValueError("invalid_tencent_response_schema")
        key = "day" if not adjust else f"{adjust}day"
        if not payload.get(key):
            raise ValueError("requested_price_basis_not_available")
        rows = payload[key]
        if any(len(row) < 9 for row in rows):
            raise ValueError("tencent_daily_schema_changed")
        chunk = pd.DataFrame([[row[0], row[1], row[3], row[4], row[2], row[5], row[8], row[7]]
                             for row in rows], columns=FIELDS)
        if chunk["date"].duplicated().any() or not chunk["date"].is_monotonic_increasing:
            raise ValueError("duplicate_or_reverse_source_dates")
        chunks.append(chunk)
        keys.append(key)
    combined = pd.concat(chunks, ignore_index=True)
    for field in FIELDS[1:]:
        combined[field] = pd.to_numeric(combined[field], errors="raise")
    overlap = combined[combined["date"].duplicated(keep=False)]
    if not overlap.empty and (overlap.groupby("date")[list(FIELDS[1:])].nunique() > 1).any().any():
        raise ValueError("overlapping_year_prices_disagree")
    result = combined.drop_duplicates("date", keep="last").sort_values("date")
    result = result[(result["date"] >= start) & (result["date"] <= end)].reset_index(drop=True)
    for field in FIELDS[1:]:
        result[field] = pd.to_numeric(result[field], errors="raise")
    # 普通股票原始成交量为手，科创板为股；sz000 开头在此均为股票，不按指数处理。
    if not symbol.startswith("sh688"):
        result["volume"] *= 100
    result["amount"] *= 10000
    result["turnover"] /= 100
    result.attrs.update({"source": "tencent.newfqkline", "response_price_keys": sorted(set(keys)),
                         "volume_unit": "shares", "amount_unit": "CNY", "turnover_unit": "fraction"})
    return result


def sina_factor_adjusted(raw, code, end, get=None):
    """按新浪明确返回的前复权因子转换，不从缺少 qfq 字段推断因子为 1。"""
    from core.data_source import with_prefix
    symbol = with_prefix(code)
    get = get or requests.get
    url = f"https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
    response = get(url, timeout=15)
    response.raise_for_status()
    prefix = f"var {symbol}qfq="
    text = response.text.strip()
    if not text.startswith(prefix):
        raise ValueError("factor_symbol_or_schema_mismatch")
    # JSONDecoder 只解码对象，不执行来源的 JavaScript 或尾随文本。
    payload, _ = json.JSONDecoder().raw_decode(text[len(prefix):])
    rows = payload.get("data", [])
    if not rows or payload.get("total") != len(rows):
        raise ValueError("incomplete_adjustment_factors")
    factors = pd.DataFrame(rows)
    if set(factors.columns) != {"d", "f"}:
        raise ValueError("invalid_adjustment_factor_schema")
    factors["d"] = pd.to_datetime(factors["d"], errors="raise").dt.strftime("%Y-%m-%d")
    factors["f"] = pd.to_numeric(factors["f"], errors="raise")
    if factors["d"].duplicated().any() or not np.isfinite(factors["f"]).all() or (factors["f"] <= 0).any():
        raise ValueError("invalid_adjustment_factors")
    factors = factors[factors["d"] <= end].sort_values("d")
    if factors.empty or factors.iloc[0]["d"] > str(raw.iloc[0]["date"]):
        raise ValueError("adjustment_factor_history_uncovered")
    result = raw.copy()
    lookup = pd.merge_asof(result[["date"]].assign(date=pd.to_datetime(result["date"])),
        factors.assign(d=pd.to_datetime(factors["d"])), left_on="date", right_on="d", direction="backward")
    factor = lookup["f"].to_numpy(dtype=float) / float(factors.iloc[-1]["f"])
    result[["open", "high", "low", "close"]] = result[["open", "high", "low", "close"]].div(factor, axis=0)
    result.attrs.update(source="tencent.raw+sina.qfq_factors", response_price_keys=["day", "sina_qfq_factor"],
                        factor_evidence={"source": url, "symbol": symbol, "as_of": end, "payload": payload},
                        adjustment_method="raw_divide_relative_qfq_factor")
    return result
