# A股三层分析看板 — 设计文档(Rev.2)

- **日期**: 2026-08-11(Rev.2: 响应评审,补持久化层/阈值表/API契约等)
- **状态**: 已确认,待实施
- **目标目录**: `C:\stock`

## 1. 目标与范围

本地运行的 A 股分析看板,面向**中短线波段**风格的投资决策辅助。核心诉求:

- **三层分析**:大盘总览 → 板块强弱(含板块级情绪)→ 个股量价体检
- **数据 + 明确结论**:规则打分直接给出方向结论,同时保留图表与指标数据供验证
- **盘中可用**:手动刷新 + 可选自动刷新(默认关闭)
- **界面**:本地网页看板(Flask 后端 + ECharts 前端)

### 范围外(本期不做)
- 东方财富板块热力图、北向资金(数据源被网络屏蔽)
- 实盘下单、买卖信号推送
- AI 大模型解读(可选后续)
- 周K/月K/分钟K(以日K + 分时为核心)

## 2. 环境与数据源约束

- Python 3.13 / miniconda,akshare 1.18.84,flask(版本见 L2)
- **可用**:新浪(Sina)、腾讯(Tencent)、同花顺(THS)
- **被屏蔽**:东方财富(East Money)、网易(连接被重置)→ 全部避开 `xxx_em` 函数

已验证可行的数据接口:
- 新浪全市场实时快照 `stock_zh_a_spot()`(约 5541 只,含涨跌幅/成交额,约 10s)
- 新浪指数日线 `stock_zh_index_daily`;个股日线/分钟线 `stock_zh_a_daily` / `stock_zh_a_minute`
- 同花顺板块列表 `stock_board_industry_name_ths`(90)/ `stock_board_concept_name_ths`(375)
- 同花顺板块摘要 `stock_board_industry_summary_ths` / `stock_board_concept_summary_ths`
- 同花顺板块指数日线 `stock_board_industry_index_ths` / `stock_board_concept_index_ths`
- 腾讯实时盘口 `qt.gtimg.cn/q=sh600519`

> **待实施确认(风险项)**:同花顺成分股接口函数名 `stock_board_industry_info_ths` / `stock_board_concept_info_ths` 需在实施第一步验证字段(是否含成分股、是否含成交额);若缺字段,按 §5.2 替代方案处理。

## 3. 架构

```
C:\stock\
├── app.py               # Flask 入口 + API 路由
├── data_source.py       # 数据层:akshare 封装 + 线程安全 TTL 缓存(§9)
├── analysis.py          # 分析层:指标计算 + 打分 + 阈值表(§6)
├── store.py             # 持久化层:SQLite 每日快照(§5)
├── install.py           # 首次安装:装依赖 + 下载 echarts(注意命名,见 L1)
├── requirements.txt     # pin 版本(见 L2)
├── static\
│   ├── echarts.min.js   # 本地图表库,版本固定(见 L2)
│   ├── app.js
│   └── style.css
├── templates\
│   └── index.html
├── data\
│   └── market.db        # SQLite,运行时生成(gitignore)
├── docs\superpowers\specs\
└── README.md
```

分层职责:
- **数据层**:取数 + 缓存,不含业务判断
- **持久化层**:每日快照归档,供历史依赖指标(§5)使用
- **分析层**:吃数据层/持久化层产出,输出打分与结论
- **API 层**:组合,输出契约化 JSON(§8)

## 4. 数据层 (`data_source.py`)

| 函数 | 数据源 | 缓存 TTL | 用途 |
|---|---|---|---|
| `get_market_spot()` | 新浪全市场快照 | 60s | 全市场情绪统计、涨停/跌停口径(§7) |
| `get_index_realtime()` | 新浪指数实时 | 30s | 三大指数实时涨跌 |
| `get_index_daily(code)` | 新浪指数日线 | 10min | 大盘K线 |
| `get_sector_summary(type)` | 同花顺板块摘要 | 60s | 板块强弱榜单 |
| `get_sector_index_history(code, type)` | 同花顺板块指数日线 | 30min | 板块K线 |
| `get_sector_components(code, type)` | 同花顺成分股 | 60s | 板块成分股列表(§4 补充) |
| `get_stock_daily(code)` | 新浪个股日线(前复权) | 10min | 个股K线 |
| `get_stock_minute(code)` | 新浪个股分时 | 60s | 分时图 |
| `get_stock_quote(code)` | 腾讯实时盘口 | 30s | 个股实时报价 |

