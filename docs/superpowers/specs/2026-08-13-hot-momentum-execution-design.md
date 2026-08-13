# 「热板块动量选股 + 次日执行口径」设计文档

> 状态:已确认 + 评审修订 v3(二轮评审:10+3 必改、建议、小问题全部吸收)
> 日期:2026-08-13
> 修订依据:用户 P0-P4 提案 + nxday 次日回测证据(`_analysis/nxday_backtest.py` + `nxday_results.json`,2026-08-13 新增)
> 前置决策:① 接受动量,改选股权重(热板块内动量赢滞涨);② 一期全做(六项一个 spec);③ 采用方案一(任何改变选股的机制,阈值/形态由探针产出)

## 1. 背景与问题

v3 评分(commit 7806ed4 + 校准 9af9b98)以「低位滞涨」(position 权重 55%)为核心买点。用户复盘 08-12 推荐输出 + 新增次日回测,提出六项改进。

### 1.1 次日回测证据(`_analysis/nxday_results.json`,230 评估日,1623 只)

| 篮子 | mean 次日od | win | n | 解读 |
|---|---|---|---|---|
| **A 实际管线(热板块×top5)** | **+0.112%** | 51.7% | 209 | 开盘买→收盘卖 |
| A 隔夜 gap | −0.213% | 33.5% | 209 | **66.5% 低开** |
| A close→next close | −0.101% | 45.5% | 209 | 收盘买→次日收盘卖为负 |
| E 板块内低位股(pos 高分) | +0.11% | 52.6% | 209 | position 拉向滞涨 |
| E 板块内高位股(pos 低分) | **+0.477%** | 61.2% | 209 | 热板块内动量反超滞涨 +0.37%/天 |
| B 全市场 v9 top15 | +0.052% | 47.8% | 23 | 采样稀(n=23),仅供参照 |
| C 热板块随机 | **+0.253%** | 55.7% | 230 | 热板块跑赢市场 |
| D 全市场基准 | +0.106% | 58.3% | 230 | 基准 |

核心结论(**v2 修正**):

1. **管线在 od 口径下≈市场基准,不是「正的」**:A +0.112% vs D +0.106%,**零 alpha**。隔夜低开(gap −0.213%,66.5%)只是亏损的表象,真正的缺口是**选股在热板块内主动挑输家**(A +0.112% < C 热随机 +0.253%)。P1/P2 只修执行体验与口径,**不产生 alpha**;P0b 是唯一的价值来源。
2. **热板块内动量赢滞涨是方向性证据,但机制待直接验证**:E 篮子按 `sc["position"]`(60 日位置 50% + 乖离 35% + 平台 15%)排序,**不是** 5 日涨幅分位。「60 日高位但 5 日横盘」与「60 日低位但 5 日大涨」完全不同。E 篮子只能算**旁证**,rel_strength(5 日分位)的操作化必须由 P0b 探针直接验证(见 §3.2、§12)。
3. **热度本身是真信号**(C 热随机 +0.253% > D +0.106%),故过热不该砍分,只该改标签/提示(P0 只改标签不改分)。

### 1.2 各提案证据判定(v2 修正)

| 提案 | 判定 | 依据 |
|---|---|---|
| P0 板块过热纪律 | ⚠️ 改标签不改分 | 热度跑赢市场,砍分丢 alpha;机制漏洞确认:sector_risk 只抓单日>5%/3日>10%,持续阴涨逃过,`consecutive_days` 反而给 strength 加分 |
| P0b 板块内相对强度 | ⚠️ **待 P0b 探针直接验证**(v2) | E 篮子(+0.37%/天)仅旁证(按 60 日 position 排序);A<C 证明「当前 composite 在热板块内挑滞涨」,但 5 日分位操作化本身未验证;叠加 signal 因子内含 ret5,存在双重动量风险(§12 P0b) |
| P1 次日执行确认 | ✅ 体验/口径修复,非 alpha | gap 负 66.5% 实测量出;真正的 alpha 缺口在选股(§1.1 结论 1) |
| P2 校准口径改次日开盘 | ✅ 方法论正确 | 旧 fwd5 close-to-close 掩盖跳空;nxday 即新 harness |
| P3 板块共振门槛 | ⚠️ 探针先行 | 样本小(n=23),阈值待 od 口径校准后定 |
| P4 低价股护栏 | ⚠️ 探针先行 | 08-12 输出确含 2.24/2.30/2.52 元股,但低价是否为独立负因子未验证 |

