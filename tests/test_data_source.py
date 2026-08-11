# -*- coding: utf-8 -*-
import pandas as pd
import pytest
import data_source as ds


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
    c.mark_failure("k")            # 失败1次 → 退避 30s(n 从 0 起)
    clock.t = 30
    assert c.get("k") == ("v", True)     # 恰好 30s 仍新鲜
    clock.t = 31
    assert c.get("k") == ("v", False)    # 超过 30s → stale 回退
    c.mark_failure("k")            # 失败2次 → 退避 60s
    clock.t = 90
    assert c.get("k") == ("v", True)     # 距上次失败 59s < 60s 仍新鲜
    clock.t = 92
    assert c.get("k") == ("v", False)    # 超过 60s → stale


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


def test_new_stocks_english_column(monkeypatch):
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
