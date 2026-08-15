# 快照对比器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增只读模块 `compare.py`,把冻结的前向预测快照(`prediction_snapshot.json`)与后来到达的真实行情做严格时间隔离比对,产出六维准确率报告。

**Architecture:** 单模块纯函数 + CLI,与 `predict/backtest/evaluate` 平级。复用 `backtest.py` 公开函数(`load_sector_map`/`build_universe`/`build_calendar`)与 `predict.py` 常量(`ADVERSE_THRESHOLD`/`N_BINS`);指标公式镜像 predict 但输入为「已存预测字段」而非「composite+校准器」(签名已核不可复用)。

**Tech Stack:** Python 3, pandas, numpy, argparse;测试 pytest。

**Spec:** docs/superpowers/specs/2026-08-15-snapshot-comparator-design.md(权威;本计划从其逐字取值)。

## Global Constraints

- **只读、不 fetch**:comparator 只读 `_analysis/daily/*.pkl` + `_analysis/code2sector.json` + 快照 JSON,绝不 fetch。数据刷新不在本模块。
- **严格时间隔离**:预测字段是快照里冻结的,comparator **不重跑 `score_at`/`predict_at`/任何校准器**;验证只读 ≤as_of 的锚点 close(分母)+ >as_of 的 open1/close1/close3。
- **不 import 下划线私有函数**:只复用 `bt.load_sector_map`/`bt.build_universe`/`bt.build_calendar`(公开)与 `pr.ADVERSE_THRESHOLD`/`pr.N_BINS`(模块常量),不碰 `predict._*`、`backtest._*`。
- **Python 源 ASCII `-`**:Python 源码里所有负号/减号用 ASCII `-`,禁用 U+2212 `−`(仅中文 prose/注释可含 `−`/`±`/`×` 等)。
- **GBK**:`code2sector.json` 以 `encoding="gbk"` 打开(经 `bt.load_sector_map`,无需额外处理)。
- **指标 docstring 交叉引用**:每个 metric 函数 docstring 注明对应 predict 函数(`_class_metric`↔`_metric`、`_path_metric`↔`_metric_path`、`_return_metric`↔`_metric_return`、`_risk_metric`↔`_metric_risk`)。
- **`_analysis/` gitignored**:产物不提交;本模块与测试提交 main。`prediction_snapshot.json` 已在 main(只读输入)。
- **TDD**:每任务先写失败测试 → 跑红 → 实现 → 跑绿 → 提交。
- **直接提交 main**(用户既定偏好,无 worktree)。

---

## File Structure

- **Create** `compare.py` — 快照对比器(纯函数 + CLI)。
- **Create** `tests/test_compare.py` — 逐任务追加测试。

---

### Task 1: `_path_label` + `_actual_outcomes`(标签辅助)

**Files:**
- Create: `compare.py`(模块骨架 + 2 函数)
- Test: `tests/test_compare.py`(骨架 + 2 测试)

**Interfaces:**
- Produces(供 Task 2/4 用):
  - `_path_label(gap_up, od_up) -> str`:`(1,1)→"高开高走"`、`(1,0)→"高开低走"`、`(0,1)→"低开高走"`、`(0,0)→"低开低走"`。
  - `_actual_outcomes(d, bar) -> dict`:`{close1, gap, od, trend3}`,缺对应 bar 时该项为 None(bar+1 越界或任一价格非正 → 四项全 None;bar+3 越界 → 仅 trend3 为 None)。

- [ ] **Step 1: 写失败测试**

`tests/test_compare.py` 新建:

