# 评分影响项修复 + V2 重测 设计

> 状态:待评审。本 spec 实现「审计/回测/优化大任务」的下一个优化切片(切片 A):修 3 个评分影响项,再重测 V2 对比冻结基线 V1 看准确率是否真提升。

## 1. 目标

修复生产代码中 3 个「评分影响」缺陷——P0 量能单位 mismatch、P0 quote 缺 high/low/open、P1 热权重不一致(signal 模式)——同步更新回测模块 `backtest.py` 的 `score_at` 使其反映修复后行为,然后重测 V2 对比冻结基线 `backtest_baseline.json`(V1),如实报告准确率是否提升。

## 2. 背景:核验结论

多代理核验(14/15 CONFIRMED,1 CHANGED)确认 3 个评分影响项全在 `recommend.py` 的 `_score_candidate` 路径,根因落在 `data_source.py`:

| ID | 缺陷 | 现状证据 |
|---|---|---|
| P0-volume-unit | 现货成交量「手」直传 `quote["volume"]`,分母 `_avg5_volume` 读日线「股」→ vr 低估 ~100x | `data_source.py:220` 无 ×100,腾讯路径 `:186` 已 ×100 并注释「手→股」 |
| P0-quote-highlowopen | `_score_candidate` quote 无 high/low/open → `compute_stock_risk`「高位长上影 +20」分支死 | `analysis.py:497-503` 要求三者皆非 None;`recommend.py:97-98` quote 只 4 字段 |
| P1-hot-weight | `collect_actionable_leaders` 走 `_score_candidate` 但不走 `_apply_hot_weights` → 龙头页与推荐页 composite/verdict 不一致 | `recommend.py:361-363` 与 `:132-155` |

## 3. 关键洞察:三个修复项对回测的关系不对称

`stock_composite_v3 = (quality + bonus) * max(0, 1 - risk/100)`,风险既是折扣乘子又是 risk<70 硬门槛。据此:

| 修复项 | 修复位置 | 基线 V1 是否有此 bug | V2 是否变化 |
|---|---|---|---|
| P0-volume-unit | 仅生产 `data_source.py` | 无(基线 `score_at` 直读日线股,单位一致) | **不变** |
| P0-quote-highlowopen | 生产 + 基线 `score_at` | 有(基线 quote 同样缺 high/low/open) | **变**(唯一动准确率) |
| P1-hot-weight | 仅生产 `recommend.py` | 无(基线篮 A 是代理管线,不建模热权重) | **不变** |

**推论:** V2 vs V1 的差异将**全部来自 P0-quote-highlowopen**。P0-volume-unit 与 P1-hot-weight 修的是上线后生产正确性(量能因子不再系统性偏低、两页分数对齐),但当前回测仪器既已量能一致、又不建模热权重,二者准确率影响**测不出**。本 spec 如实记录,不夸大。

## 4. 设计

### 4.1 P0-volume-unit(仅生产)

**文件:** `data_source.py` `get_market_spot`(现 212-228 行)。

现货路径补上单位换算,与腾讯路径 `:186` 一致:

```python
for col in ("price", "change_pct", "volume", "amount", "open", "high", "low"):
    out[col] = pd.to_numeric(out[col], errors="coerce")
out["volume"] = out["volume"] * 100.0   # 手 → 股(与日线成交量单位一致)
```

**安全性:** `get_market_spot` 的 volume 消费方里,`filter_candidates`(recommend.py:72)、`pick_leaders`(:202)只用 volume 做「停牌」(空/零)布尔判断,`_market_turnover` 用 `amount` 不用 volume,均对量级不敏感;唯一量级敏感的消费方是 `_score_candidate → score_stock → custom_volume_ratio`(正是要修的病)。×100 不扰动其他消费方。

### 4.2 P0-quote-highlowopen(生产 + 基线)

**文件 A:** `data_source.py` `get_market_spot` —— 补 high/low 列(open 已有):

```python
"high": _pick(raw, "最高", "high"),
"low": _pick(raw, "最低", "low"),
```
并加入 4.1 的 to_numeric 循环(见上)。

**文件 B:** `recommend.py` `_score_candidate`(96-98 行)quote 增加三字段,用 `.get()` 兼容缺列的行(旧测试不提供 high/low 也不崩):

```python
quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
         "volume": _num(row["volume"]), "amount": _num(row["amount"]),
         "high": _num(row.get("high")), "low": _num(row.get("low")),
         "open": _num(row.get("open"))}
```

**文件 C:** `backtest.py` `score_at`(115-124 行)quote 增加三字段,取自时点切片 `df = d.iloc[:i+1]` 的末根(≤ T,无未来泄露):

