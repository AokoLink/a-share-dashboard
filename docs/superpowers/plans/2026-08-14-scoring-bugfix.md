# 评分影响项修复 + V2 重测 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修 3 个评分影响缺陷(P0 量能单位、P0 quote 缺 high/low/open、P1 热权重 signal 模式),同步更新 `backtest.py` 的 `score_at` 并加诊断计数,然后重测 V2 对比冻结基线 V1 看准确率是否真提升。

**Architecture:** 三处生产代码原子修改(`data_source.py` `get_market_spot`、`recommend.py` `_score_candidate`、`recommend.py` `_apply_hot_weights`)+ 一处基线模块修改(`backtest.py` `score_at` + 诊断计数)。V2 vs V1 差异 100% 来自 `score_at` 一处(P0-quote-highlowopen),另两处只修生产正确性、回测测不出。

**Tech Stack:** Python 3 + pandas 3.0.5(Copy-on-Write)+ pytest。

**Spec:** `docs/superpowers/specs/2026-08-14-scoring-bugfix-design.md`(权威;本计划据此论证,冲突时以 spec 为准)

## Global Constraints

- 直接 commit 到 `main`,不建 worktree / feature branch(用户长期约束)。
- `_analysis/` 已 gitignore,探针/临时产物绝不 commit。
- Python 源码只用 ASCII `-`,禁用 U+2212 `−`。
- `_analysis/code2sector.json` 是 GBK 编码(读时 `encoding="gbk"`)。
- backtest 反未来泄露:评分输入 ≤ T,验证只读 > T(`score_at` 用 `d.iloc[:i+1]`)。
- 全量测试命令:`python -m pytest tests/ -q`(当前 164 passed)。
- pandas CoW:禁止 `df["col"].iloc[0] = x` 链式赋值,用 `df.iloc[0, df.columns.get_loc("col")] = x`。

---

### Task 1: `get_market_spot` 量能单位手→股 + 补 high/low 列

**Files:**
- Modify: `data_source.py:212-228`(`get_market_spot` 内 `fetch`)
- Test: `tests/test_data_source.py`(新增 1 条)

**Interfaces:**
- Produces: `get_market_spot()` 返回的 DataFrame 新增 `high`/`low` 列;`volume` 列改为「股」(原值 ×100)。下游消费方 `recommend.py` `_score_candidate`(Task 2 补 high/low/open)与 `filter_candidates`/`pick_leaders`(仅布尔用 volume,量级不敏感)均兼容。
- Consumes: `_pick(raw, *names)`(已存在)、`pd.to_numeric`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_data_source.py` 末尾追加(参考现有 `test_market_spot_includes_open` 的 `ds.cache._data.clear()` 防污染模式):

```python
def test_market_spot_volume_hand_to_share_and_high_low(monkeypatch):
    ds.cache._data.clear()                          # 避免被其他用例缓存污染
    raw = make_spot().copy()
    raw["成交量"] = [1000, 2000, 3000, 0, 4000]       # 手
    raw["最高"] = [10.5, 1400.0, 12.5, 45.0, 210.0]
    raw["最低"] = [9.5, 1330.0, 11.5, 44.0, 190.0]
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: raw)
    df, stale = ds.get_market_spot()
    assert stale is False
    assert df.loc[0, "volume"] == pytest.approx(1000.0 * 100.0)   # 手 → 股
    assert df.loc[0, "high"] == pytest.approx(10.5)
    assert df.loc[0, "low"] == pytest.approx(9.5)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_data_source.py::test_market_spot_volume_hand_to_share_and_high_low -v`
Expected: FAIL — `df["volume"]` 仍为 1000(未 ×100),`df` 无 `high`/`low` 列 → KeyError/断言失败。

- [ ] **Step 3: 改 `get_market_spot`**

`data_source.py` `get_market_spot` 内 `fetch`(现 213-226 行)改为:

```python
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
```

> ⚠️ 必须是同一函数的一次原子编辑:先加 high/low 列、再 to_numeric、再 volume×100。不可分半(单独 to_numeric 会因无 high/low 列而 KeyError)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_data_source.py -q`
Expected: PASS(含新增用例与既有 `test_market_spot_normalized`/`test_market_spot_stale_on_failure`/`test_market_spot_includes_open` 全绿)。

- [ ] **Step 5: Commit**

```bash
git add data_source.py tests/test_data_source.py
git commit -m "fix(data): get_market_spot 量能单位手->股 + 补 high/low 列"
```

---

### Task 2: `_score_candidate` quote 补 high/low/open