```python
# -*- coding: utf-8 -*-
"""compare.py 快照对比器测试。"""
import pandas as pd
import pytest

import compare


def _mk_df(closes, opens):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-08-{i + 1:02d}" for i in range(n)],
        "open": list(opens),
        "close": list(closes),
    })


def test_path_label_quadrants():
    assert compare._path_label(1, 1) == "高开高走"
    assert compare._path_label(1, 0) == "高开低走"
    assert compare._path_label(0, 1) == "低开高走"
    assert compare._path_label(0, 0) == "低开低走"


def test_actual_outcomes_values():
    d = _mk_df([100.0, 110.0, 121.0, 108.9], [100.0, 105.0, 115.0, 105.0])
    oc = compare._actual_outcomes(d, 0)
    assert oc["close1"] == pytest.approx(0.10)
    assert oc["gap"] == pytest.approx(0.05)
    assert oc["od"] == pytest.approx(110.0 / 105.0 - 1.0)
    assert oc["trend3"] == pytest.approx(108.9 / 100.0 - 1.0)


def test_actual_outcomes_edge_cases():
    d = _mk_df([100.0, 110.0, 121.0], [100.0, 105.0, 115.0])
    # bar+1 越界(末根)
    assert compare._actual_outcomes(d, 2) == {"close1": None, "gap": None, "od": None, "trend3": None}
    # bar+3 越界(bar=1: close1/gap/od 有值,trend3 None)
    oc = compare._actual_outcomes(d, 1)
    assert oc["close1"] == pytest.approx(121.0 / 110.0 - 1.0)
    assert oc["trend3"] is None
    # 非正价
    d2 = _mk_df([-5.0, 110.0], [100.0, 105.0])
    assert compare._actual_outcomes(d2, 0) == {"close1": None, "gap": None, "od": None, "trend3": None}
```

- [ ] **Step 2: 跑红**

Run: `python -m pytest tests/test_compare.py -q`
Expected: `ModuleNotFoundError: No module named 'compare'`(或 `AttributeError: module 'compare' has no attribute '_path_label'`)。

- [ ] **Step 3: 实现**

`compare.py` 新建:

```python
# -*- coding: utf-8 -*-
"""快照对比器:冻结前向快照 vs 真实结果,严格时间隔离。

只读 _analysis/daily/*.pkl、_analysis/code2sector.json(GBK)、冻结快照 JSON,不 fetch。
验证只读 > as_of_date 的真实行情(锚点 close <= as_of_date 作 T+1 分母)。
预测字段是快照里冻结的,绝不重跑 score_at/校准器。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

import backtest as bt
import predict as pr

MODULE_VERSION = "1.0.0"


def _path_label(gap_up, od_up):
    """gap x od -> 四分类路径(镜像 predict._path_label,自实现不引私有)。"""
    if gap_up and od_up:
        return "高开高走"
    if gap_up and not od_up:
        return "高开低走"
    if not gap_up and od_up:
        return "低开高走"
    return "低开低走"


def _actual_outcomes(d, bar):
    """读 >bar 真实行情 -> {close1, gap, od, trend3};缺对应 bar 时该项为 None。"""
    if bar + 1 >= len(d):
        return {"close1": None, "gap": None, "od": None, "trend3": None}
    close0 = float(d["close"].iloc[bar])
    open1 = float(d["open"].iloc[bar + 1])
    close1 = float(d["close"].iloc[bar + 1])
    if close0 <= 0 or open1 <= 0 or close1 <= 0:
        return {"close1": None, "gap": None, "od": None, "trend3": None}
    out = {
        "close1": close1 / close0 - 1.0,
        "gap": open1 / close0 - 1.0,
        "od": close1 / open1 - 1.0,
        "trend3": None,
    }
    if bar + 3 < len(d):
        close3 = float(d["close"].iloc[bar + 3])
        if close3 > 0:
            out["trend3"] = close3 / close0 - 1.0
    return out
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_compare.py -q`
Expected: 3 passed。

- [ ] **Step 5: 提交**

```bash
git add compare.py tests/test_compare.py
git commit -m "feat(compare): _path_label + _actual_outcomes 标签辅助"
```

---

### Task 2: `_class_metric` + `_path_metric`(分类 + 路径指标)

**Files:**
- Modify: `compare.py`(追加 2 函数)
- Test: `tests/test_compare.py`(追加 3 测试)

**Interfaces:**
- Consumes: Task 1 的 `_path_label`。
- Produces(供 Task 4 用):
  - `_class_metric(rows) -> dict`:`rows = [(pred, label)]`,`pred in {"up","down","hold"}`(gap 维在调用前映射 `high->up`/`low->down`)、`label in {0,1}`;返回 `{n, base_rate, hit_rate, n_hold, n_bet}`(n_bet=0 时 hit_rate=None)。
  - `_path_metric(rows) -> dict`:`rows = [(pred_path, actual_path)]`(pred_path 为 None 者已剔除);返回 `{n, acc_path}`(n=0 时 acc_path=None)。

