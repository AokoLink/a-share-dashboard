# 波段候选 + 个股技术详情 设计

> 状态:v2(对抗自审修订后)
> 依赖前文:`2026-08-15-three-board-recommendation-design.md`(波段=自选+regime 启发式看板)

## 0. 目标与诚实边界(最高优先级,不可违反)

用户诉求两句:
1. 推荐适合做波段的股票;
2. 点击推荐股 → 详细分析页(适不适合持有 / 量价分析 / KDJ 等指标)。

**诚实约束(否决任何与之冲突的实现):**

- **C1 方向性选股已证伪。** 历史回测结论为「个股方向无可交易 edge」「选股 edge 证伪」,#129/#130 明确「勿加方向旋钮」。功能 1 只产出「可操作、流动性够、波动够」的波段标的(方向中性),**绝不**产出「会涨」。波段候选的**过滤键与排序键与展示字段均不得含方向性量**:不得用 `composite`/`verdict`/`position`(打分)、`涨幅 top`(追涨)、金叉死叉(方向)作筛选或排序;只用成交额(流动性)、换手率(活跃度)、振幅(波动)、风险尾/高位(风险控制)这些方向中性量。
- **C2 「适不适合持有」分两层,只有大盘 regime 层有真实 edge。** regime 仅在「进场/回避」上验证过(恐慌=等机会/超跌反弹,高潮/退潮=卖出回避),非个股退出信号。个股层只给**风险 + 位置**两个尾部描述,不给方向承诺;`hold` 块不得含 `verdict`/`tier`/`composite` 等方向性结论。
- **C3 技术指标是描述性的。** KDJ/MACD/RSI/量价描述「现在发生了什么」,不预测「接下来涨跌」。UI 显式标注;「超买/超卖」额外注明「仅描述当前位置,不代表即将反转」。

## 1. 功能 1:波段候选(方向中性可操作性筛选)

### 1.1 定位
从全市场活跃股里筛「流动性够、有波动、当前可操作、风险不在尾部」的标的,按**活跃度(换手率)**排序。与「可介入龙头」(强调追强势龙头)不同:波段候选方向中性,只用成交额/换手率/振幅/风险尾/高位。

### 1.2 候选池(方向中性)
**不**复用 `pick_leaders`(其「涨幅 top3」是追涨方向性选择,违背 C1)。改为:
1. `ds.get_market_spot()`(已缓存,全市场);
2. 新纯函数 `swing_pool(spot_df, exclude_codes, min_amount, top_n=200)`:排除 ST、停牌(price/volume 缺失)、新股(`exclude_codes`)、涨停(`change_pct >= limit_threshold`);`amount >= min_amount(1e8)`;按**成交额**降序取前 `top_n=200`(纯流动性,方向中性)。
3. 对候选逐股 `get_stock_daily` 取 `turnover`(换手率 fraction)与振幅,`score_stock` 取 `risk`/`pos60`。

### 1.3 硬过滤(方向中性)
纯函数 `swing_eligible(turnover_frac, risk, pos60)` → `(ok, reason)`,命中即剔除:
1. `turnover_frac` 缺失或 `< 0.02`(换手不足 2%);
2. `risk` 非 None 且 `>= 60`(风险尾部,记忆:唯一幸存风险控制);
3. `pos60` 非 None 且 `>= 0.85`(60 日高位,记忆:pos60 单调越高越差)。

`risk`/`pos60` 为 None(历史 <61 根)时 fail-open(不因无法评估而剔除,与 `filter_candidates` 一致)。ST/停牌/涨停/新股/流动性不足已在 `swing_pool` 预滤。

### 1.4 排序
按换手率 `turnover_frac` 降序;同换手率按 `amp20` 降序,再按成交额降序。**不产出「波段综合分」**。展示原始值,UI 顶部注明「按活跃度(换手率)排序,非收益预测」。

### 1.5 输出契约
`GET /api/swing-candidates` →
```json
{
  "generated_at": "…",
  "regime": {"as_of":"…","label":"震荡","swing":{"action":"hold","message":"…"}},
  "items": [
    {"code":"sh600487","name":"亨通光电","price":62.98,"change_pct":10.0,
     "amount":19196730000.0,"turnover_pct":12.97,"amp20":3.8,
     "risk":28,"pos60":0.73,"sector_name":"通信设备"}
  ]
}
```
- 字段全部方向中性:`change_pct` 为当日描述、`risk`/`pos60` 为风险控制、`turnover_pct`/`amp20`/`amount` 为流动性/波动。**无 composite/verdict/position。**
- `turnover_pct = round(turnover_frac * 100, 2)`(换手率唯一来源 `daily["turnover"].iloc[-1]`,fraction,akshare 口径;**严禁**用 `quote["turnover"]`(那是成交额元)或 spot(无该列))。
- `amp20` 见 §2.6。
- `sector_name` 由 `ds.resolve_code_sectors(code)` 本地映射 best-effort,无则 `null`。
- `regime` 由 `env.load_cached_regime` 注入(读缓存零重算)。
- **返回上限 top 60**(排序后截断);**不落库、无快照**(与 recommend 的 prev_snapshot 机制无关)。
- **时滞诚实注记**:`daily` 末根是**上一个已完成的交易日**(盘中时非当日实时),故 `turnover_pct`/`amp20`/`risk`/`pos60`/全部指标均基于上一交易日收盘,是隔日滞后值;当日实时仅 `quote`(change_pct/price/量比)。UI 在候选表与详情页注明「基于上一交易日收盘」。

