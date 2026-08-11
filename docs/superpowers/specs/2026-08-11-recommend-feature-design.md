# 股票推荐功能设计

日期:2026-08-11
状态:已获用户确认(第 1/2/3 节逐节通过)

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

**结论**:THS 板块打分与成分股来源必须跨分类体系。方案 A(已选):板块层保持 THS 口径,成分股经**手工映射表**转新浪行业。

## 3. 架构与数据流

```
GET /api/recommend
   │
   ├─ 1️⃣ 板块层(复用现有 THS 打分,无新增网络调用)
   │      get_sector_summary(THS) + store 历史 → sector_emotion/strength/risk
   │      → composite + verdict
   │      → 取 verdict ∈ {建议关注, 跟踪} 且 composite 最高的前 3 个
   │      → 过滤无法映射成分股的板块(补位下一个)
   │
   ├─ 2️⃣ 成分股(新增 data_source.get_sector_constituents(板块名))
   │      THS板块名 → SECTOR_CONS_MAP → 新浪行业 label → stock_sector_detail(label)
   │      TTL 缓存 30 分钟
   │
   ├─ 3️⃣ 个股层(复用现有 score_stock)
   │      对每个候选成分股:
   │        ① 硬过滤(全市场 spot,零额外请求)
   │        ② score_stock(daily, spot行情dict, now)  ← 每只 1 次日线请求
   │        ③ 剔除 verdict=="规避" 或 risk≥70 → composite 降序取 Top 5
   │
   └─ 4️⃣ 返回:强势板块列表(每板块含 Top5 个股) + skipped_sectors
```

## 4. 筛选规则

### 4.1 板块层入选口径

- 仅 `建议关注`(P3)与 `跟踪`(P4)入选。
- `谨慎追高(过热)`/`风险提示(回避)`/`警惕一日游`/`观望` 一律排除。
- 按 `composite_score` 降序取前 `top_sectors`(默认 3,范围 1–5)个**可映射**板块;未映射的强势板块补位。

### 4.2 个股硬过滤(用全市场 spot 行情,无额外请求)

- 停牌:`volume == 0` 或 `price == 0`(复用 `filter_active`)。
- ST 股:名称含 `ST`。
- 新股:复用 `get_new_stocks()`。
- 涨停买不进:`change_pct >= limit_threshold(code)`。
- 跌停/大跌:`change_pct <= -7`。
- 流动性差:成交额 `< 1 亿`。

### 4.3 个股打分与排序

- `score_stock(daily, quote, now)`,其中 `quote` 取 spot 行的 `{price, change_pct, volume, amount}`(缺 high/low/open → "高位长上影"风险项自动跳过,其余分完整)。
- 剔除 `verdict == "规避"` 或 `risk >= 70`。
- 按 `composite` 降序取前 `per_sector`(默认 5,范围 1–10)。

## 5. 映射表(方案 A 核心)

- 位置:`data_source.py` 顶层常量 `SECTOR_CONS_MAP = {THS板块名: 新浪label}`。
- 生成:实现时写一次性脚本,对照 THS 板块名与新浪 49 个行业名,产出约 30–40 条初始映射(覆盖市值大、常进 Top3 的板块)。
- 查找:`get_sector_constituents(ths_name)` 查到 → 拉新浪成分股(缓存 1800s);查不到 → `None`,上层标记 `no_mapping`。
- 不做模糊匹配,宁缺毋滥,避免跨分类体系硬凑。
- 申万三级成分股(乐咕乐股)实测可用,但分类粒度过细(335 个三级行业)且与 THS 板块名对应更弱,本次**不使用**;留作未来扩展。

## 6. API 设计

```
GET /api/recommend?top_sectors=3&per_sector=5
```

```jsonc
{
  "ok": true,
  "meta": {"stale": false, "updated_at": "..."},
  "data": {
    "generated_at": "2026-08-11 15:05",
    "sectors": [
      {
        "code": "industry:885887", "name": "半导体",
        "verdict": "建议关注", "composite_score": 78,
        "stocks": [
          {"code": "sh600584", "name": "长电科技",
           "scores": {"trend":85,"volume_price":70,"signal":90,"risk":20,"composite":82},
           "verdict": "关注"}
        ]
      }
    ],
    "skipped_sectors": [{"name": "白酒", "reason": "no_mapping"}]
  }
}
```

- 复用现有 `ok()/err()` 与 stale 透传;数据源异常 → `SOURCE_FAIL` + 现有过期回退。

## 7. 前端「推荐」Tab

- `index.html` 导航新增第 4 个 Tab「推荐」,复用现有 Tab 切换。
- `app.js` 新增 `loadRecommend()`:
  - 板块卡片:板块名 + verdict 徽章 + composite 分 + 成分股表格(代码/名称/综合分/风险分/verdict,点击跳转个股详情)。
  - 板块标题旁标成分股源口径(如"成分股:新浪行业·电子设备")。
  - 底部列出 `skipped_sectors`(灰字 + 原因)。
  - 顶部免责声明:"仅供研究参考,不构成投资建议。"。
  - 手动刷新 + 60s 自动刷新,沿用现有逻辑。

## 8. 错误处理与性能

| 场景 | 行为 |
|---|---|
| 映射表查不到 | `skipped_sectors(reason=no_mapping)`,补位下一个 |
| 新浪成分股拉取失败 | `skipped_sectors(reason=source_fail)`,其余继续 |
| 单只候选日线失败 | 单只跳过,同板块其余继续;整板块候选全失败 → 板块降级 |
| 强势板块不足 | 返回实际数量,前端提示 |
| stale 缓存 | 沿用 `meta.stale` + 前端过期提示 |

- `get_sector_constituents` TTL 1800s;`get_stock_daily` 复用 600s 缓存。
- 首次加载 ≈ 3 成分股 + 15 日线请求(10–20s,已获用户接受)。
- 全市场 spot 复用现有 60s 缓存,不新增全市场级调用。

## 9. 文件改动清单

| 文件 | 改动 |
|---|---|
| `data_source.py` | + `SECTOR_CONS_MAP`、+ `get_sector_constituents(name)`(TTL 缓存) |
| `recommend.py`(新) | 纯函数:`select_sectors()`、`filter_candidates()`、`rank_candidates()`、`build_recommend()` |
| `app.py` | + `GET /api/recommend` 路由 |
| `templates/index.html` | + 推荐 Tab |
| `static/app.js` | + `loadRecommend()` + Tab 绑定 |
| `static/style.css` | 少量推荐卡片样式 |
| `tests/test_recommend.py`(新) | 纯函数单测 + API 测试(mock 数据源,离线可跑) |

## 10. 测试

1. **映射表完整性**:`SECTOR_CONS_MAP` 的 key ∈ THS 板块名、value ∈ 新浪 label(mock 集合校验)。
2. **`select_sectors`**:verdict 过滤、composite 排序、top 数量、未映射补位。
3. **`filter_candidates`**:停牌/ST/新股/涨停/大跌/流动性逐条过滤。
4. **`rank_candidates`**:剔除规避、composite 排序、Top N。
5. **API 路由**:mock 数据源,断言响应结构、skipped_sectors、参数校验、stale 透传。
6. **回归**:`pytest tests/ -v` 全绿。