- [ ] **Step 1: 写失败测试**

`tests/test_compare.py` 追加:

```python
def test_class_metric():
    rows = [("up", 1), ("down", 0), ("up", 0), ("hold", 1)]
    m = compare._class_metric(rows)
    assert m["n"] == 4
    assert m["n_hold"] == 1
    assert m["n_bet"] == 3
    assert m["hit_rate"] == pytest.approx(2.0 / 3.0)
    assert m["base_rate"] == pytest.approx(0.5)


def test_class_metric_all_hold():
    m = compare._class_metric([("hold", 1), ("hold", 0)])
    assert m["n_bet"] == 0
    assert m["hit_rate"] is None
    assert m["n_hold"] == 2
    assert m["base_rate"] == pytest.approx(0.5)


def test_path_metric():
    rows = [("高开高走", "高开高走"), ("高开高走", "低开低走"), ("低开低走", "低开低走")]
    m = compare._path_metric(rows)
    assert m["n"] == 3
    assert m["acc_path"] == pytest.approx(2.0 / 3.0)
```

- [ ] **Step 2: 跑红**

Run: `python -m pytest tests/test_compare.py -q`
Expected: `AttributeError: module 'compare' has no attribute '_class_metric'`。

- [ ] **Step 3: 实现**

`compare.py` 追加:

```python
def _class_metric(rows):
    """分类维命中率(镜像 predict._metric 的 hit_rate/n_hold/base_rate,输入为已存预测)。

    rows = [(pred, label)];pred in {"up","down","hold"},label in {0,1}。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "base_rate": None, "hit_rate": None, "n_hold": 0, "n_bet": 0}
    n_hold = 0
    n_bet = 0
    hit = 0
    for pred, label in rows:
        if pred == "hold":
            n_hold += 1
            continue
        n_bet += 1
        if (pred == "up" and label == 1) or (pred == "down" and label == 0):
            hit += 1
    base_rate = float(sum(label for _, label in rows)) / n
    return {"n": n, "base_rate": base_rate,
            "hit_rate": hit / n_bet if n_bet else None,
            "n_hold": n_hold, "n_bet": n_bet}


def _path_metric(rows):
    """路径四分类命中率(镜像 predict._metric_path,输入为已存预测)。

    rows = [(pred_path, actual_path)];pred_path 为 None 者已在采集时剔除。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "acc_path": None}
    correct = sum(1 for p, a in rows if p == a)
    return {"n": n, "acc_path": correct / n}
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_compare.py -q`
Expected: 6 passed。

- [ ] **Step 5: 提交**

```bash
git add compare.py tests/test_compare.py
git commit -m "feat(compare): _class_metric + _path_metric 分类/路径指标"
```

---

### Task 3: `_return_metric` + `_risk_metric`(涨跌幅 + 风险指标)

**Files:**
- Modify: `compare.py`(追加 2 函数)
- Test: `tests/test_compare.py`(追加 2 测试)

**Interfaces:**
- Consumes: `pr.N_BINS`(risk 分箱)、`pr.ADVERSE_THRESHOLD`(供 Task 4 用,本任务只 N_BINS)。
- Produces(供 Task 4 用):
  - `_return_metric(rows) -> dict`:`rows = [(er, close1)]`;返回 `{n, mae, rmse, sign_agreement, sign_n, mean_residual}`(n=0 全 None/sign_n=0)。
  - `_risk_metric(rows) -> dict`:`rows = [(risk_p, adverse)]`;返回 `{n, adverse_rate, ece, brier, lift}`(n=0 全 None;n<N_BINS 降单箱 lift=None)。

- [ ] **Step 1: 写失败测试**

`tests/test_compare.py` 追加:

