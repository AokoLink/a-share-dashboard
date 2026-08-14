# 预测引擎(S1)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增生产级模块 `predict.py`,在现有 `analysis.score_stock` 评分之上叠校准概率层,产出次日方向/开盘/盘中路径/T+3 趋势的结构化预测(带版本戳),并支持回测评估与前向快照。

**Architecture:** 单模块纯函数 `predict.py`,复用 `backtest.py` 公开函数(`build_universe`/`build_calendar`/`build_buyable`/`score_at`/`next_returns`)与 `analysis.limit_threshold`/`analysis.score_stock`;不修改 backtest.py。核心是四个二分类 `Calibrator`(composite → 经验概率,等量 10 分箱 + PAV 单调池化,手写不引 scipy),按 80/20 temporal 切分 train 拟合 / valid 评估。

**Tech Stack:** Python 3 + pandas 3.0.5 + numpy + argparse;测试 pytest。无 scipy(PAV/Welch 手写)。

**Spec:** `docs/superpowers/specs/2026-08-14-prediction-engine-design.md`(计划从 spec 论证,执行者两份都读)。

## Global Constraints

- 直接提交 main,不建 worktree/feature 分支。
- `_analysis/` gitignore——探针/临时数据永不提交。
- Python 源码仅 ASCII `-`,禁用 U+2212 `−`(代码/注释里的减号一律 ASCII)。
- pandas 3.x CoW:`df["col"].iloc[0] = x` 抛错;赋整列用单次 `.iloc[row, colidx] = v`。
- 不伪造测试成功;每个任务跑真实 `python -m pytest tests/test_predict.py -q` 与全量回归。
- 测试输出一律 `tmp_path`,不污染 repo 根;S1 基线产物(`prediction_baseline.json`/`.md`)在最终验收时于 repo 根生成并提交(同 `backtest_baseline.json` 先例)。
- `import backtest as bt`、`import analysis as an`、`import predict as pr` 依赖根 `conftest.py` 的 `sys.path.insert`(已存在)。
- `an.limit_threshold(code)` 返回 **9.9/19.9/29.9**(百分比);跌停阈值语义用字面 `-7.0`(backtest.py:155)。流动性阈值用 `bt.MIN_AMOUNT`(=1e8)。

## File Structure

- **Create `predict.py`**(repo 根,与 analysis.py/backtest.py 平级):全部逻辑。
- **Create `tests/test_predict.py`**:全部测试(含本地 `make_daily` 辅助)。

`predict.py` 顶层(导入块 + 常量)在 Task 1 落地,后续任务沿用:

```python
# -*- coding: utf-8 -*-
"""预测引擎(多维结构化预测):在评分之上叠校准概率层。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
严格禁止未来数据泄露:预测输入 <= T,验证只读 > T 的真实数据。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import analysis as an
import backtest as bt

MODULE_VERSION = "1.0.0"
EVAL_DAYS = 1200
TRAIN_FRAC = 0.8
N_BINS = 10
DIRECTION_BAND = 0.05
AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)
```

`tests/test_predict.py` 顶层:

```python
# -*- coding: utf-8 -*-
import json

import numpy as np
import pandas as pd
import pytest

import backtest as bt
import analysis as an
import predict as pr


def make_daily(closes, opens=None, volumes=None, start="2026-01-01", code="000001"):
    """构造日线 DataFrame,含 date/open/high/low/close/volume/code/change_pct。"""
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    vols = [float(v) for v in volumes] if volumes is not None else [100000.0] * n
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    closes = [float(c) for c in closes]
    change = [0.0] + [
        round((closes[i] / closes[i - 1] - 1) * 100, 6) if closes[i - 1] != 0 else 0.0
        for i in range(1, n)
    ]
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols, "code": [code] * n, "change_pct": change,
    })
```

---

### Task 1: `fwd_close` + `_labels`

**Files:**
- Create: `predict.py`(导入块 + 常量 + 两个函数)
- Test: `tests/test_predict.py`(make_daily + 6 个测试)

**Interfaces:**
- Produces:
  - `fwd_close(d, i, k)` → float(close[i+k]/close[i]-1)或 None(越界/非正价)。
  - `_labels(d, i)` → dict `{close1, gap, od, trend3}`(0/1/None),`bt.next_returns` None 则整体返回 None。

- [ ] **Step 1: 写失败测试**

