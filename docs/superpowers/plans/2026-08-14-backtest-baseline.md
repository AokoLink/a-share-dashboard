# 历史盲测基线模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 gitignored 探针 `_analysis/nxday_backtest.py` 的核心次日盲测路径固化为一个提交入库、可测、可复现、带版本戳的 `backtest.py` 模块,量出当前版本(HEAD)的次日准确率基线。

**Architecture:** 单平铺模块 `backtest.py`(与 `analysis.py` 平级),纯函数 + 依赖注入(path / `now` / `get_score` / `rng` 传参)+ argparse CLI。只读 `_analysis/daily/*.pkl` 与 `_analysis/code2sector.json`(GBK),不 fetch。`import analysis as an`。无未来数据泄露:评分输入 `d.iloc[:i+1]`(≤T),验证读 `i+1`(T+1)。

**Tech Stack:** Python 3 + pandas + numpy(无 scipy 依赖,Welch t 检验手写)。pytest。

**Spec:** `docs/superpowers/specs/2026-08-14-backtest-baseline-design.md`(v3,权威)。

## Global Constraints

- **只读本地缓存,严禁网络 fetch**(`--data-dir` 指向 `_analysis/daily/*.pkl`,默认 `_analysis/daily`)。
- **`_analysis/` 是 gitignored**,探针与缓存永不提交;但本计划产出的 `backtest.py`、`tests/test_backtest.py` 及计划/报告**须提交**。
- **`_analysis/code2sector.json` 为 GBK 编码**,必须 `open(..., encoding="gbk")` 显式打开(不得靠 Windows 默认隐式解码)。
- **GBK 控制台约束:Python 源码内禁止 U+2212 `−`(GBK 无法编码),减号一律用 ASCII `-`**;`≤`/`≥` 可用(GBK 可编码)。
- **直接提交到 main**(用户既有工作流,无 worktree、无 feature branch)。提交信息中文、conventional、结尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- **导入方式与仓库一致**:`import analysis as an`、`import backtest as bt`(测试内)。仓库根 `conftest.py` 已把根目录插入 `sys.path`。
- **常量逐字**(spec §5.1):`TOP_SECTORS=3`、`PER_SECTOR=5`、`EVAL_DAYS=1200`、`B_SAMPLE_EVERY=10`、`MIN_AMOUNT=1e8`、`AFTER_CLOSE=datetime(2026,1,1,15,1)`。
- **确定性**(spec §5.5/§5.6 C):`sector_members` 按 code2sector.json 文件顺序构建(且 `if c in universe`);`buyable` 迭代用 `sorted(buyable)`;C 候选用 `sorted(hot_members ∩ buyable)`;C 的 `rng` 为模块级单实例 `np.random.default_rng(0)`、评估日复用、不重建。
- **测试约定**:`pytest.approx` 断言浮点、`monkeypatch.setattr` 做 mock、`tmp_path` 做临时目录、`is None`/`is True` 断言布尔、文件内 `make_daily` 辅助(不共享 conftest fixture)。
- 全量回归 `python -m pytest tests/ -q` 须保持 **129 + 新增全部通过**。

## File Structure

- **Create `backtest.py`** — 模块本体。职责分层:数据加载(`load_daily`/`load_sector_map`)→ 全市场/日历(`build_universe`/`build_calendar`/`build_sector_members`)→ 目标(`next_returns`)→ 板块热度(`_win_gain`/`sector_heat`)→ 评分(`score_at`)→ 可买集合(`build_buyable`)→ 篮子(`select_baskets`)→ 度量(`stats`/`summ`/`summ_year`/`welch_t`)→ 主循环(`run`)→ 报告与 CLI(`build_report`/`render_markdown`/`main`)。
- **Create `tests/test_backtest.py`** — 对应测试。文件内 `make_daily` 辅助。

依赖顺序(任务顺序即实现顺序):数据加载 → 全市场/日历 → 目标 → 板块热度 → 评分 → 可买+篮子 → 度量 → 主循环+报告+CLI。

---

### Task 1: 模块骨架 + 常量 + 数据加载

**Files:**
- Create: `backtest.py`
- Create: `tests/test_backtest.py`

**Interfaces:**
- Produces: 常量 `MODULE_VERSION/TOP_SECTORS/PER_SECTOR/EVAL_DAYS/B_SAMPLE_EVERY/MIN_AMOUNT/AFTER_CLOSE/RNG`;函数 `load_daily(data_dir, code) -> DataFrame`、`load_sector_map(path) -> dict`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_backtest.py`:

```python
# -*- coding: utf-8 -*-
import json

import numpy as np
import pandas as pd
import pytest

import backtest as bt
import analysis as an


def make_daily(closes, opens=None, volumes=None, start="2026-01-01"):
    """构造日线 DataFrame,date 递增。open 缺省 = close;volume 缺省 100000。"""
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    vols = [float(v) for v in volumes] if volumes is not None else [100000.0] * n
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": [float(c) for c in closes], "volume": vols,
    })


def test_load_sector_map_decodes_gbk(tmp_path):
    path = tmp_path / "code2sector.json"
    with open(path, "w", encoding="gbk") as f:
        json.dump({"600000": ["半导体", "白酒"]}, f, ensure_ascii=False)
    assert bt.load_sector_map(str(path)) == {"600000": ["半导体", "白酒"]}


def test_load_sector_map_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.load_sector_map(str(tmp_path / "nope.json"))


