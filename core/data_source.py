# -*- coding: utf-8 -*-
"""数据层:akshare 封装 + 线程安全 TTL 缓存。所有 get_* 返回 (data, stale)。"""
import json
import os
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


# ---- 推荐:THS 板块 → 新浪行业成分股 映射(规格 §5) ----
# 新浪为旧分类(49 板块),仅收录有清晰对应的 THS 板块;未覆盖板块由上层标记 no_mapping。
SECTOR_CONS_MAP = {
    "白酒": "new_ljhy", "白色家电": "new_jdhy", "黑色家电": "new_jdhy", "小家电": "new_jdhy",
    "电力": "new_dlhy", "房地产": "new_fdc", "钢铁": "new_gthy",
    "服装家纺": "new_fzxl", "纺织制造": "new_fzhy",
    "环保设备": "new_hbhy", "环境治理": "new_hbhy",
    "建筑材料": "new_jzjc", "建筑装饰": "new_jzjc",
    "旅游及酒店": "new_jdly", "煤炭开采加工": "new_mthy",
    "农化制品": "new_nyhf", "汽车整车": "new_qczz", "汽车零部件": "new_qczz",
    "燃气": "new_gsgq", "塑料制品": "new_slzp", "食品加工制造": "new_sphy",
    "石油加工贸易": "new_syhy", "有色金属": "new_ysjs", "贵金属": "new_ysjs",
    "造纸": "new_zzhy", "医疗器械": "new_ylqx", "生物制品": "new_swzz",
    "半导体": "new_dzxx", "消费电子": "new_dzxx", "通信设备": "new_dzxx",
    "计算机设备": "new_dzxx", "软件开发": "new_dzxx",
    "光学光电子": "new_dzqj", "元件": "new_dzqj",
    "工程机械": "new_jxhy", "通用设备": "new_jxhy", "专用设备": "new_jxhy",
}
SECTOR_CONS_EXPECTED = {   # label → 预期新浪名,启动校验检测改名漂移
    "new_ljhy": "酿酒行业", "new_jdhy": "家电行业", "new_dlhy": "电力行业",
    "new_fdc": "房地产", "new_gthy": "钢铁行业", "new_fzxl": "服装鞋类",
    "new_fzhy": "纺织行业", "new_hbhy": "环保行业", "new_jzjc": "建筑建材",
    "new_jdly": "酒店旅游", "new_mthy": "煤炭行业", "new_nyhf": "农药化肥",
    "new_qczz": "汽车制造", "new_gsgq": "供水供气", "new_slzp": "塑料制品",
    "new_sphy": "食品行业", "new_syhy": "石油行业", "new_ysjs": "有色金属",
    "new_zzhy": "造纸行业", "new_ylqx": "医疗器械", "new_swzz": "生物制药",
    "new_dzxx": "电子信息", "new_dzqj": "电子器件", "new_jxhy": "机械行业",
}
SECTOR_KEYWORDS = {          # THS 名 → 可接受的新浪行业名(同义词兜底,仅高置信)
    "半导体": ["电子信息", "电子器件"],
    "白酒": ["酿酒行业"],
}


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


# ---------- 个股 → 板块(规格 §5) ----------

_SECTOR_CODES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sector_codes.json")
_sector_codes = None


def _load_sector_codes():
    global _sector_codes
    if _sector_codes is None:
        with open(_SECTOR_CODES_PATH, encoding="utf-8") as f:
            _sector_codes = json.load(f)
    return _sector_codes