```python
def test_fwd_close_exact():
    d = make_daily([10.0, 11.0, 12.0, 13.0, 14.0])
    assert pr.fwd_close(d, 0, 1) == pytest.approx(0.1)
    assert pr.fwd_close(d, 0, 3) == pytest.approx(0.3)


def test_fwd_close_out_of_range_none():
    d = make_daily([10.0, 11.0])
    assert pr.fwd_close(d, 0, 2) is None
    assert pr.fwd_close(d, 1, 1) is None


def test_fwd_close_nonpositive_none():
    d = make_daily([10.0, 0.0, 11.0])
    assert pr.fwd_close(d, 0, 1) is None


def test_labels_all_four():
    closes = [10.0, 11.0, 12.0, 13.0]
    opens = [10.0, 10.5, 12.0, 13.0]
    d = make_daily(closes, opens=opens)
    lbl = pr._labels(d, 0)
    assert lbl["close1"] == 1
    assert lbl["gap"] == 1
    assert lbl["od"] == 1
    assert lbl["trend3"] == 1


def test_labels_trend3_none_when_short():
    d = make_daily([10.0, 11.0, 12.0])  # i=0 → i+3=3 >= len → trend3 None
    lbl = pr._labels(d, 0)
    assert lbl["close1"] in (0, 1)
    assert lbl["trend3"] is None


def test_labels_next_returns_none():
    d = make_daily([10.0])  # i=0 → i+1 >= len → next_returns None
    assert pr._labels(d, 0) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py -q`
Expected: FAIL(ModuleNotFoundError: predict / NameError: fwd_close)

- [ ] **Step 3: 实现**

```python
def fwd_close(d, i, k):
    """close[i+k]/close[i]-1;越界或非正价返回 None。"""
    if i + k >= len(d):
        return None
    c0 = float(d["close"].iloc[i])
    ck = float(d["close"].iloc[i + k])
    if c0 <= 0 or ck <= 0:
        return None
    return ck / c0 - 1


def _labels(d, i):
    """四 horizon 二分类标签 {close1, gap, od, trend3};next_returns None 则整体 None。"""
    nr = bt.next_returns(d, i)
    if nr is None:
        return None
    out = {
        "close1": 1 if nr["close1"] > 0 else 0,
        "gap": 1 if nr["gap"] > 0 else 0,
        "od": 1 if nr["od"] > 0 else 0,
        "trend3": None,
    }
    f3 = fwd_close(d, i, 3)
    if f3 is not None:
        out["trend3"] = 1 if f3 > 0 else 0
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): fwd_close + _labels 目标公式(T+1/T+3 标签)"
```

---

### Task 2: `Calibrator` + `_fit_calibrator`

**Files:**
- Modify: `predict.py`(追加两个符号)
- Test: `tests/test_predict.py`(追加 4 个测试)

**Interfaces:**
- Produces:
  - `Calibrator(bins, degraded=False)` — 类,`bins` 为 `[(upper, p), ...]`(upper 升序,末箱 upper=inf),`p_up(composite)` → float/None。
  - `_fit_calibrator(pairs, n_bins)` — `pairs` 为 `[(composite, label), ...]`(0/1),返回 `Calibrator`。

- [ ] **Step 1: 写失败测试**

```python
def test_fit_calibrator_bin_probabilities():
    pairs = [(1.0, 0)] * 10 + [(9.0, 1)] * 10
    cal = pr._fit_calibrator(pairs, 2)
    assert not cal.degraded
    assert cal.p_up(1.0) == pytest.approx(0.0)
    assert cal.p_up(9.0) == pytest.approx(1.0)


def test_fit_calibrator_pav_monotone():
    pairs = [(1.0, 1)] * 4 + [(2.0, 0)] * 4 + [(3.0, 1)] * 4
    cal = pr._fit_calibrator(pairs, 3)
    ps = [p for _, p in cal.bins]
    assert ps == sorted(ps)  # PAV 后单调不减
    assert cal.p_up(1.0) == pytest.approx(0.5)
    assert cal.p_up(2.0) == pytest.approx(0.5)
    assert cal.p_up(3.0) == pytest.approx(1.0)


def test_p_up_boundary_and_none():
    cal = pr._fit_calibrator([(1.0, 0)] * 5 + [(2.0, 1)] * 5, 2)
    assert cal.p_up(0.5) == pytest.approx(0.0)
    assert cal.p_up(1.0) == pytest.approx(0.0)
    assert cal.p_up(1.5) == pytest.approx(0.0)
    assert cal.p_up(2.0) == pytest.approx(1.0)
    assert cal.p_up(99.0) == pytest.approx(1.0)
    assert cal.p_up(None) is None
    assert cal.p_up(float("nan")) is None


def test_fit_calibrator_degraded_single_bin():
    cal = pr._fit_calibrator([(1.0, 1), (2.0, 0)], 10)
    assert cal.degraded
    assert len(cal.bins) == 1
    assert cal.p_up(1.0) == pytest.approx(0.5)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py -q`