## 2. 设计原则(v2 增补)

- **探针决定机制**:任何改变选股的机制,其阈值/形态由 `_analysis/` 内探针产出。**探针用多年窗口 + 按年切分**,防 1 年 regime overfit(§6)。
- **探针与生产分离**:探针全在 gitignored `_analysis/`,不 CI 不部署;生产代码只消费探针产出的常量。
- **`analysis.score_stock` 保持纯净**:选股权重变化在 recommend 层聚合阶段完成,不污染单股打分核心。
- **快照生命周期**:信号是可复现的历史事实(某天收盘的推荐),必须持久化后供「次日」对比,不能渲染时从 live daily_df 重算(§5,评审 #2)。

## 3. 设计:P0b 热板块动量权重(改变选股的核心机制)

### 3.1 热板块谓词

`is_hot = sector_composite >= 68`(即 `sector_bonus` 的 +8 档,已校准 P80 锚)。build_recommend 与 collect_actionable_leaders 统一使用。非热板块保持 v3 原权重,保住 §9.2 全市场 position +0.075 的边际。

- **单请求内固定**:`score_all_sectors` 每个请求调用一次,`build_recommend` 全程复用其 `s["composite"]` 快照 → 同一次推荐内热谓词不会漂移。跨请求(60s 刷新)漂移是固有的(与 sector_bonus 现状一致),接受。
- 探针(P2)如显示热板块内 position/rel_strength 的 od 边际支持其他阈值,谓词随之调整。

### 3.2 权重移位(**provisional,探针定稿**)——仅热板块内

| 因子 | v3(现状) | 热板块(provisional) |
|---|---|---|
| position | 0.55 | **0.40** |
| volume_price | 0.15 | 0.15 |
| trend | 0.10 | 0.10 |
| signal | 0.20 | 0.20 |
| **rel_strength(新)** | — | **0.15** |
| 合计 | 1.00 | 1.00 |

**v2 标注**:0.40/0.15 是**拍脑袋初值**,ship 前必须由 P0b 探针在多年窗口、热板块内、od 口径下直接验证,并在三种配置间选优(§12 P0b):

- **(a) v3**:0.55/0.15/0.10/0.20(现状基线)
- **(b) position→signal**:0.40/0.15/0.10/0.35(signal 因子已含 ret5 动量,`_momentum_score(ret5)` analysis.py:389;若 rel_strength 无增量,这是最简替代,避免双重动量)
- **(c) position→rel_strength**:0.40/0.15/0.10/0.20 + 0.15 rel_strength(5 日分位)

探针结论决定 (b) 还是 (c) 落生产;若 (b) 胜,rel_strength 整个砍掉。

### 3.3 rel_strength 定义(v2 修正基数)

`rel_strength = 个股 5 日涨幅在板块成分股中的分位 × 100`(0-100)。有界、对市场环境稳健、可解释。

- 5 日涨幅 = `daily_df.close[-1] / daily_df.close[-6] - 1`(前复权 close,与 nxday 一致)。
- **分位基数 = 该板块通过 `filter_candidates` 且有 ≥6 根日线的成分股**(5 日涨幅只需 6 根)。**与「打分成功集合(≥61 根、risk<70、verdict≠回避)」解耦**——否则小板块分位基数是 3-5 只,噪声极大。基数大于打分集合,分位更稳。
- **并列分位(ties)算法**:`rank(method='average')` 的百分位,即并列值取它们应占秩的**均值**再 ×100/基数(等价 `pandas.DataFrame.rank(pct=True, method='average') × 100`)。不用 min/max(并列时偏向极端),均值秩是标准稳健选择。
- **回退**:基数 <3 只 → `rel_strength = None` → 回退 v3 权重(与 nxday `len(gains) >= 3` 门槛一致)。
- **重叠风险**:signal 因子内 `_momentum_score(ret5)` 已含 5 日动量,rel_strength 是同一 ret5 的板块内分位 → 可能把动量算两次。§12 P0b 探针必须测 rel_strength 相对 (b) 配置的**增量**。

### 3.4 数据流(改动在 `_score_sector_stocks` 聚合阶段)

```
_score_sector_stocks 线程池拉全成分股日线(现状已如此,未新增网络)
  → pass1:每只股票 (基准分, 自身5日涨幅)                        # P0b 字段
  → pass1(同轮):每只股票 (signal_close, close_date)            # P1 出参,见 §5.2
  → 聚合:板块内基数(过滤后成分股, ≥6根)的 5日涨幅 → 分位
  → pass2:rel_strength = 分位×100 → final = stock_composite_v3_rel(...)
```

`analysis.py` 扩展 `stock_composite_v3(position, vp, trend, signal, risk, sector_bonus=0, rel_strength=None)`——`rel_strength=None` 时行为与现状逐位一致(v3 权重),非 None 时走热板块权重。recommend 层传参决定。

## 4. 设计:P0 过热标签(砍分不动,拦截可动;v2 改结构化检测)

### 4.1 检测与展示分离(评审 #6)

`score_sector` 输出结构化布尔 `overheated`:`consecutive_days >= N`(探针定,默认 4)且 `e_hi and s_hi and not r_hi`。**composite 完全不动**(不砍热板块 alpha)。

- **路径 A(拦截)**:verdict 覆盖为**独立标签「过热(连涨)」**,与现有风险驱动的「谨慎追高(过热)」(analysis.py:168)区分。该标签不在 `QUALIFYING_VERDICTS` → 板块从推荐页消失。前端按标签渲染徽章。
- **路径 B(仅徽章)**:verdict 保持「建议关注」,前端按 `overheated` 结构化字段渲染「过热」徽章,保留推荐。
- 探针结果决定 shipping 哪条路径;`overheated` 字段两条路径都输出。

### 4.2 生效顺序(评审小问题)

有效推荐谓词 = `not overheated and verdict in QUALIFYING_VERDICTS`。**过热板块不参与 P0b 热权重、不触发 sector_bonus 加成**——路径 A 下它们根本不进 `build_recommend` 的推荐集合,天然满足;路径 B 下它们仍被推荐,但 `sector_bonus` 与热权重照常(仅徽章提示),该语义写死避免两条路径行为漂移。

## 5. 设计:P1 次日执行确认(v2 快照+open;v3 signal_date/close_date 语义分离 + 相邻性校验)

### 5.1 数据源:spot 补 `open`(评审 #1)

`data_source.get_market_spot()`(data_source.py:212-227)现只选 code/name/price/change_pct/volume/amount 六列,**无今开 → §5.2 的 gap 永远算不出**。加一行 `"open": _pick(raw, "今开", "open")`,并入 `to_numeric` 循环。

### 5.2 快照生命周期(评审 #2,核心;v3 修 signal_date/close_date 语义分离)

**问题**(v2 遗留):快照键曾用 `daily_df 末根日期`,混了两个语义——「推荐在哪天生成」(应 = now 的交易日)与「日线数据滞后到哪天」(daily_df 末根,盘中 = 昨日)。`get_stock_daily` 用 `akshare.stock_zh_a_daily`(data_source.py:259-268)返回**已完成交易日**日线,盘中不含当日。用末根日期做键时:盘中一整天 signal_date 恒等于昨日、60s 刷新反复 upsert 同一 PK,昨晚 17:40 的干净快照被盘中刷新覆盖;且 `get_before(昨日)` 返回**前天**——错位一天。

**改法:两个日期字段分离,键用「生成交易日」**:

| 字段 | 语义 | 取值 |
|---|---|---|
| `signal_date` | 推荐**生成**的交易日(PK) | 由 `now` + `is_trading_time`/`is_after_close` 派生:交易日盘中或收盘后(周一至五且 `is_trading_time(now) or is_after_close(now)`)→ `now.date()`;否则(周末/节假日/开盘前)→ 回退 `close_date` |
| `close_date` | 信号收盘价对应的**日线末根**日期 | 得分股票 daily_df 末根日期的**众数**(非 max——停牌/延迟股末根更早,max 会虚高) |

- **store.py 新增表** `recommend_snapshot(signal_date TEXT PRIMARY KEY, generated_at TEXT, close_date TEXT, prev_trading_date TEXT, payload TEXT)`;`payload` = JSON `[{code, name, signal_close}]`(每股 signal_close 用**各自** daily_df 末根收盘,round 2;停牌股仍用自己更早的末根,不拖累 close_date)。
- **`prev_trading_date`**:`close_date` 之前最近一个交易日,由本次推荐各股 daily_df 的 date 并集(交易日历)派生,存进快照供 §5.3 校验相邻性。
- **build_recommend 出参**加 `signal_date`、`close_date` 与每股 `signal_close`。
- **app.py `api_recommend`**:① build → payload(含 signal_date/close_date);② `prev = store.get_recommend_snapshot_before(payload["signal_date"])`(latest 行 WHERE signal_date < 当前);③ `store.save_recommend_snapshot(signal_date, now, close_date, prev_trading_date, stocks)` upsert(PK 去重,同日重建覆盖);④ prev 每只股票与**今日 spot open**(§5.1)合并算 `gap = open/signal_close − 1`,出参 `prev_snapshot`(含 prev.signal_date / prev.close_date)。

**时间线验证**(解决 v2 两后果):

| 时刻 | signal_date | close_date | save 动作 | get_before |
|---|---|---|---|---|
| 08-12 17:40(收盘后) | 08-12 | 08-12 | 存 PK 08-12 | — |
| 08-13 09:30(盘中) | 08-13 | 08-12(日线未更新) | 存 PK 08-13,**不冲突** | 返回 08-12 ✓ |
| 08-13 09:31 再刷新 | 08-13 | 08-12 | upsert 08-13(幂等) | 返回 08-12 ✓ |

「昨晚(08-12)推荐今早低开多少」:get_before(08-13) 正确命中 08-12,不再错位。

### 5.3 前端(`renderRecommend`)

- **静态免责**(标注出处与时效,评审 #9):「信号基于 {close_date} 收盘;**基于旧策略的历史回测(2025-08~2026-08)**:历史 {209} 个信号日次日开盘 {66.5%} 低开、均值 −{0.21}%。次日开盘执行,勿挂昨日收盘价。P0b 换权重后这些数字将重标。」
- **昨日信号对比块**:渲染 `prev_snapshot` ——「昨日信号(基于 {prev.close_date} 收盘):代码/名称/信号收盘/今日开盘/低开%」,`gap <= -1.5` 时标红 chip「低开 {gap}%,信号撤回/谨慎」。
- **相邻性校验**(评审 #2,核心):`is_next_day = (prev.close_date == 当前 prev_trading_date)`。**仅当 is_next_day 为真**才渲染「次日低开」;否则 prev 是周末/节假日/漏生成前的隔多日快照,`今日 open/signal_close − 1` 是**多日 gap**,不标「次日低开」,改标「隔 N 个交易日」(N = 交易日历中 close_date 到今日的间隔)。
- `prev_snapshot` 为空(首日/无上一期)→ 不渲染;个股 spot 无 open → 跳过该股。

### 5.4 警示阈值(评审 #10)

`-1.5%` 为**临时值**,无分布依据。P1 探针从 nxday gap 分布取分位(如 P20 或 2σ)定稿(§12);临时期间在常量处标注「provisional, P1 探针定稿」。

## 6. 设计:P2 校准口径改「次日开盘买入」(v2 加多年窗口)

`_analysis/` 内扩展:在 fwd5 之外为每个 (股,日) 样本增加 `gap`、`od`、`open→3d`(open[T+1]→close[T+3])三口径。在 **od 目标**上重跑:

1. §9.1 单调性 + §9.2 因子边际——**加「热板块/非热板块」条件切分**,验证 P0b 权重(§3.2 三种配置)。
2. §9.5 verdict 阈值重锚(现 67/62/52/42,od 可介入率目标 20-30%)。
3. §9.4 sector_bonus 档位重锚(现 68/60/50)。

**verdict 阈值决策(评审 #3,双分布复杂度)**:P0b 让热/非热板块走不同权重,position 降权使热板块 composite 整体移位,而 `stock_verdict`(67/62/52/42)与 `tier_for_verdict` 是**全局单阈值**。**决策:保持全局单套阈值,不分治**——理由:① composite 的意义就是跨截面统一排序,若热/非热需不同阈值,说明权重该改而非阈值该分;② 分治两套阈值翻倍校准面、污染 `stock_verdict` 纯函数。代价与约束:**P2 重锚必须额外报告「热板块子样本可介入率」**,确认没被非热板块主导的阈值拉偏;若热板块可介入率系统性偏离 20-30%,那是 §3.2 权重(0.40/0.15)要回调的信号,不是开第二套阈值。

**窗口(评审 #5)**:`_analysis/daily/*.pkl` 含多年历史(qfq 对收益口径有效)。探针窗口从 `tail(260)`(1 年)扩到 **~1200 根(≈5 年)**,评估日采样维持 ~230-300 天以限计算;并**按年切分报告**,确认 P0b 动量结论跨年稳定(2025-08~2026-08 单年是均值回归年,翻动量有 regime overfit 风险——v3 正是全市场 fwd5 校准选出 position 的)。

产出:数值表 → 常量任务落地,旧值留注释。**全计划最贵一步**,在 gitignored 工作区跑,不碰生产。

## 7. 设计:P3 板块共振门槛(探针先行)

- 探针:对吃加成(composite≥60)的样本,按「板块 composite 分桶(≥75 / 68-75 / 60-68)× 个股 quality(<50 / ≥50)」测 od,定位 alpha 所在格。
- 机制(探针定):+8 门槛提到 ≥75?quality<50 不给加成?两者都改?落在 `sector_bonus` + `_score_candidate`。

## 8. 设计:P4 低价股护栏(探针先行)

- 探针:候选集内按股价分桶(<3 / 3-5 / 5-10 / ≥10)测 od,控制 momentum/position 后看低价是否仍有独立负效应。
- 机制(探针定):排除 <3?<3 需更高 rel_strength 分位?不动?落在 `filter_candidates` 或 `_score_candidate`。

## 9. 错误处理(v2 更新)

| 场景 | 行为 |
|---|---|
| 板块内分位基数(过滤后成分股, ≥6根) <3 | `rel_strength=None` → 回退 v3 权重 |
| spot 无 open / 个股不在今日 spot | 该股跳过 gap 合并,`prev_snapshot` 其余股照常 |
| 无上一期快照(首日/首测) | `prev_snapshot` 空,前端不渲染昨日块 |
| concurrent_days 缺失(data_complete=False) | `overheated=False`,不触发(沿用 data_complete 处理) |
| signal_date 为最新(无更早快照) | `prev_snapshot` 空 |
| close_date 含停牌股(末根更早) | close_date 取众数;停牌股 signal_close 用各自末根,不拖累众数 |
| prev.close_date != 当前 prev_trading_date(隔多日) | 不标「次日低开」,改标「隔 N 个交易日」 |
| 重锚常量 | 旧值留注释注明原因(现有 §9.4/§9.5 模式) |

## 10. 测试(v2 更新)

- **单测**:
  - `data_source.get_market_spot` mock 行含 `open`(新增列)。
  - `stock_composite_v3_rel`:rel_strength=None 时与 v3 逐位一致;非 None 时 0.40/0.15/0.10/0.20/0.15。
  - rel_strength 分位:并列分位取均值秩、基数 <3 回退、基数定义(过滤后+≥6根,与 61 根打分解耦)。
  - `sector_verdict` 过热检测:`overheated` 结构化字段;路径 A 独立标签「过热(连涨)」不撞「谨慎追高(过热)」。
  - `sector_bonus` 新门槛(若 P3 探针产出)。
  - store `recommend_snapshot`:upsert 按 signal_date 覆盖、`get_before` 语义、`prev_trading_date` 派生正确。
  - close_date 众数:含停牌股(末根更早)时仍取多数股的最新交易日。
  - app `api_recommend`:prev_snapshot 合并(今日 open × 上一期 signal_close)、gap 计算、`is_next_day` 相邻性判定(相邻 → 次日低开;隔多日 → 隔 N 个交易日)、无上一期→空。
  - app payload 新字段存在性(`signal_date` / `close_date` / `signal_close` / `prev_snapshot`)。
- **探针脚本** gitignored 不 CI,产出常量带钉值单测落地。
- 收尾全量 `pytest` 回归(现 110 green,已核实)。

## 11. 文件改动清单(v2 补 data_source.py、store.py;去「如无」)

**生产代码(唯一非探针部分):**
- `analysis.py`:`stock_composite_v3` 加可选 `rel_strength`;`score_sector` 输出 `overheated` 结构化字段 + 路径 A 独立标签。
- `recommend.py`:`_score_sector_stocks` 两遍聚合(5 日涨幅分位 + 热板块权重);`_score_candidate` 加 `signal_close` / `signal_date` 出参;`sector_bonus` / `filter_candidates`(P3/P4 探针产出时)。
- **`data_source.py`:`get_market_spot` 加 `"open"` 列**(评审 #1)。
- **`store.py`:新增 `recommend_snapshot(signal_date PK, generated_at, close_date, prev_trading_date, payload)` 表 + `save_recommend_snapshot` / `get_recommend_snapshot_before`**。
- `app.py`:api_recommend 的快照持久化 + prev_snapshot 合并出参;payload 直通。
- `static/app.js` + `static/style.css`:昨日信号对比块 + 每股警示 chip + 过热徽章样式。
- `templates/index.html`:复用 `#reco-disclaimer`(index.html:44,已存在)。

**探针(`_analysis/`,gitignored):**
- `nxday_backtest.py` 扩展:多年窗口 + P0 过热切分、P0b 三配置、P3 加成×质量交互、P4 价格分桶、gap 分布分位。
- `spike_v3b.py` 扩展(或姊妹脚本):gap/od/open→3d 三口径 + 热板块条件切分 + 多年窗口。
- 产出数值表 → `_analysis/` 落盘供常量任务消费。

## 12. 探针清单(v2 重排优先级与依赖)

| # | 探针 | 位置 | 决定什么 | 依赖 |
|---|---|---|---|---|
| **P0b** | **5/10/20 日涨幅分位在热板块内的 od 边际(多年窗口,按年切分);三配置 a/b/c 选优** | nxday 扩展 | rel_strength 是否有效、权重 0.40/0.15 定稿还是 0.35/0.20、还是 (b) position→signal | **gate P0b 实现**(§3.2) |
| P0 | 过热(连涨≥N)vs 新鲜热次日 od(多年窗口) | nxday 扩展 | 路径 A(拦截)还是 B(徽章);N 值 | 独立 |
| P1 | nxday gap 分布分位 → 警示阈值 | nxday 扩展 | −1.5% 临时值定稿 | 独立 |
| P2 | od 口径全量重校准(§9.1/9.2/9.5/9.4,多年窗口) | spike_v3b 扩展 | verdict 阈值、sector_bonus 档位重锚、**热板块子样本可介入率(验 §6 双分布未拉偏)** | 消费 P0b 探针 |
| P3 | 加成×质量交互 | nxday 扩展 | +8 门槛 / quality 门控 | 消费 P0b 探针 |
| P4 | 价格分桶条件收益 | nxday 扩展 | 低价护栏形态 | 消费 P0b 探针 |

**执行顺序**:P0b 探针先行(它 gate 生产选股改动,且 P2/P3/P4 都消费其结果);P0/P1 探针并行;P2 全量重校准殿后(最贵)。

## 13. 与现有系统的关系(v2 更新)

- **不破坏**:`analysis.score_stock` 纯净;`stock_composite_v3` 加参后旧调用(无 rel_strength)行为不变;`QUALIFYING_VERDICTS` 逻辑保持(路径 A 用独立标签排除)。
- **已知不一致(评审小问题,接受并标注)**:`/api/stock` 详情页(app.py:242-245)应用 `sector_bonus` 但**无法计算 rel_strength 分位**(需板块内成分股 5 日涨幅,详情页没有)→ 详情页 composite 与推荐列表 composite 不一致。处理:详情页分数标注「未含板块内相对强度」,属既有 sector_bonus 不一致的延伸,接受。
- **单请求快照**:热谓词与 composite 在 `build_recommend` 单请求内固定(§3.1),跨请求漂移接受。
- **校准一致性**:本次所有阈值改动延续 §9.4/§9.5「探针→常量→钉值单测」模式。
- **工作区**:探针产物全部落在 gitignored `_analysis/`,不污染仓库(延续 dd36eb8 的 gitignore 决定)。