```python
def test_return_metric():
    rows = [(0.10, 0.10), (0.0, 0.05), (-0.05, -0.05)]
    m = compare._return_metric(rows)
    assert m["n"] == 3
    assert m["mae"] == pytest.approx((0.0 + 0.05 + 0.0) / 3.0)
    assert m["rmse"] == pytest.approx(((0.0 ** 2 + 0.05 ** 2 + 0.0 ** 2) / 3.0) ** 0.5)
    assert m["mean_residual"] == pytest.approx((0.0 + 0.05 + 0.0) / 3.0)
    assert m["sign_n"] == 3
    assert m["sign_agreement"] == pytest.approx(2.0 / 3.0)


def test_risk_metric():
    rows = [(0.1, 0), (0.2, 0), (0.3, 1), (0.4, 1)]
    m = compare._risk_metric(rows)
    assert m["n"] == 4
    assert m["adverse_rate"] == pytest.approx(0.5)
    assert m["brier"] == pytest.approx((0.01 + 0.04 + 0.49 + 0.36) / 4.0)
    assert m["ece"] == pytest.approx(abs(0.25 - 0.5))
    assert m["lift"] is None  # n=4 < N_BINS=10 降单箱
```

- [ ] **Step 2: 跑红**

Run: `python -m pytest tests/test_compare.py -q`
Expected: `AttributeError: module 'compare' has no attribute '_return_metric'`。

- [ ] **Step 3: 实现**

`compare.py` 追加:

```python
def _return_metric(rows):
    """涨跌幅误差(镜像 predict._metric_return,输入为已存预测)。

    rows = [(er, close1)];er=预测 expected_return,close1=实际 T+1 收益。
    residual = close1 - er(正 = 系统性低估,与 predict 同向)。
    符号一致率:close1==0 样本剔除(不计分子分母)。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "sign_agreement": None,
                "sign_n": 0, "mean_residual": None}
    abs_errs = []
    sq_errs = []
    residuals = []
    sign_num = 0
    sign_den = 0
    for er, close1 in rows:
        e = er - close1
        abs_errs.append(abs(e))
        sq_errs.append(e * e)
        residuals.append(close1 - er)
        if close1 != 0:
            sign_den += 1
            if (er > 0 and close1 > 0) or (er < 0 and close1 < 0):
                sign_num += 1
    return {"n": n, "mae": float(np.mean(abs_errs)), "rmse": float(np.sqrt(np.mean(sq_errs))),
            "sign_agreement": sign_num / sign_den if sign_den else None,
            "sign_n": sign_den, "mean_residual": float(np.mean(residuals))}


def _risk_metric(rows):
    """风险概率校准度(镜像 predict._metric_risk,输入为已存 risk_p 直接分箱)。

    rows = [(risk_p, adverse)];adverse = 1[close1 < ADVERSE_THRESHOLD]。
    ECE 按 risk_p 升序等量分 N_BINS 箱(等样本数;n < N_BINS 降单箱),比箱内 mean_p vs realized。
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "adverse_rate": None, "ece": None, "brier": None, "lift": None}
    adverse_rate = float(np.mean([a for _, a in rows]))
    brier = float(np.mean([(rp - a) ** 2 for rp, a in rows]))
    rows_sorted = sorted(rows, key=lambda x: x[0])
    if n < pr.N_BINS:
        bins = [rows_sorted]
    else:
        bins = []
        for b in range(pr.N_BINS):
            lo = b * n // pr.N_BINS
            hi = (b + 1) * n // pr.N_BINS
            bins.append(rows_sorted[lo:hi])
    ece = 0.0
    realized = []
    for chunk in bins:
        if not chunk:
            continue
        mean_p = float(np.mean([rp for rp, _ in chunk]))
        realized_bin = float(np.mean([a for _, a in chunk]))
        realized.append(realized_bin)
        ece += (len(chunk) / n) * abs(mean_p - realized_bin)
    lift = (realized[-1] - realized[0]) if len(realized) >= 2 else None
    return {"n": n, "adverse_rate": adverse_rate, "ece": ece, "brier": brier, "lift": lift}
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_compare.py -q`
Expected: 8 passed。

- [ ] **Step 5: 提交**

```bash
git add compare.py tests/test_compare.py
git commit -m "feat(compare): _return_metric + _risk_metric 涨跌幅/风险指标"
```

---

### Task 4: `verify` 主流程 + 反泄露测试

**Files:**
- Modify: `compare.py`(追加 `verify`)
- Test: `tests/test_compare.py`(追加 6 测试)