> 所有板块代码统一携带命名空间前缀 `type:code`(如 `industry:885887`),列表/详情/成分股全链路一致,解决行业与概念代码可能冲突的问题(M4)。

## 5. 持久化层 (`store.py`) — 新增

SQLite 数据库 `data/market.db`,解决 H1 三个历史依赖指标。每次拉取大盘/板块数据后,**当日快照 upsert 进库**(同一交易日的多时段取值覆盖为最新,每个交易日保留一行)。

**表结构**:
```
market_daily(
  date TEXT PRIMARY KEY,        -- 'YYYY-MM-DD'
  up_count/down_count/flat_count INTEGER,
  limit_up INTEGER, limit_down INTEGER,
  total_turnover REAL,          -- 全市场成交额(元)
  index_close REAL,             -- 上证指数
  snapshot_time TEXT
)

sector_daily(
  date TEXT, type TEXT,         -- 'industry'|'concept'
  code TEXT, name TEXT,
  change_pct REAL, up_ratio REAL,
  turnover REAL, rank INTEGER,  -- 当日板块涨幅全市场排名
  PRIMARY KEY(date, type, code)
)
```

**消费方**:
| 指标 | 查询 | 来源表 |
|---|---|---|
| 连续上榜天数 | 连续 count(rank ≤ 20) | sector_daily |
| 板块成交额放量(对比近期均值) | 近 N=5 日 turnover 均值 | sector_daily |
| 大盘较昨日量能变化 | 昨日 total_turnover | market_daily |

**冷启动行为(H1)**:历史数据不足时——
- 连续上榜天数无数据 → 视为 1 天(仅当日),并在 UI 标注"持续性数据积累中"
- 板块成交额均值无数据 → 该项权重并入"板块内上涨家数占比"(§5.2)
- 量能变化无昨日值 → 显示 `null`,前端显示"暂无对比",不参与打分
- 自运行首日起持续归档,**运行 N 个交易日后自动恢复完整指标**

## 6. 分析打分层 (`analysis.py`)

### 6.1 大盘总览(无单一情绪结论)
输出:三大指数涨跌、上涨/下跌/平家数、涨停/跌停数、总成交额、较昨日量能变化(`null` 允许)。情绪判断落到板块层。

### 6.2 板块打分(板块级情绪)

**板块情绪分**(0-100):板块内上涨家数占比(40%)+ 板块内涨停家数(20%)+ 板块成交额放量程度(25%,对比 §5 近5日均值)+ 领涨股强度(15%,定义见 L5)

**板块强度分**(0-100):板块指数涨幅(40%)+ 连续上榜天数(30%,rank≤20,定义见 §5)+ 资金活跃度(30%,板块成交额占全市场比例)

**板块风险分**(0-100,加权求和封顶 100):单日涨幅>5% 或 3日累计>10% 过热(+40)+ 放量滞涨(涨幅收窄但量放大)(+30)+ 板块内严重分化(上涨家数占比<0.35)(+30)

> 若同花顺摘要/成分股接口无法提供板块成交额 → 该项权重并入"板块内上涨家数占比",并 UI 标注"量能指标缺失"。

**阈值表(H2)** — 打分 → 档位映射,测试断言依据:
| 维度 | 高 | 中 | 低 |
|---|---|---|---|
| 情绪分 | ≥70 | 45–69 | <45 |
| 强度分 | ≥60 | 35–59 | <35 |
| 风险分 | ≥65 | 40–64 | <40 |

**板块结论规则**(优先级从 1 到 5,先命中先出):
| 优先级 | 条件 | 结论 |
|---|---|---|
| 1 | 强度=低 且 风险=高 | 🚫 **风险提示/回避** |
| 2 | 风险=高 且 (情绪=高 或 强度=高) | ⚠️ **谨慎追高(过热)** |
| 3 | 情绪=高 且 强度=高 且 风险≠高 | ✅ **建议关注** |
| 4 | 情绪=高 且 强度=中/低 且 连续上榜=1天 | 🚨 **警惕一日游**(首日异动,无持续性) |
| 5 | 其余 | 👀 **观望** |

**综合评分(M5)**:`综合分 = 0.4×强度 + 0.35×情绪 + 0.25×(100−风险)`。榜单按综合分降序,服务端每类返回 top 60,支持 `search` 参数。

### 6.3 个股量价打分