## 2. 功能 2:个股技术详情(描述性)

### 2.1 扩展 `/api/stock`
新增 `indicators` 与 `hold` 两个顶层键,其余不变。既有 `scores`/`composite`/`verdict`/`tier` 原样保留(那是既有的「三层评分」面板,不在本次改动范围);**新增的 `hold` 块不得重复这些方向性结论**。

### 2.2 `indicators`(描述性,`history_limited=true` 时全字段为 null)
```json
{
  "history_limited": false,
  "ma": {"ma5":…,"ma10":…,"ma20":…,"ma60":…},
  "macd": {"dif":…,"dea":…,"macd":…,"cross":"金叉","zero":"零轴上"},
  "kdj": {"k":…,"d":…,"j":…,"state":"中性","cross":"多头"},
  "rsi": {"rsi14":…,"state":"中性"},
  "bias": {"ma20":…,"pct":…},
  "pos60":0.62,
  "turnover_pct":12.97,
  "volume_price": {"vr":…,"vol_ratio":…,"state":"放量上涨"}
}
```
- `macd.cross` ∈ {金叉, 死叉, 多头, 空头, —};`macd.zero` ∈ {零轴上, 零轴下}。二者正交,独立纯函数 `macd_state(dif,dea,prev_dif,prev_dea)` 计算,**不复用** `_macd_branch`(那是 signal 打分助手,返回 30/15/12/8/0,不产状态标签)。判定:当根金叉(prev_dif≤prev_dea 且 dif>dea)→ 金叉;当根死叉(prev_dif≥prev_dea 且 dif<dea)→ 死叉;否则 dif>dea → 多头 / dif<dea → 空头 / 相等或 NaN → —。`zero`:dif>0 → 零轴上,dif<0 → 零轴下,NaN → null。
- `kdj.state` ∈ {超买, 超卖, 中性, —};`kdj.cross` ∈ {金叉, 死叉, 多头, 空头, —}。见 §2.4。
- `rsi.state`:rsi14>70 → 超买,<30 → 超卖,否则 中性。**独立描述阈值,不沿用 `_rsi_score` 的 65/80 打分阈值**(两者用途不同)。
- `bias.pct` 乖离率复用 `recommend.bias_pct`。
- `turnover_pct` 同上,来自 `daily["turnover"]` fraction ×100;`pos60` 来自 `analysis._pos60`(0..1)。

### 2.3 `hold`(分层,无单一个股买卖指令)
```json
{
  "regime": {"label":"震荡","action":"hold","message":"持有(中性,启发式,未回测)"},
  "stock": {"risk":30,"risk_note":"个股风险可控","pos60":0.62,"pos_note":"位置中性"},
  "summary":"大盘:震荡(持有,中性);个股:风险可控、位置中性。个股方向无算法 edge(历史回测证伪),仅供研究参考。"
}
```
- `regime` 由 app 层调 `env.regime_swing_action(label)` 得到 action/message,再拼 `label` 组装;**analysis.py 不得 import environment**(environment 已 import analysis,成环)。
- `stock` **只含 risk/pos60 两个尾部描述,无 verdict/tier/composite。**
- `stock.risk_note`:risk None → "历史不足,无法评估风险";risk≥60 → "个股风险尾部,注意回撤";否则 "个股风险可控"。
- `stock.pos_note`:pos60 None → "历史不足,无法评估位置";pos60≥0.85 → "60 日高位,注意追高风险";否则 "位置中性"。
- `summary`:拼接两层模板句,末尾恒定免责「个股方向无算法 edge(历史回测证伪),仅供研究参考。」。

### 2.4 KDJ(`analysis.add_kdj`,n=9,K0=D0=50)
```
RSV = (C - Low9) / (High9 - Low9) * 100      (High9==Low9 或不足9根 → NaN)
K   = (2*K_prev + RSV) / 3
D   = (2*D_prev + K) / 3
J   = 3*K - 2*D
```
NaN 守卫:K/D/J 任一为 NaN 时置 None(不传染,不落 else 分支)。
`kdj_state(k,d,j,prev_k,prev_d)` → `(state, cross)`:
- 任一输入 None → `(None, None)`(前端显示 —)。
- `state`:K>80 且 D>80 → 超买;K<20 且 D<20 → 超卖;否则 中性。
- `cross`:prev_k≤prev_d 且 k>d → 金叉;prev_k≥prev_d 且 k<d → 死叉;否则 k>d → 多头 / k<d → 空头 / k==d → —。

