# A股三层分析看板 + 离线回测/预测/评估研究管线

本地运行的 A 股分析工具,两个使用面:

- **在线看板**(`app.py`,Flask + ECharts):大盘 → 板块 → 个股三层实时分析,外加股票推荐 / 可介入龙头 / 交易回测三个视图。
- **研究管线**(CLI 模块):读本地日线数据做「历史盲测 → 概率预测 → 交易模拟 → 快照验证 → 六维评估 → 环境分类 → 自动复盘」，并追加真实前向信号与观察记录。全程严格时间隔离、不编造结果。

---

## 安装

```bash
cd C:\stock
python install.py     # 装依赖(requirements.txt)+ 下载 ECharts 5.5.1 到 static/
```

## 启动在线看板

```bash
python app.py         # 打开 http://127.0.0.1:8000
```

首次运行会在 `data/market.db` 建 SQLite 快照库(自动)。

---

## 在线看板功能(Flask API + `static/app.js`)

| 视图 | 内容 |
|---|---|
| **大盘总览** | 三大指数(上证/深证/创业板)、涨/跌/平家数、涨停/跌停、总成交额、量能较昨日(收盘后有效)、数据过期提示 |
| **行业板块** | 行业榜(top 60,支持搜索):情绪 / 强度 / 风险 / 综合分 + 六档结论(建议关注/跟踪(热点延续)/谨慎追高(过热)/风险提示回避/警惕一日游/观望);点行看板块指数 K 线 + 龙头股 |
| **个股体检** | 日 K + MA + 成交量、分时(价格+均价+量)、四因子(位置/量价/趋势/信号)+ 风险分 + 五档结论(强烈关注/关注/持有跟踪/观望/回避) |
| **股票推荐** | 板块→个股双层推荐(THS 强势板块 + 成分股经映射表转新浪行业,Top 5);含上一期信号次日的跳空对比(跳空预警)、被跳过板块透明化 |
| **可介入龙头** | 全市场"可介入/观察"两档龙头(板块评价 + 现价/涨跌幅/位置/标签/综合分/风险/乖离率) |
| **交易回测** | 逐篮盈亏比/胜率/期望/最大回撤表(读取预生成的 `_analysis/trade_sim.json`,需先跑 `pipeline/simulate.py`) |
| **策略验证** | 收盘后冻结三套独立策略的真实候选，查看版本、修订、前向观察与证据阻断项 |
| **持仓诊断** | 输入代码/自选 → 1/3/5/10/20 日 P(涨)曲线 + 期望收益,标注是否超过成本、与全市场基准对比;测不到明说「信号不足,无法判断」(读取 `_analysis/forward_calib.json`,需先跑 `python -m core.forward`) |
| **自选股** | 客户端自选(localStorage 持久化),点击查看个股 |

> 盘中可用性:个股量比按全日折算盘中即比;板块"成交额放量/放量滞涨"仅收盘后计算,盘中自动降级。手动刷新 + 可选 60s 自动刷新。

---

## 研究管线与前向记录

这些模块主要读取 `_analysis/daily/*.pkl`(个股日线,9 列)与 `_analysis/code2sector.json`(GBK)。第一阶段已修复部分交易时序与验证边界；历史成分、退市股及未复权成交价仍不完整，旧报告不能视为无偏回测，详见 [第一阶段基线状态](docs/phase1-baseline.md)。

第一阶段新增可审计输入采集：`python -m pipeline.collect_stage1`。它把主题核心股的未复权日线、分红送转原始事件、真实交易日历、沪深退市清单和哈希审计保存到 `_analysis/stage1/`；当前只有部分输入就绪，`audit.json` 的状态仍为 `exploratory`，不代表旧回测已经升级为正式基线。

第二阶段新增三套独立的技术策略研究定义：`core/strategies.py` 同时供 `/api/strategy-candidates` 和 `python -m pipeline.evaluate_strategies` 使用。输出为研究候选和同日对照，不给出交易操作建议；中期配置因缺少财报披露时间历史暂不筛股。详见[第二阶段实施与对照结果](docs/phase2-strategy.md)。

第三阶段的板块详情增加相对上证综指的 5/20/60 日强度、当前成分广度与成交集中度；“轮动矩阵”用于横向比较。成交核心、价格领涨和产业核心证据状态分开显示；“组合透视”按自选等权估计重复板块暴露、相关性和波动贡献。详见[第三阶段实施记录](docs/phase3-sector-portfolio.md)。