- **趋势分**:MA5/10/20/60 多头排列、站上 MA20/MA60
- **量价分**:放量突破、**自定义量比>1.5 且量价齐升**、缩量健康回踩
- **信号分**:MACD 金叉/死叉、突破近期平台高点
- **风险项**:乖离率(偏离 MA20)>15%、放量滞涨、放量跌破 MA20、高位长上影

**合成规则(H5)**:`个股综合分 = 0.4×趋势 + 0.35×量价 + 0.25×信号`;**个股风险分 = max(各风险项得分,0-100)**(乖离率>15%→+60、放量滞涨→+50、放量跌破MA20→+60、高位长上影→+30);风险分≥70(任一重大风险)直接降级。结论:
| 综合分 | 无重大风险 | 有重大风险 |
|---|---|---|
| ≥70 | ✅ **关注** | 🚫 **规避** |
| 55–69 | 🔶 **持有/跟踪** | ⚠️ **回调风险** |
| <55 | 👀 **观望** | 🚫 **规避** |

## 7. 数据口径定义(新增,保证可复现)

- **自定义量比(M2)**:`当日成交量 / 近5日日均成交量(不含当日)`。**非标准分时量比**(新浪日线算不出每分钟均量),文档与测试均以此口径。
- **涨停/跌停近似(M6)**:按涨跌幅阈值近似,无封板状态:
  - 主板(60xxxx/00xxxx):±9.9%
  - 创业板(30xxxx)、科创板(688xxx):±19.9%
  - 北交所(8xxxxx/4xxxxx):±29.9%
  - 限制说明:ST 股 ±5% 不做区分,接口注释注明"近似值"
- **领涨股强度(L5)**:板块成分股中当日涨幅最大的个股涨幅(%)
- **近期平台高点(L5)**:当前价 > 前 20 日(不含当日)最高价即"突破平台"

## 8. API 层与 JSON 契约(H4)

**统一响应契约**:
- 成功 → `200` `{"ok": true, "meta": {"stale": bool, "updated_at": "..."}, "data": {...}}`
- 数据源失败但返回了上次缓存 → `200` `ok:true` + `meta.stale:true`(data 字段完整)
- 完全失败(无缓存)→ `500` `{"ok": false, "error": {"code": "SOURCE_FAIL", "message": "..."}}`

前端只认 `ok` + `meta.stale`,不依赖 HTTP 状态码区分业务错误。

**`GET /api/market`**
```json
{"ok": true, "meta": {"stale": false, "updated_at": "2026-08-11 10:30:05"},
 "data": {"indices": [{"code": "sh000001", "name": "上证指数", "price": 3456.78, "change_pct": 0.45}],
          "breadth": {"up": 3012, "down": 2300, "flat": 229, "limit_up": 45, "limit_down": 3, "total_turnover": 890000000000},
          "volume_vs_yesterday": {"pct": 12.5}}}
```

**`GET /api/sectors?type=industry&top=60&search=半导`**
```json
{"ok": true, "meta": {"stale": false},
 "data": {"type": "industry", "total": 90,
          "sectors": [{"code": "industry:885887", "name": "半导体", "index_change_pct": 3.2,
                        "emotion_score": 78, "strength_score": 82, "risk_score": 55,
                        "composite_score": 71.5, "verdict": "建议关注",
                        "consecutive_days": 2, "data_complete": true}]}}
```

**`GET /api/sector?type=industry&code=885887`**
```json
{"ok": true, "meta": {"stale": false},
 "data": {"code": "industry:885887", "name": "半导体",
          "scores": {"emotion": 78, "strength": 82, "risk": 55, "composite": 71.5},
          "verdict": "建议关注",
          "index_history": [{"date": "2026-08-10", "open": 100, "high": 102, "low": 99, "close": 101.5, "volume": 123456}],
          "components": [{"code": "sh600000", "name": "浦发银行", "price": 12.3, "change_pct": 2.1}]}}
```

**`GET /api/stock?code=600519`**
```json
{"ok": true, "meta": {"stale": false},
 "data": {"code": "sh600519", "name": "贵州茅台",
          "quote": {"price": 1348.9, "change_pct": 0.003, "open": 1348.0, "high": 1352.65,
                    "low": 1338.18, "volume": 682720, "turnover": 919035968},
          "scores": {"trend": 70, "volume_price": 65, "signal": 60, "risk": 30, "composite": 65.5},
          "verdict": "持有",
          "kline": [{"date": "2026-08-11", "open": 1348, "high": 1352.65, "low": 1338.18, "close": 1348.9, "volume": 682720}],
          "intraday": [{"time": "10:00", "price": 1348.9, "avg": 1342.1, "volume": 12000}]}}
```

