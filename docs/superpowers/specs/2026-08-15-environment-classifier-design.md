# 市场环境分类(牛/熊/震荡/恐慌/高潮/退潮/恢复)设计

- 日期:2026-08-15
- 状态:草案(自审后提交)
- 上游:审计/回测/优化大任务第 7 点「市场环境分类,按环境分准确率」
- 前置:`_analysis/daily/*.pkl` 已为 9 列(含 amount/turnover,Step 1 commit d3d72fd);`backtest.py`/`predict.py`/`evaluate.py`/`compare.py` 已冻结

## 1. 目标

新增生产级只读模块 `environment.py`,把每个交易日归入七态之一(**牛 / 熊 / 震荡 / 恐慌 / 高潮 / 退潮 / 恢复**),并把这套时间维标签接入评估链路:按环境切片统计预测准确率。分类器只用 ≤ T 的市场数据,**不 fetch**,与 backtest/predict/evaluate/compare 同一数据契约;分类是纯因果的(第 i 日标签只依赖 ≤ i 的数据)。

## 2. 范围

**包含:**

- 从 `_analysis/daily/*.pkl` 离线自算「合成市场序列」:每日等权(中位)收益、涨跌家数比、涨停/跌停家数、总成交额、量能比、合成市场指数的 MA/区间收益(§5)。
- 优先级有序的确定性七态决策树(§6),每日最多一标签。
- 历史全回填:对 universe 覆盖的每个交易日打标签,输出状态分布 + 各态特征统计 + 诚实声明(§7)。
- 接入 `evaluate.py`:按日期环境标签切片六维准确率(§8)。
- 反泄露测试(§9)+ 阈值锚定标定(§10)。

**不包含(后续或独立项):**

- **不 fetch、不重拉数据**:市场序列只从 pkl 自算;真实指数(上证/深证/创业板)历史**不使用**。合成指数与真实指数的口径差异如实披露(§3、§7)。
- **不重跑评分/预测/校准**:环境分类器不调 `score_at`/`predict_at`/任何校准器。
- **不做自动阈值调参**:七态阈值是模块常量(初值),v1 只做「锚定标定报告」证明阈值落在真实分布内,不做自动优化(对应大任务第 9 点防过拟合的克制)。
- **交易模拟 / 版本注册表 / 自动复盘 / 最终自审**:分别留待 #128/#129/#130。
- **分钟级盘中状态**:日线 pkl 无分钟数据,环境按日线口径,盘中实时分类不做。

## 3. 数据契约(只读,不 fetch)

- 日线缓存:`--data-dir` 指向 `_analysis/daily/*.pkl`(默认)。9 列 `[date, open, high, low, close, volume, amount, outstanding_share, turnover]`;`close/open/high/low` 前复权,`amount`(成交额,元)不复权。文件名 = 6 位代码。
- 板块映射:`--sector-map` 指向 `_analysis/code2sector.json`(默认),GBK 编码。environment 只用它做 universe 过滤(与 backtest/predict/evaluate 同源),不读板块语义。
- **合成市场指数的口径声明(重要,诚实)**:本模块用 1623 只 universe 股的**中位日收益**复合成一条「合成市场指数」M,不是上证指数。M 只覆盖 universe(幸存者 + 大中盘子集,≥1200 根过滤),与真实全 A 指数存在口径差异;涨跌停阈值按个股 `an.limit_threshold(code)` 区分主板/创业板/科创板。所有结论以此为事实,不冒充真实指数。

## 4. 架构(单模块 `environment.py`,纯函数 + CLI)

与 `analysis.py`/`backtest.py`/`predict.py`/`evaluate.py`/`compare.py` 平级,平铺纯函数。导入:`import analysis as an`、`import backtest as bt`、`import pandas as pd`、`import numpy as np`。

**复用既有公开函数(不修改、不 import 下划线私有):**

| 复用符号 | 用途 |
|---|---|
| `bt.load_sector_map(path)` | 读 GBK 板块映射 |
| `bt.build_universe(data_dir, sector_map)` | `(universe, codes)`;universe[c] 含 `change_pct`(百分数)与 `amount` 列 |
| `bt.build_calendar(universe, codes)` | `(all_days, pos_of)` |
| `an.limit_threshold(code)` | 涨停阈值(百分数,区分板块) |

**本模块对外提供(供 evaluate.py 接入):**

| 符号 | 签名 | 说明 |
|---|---|---|
| `build_series` | `(universe, pos_of, all_days) -> pd.DataFrame` | 合成市场序列,行 = all_days 位置,列见 §5;末列 `environment` |
| `classify` | `(row) -> str \| None` | 单日决策,返回七态之一或 `None`(历史不足) |

## 5. 合成市场序列(§5 特征定义)

