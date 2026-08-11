# 股票推荐功能设计(Rev.3)

日期:2026-08-11
状态:Rev.2 后按第二轮修订:① 个股响应补 price/change_pct ② meta.stale 聚合显式化 ③ 不在 spot 快照的候选行为定义 ④ 大跌命名 ⑤ 测试 fixture 注入假校验 ⑥ §9 措辞修正

## 1. 目标与范围

在现有「A股三层分析看板」(大盘 → 板块 → 个股)之上新增一个「推荐」Tab,给出**板块 → 个股双层推荐**:

- 板块层:复用现有 THS(同花顺)行业板块打分,选出"建议关注/跟踪"且综合分最高的前 3 个强势板块。
- 个股层:对每个强势板块的成分股,先用全市场 spot 行情做硬过滤(零额外请求),再对余下候选复用现有 `score_stock` 打分,取 Top 5。

**不做**:个股买卖建议、预测涨跌、信号推送、ML 选股、持仓管理。

## 2. 数据源约束(实测结论)

| 数据源 | 板块打分 | 成分股 |
|---|---|---|
| 同花顺 THS(现有板块层) | ✅ `stock_board_industry_summary_ths` | ❌ 无成分股接口(info 仅返回涨跌汇总) |
| 东方财富 | — | ❌ 本网络被屏蔽(实测 ConnectionError) |
| 新浪行业 | ✅ spot(49 个旧分类板块) | ✅ `stock_sector_detail(sector=label)` 实测可用 |
| 申万 SW | — | ✅ 三级行业成分股(乐咕乐股) |

**结论**:THS 板块打分与成分股来源必须跨分类体系。方案 A(已选):板块层保持 THS 口径,成分股经**映射表(手动 + 关键词兜底)**转新浪行业。申万三级成分股(乐咕乐股)实测可用,但分类过细(335 个三级行业)且与 THS 板块名对应更弱,本次**不使用**,留作扩展。

## 3. 架构与数据流

```
GET /api/recommend
   │
   ├─ 1️⃣ 板块层(复用现有 THS 打分 — 经公共打分函数,见 §4.0)
   │      get_sector_summary(THS) + store 历史 → collect_sector_metrics + score_sector
   │      → verdict / composite
   │      → 取 verdict ∈ {建议关注, 跟踪} 且 composite 最高的前 top_sectors 个
   │      → 过滤无法映射的板块(补位),被跳过的板块带分进 skipped_sectors(§5.2)
   │
   ├─ 2️⃣ 成分股(新增 get_sector_constituents(板块名),TTL 1800s)
   │      THS板块名 → 映射(手动表 → 关键词兜底,均标注 match_type)
   │      映射健康:启动校验 + 运行时覆盖率,见 §5
   │
   ├─ 3️⃣ 个股层(复用 score_stock;日线并发拉取)
   │      每板块成分股:
   │        ① 硬过滤(全市场 spot,零额外请求)
   │        ② ThreadPoolExecutor(max_workers=8) 并发 get_stock_daily + score_stock
   │        ③ 剔除 verdict=="规避" 或 risk≥70 → composite 降序 Top per_sector
   │
   └─ 4️⃣ 返回:sectors(带 match_type) + skipped_sectors(带分) + coverage(meta)
```

## 4. 筛选规则

### 4.0 板块打分复用(重构前置,Rev.2 新增)

现状:`api_sectors` 与 `api_sector` 两个路由**各自内联**调用 `sector_emotion/strength/risk/composite/verdict`。若推荐再复制一份,三处打分口径必然漂移。

重构(行为不变,回归测试兜底):

- `analysis.py` 新增两个函数,成为板块打分的**唯一入口**:
  - `score_sector(metrics) -> {emotion, strength, risk, composite, verdict}`:组合现有 5 个纯函数;
  - `collect_sector_metrics(row, store_ctx, market_turnover, now, data_complete) -> metrics`:把 summary 行 + store 历史值 + 市场成交额聚合为打分输入(含 up_ratio、activity、turnover_ratio 等推导)。
- `store_ctx` 为 duck-typed 访问器(4 个 getter:turnover_avg / prev_change / change_3d / consecutive),测试注入 fake,保持 `analysis.py` 可单测。
- 现有 `api_sectors`/`api_sector` 改为调用上述两函数,删除内联打分。
- `recommend.py` 走**同一路径** → 板块 Tab 与推荐 Tab 打分逐字段一致。