Expected: FAIL(NameError: Calibrator)

- [ ] **Step 3: 实现**

```python
class Calibrator:
    """composite → 单调经验概率。bins = [(upper, p), ...],upper 升序,末箱 upper=inf。"""

    def __init__(self, bins, degraded=False):
        self.bins = bins
        self.degraded = degraded

    def p_up(self, composite):
        if composite is None:
            return None
        try:
            f = float(composite)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        for upper, p in self.bins:
            if f <= upper:
                return p
        return self.bins[-1][1]


def _fit_calibrator(pairs, n_bins):
    """等量分箱 + PAV 单调池化。n < n_bins 降为单箱(degraded)。"""
    clean = []
    for c, l in pairs:
        try:
            fc, fl = float(c), float(l)
        except (TypeError, ValueError):
            continue
        if fc != fc or fl != fl:
            continue
        clean.append((fc, fl))
    if len(clean) < n_bins:
        if not clean:
            return Calibrator([(float("inf"), 0.5)], degraded=True)
        p = sum(l for _, l in clean) / len(clean)
        return Calibrator([(float("inf"), p)], degraded=True)
    clean.sort(key=lambda x: x[0])
    n = len(clean)
    bins = []
    for b in range(n_bins):
        lo = b * n // n_bins
        hi = (b + 1) * n // n_bins
        chunk = clean[lo:hi]
        p = sum(l for _, l in chunk) / len(chunk)
        if hi < n:
            upper = (chunk[-1][0] + clean[hi][0]) / 2.0
        else:
            upper = chunk[-1][0]
        bins.append([upper, p, len(chunk)])
    while True:
        merged = False
        out = []
        i = 0
        while i < len(bins):
            if i + 1 < len(bins) and bins[i][1] > bins[i + 1][1]:
                n_m = bins[i][2] + bins[i + 1][2]
                p_m = (bins[i][1] * bins[i][2] + bins[i + 1][1] * bins[i + 1][2]) / n_m
                out.append([bins[i + 1][0], p_m, n_m])
                i += 2
                merged = True
            else:
                out.append(bins[i])
                i += 1
        bins = out
        if not merged:
            break
    finite = [(u, p) for u, p, _ in bins]
    finite[-1] = (float("inf"), finite[-1][1])
    return Calibrator(finite, degraded=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): Calibrator 等量分箱 + PAV 单调池化"
```

---

### Task 3: `forward_universe`

**Files:**
- Modify: `predict.py`(追加一个函数)
- Test: `tests/test_predict.py`(追加 1 个测试)

**Interfaces:**
- Produces: `forward_universe(universe, pos_of, all_days)` → dict `{code: bar}`。
- Consumes: `an.limit_threshold(code)`、`bt.MIN_AMOUNT`。

- [ ] **Step 1: 写失败测试**

```python
def test_forward_universe_filters():
    def mk(code, closes, vols):
        return make_daily(closes, volumes=vols, code=code)

    universe = {
        "000001": mk("000001", [10.0, 10.2, 10.4], [1e7] * 3),   # 正常(末 bar 无 T+1,仍保留)
        "000002": mk("000002", [5.0, 5.1], [1e7] * 2),          # 停牌:无 as_of_date bar
        "000003": mk("000003", [10.0, 10.0, 11.0], [1e7] * 3),  # 涨停:chg ≈ 10% >= 9.9
        "000004": mk("000004", [10.0, 10.0, 9.0], [1e7] * 3),   # 跌超 7%:-10%
        "000005": mk("000005", [10.0, 10.0, 10.1], [1e3] * 3),  # 低换手:vol*close < 1e8
    }
    pos_of = {c: {dt: i for i, dt in enumerate(d["date"])} for c, d in universe.items()}
    all_days = sorted(set().union(*[set(d["date"]) for d in universe.values()]))
    fwd = pr.forward_universe(universe, pos_of, all_days)
    assert set(fwd.keys()) == {"000001"}
    assert fwd["000001"] == 2  # 最后一天 index
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py::test_forward_universe_filters -q`
Expected: FAIL(NameError: forward_universe)

- [ ] **Step 3: 实现**

```python
def forward_universe(universe, pos_of, all_days):
    """前向宇宙 {code: bar}:as_of_date 当日可交易股,不要求 T+1 bar 存在。"""
    dt = all_days[-1]
    out = {}
    for c in universe:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        d = universe[c]
        chg = float(d["change_pct"].iloc[bar])
        close = float(d["close"].iloc[bar])
        th = an.limit_threshold(c)
        if chg >= th or chg <= -7.0:
            continue
        if float(d["volume"].iloc[bar]) * close < bt.MIN_AMOUNT:
            continue
        out[c] = bar
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): forward_universe 前向宇宙(不要求 T+1)"
```

