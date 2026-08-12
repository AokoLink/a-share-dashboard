# 「可介入龙头」全局视图 设计文档

> 状态:已确认(方案 A 全局汇总表;两档口径=可介入/观察;不过滤板块)
> 日期:2026-08-12

## 1. 背景与目标

用户交易模式:买龙头股、持有 3-5 天。策略核心:只在低风险位置进场,高评分≠能买(哈药股份 95 分被「规避」是现成反例)。

现有工具已提供:
- **发现**:板块条带(每板块 5 只龙头/强势股,`pick_leaders`)。
- **把关**:个股评分(趋势/量价/信号/综合分)+ 风险门(乖离率/量比/长上影)→ 评价(关注/持有跟踪/观望/回调风险/规避)。

**缺口**:没有一个「全市场现在哪些龙头符合进场条件」的聚合视图。单板块内可介入的往往只有 0-2 只,逐板块点开视野太窄。

**目标**:左侧面板新增第三标签页「可介入龙头」,汇总**全部已映射板块**的龙头/强势股,按个股评价过滤为「可介入/观察」两档,一张表看完当前可操作标的。

## 2. 设计决策

| 决策点 | 决定 | 理由 |
|---|---|---|
| 展示形式 | 左侧第三标签页(行业板块 / 推荐 / 可介入龙头),全局汇总表 | 单板块可介入常为 0-2 只,聚合才有视野 |
| 板块范围 | **不过滤板块**:所有已映射板块(约 30-37 个)均纳入,板块评价仅作展示列 | 用户确认「不过滤板块吧」 |
| 龙头来源 | 复用 `pick_leaders`(每板块 5 只,龙头池+强势池合并去重) | 与板块条带口径完全一致 |
| 跨板块去重 | 按 code 去重;先按板块 composite 降序处理,先到先得 | 一只股可能是多板块龙头(如恒瑞),保留最强板块那一条 |
| 个股口径(两档) | **可介入** = 评价「关注」(综合分≥70 且 风险<70);**观察** = 评价「持有/跟踪」(55≤综合分<70 且 风险<70) | 用户确认两档;两者都隐含风险<70,「规避」「回调风险」自动排除 |
| 乖离率 | 展示列:现价偏离 MA20 百分比 | 用户策略的核心入场门槛,需直观可见 |
| 排序 | 可介入在前、观察在后;组内按综合分降序 | 越可操作越靠前 |
| 降级 | 板块未映射/网络失败 → 跳过并计入 `skipped_sectors` 诊断;单只日线失败 → 跳过该股 | 与 recommend 同款降级语义,不阻塞核心视图 |

## 3. 数据流

```
GET /api/actionable-leaders
  → summary(板块列表)+ 全市场 spot               # 与 /api/recommend 同款
  → 全板块 collect_sector_metrics(复用 select_sectors)→ 按 sector composite 降序
  → 逐板块:ds.resolve_sector_constituents(name)
       失败 → skipped(no_mapping/source_fail)
       成功 → codes ∩ spot → pick_leaders(5)     # 龙头池+强势池
  → 全部 leader 并发拉日线 + score_stock + 乖离率    # ≤5×37≈185 只,缓存 1800s
  → 过滤 verdict ∈ {关注, 持有/跟踪} → 映射 tier
  → 按 code 去重(板块 composite 降序先到先得)
  → 排序:可介入→观察,组内 composite 降序
  → 返回 {generated_at, sectors_scanned, total, items, skipped_sectors, diagnostics}
```

## 4. 后端

### 4.1 `recommend.py` 新增

- `tier_for_verdict(verdict)` → `"可介入" | "观察" | None`
  - `"关注"` → `"可介入"`;`"持有/跟踪"` → `"观察"`;其余 → `None`(规避/回调风险/观望一律排除)。
  - 纯函数,单测覆盖四档映射 + None。
- `bias_pct(daily_df, price)` → `float | None`
  - 用 `analysis.add_ma(daily_df, (20,))` 取 MA20;`(price - ma20)/ma20*100`;MA20 无效 → `None`。
  - 纯函数,单测覆盖正常偏离 / 无 MA20 / 空 df。
- `collect_actionable_leaders(summary_df, spot_df, db, type_key, now, resolve_fn, get_daily_fn)` → `(payload, stale_any)`
  - 编排:选板块 → 解析 → pick_leaders → 并发打分 → 过滤/去重/排序 → 组装 payload。
  - `resolve_fn` / `get_daily_fn` 可注入(测试 mock),生产传 `ds.resolve_sector_constituents` / `ds.get_stock_daily`。
  - 复用 `select_sectors`(全板块打分)、`_score_candidate`(打分)、`ThreadPoolExecutor(max_workers=8)`(并发)。
  - `payload = {"generated_at", "sectors_scanned", "total", "items", "skipped_sectors", "diagnostics"}`。
  - `stale_any` = 任一 摘要/spot/个股日线 stale。

