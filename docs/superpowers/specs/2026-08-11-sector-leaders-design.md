# 板块详情「龙头/强势股」条带 设计文档

> 状态:已确认(方案 A;混合口径=龙头池 top3+强势池 top3 合并去重取前 5;保留涨停股)
> 日期:2026-08-11

## 1. 背景与目标

当前点击左侧板块行,右侧详情区展示板块分数卡片 + 板块指数 K 线(`/api/sector`)。

**目标**:在板块详情基础上,额外展示该板块 5 个「龙头/强势股」—— 只显示当日价格与涨跌幅,点击任一股票跳转到现有完整个股详情(`openStock`)。

- 口径:混合 —— 龙头(按当日成交额)+ 强势(按当日涨幅),各取 top3 合并去重后取前 5。
- 展示:条带,每项 = 标签(龙头/强势/龙头+强势)+ 名称 + 现价 + 涨跌幅。
- 交互:点击 → 复用 `openStock(code)` 的个股详情(K 线/分时/评分)。

## 2. 设计决策

| 决策点 | 决定 | 理由 |
|---|---|---|
| 数据源 | 复用 `ds.resolve_sector_constituents(ths_name)`(THS→新浪行业映射,缓存 1800s)+ 已拉取的全市场 `spot` | 全局约束「数据源仅 Sina/Tencent/THS」下,akshare 1.18.84 无 THS 板块成分股接口(`stock_board_industry_cons_em` 为东财,被禁);映射基建已由推荐功能建好并测试 |
| 拉取方式 | 内联进 `/api/sector`(方案 A),非独立接口 | 一次请求拿到全部;板块详情不因 leaders 多一次往返 |
| 排名口径 | 龙头池=按成交额降序 top3;强势池=按涨幅降序 top3;合并去重(按 code)取前 5;龙头在前 | 用户确认「混合(龙头+强势)」+「5 个」 |
| 涨停股 | **保留**。展示条带非买入推荐,涨停往往是板块真正的龙头/强势 | 用户未否决设计默认 |
| 过滤 | 轻过滤:排除 停牌(price/volume 空)、ST(名含 ST)、新股(`get_new_stocks()`,缓存 1800s) | 复用推荐功能的排除语义;不做涨停/大跌/低流动可买入过滤 |
| 降级 | 映射失败/未映射/网络失败 → `leaders` 为空 + `leaders_status` 标记,板块图表正常返回 200 | leaders 是增强信息,不得阻塞核心板块视图 |

## 3. 数据流

```
GET /api/sector?code=industry:885887
  → summary / get_sector_index_history / get_market_spot(现有)
  → name = r["name"]                                   # THS 板块名
  → res = ds.resolve_sector_constituents(name)         # 缓存 1800s;DataSourceError→status=source_fail
  → res.ok False → leaders_status = res["reason"]      # no_mapping | ambiguous
  → 成分股代码 ∩ spot:spot_index = {code: row}
  → rows = [spot_index[c] for c in res["codes"] if c in spot_index]   # 零额外请求
  → leaders = recommend.pick_leaders(rows, total=5, exclude_codes=ds.get_new_stocks())
  → 返回 {..., leaders, leaders_status, leaders_source}
```

## 4. 后端

### 4.1 新纯函数 `recommend.pick_leaders(spot_rows, total=5, exclude_codes=frozenset())`

输入:该板块在 spot 中的成分股行(list of dict,含 `code/name/price/change_pct/amount`)。

逻辑(纯函数,无网络):
1. 轻过滤:跳过 `code in exclude_codes`(新股)、名称含 `ST`、`price`/`volume` 为空(停牌)。
2. 龙头池 = 按 `amount` 降序前 `min(3, total)`(仅取 `amount` 非空;`_num` 归一)。
3. 强势池 = 按 `change_pct` 降序前 `min(3, total)`(仅取 `change_pct` 非空;`_num` 归一)。
4. 按 龙头池 → 强势池 顺序合并去重(按 code),截断到 `total`。
5. 每项打 `tag`:同时进两池 → `"龙头+强势"`;仅龙头池 → `"龙头"`;仅强势池 → `"强势"`。

返回:`[{code, name, price, change_pct, amount, tag}]`(价格/涨跌幅经 `_num` 归一无;`price`/`change_pct` 保留原数值)。

边界:空输入 → `[]`;池内字段全空(如全部停牌)→ 对应池空;过滤后为空 → `[]`。

### 4.2 `app.py` 的 `api_sector` 扩展

在现有 `scores`/`index_history` 计算之后追加:

