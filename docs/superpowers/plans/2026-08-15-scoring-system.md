# 评分系统(六维 + 八分层测试池)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把预测引擎从「方向/开盘/路径/T+3趋势」四 horizon 扩展为「方向 + 量级 + 风险」六维,并新建 `evaluate.py` 对八分层测试池逐维报准确率 + ±2σ 噪声界。

**Architecture:** 复用 `predict.py` 的 `_fit_calibrator`(等量十分箱)扩展两个校准器(return 无 PAV、risk PAV),所有指标函数只存在 `predict.py` 一份;`evaluate.py` 只调用 `predict.run_backtest` 取校准器 + valid 记录,自建 universe 打 8 层标签,逐层调用 predict 的指标函数,再做「层 vs 总体」的 ±2σ 显著性判据。

**Tech Stack:** Python 3 + numpy + pandas + pytest(与现有代码一致,无新依赖)。

**Spec:** `docs/superpowers/specs/2026-08-14-scoring-system-design.md`(权威;本计划据其展开,冲突以 spec 为准)。

## Global Constraints

- 只读缓存不 fetch:`predict.py`/`evaluate.py` 只读 `_analysis/daily/*.pkl`(9 列)与 `_analysis/code2sector.json`(GBK),绝不调 `data_source` fetch。
- 严格时间隔离:预测输入 ≤ T,验证只读 > T(沿用 V1.0 反泄露测试模式:改未来 bar → 输出逐位不变)。
- 指标代码只存一份(predict.py);`evaluate.py` 不重拟合、只做分层报告。
- `amount` 不复权(原始成交额,元);大盘层同日横截面 top20%,NaN 排名前 dropna,全缺失日 n=0 如实标注。
- Python 源码只用 ASCII `-`,不用 U+2212 `−`。
- `_fit_calibrator` 的 PAV 通过新增 `monotone=True` 形参控制(参数化,非注释);默认 True 保证 direction/gap/od/trend3/risk 五校准器行为不变。
- 版本:`predict.py` MODULE_VERSION → `1.1.0`;`evaluate.py` MODULE_VERSION = `1.0.0`。
- 不编造结果:命中率/指标不可得时输出 `None`,报告如实标注;n<30 的层只报样本量不报值。
- 每次改完跑 `pytest`;`_analysis/` gitignored,探针不入库。

## 文件结构

- `predict.py`(改):新增 `ADVERSE_THRESHOLD`、`_fit_calibrator(monotone=)`、`_metric_return`、`_metric_risk`;`_iter_scored` 产出 dict;`_collect_samples`/`_fit_all`/`_evaluate`/`run_backtest`/`predict_at`/`build_report`/`render_markdown` 扩展;版本 1.1.0。
- `tests/test_predict.py`(改):更新 `_fake_cals`、`test_run_backtest_integration`、`test_predict_at_full_schema`;新增 monotone/return/risk 指标/扩展字段测试。
- `evaluate.py`(新):8 个分层谓词 + `evaluate()` 核心(打标签 + 逐层指标 + ±2σ 判据)+ `run`/`build_report`/`render_markdown`/`main`。
- `tests/test_evaluate.py`(新):8 分层谓词单测 + `evaluate()` 合成数据集成 + 噪声界/无分化标志 + CLI。

---

### Task 1: `_fit_calibrator` 增加 `monotone` 形参

**Files:**
- Modify: `predict.py:78-126`
- Test: `tests/test_predict.py`

**Interfaces:**
- Produces: `_fit_calibrator(pairs, n_bins, monotone=True) -> Calibrator`。`monotone=False` 时跳过 PAV 池化(等量分箱后直接返回,保留非单调箱)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_predict.py` 末尾追加:

```python
def test_fit_calibrator_monotone_false_keeps_nonmonotone():
    # 非单调输入:PAV 会把它压平为单调,monotone=False 应保留原始非单调箱序
    pairs = [(1.0, 0)] * 4 + [(2.0, 1)] * 4 + [(3.0, 0)] * 4
    cal = pr._fit_calibrator(pairs, 3, monotone=False)
    ps = [p for _, p in cal.bins]
    assert ps != sorted(ps)  # 非单调(1.0→p≈0, 2.0→p≈1, 3.0→p≈0)


def test_fit_calibrator_default_still_pav():
    pairs = [(1.0, 0)] * 4 + [(2.0, 1)] * 4 + [(3.0, 0)] * 4
    cal = pr._fit_calibrator(pairs, 3)
    ps = [p for _, p in cal.bins]
    assert ps == sorted(ps)  # 默认 PAV 单调不减
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_fit_calibrator_monotone_false_keeps_nonmonotone -v`
Expected: FAIL — `_fit_calibrator() got an unexpected keyword argument 'monotone'`

- [ ] **Step 3: 实现**

`predict.py` 修改两处签名与循环:

```python
def _fit_calibrator(pairs, n_bins, monotone=True):
    """等量分箱 + 可选 PAV 单调池化。n < n_bins 降为单箱(degraded)。"""
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
    if monotone:
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

（等价于把原 PAV `while True` 块包进 `if monotone:`。注意保持缩进,其余不变。）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过(既有 22 + 新增 2)。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): _fit_calibrator 增加 monotone 形参(默认 True 行为不变)"
```

---

### Task 2: `_metric_return` + `_metric_path` 指标函数

**Files:**
- Modify: `predict.py`(新增函数,放在 `_metric` 之后,约 line 290)
- Test: `tests/test_predict.py`

**Interfaces:**
- Consumes: `Calibrator.p_up`、`_dir_conf`、`_gap_dir`、`_path`、`_path_label`(均为 predict.py 既有函数,Task 1 已就绪)。
- Produces:
  - `_metric_return(cal, direction_cal, samples) -> dict`,samples = `[(composite, close1), ...]`(close1 连续)。返回键:`n, mae, mae_std, rmse, rmse_std, sign_agreement, sign_n, mean_residual, residual_std, dir_cond_mae`。
  - `_metric_path(gap_cal, od_cal, samples) -> dict`,samples = `[(composite, gap_up, od_up), ...]`(gap_up/od_up 为 0/1)。返回键:`n, acc_path`(gap×od 四分类命中率;任一方向 hold 的样本不计入 n)。

- [ ] **Step 1: 写失败测试**

```python
def test_metric_return_mae_rmse_sign_residual():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)  # 单箱 mean=0.02
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    samples = [(5.0, 0.0), (5.0, 0.04), (5.0, -0.02)]
    m = pr._metric_return(ret, dircal, samples)
    # er=0.02 恒定:abs err = |0.02-close1| = [0.02, 0.02, 0.04];
    # residual = close1-er = [-0.02, 0.02, -0.04]
    assert m["n"] == 3
    assert m["mae"] == pytest.approx((0.02 + 0.02 + 0.04) / 3)
    assert m["rmse"] == pytest.approx(((0.02 ** 2 + 0.02 ** 2 + 0.04 ** 2) / 3) ** 0.5)
    assert m["mean_residual"] == pytest.approx((-0.02 + 0.02 - 0.04) / 3)
    # sign_agreement:close1==0 样本剔除(不计分子分母);非零样本 0.04、-0.02 中
    # er>0 且 close1>0 的只有 0.04 → 1/2
    assert m["sign_n"] == 2
    assert m["sign_agreement"] == pytest.approx(1 / 2)
    # 全部 up,但 n=3 < 30 → dir_cond_mae["up"] 只报 n 不报值
    assert m["dir_cond_mae"]["up"]["n"] == 3
    assert m["dir_cond_mae"]["up"]["mae"] is None
    assert m["dir_cond_mae"]["down"]["n"] == 0
    assert m["dir_cond_mae"]["down"]["mae"] is None


def test_metric_return_zero_close1_excluded_from_sign():
    ret = pr._fit_calibrator([(5.0, 0.0)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)
    m = pr._metric_return(ret, dircal, [(5.0, 0.0)])
    assert m["sign_n"] == 0
    assert m["sign_agreement"] is None


def test_metric_return_empty():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)
    m = pr._metric_return(ret, dircal, [])
    assert m["n"] == 0
    assert m["mae"] is None


