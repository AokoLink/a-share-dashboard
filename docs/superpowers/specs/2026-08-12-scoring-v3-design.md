# 「个股评分 v3」设计文档

> 状态:已确认(方案 B 全面重构;位置中心型 v3;乘法风险折扣;板块共振进分;verdict 五档;真实数据回测校准)
> 日期:2026-08-12

## 1. 背景与问题

现有个股评分(analysis.py)对用户「买龙头、持 3-5 天」的策略把关,但存在系统性缺陷:

1. **区分度差**:趋势分只有 40/70/90/100 四档、量价分 40/70 两档,大量股票挤在 90-100,「刚金叉弱多头」与「均线充分发散」同分。
2. **维度缺位**:复盘(2026-08-12)发现的「低位启动、板块共振、量能持续」等大涨共性,评分一个都没用上。
3. **综合分与实操割裂**:风险只当 verdict 硬门槛、不进综合分,风险 0 和 65 同分;出现「95 分却规避」的哈药股份式反例。
4. **方向性错误**(回测证据,见 §2):v1 重奖趋势/信号,而两者在 5 日/30 日口径下是负向因子——「已涨的」被排到最高分,等于引导追高。

## 2. 回测校准证据

数据:37 个已映射板块成分股并集 1769 只,新浪前复权日线;两个口径:

- **单窗口**(2026-07 上旬 → 08-12,163 只深复权样本):位置/乖离/趋势/信号与后续 30 日涨幅的 spearman 分别为 −0.54/−0.47/−0.49/−0.46(**位置是最强反向预测**),量价 +0.06(唯一正向)。
- **全年滚动**(1623 只 × 近 300 根,每 5 根评估一次,53,537 条观测,后续 5 日涨幅):

| 方案 | pooled spearman(5日) | 最高十分位 5日均值 | 最低十分位 5日均值 |
|---|---|---|---|
| **v1(现状)** | **−0.017** | +0.13 | 杂乱不单调 |
| v2(原始草案) | +0.048 | +0.56 | −0.26 |
| **v3(位置中心型)** | **+0.064** | **+0.39** | −0.21 |

结论:
- 个股 5 日收益大部分是噪声,任何纯价量因子 edge 都有限(spearman 0.06 级)属正常;但 **v1 是负的**——新模型至少要转正并消除「追高接盘」的负 alpha。
- **位置(低 60 日区间位置、乖离甜区)是全年最稳健的正向方向**,单因子 spearman +0.071。
- 风险折扣方向正确(v3 计入风险后 0.064 > 不计 0.039)。
- 上个月是「低位均值回归」极端风格,单窗口强相关不可外推;全年池化才是校准基准。

## 3. 设计决策

| 决策点 | 决定 | 理由 |
|---|---|---|
| 改造路线 | 方案 B 全面重构 | 用户选定;v1 方向性错误需重做,非调参可解 |
| 综合分结构 | **质量分 × max(0, 1−风险/100)** | 乘法折扣让风险直接压低分数,高分高风险自动低分,消除割裂 |
| 主因素 | 位置(40%)/量价(20%)/趋势结构(15%)/信号(25%) | 回测证据:位置最强且方向正确;趋势只取「结构」不含发散,避免奖励延伸 |
| 风险 | 累加、cap 100,作折扣系数 | 多个风险信号并存时叠加;不再只取 max |
| 板块共振 | 进综合分,编排层加成 | 复盘「板块共振」共性;用现有 THS 板块数据,不引新源 |
| verdict | 五档:强烈关注/关注/持有跟踪/观望/回避 | 用户选定;阈值由校准任务用当前全市场分布定 |
| 可介入两档 | 强烈关注/关注 → 可介入;持有跟踪 → 观察;观望/回避 → 排除 | 保持已确认的两档视图语义 |
| 参数校准 | 真实数据回测校准 | 用户选定;校准作为实施任务,输出报告 |

## 4. 评分模型 v3(纯函数,analysis.py)

`score_stock(daily_df, quote, now)` 签名不变,返回 dict 扩展为:
`{"position", "trend", "volume_price", "signal", "risk", "composite", "verdict"}`。
(趋势结构对外仍叫 `trend`,语义改为「结构分」。)

### 4.1 位置 position(0–100,权重 40%)

