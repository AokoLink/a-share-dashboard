# 「热板块动量选股 + 次日执行口径」设计文档

> 状态:已确认(六区块:A 架构 / B P0b+P0 / C P1+P2 / D P3+P4+错误处理+测试)
> 日期:2026-08-13
> 修订依据:用户 P0-P4 提案 + nxday 次日回测证据(`_analysis/nxday_backtest.py` + `nxday_results.json`,2026-08-13 新增)
> 前置决策:① 接受动量,改选股权重(热板块内动量赢滞涨);② 一期全做(六项一个 spec);③ 采用方案一(任何改变选股的机制,阈值/形态由探针产出)

## 1. 背景与问题

v3 评分(commit 7806ed4 + 校准 9af9b98)以「低位滞涨」(position 权重 55%)为核心买点。用户复盘 08-12 推荐输出 + 新增次日回测,提出六项改进。逐项证据判定:

### 1.1 次日回测证据(`_analysis/nxday_results.json`,230 评估日,1623 只)

| 篮子 | mean 次日od | win | n | 解读 |
|---|---|---|---|---|
| **A 实际管线(热板块×top5)** | **+0.112%** | 51.7% | 209 | 开盘买→收盘卖**为正** |
| A 隔夜 gap | **−0.213%** | 33.5% | 209 | **66.5% 低开**,亏损全部来源 |
| A close→next close | −0.101% | 45.5% | 209 | 收盘买→次日收盘卖为负 |
| E 板块内低位股(pos 高分) | +0.11% | 52.6% | 209 | position 拉向滞涨 |
| E 板块内高位股(pos 低分) | **+0.477%** | 61.2% | 209 | 热板块内动量反超滞涨 **+0.37%/天** |
| B 全市场 v9 top15 | +0.052% | 47.8% | 23 | 采样稀(n=23),仅供参照 |
| C 热板块随机 | **+0.253%** | 55.7% | 230 | **热板块跑赢市场** |
| D 全市场基准 | +0.106% | 58.3% | 230 | 基准 |

三条核心结论:

1. **管线在「次日开盘买入」口径下是正的**(od +0.112%),全部亏损来自隔夜跳空(gap −0.213%,66.5% 低开)→ 执行口径与提示直接修复收益(P1/P2)。
2. **热板块内动量赢滞涨 +0.37%/天**(E 高位 +0.477% vs 低位 +0.11%),且 A 实际管线跑输 C 热随机(+0.112% vs +0.253%)——composite 的 position 倾斜在热板块内**主动挑输家**(P0b)。
3. **热度本身是真信号**(C 热随机 +0.253% > D +0.106%),故过热不该砍分,只该改标签/提示(P0 只改标签不改分)。

### 1.2 各提案证据判定

| 提案 | 判定 | 依据 |
|---|---|---|
| P0 板块过热纪律 | ⚠️ 改标签不改分 | 热度跑赢市场,砍分丢 alpha;机制漏洞确认:sector_risk 只抓单日>5%/3日>10%,持续阴涨逃过,`consecutive_days` 反而给 strength 加分 |
| P0b 板块内相对强度 | ✅ 数据最硬 | E 篮子 +0.37%/天;A<C 证明选股在挑滞涨 |
| P1 次日执行确认 | ✅ 零风险 | gap 负 66.5% 实测量出 |
| P2 校准口径改次日开盘 | ✅ 方法论正确 | 旧 fwd5 close-to-close 掩盖跳空;nxday 即新 harness |
| P3 板块共振门槛 | ⚠️ 探针先行 | 样本小(n=23),阈值待 od 口径校准后定 |
| P4 低价股护栏 | ⚠️ 探针先行 | 08-12 输出确含 2.24/2.30/2.52 元股,但低价是否为独立负因子未验证 |

## 2. 设计原则

- **探针决定机制**:任何改变选股的机制,其阈值/形态由 `_analysis/` 内探针产出,不在设计时拍脑袋。探针结果落地为带注释的常量值 + 单测钉值(沿用 §9.4/§9.5 重锚模式)。
- **探针与生产分离**:探针全在 gitignored `_analysis/`,不 CI 不部署;生产代码只消费探针产出的常量。
- **`analysis.score_stock` 保持纯净**:选股权重变化在 recommend 层聚合阶段完成,不污染单股打分核心。

## 3. 设计:P0b 热板块动量权重(改变选股的核心机制)

### 3.1 热板块谓词

`is_hot = sector_composite >= 68`(即 `sector_bonus` 的 +8 档,已校准 P80 锚)。build_recommend 与 collect_actionable_leaders 统一使用。非热板块保持 v3 原权重,保住 §9.2 全市场 position +0.075 的边际。

> 探针(P2)如显示热板块内 position/rel_strength 的 od 边际支持其他阈值,谓词随之调整。