**Interfaces:**
- Consumes: Task 1/2/3 的 `_path_label`/`_actual_outcomes`/`_class_metric`/`_path_metric`/`_return_metric`/`_risk_metric`;`bt.load_sector_map`/`bt.build_universe`/`bt.build_calendar`;`pr.ADVERSE_THRESHOLD`。
- Produces(供 Task 5 用):
  - `verify(snapshot_path, data_dir, sector_map_path) -> dict`:`{snapshot, data_range, verification, metrics}`(§7 schema;return/risk 字段缺失时 `{"available": false, "reason": ...}`)。

- [ ] **Step 1: 写失败测试**

`tests/test_compare.py` 追加(公共 fixture + 6 测试):

```python
import backtest as bt

D1_CLOSES = [90.0, 92.0, 95.0, 100.0, 110.0, 121.0, 108.9, 108.9]
D1_OPENS = [90.0, 90.0, 93.0, 97.0, 105.0, 115.0, 105.0, 105.0]
# as_of bar=3(close0=100),open1=105,close1=110,close3=108.9


def _fixture(monkeypatch):
    universe = {
        "000001": _mk_df(D1_CLOSES, D1_OPENS),
        "000002": _mk_df([50.0, 55.0, 60.0, 65.0], [50.0, 55.0, 60.0, 65.0]),
    }
    codes = ["000001", "000002"]
    all_days = ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    pos_of = {
        "000001": {"2026-08-11": 3},
        "000002": {"2026-08-11": 3},  # d2 末根,bar+1 越界
    }
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: (universe, codes))
    monkeypatch.setattr(bt, "build_calendar", lambda univ, cs: (all_days, pos_of))
    return universe


def _snapshot():
    return {
        "mode": "predict", "system_version": "62c60a7", "module_version": "1.0.0",
        "generated_at": "2026-08-14T20:52:42", "as_of_date": "2026-08-11",
        "predictions": [
            {"code": "000001", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "up", "confidence": 0.6, "gap": "high", "od": "up", "path": "高开高走"},
             "T+3": {"direction": "up", "confidence": 0.6}},
            {"code": "000002", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "down", "confidence": 0.6, "gap": "low", "od": "down", "path": "低开低走"},
             "T+3": {"direction": "down", "confidence": 0.6}},
            {"code": "000003", "date": "2026-08-11", "composite": 50.0,
             "T+1": {"direction": "hold", "confidence": 0.5, "gap": "hold", "od": "hold", "path": None},
             "T+3": {"direction": "hold", "confidence": 0.5}},
        ],
    }


def test_verify_core(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    snap = tmp_path / "s.json"
    import json as _json
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r = compare.verify(str(snap), "dd", "sm")
    v = r["verification"]
    assert v["n_predictions"] == 3
    assert v["n_verified"] == 1
    assert v["n_trend3_verified"] == 1
    assert v["n_unverified"] == 2
    assert v["unverified_reasons"] == {"not_in_universe": 1, "no_next_bar": 1, "non_positive_close": 0}
    m = r["metrics"]
    for name in ("direction", "gap", "od", "trend3"):
        assert m[name]["n"] == 1 and m[name]["hit_rate"] == pytest.approx(1.0)
    assert m["path"]["n"] == 1 and m["path"]["acc_path"] == pytest.approx(1.0)
    assert m["return"]["available"] is False
    assert m["risk"]["available"] is False


def test_verify_field_missing(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r = compare.verify(str(snap), "dd", "sm")
    assert r["metrics"]["return"]["available"] is False
    assert "reason" in r["metrics"]["return"]
    assert r["metrics"]["risk"]["available"] is False


def test_verify_anti_leak_snapshot(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    snap2 = _snapshot()
    snap2["predictions"][0]["T+1"]["direction"] = "down"
    snap.write_text(_json.dumps(snap2, ensure_ascii=False), encoding="utf-8")
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r1["metrics"]["direction"]["hit_rate"] != r2["metrics"]["direction"]["hit_rate"]


def test_verify_anti_leak_past(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"]["close"].iloc[2] = 9999.0  # close[bar-1], <= as_of
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"] == r1["metrics"]


def test_verify_anti_leak_anchor(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"]["close"].iloc[3] = 50.0  # close[bar] 锚点分母
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"]["direction"]["hit_rate"] != r1["metrics"]["direction"]["hit_rate"]


def test_verify_anti_leak_future(tmp_path, monkeypatch):
    universe = _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    r1 = compare.verify(str(snap), "dd", "sm")
    universe["000001"]["close"].iloc[4] = 5.0  # close1, > as_of
    r2 = compare.verify(str(snap), "dd", "sm")
    assert r2["metrics"]["direction"]["hit_rate"] != r1["metrics"]["direction"]["hit_rate"]
```