def test_load_daily_roundtrip(tmp_path):
    d = make_daily([10.0, 10.5, 11.0])
    d.to_pickle(tmp_path / "600000.pkl")
    loaded = bt.load_daily(str(tmp_path), "600000")
    assert list(loaded.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(loaded) == 3


def test_load_daily_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        bt.load_daily(str(tmp_path), "999999")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(4 个测试全因 `ModuleNotFoundError: No module named 'backtest'`)

- [ ] **Step 3: 写最小实现**

创建 `backtest.py`:

```python
# -*- coding: utf-8 -*-
"""历史盲测基线模块(生产级固化 nxday_backtest.py 核心次日盲测路径)。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
严格禁止未来数据泄露:评分输入 <= T,验证只读 T+1。
"""
import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import analysis as an

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
PER_SECTOR = 5
EVAL_DAYS = 1200
B_SAMPLE_EVERY = 10
MIN_AMOUNT = 1e8
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)

RNG = np.random.default_rng(0)   # 模块级单实例,评估日按循环顺序复用(spec §5.6 C)


def load_daily(data_dir, code):
    path = os.path.join(data_dir, f"{code}.pkl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing daily pkl: {path}")
    return pd.read_pickle(path)


def load_sector_map(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"missing sector map: {path}")
    with open(path, "r", encoding="gbk") as f:
        return json.load(f)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(4 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): 骨架+常量+GBK 数据加载(load_daily/load_sector_map)"
```

---

### Task 2: 全市场、日历、板块成员

**Files:**
- Modify: `backtest.py`(追加 `build_universe`/`build_calendar`/`build_sector_members`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Consumes: `load_daily`/`load_sector_map`(Task 1)。
- Produces: `build_universe(data_dir, sector_map) -> (universe: {code: df}, codes: [code])`(过滤 `code in sector_map` 且 `len>=1200`,每只 `tail(1200)`+`reset_index(drop=True)`+加 `change_pct`);`build_calendar(universe, codes) -> (all_days, pos_of)`;`build_sector_members(sector_map, universe) -> {sector: [codes]}`(code2sector 文件顺序,`if c in universe`)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_backtest.py` 末尾追加:

```python
def _write_universe(tmp_path, sector_map, stocks):
    dailydir = tmp_path / "daily"
    dailydir.mkdir()
    for code, df in stocks.items():
        df.to_pickle(dailydir / f"{code}.pkl")
    sm = tmp_path / "code2sector.json"
    with open(sm, "w", encoding="gbk") as f:
        json.dump(sector_map, f, ensure_ascii=False)
    return str(dailydir), str(sm)


def test_build_universe_filters_and_truncates(tmp_path):
    sector_map = {"600000": ["半导体"], "600001": ["半导体"], "999999": ["半导体"]}
    long_df = make_daily([10.0 + i * 0.01 for i in range(1300)])
    short_df = make_daily([10.0, 10.1])  # len < 1200 -> 剔除
    stocks = {"600000": long_df, "600001": short_df}  # 999999 无文件
    dailydir, sm = _write_universe(tmp_path, sector_map, stocks)
    universe, codes = bt.build_universe(dailydir, bt.load_sector_map(sm))
    assert codes == ["600000"]
    assert len(universe["600000"]) == 1200
    assert "change_pct" in universe["600000"].columns
    assert universe["600000"]["change_pct"].iloc[0] == 0.0


def test_build_calendar_sorted_union_and_pos():
    d1 = make_daily([10.0, 10.1], start="2026-01-01")          # 01-01, 01-02
    d2 = make_daily([20.0, 20.1, 20.2], start="2026-01-02")    # 01-02, 01-03, 01-04
    all_days, pos_of = bt.build_calendar({"a": d1, "b": d2}, ["a", "b"])
    assert all_days == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    assert pos_of["a"]["2026-01-01"] == 0
    assert pos_of["a"]["2026-01-02"] == 1
    assert pos_of["b"]["2026-01-02"] == 0
    assert pos_of["b"]["2026-01-04"] == 2


def test_build_sector_members_file_order_and_filter():
    sector_map = {"a": ["S2", "S1"], "b": ["S1"], "c": ["S1"]}
    universe = {"a": None, "c": None}  # b 不在 universe -> 剔除
    sm = bt.build_sector_members(sector_map, universe)
    assert sm["S2"] == ["a"]
    assert sm["S1"] == ["a", "c"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`AttributeError: module 'backtest' has no attribute 'build_universe'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 的 `load_sector_map` 之后追加:

```python
def build_universe(data_dir, sector_map):
    universe, codes = {}, []
    files = sorted(f for f in os.listdir(data_dir) if f.endswith(".pkl"))
    for fn in files:
        code = fn[:-4]
        if code not in sector_map:
            continue
        d = load_daily(data_dir, code)
        if len(d) < 1200:
            continue
        d = d.reset_index(drop=True).tail(1200).copy()
        d["code"] = code
        d["change_pct"] = d["close"].pct_change() * 100.0
        d.iloc[0, d.columns.get_loc("change_pct")] = 0.0  # CoW 安全(避免链式赋值)
        universe[code] = d
        codes.append(code)
    return universe, codes


def build_calendar(universe, codes):
    all_days = sorted(set().union(*[set(universe[c]["date"].values) for c in codes]))
    pos_of = {c: {dt: int(idx) for idx, dt in enumerate(universe[c]["date"].values)} for c in codes}
    return all_days, pos_of


def build_sector_members(sector_map, universe):
    sector_members = {}
    for c, secs in sector_map.items():
        if c in universe:
            for s in secs:
                sector_members.setdefault(s, []).append(c)
    return sector_members
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(7 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): build_universe/build_calendar/build_sector_members(1200 截尾+change_pct+文件序成员)"
```

---

### Task 3: 目标公式 next_returns

**Files:**
- Modify: `backtest.py`(追加 `next_returns`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Produces: `next_returns(d, i) -> {"gap","close1","od"} | None`。`i` 为**该股自身 bar 索引**(非日历索引);越界或任一 `close[T]/open[T+1]/close[T+1] <= 0` 返回 None。

- [ ] **Step 1: 写失败测试**

追加:

```python
def test_next_returns_exact():
    d = make_daily([100.0, 110.0], opens=[100.0, 105.0])
    r = bt.next_returns(d, 0)
    assert r["gap"] == pytest.approx(105.0 / 100.0 - 1)
    assert r["od"] == pytest.approx(110.0 / 105.0 - 1)
    assert r["close1"] == pytest.approx(110.0 / 100.0 - 1)


def test_next_returns_out_of_range():
    d = make_daily([100.0, 110.0])
    assert bt.next_returns(d, 1) is None


def test_next_returns_nonpositive():
    d = make_daily([0.0, 110.0], opens=[0.0, 105.0])
    assert bt.next_returns(d, 0) is None  # close[T] <= 0
    d2 = make_daily([100.0, 0.0], opens=[100.0, 0.0])
    assert bt.next_returns(d2, 0) is None  # close[T+1] <= 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute 'next_returns'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def next_returns(d, i):
    if i + 1 >= len(d):
        return None
    c0 = float(d["close"].iloc[i]); o1 = float(d["open"].iloc[i + 1]); c1 = float(d["close"].iloc[i + 1])
    if c0 <= 0 or o1 <= 0 or c1 <= 0:
        return None
    return {"gap": o1 / c0 - 1, "close1": c1 / c0 - 1, "od": c1 / o1 - 1}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(10 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): next_returns 目标公式(gap/od/close1 + 越界/非正价守卫)"
```

---

### Task 4: 板块热度代理(_win_gain + sector_heat)

**Files:**
- Modify: `backtest.py`(追加 `_win_gain`/`sector_heat`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Consumes: `build_calendar`(Task 2)。
- Produces: `_win_gain(universe, pos_of, all_days, code, i, w) -> float | None`(`i` 为**日历索引**);`sector_heat(universe, sector_members, pos_of, all_days, i) -> {sector: median}`(有效成员 <3 的板块不入 heat)。

- [ ] **Step 1: 写失败测试**

追加:

```python
def test_win_gain():
    d = make_daily([10.0, 10.5, 11.0, 11.5, 12.0, 13.0], start="2026-01-01")
    all_days, pos_of = bt.build_calendar({"a": d}, ["a"])
    # i=5(w=5): end=all_days[5], start=all_days[1] -> close[5]/close[1]-1
    g = bt._win_gain({"a": d}, pos_of, all_days, "a", 5, 5)
    assert g == pytest.approx(13.0 / 10.5 - 1)


def test_win_gain_missing_day():
    d1 = make_daily([10.0, 11.0], start="2026-01-01")             # 01-01, 01-02
    d2 = make_daily([20.0, 21.0, 22.0, 23.0], start="2026-01-01")  # 01-01..01-04
    universe = {"a": d1, "b": d2}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b"])
    # all_days[2]=01-03,a 无此 bar -> None
    assert bt._win_gain(universe, pos_of, all_days, "a", 2, 5) is None


def test_sector_heat_median_and_min3():
    d = make_daily([10.0 + i * 0.5 for i in range(10)], start="2026-01-01")
    universe = {c: d for c in ["a", "b", "c"]}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b", "c"])
    heat = bt.sector_heat(universe, {"S": ["a", "b", "c"]}, pos_of, all_days, 9)
    assert set(heat) == {"S"}
    expected = d["close"].iloc[9] / d["close"].iloc[5] - 1  # 三只同序列 -> median = 该涨幅
    assert heat["S"] == pytest.approx(expected)


def test_sector_heat_under3_dropped():
    d = make_daily([10.0 + i * 0.5 for i in range(10)], start="2026-01-01")
    universe = {c: d for c in ["a", "b"]}
    all_days, pos_of = bt.build_calendar(universe, ["a", "b"])
    heat = bt.sector_heat(universe, {"S": ["a", "b"]}, pos_of, all_days, 9)
    assert heat == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute '_win_gain'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def _win_gain(universe, pos_of, all_days, code, i, w):
    j_hi = pos_of[code].get(all_days[i])
    if j_hi is None:
        return None
    j_lo = pos_of[code].get(all_days[max(0, i - (w - 1))])
    if j_lo is None or j_hi <= j_lo:
        return None
    c0 = float(universe[code]["close"].iloc[j_lo])
    c1 = float(universe[code]["close"].iloc[j_hi])
    if c0 <= 0:
        return None
    return c1 / c0 - 1


def sector_heat(universe, sector_members, pos_of, all_days, i):
    heat = {}
    for s, members in sector_members.items():
        gains = {}
        for c in members:
            g = _win_gain(universe, pos_of, all_days, c, i, 5)
            if g is not None:
                gains[c] = g
        if len(gains) >= 3:
            heat[s] = float(np.median(list(gains.values())))
    return heat
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(14 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): sector_heat 板块中位数 5 日涨幅代理(_win_gain + min3)"
```

---

### Task 5: 评分调用 score_at + 反泄露测试

**Files:**
- Modify: `backtest.py`(追加 `score_at`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Consumes: `analysis.score_stock(daily_df, quote, now)`(外部)。
- Produces: `score_at(d, i, now) -> dict | None`。`i` 为 bar 索引;`len(df)<61` 或 `sc["composite"] is None` 返回 None;重建 quote `{price, change_pct, volume, amount=volume*close}`。

- [ ] **Step 1: 写失败测试(含 spec §11.3 反泄露核心测试)**

追加:

```python
def test_score_at_reconstructs_quote(monkeypatch):
    closes = [10.0 + i * 0.1 for i in range(65)]
    d = make_daily(closes)
    d["change_pct"] = d["close"].pct_change() * 100.0
    d["change_pct"].iloc[0] = 0.0
    calls = []
    def fake(df, quote, now):
        calls.append((len(df), dict(quote)))
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake)
    i = 63
    s = bt.score_at(d, i, bt.AFTER_CLOSE)
    assert s["composite"] == 40
    assert calls[0][0] == 64                       # df 只含 0..63(<=T)
    assert calls[0][1]["price"] == float(closes[63])
    assert calls[0][1]["amount"] == float(d["volume"].iloc[63]) * float(closes[63])


def test_score_at_short_history_returns_none(monkeypatch):
    d = make_daily([10.0 + i * 0.1 for i in range(60)])  # 60 根 < 61
    d["change_pct"] = 0.0
    assert bt.score_at(d, 59, bt.AFTER_CLOSE) is None


def test_score_at_no_future_leak(monkeypatch):
    closes = [10.0 + i * 0.1 for i in range(65)]
    d = make_daily(closes)
    d["change_pct"] = d["close"].pct_change() * 100.0
    d["change_pct"].iloc[0] = 0.0
    seen_prices = []
    def fake(df, quote, now):
        seen_prices.append(quote["price"])
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake)
    i = 63
    s1 = bt.score_at(d, i, bt.AFTER_CLOSE)
    # 把 T+1 的 open/close 改成极端正值(保持 >0,使 next_returns 仍有效)
    d.loc[i + 1, "open"] = 1e9
    d.loc[i + 1, "close"] = 1e9
    s2 = bt.score_at(d, i, bt.AFTER_CLOSE)
    assert s2 == s1                                  # 评分逐位不变
    assert seen_prices == [float(closes[63])] * 2     # 两次都只读到 T 的 close
    nr = bt.next_returns(d, i)
    assert nr is not None and nr["gap"] > 1.0         # 验证 next_returns 确实读了 T+1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute 'score_at'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def score_at(d, i, now):
    df = d.iloc[: i + 1]
    if len(df) < 61:
        return None
    quote = {"price": float(df["close"].iloc[-1]),
             "change_pct": float(df["change_pct"].iloc[-1]),
             "volume": float(df["volume"].iloc[-1]),
             "amount": float(df["volume"].iloc[-1]) * float(df["close"].iloc[-1])}
    sc = an.score_stock(df, quote, now)
    return sc if sc["composite"] is not None else None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(17 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): score_at 评分调用 + spec §11.3 反泄露测试(评分逐位不变)"
```

---

### Task 6: 可买集合 build_buyable + 篮子 select_baskets

**Files:**
- Modify: `backtest.py`(追加 `build_buyable`/`select_baskets`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Consumes: `next_returns`(Task 3)、`analysis.limit_threshold`(外部)。
- Produces:
  - `build_buyable(universe, pos_of, all_days, i) -> (buy: set, od_m, gap_m, c1_m)`。逐字过滤:bar 存在且 `bar+1<len`、`next_returns` 非 None、`chg < limit_threshold(code)` 且 `chg > -7.0`、`volume[bar]*close[bar] >= MIN_AMOUNT`。
  - `select_baskets(sector_members, pos_of, all_days, i, buyable, hot, get_score, start, rng) -> {"A","E_hi","E_lo","B","C","D"}`。`get_score(code, bar)` 为可调用对象;B 未采样日返回 `B=None`。

- [ ] **Step 1: 写失败测试**

追加:

```python
def _with_change_pct(df, vals):
    df = df.copy()
    df["change_pct"] = [float(v) for v in vals]
    return df


def test_build_buyable_filters():
    d1 = _with_change_pct(make_daily([10.0, 11.0, 12.0]), [0.0, 10.0, 0.0])  # 600000 bar1 涨停(>=9.9)
    d2 = _with_change_pct(make_daily([10.0, 11.0, 12.0], volumes=[1000] * 3), [0.0, 1.0, 0.0])  # 量能不足
    d3 = _with_change_pct(make_daily([1000.0, 1001.0, 1002.0], volumes=[200000] * 3), [0.0, 1.0, 0.0])  # 合格
    universe = {"600000": d1, "000001": d2, "600519": d3}
    codes = ["600000", "000001", "600519"]
    all_days, pos_of = bt.build_calendar(universe, codes)
    buy, od_m, gap_m, c1_m = bt.build_buyable(universe, pos_of, all_days, 1)
    assert buy == {"600519"}
    assert set(od_m) == set(gap_m) == set(c1_m) == {"600519"}


def test_select_baskets_exact():
    sector_members = {"S1": ["a", "b", "c", "d", "e", "f", "g"], "S2": ["h", "i"], "S3": ["j"]}
    buyable = {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j"}
    hot = ["S1", "S2", "S3"]
    scores = {
        "a": {"composite": 80, "risk": 10, "position": 30},
        "b": {"composite": 70, "risk": 10, "position": 80},
        "c": {"composite": 65, "risk": 10, "position": 50},
        "d": {"composite": 55, "risk": 10, "position": 20},
        "e": {"composite": 50, "risk": 80, "position": 90},  # risk>=70 -> 排除
        "f": {"composite": 40, "risk": 10, "position": 10},
        "g": {"composite": 30, "risk": 10, "position": 70},
        "h": {"composite": 75, "risk": 10, "position": 40},
        "i": {"composite": 60, "risk": 10, "position": 60},
        "j": {"composite": 90, "risk": 10, "position": 25},
    }
    def get_score(code, bar):
        return scores[code]
    all_days = ["2026-01-01"]
    dt = all_days[0]
    pos_of = {c: {dt: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    assert b["A"] == ["a", "b", "c", "d", "f", "h", "i", "j"]
    assert b["E_hi"] == ["b", "g", "c", "a", "d", "i", "h", "j"]
    assert b["E_lo"] == ["g", "c", "a", "d", "f", "i", "h", "j"]
    assert b["B"] == ["j", "a", "h", "b", "c", "i", "d", "f", "g"]  # (0-0)%10==0 采样
    assert b["C"] == ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]  # 10<=15 不抽样
    assert set(b["D"]) == buyable


def test_select_baskets_B_not_sampled():
    sector_members = {"S1": ["a", "b", "c"]}
    buyable = {"a", "b", "c"}
    hot = ["S1"]
    def get_score(code, bar):
        return {"composite": 60, "risk": 10, "position": 50}
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 3, np.random.default_rng(0))
    assert b["B"] is None  # (0-3)%10 != 0 -> 未采样


def test_select_baskets_E_position_tie_breaks_by_fa():
    sector_members = {"S1": ["x", "y"]}
    buyable = {"x", "y"}
    hot = ["S1"]
    scores = {
        "x": {"composite": 70, "risk": 10, "position": 50},
        "y": {"composite": 80, "risk": 10, "position": 50},  # 同 position,fa 更高
    }
    def get_score(code, bar):
        return scores[code]
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in buyable}
    b = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    # 先 -fa 排序(y 前),稳定 -position 重排,并列按 fa 序 -> y 仍在前
    assert b["E_hi"] == ["y", "x"]


def test_select_baskets_C_rng_sampling_deterministic():
    members = [f"c{i:02d}" for i in range(20)]
    sector_members = {"S1": members}
    buyable = set(members)
    hot = ["S1"]
    def get_score(code, bar):
        return {"composite": 50, "risk": 10, "position": 50}
    all_days = ["2026-01-01"]
    pos_of = {c: {all_days[0]: 0} for c in members}
    b1 = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    b2 = bt.select_baskets(sector_members, pos_of, all_days, 0, buyable, hot, get_score, 0, np.random.default_rng(0))
    assert len(b1["C"]) == 15
    assert b1["C"] == b2["C"]
    assert set(b1["C"]) <= buyable
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute 'build_buyable'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def build_buyable(universe, pos_of, all_days, i):
    dt = all_days[i]
    buy = set()
    od_m, gap_m, c1_m = {}, {}, {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None or bar + 1 >= len(universe[c]):
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        nr = next_returns(d, bar)
        if nr is None:
            continue
        th = an.limit_threshold(c)
        if chg >= th or chg <= -7.0:
            continue
        if float(d["volume"].iloc[bar]) * close < MIN_AMOUNT:
            continue
        buy.add(c)
        od_m[c] = nr["od"]; gap_m[c] = nr["gap"]; c1_m[c] = nr["close1"]
    return buy, od_m, gap_m, c1_m


def select_baskets(sector_members, pos_of, all_days, i, buyable, hot, get_score, start, rng):
    dt = all_days[i]
    hot_members = set()
    for s in hot:
        hot_members.update(sector_members.get(s, []))
    basket_a, pos_hi, pos_lo = [], [], []
    for s in hot:
        scored = []
        for c in sector_members.get(s, []):
            if c not in buyable:
                continue
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            bonus = 8 if sc["composite"] >= 68 else 4 if sc["composite"] >= 60 else 0
            fa = sc["composite"] + bonus
            scored.append((fa, c, sc["position"]))
        scored.sort(key=lambda x: -x[0])          # 先 -fa(spec §5.6 E 两段式)
        basket_a += [x[1] for x in scored[:PER_SECTOR]]
        scored.sort(key=lambda x: -x[2])          # 稳定重排 -position,并列按 fa 序
        pos_hi += [x[1] for x in scored[:PER_SECTOR]]
        pos_lo += [x[1] for x in scored[-PER_SECTOR:]]
    basket_b = None
    if (i - start) % B_SAMPLE_EVERY == 0:
        scored_b = []
        for c in sorted(buyable):
            sc = get_score(c, pos_of[c][dt])
            if sc is None or sc["risk"] >= 70:
                continue
            scored_b.append((sc["composite"], c))
        scored_b.sort(key=lambda x: -x[0])
        basket_b = [c for _, c in scored_b[:TOP_SECTORS * PER_SECTOR]]
    basket_c = sorted(hot_members & buyable)
    if len(basket_c) > TOP_SECTORS * PER_SECTOR:
        basket_c = list(rng.choice(basket_c, size=TOP_SECTORS * PER_SECTOR, replace=False))
    basket_d = list(buyable)
    return {"A": basket_a, "E_hi": pos_hi, "E_lo": pos_lo, "B": basket_b, "C": basket_c, "D": basket_d}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(22 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): build_buyable + select_baskets(A/B/C/D/E,两段式 E 排序)"
```

---

### Task 7: 度量(stats / summ / summ_year / welch_t)

**Files:**
- Modify: `backtest.py`(追加 `stats`/`summ`/`summ_year`/`_betacf`/`_betainc`/`_t_cdf`/`welch_t`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Produces: `stats(basket, m) -> float`(空篮子 NaN);`summ(arr) -> (mean_pct, win_rate, n)`(空 → `(nan, nan, 0)`);`summ_year(arr, years) -> [(year, mean_pct, win_rate, n)]`;`welch_t(a, b) -> {"t","p"}`(n<2 或任一无效 → `{"t": None, "p": None}`)。

- [ ] **Step 1: 写失败测试**

追加:

```python
def test_stats_mean_and_empty():
    m = {"a": 0.1, "b": 0.2, "c": 0.3}
    assert bt.stats(["a", "b", "c"], m) == pytest.approx(0.2)
    assert bt.stats(["a", "zz"], m) == pytest.approx(0.1)
    assert np.isnan(bt.stats([], m))


def test_summ():
    arr = [0.01, 0.02, -0.01, 0.03, 0.0]
    m, w, n = bt.summ(arr)
    assert m == pytest.approx((0.01 + 0.02 - 0.01 + 0.03 + 0.0) / 5 * 100)
    assert w == pytest.approx(60.0)  # 0.01/0.02/0.03 正 -> 3/5
    assert n == 5


def test_summ_nan_and_none_filtered():
    m, w, n = bt.summ([0.01, float("nan"), None, -0.02])
    assert n == 2
    assert m == pytest.approx((0.01 - 0.02) / 2 * 100)


def test_summ_empty():
    m, w, n = bt.summ([])
    assert np.isnan(m) and np.isnan(w) and n == 0


def test_summ_year_groups():
    out = bt.summ_year([0.01, 0.02, 0.03], ["2023", "2023", "2024"])
    assert out[0][0] == "2023" and out[0][3] == 2
    assert out[1][0] == "2024" and out[1][3] == 1


def test_welch_t_formula():
    a = [1.0 + 0.1 * i for i in range(10)]
    b = [5.0 + 0.1 * i for i in range(10)]
    r = bt.welch_t(a, b)
    m1, m2 = np.mean(a), np.mean(b)
    v1, v2 = np.var(a, ddof=1), np.var(b, ddof=1)
    n1 = n2 = 10
    expected_t = (m1 - m2) / ((v1 / n1 + v2 / n2) ** 0.5)
    assert r["t"] == pytest.approx(expected_t)
    assert 0.0 <= r["p"] <= 1.0
    assert r["p"] < 0.001  # 差异极大 -> 极显著


def test_welch_t_identical():
    a = [1.0, 1.1, 1.2, 1.3, 1.4]
    r = bt.welch_t(a, a)
    assert r["t"] == 0.0 and r["p"] == 1.0


def test_welch_t_too_small():
    r = bt.welch_t([1.0], [2.0])
    assert r["t"] is None and r["p"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute 'stats'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def stats(basket, m):
    return float(np.mean([m[c] for c in basket if c in m])) if basket else np.nan


def summ(arr):
    a = np.array([x for x in arr if x == x and x is not None])
    if len(a):
        return (round(float(np.nanmean(a) * 100), 3),
                round(float(np.mean(a > 0) * 100), 1),
                int(len(a)))
    return (np.nan, np.nan, 0)


def summ_year(arr, years):
    groups = {}
    for x, y in zip(arr, years):
        if x is None or x != x:
            continue
        groups.setdefault(y, []).append(x)
    out = []
    for y in sorted(groups):
        a = np.array(groups[y])
        if len(a):
            out.append((y, round(float(np.nanmean(a) * 100), 3),
                        round(float(np.mean(a > 0) * 100), 1), int(len(a))))
    return out


def _betacf(a, b, x):
    """不完全 beta 的连分式展开(Numerical Recipes 6.4)。"""
    MAXIT = 200
    EPS = 3.0e-14
    FPMIN = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betainc(a, b, x):
    """正则化不完全 beta I_x(a,b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(ln_beta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t, df):
    """Student's t CDF(t >= 0)。"""
    x = df / (df + t * t)
    return 1.0 - 0.5 * _betainc(df / 2.0, 0.5, x)


def welch_t(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return {"t": None, "p": None}
    m1, m2 = a.mean(), b.mean()
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se = (v1 / n1 + v2 / n2) ** 0.5
    if se == 0.0:
        return {"t": 0.0, "p": 1.0}
    t = (m1 - m2) / se
    df = (v1 / n1 + v2 / n2) ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2.0 * (1.0 - _t_cdf(abs(t), df))
    return {"t": float(t), "p": float(p)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(30 passed)

- [ ] **Step 5: 提交**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): 度量 stats/summ/summ_year + 手写 Welch t 检验(无 scipy)"
```

---

### Task 8: 主循环 run + 报告 build_report/render_markdown + CLI main

**Files:**
- Modify: `backtest.py`(追加 `run`/`_git_short_sha`/`_json_num`/`build_report`/`render_markdown`/`main` + `if __name__ == "__main__"`)
- Modify: `tests/test_backtest.py`(追加测试)

**Interfaces:**
- Consumes: 前面所有函数。
- Produces: `run(data_dir, sector_map_path) -> dict`(键 `rows/by_year/welch/n_eval/step/window/data_range`,rows 值为 `(mean_pct,win_rate,n)` 元组、by_year 为 `[(year,..)]`);`build_report(results, system_version=None, generated_at=None) -> dict`(JSON-ready,NaN→None);`render_markdown(payload) -> str`;`main(argv=None) -> int`(0 成功 / 1 运行错;argparse 参数错默认 2)。

- [ ] **Step 1: 写失败测试**

追加:

```python
def _make_universe_fixture(tmp_path, n_bars=1200):
    codes = ["600001", "600002", "600003", "000001", "000002", "000003"]
    sector_map = {
        "600001": ["半导体"], "600002": ["半导体"], "600003": ["半导体"],
        "000001": ["白酒"], "000002": ["白酒"], "000003": ["白酒"],
    }
    dailydir = tmp_path / "daily"
    dailydir.mkdir()
    for i, code in enumerate(codes):
        closes = [2000.0 + (i + 1) * 0.1 * k for k in range(n_bars)]
        make_daily(closes, start="2020-01-01").to_pickle(dailydir / f"{code}.pkl")
    sm = tmp_path / "code2sector.json"
    with open(sm, "w", encoding="gbk") as f:
        json.dump(sector_map, f, ensure_ascii=False)
    return str(dailydir), str(sm)


def test_run_end_to_end(tmp_path, monkeypatch):
    dailydir, sm = _make_universe_fixture(tmp_path)
    def fake_score_stock(df, quote, now):
        return {"position": 50.0, "trend": 50.0, "volume_price": 50.0,
                "signal": 50.0, "risk": 10.0, "composite": 70.0}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    results = bt.run(dailydir, sm)
    assert set(results["rows"]) == {
        "A 实际管线(热板块xtop5)  次日od", "A 隔夜gap", "A close->next close",
        "E 板块内低位股(pos分top5)次日od", "E 板块内高位股(pos分bot5)次日od",
        "B 全市场top15  次日od", "C 热板块随机  次日od", "D 全市场基准  次日od",
    }
    assert set(results["welch"]) == {
        "A 实际管线(热板块xtop5)  次日od", "B 全市场top15  次日od",
        "C 热板块随机  次日od", "E 板块内低位股(pos分top5)次日od", "E 板块内高位股(pos分bot5)次日od",
    }
    assert results["n_eval"] > 0
    assert results["step"] >= 1
    assert not np.isnan(results["rows"]["A 实际管线(热板块xtop5)  次日od"][0])
    assert results["data_range"]["start"] == "2020-01-01"


def test_build_report_fields():
    results = {
        "rows": {"A x": (1.5, 60.0, 10), "D y": (float("nan"), float("nan"), 0)},
        "by_year": {"A": [("2024", 1.5, 60.0, 10)]},
        "welch": {"A x": {"t": 2.0, "p": 0.05}},
        "n_eval": 10, "step": 3,
        "window": {"start": "2021-01-01", "end": "2026-01-01"},
        "data_range": {"start": "2019-01-01", "end": "2026-01-01"},
    }
    payload = bt.build_report(results, system_version="abc1234", generated_at="2026-08-14T00:00:00")
    assert payload["system_version"] == "abc1234"
    assert payload["module_version"] == "1.0.0"
    assert payload["rows"]["A x"] == {"mean_pct": 1.5, "win_rate": 60.0, "n": 10}
    assert payload["rows"]["D y"] == {"mean_pct": None, "win_rate": None, "n": 0}
    assert payload["welch"]["A x"]["t"] == 2.0
    assert "sector_heat_note" in payload


def test_render_markdown_has_sections():
    results = {
        "rows": {"A x": (1.5, 60.0, 10)},
        "by_year": {"A": [("2024", 1.5, 60.0, 10)]},
        "welch": {"A x": {"t": 2.0, "p": 0.05}},
        "n_eval": 10, "step": 3,
        "window": {"start": "2021-01-01", "end": "2026-01-01"},
        "data_range": {"start": "2019-01-01", "end": "2026-01-01"},
    }
    payload = bt.build_report(results, system_version="abc1234", generated_at="2026-08-14T00:00:00")
    md = bt.render_markdown(payload)
    assert "历史盲测基线报告" in md
    assert "代理声明" in md
    assert "| A x |" in md


def test_main_writes_outputs(tmp_path, monkeypatch):
    dailydir, sm = _make_universe_fixture(tmp_path)
    def fake_score_stock(df, quote, now):
        return {"position": 50.0, "trend": 50.0, "volume_price": 50.0,
                "signal": 50.0, "risk": 10.0, "composite": 70.0}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    out = tmp_path / "baseline.json"
    rc = bt.main(["--data-dir", dailydir, "--sector-map", sm, "--out", str(out)])
    assert rc == 0
    assert out.exists()
    payload = json.load(open(out, encoding="utf-8"))
    assert payload["system_version"]
    assert (tmp_path / "baseline.md").exists()


def test_main_missing_data_dir_exits_nonzero(tmp_path):
    rc = bt.main(["--data-dir", str(tmp_path / "nope")])
    assert rc == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: FAIL(`no attribute 'run'`)

- [ ] **Step 3: 写最小实现**

在 `backtest.py` 追加:

```python
def run(data_dir, sector_map_path):
    sector_map = load_sector_map(sector_map_path)
    universe, codes = build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl (code in sector map and >=1200 bars) in {data_dir}")
    sector_members = build_sector_members(sector_map, universe)
    all_days, pos_of = build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)

    score_cache = {}
    def get_score(code, bar):
        key = (code, bar)
        if key not in score_cache:
            score_cache[key] = score_at(universe[code], bar, AFTER_CLOSE)
        return score_cache[key]

    A_od, A_gap, A_c1 = [], [], []
    E_hi_od, E_lo_od = [], []
    B_od, C_od, D_od = [], [], []
    eval_years = []
    n_eval = 0

    for i in range(start, len(all_days) - 1, step):
        heat = sector_heat(universe, sector_members, pos_of, all_days, i)
        if not heat:
            continue
        hot = sorted(heat, key=heat.get, reverse=True)[:TOP_SECTORS]
        buyable, od_m, gap_m, c1_m = build_buyable(universe, pos_of, all_days, i)
        baskets = select_baskets(sector_members, pos_of, all_days, i,
                                 buyable, hot, get_score, start, RNG)
        A_od.append(stats(baskets["A"], od_m))
        A_gap.append(stats(baskets["A"], gap_m))
        A_c1.append(stats(baskets["A"], c1_m))
        E_hi_od.append(stats(baskets["E_hi"], od_m))
        E_lo_od.append(stats(baskets["E_lo"], od_m))
        if baskets["B"] is not None:
            B_od.append(stats(baskets["B"], od_m))
        C_od.append(stats(baskets["C"], od_m))
        D_od.append(stats(baskets["D"], od_m))
        eval_years.append(all_days[i][:4])
        n_eval += 1

    rows = {
        "A 实际管线(热板块xtop5)  次日od": summ(A_od),
        "A 隔夜gap": summ(A_gap),
        "A close->next close": summ(A_c1),
        "E 板块内低位股(pos分top5)次日od": summ(E_hi_od),
        "E 板块内高位股(pos分bot5)次日od": summ(E_lo_od),
        "B 全市场top15  次日od": summ(B_od),
        "C 热板块随机  次日od": summ(C_od),
        "D 全市场基准  次日od": summ(D_od),
    }
    by_year = {
        "A 实际管线": summ_year(A_od, eval_years),
        "E 板块内低位股(pos top5)": summ_year(E_hi_od, eval_years),
        "E 板块内高位股(pos bot5)": summ_year(E_lo_od, eval_years),
        "C 热板块随机": summ_year(C_od, eval_years),
        "D 全市场基准": summ_year(D_od, eval_years),
    }
    welch = {
        "A 实际管线(热板块xtop5)  次日od": welch_t(A_od, D_od),
        "B 全市场top15  次日od": welch_t(B_od, D_od),
        "C 热板块随机  次日od": welch_t(C_od, D_od),
        "E 板块内低位股(pos分top5)次日od": welch_t(E_hi_od, D_od),
        "E 板块内高位股(pos分bot5)次日od": welch_t(E_lo_od, D_od),
    }
    data_range = {"start": str(all_days[0]), "end": str(all_days[-1])}
    window = {"start": str(all_days[start]), "end": str(all_days[len(all_days) - 2])}
    return {"rows": rows, "by_year": by_year, "welch": welch,
            "n_eval": n_eval, "step": step, "window": window, "data_range": data_range}


def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def _json_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def build_report(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    rows = {k: {"mean_pct": _json_num(v[0]), "win_rate": _json_num(v[1]), "n": v[2]}
            for k, v in results["rows"].items()}
    by_year = {k: [{"year": y, "mean_pct": _json_num(m), "win_rate": _json_num(w), "n": n}
                   for y, m, w, n in v] for k, v in results["by_year"].items()}
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "data_range": results["data_range"],
        "n_eval": results["n_eval"],
        "step": results["step"],
        "window": results["window"],
        "sector_heat_note": "median-5-day-gain proxy (daily pkl 无 amount,生产板块 composite 不可复现)",
        "rows": rows,
        "by_year": by_year,
        "welch": results["welch"],
    }


def render_markdown(payload):
    def fmt(x, nd=3):
        return "-" if x is None else f"{x:.{nd}f}"

    lines = ["# 历史盲测基线报告", ""]
    lines.append(f"- system_version: `{payload['system_version']}`")
    lines.append(f"- module_version: {payload['module_version']}")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    lines.append(f"- 评估窗口: {payload['window']['start']} -> {payload['window']['end']} (n_eval={payload['n_eval']}, step={payload['step']})")
    lines += ["", "## 整体(次日)", "", "| 篮子 | mean_pct% | win_rate% | n |", "|---|---|---|---|"]
    for k, v in payload["rows"].items():
        lines.append(f"| {k} | {fmt(v['mean_pct'])} | {fmt(v['win_rate'], 1)} | {v['n']} |")
    lines += ["", "## Welch(对 D 全市场基准,非配对且样本重叠,仅定性参考)", "", "| 篮子 | t | p |", "|---|---|---|"]
    for k, v in payload["welch"].items():
        lines.append(f"| {k} | {fmt(v['t'], 4)} | {fmt(v['p'], 4)} |")
    lines += ["", "## 按年", "", "| 篮子 | 年份 | mean_pct% | win_rate% | n |", "|---|---|---|---|---|"]
    for k, rows in payload["by_year"].items():
        for r in rows:
            lines.append(f"| {k} | {r['year']} | {fmt(r['mean_pct'])} | {fmt(r['win_rate'], 1)} | {r['n']} |")
    lines += ["", "## 篮子 A 代理声明", ""]
    lines.append(payload["sector_heat_note"])
    lines.append("")
    lines.append("因日线 pkl 无 amount(成交额),生产 collect_sector_metrics 所需 turnover_ratio(emotion 25%)与 activity(strength 30%)无法历史复现。篮子 A 与生产 recommend.py 管线存在以下代理差异:")
    lines += ["", "| 维度 | 生产 recommend.py | 基线篮 A(代理) |", "|---|---|---|",
              "| 板块选择 | select_sectors verdict∈{建议关注, 跟踪(热点延续)} + composite 降序 + top3 | hot = 板块中位数 5 日涨幅 top3 |",
              "| 个股加成 | sector_bonus(68/60/50,+8/+4/-5) | composite 分档 8/4/0(无 -5) |",
              "| 热权重重算 | _apply_hot_weights(composite>=68 改用 HOT_SIGNAL_WEIGHTS) | 无(恒 V3_WEIGHTS) |",
              "| 个股硬过滤 | filter_candidates ST/新股/停牌/涨停/<=-7%/amount<1e8 | buyable 涨停/<=-7%/volume*close>=1e8 |",
              "| 加成×风险折扣位置 | (quality+bonus)*(1-risk/100) 加成在折扣内 | quality*(1-risk/100)+bonus 加成在折扣外 |",
              "| verdict 过滤 | rank_candidates 剔 verdict==回避(<42) | 仅 risk<70 |",
              ""]
    lines.append("结论:基线篮 A 的准确率是「代理管线」的准确率,作为当前版本可复现的近似基线;真正的生产口径基线须待 amount 数据补齐(后续切片)。")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="历史盲测基线(次日方向)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="backtest_baseline.json")
    args = parser.parse_args(argv)

    try:
        results = run(args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    payload = build_report(results)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {args.out} and {md_path} (n_eval={payload['n_eval']}, step={payload['step']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(35 passed)

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest tests/ -q`
Expected: PASS(129 + 35 = 164 passed)

```bash
git add backtest.py tests/test_backtest.py
git commit -m "feat(backtest): run 主循环 + build_report/render_markdown + argparse CLI"
```

---

## 验收(实现后手动执行,spec §12)

1. `python -m pytest tests/ -q` 全绿(129 + 35)。
2. `python backtest.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out backtest_baseline.json` 在真实缓存产出;核对 `rows` 与 `_analysis/nxday_results.json` 同名 8 行(A od/gap/close1 + E 两行 + B/C/D)数值一致,`n_eval`/`step`/`window` 一致。**C 行例外**:模块 C 用 `sorted` 定序,与探针 set 顺序可能不同(属预期)。整文件 diff 时 `by_year` 少 4 个 P0b 键(属预期)。
3. 报告含 `system_version`(git 短哈希)、`data_range`、篮子 A 代理声明。
4. `test_score_at_no_future_leak` 通过(无未来数据泄露)。