**Files:**
- Modify: `recommend.py:96-98`(`_score_candidate` quote 构造)
- Test: `tests/test_recommend.py`(新增 2 条)

**Interfaces:**
- Consumes: `row`(dict,来自 `get_market_spot().to_dict("records")`,Task 1 后含 high/low/open 键);`_num(v)`(已存在,None → None)。
- Produces: `quote` 新增 `high`/`low`/`open` 键,用 `.get()` 兼容缺列行(旧测试/无 high/low 的 spot 行 → None,不崩)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_recommend.py` 追加:

```python
def test_score_candidate_passes_high_low_open(monkeypatch):
    captured = {}
    def fake_score_stock(daily_df, quote, now):
        captured.update(quote)
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    row = {"code": "600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
           "volume": 100000, "amount": 2e8, "high": 5.5, "low": 4.5, "open": 5.1}
    daily = make_daily([10 + i for i in range(65)])
    out = recommend._score_candidate(row, daily, datetime.datetime(2026, 8, 11, 15, 0),
                                     sector_composite=78.0)
    assert out is not None
    assert captured["high"] == pytest.approx(5.5)
    assert captured["low"] == pytest.approx(4.5)
    assert captured["open"] == pytest.approx(5.1)


def test_score_candidate_missing_high_low_open_ok(monkeypatch):
    captured = {}
    def fake_score_stock(daily_df, quote, now):
        captured.update(quote)
        return {"position": 50, "trend": 50, "volume_price": 50, "signal": 50, "risk": 20, "composite": 40}
    monkeypatch.setattr(an, "score_stock", fake_score_stock)
    row = {"code": "600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
           "volume": 100000, "amount": 2e8}              # 无 high/low/open
    daily = make_daily([10 + i for i in range(65)])
    out = recommend._score_candidate(row, daily, datetime.datetime(2026, 8, 11, 15, 0),
                                     sector_composite=78.0)
    assert out is not None
    assert captured["high"] is None
    assert captured["low"] is None
    assert captured["open"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_recommend.py::test_score_candidate_passes_high_low_open tests/test_recommend.py::test_score_candidate_missing_high_low_open_ok -v`
Expected: FAIL — `captured["high"]` 为 KeyError(quote 无 high 键)。

- [ ] **Step 3: 改 `_score_candidate`**

`recommend.py:97-98` 改为:

```python
    quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
             "volume": _num(row["volume"]), "amount": _num(row["amount"]),
             "high": _num(row.get("high")), "low": _num(row.get("low")),
             "open": _num(row.get("open"))}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_recommend.py -q`
Expected: PASS(新增 2 条 + 既有全部全绿;`test_collect_actionable_leaders_two_tiers_and_dedupe` 此时仍 83.5,尚未动,Task 4 才改)。

- [ ] **Step 5: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "fix(recommend): _score_candidate quote 补 high/low/open(.get 兼容缺列)"
```

---

### Task 3: `backtest.py` `score_at` 补 high/low/open + 诊断计数

**Files:**
- Modify: `backtest.py:115-124`(`score_at`)、`:296-366`(`run`)、`:387-406`(`build_report`)、`:409-443`(`render_markdown`)
- Test: `tests/test_backtest.py`(扩 1 条 + 新增 1 条)

**Interfaces:**
- Produces: 新增纯函数 `_long_upper_shadow(high, low, open_, close) -> bool`;`run()` 返回 dict 新增键 `"diag": {"long_upper_shadow": int, "pushed_over_70": int}`;`build_report` payload 新增 `"diag"`;`render_markdown` 输出新增「诊断」段。
- Consumes: `an.score_stock`(已存在)、`score_at`(本任务改)。

- [ ] **Step 1: 写失败测试**

(a) 扩展 `tests/test_backtest.py` 现有 `test_score_at_reconstructs_quote`(149-164 行),在 4 个既有断言后追加 3 行(该测试已 monkeypatch `an.score_stock` 并捕获 `dict(quote)`):

```python
    assert calls[0][1]["high"] == float(d["high"].iloc[63])
    assert calls[0][1]["low"] == float(d["low"].iloc[63])
    assert calls[0][1]["open"] == float(d["open"].iloc[63])
```

(b) 在 `tests/test_backtest.py` 追加纯函数测试:

```python
def test_long_upper_shadow_detection():
    assert bt._long_upper_shadow(10.5, 9.5, 10.0, 10.1) is True    # upper=0.4>0.2, amp≈9.9>5
    assert bt._long_upper_shadow(10.15, 9.5, 10.0, 10.1) is False  # upper=0.05 < 2*body
    assert bt._long_upper_shadow(10.5, 9.5, 10.1, 10.1) is False   # body=0(开=收)
    assert bt._long_upper_shadow(None, 9.5, 10.0, 10.1) is False   # high 缺失
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_backtest.py::test_score_at_reconstructs_quote tests/test_backtest.py::test_long_upper_shadow_detection -v`
Expected: FAIL — quote 无 high/low/open 键(KeyError);`bt._long_upper_shadow` 不存在(AttributeError)。