### 4.1 板块层入选口径

- 仅 `建议关注`(P3)与 `跟踪`(P4)入选。
- `谨慎追高(过热)`/`风险提示(回避)`/`警惕一日游`/`观望` 一律排除。
- 按 `composite_score` 降序取前 `top_sectors`(默认 3,范围 1–5)个**可映射**板块;未映射的强势板块补位,且进入 `skipped_sectors`。

### 4.2 个股硬过滤(用全市场 spot 行情,无额外请求)

- 停牌:`volume == 0` 或 `price == 0`(复用 `filter_active`)。
- ST 股:名称含 `ST`。
- 新股:复用 `get_new_stocks()`(该函数本次加缓存,见 §8)。
- 涨停买不进:`change_pct >= limit_threshold(code)`。
- 大跌(排除当日大跌):`change_pct <= -7`。涨停用动态阈值,跌停**不做对称阈值**——创业板/科创板 -7 并非跌停,命名只表"大跌"(对称跌停为可选改法,本次不采用)。
- 流动性差:成交额 `< 1 亿`。
- **不在全市场 spot 快照**(如北交所、新上市、退市整理):无 quote,无法硬过滤也无法打分 → 跳过,计入 `diagnostics.stocks_not_in_spot`(§6/§8)。

### 4.3 个股打分与排序

- `score_stock(daily, quote, now)`,其中 `quote` 取 spot 行的 `{price, change_pct, volume, amount}`(缺 high/low/open → "高位长上影"风险项自动跳过,其余分完整)。
- 剔除 `verdict == "规避"` 或 `risk >= 70`。
- 按 `composite` 降序取前 `per_sector`(默认 5,范围 1–10)。
- 多只候选的日线经 §8 并发拉取后并行打分。

## 5. 映射表与成分股源(风险缓解,Rev.2 重写)

### 5.1 两级映射

- **Tier 1 · 手动表**:`data_source.py` 顶层常量 `SECTOR_CONS_MAP = {THS板块名: 新浪label}`。实现时用一次性脚本对照 THS 板块名与新浪 49 个行业名人工挑选,**目标建全**(对每个有清晰新浪对应的 THS 板块建条目,预期覆盖绝大多数常进 Top3 的板块)。
- **Tier 2 · 关键词兜底**:手动表查不到时,用 THS 板块名在新浪行业名上做匹配——(a) 双向包含规则(新浪名含 THS 名,或反之);(b) 少量同义词语料 `SECTOR_KEYWORDS`(如 `半导体 → 电子设备`)。命中项标记 `match_type: "keyword"`。
  - **多命中视为歧义**:同义词料一次命中多个新浪板块 → 不静默取第一个,记 `skipped(reason=ambiguous)`,保证映射可验证。
- 两级都失败 → `skipped_sectors(reason=no_mapping)`。
- 不做无约束模糊匹配;关键词命中必须满足可验证规则,且经测试评测(§10)。

### 5.2 skipped 带分(透明化机会成本)

`skipped_sectors` 每条含 `{name, composite_score, verdict, reason}`,前端展示"被跳过的最强板块及强度",让用户看到机会成本、决定补哪些映射。

`reason` 枚举:`no_mapping`(两级都失败)/ `stale_map`(映射失效)/ `ambiguous`(关键词多命中)/ `source_fail`(拉取失败)/ `too_few`(候选全被过滤或打分剔除)。

### 5.3 启动校验 + 漂移检测

`validate_sector_map()`(data_source.py),`create_app()` 启动时调用,失败不阻塞(仅告警):

1. 拉新浪行业 spot 的 `(label, name)` 集合;
2. 每个手动 value 的 label 必须存在于当前集合 → 不存在记 `stale`;
3. 每个手动 value 的 label 对应新浪名,与 `SECTOR_CONS_EXPECTED`(label→预期名)比对 → 改名记 `renamed`(分类体系名称漂移信号);
4. 输出 `{total, valid, stale, renamed}` 日志告警。

- 运行时:命中 stale label 的板块自动跳过,`reason=stale_map`(与 `source_fail` 区分)。
- `/api/recommend` meta 含 `mapping_health`:最近一次校验结果。