- [ ] **Step 2: 跑红**

Run: `python -m pytest tests/test_compare.py -q`
Expected: `AttributeError: module 'compare' has no attribute 'verify'`。

- [ ] **Step 3: 实现**

`compare.py` 追加:

```python
def verify(snapshot_path, data_dir, sector_map_path):
    with open(snapshot_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("mode") != "predict":
        raise RuntimeError(f"snapshot mode != predict: {snap.get('mode')}")
    as_of_date = snap["as_of_date"]
    predictions = snap.get("predictions", [])
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    all_days, pos_of = bt.build_calendar(universe, codes)

    unverified = {"not_in_universe": 0, "no_next_bar": 0, "non_positive_close": 0}
    n_verified = 0
    n_trend3_verified = 0
    rows = {"direction": [], "gap": [], "od": [], "trend3": [], "path": [], "return": [], "risk": []}
    has_expected_return = False
    has_risk_p = False

    for pred in predictions:
        code = str(pred["code"])
        date = str(pred.get("date") or as_of_date)
        bar = pos_of.get(code, {}).get(date)
        if bar is None:
            unverified["not_in_universe"] += 1
            continue
        d = universe[code]
        if bar + 1 >= len(d):
            unverified["no_next_bar"] += 1
            continue
        close0 = float(d["close"].iloc[bar])
        open1 = float(d["open"].iloc[bar + 1])
        close1 = float(d["close"].iloc[bar + 1])
        if close0 <= 0 or open1 <= 0 or close1 <= 0:
            unverified["non_positive_close"] += 1
            continue
        oc = _actual_outcomes(d, bar)
        n_verified += 1
        close1_up = 1 if oc["close1"] > 0 else 0
        gap_up = 1 if oc["gap"] > 0 else 0
        od_up = 1 if oc["od"] > 0 else 0
        path_actual = _path_label(gap_up, od_up)

        t1 = pred.get("T+1") or {}
        t3 = pred.get("T+3") or {}
        rows["direction"].append((t1.get("direction"), close1_up))
        gap_pred = {"high": "up", "low": "down"}.get(t1.get("gap"), "hold")
        rows["gap"].append((gap_pred, gap_up))
        rows["od"].append((t1.get("od"), od_up))
        pred_path = t1.get("path")
        if pred_path is not None:
            rows["path"].append((pred_path, path_actual))
        if oc["trend3"] is not None:
            trend3_up = 1 if oc["trend3"] > 0 else 0
            n_trend3_verified += 1
            rows["trend3"].append((t3.get("direction"), trend3_up))
        er = pred.get("expected_return")
        if er is not None:
            has_expected_return = True
            rows["return"].append((float(er), oc["close1"]))
        rp = pred.get("risk_p")
        if rp is not None:
            has_risk_p = True
            rows["risk"].append((float(rp), 1 if oc["close1"] < pr.ADVERSE_THRESHOLD else 0))

    metrics = {}
    for name in ("direction", "gap", "od", "trend3"):
        metrics[name] = _class_metric(rows[name])
    metrics["path"] = _path_metric(rows["path"])
    if has_expected_return:
        metrics["return"] = _return_metric(rows["return"])
        metrics["return"]["available"] = True
    else:
        metrics["return"] = {"available": False, "reason": "快照无 expected_return 字段(1.0.0 引擎生成)"}
    if has_risk_p:
        metrics["risk"] = _risk_metric(rows["risk"])
        metrics["risk"]["available"] = True
    else:
        metrics["risk"] = {"available": False, "reason": "快照无 risk_p 字段(1.0.0 引擎生成)"}

    return {
        "snapshot": {
            "path": snapshot_path,
            "system_version": snap.get("system_version"),
            "module_version": snap.get("module_version"),
            "generated_at": snap.get("generated_at"),
            "as_of_date": as_of_date,
        },
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        "verification": {
            "n_predictions": len(predictions),
            "n_verified": n_verified,
            "n_trend3_verified": n_trend3_verified,
            "n_unverified": sum(unverified.values()),
            "unverified_reasons": unverified,
        },
        "metrics": metrics,
    }
```