- [ ] **Step 3: 改 `score_at` + 加 helper**

`backtest.py:115-124` 的 `score_at` 改为:

```python
def score_at(d, i, now):
    df = d.iloc[: i + 1]
    if len(df) < 61:
        return None
    quote = {"price": float(df["close"].iloc[-1]),
             "change_pct": float(df["change_pct"].iloc[-1]),
             "volume": float(df["volume"].iloc[-1]),
             "amount": float(df["volume"].iloc[-1]) * float(df["close"].iloc[-1]),
             "high": float(df["high"].iloc[-1]),
             "low": float(df["low"].iloc[-1]),
             "open": float(df["open"].iloc[-1])}
    sc = an.score_stock(df, quote, now)
    return sc if sc["composite"] is not None else None
```

在 `score_at` 之后新增纯函数(与 `analysis.py:498-503` 同式):

```python
def _long_upper_shadow(high, low, open_, close):
    """「高位长上影」谓词,与 analysis.py:498-503 同式;输入取自时点 bar(≤T)。"""
    if None in (high, low, open_, close) or close <= 0:
        return False
    body = max(open_, close) - min(open_, close)
    upper = high - max(open_, close)
    amplitude = (high - low) / max(open_, close) * 100 if max(open_, close) > 0 else 0
    return body > 0 and upper > 2 * body and amplitude > 5
```

- [ ] **Step 4: `run()` 加诊断计数**

`backtest.py:306-311` 的 `get_score` 闭包改为(新增 `diag` 与计数;`bar` 是整数索引,直接 `d[...].iloc[bar]` 取时点 bar,无需再切片):

```python
    score_cache = {}
    diag = {"long_upper_shadow": 0, "pushed_over_70": 0}
    def get_score(code, bar):
        key = (code, bar)
        if key not in score_cache:
            sc = score_at(universe[code], bar, AFTER_CLOSE)
            score_cache[key] = sc
            if sc is not None:
                d = universe[code]
                hi = float(d["high"].iloc[bar])
                lo = float(d["low"].iloc[bar])
                op = float(d["open"].iloc[bar])
                cl = float(d["close"].iloc[bar])
                if _long_upper_shadow(hi, lo, op, cl):
                    diag["long_upper_shadow"] += 1
                    if sc["risk"] >= 70 and sc["risk"] - 20 < 70:
                        diag["pushed_over_70"] += 1
        return score_cache[key]
```

`run()` 的 return(365-366 行)改为加 `"diag": diag`:

```python
    return {"rows": rows, "by_year": by_year, "welch": welch, "diag": diag,
            "n_eval": n_eval, "step": step, "window": window, "data_range": data_range}
```

- [ ] **Step 5: `build_report` / `render_markdown` 透出 diag**

`build_report`(387-406 行)return dict 内、`"welch": results["welch"],` 之后加一行:

```python
        "diag": results.get("diag", {}),
```

`render_markdown`(409-443 行)在 `lines.append("结论:基线篮 A 的准确率是「代理管线」的准确率...")` 之后、`lines.append("")` 之前插入:

```python
    diag = payload.get("diag", {})
    if diag:
        lines += ["", "## 诊断(P0-quote-highlowopen 生效计数)", ""]
        lines.append(f"- 高位长上影触发 stock-day: {diag.get('long_upper_shadow', 0)}")
        lines.append(f"- 因 +20 越 risk>=70 门槛 stock-day: {diag.get('pushed_over_70', 0)}")
        lines.append("")
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_backtest.py -q`
Expected: PASS(新增 `test_long_upper_shadow_detection` + 扩展 `test_score_at_reconstructs_quote` + 既有 35 条全绿)。

- [ ] **Step 7: Commit**

```bash
git add backtest.py tests/test_backtest.py
git commit -m "fix(backtest): score_at quote 补 high/low/open + 长上影诊断计数"
```

---

### Task 4: `_apply_hot_weights_one` 抽取 + 龙头页复用(signal 模式)

**Files:**
- Modify: `recommend.py:132-155`(`_apply_hot_weights`)、`:361-368`(`collect_actionable_leaders.work`)
- Test: `tests/test_recommend.py:392`(钉值 83.5 → 79.0)