def test_metric_return_dir_cond_large_n_reports_mae():
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)
    dircal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    m = pr._metric_return(ret, dircal, [(5.0, 0.03)] * 30)
    assert m["dir_cond_mae"]["up"]["n"] == 30
    assert m["dir_cond_mae"]["up"]["mae"] == pytest.approx(0.01)


def test_metric_path_four_class():
    gap_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)   # p=1 → high
    od_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)    # p=1 → up
    # 预测恒「高开高走」;4 样本各命中其一 → 1/4
    samples = [(5.0, 1, 1), (5.0, 1, 0), (5.0, 0, 1), (5.0, 0, 0)]
    m = pr._metric_path(gap_cal, od_cal, samples)
    assert m["n"] == 4
    assert m["acc_path"] == pytest.approx(0.25)


def test_metric_path_hold_yields_none():
    gap_cal = pr._fit_calibrator([(5.0, 0)] * 5 + [(5.0, 1)] * 5, 1)  # p=0.5 → hold
    od_cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)  # p=1 → up
    m = pr._metric_path(gap_cal, od_cal, [(5.0, 1, 1)])
    assert m["n"] == 0
    assert m["acc_path"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_metric_return_mae_rmse_sign_residual -v`
Expected: FAIL — `AttributeError: module 'predict' has no attribute '_metric_return'`(或 `_metric_path`)

- [ ] **Step 3: 实现**

```python
def _metric_return(cal, direction_cal, samples):
    """return 维度:MAE/RMSE/符号一致率/平均残差/方向条件 MAE。

    samples = [(composite, close1), ...];cal=return 校准器(composite→mean close1);
    direction_cal 用于 dir_cond 分组(经 _dir_conf 得 up/down/hold)。
    符号一致率:close1==0 样本剔除(不计分子分母);residual = close1 - expected。
    方向条件 MAE:按预测方向分组各报 MAE,小 n(<30)只报 n 不报值。
    """
    n = len(samples)
    if n == 0:
        return {"n": 0, "mae": None, "mae_std": None, "rmse": None, "rmse_std": None,
                "sign_agreement": None, "sign_n": 0, "mean_residual": None,
                "residual_std": None,
                "dir_cond_mae": {"up": {"mae": None, "n": 0},
                                 "down": {"mae": None, "n": 0},
                                 "hold": {"mae": None, "n": 0}}}
    abs_errs = []
    sq_errs = []
    residuals = []
    sign_num = 0
    sign_den = 0
    by_dir = {"up": [], "down": [], "hold": []}
    for composite, close1 in samples:
        er = cal.p_up(composite)
        e = er - close1
        abs_errs.append(abs(e))
        sq_errs.append(e * e)
        residuals.append(close1 - er)
        if close1 != 0:
            sign_den += 1
            if (er > 0 and close1 > 0) or (er < 0 and close1 < 0):
                sign_num += 1
        d, _ = _dir_conf(direction_cal.p_up(composite))
        by_dir[d].append(abs(e))
    mae = float(np.mean(abs_errs))
    mae_std = float(np.std(abs_errs, ddof=1)) if n > 1 else 0.0
    rmse = float(np.sqrt(np.mean(sq_errs)))
    rmse_std = float(np.std(sq_errs, ddof=1)) if n > 1 else 0.0
    mean_residual = float(np.mean(residuals))
    residual_std = float(np.std(residuals, ddof=1)) if n > 1 else 0.0
    sign_agreement = sign_num / sign_den if sign_den else None
    dir_cond = {}
    for d in ("up", "down", "hold"):
        arr = by_dir[d]
        n_d = len(arr)
        dir_cond[d] = {"mae": float(np.mean(arr)) if n_d >= 30 else None, "n": n_d}
    return {"n": n, "mae": mae, "mae_std": mae_std, "rmse": rmse, "rmse_std": rmse_std,
            "sign_agreement": sign_agreement, "sign_n": sign_den,
            "mean_residual": mean_residual, "residual_std": residual_std,
            "dir_cond_mae": dir_cond}


def _metric_path(gap_cal, od_cal, samples):
    """path 维度:gap×od 四分类命中率。samples = [(composite, gap_up, od_up), ...]。"""
    path_n = 0
    path_correct = 0
    for composite, gap_up, od_up in samples:
        gap_dir = _gap_dir(gap_cal.p_up(composite))
        od_dir, _ = _dir_conf(od_cal.p_up(composite))
        pred_path = _path(gap_dir, od_dir)
        if pred_path is None:
            continue
        actual = _path_label(gap_up, od_up)
        path_n += 1
        if pred_path == actual:
            path_correct += 1
    return {"n": path_n, "acc_path": path_correct / path_n if path_n else None}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): _metric_return 六项 return 指标 + _metric_path 四分类命中率"
```

---

### Task 3: `_metric_risk` 指标函数

**Files:**
- Modify: `predict.py`(新增函数,紧跟 `_metric_return`)
- Test: `tests/test_predict.py`

**Interfaces:**
- Consumes: `Calibrator.p_up`、`_bin_index`。
- Produces: `_metric_risk(cal, samples) -> dict`,samples = `[(risk, adverse), ...]`(adverse ∈ {0,1})。返回键:`n, adverse_rate, ece, brier, lift`。

- [ ] **Step 1: 写失败测试**

```python
def test_metric_risk_ece_brier_lift():
    # 两箱:risk 低→P(adverse)=0,risk 高→P(adverse)=1
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)
    samples = [(1.0, 0)] * 5 + [(9.0, 1)] * 5
    m = pr._metric_risk(cal, samples)
    assert m["n"] == 10
    assert m["adverse_rate"] == pytest.approx(0.5)
    assert m["ece"] == pytest.approx(0.0)
    assert m["brier"] == pytest.approx(0.0)
    assert m["lift"] == pytest.approx(1.0)  # top bin realized=1 - bottom=0


def test_metric_risk_empty():
    cal = pr._fit_calibrator([(1.0, 0)] * 10 + [(9.0, 1)] * 10, 2)
    m = pr._metric_risk(cal, [])
    assert m["n"] == 0
    assert m["ece"] is None
    assert m["lift"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_metric_risk_ece_brier_lift -v`
Expected: FAIL — `AttributeError: module 'predict' has no attribute '_metric_risk'`

- [ ] **Step 3: 实现**

```python
def _metric_risk(cal, samples):
    """risk 维度:ECE/Brier/单调 lift/adverse_rate。samples = [(risk, adverse), ...]"""
    n = len(samples)
    if n == 0:
        return {"n": 0, "adverse_rate": None, "ece": None, "brier": None, "lift": None}
    counts = [0] * len(cal.bins)
    sums = [0.0] * len(cal.bins)
    brier_sum = 0.0
    for risk, adverse in samples:
        p = cal.p_up(risk)
        idx = _bin_index(cal, risk)
        counts[idx] += 1
        sums[idx] += adverse
        brier_sum += (p - adverse) ** 2
    ece = 0.0
    for idx, (_u, p) in enumerate(cal.bins):
        if counts[idx]:
            ece += (counts[idx] / n) * abs(p - sums[idx] / counts[idx])
    realized = [sums[idx] / counts[idx] if counts[idx] else None for idx in range(len(cal.bins))]
    nonempty = [r for r in realized if r is not None]
    lift = (nonempty[-1] - nonempty[0]) if nonempty else None
    adverse_rate = float(sum(l for _, l in samples)) / n
    return {"n": n, "adverse_rate": adverse_rate, "ece": ece, "brier": brier_sum / n, "lift": lift}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): _metric_risk 风险指标(ECE/Brier/lift/adverse_rate)"