- [ ] **Step 4: 跑绿**

Run: `python -m pytest tests/test_compare.py -q`
Expected: 14 passed。

- [ ] **Step 5: 提交**

```bash
git add compare.py tests/test_compare.py
git commit -m "feat(compare): verify 主流程(六维比对 + 不可验证计数 + 反泄露)"
```

---

### Task 5: `_git_short_sha` + `_num` + `build_report` + `render_markdown` + `main`

**Files:**
- Modify: `compare.py`(追加 5 函数)
- Test: `tests/test_compare.py`(追加 2 测试)

**Interfaces:**
- Consumes: Task 4 的 `verify`。
- Produces: `main(argv)` CLI(`--snapshot/--data-dir/--sector-map/--out`),写 JSON + 同基名 MD。

- [ ] **Step 1: 写失败测试**

`tests/test_compare.py` 追加:

```python
def test_main_end_to_end(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    import json as _json
    snap = tmp_path / "s.json"
    snap.write_text(_json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "v.json"
    rc = compare.main(["--snapshot", str(snap), "--data-dir", "dd",
                       "--sector-map", "sm", "--out", str(out)])
    assert rc == 0
    payload = _json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "compare"
    assert payload["snapshot"]["system_version"] == "62c60a7"
    assert set(payload["metrics"]) == {"direction", "gap", "od", "trend3", "path", "return", "risk"}
    assert payload["metrics"]["return"]["available"] is False
    assert out.with_suffix(".md").exists()


def test_main_missing_snapshot(tmp_path, monkeypatch):
    _fixture(monkeypatch)
    rc = compare.main(["--snapshot", str(tmp_path / "nope.json"), "--data-dir", "dd",
                       "--sector-map", "sm", "--out", str(tmp_path / "v.json")])
    assert rc == 1
```

- [ ] **Step 2: 跑红**

Run: `python -m pytest tests/test_compare.py -q`
Expected: `AttributeError: module 'compare' has no attribute 'main'`。

- [ ] **Step 3: 实现**

`compare.py` 追加:

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