**Interfaces:**
- Produces: 新增 `_apply_hot_weights_one(x, sector_composite, rel=None) -> x`(原地改 `x["composite"]`/`x["verdict"]`,signal 模式 `rel` 恒 None)。`_apply_hot_weights` 与 `collect_actionable_leaders.work` 复用之。
- Consumes: `an.HOT_REL_WEIGHTS`/`an.HOT_SIGNAL_WEIGHTS`/`an.stock_composite_v3`/`an.stock_verdict`、`sector_bonus`、`HOT_COMPOSITE_THRESHOLD`/`HOT_WEIGHT_MODE`(均已存在)。

- [ ] **Step 1: 写失败测试(更新钉值)**

`tests/test_recommend.py:392` 改为:

```python
    assert payload["items"][0]["composite"] == pytest.approx(round(79.0, 2))   # HOT_SIGNAL_WEIGHTS 0.40/0.15/0.10/0.35 → 0.40*90+0.15*60+0.10*50+0.35*60=71,+bonus 8=79
```

> tier 断言(`["可介入", "观察"]`)不动:79≥67 仍「可介入」;688981 权重和=1 仍 56「观察」。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_recommend.py::test_collect_actionable_leaders_two_tiers_and_dedupe -v`
Expected: FAIL — 仍是 83.5(work() 尚未走热重权)。

- [ ] **Step 3: 抽取 `_apply_hot_weights_one` 并重写 `_apply_hot_weights`**

`recommend.py:132-155` 替换为(helper 逐行等价于原内循环三条分支):

```python
def _apply_hot_weights_one(x, sector_composite, rel=None):
    """单只个股 P0b 权重重算(spec §3.2)。non-hot 或模式 off → 原样返回;
    rel 非 None 用 HOT_REL_WEIGHTS,signal 模式用 HOT_SIGNAL_WEIGHTS,否则回退 v3 权重(weights=None)。"""
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return x
    if rel is not None:
        w = an.HOT_REL_WEIGHTS
    elif HOT_WEIGHT_MODE == "signal":
        w = an.HOT_SIGNAL_WEIGHTS
    else:
        w = None
    scores = x["scores"]
    bonus = sector_bonus(sector_composite, scores["composite"])   # 自加成前 composite 重算,无重复加成
    final = an.stock_composite_v3(scores["position"], scores["volume_price"],
                                  scores["trend"], scores["signal"], scores["risk"],
                                  bonus, rel_strength=rel, weights=w)
    x["composite"] = final
    x["verdict"] = an.stock_verdict(final)
    if rel is not None:
        x["rel_strength"] = round(rel, 2)
    return x


def _apply_hot_weights(ranked, base_g5, sector_composite):
    """热板块内 P0b 权重重算(spec §3.2/§3.3)。non-hot 或模式 off → 原样返回。"""
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return ranked
    rel_map = _rel_strengths(base_g5) if HOT_WEIGHT_MODE == "rel_strength" else {}
    for x in ranked:
        rel = rel_map.get(x["code"][-6:]) if rel_map else None   # x["code"] 带 sh/sz 前缀 → 截 6 位
        _apply_hot_weights_one(x, sector_composite, rel=rel)
    return ranked
```

- [ ] **Step 4: `collect_actionable_leaders.work()` 复用 helper**

`recommend.py:361-368` 的 `work` 改为(在 `_score_candidate` 之后、`tier_for_verdict` 之前):

```python
    def work(i, s, L, row):
        daily, stale = get_daily_fn(str(L["code"]))
        scored = _score_candidate(row, daily, now, s["composite"])
        if scored is None:
            return None, stale
        _apply_hot_weights_one(scored, s["composite"], rel=None)   # signal 模式 rel 恒 None
        tier = tier_for_verdict(scored["verdict"])
        if tier is None:
            return None, stale
        return {
            "_rank": i,
            "code": scored["code"], "name": scored["name"],
            "price": scored["price"], "change_pct": scored["change_pct"],
            "tag": L["tag"], "tier": tier,
            "sector_code": "%s:%s" % (type_key, s["code"]),
            "sector_name": s["name"], "sector_verdict": s["verdict"],
            "sector_composite": s["composite"],
            "position": scored["scores"]["position"],
            "trend": scored["scores"]["trend"], "volume_price": scored["scores"]["volume_price"],
            "signal": scored["scores"]["signal"], "composite": round(scored["composite"], 2),
            "risk": scored["scores"]["risk"],
            "bias_pct": bias_pct(daily, scored["price"]),
        }, stale