```python
leaders, leaders_status, leaders_source = [], "ok", None
try:
    res = ds.resolve_sector_constituents(str(r["name"]))
except Exception:
    leaders_status = "source_fail"
else:
    if not res["ok"]:
        leaders_status = res["reason"]                # no_mapping | ambiguous
    else:
        spot_index = {str(x["code"]): x for x in spot.to_dict("records")}
        rows = [spot_index[c] for c in res["codes"] if c in spot_index]
        leaders = recommend.pick_leaders(rows, total=5, exclude_codes=ds.get_new_stocks())
        leaders_source = res.get("source_name")
```

响应新增三个字段:
- `leaders`: `[{code, name, price, change_pct, amount, tag}]`
- `leaders_status`: `"ok" | "no_mapping" | "ambiguous" | "source_fail"`
- `leaders_source`: 映射命中后的新浪行业名(如 `"电子信息"`),失败时为 `null`

注意:仅 `summary`/`get_sector_index_history`/`get_market_spot` 失败才 500;leaders 相关失败一律降级(仍 200)。

## 5. 前端

### 5.1 `index.html`

`#detail` 内 `#sector-scores` 之后加:

```html
<div id="sector-leaders" class="hidden"></div>
```

### 5.2 `static/app.js`

- `openSector`:在 `renderSectorScores` 后调用 `renderSectorLeaders(b.data)`。
- `openStock`:隐藏 `#sector-leaders`(与 `#sector-scores` 一致)。
- 新增 `renderSectorLeaders(d)`:
  - `d.leaders` 为空 → 按 `d.leaders_status` 显示灰字提示:no_mapping/ambiguous →「暂无成分股映射」;source_fail →「板块成分股拉取失败」;否则隐藏。
  - 非空 → 渲染条带:每项 `<span class="leader-chip" data-code>` 含 `<i>标签</i> 名称 现价 涨跌幅`(涨跌幅复用 `fmtPct`,红涨绿跌),绑定 `click → openStock(data-code)`。
- `refreshAll` 已通过 `openSector` 重渲染 leaders(60s 自动刷新同步更新)。

### 5.3 `static/style.css`

追加 `.leader-chip` 条带样式(胶囊样式,行内 inline-flex,点击手型,悬停高亮)。

## 6. 错误处理与覆盖度

| 场景 | 行为 |
|---|---|
| 板块未映射(no_mapping)/关键词多命中(ambiguous) | `leaders=[]` + `leaders_status` 标记,前端灰字「暂无成分股映射」,板块图表正常 |
| 新浪成分股拉取失败(source_fail) | 同上,提示「板块成分股拉取失败」,不阻塞 |
| 成分股均不在 spot(北交所/退市整理) | `leaders=[]`,前端隐藏或提示,板块图表正常 |
| 映射覆盖度 | 现映射表覆盖 37/90 板块;未映射板块无 leaders。映射表扩充属后续迭代(方案 C) |

## 7. 测试(全离线)

- `tests/test_recommend.py` 新增:
  - `test_pick_leaders_mixed`:构造含金额/涨幅已知的 spot 行,验证龙头池+强势池合并、按 code 去重、tag 正确、截断到 total、龙头在前。
  - `test_pick_leaders_excludes`:ST / 停牌(price 或 volume 空)/ 新股(exclude_codes)被排除。
  - `test_pick_leaders_empty_and_degenerate`:空输入 → `[]`;全部停牌 → `[]`;amount/change_pct 全 None → 对应池空。
- `tests/test_api.py`:
  - 现有 `client` fixture 已 mock `resolve_sector_constituents`(返回 `["600519","600000"]`,均在 spot)→ `test_sector_detail` 追加断言:有 `leaders`,首项为 `600519`(金额最大 → 龙头),含 `tag`/`price`/`change_pct`。
  - 新增 `test_api_sector_leaders_no_mapping`:局部 mock resolve 返回 `{"ok": False, "reason": "no_mapping"}` → 200,`leaders=[]`,`leaders_status="no_mapping"`。
  - 新增 `test_api_sector_leaders_source_fail`:局部 mock resolve 抛 `DataSourceError` → 200,`leaders_status="source_fail"`,`index_history` 仍正常。

## 8. 范围外(不做)

- 扩充 `SECTOR_CONS_MAP` 覆盖更多板块(方案 C,后续迭代)。
- 条带项显示更多字段(量比/换手/振幅)或完整评分。
- 龙头/强势之外的「中军」独立分层(混合口径已隐含)。
- 对 leaders 做可买入硬过滤。

## 9. 全局约束遵循

- 数据源仅 Sina/Tencent/THS:复用现有映射接口,无新数据源。
- 所有测试离线可跑:resolve/spot 均 mock。
- 数值字段一律经 `_num()` 归一无;分数断言用精确字面量或 `abs=0.01`。
- 复用现有 `fmtPct`/`openStock`/`style.css` 变量,不重复造轮子。
