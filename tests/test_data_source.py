# -*- coding: utf-8 -*-
import pandas as pd
import pytest
from core import data_source as ds


class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t


def make_spot():
    return pd.DataFrame({
        "code": ["sh600000", "600519", "sz000001", "sh688981", "300750"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际", "宁德时代"],
        "price": [10.0, 1348.9, 12.0, 45.0, 200.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0, 0.0],
        "volume": [100000, 682720, 80000, 0, 90000],
        "amount": [1e8, 9.2e8, 2e8, 0, 5e8],
    })


def test_normalize_and_prefix():
    assert ds.normalize_code("sh600000") == "600000"
    assert ds.normalize_code("600519") == "600519"
    assert ds.normalize_code("SZ000001") == "000001"
    assert ds.with_prefix("600519") == "sh600519"
    assert ds.with_prefix("000001") == "sz000001"
    assert ds.with_prefix("830000") == "bj830000"


def test_ttl_cache_fresh_then_expire():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=5, default_ttl=60, clock=clock)
    c.set("k", "v", 60)
    assert c.get("k") == ("v", True)
    clock.t = 61
    assert c.get("k") == ("v", False)  # 过期但值仍在(stale 可回退)


def test_ttl_cache_evict_oldest():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=2, default_ttl=60, clock=clock)
    c.set("a", 1); clock.t = 1
    c.set("b", 2); clock.t = 2
    c.set("c", 3)  # 淘汰最旧 a
    assert c.get("a") == (None, False)
    assert c.get("c") == (3, True)


def test_ttl_cache_mark_failure_backoff():
    clock = FakeClock()
    c = ds.TTLCache(max_entries=5, default_ttl=60, clock=clock)
    c.set("k", "v")
    clock.t = 61
    c.mark_failure("k")            # 旧数据已过期，失败后退避 30s
    assert c.get("k") == ("v", False)
    assert c.get_with_retry("k") == ("v", False, False)
    clock.t = 90
    assert c.get_with_retry("k") == ("v", False, False)
    clock.t = 91
    assert c.get_with_retry("k") == ("v", False, True)
    c.mark_failure("k")            # 再次失败，退避 60s
    clock.t = 150
    assert c.get_with_retry("k") == ("v", False, False)
    clock.t = 151
    assert c.get_with_retry("k") == ("v", False, True)


def test_cached_failure_keeps_stale_until_successful_retry(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ds, "cache", ds.TTLCache(clock=clock))
    calls = {"n": 0}
    def fetch():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("source down")
        return {"revision": calls["n"]}
    assert ds._cached("probe", 60, fetch) == ({"revision": 1}, False)
    clock.t = 61
    assert ds._cached("probe", 60, fetch) == ({"revision": 1}, True)
    clock.t = 80
    assert ds._cached("probe", 60, fetch) == ({"revision": 1}, True)
    assert calls["n"] == 2  # 退避期间不重拉，但始终标记为过期
    clock.t = 91
    assert ds._cached("probe", 60, fetch) == ({"revision": 3}, False)
    assert ds._cached("probe", 60, fetch) == ({"revision": 3}, False)


def test_parse_tencent_quote():
    # 40 槽字段:idx0..5 名称/代码/最新价/昨收/今开,idx6..29 填充,
    # idx30 时间,31 涨跌额,32 涨跌幅,33 最高,34 最低,36 成交量(手),37 成交额(万元)
    fields = ["1", "贵州茅台", "600519", "1348.90", "1345.00", "1348.00"]
    fields += ["0"] * 24                                  # idx6..29
    fields += ["20260811103001", "3.90", "0.29", "1352.65", "1338.18"]  # idx30..34
    fields += ["0"]                                       # idx35
    fields += ["682720", "91903.5968"]                    # idx36..37
    fields += ["0"] * 2
    text = 'v_sh600519="' + "~".join(fields) + '";'
    q = ds._parse_tencent_quote(text)
    assert q["name"] == "贵州茅台"
    assert q["price"] == pytest.approx(1348.90)
    assert q["change_pct"] == pytest.approx(0.29)
    assert q["high"] == pytest.approx(1352.65)
    assert q["low"] == pytest.approx(1338.18)
    assert q["volume"] == pytest.approx(682720.0 * 100)      # 手 → 股
    assert q["turnover"] == pytest.approx(91903.5968 * 10000)  # 万元 → 元