第四阶段新增不可覆盖的当日策略快照、T+1 开盘模拟与各策略持有期跟踪、同日冻结池归因和版本状态事件。第二轮 B 批次已统一为 `python -m pipeline.run_daily`：检查交易日和收盘时间，采集现货与双价格日线、输出覆盖及板块证据、冻结研究候选，并更新已有信号的入场、持仓与退出进度。网页只读保存结果。原 `python pipeline/freeze_strategy_signals.py` 入口转发至同一任务。详见[步骤 B 实施与运行说明](docs/step-b-implementation.md)及[第四阶段说明](docs/phase4-forward-lifecycle.md)。

| 模块 | 作用 | 命令 | 默认输出 |
|---|---|---|---|
| `core/backtest.py` | 历史盲测基线:6 篮(A/E_hi/E_lo/B/C/D)次日方向、按年、Welch 检验 | `python -m core.backtest` | `backtest_baseline.json` |
| `pipeline/predict.py` | 预测引擎:composite→校准概率(等量分箱+PAV 单调),T+1 方向/gap/路径 + T+3 趋势,回测 + 前向快照两模式 | `python -m pipeline.predict` / `python -m pipeline.predict --predict` | `prediction_baseline.json` / `prediction_snapshot.json` |
| `core/forward.py` | 多 horizon 前向分布引擎:1/3/5/10/20 日(合法 oo 窗口)P(涨)/期望收益/左尾概率,持仓诊断校准器;「信号不足」为合法输出 | `python -m core.forward` / `python -m core.forward --predict` | `_analysis/forward_calib.json` / `_analysis/forward_snapshot.json` |
| `pipeline/compare.py` | 快照对比:冻结的前向预测 vs 真实 T+1/T+3 行情,算六维命中率 + 回归误差 + 风险校准度 | `python -m pipeline.compare --snapshot prediction_snapshot.json` | `snapshot_verification.json` |
| `pipeline/simulate.py` | 交易模拟 + 风险收益评价:逐笔保守日内假设,胜率/盈亏比/期望/最大回撤/Profit Factor;`--ab` 做 risk_p 止损参数化对照 | `python -m pipeline.simulate` / `python -m pipeline.simulate --ab` | `trade_sim.json` / `trade_sim_ab.json` |
| `pipeline/evaluate.py` | 六维评估:八层切片(大盘/热门/龙头/趋势/高位/超跌/反抽/震荡)+ 按市场环境切片 + ±2σ 显著性 | `python -m pipeline.evaluate` | `evaluate_report.json` |
| `core/environment.py` | 市场环境分类:七态决策树(恐慌→高潮→熊→牛→恢复→退潮→震荡),纯因果(只读 ≤i) | `python -m core.environment` | `environment_report.json` |
| `pipeline/review.py` | 自动复盘 + 优化建议:只读消费 evaluate 报告,产出弱项/无 edge/退化判定 + R1-R5 数据锚定建议 + 26 项旋钮清单 | `python -m pipeline.review --in evaluate_report.json` | `review_report.json` |

**推荐流程顺序**:`core/backtest.py`(基线)→ `pipeline/predict.py`(校准)→ `pipeline/predict.py --predict`(冻结快照)→ `pipeline/compare.py`(真实验证)→ `pipeline/evaluate.py`(评估)→ `pipeline/review.py`(复盘)。持仓诊断前先跑 `core/forward.py`(多 horizon 校准)→ 看板「持仓诊断」视图。

> 前置数据:管线需要 `_analysis/daily/*.pkl` 与 `_analysis/code2sector.json`,由离线脚本(如 `_analysis/refetch_pkls.py`,gitignored)经 `data_source.get_stock_daily` 重拉生成,不入库。

---

## 核心评分体系(`core/analysis.py`,纯函数)

- **个股四因子 v3**:位置分(60 日区间位置+乖离甜点+平台)、量价分(量比健康度+量价配合+持续性)、趋势分(均线多头+金叉)、信号分(MACD+RSI+低位突破+动量)。
- **风险分**(0-100,封顶):乖离梯度 + 放量跌破 + 量能滞涨 + 高位长上影 + 回撤。
- **综合分**:四因子加权 ×(1−风险/100)+ 板块共振加成(sector_bonus +8/+4/0/−5),再由 `stock_verdict` 分五档。
- **板块三因子**:情绪 / 强度 / 风险 → 板块综合分 + 六档结论 + 过热标记(连板天数)。
- **技术指标**:MA(5/10/20/60)、MACD、RSI(14)、20 日最大回撤;涨跌停阈值按代码区分(9.9%/19.9%/29.9%)。

---

## 数据与持久化