### 5.4 覆盖率度量

- 运行时 meta 输出 `coverage`:

```jsonc
"coverage": {
  "strong_candidates": 7,          // 今日 verdict∈{建议关注,跟踪} 板块数
  "mapped": 3,                      // 成功返回的板块数
  "skipped": 4,                     // 被跳过板块数
  "skipped_by_reason": {"no_mapping": 2, "stale_map": 1, "source_fail": 1}
}
```

- 维护脚本 `tools/check_sector_map.py`:离线对全部 THS 板块统计手动 / 关键词 / 未映射三级覆盖 + label 有效性,供定期检查映射是否齐全、是否漂移。

## 6. API 设计

```
GET /api/recommend?top_sectors=3&per_sector=5
```

```jsonc
{
  "ok": true,
  "meta": {
    "stale": false, "updated_at": "...",
    "mapping_health": {"total": 48, "valid": 47, "stale": 1, "renamed": 0},
    "coverage": {"strong_candidates": 7, "mapped": 3, "skipped": 4,
                 "skipped_by_reason": {"no_mapping": 2, "stale_map": 1, "source_fail": 1}}
  },
  "data": {
    "generated_at": "2026-08-11 15:05",
    "sectors": [
      {
        "code": "industry:885887", "name": "半导体",
        "verdict": "建议关注", "composite_score": 78,
        "match_type": "keyword",              // "manual" | "keyword"
        "constituent_source": "新浪行业·电子设备",
        "stocks": [
          {"code": "sh600584", "name": "长电科技",
           "price": 38.52, "change_pct": 3.21,
           "scores": {"trend":85,"volume_price":70,"signal":90,"risk":20,"composite":82},
           "verdict": "关注"}
        ]
      }
    ],
    "skipped_sectors": [
      {"name": "白酒", "verdict": "建议关注", "composite_score": 76, "reason": "no_mapping"}
    ],
    "diagnostics": {"stocks_not_in_spot": 3, "stocks_daily_failed": 0}
  }
}
```

- 复用现有 `ok()/err()` 与 stale 透传;数据源异常 → `SOURCE_FAIL` + 现有过期回退。

## 7. 前端「推荐」Tab

- `index.html` 导航新增第 4 个 Tab「推荐」,复用现有 Tab 切换。
- `app.js` 新增 `loadRecommend()`:
  - 板块卡片:板块名 + verdict 徽章 + composite 分 + 成分股表格(代码/名称/**现价**/**涨跌幅**/综合分/风险分/verdict,点击跳转个股详情)。现价/涨跌幅取 spot,与打分同源、零额外请求。
  - 板块标题旁标成分股源口径与 match_type(如"成分股:新浪行业·电子设备[关键词]"——非手动映射时显式标注,提醒可靠性差异)。
  - 底部列出 `skipped_sectors`(灰字 + composite 分 + 原因),并用 meta.coverage 展示"今日强势板块 N 个,已覆盖 M 个"。
  - 顶部免责声明:"仅供研究参考,不构成投资建议。"。
  - 手动刷新 + 60s 自动刷新,沿用现有逻辑。

## 8. 错误处理与性能

### 错误处理

| 场景 | 行为 |
|---|---|
| 手动表 / 关键词兜底均查不到 | `skipped_sectors(reason=no_mapping, 带分)`,补位下一个 |
| 关键词多命中 | `skipped(reason=ambiguous)`,不静默选一个 |
| 映射 label 已失效(启动校验发现) | `skipped(reason=stale_map)`,启动时告警 |
| 新浪成分股拉取失败 | `skipped(reason=source_fail)`,其余板块继续 |
| 单只候选日线失败 | 单只跳过,同板块其余继续,计入 `diagnostics.stocks_daily_failed`;整板块候选全失败 → 板块降级 |
| 成分股不在 spot 快照(北交所/新上市/退市整理) | 跳过,计入 `diagnostics.stocks_not_in_spot` |
| 强势板块不足 | 返回实际数量,前端提示 |
| stale 缓存 | 沿用 `meta.stale` + 前端过期提示 |

**stale 聚合口径**:一次 recommend 涉及约 20 个 `(data, stale)` 源(spot、板块摘要、3 成分股、~15 日线)。`meta.stale = any(各源 stale)`;**任一候选股的数据 stale → 整包标 stale**(前端按现有逻辑提示过期)。测试见 §10.7。