### 3.2 权重移位(仅热板块内)

| 因子 | v3(现状) | 热板块(新) |
|---|---|---|
| position | 0.55 | **0.40** |
| volume_price | 0.15 | 0.15 |
| trend | 0.10 | 0.10 |
| signal | 0.20 | 0.20 |
| **rel_strength(新)** | — | **0.15** |
| 合计 | 1.00 | 1.00 |

15 分全部从 position 扣,其余三项不动。

### 3.3 rel_strength 定义

`rel_strength = 个股 5 日涨幅在板块成分股中的分位 × 100`(0-100)。有界、对市场环境稳健(普涨年不被 z-score 放大)、可解释。

- 5 日涨幅 = `daily_df.close[-1] / daily_df.close[-6] - 1`(前复权 close,与 nxday 一致)。
- 板块成分股集合 = 该板块**过滤后**成分股(`filter_candidates` 输出,与打分集合一致)。
- **回退**:板块内打分成功的成分股 <3 只 → `rel_strength = None` → 回退 v3 权重(与 nxday `len(gains) >= 3` 门槛一致)。

### 3.4 数据流(改动在 `_score_sector_stocks` 聚合阶段)

```
_score_sector_stocks 线程池拉全成分股日线(现状已如此,未新增网络)
  → pass1:每只股票 (基准分, 自身5日涨幅)
  → 聚合:板块内 5日涨幅中位数 / 分位
  → pass2:rel_strength = 分位×100 → final = stock_composite_v3_rel(...)
```

`analysis.py` 新增可选变体 `stock_composite_v3(position, vp, trend, signal, risk, bonus, rel_strength=None)`——`rel_strength=None` 时行为与现有 `stock_composite_v3` 逐位一致(v3 权重),非 None 时走热板块权重。recommend 层传参决定。

## 4. 设计:P0 过热标签(砍分不动,拦截可动)

### 4.1 机制

`sector_verdict` 加分支:当 `consecutive_days >= N`(探针定,默认 4)且 `e_hi and s_hi and not r_hi` 时,覆盖 P3 → 返回「谨慎追高(过热)」。**composite 完全不动**(不砍热板块 alpha)。

### 4.2 产品效果与双路径

「谨慎追高(过热)」不在 `QUALIFYING_VERDICTS`(建议关注/跟踪)→ 该板块从推荐页消失(符合用户「不再作为推荐」原意)。代价:若探针显示过热板块次日仍跑赢,拦截丢 alpha。故:

- **P0 探针**:nxday 加条件——按板块连涨天数(consecutive 代理)/ MA 乖离切分,测「过热 vs 新鲜热」的次日 od。
- **路径 A**(过热显著跑输)→ 翻转标签拦截(§4.1)。
- **路径 B**(过热仍跑赢)→ 降级为**展示徽章**「过热」,保留推荐但提示纪律(verdict 展示改、QUALIFYING_VERDICTS 不动)。

探针结果决定 shipping 哪条路径。

## 5. 设计:P1 次日执行确认(前端为主,服务端补两字段)

### 5.1 服务端(`_score_candidate` 出参)

- `signal_close`:`daily_df` 末根收盘(信号收盘价,round 2)。
- `next_gap`:仅当 `daily_df 末根日期 < 今日`(信号已过期、spot 的 open 才是「次日开盘」)且 spot 行有 `open` 字段时,`open/signal_close − 1`(round 2 百分数);信号当天或数据缺失 → `None`。
  - **语义防错**:信号当天 spot open 是当天开盘,与信号收盘比无意义,必须日期判空。

### 5.2 前端(`renderRecommend`)

- 页头 `#reco-disclaimer` 加一行(数值标注出处):「信号基于 {generated_at} 收盘;历史 {209} 个信号日次日开盘 {66.5%} 低开、均值 −{0.21}%。**次日开盘执行,勿挂昨日收盘价**。」
- 每股:当 `next_gap <= -1.5` 时,结论列旁加红色警示 chip「次日低开 {gap}%,信号撤回/谨慎」。
- 字段缺失前端不渲染(优雅降级)。

## 6. 设计:P2 校准口径改「次日开盘买入」

`_analysis/` 内扩展:在 fwd5 之外为每个 (股,日) 样本增加 `gap`、`od`、`open→3d`(open[T+1]→close[T+3])三口径。在 **od 目标**上重跑:

1. §9.1 单调性 + §9.2 因子边际——**加「热板块/非热板块」条件切分**,验证 P0b 的 0.40/0.15 权重(数据支持保持,支持 0.35/0.20 则调)。
2. §9.5 verdict 阈值重锚(现 67/62/52/42,od 可介入率目标 20-30%)。
3. §9.4 sector_bonus 档位重锚(现 68/60/50)。