- **数据层 `core/data_source.py`**:akshare 多源获取(腾讯实时行情、新浪板块/指数/现货、同花顺行业板块、个股日线/分时历史);线程安全 TTL 缓存(默认 60s,200 条上限)+ 过期兜底 + 失败指数退避(30s×2ⁿ 封顶 10min)。
- **持久化 `core/store.py`**:SQLite(WAL)每日大盘/板块快照、板块成交额 5 日均值、推荐快照(JSON 序列化),供复盘与"上一期对比"。
- **板块关系**:每日任务的板块证据、独立策略、历史策略评估及组合暴露共用 `_analysis/daily_pipeline/sector_evidence/` 的带日期快照。精确与近似映射分开，行业／细分行业／题材分别展示；旧 `code2sector.json` 与 `sector_codes.json` 保留供此前研究入口使用，不能视为历史成分。当前接口、80% 广度门槛和运行方式见 [步骤 C 实施记录](docs/step-c-implementation.md)。

---

## 测试

独立策略的每日候选与历史评估共用 `core/screening_engine.py`：资格确认后才排序截断。每日任务的待资格采集范围不再预截断为 200 只；股票池最多 200 只、每策略最多 20 只。实验先登记再运行，未成交和未知执行事件保留，详见 [步骤 D 实施记录](docs/step-d-implementation.md)与[实验登记说明](docs/strategy-experiments.md)。

步骤 E 新增 `core/account_ledger.py` 和 `python -m pipeline.account_report`，每日任务在逐股跟踪后核算共享现金、持仓股数、应收分红、净值、实际模拟成交金额换手及风险预算。组合透视页读取账户报告；失败退出继续持仓，缺失行情保留未知收益。预算与完整输入、账本和同资本对照单独留档；当前真实冻结信号为零，没有前向账户收益证据。详见 [步骤 E 实施记录](docs/step-e-implementation.md)、[输入规范](docs/account-input-schema.md)及[示例预算](docs/examples/account-config.json)。

步骤 F 将主流程整理为市场、板块、策略候选、组合风险与验证记录；增加分层版本、完整输入与源代码归档、离线重放、监控分页、报告缓存和逐代码故障清单。SQLite 与研究资料支持带哈希校验的备份及新目录恢复，保存验证环境的 `requirements.lock.txt`。实际归档重放及恢复后的重放均一致；数据覆盖和真实连续运行仍待验收。详见 [步骤 F 实施记录](docs/step-f-implementation.md)及[运行维护说明](docs/operations.md)。

后续 P0 已接入完整资格范围的首次价格补齐、跨运行缓存、每日增量与重叠校验、独立复权因子补采、限速重试和失败恢复。`--refresh-prices` 可强制重查全部双价格历史；真实覆盖、缺日及十日连续运行进度见 [P0 数据与稳定性记录](docs/p0-data-stability.md)。

后续 P1 接入原生分类与上证基准、登记后前向检验、来源导入及逐证券数量／费用核算。本轮 330 个分类成员枚举通过，研究范围覆盖 2,446/2,468；申万 155 个行业指数与成员按原生代码关联，另保存 90 个同花顺指数和 12,925 条官方分类历史记录。轮动页面只读展示；历史时点完整性、真实执行输入及未来样本仍待验收。入口和详情见 [P1 实施记录](docs/p1-evidence-and-validation.md)。

```bash
python -m pytest tests/ -v    # 全量;mock 数据源,离线可跑
```

---

## 诚实声明与已知局限

本项目明确"不编造结果、失败如实记录"。当前研究结论:

1. **方向预测无真实 edge** —— 代理管线 composite 对次日方向几乎无区分度(校准 P(up) 落 0.42-0.49,valid 段全 hold)。已核实这是**真实结论、非代理假象**:股票级 composite 只读 close/high/low/volume,不依赖缺失的 amount/turnover。
2. **交易模拟无篮子正净期望** —— 基线 A(代理管线)−0.527%/笔,弱于全市场基准 D;`--ab` 实验证实 risk_p 条件化止损同样不改善(唯一正期望篮 B 被损害)。
3. **代理口径**:盲测篮 A 是"代理管线"(板块热用中位数 5 日涨幅 top3 代理,无生产 amount/turnover),非生产推荐口径。
4. **数据缺口**:本地日线 universe 缺 2020-04 → 2021-03 约 13 个月(2020-07 牛市主升、2021-02 见顶无法覆盖)。
5. **统计口径**:样本按「股票×时间」聚集、非 i.i.d.,n 为样本数而非独立观测数;显著性检验仅作定性参考。

规格/设计文档见 `docs/superpowers/specs/`(看板、推荐、龙头、评分 v3、热动量、盲测基线、预测引擎、环境分类等),研究决策留档见 `_analysis/decisions.md`。
