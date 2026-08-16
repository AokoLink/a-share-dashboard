# A股三层分析看板 + 离线回测/预测/评估研究管线

本地运行的 A 股分析工具,两个使用面:

- **在线看板**(`app.py`,Flask + ECharts):大盘 → 板块 → 个股三层实时分析,外加股票推荐 / 可介入龙头 / 交易回测三个视图。
- **离线研究管线**(一组只读 CLI 模块):读本地日线数据做「历史盲测 → 概率预测 → 交易模拟 → 快照验证 → 六维评估 → 环境分类 → 自动复盘」,输出 JSON + Markdown 报告。全程严格时间隔离、不编造结果。

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
| **持仓诊断** | 输入代码/自选 → 1/3/5/10/20 日 P(涨)曲线 + 期望收益,标注是否超过成本、与全市场基准对比;测不到明说「信号不足,无法判断」(读取 `_analysis/forward_calib.json`,需先跑 `python -m core.forward`) |
| **自选股** | 客户端自选(localStorage 持久化),点击查看个股 |

> 盘中可用性:个股量比按全日折算盘中即比;板块"成交额放量/放量滞涨"仅收盘后计算,盘中自动降级。手动刷新 + 可选 60s 自动刷新。

---

## 离线研究管线(只读 CLI)

这些模块只读 `_analysis/daily/*.pkl`(个股日线,9 列)与 `_analysis/code2sector.json`(GBK),**不 fetch、不写库**;评分输入 `≤ T`,验证只读 `> T`,严格防未来数据泄露。每步默认同时产出 `.json` + `.md`。

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
- **板块映射**:`code2sector.json`(THS 行业 → 新浪行业,GBK)+ `sector_codes.json`,启动时校验板块标签漂移。

---

## 测试

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