def resolve_code_sectors(code: str) -> list[str]:
    """code → THS 板块名列表(手动映射表优先,规格 §5)。未映射 → []。"""
    return list(_load_sector_codes().get(normalize_code(code), []))


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
            "open": _pick(raw, "今开", "open"),
            "high": _pick(raw, "最高", "high"),
            "low": _pick(raw, "最低", "low"),
        })
        for col in ("price", "change_pct", "volume", "amount", "open", "high", "low"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out["volume"] = out["volume"] * 100.0   # 手 → 股(与日线成交量单位一致,对齐腾讯路径 :186)
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
        out = raw[["date", "open", "high", "low", "close", "volume",
                   "amount", "outstanding_share", "turnover"]].copy()
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
        return out.astype(object).where(pd.notna(out), None)  # NaN → None,避免 NaN 污染 JSON/排序

    return _cached(_key("stock_minute", symbol), 60, lambda: _fetch_with_retry(fetch))


def get_new_stocks():
    """上市≤5交易日的股票(尽力而为):取最近新股列表;接口不可用 → 空集。
    仅缓存成功结果;失败不缓存(下次仍重试),并回退旧值。"""
    def fetch():
        raw = _ak.stock_zh_a_new()
        codes = _pick(raw, "代码", "code").map(normalize_code).tolist()
        return set(codes)

    val, fresh = cache.get(_key("new_stocks"))
    if fresh:
        return val
    try:
        data = _fetch_with_retry(fetch)
        cache.set(_key("new_stocks"), data, 1800)
        _set_updated()
        return data
    except Exception:
        return val if val is not None else set()


def _sina_industry_names():
    """新浪行业 spot:label→name 对照,缓存 1800s。"""
    def fetch():
        raw = _ak.stock_sector_spot(indicator="新浪行业")
        return {str(r["label"]): str(r["板块"]) for _, r in raw.iterrows()}
    return _cached(_key("sina_industry_names"), 1800, lambda: _fetch_with_retry(fetch))


def _fetch_sina_constituents(label):
    """新浪板块成分股 → 6 位代码列表,缓存 1800s。"""
    def fetch():
        raw = _ak.stock_sector_detail(sector=label)
        codes = _pick(raw, "code", "symbol").astype(str).map(normalize_code).tolist()
        return [c for c in codes if c.isdigit()]
    return _cached(_key("sector_cons", label), 1800, lambda: _fetch_with_retry(fetch))


def _keyword_lookup(ths_name, label_to_name):
    """关键词兜底:双向包含 + SECTOR_KEYWORDS 同义词语料。→ (label, ambiguous) | None。"""
    cands = []
    for label, name in label_to_name.items():
        if ths_name in name or name in ths_name:
            cands.append(label)
        elif ths_name in SECTOR_KEYWORDS and name in SECTOR_KEYWORDS[ths_name]:
            cands.append(label)
    if not cands:
        return None
    uniq = list(dict.fromkeys(cands))
    return uniq[0], len(uniq) > 1


def resolve_sector_constituents(ths_name):
    """板块名 → 成分股(手动表 → 关键词兜底)。失败原因 no_mapping/ambiguous;网络失败抛异常。"""
    label = SECTOR_CONS_MAP.get(ths_name)
    if label is not None:
        codes, _ = _fetch_sina_constituents(label)
        names, _ = _sina_industry_names()
        return {"ok": True, "codes": codes, "match_type": "manual",
                "source_name": names.get(label, label)}
    names, _ = _sina_industry_names()
    hit = _keyword_lookup(ths_name, names)
    if hit is None:
        return {"ok": False, "reason": "no_mapping"}
    label, ambiguous = hit
    if ambiguous:
        return {"ok": False, "reason": "ambiguous"}
    codes, _ = _fetch_sina_constituents(label)
    return {"ok": True, "codes": codes, "match_type": "keyword",
            "source_name": names.get(label, label)}


def validate_sector_map():
    """启动校验:手动映射的每个新浪 label 是否仍存在、名称是否漂移。失败不阻塞,仅返回报告。"""
    try:
        names, _ = _sina_industry_names()
    except Exception as e:
        return {"ok": False, "error": str(e), "total": 0, "valid": 0,
                "stale": [], "renamed": []}
    stale, renamed = [], []
    for ths, label in SECTOR_CONS_MAP.items():
        if label not in names:
            stale.append({"ths": ths, "label": label})
        else:
            expected = SECTOR_CONS_EXPECTED.get(label)
            if expected and names[label] != expected:
                renamed.append({"ths": ths, "label": label,
                                "expected": expected, "actual": names[label]})
    return {"ok": True, "total": len(SECTOR_CONS_MAP),
            "valid": len(SECTOR_CONS_MAP) - len(stale),
            "stale": stale, "renamed": renamed}


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
        name_map, _ = _industry_code_map()
        inv = {v: k for k, v in name_map.items()}          # code→name(THS 板块指数接口按名称查询)
        name = inv.get(symbol)
        if name is None:
            raise DataSourceError("unknown sector code: %s" % symbol)
        raw = _ak.stock_board_industry_index_ths(symbol=name, start_date="20200101", end_date=end_date)
        return pd.DataFrame({
            "date": raw["日期"].astype(str),
            "open": raw["开盘价"], "high": raw["最高价"],
            "low": raw["最低价"], "close": raw["收盘价"],
            "volume": raw["成交量"],
        })

    return _cached(_key("sector_index", type, symbol), 1800, lambda: _fetch_with_retry(fetch))