---

### Task 4: `predict_at` + 方向/置信度/路径辅助 + 反泄露

**Files:**
- Modify: `predict.py`(追加 `_dir_conf`/`_gap_dir`/`_path`/`predict_at`)
- Test: `tests/test_predict.py`(追加 4 个测试,含反泄露)

**Interfaces:**
- Produces: `predict_at(d, i, now, cals)` → dict(§7 schema)或 None;`_dir_conf(p)` → `(direction, confidence)`。
- Consumes: `bt.score_at(d, i, now)`(返回 sc 或 None,sc 含 `composite`);`cals` 为 dict 键 `direction`/`gap`/`od`/`trend3`,各为 `Calibrator`。

- [ ] **Step 1: 写失败测试**

```python
def _fake_cals():
    up = pr._fit_calibrator([(5.0, 1)] * 10, 2)    # p=1
    down = pr._fit_calibrator([(5.0, 0)] * 10, 2)  # p=0
    return {"direction": up, "gap": down, "od": up, "trend3": up}


def test_predict_at_full_schema(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4})
    pred = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert pred["code"] == "000001"
    assert pred["date"] == d["date"].iloc[10]
    assert pred["composite"] == pytest.approx(61.4)
    t1 = pred["T+1"]
    assert t1["direction"] == "up"
    assert t1["confidence"] == pytest.approx(1.0)
    assert t1["gap"] == "low"
    assert t1["od"] == "up"
    assert t1["path"] == "低开高走"
    assert pred["T+3"]["direction"] == "up"
    assert pred["T+3"]["confidence"] == pytest.approx(1.0)


def test_predict_at_t3_no_future_bars(monkeypatch):
    # B2 修复:T+3 只由 calibrator 决定,不读 fwd_close;锚定最后一根仍产出
    d = make_daily([10.0] * 5)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 50.0})
    pred = pr.predict_at(d, 4, pr.AFTER_CLOSE, _fake_cals())  # i=4 最后一根
    assert pred["T+3"]["direction"] == "up"
    assert pred["T+3"]["confidence"] == pytest.approx(1.0)


def test_predict_at_composite_none_returns_none(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: None)
    assert pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals()) is None


def test_predict_at_no_future_leak(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4})
    before = pr.fwd_close(d, 10, 3)
    p1 = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    for col in ("open", "close"):
        d.iloc[11:14, d.columns.get_loc(col)] = 999.0
    after = pr.fwd_close(d, 10, 3)
    assert after != before  # 未来确实被改动
    p2 = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert p1 == p2  # 但预测逐位不变(不读未来)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py -q`
Expected: FAIL(NameError: predict_at)

- [ ] **Step 3: 实现**

```python
def _dir_conf(p):
    """二分类校准概率 → (direction, confidence);direction ∈ {up, down, hold}。"""
    if p is None:
        return None, None
    if p > 0.5 + DIRECTION_BAND:
        return "up", p
    if p < 0.5 - DIRECTION_BAND:
        return "down", 1.0 - p
    return "hold", max(p, 1.0 - p)


def _gap_dir(p):
    d, _ = _dir_conf(p)
    if d == "up":
        return "high"
    if d == "down":
        return "low"
    return "hold"


def _path(gap_dir, od_dir):
    """gap×od → 四分类路径;任一方观望返回 None。"""
    if gap_dir == "hold" or od_dir == "hold":
        return None
    if gap_dir == "high" and od_dir == "up":
        return "高开高走"
    if gap_dir == "high" and od_dir == "down":
        return "高开低走"
    if gap_dir == "low" and od_dir == "up":
        return "低开高走"
    return "低开低走"


def predict_at(d, i, now, cals):
    """单股 ≤T → 结构化预测 dict;composite 不可得返回 None。"""
    sc = bt.score_at(d, i, now)
    if sc is None:
        return None
    composite = float(sc["composite"])
    p_dir = cals["direction"].p_up(composite)
    p_gap = cals["gap"].p_up(composite)
    p_od = cals["od"].p_up(composite)
    p_t3 = cals["trend3"].p_up(composite)
    direction, confidence = _dir_conf(p_dir)
    gap_dir = _gap_dir(p_gap)
    od_dir, _ = _dir_conf(p_od)
    t3_dir, t3_conf = _dir_conf(p_t3)
    return {
        "code": str(d["code"].iloc[i]),
        "date": str(d["date"].iloc[i]),
        "composite": composite,
        "T+1": {
            "direction": direction,
            "confidence": confidence,
            "gap": gap_dir,
            "od": od_dir,
            "path": _path(gap_dir, od_dir),
        },
        "T+3": {"direction": t3_dir, "confidence": t3_conf},
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): predict_at + 方向/置信度/路径 + 反泄露测试"
```