def test_market_spot_normalized(monkeypatch):
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: make_spot())
    df, stale = ds.get_market_spot()
    assert stale is False
    assert list(df["code"]) == ["600000", "600519", "000001", "688981", "300750"]


def test_market_spot_stale_on_failure(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return make_spot()
        raise RuntimeError("network down")

    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", flaky)
    # 可控时钟 + 清空模块级缓存,避免污染其他用例
    clock = FakeClock()
    ds.cache._clock = clock
    ds.cache._data.clear()
    df, stale1 = ds.get_market_spot()
    assert stale1 is False
    # 缓存命中(60s 内),不触发 fetch
    df2, stale2 = ds.get_market_spot()
    assert stale2 is False and calls["n"] == 1
    # 超过 TTL 后重试失败 → stale 回退
    clock.t = 61
    df3, stale3 = ds.get_market_spot()
    assert stale3 is True and df3["code"].iloc[0] == "600000"


def test_sector_summary_mapping(monkeypatch):
    raw = pd.DataFrame({
        "板块": ["半导体", "白酒"],
        "涨跌幅": [3.2, -1.0],
        "上涨家数": [80, 30],
        "下跌家数": [5, 60],
        "领涨股": ["X", "Y"],
        "领涨股-涨跌幅": [10.0, 0.5],
    })
    name_map = pd.DataFrame({"name": ["半导体", "白酒"], "code": ["881121", "881273"]})
    monkeypatch.setattr(ds._ak, "stock_board_industry_summary_ths", lambda: raw)
    monkeypatch.setattr(ds._ak, "stock_board_industry_name_ths", lambda: name_map)
    df, _ = ds.get_sector_summary("industry")
    assert list(df["code"]) == ["881121", "881273"]   # code 来自 name→code 表关联
    assert df["turnover"].isna().all()  # 缺总成交额 → 全 None(分析层自动降级)
    assert df["change_pct"].iloc[0] == pytest.approx(3.2)


def test_stock_minute_normalized(monkeypatch):
    # 分时走 stock_zh_a_minute(period='1'),均价 = 累计成交额/累计成交量
    raw = pd.DataFrame({
        "day": ["2026-08-11 10:00:00", "2026-08-11 10:01:00"],
        "open": [1347.0, 1348.5], "high": [1350.0, 1351.0],
        "low": [1346.0, 1347.0], "close": [1348.0, 1349.0],
        "volume": [12000, 5000], "amount": [1.6e7, 6.7e6],
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_minute",
                        lambda symbol, period, adjust: raw)
    df, _ = ds.get_stock_minute("sh600519")
    assert list(df["time"]) == ["10:00", "10:01"]
    assert list(df["avg"]) == pytest.approx([1.6e7 / 12000, 2.27e7 / 17000])


def test_stock_minute_nan_to_none(monkeypatch):
    # Fix:累计量为 0 处 avg 产 NaN → 归一为 None(避免 Flask 序列化出非法 NaN JSON)
    # 用 sh600518(非 test_stock_minute_normalized 的 sh600519):两者同 symbol 会互相命中缓存,
    # 使本任务定向测试命令(nan_to_none 先于 normalized)与全量套件均无法同时通过。
    ds.cache._data.clear()  # 隔离:清模块级缓存,避免被其他用例的 stock_minute:* 命中
    raw = pd.DataFrame({
        "day": ["2026-08-11 10:00:00", "2026-08-11 10:01:00"],
        "open": [1347.0, 1348.5], "high": [1350.0, 1351.0],
        "low": [1346.0, 1347.0], "close": [1348.0, 1349.0],
        "volume": [0, 5000], "amount": [0.0, 6.7e6],   # 首根累计量为 0 → avg NaN
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_minute",
                        lambda symbol, period, adjust: raw)
    df, _ = ds.get_stock_minute("sh600518")
    assert df["avg"].iloc[0] is None          # NaN 归一为 None
    assert df["avg"].iloc[1] == pytest.approx(6.7e6 / 5000)


def test_get_stock_daily_preserves_amount_turnover(monkeypatch):
    # 回归:get_stock_daily 曾只选 6 列,丢掉 akshare 已返回的 amount/outstanding_share/turnover,
    # 使板块级 composite(turnover/amount)退化为代理、预测层无方向优势。须全列保留。
    # 2026-08-25 起改用东财 stock_zh_a_hist(纯 HTTP),弃新浪 stock_zh_a_daily:
    # 后者每次调用 new 一个 MiniRacer 解密 JS,多线程并发时 V8 崩溃打死进程。
    # 东财单位:成交量=手、换手率=%;须折算成与离线 pkl 一致(volume=股、turnover=小数、outstanding 反推)。
    ds.cache._data.clear()  # 隔离模块级缓存
    raw = pd.DataFrame({
        "日期": ["2026-01-02", "2026-01-03"],
        "股票代码": ["000002", "000002"],
        "开盘": [10.0, 10.1], "收盘": [10.1, 10.2], "最高": [10.2, 10.3], "最低": [9.9, 10.0],
        "成交量": [1e6, 1.1e6], "成交额": [1.0e8, 1.1e8],
        "振幅": [3.0, 3.0], "涨跌幅": [1.0, 1.0], "涨跌额": [0.1, 0.1], "换手率": [2.0, 2.2],
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_hist",
                        lambda symbol, period, adjust: raw)
    df, stale = ds.get_stock_daily("000002")
    assert stale is False
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume",
                                "amount", "outstanding_share", "turnover"]
    assert df["volume"].iloc[0] == pytest.approx(1.0e8)          # 手 → 股(×100)
    assert df["amount"].iloc[0] == pytest.approx(1.0e8)
    assert df["turnover"].iloc[1] == pytest.approx(0.022)        # % → 小数(/100)
    assert df["outstanding_share"].iloc[1] == pytest.approx(1.1e6 * 100 / 0.022)


def test_new_stocks_english_column(monkeypatch):
    ds.cache._data.clear()  # 避免被其他用例缓存污染
    # akshare 1.18.84 的 stock_zh_a_new() 返回英文列(无“代码”列),须能经 code 列归一化
    raw = pd.DataFrame({
        "symbol": ["bj920000", "sh688001"],
        "code": ["920000", "688001"],
        "name": ["A", "B"], "open": [1.0, 2.0], "high": [1.1, 2.1],
        "low": [0.9, 1.9], "volume": [100, 200], "amount": [1e5, 2e5],
        "mktcap": [1e8, 2e8], "turnoverratio": [1.0, 2.0],
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", lambda: raw)
    assert ds.get_new_stocks() == {"920000", "688001"}


def test_sector_index_history_uses_name_not_code(monkeypatch):
    # 回归:THS 板块指数接口按板块名称查询,不能直接用摘要的代码 symbol
    monkeypatch.setattr(ds, "_industry_code_map", lambda: ({"半导体": "881121"}, False))
    captured = {}
    def fake_index(symbol, start_date, end_date):
        captured["symbol"] = symbol
        return pd.DataFrame({"日期": ["2026-08-10"], "开盘价": [1.0], "最高价": [2.0],
                             "最低价": [0.5], "收盘价": [1.5], "成交量": [100]})
    monkeypatch.setattr(ds._ak, "stock_board_industry_index_ths", fake_index)
    df, stale = ds.get_sector_index_history("881121", "industry")
    assert captured["symbol"] == "半导体"        # 传的是名称,不是代码
    assert stale is False
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["close"].iloc[0] == pytest.approx(1.5)


def test_new_stocks_cached_on_success(monkeypatch):
    calls = {"n": 0}
    def fake():
        calls["n"] += 1
        return pd.DataFrame({"code": ["920000"], "name": ["A"]})
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fake)
    clock = FakeClock()
    monkeypatch.setattr(ds.cache, "_clock", clock)   # monkeypatch 自动还原,避免污染后续用例
    ds.cache._data.clear()
    assert ds.get_new_stocks() == {"920000"}
    assert ds.get_new_stocks() == {"920000"}
    assert calls["n"] == 1                       # 缓存命中,不再拉取
    clock.t = 1801
    monkeypatch.setattr(ds._ak, "stock_zh_a_new",
                        lambda: pd.DataFrame({"code": ["920001"], "name": ["B"]}))
    assert ds.get_new_stocks() == {"920001"}     # 过期后重新拉取


def test_new_stocks_not_cached_on_failure(monkeypatch):
    calls = {"n": 0}
    def fail():
        calls["n"] += 1
        raise RuntimeError("network down")
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fail)
    ds.cache._data.clear()
    assert ds.get_new_stocks() == set()          # 失败 → 空集
    assert ds.get_new_stocks() == set()          # 且不缓存:再次调用仍重试
    assert calls["n"] == 4                       # 2 次调用 × _fetch_with_retry 内部重试 1 次


def test_new_stocks_status_distinguishes_failed_fallback(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ds, "cache", ds.TTLCache(clock=clock))
    calls = {"n": 0}
    def fail():
        calls["n"] += 1
        raise RuntimeError("source down")
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fail)
    assert ds.get_new_stocks_with_status() == (set(), True)
    monkeypatch.setattr(ds._ak, "stock_zh_a_new",
                        lambda: pd.DataFrame({"code": ["920000"]}))
    assert ds.get_new_stocks_with_status() == ({"920000"}, False)
    clock.t = 1801
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fail)
    assert ds.get_new_stocks_with_status() == ({"920000"}, True)
    failure_calls = calls["n"]
    legacy = ds.get_new_stocks()
    assert legacy == {"920000"}
    assert legacy.stale is True
    assert calls["n"] == failure_calls  # 退避期间不重复请求
    clock.t += 31
    monkeypatch.setattr(ds._ak, "stock_zh_a_new",
                        lambda: pd.DataFrame({"code": ["920001"]}))
    assert ds.get_new_stocks_with_status() == ({"920001"}, False)


def _mock_sina_spot(monkeypatch):
    rows = [{"label": label, "板块": name} for label, name in ds.SECTOR_CONS_EXPECTED.items()]
    df = pd.DataFrame(rows)
    df["公司家数"] = 1; df["涨跌额"] = 0.0; df["涨跌幅"] = 1.0
    df["总成交量"] = 1; df["总成交额"] = 1.0; df["股票代码"] = "a"
    df["领涨股-涨跌幅"] = 1.0; df["领涨股-当前价"] = 1.0; df["领涨股"] = "a"
    monkeypatch.setattr(ds._ak, "stock_sector_spot", lambda indicator: df)


def test_constituents_manual_mapping(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    monkeypatch.setattr(ds._ak, "stock_sector_detail",
                        lambda sector: pd.DataFrame({"symbol": ["sh600050", "sh600100"],
                                                     "code": ["600050", "600100"],
                                                     "name": ["中国联通", "同方股份"]}))
    res = ds.resolve_sector_constituents("白酒")
    assert res["ok"] is True
    assert res["match_type"] == "manual"
    assert res["codes"] == ["600050", "600100"]
    assert res["source_name"] == "酿酒行业"
    assert res["historical"] is False


def test_constituent_fallback_is_marked_stale(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(ds, "cache", ds.TTLCache(clock=clock))
    _mock_sina_spot(monkeypatch)
    monkeypatch.setattr(ds._ak, "stock_sector_detail",
                        lambda sector: pd.DataFrame({"symbol": ["sh600050"]}))
    first = ds.resolve_sector_constituents("白酒")
    assert first["ok"] and first["stale"] is False
    clock.t = 1801
    def fail(*args, **kwargs):
        raise RuntimeError("source down")
    monkeypatch.setattr(ds._ak, "stock_sector_spot", fail)
    monkeypatch.setattr(ds._ak, "stock_sector_detail", fail)
    old = ds.resolve_sector_constituents("白酒")
    assert old["ok"] and old["stale"] is True
    assert old["observed_at"] is None


def test_broad_electronics_pool_is_not_sold_as_semiconductor(monkeypatch):
    monkeypatch.setattr(ds, "_exact_em_constituents", lambda name: None)
    monkeypatch.setattr(ds, "_fetch_sina_constituents",
                        lambda label: pytest.fail("细分行业不可读取宽泛的新浪成分池"))
    res = ds.resolve_sector_constituents("半导体")
    assert res["ok"] is False
    assert res["reason"] == "coverage_insufficient"


def test_precise_sector_uses_exact_em_name(monkeypatch):
    ds.cache._data.clear()
    monkeypatch.setattr(ds._ak, "stock_board_industry_name_em",
                        lambda: pd.DataFrame({"板块名称": ["半导体", "消费电子"]}))
    monkeypatch.setattr(ds._ak, "stock_board_industry_cons_em",
                        lambda symbol: pd.DataFrame({"代码": ["688981"] if symbol == "半导体" else ["002475"]}))
    semiconductor = ds.resolve_sector_constituents("半导体")
    electronics = ds.resolve_sector_constituents("消费电子")
    assert semiconductor["codes"] == ["688981"]
    assert electronics["codes"] == ["002475"]
    assert semiconductor["source"] == "eastmoney_industry"


def test_constituents_no_mapping_and_ambiguous(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    assert ds.resolve_sector_constituents("绝对不存在的板块")["reason"] == "no_mapping"
    # 关键词兜底:双向包含命中但多命中 → ambiguous(不静默取第一个)
    label_to_name = {"new_dzxx": "电子信息", "new_dzqj": "电子器件"}
    assert ds._keyword_lookup("电子", label_to_name) == ("new_dzxx", True)


def test_validate_sector_map_detects_stale_and_renamed(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    # 临时替换常量,验证检测逻辑(不改动正式常量)
    monkeypatch.setattr(ds, "SECTOR_CONS_MAP", {"半导体": "new_dzxx", "坏映射": "new_xxxx"})
    monkeypatch.setattr(ds, "SECTOR_CONS_EXPECTED", {"new_dzxx": "电子信息", "new_xxxx": "旧名"})
    h = ds.validate_sector_map()
    assert h["ok"] is True and h["total"] == 2 and h["valid"] == 1
    assert h["stale"] == [{"ths": "坏映射", "label": "new_xxxx"}]
    assert h["renamed"] == []                            # new_dzxx 名未变
    monkeypatch.setattr(ds, "SECTOR_CONS_EXPECTED", {"new_dzxx": "旧名"})
    h2 = ds.validate_sector_map()
    assert h2["renamed"][0]["ths"] == "半导体" and h2["renamed"][0]["actual"] == "电子信息"


def test_constituents_map_keys_values_valid(monkeypatch):
    _mock_sina_spot(monkeypatch)
    assert all(isinstance(k, str) and k for k in ds.SECTOR_CONS_MAP)
    # 每个映射 value 都必须是有效新浪 label(与 SECTOR_CONS_EXPECTED 全集一致)
    assert set(ds.SECTOR_CONS_MAP.values()) <= set(ds.SECTOR_CONS_EXPECTED)


def test_resolve_code_sectors_manual_map():
    assert "白酒" in ds.resolve_code_sectors("600519")
    assert "半导体" in ds.resolve_code_sectors("600050")
    assert ds.resolve_code_sectors("sh600519") == ds.resolve_code_sectors("600519")  # 归一化
    assert ds.resolve_code_sectors("999999") == []      # 未映射 → 空


def test_market_spot_includes_open(monkeypatch):
    ds.cache._data.clear()                          # 避免被其他用例缓存污染(与 test_market_spot_stale_on_failure 同模式)
    raw = make_spot().copy()
    raw["今开"] = [9.9, 1340.0, 11.8, 44.5, 199.0]  # 东财 spot 列名「今开」
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: raw)
    df, stale = ds.get_market_spot()
    assert stale is False and "open" in df.columns
    assert float(df.loc[0, "open"]) == pytest.approx(9.9)   # sh600000 今开 9.9


def test_market_spot_sina_share_units_and_high_low(monkeypatch):
    ds.cache._data.clear()                          # 避免被其他用例缓存污染
    raw = make_spot().copy()
    raw["成交量"] = [100000, 200000, 300000, 0, 400000]  # 新浪原始单位为股
    raw["amount"] = [1e6, 2.7e8, 3.6e6, 0, 8e7]
    raw["最高"] = [10.5, 1400.0, 12.5, 45.0, 210.0]
    raw["最低"] = [9.5, 1330.0, 11.5, 44.0, 190.0]
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: raw)
    df, stale = ds.get_market_spot()
    assert stale is False
    assert df.loc[0, "volume"] == pytest.approx(100000.0)
    assert df.loc[0, "amount"] / df.loc[0, "volume"] == pytest.approx(10.)
    assert df.loc[0, "high"] == pytest.approx(10.5)
    assert df.loc[0, "low"] == pytest.approx(9.5)