对 all_days 的第 `i` 日(位置 i),遍历 `universe` 中每只股,取其 `bar = pos_of[c].get(all_days[i])`;若 bar 非 None,读该股当日 `change_pct`(百分数)与 `amount`。聚合得到:

- `r1[i]` = 当日市场收益(小数)= `median(change_pct/100)`(对非 NaN 的 change_pct;空集 → None)。
- `up_ratio[i]` = 上涨家数占比 = `mean(change_pct > 0)`(空集 → None)。
- `limit_up[i]` = 涨停家数 = `sum(change_pct >= an.limit_threshold(code))`。
- `limit_down[i]` = 跌停家数 = `sum(change_pct <= -an.limit_threshold(code))`。
- `turnover[i]` = 总成交额 = `sum(amount)`(非 NaN)。
- `turnover_ratio[i]` = 量能比 = `turnover[i] / mean(turnover[i-5..i-1])`;`i<5` 或分母 0 → None。
- `M[i]` = 合成市场指数:`M[0]=1.0`;`M[i]=M[i-1]*(1+r1[i])`(r1 None → M 沿用前值,即跳空不增长)。
- `ma5[i]/ma20[i]/ma60[i]` = M 的滚动均值(`rolling(k).mean()`)。
- `r5[i]/r20[i]/r60[i]` = `M[i]/M[i-k]-1`;`i<k` → None。

DataFrame 列顺序:`[date, r1, up_ratio, limit_up, limit_down, turnover, turnover_ratio, M, ma5, ma20, ma60, r5, r20, r60, environment]`。

**缺失值表示**:序列中「尚未有足够历史」的字段以 NaN 表示(pandas `rolling(k).mean()` 前 k-1 行自然为 NaN);`classify` 对 NaN 与 None 一视同仁,均视为缺失(该条不命中/不足)。`environment` 列值:七态字符串或 None(不足)。

`change_pct` 首 bar 桩 0.0(bt.build_universe 设定)只影响序列最前若干日;因分类要求 `MIN_HISTORY` 历史,这些日标签为 None(不足),桩值不进入任何已分类日。

## 6. 七态决策树(优先级有序,先命中先得)

模块常量(初值,§10 锚定验证):

```python
MODULE_VERSION = "1.0.0"
MIN_HISTORY = 60            # 至少 60 根,否则 label=None(不足)
MIN_STATE_N = 20            # 状态样本数低于此 → 报告标「退化态」诚实提示

# 恐慌 panic
PANIC_R5 = -0.08
PANIC_R1 = -0.04
PANIC_UP_RATIO = 0.15
PANIC_LIMIT_DOWN = 300
# 高潮 climax
CLIMAX_R5 = 0.08
CLIMAX_UP_RATIO = 0.85
CLIMAX_LIMIT_UP = 100
CLIMAX_TURNOVER = 1.8
# 熊 / 牛
BEAR_R20 = -0.08
BULL_R20 = 0.08
# 恢复 recovery
RECOVERY_R5 = 0.03
RECOVERY_UP_RATIO = 0.55
# 退潮 receding
RECEDE_R5 = -0.03
RECEDE_UP_RATIO = 0.45
```

`classify(row)` 先做**历史闸**:`r60` 或 `ma60` 为缺失(NaN/None)→ 直接返回 `None`(不足,历史未满 60 根)。通过闸后按下面优先级判定(任一项命中即返回,不再往下):

1. **恐慌 panic**:`r5 <= PANIC_R5` 或(`r1 <= PANIC_R1` 且 `up_ratio <= PANIC_UP_RATIO`)或 `limit_down >= PANIC_LIMIT_DOWN`。
2. **高潮 climax**:`r5 >= CLIMAX_R5` 且(`up_ratio >= CLIMAX_UP_RATIO` 或 `limit_up >= CLIMAX_LIMIT_UP` 或 `turnover_ratio >= CLIMAX_TURNOVER`)。
3. **熊 bear**:`r20 <= BEAR_R20` 且 `ma5 < ma20 < ma60`。
4. **牛 bull**:`r20 >= BULL_R20` 且 `ma5 > ma20 > ma60`。
5. **恢复 recovery**:`r5 >= RECOVERY_R5` 且 `r20 < 0` 且 `up_ratio >= RECOVERY_UP_RATIO`。
6. **退潮 receding**:`r20 >= 0` 且 `r5 <= RECEDE_R5` 且 `up_ratio <= RECEDE_UP_RATIO`。
7. **震荡 oscillation**:兜底(通过历史闸且上述六条均未命中的日子)。

历史闸用 `r60`/`ma60`(均需 60 根历史)统一卡住早期日,避免恐慌/高潮两条只用短窗字段在 `i < MIN_HISTORY` 时误触发。任一判定所用字段为 NaN/None → 该条不命中(跳到下一条)。