---

### Task 5: `run_backtest` + 采样/评估/指标

**Files:**
- Modify: `predict.py`(追加 `_iter_scored`/`_collect_samples`/`_fit_all`/`_bin_index`/`_metric`/`_path_label`/`_evaluate`/`run_backtest`)
- Test: `tests/test_predict.py`(追加 2 个测试)

**Interfaces:**
- Produces:
  - `run_backtest(data_dir, sector_map_path)` → results dict(键 `calibrators`/`n_samples`/`metrics`/`data_range`/`train_window`/`valid_window`/`n_eval`/`step`/`n_train`/`n_valid`)。
  - `_metric(name, cal, samples)` → dict(键 `n`/`base_rate`/`hit_rate`/`ece`/`brier`/`n_hold`)。
- Consumes: `bt.load_sector_map`/`build_universe`/`build_calendar`/`build_buyable`/`score_at`;`_labels`/`_fit_calibrator`/`_dir_conf`/`_gap_dir`/`_path`。

- [ ] **Step 1: 写失败测试**

```python
def test_metric_hit_rate_and_ece():
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)  # p=[0,1]
    samples = [(1.0, 0)] * 5 + [(9.0, 1)] * 5
    m = pr._metric("direction", cal, samples)
    assert m["n"] == 10
    assert m["base_rate"] == pytest.approx(0.5)
    assert m["hit_rate"] == pytest.approx(1.0)
    assert m["ece"] == pytest.approx(0.0)
    assert m["brier"] == pytest.approx(0.0)
    assert m["n_hold"] == 0


def test_run_backtest_integration(monkeypatch):
    d = make_daily([10.0 + 0.1 * i for i in range(70)])
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_buyable", lambda univ, pos, ad, i: ({"000001"}, {}, {}, {}))
    monkeypatch.setattr(bt, "score_at", lambda dd, i, now: {"composite": 50.0})
    results = pr.run_backtest("/dummy", "/dummy")
    assert set(results["calibrators"]) == {"direction", "gap", "od", "trend3"}
    assert set(results["metrics"]) == {"direction", "gap", "od", "trend3", "path"}
    assert results["n_train"] + results["n_valid"] == results["n_eval"]
    assert results["n_train"] == int(0.8 * results["n_eval"])
    assert results["data_range"]["start"] == d["date"].iloc[0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py -q`
Expected: FAIL(NameError: run_backtest / _metric)

- [ ] **Step 3: 实现**

