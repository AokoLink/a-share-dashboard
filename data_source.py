# -*- coding: utf-8 -*-
"""数据层:akshare 封装 + 线程安全 TTL 缓存。所有 get_* 返回 (data, stale)。"""
import threading
import time
from datetime import datetime

import pandas as pd
import akshare as _ak
import requests

CACHE_MAX = 200
SOURCE_TIMEOUT = 15


class DataSourceError(Exception):
    pass


class TTLCache:
    def __init__(self, max_entries=CACHE_MAX, default_ttl=60, clock=None):
        self._max = max_entries
        self._default_ttl = default_ttl
        self._clock = clock or time.time
        self._data = {}  # key -> (ts, ttl, value, failures)
        self._lock = threading.Lock()

    def get(self, key):
        """返回 (value, fresh);fresh=False 表示已过期但值仍在,可作 stale 回退。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None, False
            ts, ttl, value, _ = item
            if self._clock() - ts <= ttl:
                return value, True
            return value, False

    def set(self, key, value, ttl=None):
        with self._lock:
            if len(self._data) >= self._max and key not in self._data:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (self._clock(), ttl or self._default_ttl, value, 0)

    def mark_failure(self, key):
        """拉取失败:延长 TTL(30s×2^n 封顶 600s),保留 stale 值。"""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return
            ts, _, value, n = item
            backoff = min(30 * (2 ** n), 600)
            self._data[key] = (self._clock(), backoff, value, n + 1)


cache = TTLCache()
last_updated_at = ""  # 模块级:最近成功拉取时间


def _key(func_name, *params):
    return "%s:%s" % (func_name, sorted(str(p) for p in params))


def _set_updated():
    global last_updated_at
    last_updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cached(key, ttl, fetch):
    """缓存取数:命中返回;过期则重拉;重拉失败 → stale 回退 + 退避;全无 → 抛 DataSourceError。"""
    val, fresh = cache.get(key)
    if fresh:
        return val, False
    try:
        data = fetch()
        cache.set(key, data, ttl)
        _set_updated()
        return data, False
    except Exception as e:
        if val is not None:
            cache.mark_failure(key)
            return val, True
        raise DataSourceError(str(e)) from e


def _fetch_with_retry(fetch):
    """失败重试 1 次(规格 §10)。"""
    try:
        return fetch()
    except Exception:
        return fetch()


# ---------- 代码归一化 ----------

def normalize_code(raw):
    s = str(raw).strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[len(p):]
            break
    if s.isdigit():
        return s.zfill(6)
    return s


def with_prefix(code):
    c = normalize_code(code)
    if c.startswith(("6", "5", "9")):
        return "sh" + c
    if c.startswith(("0", "1", "2", "3")):
        return "sz" + c
    if c.startswith(("4", "8")):
        return "bj" + c
    return "sh" + c


# ---------- 腾讯盘口 ----------

def _parse_tencent_quote(text):
    fields = text.split('"')[1].split("~")
    return {
        "name": fields[1],
        "code": fields[2],
        "price": float(fields[3]),
        "prev_close": float(fields[4]),
        "open": float(fields[5]),
        "change_pct": float(fields[32]),
        "high": float(fields[33]),
        "low": float(fields[34]),
        "volume": float(fields[36]) * 100.0,  # 手 → 股(与日线成交量单位一致)
        "turnover": float(fields[37]) * 10000.0,  # 万元 → 元
    }


def get_stock_quote(code):
    symbol = with_prefix(code)

    def fetch():
        url = "https://qt.gtimg.cn/q=" + symbol
        text = requests.get(url, timeout=SOURCE_TIMEOUT).text
        return _parse_tencent_quote(text)

    return _cached(_key("quote", symbol), 30, lambda: _fetch_with_retry(fetch))


# ---------- 新浪 ----------

def _pick(df, *names):
    """按候选列名取列;返回第一个存在的列(Chinese 真实列名优先,English 测试 mock 兜底)。"""
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([None] * len(df))


def get_market_spot():
    def fetch():
        raw = _ak.stock_zh_a_spot()
        out = pd.DataFrame({
            "code": _pick(raw, "代码", "code").map(normalize_code),
            "name": _pick(raw, "名称", "name"),
            "price": _pick(raw, "最新价", "price"),
            "change_pct": _pick(raw, "涨跌幅", "change_pct"),
            "volume": _pick(raw, "成交量", "volume"),
            "amount": _pick(raw, "成交额", "amount"),
        })
        for col in ("price", "change_pct", "volume", "amount"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out

    return _cached(_key("spot"), 60, lambda: _fetch_with_retry(fetch))


INDEX_NAMES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}


def get_index_realtime():
    def fetch():
        raw = _ak.stock_zh_index_spot_sina()
        rows = []
        for _, r in raw.iterrows():
            code = str(r["代码"]).strip().lower()
            if code in INDEX_NAMES:  # 指数代码自带 sh/sz 前缀;勿用 with_prefix 重拼(000001 会错拼成 sz000001)
                rows.append({"code": code, "name": INDEX_NAMES[code],
                             "price": float(r["最新价"]), "change_pct": float(r["涨跌幅"])})
        return rows

    return _cached(_key("index_spot"), 30, lambda: _fetch_with_retry(fetch))


def get_index_daily(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_index_daily(symbol=symbol)
        out = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        out["date"] = out["date"].astype(str)
        return out

    return _cached(_key("index_daily", symbol), 600, lambda: _fetch_with_retry(fetch))


def get_stock_daily(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
        out = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        out["date"] = out["date"].astype(str)
        return out

    return _cached(_key("stock_daily", symbol), 600, lambda: _fetch_with_retry(fetch))


def get_stock_minute(code):
    symbol = with_prefix(code)

    def fetch():
        raw = _ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        price = pd.to_numeric(raw["close"], errors="coerce")
        volume = pd.to_numeric(raw["volume"], errors="coerce")
        amount = pd.to_numeric(raw["amount"], errors="coerce")
        cum_vol = volume.fillna(0).cumsum()
        cum_amt = amount.fillna(0).cumsum()
        avg = cum_amt / cum_vol.where(cum_vol > 0)
        out = pd.DataFrame({
            "time": raw["day"].astype(str).str.slice(11, 16),
            "price": price,
            "avg": avg,
            "volume": volume,
        })
        return out

    return _cached(_key("stock_minute", symbol), 60, lambda: _fetch_with_retry(fetch))


def get_new_stocks():
    """上市≤5交易日的股票(尽力而为):取最近新股列表;接口不可用 → 空集。"""
    try:
        raw = _ak.stock_zh_a_new()
        codes = _pick(raw, "代码", "code").map(normalize_code).tolist()
        return set(codes)
    except Exception:
        return set()


# ---------- 同花顺 ----------

def _ths_col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([None] * len(df))


def _industry_code_map():
    """name→code 对照表(摘要无板块代码列),静态映射,缓存 1h。"""
    def fetch():
        raw = _ak.stock_board_industry_name_ths()
        return dict(zip(raw["name"], raw["code"].astype(str)))
    return _cached(_key("industry_name_code"), 3600, lambda: _fetch_with_retry(fetch))


def get_sector_summary(type):
    if type != "industry":
        raise DataSourceError("unknown type: %s" % type)

    def fetch():
        raw = _ak.stock_board_industry_summary_ths()
        name_map, _ = _industry_code_map()
        out = pd.DataFrame({
            "code": raw["板块"].map(name_map).map(normalize_code),
            "name": raw["板块"],
            "change_pct": pd.to_numeric(_ths_col(raw, "涨跌幅"), errors="coerce"),
            "up_count": pd.to_numeric(_ths_col(raw, "上涨家数"), errors="coerce"),
            "down_count": pd.to_numeric(_ths_col(raw, "下跌家数"), errors="coerce"),
            "leader": _ths_col(raw, "领涨股"),
            "leader_change_pct": pd.to_numeric(_ths_col(raw, "领涨股-涨跌幅"), errors="coerce"),
            "turnover": pd.to_numeric(_ths_col(raw, "总成交额"), errors="coerce") * 1e8,  # 亿元 → 元
        })
        out = out[out["code"].astype(str).str.isdigit()].reset_index(drop=True)  # 防御:名→码失败的行剔除
        return out.where(pd.notna(out), None)  # NaN → None,避免 NaN 污染 JSON/排序

    return _cached(_key("sector_summary", type), 60, lambda: _fetch_with_retry(fetch))


def get_sector_index_history(code, type):
    if type != "industry":
        raise DataSourceError("unknown type: %s" % type)
    symbol = normalize_code(code)
    end_date = datetime.now().strftime("%Y%m%d")  # 接口默认 end_date 已过期(20240108),必须显式传当天

    def fetch():
        raw = _ak.stock_board_industry_index_ths(symbol=symbol, start_date="20200101", end_date=end_date)
        return pd.DataFrame({
            "date": raw["日期"].astype(str),
            "open": raw["开盘价"], "high": raw["最高价"],
            "low": raw["最低价"], "close": raw["收盘价"],
            "volume": raw["成交量"],
        })

    return _cached(_key("sector_index", type, symbol), 1800, lambda: _fetch_with_retry(fetch))
