# 评分系统(六维 + 八分层测试池)设计规格

- 状态:草案,待评审
- 日期:2026-08-14
- 上游:预测引擎 V1.0(`predict.py`,HEAD `62c60a7`)、数据修复 Step 1(`data_source.get_stock_daily` 9 列,HEAD `d3d72fd`)
- 范围:扩展引擎输出「预期收益 + 风险概率」,新建 `evaluate.py` 做六维评分 × 八分层测试池报告

## 1. 目标

在现有四校准器(direction/gap/od/trend3)之上,把预测引擎升级为「方向 + 量级 + 风险」的完整结构化预测,并用一个**不挑易预测样本**的八分层测试池,对六维指标逐一报准确率。目的不是美化准确率,而是**暴露弱层、诚实归因**。

## 2. 数据契约(①:amount 重拉)

- **来源与调整基础**:`data_source.get_stock_daily` → `_ak.stock_zh_a_daily(symbol, adjust="qfq")`(东财 hist)。该接口返回 9 列;其中 `close/open/high/low` 前复权,`volume` 与 `amount` **不复权**(原始股数 / 原始成交额,元)。已核(2026-08-14):对最大复权因子历史日(600519 2002-05-17 close 比 0.11244)amount 比不复权 = 1.000000。
- **大盘层取值**:用 `amount` 原始成交额做**同日横截面** top20% 排名。不复权正确(排名用真实成交额,不需要跨日调整)。
- **存储**:`amount` 已含于 `_analysis/daily/<code>.pkl`(9 列,Step 1 已重拉)。`evaluate.py` **只读** `df["amount"]`,**不 fetch** —— 不违反 V1.0 §3「读现有缓存,不 fetch」契约。
- **逐日对齐**:大盘层用 `universe[code]["amount"].iloc[bar]`,`bar = pos_of[code][all_days[i]]`(≤T,与 `score_at` 同 bar)。
- **缓存/幂等**:pkl 即缓存,`_analysis/` gitignored。
- **防漂移**:`evaluate.run` 启动断言每个 pkl 含 9 列;缺 `amount` → 抛错提示重拉(不静默回退到 `volume*close` 代理)。

## 3. 引擎扩展(Part 1:`predict.py`)

### 3.1 ReturnCalibrator(预期收益,非参数、无 PAV)

- 特征 = `composite`(与 direction 校准器同源),标签 = `close1 = close[T+1]/close[T] − 1`。
- 拟合 = 现有 `_fit_calibrator` 的**等量十分箱**(N_BINS=10)去掉 PAV 池化,每箱记 `upper` 与 `mean(close1)`。收益对 composite **不强制单调**,故不加 PAV。
- 退化:干净样本 < N_BINS → 单箱(全局 mean,degraded=True)。
- 预测:`expected_return(composite)` = 该 composite 落入箱的 `mean(close1)`。全宇宙当天只有 ≤10 个取值(阶梯函数)。

### 3.2 RiskCalibrator(风险概率,PAV 单调)

- 特征 = `score_at` 输出的 `risk`(0–100,≤T),标签 = 不利事件 `1[close1 < −0.03]`(③:单一阈值,**无 base-rate 兜底**)。
- 拟合 = `_fit_calibrator`(等量十分箱 + PAV 单调强制),语义上「risk 越高 → P(不利) 越高」,PAV 适用。
- 预测:`risk_p(risk)` = P(不利事件)。

### 3.3 结构化预测扩展

`predict_at` 新增两字段:`expected_return`、`risk_p`(均 ≤T 特征导出)。原四 horizon 不动。

### 3.4 run_backtest 扩展

- 拟合 6 个校准器(direction/gap/od/trend3/return/risk)。
- valid 段新增总体指标:return 的 MAE/RMSE/符号一致率/平均残差/方向条件 MAE;risk 的 ECE/Brier/单调 lift。

## 4. 六维指标(Part 2:公式钉死 ②)

记 `close1`(实际 T+1 收益)、`expected_return`(预测)、`gap`/`od`/`trend3` 如 V1.0 §8.1。方向沿用 ε-band 0.05。

1. **方向** `direction_acc`:up/down/hold 判定 vs `sign(close1)`(V1.0 §8.1 hit_rate,不改)。
2. **开盘** `gap_acc`:high/low/hold vs `sign(gap)`(V1.0,不改)。
3. **路径** `path_acc`:gap×od 四分类命中率(V1.0,不改)。
4. **T+3趋势** `trend3_acc`:V1.0,不改。
5. **涨跌幅误差**(return 维度,新增):
   - `MAE = mean |expected_return − close1|`
   - `RMSE = sqrt(mean (expected_return − close1)^2)`
   - **符号一致率** `sign_agreement = mean 1[sign(expected_return) == sign(close1)]`,零收益样本剔除(不计入分母与分子);报告非零 n。
   - **符号偏差** `mean_residual = mean(close1 − expected_return)`(正值 = 系统性低估;这是偏差不是一致率,两者都报)。
   - **方向条件误差** `dir_cond_MAE`:按**预测方向**(`_dir_conf` 输出 {up,down,hold})分组各报 MAE;每组带 n,小 n(<30)只报 n 不报值。