产出:数值表 → 常量任务落地,旧值留注释。**全计划最贵一步**(全量因子遍历,小时级),在 gitignored 工作区跑,不碰生产。

## 7. 设计:P3 板块共振门槛(探针先行)

- 探针:对吃加成(composite≥60)的样本,按「板块 composite 分桶(≥75 / 68-75 / 60-68)× 个股 quality(<50 / ≥50)」测次日 od,定位 alpha 所在格。
- 机制(探针定):+8 门槛提到 ≥75?quality<50 不给加成?两者都改?落在 `sector_bonus` + `_score_candidate`。

## 8. 设计:P4 低价股护栏(探针先行)

- 探针:候选集内按股价分桶(<3 / 3-5 / 5-10 / ≥10)测次日 od,控制 momentum/position 后看低价是否仍有独立负效应。
- 机制(探针定):排除 <3?<3 需更高 rel_strength 分位?不动?落在 `filter_candidates` 或 `_score_candidate`。

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| 板块内打分成功成分股 <3 | `rel_strength=None` → 回退 v3 权重 |
| spot 无 open / 信号当天 | `next_gap=None`,前端不渲染警示 |
| consecutive_days 缺失(data_complete=False) | P0 过热不翻转(沿用 data_complete 处理) |
| 重锚常量 | 旧值留注释注明原因(现有 §9.4/§9.5 模式) |

## 10. 测试

- **单测**:
  - `stock_composite_v3_rel`:rel_strength=None 时与 v3 逐位一致;非 None 时权重 0.40/0.15/0.10/0.20/0.15。
  - rel_strength 分位边界:并列分位、成分股 <3 回退。
  - `sector_verdict` 过热分支(consecutive_days≥N + e_hi + s_hi → 谨慎追高(过热);composite 不变)。
  - `sector_bonus` 新门槛(若 P3 探针产出)。
  - `_score_candidate` next_gap 两态:信号当天(→None)、信号过期+spot 有 open(→gap 值)。
  - app payload 新字段存在性(`signal_close` / `next_gap`)。
- **探针脚本** gitignored 不 CI,产出常量带钉值单测落地。
- 收尾全量 `pytest` 回归(现 110 green)。

## 11. 文件改动清单

**生产代码(唯一非探针部分):**
- `analysis.py`:`stock_composite_v3` 加可选 `rel_strength` 参数;`sector_verdict` 加过热分支。
- `recommend.py`:`_score_sector_stocks` 两遍聚合(5 日涨幅中位数/分位 + 热板块权重);`_score_candidate` 加 `signal_close` / `next_gap` 出参;`sector_bonus` / `filter_candidates`(P3/P4 探针产出时)。
- `app.py`:payload 直通(如需要)。
- `static/app.js` + `static/style.css`:`#reco-disclaimer` + 每股警示 chip + 过热徽章样式。
- `templates/index.html`:disclaimer 容器(如无)。

**探针(`_analysis/`,gitignored):**
- `nxday_backtest.py` 扩展:P0 过热切分、P3 加成×质量交互、P4 价格分桶。
- `spike_v3b.py` 扩展(或姊妹脚本):gap/od/open→3d 三口径 + 热板块条件切分。
- 产出数值表 → `_analysis/` 落盘供常量任务消费。

## 12. 探针清单(6 项)

| # | 探针 | 位置 | 决定什么 |
|---|---|---|---|
| P0 | 过热(连涨≥N)vs 新鲜热次日 od | nxday 扩展 | 拦截(路径 A)还是徽章(路径 B);N 值 |
| P0b | 热板块内 position vs rel_strength 的 od 边际 | spike_v3b 扩展 | 0.40/0.15 权重确认或微调 |
| P1 | (无探针,数值已定) | — | 免责声明数值 + 警示阈值 −1.5% |
| P2 | od 口径全量重校准(§9.1/9.2/9.5/9.4) | spike_v3b 扩展 | verdict 阈值、sector_bonus 档位重锚 |
| P3 | 加成×质量交互 | nxday 扩展 | +8 门槛 / quality 门控 |
| P4 | 价格分桶条件收益 | nxday 扩展 | 低价护栏形态 |

## 13. 与现有系统的关系

- **不破坏**:`analysis.score_stock` 纯净;`stock_composite_v3` 加参后旧调用(无 rel_strength)行为不变;`QUALIFYING_VERDICTS` 逻辑保持(除非 P0 路径 A 翻转标签)。
- **校准一致性**:本次所有阈值改动延续 §9.4/§9.5「探针→常量→钉值单测」模式。
- **工作区**:探针产物全部落在 gitignored `_analysis/`,不污染仓库(延续 dd36eb8 的 gitignore 决定)。