### 2.5 量价状态(`analysis.volume_price_state`)
`chg = quote.change_pct`,`vr = custom_volume_ratio(...)`:
- vr None(盘中前 15 分钟 / avg5 缺失)→ state = "数据不足"(**不**落「平量」);
- chg>1 且 vr>1.2 → 放量上涨;
- chg>0 且 vr<0.8 → 缩量上涨;
- chg<-1 且 vr>1.2 → 放量下跌;
- chg<0 且 vr<0.8 → 缩量回调;
- 否则 平量。
`vol_ratio` = 5 日均量 / 20 日均量(口径同 `compute_volume_price_score` 的 r)。

### 2.6 振幅 `amp20`(`analysis.amp20`)
`amp20(daily_df)` = 近 20 根 `(high - low) / prev_close * 100` 的均值(日均振幅 %,方向中性);不足 21 根 → None。排序键与展示值共用此实现,避免口径漂移。

### 2.7 前端(详情页)
`renderStockScores` 负责渲染,`openStock` 仅负责显示容器。新增三个容器:
- `#hold-advice` 持有建议块:regime 徽章 + message + 个股 risk/pos 注记 + summary 免责。
- `#volume-price` 量价块:量比 / 换手率 / 量能比 + state 文案。
- `#tech-indicators` 指标块:MA(5/10/20/60)、MACD(DIF/DEA/柱 + cross/zero)、KDJ(K/D/J + state/cross)、RSI + state、乖离率、60 日位置。
每块标题旁一行 `muted` 免责:「技术指标为描述性,不构成买卖信号;超买/超卖仅描述当前位置,不代表即将反转。」`indicators.history_limited` 或 null 时显示「历史不足(<3 个月)」降级。

### 2.8 前端(波段候选)
- `loadSwingCandidates` 接入 `switchView`、`refreshAll`、选项卡点击处理器(现有 `view === "swing"` 分支在 `loadSwing` 后追加 `loadSwingCandidates()`)。
- `renderSwingCandidates` 渲染 `#swing-candidates` 表格,行点击 `openStock(code)`。
- 顶部注明「按活跃度(换手率)排序,非收益预测」。

## 3. 文件改动清单

| 文件 | 改动 |
|---|---|
| `analysis.py` | +`add_kdj`、+`kdj_state`、+`macd_state`、+`volume_price_state`、+`amp20`、+`compute_technical_indicators`、+`build_hold_advice`(均纯函数;`import numpy as np`);`build_hold_advice(regime_block, risk, pos60)` regime 由参数注入,不 import environment |
| `recommend.py` | +`swing_pool`(方向中性池)、+`swing_eligible`(方向中性硬过滤)、+`collect_swing_candidates`(编排) |
| `app.py` | `/api/stock` 注入 `indicators`+`hold`(regime 由 `env.load_cached_regime` + `env.regime_swing_action` 组装后传入 `build_hold_advice`);+`/api/swing-candidates` 路由 |
| `templates/index.html` | 波段面板 +`#swing-candidates` 容器;详情区 +`#hold-advice`/`#volume-price`/`#tech-indicators` 三容器 |
| `static/app.js` | +`loadSwingCandidates`/`renderSwingCandidates` + 接线;`renderStockScores` 扩展三块 + null 降级 |
| `static/style.css` | 新卡片/徽章样式 |
| `tests/*` | `test_analysis_*` 钉 KDJ/量价/振幅/持有建议/macd_state;`test_recommend` 钉 swing_pool/swing_eligible/collect_swing_candidates;`test_api` 钉两路由 |

## 4. 测试要求

- `add_kdj`:构造已知 close/high/low,钉 K/D/J 精确值(容差 1e-6)+ state/cross;含「9 日一字板(High9==Low9)」钉 NaN→None、cross=—。
- `kdj_state`:金叉/死叉/多头/空头/K==D→—/NaN→(None,None) 各一断言。
- `macd_state`:金叉/死叉/多头/空头/NaN→— 各一断言,`zero` 零轴上/下。
- `volume_price_state`:放量上涨/缩量上涨/放量下跌/缩量回调/平量 + **vr=None→"数据不足"** 各一断言。
- `amp20`:钉一个精确均值。
- `build_hold_advice`:risk≥60 / pos60≥0.85 / 正常 / **risk=None / pos60=None(历史不足)** 分支各一断言;断言返回值不含 verdict/tier/composite。
- `swing_pool`:排除 ST/停牌/涨停/新股/流动性不足;按成交额降序取 top_n。
- `swing_eligible`:换手不足/风险尾部/高位 → False;换手正常且 risk/pos60 None(fail-open)→ True。
- `collect_swing_candidates`:mock 数据源,断言 items 契约字段(无 composite/verdict/position)、排序并列规则、regime 注入、top 60 截断、跨代码去重。
- `/api/swing-candidates` 与 `/api/stock`(含 indicators/hold)用 mock 数据源断言契约字段存在、`hold.stock` 无 verdict/tier。

## 5. 明确不做(out of scope)
- 不做任何方向性「会涨/该买/该卖」断言(C1/C2/C3)。
- 不做「波段综合分」预测强度指标。
- 波段候选不落库、无快照、无分页(固定 top 60)。
- 不改动「可介入龙头」「推荐」现有口径。
- 不新增方向旋钮或选股 edge 参数(与 #129/#130 一致)。