```
pos60 = (close − min(low[-60:])) / (max(high[-60:]) − min(low[-60:]))   # 60 日区间百分位;分母为 0 → 0.5
pos_factor = 100 × (1 − pos60)
bias = (close − MA20) / MA20 × 100
bias_sweet(乖离甜区):
  bias < −8            → 0
  −8 ≤ bias < −3       → 15
  −3 ≤ bias < 0        → 35
  0 ≤ bias ≤ 8         → min(100, 100 − max(0, (bias−3))×12)   # 峰值在 +3%
  8 < bias ≤ 15        → 40 − (bias−8)×4                        # 线性衰减
  bias > 15            → 5
platform(近 10 日振幅):amp=(max(high[-10:])−min(low[-10:]))/close[-11]×100
  amp ≤ 8 → 30; 8<amp≤15 → 15; amp>15 → 0
position = min(100, 0.5×pos_factor + 0.35×bias_sweet + 0.15×platform)
```

### 4.2 量价 volume_price(0–100,权重 20%)

```
vr(量比) = 当日volume / mean(volume[-6:-1])
分项一(量比健康度):vr≤1.5 → +35; 1.5<vr≤2.5 → +26; vr>2.5 → +12; vr缺失 → +12
分项二(价量一致):chg=当日涨跌幅%
  chg>1 且 vr>1.2 → +35; chg>0 且 vr<0.8 → +15; chg<−1 且 vr>1.2 → +5; 其余 → +25
分项三(量能持续):a5=mean(volume[-5:]), b20=mean(volume[-25:-5])
  a5/b20 ≥1.5 → +12; ≥1 → +30; ≥0.7 → +20; 其余 → +6
cap 100
```

### 4.3 趋势结构 trend(0–100,权重 15%)

```
order = (MA5>MA10) + (MA10>MA20) + (MA20>MA60)          # 各为 0/1
base = 40 × order/3
fresh:近 5 根内 MA5 上穿 MA10(昨≤今>) → fresh≥1 → +15; 否则 MA5>MA10 → +8; 否则 +0
cap 100
```
(不含发散度/乖离奖励——发散与乖离由位置因子负责,避免重复奖励「已涨」。)

### 4.4 信号 signal(0–100,权重 25%)

```
MACD:金叉且DIF>0 → +25; DIF>DEA → +30(若 pos60<0.5)否则 +15; 其余 → +0
RSI14:r≤65 → +25; 65<r≤80 → +12; r>80 → +5      # r<30 落入 ≤65 → 超卖修复加分
突破:close > max(high[-21:-1]) 且 vr>1.2 → +20; close>前20日高(不放量) → +10; 否则 +0
动量:ret5=(close/close[-6]−1)×100; 0≤ret5≤8 → +15; −3≤ret5<0 → +8; 其余 → +3
cap 100, floor 0
```

### 4.5 风险 risk(0–100,累加 cap)

```
bias>15 → +40; bias>25 → +30(可叠加,最高 70)
vr>1.5 且 close<MA20 且 chg<0 → +40
vr>1.5 且 chg < 前日涨幅 且 chg<2 → +25
高位长上影:上影>2×实体 且 振幅>5% → +20
近 20 日最大回撤>25% → +20
cap 100
```

### 4.6 综合分与 verdict

```
质量分 = 0.40×position + 0.20×volume_price + 0.15×trend + 0.25×signal
composite = round(质量分 × max(0, 1 − risk/100), 2)

stock_verdict(composite):        # 纯函数,仅依赖 composite
  ≥68 → 强烈关注; ≥58 → 关注; ≥48 → 持有/跟踪; ≥38 → 观望; <38 → 回避

tier_for_verdict(verdict):       # 两档映射
  {"强烈关注","关注"} → 可介入; {"持有/跟踪"} → 观察; 其余 → None
```

verdict 阈值(68/58/48/38)为**初值**,由校准任务(§9)用当前全市场分布复核并给出报告。

## 5. 板块共振(编排层,recommend.py)

板块共振不进 analysis.py(纯函数无板块信息),在打分编排层应用:

```
给定股票已解析到的板块(可多个),取 sector composite 最高的那个:
  ≥70 → 综合分 +8
  55 ≤ x < 70 → +4
  x < 40 → −5
  其余 → 0
final_composite = clamp(composite + 板块调整, 0, 100),round 2
verdict = stock_verdict(final_composite)
```

- 板块 composite 来自现有 `score_all_sectors`(全板块打分,60s 缓存),不新增数据源。
- **应用位置**:
  - `collect_actionable_leaders` / `build_recommend`:leader 自带 `sector_name` → 查该板块 composite。
  - `/api/stock` 个股详情:**尽力而为**——新增 `data_source.resolve_code_sectors(code)` 反向解析(手动映射表优先,关键词兜底,缓存),解析成功才加成;失败 → 0 不阻塞。
- 影响面:tier 判定在板块加成之后,强势板块可把 55 分股推入「可介入」,符合「板块共振」语义。

## 6. 数据流