```python
def _iter_scored(universe, pos_of, all_days, days):
    """逐评估日逐 buyable 股 yield (composite, lbl)。"""
    for i in days:
        dt = all_days[i]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        for c in buyable:
            bar = pos_of[c][dt]
            sc = bt.score_at(universe[c], bar, AFTER_CLOSE)
            if sc is None:
                continue
            lbl = _labels(universe[c], bar)
            if lbl is None:
                continue
            yield float(sc["composite"]), lbl


def _collect_samples(universe, pos_of, all_days, days):
    samples = {"direction": [], "gap": [], "od": [], "trend3": []}
    for composite, lbl in _iter_scored(universe, pos_of, all_days, days):
        samples["direction"].append((composite, lbl["close1"]))
        samples["gap"].append((composite, lbl["gap"]))
        samples["od"].append((composite, lbl["od"]))
        if lbl["trend3"] is not None:
            samples["trend3"].append((composite, lbl["trend3"]))
    return samples


def _fit_all(samples):
    return {name: _fit_calibrator(samples[name], N_BINS) for name in samples}


def _bin_index(cal, composite):
    for idx, (upper, _p) in enumerate(cal.bins):
        if composite <= upper:
            return idx
    return len(cal.bins) - 1


def _metric(name, cal, samples):
    n = len(samples)
    if n == 0:
        return {"n": 0, "base_rate": None, "hit_rate": None, "ece": None, "brier": None, "n_hold": 0}
    base_rate = float(sum(l for _, l in samples)) / n
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    n_hold = 0
    hit = 0
    n_bet = 0
    brier_sum = 0.0
    brier_n = 0
    for composite, label in samples:
        p = cal.p_up(composite)
        direction, _ = _dir_conf(p)
        idx = _bin_index(cal, composite)
        counts[idx] += 1
        sums[idx] += label
        if direction == "hold":
            n_hold += 1
        else:
            n_bet += 1
            correct = (direction == "up" and label == 1) or (direction == "down" and label == 0)
            if correct:
                hit += 1
            brier_sum += (p - label) ** 2
            brier_n += 1
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    return {
        "n": n,
        "base_rate": base_rate,
        "hit_rate": hit / n_bet if n_bet else None,
        "ece": ece,
        "brier": brier_sum / brier_n if brier_n else None,
        "n_hold": n_hold,
    }


def _path_label(gap_up, od_up):
    if gap_up and od_up:
        return "高开高走"
    if gap_up and not od_up:
        return "高开低走"
    if not gap_up and od_up:
        return "低开高走"
    return "低开低走"


def _evaluate(universe, pos_of, all_days, days, cals):
    val = {"direction": [], "gap": [], "od": [], "trend3": []}
    path_n = 0
    path_correct = 0
    for composite, lbl in _iter_scored(universe, pos_of, all_days, days):
        val["direction"].append((composite, lbl["close1"]))
        val["gap"].append((composite, lbl["gap"]))
        val["od"].append((composite, lbl["od"]))
        if lbl["trend3"] is not None:
            val["trend3"].append((composite, lbl["trend3"]))
        gap_dir = _gap_dir(cals["gap"].p_up(composite))
        od_dir, _ = _dir_conf(cals["od"].p_up(composite))
        pred_path = _path(gap_dir, od_dir)
        if pred_path is not None:
            actual = _path_label(lbl["gap"], lbl["od"])
            path_n += 1
            if pred_path == actual:
                path_correct += 1
    metrics = {name: _metric(name, cals[name], val[name]) for name in val}
    metrics["path"] = {"n": path_n, "acc_path": path_correct / path_n if path_n else None}
    return metrics


def run_backtest(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    n = len(eval_days)
    n_train = int(TRAIN_FRAC * n)
    train_days = eval_days[:n_train]
    valid_days = eval_days[n_train:]
    samples = _collect_samples(universe, pos_of, all_days, train_days)
    cals = _fit_all(samples)
    metrics = _evaluate(universe, pos_of, all_days, valid_days, cals)
    return {
        "calibrators": cals,
        "n_samples": {name: len(samples[name]) for name in samples},
        "metrics": metrics,
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "train_window": {"start": str(all_days[train_days[0]]), "end": str(all_days[train_days[-1]])},
        "valid_window": {"start": str(all_days[valid_days[0]]), "end": str(all_days[valid_days[-1]])},
        "n_eval": n, "step": step, "n_train": n_train, "n_valid": n - n_train,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): run_backtest 回测模式 + 采样/评估/ece/brier/hit_rate 指标"
```

---

### Task 6: `predict_now`

**Files:**
- Modify: `predict.py`(追加一个函数)
- Test: `tests/test_predict.py`(追加 1 个测试)

**Interfaces:**
- Produces: `predict_now(data_dir, sector_map_path)` → dict(键 `as_of_date`/`predictions`)。
- Consumes: `_collect_samples`/`_fit_all`/`forward_universe`/`predict_at`。

- [ ] **Step 1: 写失败测试**

```python
def test_predict_now_integration(monkeypatch):
    d = make_daily([10.0 + 0.1 * i for i in range(70)], volumes=[1e7] * 70)
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_buyable", lambda univ, pos, ad, i: ({"000001"}, {}, {}, {}))
    monkeypatch.setattr(bt, "score_at", lambda dd, i, now: {"composite": 50.0})
    res = pr.predict_now("/dummy", "/dummy")
    assert res["as_of_date"] == d["date"].iloc[-1]
    assert len(res["predictions"]) == 1
    pred = res["predictions"][0]
    assert pred["code"] == "000001"
    assert pred["date"] == d["date"].iloc[-1]
    assert pred["T+3"]["direction"] is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py::test_predict_now_integration -q`
Expected: FAIL(NameError: predict_now)

- [ ] **Step 3: 实现**

```python
def predict_now(data_dir, sector_map_path):
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)
    start = max(61, len(all_days) - 1 - EVAL_DAYS)
    step = max(1, (len(all_days) - 2 - start) // 300)
    eval_days = list(range(start, len(all_days) - 1, step))
    samples = _collect_samples(universe, pos_of, all_days, eval_days)
    cals = _fit_all(samples)
    fwd = forward_universe(universe, pos_of, all_days)
    predictions = []
    for c in sorted(fwd):
        pred = predict_at(universe[c], fwd[c], AFTER_CLOSE, cals)
        if pred is not None:
            predictions.append(pred)
    return {"as_of_date": all_days[-1], "predictions": predictions}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): predict_now 前向模式快照(as_of_date + predictions)"
```