```

> 只新增 `_apply_hot_weights_one(...)` 一行,其余 `work` 主体不动。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_recommend.py -q`
Expected: PASS(钉值 79.0;其余 3 个 leaders 用例 `resolve_failure_skips`/`daily_failure_skips`/`stale_aggregation` 走热重权路径但不钉 composite、仅断言 code/total/stale/reasons → 应仍绿。**若这 3 条红,属「档位变化」非回归**:重权把 verdict 落到 None 档被过滤,按新档重钉即可,别当 bug 排查**)。

- [ ] **Step 6: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: PASS(164 + 新增若干条全绿)。

- [ ] **Step 7: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "fix(recommend): _apply_hot_weights_one 抽取 + 龙头页复用(signal 模式)"
```

---

### Task 5: V2 重测 + 对比冻结基线 V1(验收)

**Files:**
- Create: `backtest_baseline_v2.json` + `backtest_baseline_v2.md`(运行产物,commit 同切片)
- 对照: `backtest_baseline.json`/`.md`(V1,冻结,勿动)

**Interfaces:**
- Consumes: `backtest.run(data_dir, sector_map_path)`、`build_report`、`render_markdown`(Task 3 已改)。

- [ ] **Step 1: 核对数据快照未变**

Run: `python -c "import backtest as bt; r=bt.run('_analysis/daily','_analysis/code2sector.json'); print(r['data_range'])"`
Expected: `{'start': '2019-02-25', 'end': '2026-08-11'}` 与 V1 逐位一致(若 end 已推进说明 daily pkl 被更新,须先与用户确认再跑,否则 C/D 会误触判伪)。

- [ ] **Step 2: 跑 V2**

Run: `python backtest.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out backtest_baseline_v2.json`(实测 ~11min;后台跑,别阻塞)

- [ ] **Step 3: 断言 system_version 与 data_range**

核对 `backtest_baseline_v2.json`:
- `system_version` != `151d284`(V1 冻结版本;V2 应为本次 commit 的短 SHA)。
- `data_range` 与 V1(`2019-02-25` → `2026-08-11`)逐位一致。

- [ ] **Step 4: 对比 V2 vs V1 八行 + 判伪**

对照 `backtest_baseline.json` 的 8 行(mean_pct/win_rate/n):
- **硬判伪**:C、D 两行必须逐位一致(与 V1 相同);若 C/D 变 → 数据快照漂移或未来泄露,停手排查。
- **启发式**:A(od/gap/close1)、E_hi、E_lo、B 预期变(因 high/low/open 修复);若不变,核对 Step 3 的诊断计数——若计数为 0 则「无触发样本」而非「无效果」。

- [ ] **Step 5: 读诊断计数**

核对 `backtest_baseline_v2.json` 的 `diag`:
- `long_upper_shadow`(高位长上影触发 stock-day 数)、`pushed_over_70`(因 +20 越 risk≥70 的 stock-day 数)。
- 如实报告:若两者均为 0 且 A/E/B 无变化 → 修复生效但本窗口无样本。

- [ ] **Step 6: 输出对比结论 + commit 产物**

在对话中给出一张 V1 vs V2 对照表(8 行 mean_pct/win_rate/n + 诊断计数 + 是否提升的结论),然后:

```bash
git add backtest_baseline_v2.json backtest_baseline_v2.md
git commit -m "docs(backtest): V2 重测产物(P0-quote-highlowopen 修复后重测,附诊断计数)"
```

---

## Self-Review(计划自检)

- **Spec 覆盖**:Fix 1(§4.1)→ Task 1;Fix 2 File A(§4.2)→ Task 1;Fix 2 File B(§4.2)→ Task 2;Fix 2 File C + 诊断(§4.2/§5)→ Task 3;Fix 3(§4.3)→ Task 4;V2 重测 + 判伪(§5)→ Task 5。§6 测试策略逐条落到 Task 1-4 的测试。§7 风险(79.0 钉值、其余 3 leaders 用例、数据快照核对)均已写入对应 Task 的 Expected/Step 提示。
- **占位符**:无 TBD/TODO;所有测试/代码均为完整可落地内容;值(79.0、HOT_SIGNAL_WEIGHTS、100.0、151d284、data_range)均来自 spec 与实测。
- **类型一致**:`_apply_hot_weights_one(x, sector_composite, rel=None)` 签名在 Task 4 内部两处调用(helper 定义 + `_apply_hot_weights` 循环 + `work()` 直调)一致;`_long_upper_shadow(high, low, open_, close)` 签名与 Task 3 测试一致;`diag` 键名 `long_upper_shadow`/`pushed_over_70` 在 `run`/`build_report`/`render_markdown`/Task 5 一致。