```python
"high": float(df["high"].iloc[-1]),
"low": float(df["low"].iloc[-1]),
"open": float(df["open"].iloc[-1]),
```

真实日线 pkl 列已核:`['date','open','high','low','close','volume']`,三字段齐备。

### 4.3 P1-hot-weight(仅生产,signal 模式)

**文件:** `recommend.py`。从 `_apply_hot_weights`(:132-155)内循环抽出单只重算 helper,`collect_actionable_leaders.work()`(:361-363)复用之。

抽出的 helper(行为与现内循环逐行等价):

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
```

`_apply_hot_weights`(现 132-155)改为循环调用它(保留顶部 hot/off 早退与 `rel_map` 一次性计算,行为逐行等价):

```python
def _apply_hot_weights(ranked, base_g5, sector_composite):
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return ranked
    rel_map = _rel_strengths(base_g5) if HOT_WEIGHT_MODE == "rel_strength" else {}
    for x in ranked:
        rel = rel_map.get(x["code"][-6:]) if rel_map else None
        _apply_hot_weights_one(x, sector_composite, rel=rel)
    return ranked
```

`collect_actionable_leaders.work()` 在 `_score_candidate` 之后、`tier_for_verdict` 之前调用:

```python
scored = _score_candidate(row, daily, now, s["composite"])
if scored is None:
    return None, stale
_apply_hot_weights_one(scored, s["composite"], rel=None)   # signal 模式 rel 恒 None
```

**范围:** 仅 signal 模式(默认 `HOT_WEIGHT_MODE="signal"`)。rel_strength 模式(需逐板块分位 base_g5 采集)不在本切片。

## 5. 回测重测(V2)程序与预期

1. 全量 `python -m pytest tests/ -q` 全绿。
2. `python backtest.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out backtest_baseline_v2.json`(实测 ~11min)。
3. 对比 V2 与冻结 V1(`backtest_baseline.json`)8 行。

**预期差异模式(由 §3 洞察推出,用于判伪):**

| 篮子 | 是否经 risk 折扣/risk<70 门槛 | 预期 V2 变化 |
|---|---|---|
| A(od/gap/close1)、E_hi、E_lo | 是(composite 含 risk 折扣 + risk<70 门槛) | **变** |
| B 全市场 top15 | 是(risk<70 + composite 排序) | **变** |
| C 热板块随机 | 否(无 risk 过滤/排序) | 不变 |
| D 全市场基准 | 否(全 buyable,无 risk 过滤) | 不变 |

若 V2 出现 C/D 变化、或 A/E/B 无变化,则说明修复未生效或存在未来泄露,须停手排查(判伪信号)。

## 6. 测试策略

- **4.1**:`test_data_source.py` 增一条现货路径 volume×100 用例(mock `_ak.stock_zh_a_spot` 返回「手」量,断言「股」量);现有 `:83` 已锁腾讯路径,不动。
- **4.2**:`test_recommend.py` 增一条推荐路径用例:quote 带 high/low/open 且满足「高位长上影」形态(high−max(open,close) > 2×body、振幅>5)时,risk 含 +20 且 reach 到 payload/档位。`test_backtest.py` 增 `score_at` 带 high/low/open 的用例(用 `make_daily`,其已提供 high/low/open)。现有 risk 测试(test_analysis_stock.py:145-167)直接传 high/low/open,不受影响。
- **4.3**:更新 `test_recommend.py:357-394 test_collect_actionable_leaders_two_tiers_and_dedupe` 钉值(composite 83.5 → hot-weighted ~70,须重算并钉新值);`:595-621 test_hot_sector_rel_strength_applied` 为推荐路径热权重行为的模型,复用其构造。

## 7. 风险

- **P0-quote-highlowopen 会放大 risk**:+20 可能把边界股推过 risk<70 门槛,收缩可推荐集,连带改变仍在校准中的「可操作率」指标(scoring-v3-calibration-followups 相关)。这是修复的预期效果,但幅度须在 V2 报告里单列。
- **P1 修后 composite 钉值重算**:`test_collect_actionable_leaders` 的 83.5 会变,若只改代码不改测试会红——必须同 commit 更新钉值,且新值由公式重推、不得「凑数」。
- **`.get()` vs `[]`**:quote 三字段用 `.get()` 是为兼容缺列测试行;若某生产路径 `row` 是 pandas Series(非 dict),`.get()` 仍可用(`Series.get`),无副作用。

## 8. 非目标(本切片不做)

- rel_strength 模式热权重(需逐板块分位 base_g5 采集)。
- 其余 12 条缺陷(XSS、is_next_day、2 perf、4 maint、3 范围外)→ 第二切片。
- 量能单位与热权重的「准确率影响量化」——当前回测仪器测不出,留待后续切片扩展回测建模热权重/生产口径后再测。