---

### Task 7: `build_report` / `render_markdown` / `build_snapshot` / `main`

**Files:**
- Modify: `predict.py`(追加 `_git_short_sha`/`_num`/`build_report`/`render_markdown`/`build_snapshot`/`main`)
- Test: `tests/test_predict.py`(追加 3 个测试)

**Interfaces:**
- Produces:
  - `build_report(results, system_version=None, generated_at=None)` → dict(mode="backtest",含 system_version/calibrators/metrics)。
  - `render_markdown(payload)` → str。
  - `build_snapshot(results)` → dict(mode="predict")。
  - `main(argv=None)` → int(0 成功 / 1 运行错)。
- Consumes: `run_backtest`/`predict_now`(经 monkeypatch 可替换)。

- [ ] **Step 1: 写失败测试**

```python
def _fake_results():
    return {
        "calibrators": {"direction": pr._fit_calibrator([(5.0, 1)] * 10, 2)},
        "n_samples": {"direction": 10},
        "metrics": {
            "direction": {"n": 10, "base_rate": 0.5, "hit_rate": 0.6, "ece": 0.1, "brier": 0.2, "n_hold": 1},
            "path": {"n": 8, "acc_path": 0.5},
        },
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "train_window": {"start": "2026-01-01", "end": "2026-06-01"},
        "valid_window": {"start": "2026-06-02", "end": "2026-08-13"},
        "n_eval": 300, "step": 3, "n_train": 240, "n_valid": 60,
    }


def test_main_backtest_writes_output(tmp_path, monkeypatch):
    out = tmp_path / "pb.json"
    monkeypatch.setattr(pr, "run_backtest", lambda dd, sm: _fake_results())
    rc = pr.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "backtest"
    assert payload["system_version"]
    assert "calibrators" in payload and "metrics" in payload
    assert out.with_suffix(".md").exists()


def test_main_predict_writes_snapshot(tmp_path, monkeypatch):
    out = tmp_path / "snap.json"
    monkeypatch.setattr(pr, "predict_now",
                        lambda dd, sm: {"as_of_date": "2026-08-13", "predictions": [{"code": "000001"}]})
    rc = pr.main(["--predict", "--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "predict"
    assert payload["as_of_date"] == "2026-08-13"
    assert payload["predictions"] == [{"code": "000001"}]
    assert not out.with_suffix(".md").exists()


def test_main_missing_data_dir_exits_nonzero():
    rc = pr.main(["--data-dir", "/nonexistent/xyz", "--sector-map", "/nope"])
    assert rc == 1


def test_main_end_to_end_gbk(tmp_path, monkeypatch):
    # 真实全链路:tmp pkl 集 + GBK 板块映射,monkeypatch 仅固定 score_stock 的 composite
    dailydir = tmp_path / "daily"
    dailydir.mkdir()
    closes = [2000.0 + 0.1 * k for k in range(1200)]
    make_daily(closes, start="2020-01-01", code="000001").to_pickle(dailydir / "000001.pkl")
    sm = tmp_path / "code2sector.json"
    with open(sm, "w", encoding="gbk") as f:
        json.dump({"000001": ["半导体"]}, f, ensure_ascii=False)
    monkeypatch.setattr(an, "score_stock",
                        lambda df, quote, now: {"position": 50.0, "trend": 50.0, "volume_price": 50.0,
                                                "signal": 50.0, "risk": 10.0, "composite": 70.0})
    out = tmp_path / "pb.json"
    rc = pr.main(["--data-dir", str(dailydir), "--sector-map", str(sm), "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "backtest"
    assert payload["system_version"]
    assert set(payload["calibrators"]) == {"direction", "gap", "od", "trend3"}
    assert "ece" in payload["metrics"]["direction"]
    assert out.with_suffix(".md").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_predict.py -q`
Expected: FAIL(NameError: main)