```

---

### Task 4: 接入管线(样本收集 + 6 校准器 + valid 记录)

**Files:**
- Modify: `predict.py`(`ADVERSE_THRESHOLD`、`_iter_scored`、`_collect_samples`、`_fit_all`、`_evaluate`、`run_backtest`)
- Test: `tests/test_predict.py`

**Interfaces:**
- Consumes: Task 1-3 的 `_fit_calibrator(monotone=)`、`_metric_return`、`_metric_risk`、`_metric_path`。
- Produces:
  - `ADVERSE_THRESHOLD = -0.03`(模块常量)。
  - `_iter_scored(...)` yield dict:`{code, date, bar, composite, risk, close1, lbl}`。
  - `_collect_samples(...)` 返回 6 键 `{direction, gap, od, trend3, return, risk}`;`risk` 样本为 `(risk, adverse)`,`return` 样本为 `(composite, close1)`。
  - `_fit_all(samples)` 返回 6 校准器,`return` 用 `monotone=False`。
  - `_evaluate(...) -> (metrics, records)`;metrics 含 `direction/gap/od/trend3/return/risk/path`;records 每条含 `code/date/bar/composite/risk/close1/direction_label/gap/od/trend3/expected_return/risk_p/direction`(其中 `direction_label` 为 0/1 次日方向标签,`close1` 为连续收益)。
  - `run_backtest(...)` 返回值新增 `"valid_records": records`。

- [ ] **Step 1: 写失败测试(更新既有集成测试的断言)**

更新 `tests/test_predict.py::test_run_backtest_integration`,把断言改为 6 校准器 + 6 指标 + valid_records:

```python
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
    assert set(results["calibrators"]) == {"direction", "gap", "od", "trend3", "return", "risk"}
    assert set(results["metrics"]) == {"direction", "gap", "od", "trend3", "return", "risk", "path"}
    assert results["n_train"] + results["n_valid"] == results["n_eval"]
    assert results["n_train"] == int(0.8 * results["n_eval"])
    assert results["data_range"]["start"] == d["date"].iloc[0]
    assert isinstance(results["valid_records"], list)
    if results["valid_records"]:
        rec = results["valid_records"][0]
        for k in ("code", "date", "bar", "composite", "close1", "expected_return"):
            assert k in rec