## 9. 缓存实现细节(M3)

- **线程安全**:Flask 默认多线程,缓存 dict 用 `threading.Lock` 保护(get/set/淘汰)
- **key 规则**:`f"{func_name}:{sorted(params)}"`(全市场快照独立 key)
- **内存上限**:最多 200 条,超限按插入序淘汰最旧(简化 LRU);全市场快照单条是最大项,缓存命中后 60s 内不再重复拉取
- **失败退避**:拉取失败返回 stale 缓存,并将该 key 的 TTL 延长为 `30s × 2^n`(n=连续失败次数,封顶 10min),避免数据源挂掉后每 60s 空转重试;成功后重置 n=0

## 10. 错误处理

- 每个数据源请求超时约 15s,失败重试 1 次
- 重试仍失败 → 返回 stale 缓存 + 退避(§9);前端右上角"⚠️ 数据可能过期"
- 首次运行无缓存且失败 → 走 §8 错误契约,前端给出明确报错文案
- 同花顺成分股/摘要接口字段缺失 → 权重降级(§5.2),UI 标注"指标缺失"

## 11. 前端 (`templates/index.html` + `static/app.js`)

布局(自上而下、自左而右):

```
顶栏:标题 | 更新时间 | 手动刷新 | 自动刷新开关
大盘总览条:三大指数涨跌 | 涨/跌/平 | 涨停/跌停 | 总成交额 | 量能较昨日
板块榜单(主区)               | 右侧详情区
 · 行业/概念切换              |  · 板块:指数K线+三围分+综合分+结论+成分股
 · 按综合分排序,服务端 top60  |  · 个股:日K线+MA+成交量、分时图、量价评分卡
 · 颜色+emoji+文字结论         |
自选股条:⭐ [{code,name}] 快速点击
```

**交互**:
1. 打开页面自动拉大盘 + 板块榜单
2. 点板块行 → 右侧加载板块详情(含成分股)
3. 点成分股或搜索框输代码 → 右侧加载个股详情
4. "加入自选" → localStorage 存 `[{code, name}]`(L3),自选条显示"名称+代码"
5. 手动刷新 → 重拉全量;失败时右上角"⚠️ 数据可能过期"并沿用旧数据
6. 自动刷新开关 → 每 60s 刷大盘+板块(右侧当前选中对象一并刷新)

**无障碍配色(L4)**:结论不只靠颜色——每个结论同时带 emoji 和中文文字标签(如 🚨/⚠️/✅/🔶/👀/🚫),颜色仅作辅助强化。

**图表**:板块K线(蜡烛+MA)、个股K线(蜡烛+MA+成交量副图)、分时图(价格线+均价线+分时量)。

## 12. 测试

- **数据层 smoke test**:每个数据函数返回非空且字段齐全(含 `get_sector_components` 字段验证)
- **打分规则单元测试**:构造已知涨跌的模拟数据,按 §6 阈值表断言分数档位与结论。覆盖:涨停潮、板块分化、放量滞涨、**一日游(情绪高+持续1天)**,量比按 §7 自定义口径构造
- **持久化层测试**:当日快照 upsert 幂等;冷启动(空库)下历史指标按 §5 缺省行为输出
- **API 契约测试**:curl 各路由断言 JSON 结构与 §8 示例一致(含 stale/error 两种异常态)
- **前端手动验收**:浏览器打开,检查三区块、下钻、搜索、自选、刷新开关

## 13. 验收标准(含性能,L6)

- `python app.py` 后浏览器打开 `http://127.0.0.1:8000` 三层看板正常
- 板块榜单反映当日板块涨跌与板块级情绪差异
- 每只个股给出 K线/成交量/分时 + 量价分与结论
- 手动/自动刷新正常;数据源故障时有过期提示不崩溃
- **性能**:冷启动手动刷新总耗时 < 30s(受新浪全市场快照 ~10s 主导);缓存命中后再次加载 < 3s;页面首屏静态资源 < 3s
- **冷启动收敛**:连续运行 5 个交易日后,§5 历史依赖指标全部恢复真实值

## 14. 工程细节

- **L1**:安装脚本命名 `install.py`(不用 `setup.py`,避免与打包工具语义冲突)
- **L2**:`requirements.txt` 固定版本:akshare==1.18.84、flask==3.x(实施时确认)、pandas;ECharts 固定 5.5.1,`install.py` 从官方 npm/CDN 下载到本地并记录版本号