- [ ] **Step 3: 实现**

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
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def build_report(results, system_version=None, generated_at=None):
    system_version = system_version or _git_short_sha()
    generated_at = generated_at or datetime.now().isoformat()
    calibrators = {}
    for name, cal in results["calibrators"].items():
        bins = [{"upper": None if upper == float("inf") else round(upper, 4), "p": round(p, 4)}
                for upper, p in cal.bins]
        calibrators[name] = {"bins": bins, "n": results["n_samples"].get(name), "degraded": cal.degraded}
    metrics = {}
    for name, m in results["metrics"].items():
        if name == "path":
            metrics[name] = {"n": m["n"], "acc_path": _num(m["acc_path"])}
        else:
            metrics[name] = {
                "n": m["n"], "base_rate": _num(m["base_rate"]), "hit_rate": _num(m["hit_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "n_hold": m["n_hold"],
            }
    return {
        "system_version": system_version,
        "module_version": MODULE_VERSION,
        "generated_at": generated_at,
        "mode": "backtest",
        "data_range": results["data_range"],
        "train_window": results["train_window"],
        "valid_window": results["valid_window"],
        "n_eval": results["n_eval"], "step": results["step"],
        "n_train": results["n_train"], "n_valid": results["n_valid"],
        "calibrators": calibrators,
        "metrics": metrics,
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 预测引擎基线报告", ""]
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- generated_at: {payload['generated_at']}")
    L.append(f"- data_range: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    L.append(f"- train_window: {payload['train_window']['start']} -> {payload['train_window']['end']} (n_train={payload['n_train']})")
    L.append(f"- valid_window: {payload['valid_window']['start']} -> {payload['valid_window']['end']} (n_valid={payload['n_valid']})")
    L.append(f"- n_eval={payload['n_eval']}, step={payload['step']}")
    L += ["", "## 校准表(composite 分箱 → P(up))", "", "| 校准器 | 箱上界 | P | degraded | 训练样本 n |", "|---|---|---|---|---|"]
    for name, cal in payload["calibrators"].items():
        for b in cal["bins"]:
            upper = "-" if b["upper"] is None else f"{b['upper']:.4f}"
            L.append(f"| {name} | {upper} | {b['p']:.4f} | {cal['degraded']} | {cal['n']} |")
    L += ["", "## 指标(valid 段,out-of-sample)", "", "| 校准器 | n | base_rate | hit_rate | ece | brier | n_hold |", "|---|---|---|---|---|---|---|"]
    for name, m in payload["metrics"].items():
        if name == "path":
            L.append(f"| path | {m['n']} | - | acc_path={fmt(m['acc_path'])} | - | - | - |")
        else:
            L.append(f"| {name} | {m['n']} | {fmt(m['base_rate'])} | {fmt(m['hit_rate'])} | {fmt(m['ece'])} | {fmt(m['brier'])} | {m['n_hold']} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. 校准概率基于「代理管线」评分(日线 pkl 无 amount,生产板块 composite 不可复现)。")
    L.append("2. 回测模式指标仅在 valid 窗口有效(out-of-sample)。")
    L.append("3. 分钟级路径未做(数据缺口);path 为日线 OHLC 的 gap×od 四分类。")
    L.append("4. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数,ece/brier 不可按 n 直接推置信区间。")
    L.append("5. n_eval 为完整评估日 range,与基线 backtest(跳无热板块日)的 n_eval 可能不同,非同日口径。")
    L.append("")
    return "\n".join(L)


def build_snapshot(results):
    return {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "predict",
        "as_of_date": results["as_of_date"],
        "predictions": results["predictions"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="预测引擎(校准概率 + 多维 horizon)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--predict", action="store_true", help="前向模式(默认回测)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    out = args.out or ("prediction_snapshot.json" if args.predict else "prediction_baseline.json")
    try:
        if args.predict:
            payload = build_snapshot(predict_now(args.data_dir, args.sector_map))
        else:
            payload = build_report(run_backtest(args.data_dir, args.sector_map))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if args.predict:
        print(f"wrote {out}")
    else:
        md_path = os.path.splitext(out)[0] + ".md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(payload))
        print(f"wrote {out} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_predict.py -q`
Expected: 22 passed

- [ ] **Step 5: 全量回归 + Commit**

Run: `python -m pytest tests/ -q`
Expected: 176 + 22 = 198 passed

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): 报告/版本戳/CLI 主入口(build_report/render_markdown/build_snapshot/main)"
```

---

## 收尾(所有任务完成后)

- [ ] 全量 `python -m pytest tests/ -q` 全绿(198 passed)。
- [ ] controller 驱动最终全分支 review(子代理驱动开发的 final review),裁定残余 finding 并记 ledger。
- [ ] 验收(真实缓存,controller 执行):`python predict.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out prediction_baseline.json` 产出含 system_version/train/valid 窗口/四校准器箱表/valid 段 metrics 的报告;`python predict.py --predict ... --out prediction_snapshot.json` 产出带 system_version 的非空前向快照。生成的 `prediction_baseline.json`/`.md`/`prediction_snapshot.json` 作为 S1 基线产物提交 main。
- [ ] 更新 memory 文件 `queued-audit-backtest-optimization.md`(S1 完成态)。