标签字符串:`"牛" "熊" "震荡" "恐慌" "高潮" "退潮" "恢复"`,None 表示不足(报告计为「不足」,不进分布)。

## 7. 报告(CLI `run`)

`run(data_dir, sector_map_path) -> payload`(另有 `build_report(payload)` / `render_markdown(payload)` / `main(argv)`,镜像 compare.py 的 CLI 结构,`--data-dir/--sector-map/--out`):

```
{
  "mode": "environment",
  "system_version": <git short sha>,
  "module_version": "1.0.0",
  "data_range": {"first": <date>, "last": <date>},
  "n_days": N,               # all_days 总数
  "n_classified": M,         # i >= MIN_HISTORY 且 label 非 None 的天数
  "n_insufficient": N - M,
  "distribution": {"牛": c, "熊": c, "震荡": c, "恐慌": c, "高潮": c, "退潮": c, "恢复": c},
  "state_stats": {state: {"n", "mean_r5", "mean_r20", "mean_up_ratio", "mean_turnover_ratio"}},
  "degenerate_states": [state, ...],   # count < MIN_STATE_N 或 ==0 的态,诚实列出
  "series": [{"date": ..., "environment": ...}, ...],
  "notes": ["合成指数为 universe 中位收益复合,非真实指数(口径差异见 §3)", ...],
}
```

`render_markdown` 输出:分布表(态/天数/占比)、各态特征统计表、退化态提示、诚实声明(合成指数口径、单标签互斥、标签因果 ≤T)。

## 8. 接入 evaluate.py(按环境分准确率)

- `evaluate.py` 顶部 `import environment as env`。
- `evaluate(records, universe, pos_of, all_days, ...)` 内:`series = env.build_series(universe, pos_of, all_days)`(只算一次);对每个 `date`(位置 i)取 `state = series["environment"].iloc[i]`;把该日所有 record 归入 `env_records[state]`(state 为 None → 跳过,不入任何环境)。
- 用既有 `_dim_metrics` 对每个 `state` 算六维指标:`env_metrics = {state: {dim: _dim_metrics(...)}}`,`env_n = {state: len(env_records[state])}`。
- 返回 payload 增两键:`"environments": env_metrics`、`"env_n": env_n`;`build_report`/`render_markdown` 增「各环境准确率」表(态/样本数/六维主指标)。**环境不进 ±2σ 显著性判据**(该判据仍只对 8 个横截面层);样本数 < `MIN_LAYER_N`(=30)的环境在主指标旁标注「样本不足」。
- **compare.py 不接入**:其前向快照 08-11 为单日(915 预测全在同一环境),按环境切片退化为单态,无意义。environment.py 报告可附 08-11 当日标签作上下文,但不做切片。

## 9. 反泄露测试(核心不变式)

1. **因果性**:`series["environment"].iloc[i]` 只依赖 ≤ i 的行情。构造 fixture,`universe` 某股 `> i` 日的 close/amount 用 `.loc` 改动(注意 pandas 3.0 CoW,不得链式赋值)→ `label[i]` 不变。
2. **≤T 敏感**:改某股 `<= i` 日的 close(影响 r1/up_ratio)→ `label[i]` 允许变(这不是泄露,是当日事实)。
3. **阈值边界**:每个判定式的边界值(如 `r5 == -0.08` 恰好恐慌、`r20 == 0.08` 恰好牛)钉值断言。
4. **优先级**:构造同时满足恐慌与熊的日子 → 恐慌;同时满足高潮与牛 → 高潮。
5. **历史不足**:`i < MIN_HISTORY` → `classify` 返回 None。
6. **字段 None 不命中**:任一判定字段 None/NaN → 该条跳过,落到兜底/None。

## 10. 阈值锚定标定(不编造,不自动调参)

实现收尾时对真实缓存跑一次 `run`,产出 gitignored `_analysis/environment_calibration.md`,记录:

1. 各判定指标的真实分位锚(r5/r20 的 p10/p50/p90、up_ratio、limit_up/limit_down、turnover_ratio),证明阈值落在真实分布内。
2. 七态最终分布(计数/占比);对 `count < MIN_STATE_N` 或 ==0 的态**如实标退化**,不编造分布。
3. 已知 A 股 regime 抽查(如 2015-06 高潮/退潮、2016-01 恐慌、2018 熊、2019-02 恢复、2020-02 恐慌、2020-07 高潮、2024-02 恐慌):对比分类器输出是否落在合理态,写结论。

阈值是常量;若标定发现某阈值把某态打成空/退化,报告如实写,默认**不自动改阈值**(改动留待 #128 自动复盘按新样本验证)。
