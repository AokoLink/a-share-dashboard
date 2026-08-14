# 市场环境分类(environment.py)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只读模块 `environment.py`,把每个交易日归入七态(牛/熊/震荡/恐慌/高潮/退潮/恢复),并接入 `evaluate.py` 按环境切片六维准确率。

**Architecture:** 单模块纯函数 + CLI(镜像 `compare.py`):`build_series` 从 `_analysis/daily/*.pkl` 离线自算合成市场序列(中位日收益复合指数 M + 涨跌家数比 + 涨跌停家数 + 量能比),`classify` 按优先级决策树打单标签,`run/build_report/render_markdown/main` 产出分布报告。`evaluate.py` 复用 `build_series` 把逐日 record 归入环境,复用既有 `_dim_metrics` 算各环境六维指标。

**Tech Stack:** Python 3 + pandas(3.0.5 CoW 常开)+ numpy;复用 `analysis`/`backtest`/`predict` 公开纯函数。

**Spec:** `docs/superpowers/specs/2026-08-15-environment-classifier-design.md`

## Global Constraints

- **只读、不 fetch**:只读 `_analysis/daily/*.pkl` 与 `_analysis/code2sector.json`;`code2sector.json` 用 `encoding="gbk"` 打开。
- **Python 源 ASCII `-`**,绝不使用 U+2212 `−`。
- **不 import 下划线私有函数**(不 import `bt._*`/`an._*`/`pr._*`)。
- **pandas 3.0 CoW 常开**:改值必须 `.loc[row, "col"] = v`,**禁链式赋值** `df["col"].iloc[i] = v`(惰性、无效)。
- **TDD**:每个任务先写失败测试 → 跑失败 → 最小实现 → 跑绿 → 提交。
- **直接提交 main**(no worktree/feature branch)。
- 每个任务提交后跑 `python -m pytest tests/ -q`(已知既有 2 条 `tests/test_api.py` 日期漂移失败,范围外,不计入本计划)。

---

## 文件结构

- **Create** `environment.py`:七态分类器(常量 + `_miss` + `classify` + `build_series` + `run` + `build_report` + `render_markdown` + `_git_short_sha` + `_num` + `main`)。
- **Create** `tests/test_environment.py`:classify 单测、build_series 单测 + 反泄露、run/report/main 端到端。
- **Modify** `evaluate.py`:顶部 `import environment as env`;`evaluate()` 增环境切片;`build_report()` 增 `environments`/`env_n`;`render_markdown()` 增「各环境准确率」表。
- **Modify** `tests/test_evaluate.py`:增 `test_evaluate_environments_slicing`。

---

### Task 1: `classify` 七态决策树 + 历史闸

**Files:**
- Create: `environment.py`(常量 + `_miss` + `classify`)
- Test: `tests/test_environment.py`(classify 单测)

**Interfaces:**
- Produces: `classify(row) -> str | None`(row 为 dict 或 pandas Series,含键 `r1/up_ratio/limit_up/limit_down/turnover_ratio/ma5/ma20/ma60/r5/r20/r60`);`LABELS = ("牛", "熊", "震荡", "恐慌", "高潮", "退潮", "恢复")`;模块常量(阈值)见下。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_environment.py`:

```python
# -*- coding: utf-8 -*-
"""environment.py 市场环境分类测试。"""
import environment as env