6. **风险准确率**(risk 维度,新增):
   - `ECE = Σ_bin (n_bin/n)·|p_bin − realized_adverse_bin|`
   - `Brier = mean (risk_p − adverse)^2`
   - **单调 lift** = `realized_adverse(top risk bin) − realized_adverse(bottom risk bin)`(正 = 风险分有区分度)。

## 5. 八分层测试池(Part 3:规则钉死 ②)

全部规则用 ≤T 数据;样本 = valid 段 buyable 股(与引擎预测同池)。每股可属多层,按「分层 × 维度」报准确率 + n。

| 层 | 规则(≤T) | 复用 |
|---|---|---|
| 大盘 | `amount[T]` 在当日 **buyable** 股前 20% | `df["amount"]` |
| 热门 | 属 top3 热板块(中位数 5 日涨幅 top3,成员 ≥3) | `bt.sector_heat` + `bt.build_sector_members` |
| 龙头 | 板块内 **composite 最高**的 buyable 成分,**top1**;并列取 code 最小(确定性);某股为其任一所属板块的 argmax 即标记龙头 | `score_at.composite` |
| 趋势 | `MA5 > MA20 > MA60` | `analysis.add_ma(:100)` |
| 高位 | `close[T] ≥ 0.97 · max(close[T−59..T])` | — |
| 超跌 | `ret20(T) = close[T]/close[T−20] − 1 < −0.15` | — |
| 反抽 | `ret20(T−1) < −0.15` **且** `close[T] > close[T−1]` | — |
| 震荡 | `amp20 = (max(high[T−19..T]) − min(low[T−19..T])) / close[T] × 100 < 15` | — |

- **分母钉死**:大盘 top20%、龙头 top1、热门 top3 的分母均为**当日 buyable 成员**(引擎只在 buyable 上预测,分层必须落在同池)。
- **薄层小 n**:每个「层 × 维」带 n;n<30 只报样本量、不报指标值(不当信号)。
- **不利事件口径**:`close1 = close[T+1]/close[T] − 1 < −0.03`(与引擎 close1 一致)。

## 6. 模块边界(④)

- **`predict.py`(单一真相源)**:新增两个校准器 + 全部**指标函数**(`_metric_return`、`_metric_risk`、以及复用 §8.1 的 direction/gap/od/trend3/path)。`run_backtest` 返回拟合好的 6 校准器 + valid 记录(每记录:code/date/bar/composite/risk/close1/gap/od/trend3/expected_return/risk_p/预测方向)。
- **`evaluate.py`(只做分层报告)**:调用 `predict.run_backtest` 取校准器与 valid 记录,**不重拟合**;对 valid 记录打 8 层标签,逐层调用 `predict` 的指标函数,输出「分层 × 六维」报告 + 总体。**指标代码只存在一份**(predict 内)。

## 7. 诚实性声明(⑤,报告层)

1. `expected_return` 是 composite 分箱的**阶梯函数**(全宇宙当天 ≤10 个取值),不是个股级回归预测。
2. `expected_return` 与 `risk_p` 分别是 `composite` / `risk` 的**单特征边际**,不是联合条件。
3. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数(同 V1.0 §9 ④)。
4. 大盘层用**原始成交额**(不复权)横截面排名;成交额取自 `stock_zh_a_daily(adjust="qfq")` 的 amount 列(已核:close 前复权、volume/amount 不复权)。

## 8. 版本管理

- `predict.py` MODULE_VERSION → `1.1.0`;新增 `evaluate.py` MODULE_VERSION = `1.0.0`。
- 版本记录条目:修改内容(加 return/risk 校准器)、原因(需求 #6 六维评分)、测试区间(valid 窗口)、准确率(六维 × 八层)、是否保留。

## 9. 测试

- 单测:ReturnCalibrator(每箱 mean、degraded、**无 PAV**)、RiskCalibrator(PAV 单调、无兜底)、每条分层规则(含边界:高位 0.97、超跌 −0.15、震荡 15、反抽 T−1 求值)、每个指标(MAE/符号一致率/方向条件/ECE/lift)。
- 反泄露:改未来 bar → 输出不变(沿用 V1.0 反泄露测试模式)。
- 分层报告:每「层 × 维」带 n;n<30 只报样本量。

## 10. 验收标准

1. 全量测试通过。
2. valid 段生成「八层 × 六维」报告 + 总体;诚实声明 4 条齐全。
3. 不挑易预测样本:分层报告必须**暴露**至少一个弱层(若八层全部高准确率,视为分层定义失效,需重审)。