def test_collect_samples_risk_label_strict_threshold(monkeypatch):
    # 风险标签 = 1[close1 < -0.03](单一阈值,无 base-rate 兜底);边界 -0.03 不判不利
    recs = [
        {"composite": 50.0, "risk": 30.0, "close1": -0.031,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
        {"composite": 50.0, "risk": 30.0, "close1": -0.03,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
        {"composite": 50.0, "risk": 30.0, "close1": -0.029,
         "lbl": {"close1": 0, "gap": 1, "od": 1, "trend3": 1}},
    ]
    monkeypatch.setattr(pr, "_iter_scored", lambda u, p, a, d: iter(recs))
    samples = pr._collect_samples(None, None, None, [0])
    assert samples["risk"] == [(30.0, 1), (30.0, 0), (30.0, 0)]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_run_backtest_integration -v`
Expected: FAIL — `set(results["calibrators"])` 不含 return/risk。

- [ ] **Step 3: 实现**

`predict.py` 顶部常量区(line 20-25 后)加:

```python
ADVERSE_THRESHOLD = -0.03
```

替换 `_iter_scored`(line 211-227)为:

```python
def _iter_scored(universe, pos_of, all_days, days):
    """逐评估日逐 buyable 股 yield record dict(≤T 特征 + T+1 标签)。"""
    for i in days:
        dt = all_days[i]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        for c in buyable:
            bar = pos_of[c][dt]
            sc = bt.score_at(universe[c], bar, AFTER_CLOSE)
            if sc is None:
                continue
            composite = float(sc["composite"])
            if composite != composite:  # NaN: 诚实不预测
                continue
            risk = sc.get("risk")
            risk = float(risk) if risk is not None else None
            nr = bt.next_returns(universe[c], bar)
            if nr is None:
                continue
            lbl = _labels(universe[c], bar)
            if lbl is None:
                continue
            yield {"code": c, "date": dt, "bar": bar, "composite": composite,
                   "risk": risk, "close1": nr["close1"], "lbl": lbl}
```

替换 `_collect_samples`(line 230-238)为:

```python
def _collect_samples(universe, pos_of, all_days, days):
    samples = {"direction": [], "gap": [], "od": [], "trend3": [], "return": [], "risk": []}
    for rec in _iter_scored(universe, pos_of, all_days, days):
        lbl = rec["lbl"]
        samples["direction"].append((rec["composite"], lbl["close1"]))
        samples["gap"].append((rec["composite"], lbl["gap"]))
        samples["od"].append((rec["composite"], lbl["od"]))
        if lbl["trend3"] is not None:
            samples["trend3"].append((rec["composite"], lbl["trend3"]))
        samples["return"].append((rec["composite"], rec["close1"]))
        if rec["risk"] is not None:
            samples["risk"].append((rec["risk"], 1 if rec["close1"] < ADVERSE_THRESHOLD else 0))
    return samples
```

替换 `_fit_all`(line 241-242)为:

```python
def _fit_all(samples):
    cals = {}
    for name in ("direction", "gap", "od", "trend3", "risk"):
        cals[name] = _fit_calibrator(samples[name], N_BINS)
    cals["return"] = _fit_calibrator(samples["return"], N_BINS, monotone=False)
    return cals
```

替换 `_evaluate`(line 303-323)为:

```python
def _evaluate(universe, pos_of, all_days, days, cals):
    val = {"direction": [], "gap": [], "od": [], "trend3": [], "path": [], "return": [], "risk": []}
    records = []
    for rec in _iter_scored(universe, pos_of, all_days, days):
        lbl = rec["lbl"]
        composite = rec["composite"]
        val["direction"].append((composite, lbl["close1"]))
        val["gap"].append((composite, lbl["gap"]))
        val["od"].append((composite, lbl["od"]))
        if lbl["trend3"] is not None:
            val["trend3"].append((composite, lbl["trend3"]))
        val["path"].append((composite, lbl["gap"], lbl["od"]))
        val["return"].append((composite, rec["close1"]))
        if rec["risk"] is not None:
            val["risk"].append((rec["risk"], 1 if rec["close1"] < ADVERSE_THRESHOLD else 0))
        direction, _ = _dir_conf(cals["direction"].p_up(composite))
        records.append({
            "code": rec["code"], "date": rec["date"], "bar": rec["bar"],
            "composite": composite, "risk": rec["risk"], "close1": rec["close1"],
            "direction_label": lbl["close1"], "gap": lbl["gap"], "od": lbl["od"],
            "trend3": lbl["trend3"],
            "expected_return": cals["return"].p_up(composite),
            "risk_p": cals["risk"].p_up(rec["risk"]) if rec["risk"] is not None else None,
            "direction": direction,
        })
    metrics = {name: _metric(name, cals[name], val[name]) for name in ("direction", "gap", "od", "trend3")}
    metrics["return"] = _metric_return(cals["return"], cals["direction"], val["return"])
    metrics["risk"] = _metric_risk(cals["risk"], val["risk"])
    metrics["path"] = _metric_path(cals["gap"], cals["od"], val["path"])
    return metrics, records
```

`run_backtest` 内(约 line 341-342)改 `_evaluate` 解包并挂 valid_records:

```python
    metrics, valid_records = _evaluate(universe, pos_of, all_days, valid_days, cals)
    return {
        "calibrators": cals,
        "n_samples": {name: len(samples[name]) for name in samples},
        "metrics": metrics,
        "valid_records": valid_records,
        "data_range": {"start": str(all_days[0]), "end": str(all_days[-1])},
        ...
    }
```

（保留原 `train_window`/`valid_window`/`n_eval`/`step`/`n_train`/`n_valid` 键不动。）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过。注意 `test_run_backtest_integration` 的 `score_at` 被 monkeypatch 为只有 `composite` 无 `risk`,故 `risk=None` → risk 样本空 → risk 校准器 degraded,`_metric_risk` 在 valid 段 n=0;return 正常。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): 六校准器接入 + run_backtest 返回 valid_records"
```

---

### Task 5: `predict_at` 扩展 expected_return / risk_p

**Files:**
- Modify: `predict.py:182-208`
- Test: `tests/test_predict.py`

**Interfaces:**
- Produces: `predict_at(...)` 返回 dict 增加 `expected_return`(float)与 `risk_p`(float 或 None)。

- [ ] **Step 1: 写失败测试(更新 `_fake_cals` + 断言)**

更新 `tests/test_predict.py::_fake_cals` 与 `test_predict_at_full_schema`:

```python
def _fake_cals():
    up = pr._fit_calibrator([(5.0, 1)] * 10, 2)    # p=1
    down = pr._fit_calibrator([(5.0, 0)] * 10, 2)  # p=0
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)  # mean=0.02
    risk = pr._fit_calibrator([(5.0, 0)] * 10, 2)  # p=0
    return {"direction": up, "gap": down, "od": up, "trend3": up, "return": ret, "risk": risk}


def test_predict_at_full_schema(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4})
    pred = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert pred["code"] == "000001"
    assert pred["date"] == d["date"].iloc[10]
    assert pred["composite"] == pytest.approx(61.4)
    assert pred["expected_return"] == pytest.approx(0.02)
    assert pred["risk_p"] is None  # score_at 未提供 risk → risk_p None
    t1 = pred["T+1"]
    assert t1["direction"] == "up"
    assert t1["confidence"] == pytest.approx(1.0)
    assert t1["gap"] == "low"
    assert t1["od"] == "up"
    assert t1["path"] == "低开高走"
    assert pred["T+3"]["direction"] == "up"
    assert pred["T+3"]["confidence"] == pytest.approx(1.0)


def test_predict_at_risk_p_present(monkeypatch):
    d = make_daily([10.0] * 70)
    monkeypatch.setattr(bt, "score_at", lambda d, i, now: {"composite": 61.4, "risk": 30.0})
    pred = pr.predict_at(d, 10, pr.AFTER_CLOSE, _fake_cals())
    assert pred["risk_p"] == pytest.approx(0.0)  # _fake_cals risk 单箱 p=0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_predict_at_full_schema -v`
Expected: FAIL — `pred["expected_return"]` KeyError(且 `_fake_cals` 缺 return/risk → `cals["return"]` KeyError 会先抛)。

- [ ] **Step 3: 实现**

`predict_at` 中在 `sc = bt.score_at(...)` 之后加 `risk`,并在返回 dict 增两字段:

```python
def predict_at(d, i, now, cals):
    """单股 ≤T → 结构化预测 dict;composite 不可得返回 None。"""
    sc = bt.score_at(d, i, now)
    if sc is None:
        return None
    composite = float(sc["composite"])
    risk = sc.get("risk")
    risk = float(risk) if risk is not None else None
    p_dir = cals["direction"].p_up(composite)
    p_gap = cals["gap"].p_up(composite)
    p_od = cals["od"].p_up(composite)
    p_t3 = cals["trend3"].p_up(composite)
    expected_return = cals["return"].p_up(composite)
    risk_p = cals["risk"].p_up(risk) if risk is not None else None
    direction, confidence = _dir_conf(p_dir)
    gap_dir = _gap_dir(p_gap)
    od_dir, _ = _dir_conf(p_od)
    t3_dir, t3_conf = _dir_conf(p_t3)
    return {
        "code": str(d["code"].iloc[i]),
        "date": str(d["date"].iloc[i]),
        "composite": composite,
        "expected_return": expected_return,
        "risk_p": risk_p,
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

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过(`test_predict_at_no_future_leak`、`test_predict_at_t3_no_future_bars` 等因 `_fake_cals` 已补 return/risk 而继续通过)。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): predict_at 输出 expected_return 与 risk_p"
```

---

### Task 6: `build_report` / `render_markdown` 支持六维 + 版本 1.1.0

**Files:**
- Modify: `predict.py:20`(MODULE_VERSION)、`predict.py:393-422`(build_report)、`predict.py:425-455`(render_markdown)
- Test: `tests/test_predict.py`

**Interfaces:**
- Produces: `build_report(results)` 对 `metrics["return"]`/`metrics["risk"]` 序列化;`render_markdown(payload)` 增加 return/risk 表格行。

- [ ] **Step 1: 写失败测试**

```python
def test_build_report_serializes_return_risk():
    cal = pr._fit_calibrator([(5.0, 1)] * 10, 2)
    ret = pr._fit_calibrator([(5.0, 0.02)] * 10, 2, monotone=False)
    results = {
        "calibrators": {"direction": cal, "return": ret},
        "n_samples": {"direction": 10, "return": 10},
        "metrics": {
            "direction": {"n": 10, "base_rate": 0.5, "hit_rate": 0.6, "ece": 0.1, "brier": 0.2, "n_hold": 1},
            "return": {"n": 3, "mae": 0.02, "mae_std": 0.01, "rmse": 0.03, "rmse_std": 0.01,
                       "sign_agreement": 0.5, "sign_n": 2, "mean_residual": -0.01,
                       "residual_std": 0.02,
                       "dir_cond_mae": {"up": {"mae": 0.02, "n": 3},
                                        "down": {"mae": None, "n": 0},
                                        "hold": {"mae": None, "n": 0}}},
            "risk": {"n": 4, "adverse_rate": 0.25, "ece": 0.01, "brier": 0.05, "lift": 0.3},
            "path": {"n": 8, "acc_path": 0.5},
        },
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "train_window": {"start": "2026-01-01", "end": "2026-06-01"},
        "valid_window": {"start": "2026-06-02", "end": "2026-08-13"},
        "n_eval": 300, "step": 3, "n_train": 240, "n_valid": 60,
    }
    payload = pr.build_report(results)
    assert payload["metrics"]["return"]["mae"] == pytest.approx(0.02)
    assert payload["metrics"]["return"]["dir_cond_mae"]["up"]["n"] == 3
    assert payload["metrics"]["risk"]["lift"] == pytest.approx(0.3)
    assert payload["module_version"] == "1.1.0"
    md = pr.render_markdown(payload)
    assert "return" in md and "risk" in md
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_predict.py::test_build_report_serializes_return_risk -v`
Expected: FAIL — `build_report` 对 return 走 `_num(m["base_rate"])` 分支 → KeyError,或 module_version 仍 "1.0.0"。

- [ ] **Step 3: 实现**

`predict.py:20` 改:

```python
MODULE_VERSION = "1.1.0"
```

`build_report` 的 metrics 循环改为显式分派:

```python
    metrics = {}
    for name, m in results["metrics"].items():
        if name == "path":
            metrics[name] = {"n": m["n"], "acc_path": _num(m["acc_path"])}
        elif name == "return":
            metrics[name] = {
                "n": m["n"], "mae": _num(m["mae"]), "rmse": _num(m["rmse"]),
                "sign_agreement": _num(m["sign_agreement"]), "sign_n": m["sign_n"],
                "mean_residual": _num(m["mean_residual"]),
                "dir_cond_mae": {k: {"mae": _num(v["mae"]), "n": v["n"]}
                                 for k, v in m["dir_cond_mae"].items()},
            }
        elif name == "risk":
            metrics[name] = {
                "n": m["n"], "adverse_rate": _num(m["adverse_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "lift": _num(m["lift"]),
            }
        else:
            metrics[name] = {
                "n": m["n"], "base_rate": _num(m["base_rate"]), "hit_rate": _num(m["hit_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "n_hold": m["n_hold"],
            }
```

`render_markdown` 的指标表循环改为:

```python
    for name, m in payload["metrics"].items():
        if name == "path":
            L.append(f"| path | {m['n']} | - | acc_path={fmt(m['acc_path'])} | - | - | - |")
        elif name == "return":
            L.append(f"| return | {m['n']} | - | mae={fmt(m['mae'])} | rmse={fmt(m['rmse'])} "
                     f"| sign={fmt(m['sign_agreement'])} (n={m['sign_n']}) | resid={fmt(m['mean_residual'])} |")
            L.append(f"|  dir_cond_mae | up {fmt(m['dir_cond_mae']['up']['mae'])}/{m['dir_cond_mae']['up']['n']} "
                     f"| down {fmt(m['dir_cond_mae']['down']['mae'])}/{m['dir_cond_mae']['down']['n']} "
                     f"| hold {fmt(m['dir_cond_mae']['hold']['mae'])}/{m['dir_cond_mae']['hold']['n']} | - | - | - |")
        elif name == "risk":
            L.append(f"| risk | {m['n']} | adverse_rate={fmt(m['adverse_rate'])} "
                     f"| ece={fmt(m['ece'])} | brier={fmt(m['brier'])} | lift={fmt(m['lift'])} | - |")
        else:
            L.append(f"| {name} | {m['n']} | {fmt(m['base_rate'])} | {fmt(m['hit_rate'])} "
                     f"| {fmt(m['ece'])} | {fmt(m['brier'])} | {m['n_hold']} |")
```

（表头不变;return/risk 行的列语义已在单元格内自标注,不破坏六列表结构。）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_predict.py -q`
Expected: 全通过。

- [ ] **Step 5: 提交**

```bash
git add predict.py tests/test_predict.py
git commit -m "feat(predict): build_report/render_markdown 支持 return+risk;版本 1.1.0"
```

---

### Task 7: `evaluate.py` 八分层谓词

**Files:**
- Create: `evaluate.py`
- Test: `tests/test_evaluate.py`(新建)

**Interfaces:**
- Produces(纯函数,供 Task 8 复用):
  - `_amount_top(universe, pos_of, all_days, i, buyable) -> set[str]`
  - `_hot_members(sector_members, heat) -> set[str]`
  - `_leaders(sector_map, buyable, composite_of) -> set[str]`(`composite_of: {code: composite}`)
  - `_layer_trend(d, bar) -> bool`
  - `_layer_high(d, bar) -> bool`
  - `_layer_oversold(d, bar) -> bool`
  - `_layer_rebound(d, bar) -> bool`
  - `_layer_oscillation(d, bar) -> bool`
  - 模块常量 `TOP_SECTORS=3, AMOUNT_TOP_FRAC=0.20, HIGH_POS_RATIO=0.97, OVERSOLD_RET=-0.15, AMP_THRESHOLD=15.0, MIN_LAYER_N=30, LAYERS=(...)`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_evaluate.py`(构造含 `amount` 列的合成日线,复用一个本地 `mk` 助手):

```python
# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import pytest

import evaluate as ev


def mk(closes, opens=None, highs=None, lows=None, amounts=None, code="000001"):
    n = len(closes)
    opens = [float(o) for o in opens] if opens is not None else [float(c) for c in closes]
    highs = [float(h) for h in highs] if highs is not None else [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [float(l) for l in lows] if lows is not None else [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    amounts = [float(a) for a in amounts] if amounts is not None else [1e9] * n
    dates = pd.date_range("2026-01-01", periods=n, freq="D").strftime("%Y-%m-%d").tolist()
    closes = [float(c) for c in closes]
    # change_pct 由 close 计算(与 bt.build_universe 同口径);bt.build_buyable 读该列
    change_pct = (pd.Series(closes).pct_change().fillna(0.0) * 100.0).tolist()
    return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": [1e6] * n,
                         "amount": amounts, "code": [code] * n, "change_pct": change_pct})


def test_amount_top_20pct_and_nan():
    # 5 只股,amount 10/8/6/4/NaN → top20% = max(1, int(4*0.2))=1 → 只有 amount=10 那只
    d = mk([10.0] * 100)
    universe = {"a": mk([10.0] * 100, amounts=[10.0] * 100, code="a"),
                "b": mk([10.0] * 100, amounts=[8.0] * 100, code="b"),
                "c": mk([10.0] * 100, amounts=[6.0] * 100, code="c"),
                "d": mk([10.0] * 100, amounts=[4.0] * 100, code="d"),
                "e": mk([10.0] * 100, amounts=[float("nan")] * 100, code="e")}
    all_days = sorted(set().union(*[set(v["date"]) for v in universe.values()]))
    pos_of = {c: {dt: i for i, dt in enumerate(v["date"])} for c, v in universe.items()}
    top = ev._amount_top(universe, pos_of, all_days, 99, {"a", "b", "c", "d", "e"})
    assert top == {"a"}


def test_hot_members_top3():
    sm = {"s1": ["a", "b"], "s2": ["c"], "s3": ["d"], "s4": ["e"]}
    heat = {"s1": 0.05, "s2": 0.03, "s3": 0.01, "s4": -0.02}
    assert ev._hot_members(sm, heat) == {"a", "b", "c", "d"}


def test_leaders_tie_smallest_code():
    # sector_map 为 code→sectors 映射(与 bt.load_sector_map 输出同向)
    sector_map = {"a": ["s1"], "b": ["s1"], "c": ["s2"]}
    comp = {"a": 70.0, "b": 70.0, "c": 50.0}
    # s1 内 a/b 并列 70 → 取 code 小者 a;c 是 s2 唯一成员 → 龙头
    assert ev._leaders(sector_map, {"a", "b", "c"}, comp) == {"a", "c"}


def test_layer_trend():
    d = mk(list(range(100, 200)))  # 单调涨 → MA5>MA20>MA60
    assert ev._layer_trend(d, 99)


def test_layer_high():
    closes = [10.0] * 100
    closes[99] = 15.0
    d = mk(closes)
    assert ev._layer_high(d, 99)
    closes[99] = 9.0
    assert not ev._layer_high(mk(closes), 99)


def test_layer_oversold_rebound_oscillation():
    closes = [100.0] * 100
    closes[80] = 60.0   # ret20 at bar=99 → close[99]/close[79]-1
    closes[99] = 62.0
    d = mk(closes)
    # ret20(99) = 62/100 - 1 = -0.38 < -0.15 → 超跌
    assert ev._layer_oversold(d, 99)
    # 反抽:ret20(98)=close[98]/close[78]-1=100/100-1=0 不< -0.15 → False
    assert not ev._layer_rebound(d, 99)
    # 震荡:构造窄幅
    narrow = mk([100.0 + 0.01 * i for i in range(100)],
                highs=[100.0 + 0.02 * i for i in range(100)],
                lows=[100.0 + 0.005 * i for i in range(100)])
    assert ev._layer_oscillation(narrow, 99)


def test_layer_high_boundary_097():
    # max(close[40..99])=100;close[99]=97.0 恰 =0.97*100 → True;96.9 → False
    closes = [100.0] * 100
    closes[99] = 97.0
    assert ev._layer_high(mk(closes), 99)
    closes[99] = 96.9
    assert not ev._layer_high(mk(closes), 99)


def test_layer_oversold_boundary():
    # ret20(99)=close[99]/close[79]-1;close[79]=100。85/100-1=-0.15 不满足(< 严格);84.9 → True
    closes = [100.0] * 100
    closes[99] = 85.0
    assert not ev._layer_oversold(mk(closes), 99)
    closes[99] = 84.9
    assert ev._layer_oversold(mk(closes), 99)


def test_layer_oscillation_boundary_15():
    # amp=(max(high)-min(low))/close*100;窗口 80..99。恰 15 → False;14.9 → True
    closes = [100.0] * 100
    assert not ev._layer_oscillation(mk(closes, highs=[115.0] * 100, lows=[100.0] * 100), 99)
    assert ev._layer_oscillation(mk(closes, highs=[114.9] * 100, lows=[100.0] * 100), 99)


def test_layer_rebound_true_tm1():
    # ret20(T-1)=close[98]/close[78]-1=60/100-1=-0.4<-0.15;close[99]=62>close[98]=60 → True
    closes = [100.0] * 100
    closes[98] = 60.0
    closes[99] = 62.0
    assert ev._layer_rebound(mk(closes), 99)
    closes[99] = 60.0  # 不高于 T-1 → False
    assert not ev._layer_rebound(mk(closes), 99)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_evaluate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evaluate'`。

- [ ] **Step 3: 实现**

新建 `evaluate.py`(本 Task 只放常量 + 8 谓词;`run`/`build_report` 等留 Task 8/9):

```python
# -*- coding: utf-8 -*-
"""评分系统分层报告:只读预测引擎输出 + 自建 universe,打八层标签逐维报准确率。

只读 _analysis/daily/*.pkl 与 _analysis/code2sector.json(GBK),不 fetch。
"""
import analysis as an
import backtest as bt
import predict as pr

MODULE_VERSION = "1.0.0"
TOP_SECTORS = 3
AMOUNT_TOP_FRAC = 0.20
HIGH_POS_RATIO = 0.97
OVERSOLD_RET = -0.15
AMP_THRESHOLD = 15.0
MIN_LAYER_N = 30
LAYERS = ("大盘", "热门", "龙头", "趋势", "高位", "超跌", "反抽", "震荡")


def _amount_top(universe, pos_of, all_days, i, buyable):
    """当日 buyable 中 amount 前 top20%(NaN 排名前 dropna,不参与也不计分母)。"""
    dt = all_days[i]
    pairs = []
    for c in buyable:
        bar = pos_of[c].get(dt)
        if bar is None:
            continue
        amt = float(universe[c]["amount"].iloc[bar])
        if amt != amt:
            continue
        pairs.append((c, amt))
    if not pairs:
        return set()
    pairs.sort(key=lambda x: -x[1])
    k = max(1, int(len(pairs) * AMOUNT_TOP_FRAC))
    return set(c for c, _ in pairs[:k])


def _hot_members(sector_members, heat):
    """中位数 5 日涨幅 top3 热板块的成员并集。"""
    ranked = sorted(heat, key=heat.get, reverse=True)
    out = set()
    for s in ranked[:TOP_SECTORS]:
        out.update(sector_members.get(s, []))
    return out


def _leaders(sector_map, buyable, composite_of):
    """每板块内 composite 最高的 buyable 成分 top1;并列取 code 最小。"""
    by_sector = {}
    for c in buyable:
        for s in sector_map.get(c, []):
            by_sector.setdefault(s, []).append(c)
    leaders = set()
    for members in by_sector.values():
        best_c, best_comp = None, None
        for c in members:
            comp = composite_of.get(c)
            if comp is None:
                continue
            if best_comp is None or comp > best_comp or (comp == best_comp and c < best_c):
                best_c, best_comp = c, comp
        if best_c is not None:
            leaders.add(best_c)
    return leaders


def _layer_trend(d, bar):
    df = an.add_ma(d.iloc[:bar + 1], (5, 20, 60))
    m5, m20, m60 = df["ma5"].iloc[-1], df["ma20"].iloc[-1], df["ma60"].iloc[-1]
    if any(x != x for x in (m5, m20, m60)):
        return False
    return m5 > m20 > m60


def _layer_high(d, bar):
    closes = d["close"].iloc[max(0, bar - 59): bar + 1]
    if len(closes) == 0:
        return False
    return float(d["close"].iloc[bar]) >= HIGH_POS_RATIO * float(closes.max())


def _layer_oversold(d, bar):
    if bar - 20 < 0:
        return False
    return float(d["close"].iloc[bar]) / float(d["close"].iloc[bar - 20]) - 1 < OVERSOLD_RET


def _layer_rebound(d, bar):
    if bar - 21 < 0:
        return False
    ret20_tm1 = float(d["close"].iloc[bar - 1]) / float(d["close"].iloc[bar - 21]) - 1
    return ret20_tm1 < OVERSOLD_RET and float(d["close"].iloc[bar]) > float(d["close"].iloc[bar - 1])


def _layer_oscillation(d, bar):
    window = d.iloc[max(0, bar - 19): bar + 1]
    close = float(d["close"].iloc[bar])
    if close <= 0:
        return False
    amp = (float(window["high"].max()) - float(window["low"].min())) / close * 100.0
    return amp < AMP_THRESHOLD
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_evaluate.py -v`
Expected: 全通过(8 组测试)。

- [ ] **Step 5: 提交**

```bash
git add evaluate.py tests/test_evaluate.py
git commit -m "feat(evaluate): 八分层谓词(大盘/热门/龙头/趋势/高位/超跌/反抽/震荡)"
```

---

### Task 8: `evaluate.py` 核心 `evaluate()`(打标签 + 逐层指标 + ±2σ 判据)

**Files:**
- Modify: `evaluate.py`
- Test: `tests/test_evaluate.py`

**Interfaces:**
- Consumes: Task 7 谓词 + `predict._metric`/`_metric_return`/`_metric_risk`/`_metric_path`/`_dir_conf` + `bt.build_buyable`/`bt.sector_heat`。
- Produces: 模块常量 `DIMS=(方向/开盘/路径/趋势/涨跌幅/风险)`、`PRIMARY`(各维主指标);`evaluate(records, universe, pos_of, all_days, sector_members, sector_map, cals) -> dict`,返回 `{overall, layers, layer_n, significant, se_bounds, all_undifferentiated}`。`significant = [(layer, dim, metric, layer_val, overall_val, se), ...]`;`se_bounds = {(layer, dim): 2*se}`(主指标噪声界);`all_undifferentiated` 为 `len(significant)==0`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_evaluate.py` 追加(构造合成 records + universe,直接测 `evaluate` 核心):

```python
def _mk_records(n=200):
    # 全部落在「趋势」层:单调涨;composite 恒定 50,close1 交替 ±
    recs = []
    for k in range(n):
        recs.append({"code": "000001", "date": "2026-04-01", "bar": 99,
                     "composite": 50.0, "risk": 10.0,
                     "close1": 0.01 if k % 2 == 0 else -0.01,
                     "direction_label": 1 if k % 2 == 0 else 0,
                     "gap": 1, "od": 1, "trend3": 1})
    return recs


def test_evaluate_cores_and_undifferentiated_flag():
    import backtest as bt
    import predict as pr
    d = mk(list(range(100, 200)))  # 单调涨 → 趋势层 True
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
    assert set(rep["layers"]) == set(ev.LAYERS)
    assert rep["layer_n"]["趋势"] == 200
    assert "direction" in rep["overall"] and "return" in rep["overall"] and "risk" in rep["overall"]
    # 大盘/趋势/高位/震荡四层均含全部 200 记录(单调涨 + 窄幅),与总体逐样本一致
    # → 各层指标 == 总体指标,|差| 恒 0,永不超过 2σ → 无显著分化
    assert rep["all_undifferentiated"] is True
    assert rep["significant"] == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_evaluate.py::test_evaluate_cores_and_undifferentiated_flag -v`
Expected: FAIL — `AttributeError: module 'evaluate' has no attribute 'evaluate'`。

- [ ] **Step 3: 实现**

`evaluate.py` 追加(常量后):

```python
DIMS = ("direction", "gap", "path", "trend3", "return", "risk")
PRIMARY = {"direction": "hit_rate", "gap": "hit_rate", "path": "acc_path",
           "trend3": "hit_rate", "return": "sign_agreement", "risk": "adverse_rate"}


def _binom_diff_se(p_l, n_l, p_o, n_o):
    if p_l is None or p_o is None or not n_l or not n_o:
        return None
    sl = (p_l * (1.0 - p_l) / n_l) ** 0.5
    so = (p_o * (1.0 - p_o) / n_o) ** 0.5
    return (sl * sl + so * so) ** 0.5


def _mean_diff_se(std_l, n_l, std_o, n_o):
    if std_l is None or std_o is None or not n_l or not n_o:
        return None
    return ((std_l * std_l) / n_l + (std_o * std_o) / n_o) ** 0.5


def _dim_metrics(cals, dim, records):
    """对一组记录算某维指标,复用 predict 的指标函数(单一真相源)。"""
    if dim == "path":
        samples = [(r["composite"], r["gap"], r["od"]) for r in records]
        return pr._metric_path(cals["gap"], cals["od"], samples)
    if dim == "return":
        samples = [(r["composite"], r["close1"]) for r in records]
        return pr._metric_return(cals["return"], cals["direction"], samples)
    if dim == "risk":
        samples = [(r["risk"], 1 if r["close1"] < pr.ADVERSE_THRESHOLD else 0)
                   for r in records if r["risk"] is not None]
        return pr._metric_risk(cals["risk"], samples)
    samples = []
    for r in records:
        if dim == "trend3" and r["trend3"] is None:
            continue
        label = r["direction_label"] if dim == "direction" else r[dim]
        samples.append((r["composite"], label))
    return pr._metric(dim, cals[dim], samples)


def _significance(overall, per_layer):
    """逐 (层, 维, 指标) 做 ±2σ 显著性;n<30 或值 None 跳过。
    返回 (significant, se_bounds, all_undifferentiated);se_bounds[(L, dim)] = 2*se(主指标)。
    """
    significant = []
    se_bounds = {}
    for dim, metric, kind in (
        ("direction", "hit_rate", "binom"),
        ("gap", "hit_rate", "binom"),
        ("path", "acc_path", "binom"),
        ("trend3", "hit_rate", "binom"),
        ("return", "sign_agreement", "binom"),
        ("return", "mae", "mean"),
        ("return", "rmse", "mean"),
        ("return", "mean_residual", "mean"),
        ("risk", "adverse_rate", "binom"),
    ):
        o = overall[dim]
        for L in LAYERS:
            m = per_layer[L][dim]
            if kind == "binom":
                if metric == "hit_rate":
                    n_l, n_o = m["n"] - m["n_hold"], o["n"] - o["n_hold"]
                elif metric == "sign_agreement":
                    n_l, n_o = m["sign_n"], o["sign_n"]
                else:  # adverse_rate / acc_path
                    n_l, n_o = m["n"], o["n"]
                v_l, v_o = m[metric], o[metric]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _binom_diff_se(v_l, n_l, v_o, n_o)
            else:  # mean
                std_key = {"mae": "mae_std", "rmse": "rmse_std",
                           "mean_residual": "residual_std"}[metric]
                v_l, v_o = m[metric], o[metric]
                n_l, n_o = m["n"], o["n"]
                if v_l is None or v_o is None or n_l < MIN_LAYER_N or n_o <= 0:
                    continue
                se = _mean_diff_se(m[std_key], n_l, o[std_key], n_o)
            if se is None:
                continue
            if metric == PRIMARY[dim]:
                se_bounds[(L, dim)] = 2.0 * se
            if abs(v_l - v_o) > 2.0 * se:
                significant.append((L, dim, metric, v_l, v_o, se))
    return significant, se_bounds, (len(significant) == 0)


def evaluate(records, universe, pos_of, all_days, sector_members, sector_map, cals):
    day_to_i = {dt: i for i, dt in enumerate(all_days)}
    by_date = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)

    layer_records = {L: [] for L in LAYERS}
    for date, recs in by_date.items():
        i = day_to_i[date]
        buyable, _, _, _ = bt.build_buyable(universe, pos_of, all_days, i)
        heat = bt.sector_heat(universe, sector_members, pos_of, all_days, i)
        amount_top = _amount_top(universe, pos_of, all_days, i, buyable)
        hot_members = _hot_members(sector_members, heat)
        composite_of = {r["code"]: r["composite"] for r in recs}
        leaders = _leaders(sector_map, buyable, composite_of)
        for r in recs:
            c, bar = r["code"], r["bar"]
            d = universe[c]
            tags = {
                "大盘": c in amount_top,
                "热门": c in hot_members,
                "龙头": c in leaders,
                "趋势": _layer_trend(d, bar),
                "高位": _layer_high(d, bar),
                "超跌": _layer_oversold(d, bar),
                "反抽": _layer_rebound(d, bar),
                "震荡": _layer_oscillation(d, bar),
            }
            for L, ok in tags.items():
                if ok:
                    layer_records[L].append(r)

    overall = {dim: _dim_metrics(cals, dim, records) for dim in DIMS}
    per_layer = {L: {dim: _dim_metrics(cals, dim, layer_records[L]) for dim in DIMS}
                 for L in LAYERS}
    significant, se_bounds, all_undifferentiated = _significance(overall, per_layer)
    return {"overall": overall, "layers": per_layer,
            "layer_n": {L: len(layer_records[L]) for L in LAYERS},
            "significant": significant, "se_bounds": se_bounds,
            "all_undifferentiated": all_undifferentiated}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_evaluate.py -v`
Expected: 全通过。`bt.sector_heat` 对空 `sector_members` 返回 `{}`,`_hot_members`/`_leaders` 空集;大盘/趋势/高位/震荡四层各 n=200(其余 0),填充层记录与总体逐样本一致 → 无显著分化,断言成立。

- [ ] **Step 5: 提交**

```bash
git add evaluate.py tests/test_evaluate.py
git commit -m "feat(evaluate): evaluate() 核心打标签 + 逐层指标 + ±2σ 无分化判据"
```

---

### Task 9: `evaluate.py` 的 `run` / `build_report` / `render_markdown` / `main`

**Files:**
- Modify: `evaluate.py`
- Test: `tests/test_evaluate.py`

**Interfaces:**
- Consumes: Task 8 的 `evaluate()`;`predict.run_backtest`、`bt.load_sector_map`/`bt.build_universe`/`bt.build_calendar`/`bt.build_sector_members`。
- Produces:
  - `run(data_dir, sector_map_path) -> dict`(先 `predict.run_backtest` 取 cals + valid_records + 数据窗口,再自建 universe 调 `evaluate`)。
  - `build_report(report) -> dict`(JSON-safe;n<30 的层指标值置 None 只留 n)。
  - `render_markdown(payload) -> str`(六维 × 八层表 + 无分化标注 + 4 条诚实声明)。
  - `main(argv)`(CLI `--data-dir/--sector-map/--out`,默认写 `evaluate_report.json` + `.md`)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_evaluate.py` 追加:

```python
def _fake_run_backtest():
    import predict as pr
    return {
        "calibrators": {"direction": pr._fit_calibrator([(50.0, 0)] * 10 + [(50.0, 1)] * 10, 2),
                        "gap": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "od": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "trend3": pr._fit_calibrator([(50.0, 1)] * 10, 2),
                        "return": pr._fit_calibrator([(50.0, 0.0)] * 10, 2, monotone=False),
                        "risk": pr._fit_calibrator([(10.0, 0)] * 10, 2)},
        "valid_records": _mk_records(),
        "metrics": {},
        "data_range": {"start": "2026-01-01", "end": "2026-08-13"},
        "valid_window": {"start": "2026-01-01", "end": "2026-08-13"},
    }


def test_run_build_report_and_main(tmp_path, monkeypatch):
    import backtest as bt
    import predict as pr
    d = mk(list(range(100, 200)))
    monkeypatch.setattr(pr, "run_backtest", lambda dd, sm: _fake_run_backtest())
    monkeypatch.setattr(bt, "load_sector_map", lambda p: {"000001": []})
    monkeypatch.setattr(bt, "build_universe", lambda dd, sm: ({"000001": d}, ["000001"]))
    monkeypatch.setattr(bt, "build_calendar",
                        lambda univ, codes: (list(d["date"]),
                                             {"000001": {dt: i for i, dt in enumerate(d["date"])}}))
    monkeypatch.setattr(bt, "build_sector_members", lambda sm, univ: {})
    out = tmp_path / "ev.json"
    rc = ev.main(["--data-dir", str(tmp_path), "--sector-map", "x", "--out", str(out)])
    assert rc == 0
    payload = ev.json_load(out)
    assert payload["module_version"] == "1.0.0"
    assert "overall" in payload and "layers" in payload
    assert out.with_suffix(".md").exists()
```

（`ev.json_load` 是 Task 9 实现里的一个 JSON 读助手;测试直接调它来读回。）

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_evaluate.py::test_run_build_report_and_main -v`
Expected: FAIL — `AttributeError: module 'evaluate' has no attribute 'main'`。

- [ ] **Step 3: 实现**

`evaluate.py` 追加(顶部 import 补 `argparse/json/os/sys/datetime`,并加 `import numpy as np`):

```python
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
```

追加函数:

```python
def run(data_dir, sector_map_path):
    results = pr.run_backtest(data_dir, sector_map_path)
    cals = results["calibrators"]
    valid_records = results["valid_records"]
    sector_map = bt.load_sector_map(sector_map_path)
    universe, codes = bt.build_universe(data_dir, sector_map)
    if not universe:
        raise RuntimeError(f"no usable daily pkl in {data_dir}")
    for code, df in universe.items():
        if "amount" not in df.columns:
            raise RuntimeError(
                f"pkl {code} 缺 amount 列(实际 {list(df.columns)});请重拉 _analysis/daily 为 9 列")
    all_days, pos_of = bt.build_calendar(universe, codes)
    sector_members = bt.build_sector_members(sector_map, universe)
    report = evaluate(valid_records, universe, pos_of, all_days, sector_members, sector_map, cals)
    report["data_range"] = results["data_range"]
    report["valid_window"] = results["valid_window"]
    report["n_valid_records"] = len(valid_records)
    return report


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def build_report(report):
    def dim_payload(m):
        if "acc_path" in m:  # path
            return {"n": m["n"], "acc_path": _num(m["acc_path"])}
        if "hit_rate" in m:  # direction/gap/trend3
            return {"n": m["n"], "hit_rate": _num(m["hit_rate"]),
                    "base_rate": _num(m["base_rate"]), "n_hold": m["n_hold"]}
        if "mae" in m:  # return
            return {"n": m["n"], "mae": _num(m["mae"]), "rmse": _num(m["rmse"]),
                    "sign_agreement": _num(m["sign_agreement"]), "sign_n": m["sign_n"],
                    "mean_residual": _num(m["mean_residual"]),
                    "dir_cond_mae": {k: {"mae": _num(v["mae"]), "n": v["n"]}
                                     for k, v in m["dir_cond_mae"].items()}}
        return {"n": m["n"], "adverse_rate": _num(m["adverse_rate"]),
                "ece": _num(m["ece"]), "brier": _num(m["brier"]), "lift": _num(m["lift"])}

    def layer_dim_payload(m):
        p = dim_payload(m)
        if m["n"] < MIN_LAYER_N:  # §5 薄层小 n:只报样本量
            return {"n": m["n"], "_suppressed": True}
        return p

    overall = {dim: dim_payload(report["overall"][dim]) for dim in DIMS}
    layers = {L: {dim: layer_dim_payload(report["layers"][L][dim]) for dim in DIMS}
              for L in LAYERS}
    return {
        "system_version": pr._git_short_sha(),
        "module_version": MODULE_VERSION,
        "generated_at": datetime.now().isoformat(),
        "mode": "evaluate",
        "data_range": report["data_range"],
        "valid_window": report["valid_window"],
        "n_valid_records": report["n_valid_records"],
        "overall": overall,
        "layers": layers,
        "layer_n": report["layer_n"],
        "significant": [list(s) for s in report["significant"]],
        "se_bounds": [[L, dim, se] for (L, dim), se in sorted(report["se_bounds"].items())],
        "all_undifferentiated": report["all_undifferentiated"],
    }


def render_markdown(payload):
    def fmt(x, nd=4):
        return "-" if x is None else f"{x:.{nd}f}"

    L = ["# 评分系统分层报告(六维 × 八层)", ""]
    L.append(f"- module_version: {payload['module_version']}")
    L.append(f"- system_version: `{payload['system_version']}`")
    L.append(f"- valid_window: {payload['valid_window']['start']} -> {payload['valid_window']['end']}")
    L.append(f"- n_valid_records: {payload['n_valid_records']}")
    L += ["", "## 总体(valid 段,out-of-sample)", "", "| 维度 | n | 主指标 |", "|---|---|---|"]
    for dim in DIMS:
        o = payload["overall"][dim]
        if dim == "path":
            L.append(f"| path | {o['n']} | acc_path={fmt(o['acc_path'])} |")
        elif dim in ("direction", "gap", "trend3"):
            L.append(f"| {dim} | {o['n']} | hit_rate={fmt(o['hit_rate'])} (base={fmt(o['base_rate'])}) |")
        elif dim == "return":
            L.append(f"| return | {o['n']} | mae={fmt(o['mae'])} rmse={fmt(o['rmse'])} "
                     f"sign={fmt(o['sign_agreement'])} resid={fmt(o['mean_residual'])} |")
        else:
            L.append(f"| risk | {o['n']} | adverse_rate={fmt(o['adverse_rate'])} "
                     f"ece={fmt(o['ece'])} lift={fmt(o['lift'])} |")
    se_map = {(L, dim): se for L, dim, se in payload["se_bounds"]}
    L += ["", "## 分层 × 维度(层值 vs 总体;n<30 只报样本量)", "", "| 层 | 维度 | n | 层值 | 总体值 | 2σ界 | 显著 |", "|---|---|---|---|---|---|---|"]
    for Ln in LAYERS:
        for dim in DIMS:
            cell = payload["layers"][Ln][dim]
            if cell.get("_suppressed"):
                L.append(f"| {Ln} | {dim} | {cell['n']} | (n<30) | - | - | - |")
                continue
            o = payload["overall"][dim]
            lv = cell.get(PRIMARY[dim])
            ov = o.get(PRIMARY[dim])
            bound = se_map.get((Ln, dim))
            sig = any(s[0] == Ln and s[1] == dim for s in payload["significant"])
            L.append(f"| {Ln} | {dim} | {cell['n']} | {fmt(lv)} | {fmt(ov)} "
                     f"| {fmt(bound)} | {'是' if sig else '否'} |")
    L += ["", "## 无分化判定", ""]
    if payload["all_undifferentiated"]:
        L.append("**八层无显著分化**:所有层与总体差异均在 ±2σ 噪声界内 → 触发分层定义重审(合并/换规则)。")
    else:
        L.append("存在显著分化层(见上表「显著」列),分层有区分度。")
    L += ["", "## 诚实声明", ""]
    L.append("1. expected_return 是 composite 分箱的阶梯函数(全宇宙当天 <=10 个取值),不是个股级回归预测。")
    L.append("2. expected_return 与 risk_p 分别是 composite/risk 的单特征边际,不是联合条件。")
    L.append("3. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数。")
    L.append("4. 大盘层用原始成交额(不复权)横截面排名;amount 取自 stock_zh_a_daily(adjust=qfq) 的 amount 列。")
    L.append("")
    return "\n".join(L)


def json_load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description="评分系统分层报告(六维 × 八层)")
    parser.add_argument("--data-dir", default="_analysis/daily")
    parser.add_argument("--sector-map", default="_analysis/code2sector.json")
    parser.add_argument("--out", default="evaluate_report.json")
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

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_evaluate.py -q`
Expected: 全通过。

- [ ] **Step 5: 提交**

```bash
git add evaluate.py tests/test_evaluate.py
git commit -m "feat(evaluate): run/build_report/render_markdown/main + 六维×八层报告"
```

---

## 最终收尾(全量测试 + 真实缓存跑一遍)

在最后一个 Task 提交后,执行(本计划收尾,非独立 Task):

```bash
pytest -q
```

Expected: 全量通过(既有 199 + 本计划新增,无回归)。

真实缓存冒烟(在真实 `_analysis/daily` 上跑一次,确认不崩、报告落盘):

```bash
python evaluate.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out _analysis/evaluate_report.json
```

Expected: `wrote _analysis/evaluate_report.json and _analysis/evaluate_report.md`。注意 `_analysis/` gitignored,产物不入库;若某 pkl 缺 amount **列**(老 6 列缓存),`run()` 会抛「请重拉为 9 列」而非静默回退;若 amount 列存在但值全 NaN,大盘层 n=0,报告如实标注。

## 自审(Self-Review)

- **Spec 覆盖**:§2 数据契约(大盘 amount dropna + 9 列断言)→ Task 7 `_amount_top` + Task 9 `run` 断言;§3.1 return 无 PAV → Task 1 `monotone=False` + Task 4 `_fit_all`;§3.2 risk PAV → Task 4;§3.3 predict_at 扩展 → Task 5;§3.4 run_backtest 六指标 → Task 4;§4 六维公式(含 path 四分类)→ Task 2(`_metric_return`/`_metric_path`)/3(`_metric_risk`);§5 八层 → Task 7/8;§6 模块边界(指标只在 predict)→ Task 8 `_dim_metrics`;§7 诚实声明 → Task 9 `render_markdown`;§8 版本 → Task 6(1.1.0)+ Task 9(1.0.0);§9 测试(边界 + 无兜底)→ Task 7/4;§10 ±2σ 无分化(含 se_bounds 噪声界渲染)→ Task 8/9。
- **占位符**:无 TBD/TODO;每步含实际代码。
- **类型一致**:`valid_records` 记录键在 Task 4(`_evaluate`)与 Task 8(`evaluate` 消费 `r["code"]/["bar"]/["close1"]/["risk"]/["trend3"]/["composite"]/["gap"]/["od"]`)逐键一致;`_metric_return`/`_metric_risk`/`_metric_path` 返回键与 Task 6/8 消费键一致;`evaluate` 的 `se_bounds` 在 `build_report`(序列化为 `[L,dim,se]` 列表)与 `render_markdown`(重建 `se_map`)两处一致;`DIMS`/`PRIMARY` 用 path(非 od)。