```
score_stock(daily, quote, now)          # 纯函数:4 因子 + 风险 + composite + verdict(不含板块)
  → collect_actionable_leaders / build_recommend / api_stock
  → 板块共振调整 final_composite → verdict → tier
  → 前端展示(新增 位置 列、新 verdict 标签、板块共振加成后的分数)
```

## 7. 后端变更

- **analysis.py**:
  - 重写 `compute_trend_score` → 趋势结构(§4.3);新增 `compute_position_score`(§4.1);重写 `compute_volume_price_score`(§4.2)、`compute_signal_score`(§4.4)、`compute_stock_risk`(§4.5 累加)。
  - 新增 `stock_composite_v3`(或改 `stock_composite` 签名 → 四因子+风险);重写 `stock_verdict`(五档);`score_stock` 返回扩展 dict。
  - 新增辅助:`rsi14`、`max_drawdown_20`(纯函数)。
  - 旧函数 `compute_trend_score`/`compute_volume_price_score`/`compute_signal_score`/`compute_stock_risk`/`stock_composite`/`stock_verdict` 的**旧签名全部移除**(方案 B),调用方与测试同步更新。
- **data_source.py**:新增 `resolve_code_sectors(code)` → 板块名列表(手动表→关键词兜底,缓存 1800s),复用现有 `SECTOR_CONS_MAP`/`_keyword_lookup`。
- **recommend.py**:`tier_for_verdict` 改映射(§4.6);`_score_candidate`/`collect_actionable_leaders`/`build_recommend` 增加板块共振调整(§5)。
- **app.py**:`/api/stock` 用 `resolve_code_sectors` 尽力应用板块共振;返回 dict 带 `position`。

## 8. 前端变更

- **templates/index.html**:个股详情/可介入龙头表新增「位置」列(如需要);verdict 标签按新五档渲染(后端返回即展示,改动主要为表头)。
- **static/app.js**:渲染新增 `position` 字段;`tier_for_verdict` 相关文本(`规避`→`回避`)同步;可介入龙头空态文案若引用旧标签则更新。
- **static/style.css**:如新增 verdict 徽章色(强烈关注)则补充,复用现有变量。

## 9. 校准任务(实施一部分,交付报告)

目标:证明并落地「客观合理」,不只靠设计文档。

1. **单调性回归验证**:复用已下载的 1623 只日线(或重新拉取),跑与 §2 相同的滚动回测(评估点每 5 根、后续 5 日),断言**最终实现的 `score_stock` 综合分 pooled spearman ≥ 0.04**(显著高于 v1 的 −0.017)且**十分位单调**(最高档均值 > 最低档)。输出校准报告 `_analysis/calibration_report.md`(git 忽略)。
2. **阈值校准**:对当前全市场 universe 算 v3 综合分分布,复核 68/58/48/38 五档划分是否合理(可介入层约 top 20–30%);不合理则给出调整值与理由。
3. 校准脚本放 `_analysis/`(git 忽略),实现代码在 `analysis.py`(可测)。

## 10. 测试策略(全离线)

- `tests/test_analysis_stock.py`:重写各因子测试为**确定性精确断言**(分段函数输入确定 → 输出精确字面量或 abs=0.01);新增 position/板块调整无关的纯函数测试。
- `tests/test_recommend.py`:mock `score_stock`(如同现状 test_recommend.py:340-345 的模式),更新 tier 映射断言;板块共振调整单测(给 sector composite → 断言 final_composite/verdict)。
- `tests/test_api.py`:个股详情返回含 `position`;板块共振尽力模式(解析失败 → 0,不 500)。
- 数值字段一律经 `_num()` 归一无;对 `round(...,2)` 结果断言用精确字面量或 `abs=0.01`。

## 11. 范围外(不做)

- 卖出信号/止盈止损规则(策略纪律用户自定)。
- 板块打分 P1–P6 重构(已是三因素连续分,不在本次范围)。
- 市值/换手率/北向/龙虎榜(需其他数据源,东财被禁)。
- 机器学习/正则化模型(黑盒,用户要求「客观可解释」)。
- 多窗口超参数寻优(防止过拟合到单月风格;以 §2 全年滚动口径为校准基准)。

## 12. 全局约束遵循

- 数据源仅 Sina/Tencent/THS:新增的 `resolve_code_sectors` 复用现有手动映射表 + 关键词兜底,无新数据源;东财接口不引入。
- 所有测试离线可跑:analysis.py 纯函数输入注入;板块共振在编排层 mock。
- 数值字段一律 `_num()` 归一无;round(...,2) 断言用精确字面量或 abs=0.01。
- Git:直接在 `main` 分支工作,每任务独立 commit,末尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。