### 4.2 `app.py` 新增 `/api/actionable-leaders`

- `summary, stale1 = ds.get_sector_summary(type_key)`;`spot, stale2 = ds.get_market_spot()`;失败 → 500 SOURCE_FAIL(与 api_sectors 同款)。
- 调 `recommend.collect_actionable_leaders(...)`,返回 `ok(payload, stale=stale1 or stale2 or stale_any, extra_meta={coverage, mapping_health})`。
- items 单条结构:
  ```json
  {"code": "sh600276", "name": "恒瑞医药", "price": 54.97, "change_pct": 1.46,
   "tag": "龙头+强势", "tier": "可介入",
   "sector_code": "industry:881142", "sector_name": "生物制品",
   "sector_verdict": "建议关注", "sector_composite": 64.21,
   "trend": 100, "volume_price": 100, "signal": 80,
   "composite": 95.0, "risk": 0, "bias_pct": 3.2}
  ```

## 5. 前端

### 5.1 `templates/index.html`

- 左侧 `.tabs` 追加第三个按钮:`<button class="tab" data-view="actionable">可介入龙头</button>`。
- 追加 `<div id="actionable-panel" class="hidden">`(与 `#reco-panel` 平级),内含 `#actionable-meta`(统计)、`#actionable-table`(表体)、`#actionable-skipped`(诊断,可折叠/灰字)。

### 5.2 `static/app.js`

- `refreshAll` 增补:`loadActionableLeaders()`(与 recommend 同款,60s 自动刷新同步)。
- 新增 `loadActionableLeaders()`:
  - `GET /api/actionable-leaders` → 渲染 `#actionable-table`。
  - 表头:板块 | 板块评价 | 名称 | 代码 | 现价 | 涨跌幅 | 标签 | 档位 | 综合分 | 风险 | 乖离率%。
  - 行 `<tr data-code>` 点击 → `openStock(data-code)`(复用现有个股详情)。
  - 档位徽章:`可介入` 绿底、`观察` 灰底(复用现有 up/down/muted 变量,不新造色)。
  - 涨跌幅列复用 `fmtPct` 红涨绿跌;价格 `toFixed(2)`。
  - 空态:items 空 → 提示「当前无可介入龙头(规避超买追高)」。
  - 失败态:接口 500 → 灰字「可介入龙头拉取失败」。
- Tab 切换逻辑扩展:点击 `data-view="actionable"` → 隐藏 sector-view/reco-panel,显示 actionable-panel,并触发加载。

### 5.3 `static/style.css`

- 追加 `.actionable-table`(复用现有表格/徽章样式变量)、`.tier-badge`、`.tier-buy`/`.tier-watch` 两档配色。

## 6. 测试(全离线)

### `tests/test_recommend.py` 新增
- `test_tier_for_verdict`:关注→可介入;持有/跟踪→观察;观望/回调风险/规避→None。
- `test_bias_pct`:已知 daily(单调上涨,price 高于 MA20)→ 精确乖离或 `abs=0.01`;空 df / 无 MA20 → None。
- `test_collect_actionable_leaders`(mock resolve_fn + get_daily_fn + collect_sector_metrics):
  - 多板块 → pick_leaders 后过滤:规避票被排除、关注/持有跟踪入两档、跨板块同 code 去重、排序可介入在前。
  - resolve 抛异常/no_mapping → 计入 skipped,不 500。
  - 单股日线抛异常 → 跳过该股,其余保留。

### `tests/test_api.py` 新增
- `test_actionable_leaders_endpoint`:复用 `client` fixture + 局部 mock `recommend.collect_actionable_leaders` 返回固定 payload → 200,items 结构/档位/统计正确。
- `test_actionable_leaders_source_fail`:mock `get_sector_summary`/`get_market_spot` 抛 `DataSourceError` → 500 SOURCE_FAIL。
- 其余 API 测试全部离线(mock resolve/daily)。

## 7. 范围外(不做)

- 卖出信号 / 止盈止损规则(策略纪律由用户自定)。
- 分页或虚拟滚动(表量 ≤ ~185,一次性渲染)。
- 改动 `analysis.score_stock` 签名(乖离率在编排层计算,不动现有评分接口与测试)。
- 板块评价过滤开关(用户确认不过滤板块)。

## 8. 全局约束遵循

- 数据源仅 Sina/Tencent/THS:复用 `resolve_sector_constituents`(THS→新浪映射),无新数据源。
- 所有测试离线可跑:resolve/daily/spot/summary 全部 mock。
- 数值字段一律经 `_num()` 归一无;测试对经 `round(...,2)` 的分数断言用精确字面量或 `abs=0.01`。
- 板块打分口径以 `analysis.sector_verdict` 为准(展示列用 `sector_verdict` 原串)。