### 性能(Rev.2 优化)

- **日线并发拉取**:候选个股的 `get_stock_daily + score_stock` 经 `ThreadPoolExecutor(max_workers=8)` 并发执行,失败单只跳过。首次加载从约 10–20s 降至约 **3–6s**。
- **`get_new_stocks()` 加缓存**:现为每次调用实时拉取(推荐与大盘都会调)。改为仅对**成功**结果 TTL 缓存 1800s;拉取失败返回空集且**不缓存**(下次仍重试),保持现有容错语义。
- 成分股缓存 1800s;`get_stock_daily` 复用现有 600s 缓存;spot 复用 60s 缓存。
- 并发安全说明:TTLCache 内部加锁,并发 set/evict 安全;`last_updated_at` 为尽力而为时间戳,竞态无害;同码并发重复拉取为良性重复(不做 single-flight,YAGNI)。

## 9. 文件改动清单

| 文件 | 改动 |
|---|---|
| `analysis.py` | + `score_sector(metrics)`、+ `collect_sector_metrics(row, store_ctx, ...)`(板块打分唯一入口) |
| `data_source.py` | + `SECTOR_CONS_MAP`、`SECTOR_CONS_EXPECTED`、`SECTOR_KEYWORDS`、+ `get_sector_constituents(name)`、+ `validate_sector_map()`;`get_new_stocks()` 加成功缓存 |
| `recommend.py`(新) | 纯函数:`select_sectors()`、`filter_candidates()`、`rank_candidates()`;`build_recommend()` 编排(调公共打分 `collect_sector_metrics`/`score_sector` + 并发拉取) |
| `app.py` | `api_sectors`/`api_sector` 改用公共打分函数;+ `GET /api/recommend` 路由;`create_app` 调用 `validate_sector_map` |
| `templates/index.html` | + 推荐 Tab |
| `static/app.js` | + `loadRecommend()` + Tab 绑定 |
| `static/style.css` | 少量推荐卡片样式 |
| `tools/check_sector_map.py`(新) | 离线映射覆盖度 / label 有效性检查 |
| `tests/test_recommend.py`(新) | 见 §10 |
| `tests/test_analysis_sector.py` | + 打分一致性断言 |

## 10. 测试(离线可跑,mock 数据源)

1. **打分复用一致性**:同一输入下,`api_sectors` / `api_sector` / recommend 经公共函数产出的 emotion/strength/risk/composite/verdict **逐字段一致**(重构回归 + 一致性断言)。
2. **映射表**:
   - 完整性:Tier1 key ∈ THS 板块名、value ∈ 新浪 label(mock 集合);
   - `validate_sector_map` 的 stale / renamed 检测;
   - 关键词兜底:包含规则命中、`match_type="keyword"` 标记、歧义多命中 → `ambiguous`、误配样例(如 THS"白酒"不应命中"电子设备")。
3. **`filter_candidates`**:停牌/ST/新股/涨停/大跌/流动性逐条过滤;**不在 spot 快照的候选 → 跳过并计入 `diagnostics.stocks_not_in_spot`**。
4. **`rank_candidates`**:剔除规避、composite 排序、Top N。
5. **请求数兜底**:注入计数 mock fetch,断言 `get_sector_constituents` 每板块恰好 1 次、`get_stock_daily` 每候选恰好 1 次、`get_new_stocks` 缓存后仅 1 次、失败时不缓存。
6. **并发兜底**:mock 慢速 fetch,并发执行候选打分,断言结果按板块正确聚合、单股失败不影响同板块其余、无死锁/重复崩溃。
7. **API 路由**:响应结构、skipped 带分、coverage / mapping_health meta、参数校验(top/per_sector 范围)、**stale 聚合(任一候选股数据 stale → 整包 `meta.stale=true`)**、stocks 对象含 price/change_pct。
8. **测试 fixture 注入假校验**:`create_app()` 启动会调 `validate_sector_map()`;`test_api.py` 的 client fixture 直接 `create_app(...)`,不 mock 会真发新浪请求(即使 try/except 不挂也会拖慢/抖动)。**测试须 mock 该校验**,保持离线确定。
9. **回归**:`pytest tests/ -v` 全绿。