def _row(r5=None, r1=None, up=None, ld=0, lu=0, tr=None, r20=None, r60=0.0,
         ma5=1.0, ma20=1.0, ma60=1.0):
    """构造 classify 输入行;默认 r60=0.0/ma60=1.0 通过历史闸,ma 平坦无趋势。"""
    return {"r1": r1, "up_ratio": up, "limit_up": lu, "limit_down": ld,
            "turnover_ratio": tr, "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "r5": r5, "r20": r20, "r60": r60}


def test_insufficient_history():
    assert env.classify(_row(r60=None)) is None
    assert env.classify(_row(ma60=None)) is None


def test_panic_r5_boundary():
    assert env.classify(_row(r5=-0.08)) == "恐慌"
    assert env.classify(_row(r5=-0.079)) == "震荡"


def test_panic_breadth():
    assert env.classify(_row(r1=-0.04, up=0.15)) == "恐慌"


def test_panic_limit_down():
    assert env.classify(_row(ld=300)) == "恐慌"
    assert env.classify(_row(ld=299)) == "震荡"


def test_climax():
    assert env.classify(_row(r5=0.08, up=0.85)) == "高潮"
    assert env.classify(_row(r5=0.08, lu=100)) == "高潮"
    assert env.classify(_row(r5=0.08, tr=1.8)) == "高潮"
    assert env.classify(_row(r5=0.079, up=0.9)) == "震荡"


def test_bear():
    assert env.classify(_row(r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "熊"


def test_bull():
    assert env.classify(_row(r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "牛"


def test_recovery():
    assert env.classify(_row(r5=0.03, r20=-0.01, up=0.55)) == "恢复"


def test_receding():
    assert env.classify(_row(r20=0.01, r5=-0.03, up=0.45)) == "退潮"


def test_oscillation_fallback():
    assert env.classify(_row(r5=0.0, r20=0.0, up=0.5)) == "震荡"


def test_priority_panic_over_bear():
    assert env.classify(_row(r5=-0.08, r20=-0.08, ma5=0.9, ma20=1.0, ma60=1.1)) == "恐慌"


def test_priority_climax_over_bull():
    assert env.classify(_row(r5=0.08, up=0.85, r20=0.08, ma5=1.1, ma20=1.0, ma60=0.9)) == "高潮"
```

- [ ] **Step 2: 跑失败**

Run: `python -m pytest tests/test_environment.py -q`
Expected: FAIL(`ModuleNotFoundError: No module named 'environment'` 或 ImportError)。

- [ ] **Step 3: 最小实现**

创建 `environment.py`:

```python
# -*- coding: utf-8 -*-
"""市场环境分类:七态(牛/熊/震荡/恐慌/高潮/退潮/恢复)决策树。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
合成市场指数 = universe 中位日收益复合,非真实指数(口径差异见 spec §3)。
分类纯因果:第 i 日标签只依赖 <= i 的数据。
"""

MODULE_VERSION = "1.0.0"
MIN_HISTORY = 60
MIN_STATE_N = 20

PANIC_R5 = -0.08
PANIC_R1 = -0.04
PANIC_UP_RATIO = 0.15
PANIC_LIMIT_DOWN = 300
CLIMAX_R5 = 0.08
CLIMAX_UP_RATIO = 0.85
CLIMAX_LIMIT_UP = 100
CLIMAX_TURNOVER = 1.8
BEAR_R20 = -0.08
BULL_R20 = 0.08
RECOVERY_R5 = 0.03
RECOVERY_UP_RATIO = 0.55
RECEDE_R5 = -0.03
RECEDE_UP_RATIO = 0.45

LABELS = ("牛", "熊", "震荡", "恐慌", "高潮", "退潮", "恢复")


def _miss(v):
    """None 或 NaN 都视为缺失。"""
    return v is None or v != v


def classify(row):
    """单日七态决策;历史不足(r60/ma60 缺失)返回 None。row 为 dict 或 pandas Series。"""
    if _miss(row["r60"]) or _miss(row["ma60"]):
        return None
    r5 = row["r5"]; r1 = row["r1"]; up = row["up_ratio"]
    r20 = row["r20"]; ld = row["limit_down"]; lu = row["limit_up"]
    tr = row["turnover_ratio"]; ma5 = row["ma5"]; ma20 = row["ma20"]; ma60 = row["ma60"]
    if (not _miss(r5) and r5 <= PANIC_R5) \
            or (not _miss(r1) and not _miss(up) and r1 <= PANIC_R1 and up <= PANIC_UP_RATIO) \
            or ld >= PANIC_LIMIT_DOWN:
        return "恐慌"
    if not _miss(r5) and r5 >= CLIMAX_R5 and (
            (not _miss(up) and up >= CLIMAX_UP_RATIO) or lu >= CLIMAX_LIMIT_UP
            or (not _miss(tr) and tr >= CLIMAX_TURNOVER)):
        return "高潮"
    if not _miss(r20) and r20 <= BEAR_R20 and not _miss(ma5) and not _miss(ma20) \
            and ma5 < ma20 < ma60:
        return "熊"
    if not _miss(r20) and r20 >= BULL_R20 and not _miss(ma5) and not _miss(ma20) \
            and ma5 > ma20 > ma60:
        return "牛"
    if not _miss(r5) and not _miss(r20) and not _miss(up) \
            and r5 >= RECOVERY_R5 and r20 < 0 and up >= RECOVERY_UP_RATIO:
        return "恢复"
    if not _miss(r20) and not _miss(r5) and not _miss(up) \
            and r20 >= 0 and r5 <= RECEDE_R5 and up <= RECEDE_UP_RATIO:
        return "退潮"
    return "震荡"
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_environment.py -q`
Expected: `12 passed`

- [ ] **Step 5: 提交**

```bash
git add environment.py tests/test_environment.py
git commit -m "feat(environment): classify 七态决策树 + 历史闸"
```

---

### Task 2: `build_series` 合成市场序列 + 反泄露

**Files:**
- Modify: `environment.py`(顶部加 import + `build_series`)
- Test: `tests/test_environment.py`(追加 build_series 测试 + 夹具)

**Interfaces:**
- Consumes: `universe`(dict[code -> DataFrame],含 `change_pct` 百分数列 + `amount` 列)、`pos_of`(dict[code -> {date -> bar}])、`all_days`(date 列表)。
- Produces: `build_series(universe, pos_of, all_days) -> pd.DataFrame`,列顺序 `[date, r1, up_ratio, limit_up, limit_down, turnover, turnover_ratio, M, ma5, ma20, ma60, r5, r20, r60, environment]`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_environment.py`:

```python
import pandas as pd
import pytest


def _mk_df(closes, amounts=None, change_pct=None):
    """构造 universe 单股 DataFrame(含 change_pct/amount,与 bt.build_universe 同口径)。"""
    n = len(closes)
    if amounts is None:
        amounts = [1e8] * n
    if change_pct is None:
        change_pct = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)]
        change_pct = [v * 100.0 for v in change_pct]
    return pd.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(n)],
        "close": [float(c) for c in closes],
        "amount": [float(a) for a in amounts],
        "change_pct": [float(v) for v in change_pct],
    })


def _universe(n=70):
    """单股单调涨(close=100..100+n-1)、amount 恒定。"""
    closes = [100.0 + i for i in range(n)]
    universe = {"000001": _mk_df(closes)}
    all_days = list(universe["000001"]["date"])
    pos_of = {"000001": {dt: i for i, dt in enumerate(all_days)}}
    return universe, all_days, pos_of


def test_build_series_columns():
    universe, all_days, pos_of = _universe(70)
    s = env.build_series(universe, pos_of, all_days)
    assert list(s.columns) == ["date", "r1", "up_ratio", "limit_up", "limit_down",
                               "turnover", "turnover_ratio", "M", "ma5", "ma20", "ma60",
                               "r5", "r20", "r60", "environment"]
    assert len(s) == 70


def test_build_series_r1_median():
    n = 10
    base = [0.0] * n
    c1 = base[:]; c1[5] = 1.0
    c2 = base[:]; c2[5] = 2.0
    c3 = base[:]; c3[5] = 3.0
    universe = {"000001": _mk_df([100.0] * n, change_pct=c1),
                "000002": _mk_df([100.0] * n, change_pct=c2),
                "000003": _mk_df([100.0] * n, change_pct=c3)}
    all_days = list(universe["000001"]["date"])
    pos_of = {c: {dt: i for i, dt in enumerate(all_days)} for c in universe}
    s = env.build_series(universe, pos_of, all_days)
    assert s["r1"].iloc[5] == pytest.approx(0.02)   # median(0.01,0.02,0.03)


def test_build_series_environment_causal():
    """反泄露:改未来(i+2)的 change_pct/amount 不影响 label[i](pandas 3.0 CoW 须用 .loc)。"""
    universe, all_days, pos_of = _universe(70)
    s1 = env.build_series(universe, pos_of, all_days)
    i = 65
    label_before = s1["environment"].iloc[i]
    d = universe["000001"]
    d.loc[i + 2, "change_pct"] = -10.0
    d.loc[i + 2, "amount"] = 0.0
    s2 = env.build_series(universe, pos_of, all_days)
    assert s2["environment"].iloc[i] == label_before


def test_build_series_le_sensitive():
    """<=T 敏感:改当日 change_pct 会改变 r1[i](证明序列确实消费 <=T 数据,非真空)。"""
    universe, all_days, pos_of = _universe(70)
    s1 = env.build_series(universe, pos_of, all_days)
    i = 65
    universe["000001"].loc[i, "change_pct"] = -20.0
    s2 = env.build_series(universe, pos_of, all_days)
    assert s2["r1"].iloc[i] != s1["r1"].iloc[i]
```

- [ ] **Step 2: 跑失败**

Run: `python -m pytest tests/test_environment.py -q`
Expected: 新测试 FAIL(`AttributeError: module 'environment' has no attribute 'build_series'`)。

- [ ] **Step 3: 最小实现**

在 `environment.py` 顶部(模块 docstring 之后、常量之前)加 import:

```python
import numpy as np
import pandas as pd

import analysis as an
```

在 `classify` 之后追加:

```python
def build_series(universe, pos_of, all_days):
    """合成市场序列:行 = all_days 位置,列见 spec §5;末列 environment。"""
    codes = list(universe.keys())
    rows = []
    for dt in all_days:
        rets = []
        up = 0
        lu = 0
        ld = 0
        turnover = 0.0
        n = 0
        for c in codes:
            bar = pos_of[c].get(dt)
            if bar is None:
                continue
            d = universe[c]
            chg = d["change_pct"].iloc[bar]
            if _miss(chg):
                continue
            chg = float(chg)
            rets.append(chg / 100.0)
            n += 1
            if chg > 0:
                up += 1
            th = an.limit_threshold(c)
            if chg >= th:
                lu += 1
            elif chg <= -th:
                ld += 1
            amt = d["amount"].iloc[bar]
            if not _miss(amt):
                turnover += float(amt)
        rows.append({"date": dt,
                     "r1": float(np.median(rets)) if rets else None,
                     "up_ratio": (up / n) if n else None,
                     "limit_up": lu, "limit_down": ld,
                     "turnover": turnover})
    df = pd.DataFrame(rows)
    df["turnover_ratio"] = df["turnover"] / df["turnover"].shift(1).rolling(5).mean()
    r1 = df["r1"].fillna(0.0).to_numpy()
    M = np.empty(len(df))
    if len(df) > 0:
        M[0] = 1.0
        M[1:] = np.cumprod(1.0 + r1[1:])
    df["M"] = M
    df["ma5"] = df["M"].rolling(5).mean()
    df["ma20"] = df["M"].rolling(20).mean()
    df["ma60"] = df["M"].rolling(60).mean()
    df["r5"] = df["M"] / df["M"].shift(5) - 1.0
    df["r20"] = df["M"] / df["M"].shift(20) - 1.0
    df["r60"] = df["M"] / df["M"].shift(60) - 1.0
    df["environment"] = df.apply(classify, axis=1)
    return df
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_environment.py -q`
Expected: `16 passed`

- [ ] **Step 5: 提交**

```bash
git add environment.py tests/test_environment.py
git commit -m "feat(environment): build_series 合成市场序列 + 反泄露"
```

---

### Task 3: `run`/`build_report`/`render_markdown`/`main` CLI

**Files:**
- Modify: `environment.py`(顶部加 import + 5 个函数)
- Test: `tests/test_environment.py`(追加 run/report/main 测试)

**Interfaces:**
- Consumes: `bt.load_sector_map`、`bt.build_universe`、`bt.build_calendar`(backtest.py 公开函数)。
- Produces: `run(data_dir, sector_map_path) -> dict`、`build_report(report) -> dict`、`render_markdown(payload) -> str`、`main(argv=None) -> int`、`_git_short_sha()`、`_num(v)`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_environment.py`:

```python
import backtest as bt


def _patch_bt(monkeypatch, universe, all_days, pos_of):
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: (universe, list(universe.keys())))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))


def test_run_distribution(tmp_path, monkeypatch):
    universe, all_days, pos_of = _universe(70)
    _patch_bt(monkeypatch, universe, all_days, pos_of)
    rep = env.run(str(tmp_path), "x")
    assert rep["n_days"] == 70
    assert rep["n_classified"] == 10
    assert rep["n_insufficient"] == 60
    assert sum(rep["distribution"].values()) == 10
    assert rep["distribution"]["牛"] == 10
    assert set(rep["state_stats"]) == set(env.LABELS)
    assert set(rep["degenerate_states"]) == set(env.LABELS) - {"牛"}


def test_build_report_render():
    rep = {
        "data_range": {"first": "2026-01-01", "last": "2026-08-13"},
        "n_days": 100, "n_classified": 40, "n_insufficient": 60,
        "distribution": {L: (10 if L == "牛" else 5) for L in env.LABELS},
        "state_stats": {L: {"n": 5, "mean_r5": 0.01, "mean_r20": 0.02,
                            "mean_up_ratio": 0.5, "mean_turnover_ratio": 1.0}
                        for L in env.LABELS},
        "degenerate_states": ["恢复"],
        "series": [],
    }
    payload = env.build_report(rep)
    assert payload["mode"] == "environment"
    assert payload["distribution"]["牛"] == 10
    md = env.render_markdown(payload)
    assert "## 状态分布" in md
    assert "## 退化态提示" in md
    assert "恢复" in md


def test_main_end_to_end(tmp_path, monkeypatch):
    import json
    universe, all_days, pos_of = _universe(70)
    _patch_bt(monkeypatch, universe, all_days, pos_of)
    out = tmp_path / "env.json"
    rc = env.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "environment"
    assert payload["n_days"] == 70
    assert out.with_suffix(".md").exists()
```

- [ ] **Step 2: 跑失败**

Run: `python -m pytest tests/test_environment.py -q`
Expected: 新测试 FAIL(`AttributeError: module 'environment' has no attribute 'run'`)。

- [ ] **Step 3: 最小实现**

在 `environment.py` 顶部 import 块加:

```python
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import backtest as bt
```

在 `build_series` 之后追加:

```python
def _git_short_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True)
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def run(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    series = build_series(universe, pos_of, all_days)
    dist = {L: 0 for L in LABELS}
    n_classified = 0
    n_insufficient = 0
    for lab in series["environment"]:
        if lab is None:
            n_insufficient += 1
        else:
            dist[lab] += 1
            n_classified += 1
    state_stats = {}
    for L in LABELS:
        sub = series[series["environment"] == L]
        if len(sub) == 0:
            state_stats[L] = {"n": 0, "mean_r5": None, "mean_r20": None,
                              "mean_up_ratio": None, "mean_turnover_ratio": None}
            continue
        state_stats[L] = {"n": int(len(sub)),
                          "mean_r5": _num(sub["r5"].mean()),
                          "mean_r20": _num(sub["r20"].mean()),
                          "mean_up_ratio": _num(sub["up_ratio"].mean()),
                          "mean_turnover_ratio": _num(sub["turnover_ratio"].mean())}
    degenerate = [L for L in LABELS if dist[L] < MIN_STATE_N]
    series_list = [{"date": str(dt), "environment": lab}
                   for dt, lab in zip(series["date"], series["environment"])]
    return {
        "data_range": {"first": str(all_days[0]), "last": str(all_days[-1])},
        "n_days": len(all_days),
        "n_classified": n_classified,
        "n_insufficient": n_insufficient,
        "distribution": dist,
        "state_stats": state_stats,
        "degenerate_states": degenerate,
        "series": series_list,
    }


def build_report(report):
    return {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "environment",
        "data_range": report["data_range"],
        "n_days": report["n_days"],
        "n_classified": report["n_classified"],
        "n_insufficient": report["n_insufficient"],
        "distribution": report["distribution"],
        "state_stats": report["state_stats"],
        "degenerate_states": report["degenerate_states"],
        "series": report["series"],
        "notes": ["合成指数为 universe 中位收益复合,非真实指数(口径差异见 spec §3)"],
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 市场环境分类报告(七态)", ""]
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- 数据区间: {payload['data_range']['first']} -> {payload['data_range']['last']}")
    L.append(f"- 总交易日: {payload['n_days']};已分类: {payload['n_classified']};"
             f"不足: {payload['n_insufficient']}")
    L += ["", "## 状态分布", "", "| 状态 | 天数 | 占比 |", "|---|---|---|"]
    n = payload["n_classified"]
    for lab in LABELS:
        c = payload["distribution"][lab]
        pct = f"{c / n * 100:.1f}%" if n else "-"
        L.append(f"| {lab} | {c} | {pct} |")
    L += ["", "## 各态特征均值", "", "| 状态 | n | mean_r5 | mean_r20 | mean_up_ratio | mean_turnover_ratio |",
          "|---|---|---|---|---|---|"]
    for lab in LABELS:
        ss = payload["state_stats"][lab]
        L.append(f"| {lab} | {ss['n']} | {fmt(ss['mean_r5'])} | {fmt(ss['mean_r20'])} "
                 f"| {fmt(ss['mean_up_ratio'])} | {fmt(ss['mean_turnover_ratio'])} |")
    if payload["degenerate_states"]:
        L += ["", "## 退化态提示", "",
              f"- 以下状态样本数 < {MIN_STATE_N},分布可能不稳定:"
              f"{', '.join(payload['degenerate_states'])}"]
    L += ["", "## 诚实声明", ""]
    L.append("1. 合成市场指数 = universe 中位日收益复合,非真实上证指数(幸存者+大中盘口径)。")
    L.append("2. 每个交易日恰一标签(优先级决策树,互斥);标签只用 <= 当日数据(纯因果)。")
    L.append("3. 阈值是初值常量,本报告仅锚定分布、不自动调参;退化态如实标注,不编造。")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    parser = argparse.ArgumentParser(description="市场环境分类(七态)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="environment_report.json")
    args = parser.parse_args(argv)
    try:
        report = run(args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = build_report(report)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    md_path = os.path.splitext(args.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"wrote {args.out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_environment.py -q`
Expected: `19 passed`

- [ ] **Step 5: 提交**

```bash
git add environment.py tests/test_environment.py
git commit -m "feat(environment): run/build_report/render_markdown/main CLI"
```

---

### Task 4: 接入 evaluate.py(按环境分准确率)

**Files:**
- Modify: `evaluate.py`(顶部 import + `evaluate()` + `build_report()` + `render_markdown()`)
- Test: `tests/test_evaluate.py`(追加 `test_evaluate_environments_slicing`)

**Interfaces:**
- Consumes: `env.build_series(universe, pos_of, all_days)`、`env.LABELS`(Task 2/1 产物)。
- Produces: `evaluate()` 返回 payload 增 `"environments"`/`"env_n"`;`build_report()` 增同名键;`render_markdown()` 增「各环境准确率」表。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_evaluate.py`(复用文件顶部的 `mk`、`_mk_records`):

```python
def test_evaluate_environments_slicing():
    import backtest as bt
    import predict as pr
    d = mk(list(range(100, 200)))  # 100 日单调涨
    universe = {"000001": d}
    all_days = list(d["date"])
    pos_of = {"000001": {dt: i for i, dt in enumerate(d["date"])}}
    sector_members = {}
    sector_map = {"000001": []}
    cals = {"direction": pr._fit_calibrator([(50.0, 0)] * 10 + [(50.0, 1)] * 10, 2),
            "gap": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "od": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "trend3": pr._fit_calibrator([(50.0, 1)] * 10, 2),
            "return": pr._fit_calibrator([(50.0, 0.0)] * 10, 2, monotone=False),
            "risk": pr._fit_calibrator([(10.0, 0)] * 10, 2)}
    rep = ev.evaluate(_mk_records(), universe, pos_of, all_days, sector_members, sector_map, cals)
    assert set(rep["environments"]) == set(ev.env.LABELS)
    assert sum(rep["env_n"].values()) == 200
    # 200 记录全在 date "2026-04-01"(索引 90);单调涨 100 日 → 该日环境「牛」
    assert rep["env_n"]["牛"] == 200
```

- [ ] **Step 2: 跑失败**

Run: `python -m pytest tests/test_evaluate.py::test_evaluate_environments_slicing -q`
Expected: FAIL(`KeyError: 'environments'`)。

- [ ] **Step 3: 最小实现**

**3a. `evaluate.py` 顶部**(`import predict as pr` 之后)加:

```python
import environment as env
```

**3b. `evaluate()` 内**(`per_layer = {...}` 之后、`significant, se_bounds, ... = _significance(...)` 之前)加:

```python
    series = env.build_series(universe, pos_of, all_days)
    env_records = {L: [] for L in env.LABELS}
    for date, recs in by_date.items():
        i = day_to_i[date]
        state = series["environment"].iloc[i]
        if state is None:
            continue
        env_records[state].extend(recs)
    env_metrics = {L: {dim: _dim_metrics(cals, dim, env_records[L]) for dim in DIMS}
                   for L in env.LABELS}
    env_n = {L: len(env_records[L]) for L in env.LABELS}
```

**3c. `evaluate()` 返回**改为:

```python
    return {"overall": overall, "layers": per_layer,
            "layer_n": {L: len(layer_records[L]) for L in LAYERS},
            "environments": env_metrics, "env_n": env_n,
            "significant": significant, "se_bounds": se_bounds,
            "all_undifferentiated": all_undifferentiated}
```

**3d. `build_report()` 内**(`layers = {...}` 之后、`return {` 之前)加:

```python
    environments = {L: {dim: layer_dim_payload(dim, report["environments"][L][dim])
                        for dim in DIMS} for L in env.LABELS}
```

并在 `return {` 字典里、`"layers": layers,` 之后加两行:

```python
        "environments": environments,
        "env_n": report["env_n"],
```

**3e. `render_markdown()` 内**(「无分化判定」段之前)加:

```python
    L += ["", "## 各环境准确率(按日期环境切片)", "",
          "| 环境 | 维度 | n | 主指标 |", "|---|---|---|---|"]
    for envname in env.LABELS:
        for dim in DIMS:
            cell = payload["environments"][envname][dim]
            if cell.get("_suppressed"):
                L.append(f"| {envname} | {dim} | {cell['n']} | (样本不足) |")
                continue
            L.append(f"| {envname} | {dim} | {cell['n']} | {fmt(cell.get(PRIMARY[dim]))} |")
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_evaluate.py tests/test_environment.py -q`
Expected: 全绿(environment 19 + evaluate 新增 1 + 既有 evaluate 测试全绿)。

- [ ] **Step 5: 提交**

```bash
git add evaluate.py tests/test_evaluate.py
git commit -m "feat(evaluate): 按环境切片六维准确率(接入 environment.build_series)"
```

---

## 收尾(非 TDD 任务,SDD 控制端执行)

1. 全量 `python -m pytest tests/ -q` → 期望 **2 failed, N passed**(2 失败为既有 `test_api.py` 日期漂移,范围外)。
2. 真实缓存冒烟:`python environment.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out _analysis/environment_report.json` → exit 0,写 `.json` + `.md`。
3. 产 gitignored `_analysis/environment_calibration.md`(spec §10):各判定指标分位锚 + 七态分布 + 已知 regime 抽查(2015-06/2016-01/2018/2019-02/2020-02/2020-07/2024-02),退化态如实标注,不自动改阈值。