def build_report(results):
    payload = {
        "system_version": _git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "compare",
        "snapshot": results["snapshot"],
        "data_range": results["data_range"],
        "verification": results["verification"],
        "metrics": {},
    }
    for name, m in results["metrics"].items():
        if name in ("return", "risk") and m.get("available") is False:
            payload["metrics"][name] = {"available": False, "reason": m["reason"]}
            continue
        if name in ("direction", "gap", "od", "trend3"):
            payload["metrics"][name] = {"n": m["n"], "base_rate": _num(m["base_rate"]),
                                        "hit_rate": _num(m["hit_rate"]), "n_hold": m["n_hold"],
                                        "n_bet": m["n_bet"]}
        elif name == "path":
            payload["metrics"][name] = {"n": m["n"], "acc_path": _num(m["acc_path"])}
        elif name == "return":
            payload["metrics"][name] = {"available": True, "n": m["n"], "mae": _num(m["mae"]),
                                        "rmse": _num(m["rmse"]),
                                        "sign_agreement": _num(m["sign_agreement"]),
                                        "sign_n": m["sign_n"], "mean_residual": _num(m["mean_residual"])}
        else:
            payload["metrics"][name] = {"available": True, "n": m["n"],
                                        "adverse_rate": _num(m["adverse_rate"]),
                                        "ece": _num(m["ece"]), "brier": _num(m["brier"]),
                                        "lift": _num(m["lift"])}
    return payload


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    snap = payload["snapshot"]
    L = ["# 快照对比报告(预测 vs 真实结果)", ""]
    L.append(f"- 快照 as_of_date: {snap['as_of_date']}")
    L.append(f"- 快照引擎 system_version: `{snap['system_version']}` (module {snap['module_version']})")
    L.append(f"- 快照生成时间: {snap['generated_at']}")
    L.append(f"- comparator system_version: `{payload['system_version']}` (module {payload['module_version']})")
    L.append(f"- 数据区间: {payload['data_range']['start']} -> {payload['data_range']['end']}")
    v = payload["verification"]
    L += ["", "## 验证计数", ""]
    L.append(f"- 预测数: {v['n_predictions']}")
    L.append(f"- T+1 可验证: {v['n_verified']} / 不可验证: {v['n_unverified']}"
             f"(无下日 bar {v['unverified_reasons']['no_next_bar']} / 不在宇宙 {v['unverified_reasons']['not_in_universe']}"
             f" / 非正价 {v['unverified_reasons']['non_positive_close']})")
    L.append(f"- T+3 可验证: {v['n_trend3_verified']}")
    L += ["", "## 六维指标", "", "| 维度 | n | 主指标 | 备注 |", "|---|---|---|---|"]
    m = payload["metrics"]
    for name, label in (("direction", "方向"), ("gap", "开盘"), ("od", "盘中"), ("trend3", "T+3趋势")):
        mm = m[name]
        L.append(f"| {label} | {mm['n']} | hit_rate={fmt(mm['hit_rate'])} (base={fmt(mm['base_rate'])})"
                 f" | n_hold={mm['n_hold']} n_bet={mm['n_bet']} |")
    pm = m["path"]
    L.append(f"| 路径 | {pm['n']} | acc_path={fmt(pm['acc_path'])} | - |")
    for name, label in (("return", "涨跌幅"), ("risk", "风险")):
        mm = m[name]
        if not mm.get("available"):
            L.append(f"| {label} | - | 不可验证 | {mm['reason']} |")
        elif name == "return":
            L.append(f"| {label} | {mm['n']} | mae={fmt(mm['mae'])} rmse={fmt(mm['rmse'])}"
                     f" sign={fmt(mm['sign_agreement'])} (n={mm['sign_n']}) | resid={fmt(mm['mean_residual'])} |")
        else:
            L.append(f"| {label} | {mm['n']} | adverse_rate={fmt(mm['adverse_rate'])}"
                     f" ece={fmt(mm['ece'])} brier={fmt(mm['brier'])} | lift={fmt(mm['lift'])} |")
    L += ["", "## 诚实声明", ""]
    L.append("1. 验证读两类行情:<=as_of 锚点 close(分母)+ >as_of 的 open1/close1/close3;预测字段是快照里冻结的,comparator 不重跑评分/校准。")
    L.append("2. 快照不可变:快照引擎代码 / comparator 代码 / 验证数据区间三者独立,互不冒充。")
    L.append("3. 样本按「股票x时间」聚集、非 i.i.d.,n 为样本数;单份前向快照 n 较小,准确率波动大,只作单次前向验证。")
    L.append("4. return/risk 维若快照无对应字段,如实标「不可验证」,不回退、不编造。")
    L.append("5. risk 维 ECE 按已存 risk_p 直接分箱(非原始 risk 经校准器)。")
    L.append("")
    return "\n".join(L)


def main(argv=None):
    parser = argparse.ArgumentParser(description="快照对比器(预测快照 vs 真实结果)")
    parser.add_argument("--snapshot", default="prediction_snapshot.json")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="snapshot_verification.json")
    args = parser.parse_args(argv)
    try:
        results = verify(args.snapshot, args.data_dir, args.sector_map)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    payload = build_report(results)
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

Run: `python -m pytest tests/test_compare.py -q`
Expected: 16 passed。

- [ ] **Step 5: 提交**

```bash
git add compare.py tests/test_compare.py
git commit -m "feat(compare): build_report/render_markdown/main + CLI 报告"
```

---

## 最终收尾(全量测试 + 真实缓存冒烟,非独立 Task)

最后一个 Task 提交后:

```bash
python -m pytest tests/ -q
```

Expected: 既有 224 passed + 新增 16 = 240 passed;2 条既有 `tests/test_api.py` 日期漂移失败(范围外,已知,不计)。

真实缓存冒烟:

```bash
python compare.py --snapshot prediction_snapshot.json --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out _analysis/snapshot_verification.json
```

Expected: `wrote _analysis/snapshot_verification.json and _analysis/snapshot_verification.md`;按 spec §3 实测分布验收(gap/trend3 出真实 hit_rate,direction/od 全 hold → hit_rate=None,path n=0,return/risk 不可验证)。
