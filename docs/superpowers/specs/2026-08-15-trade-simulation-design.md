# 交易模拟 + 风险收益评价 设计(A1-A4)

- 日期:2026-08-15
- 状态:定稿(自审后)
- 范围:补齐大任务 §20(风险收益)/§21(交易模拟)/§12(真实结果快照)/§10(T+3 完整回测)
- 上游依赖:`backtest.py`(选篮)、`compare.py`(快照对比)、`predict.py`(ADVERSE_THRESHOLD/风险口径)
- 原则:严格时间隔离(交易决策只用 <=T,结果只读 >T);诚实声明日内假设;不伪造

---

## 0. 背景(为什么)

六维评估已三重确认:方向零 edge、path n=0、trend3 仅 +2.17pp 不可交易,唯一可交付的是风险校准(risk_p ECE 0.00989、lift 0.110)。当前 `backtest.py` 只报篮子**次日 mean%/win_rate**,回答不了大任务 §20/§21 反复强调的核心问题:

> 这套选股**作为一笔笔交易**,止损止盈后历史到底赚不赚、回撤多大、盈亏比如何?

本设计把"篮子盲测"升级为"逐笔交易模拟 + 风险收益评价",让唯一可用的 risk_p 转成可被历史检验的盈亏比/回撤/Profit Factor。

## 1. 组件拆分

三个增量,A1/A2 在现有模块上富化,A3+A4 为独立新模块。

### A1:持有期行情助手 + 真实结果补全(backtest.py + compare.py)

新增共享助手(供 compare.py 与 simulate.py 复用):

```python
# backtest.py
def holding_window(d, bar, h):
    """(bar, bar+h] 的真实 OHLCV 路径,bar 是买入决策 bar(含 close[bar] 作入场价分母)。

    返回 {"n": m, "open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]}
    - m = 实际可用根数(1 <= m <= h);尾部越界/非正价即截断;
    - 任一根 open<=0 或 close<=0 视为无效,截断到该根之前;
    - m == 0(连 T+1 都不可得)返回 None。
    时间隔离:只读 > bar 的行情,绝不回读 <=bar 特征参与决策。
    """
```

`compare.py` 的 `_actual_outcomes(d, bar)` 富化,在现有 `{close1, gap, od, trend3}` 之上补:

- `high1`/`low1`/`volume1`:T+1 单根的高/低/量(满足 §12 的 actual_high/low/volume)
- `path3`:`[r1, r2, r3]` 逐日 close-to-close 收益率(缺某根为 None)
- `cum3`:3 日累计收益 = close[bar+3]/close[bar]-1(尾部不足为 None)
- `max_high3`/`min_low3`:窗口内最高 high / 最低 low 相对 close[bar] 的收益(期间最高/最大回撤)

`verify()` 把逐条真实结果(含上述字段)写入输出 `actuals` 列表,做到"真实结果快照落库"(可审计、可复现),而非只在聚合指标里湮灭。

### A2:T+3 完整回测统计(compare.py)

`trend3` 维度在保留二分类 hit_rate 之外,新增基于 A1 字段的分布统计:

- `mean_cum3` / `mean_max_high3` / `mean_min_low3`:3 日累计收益、期间最高收益、期间最大回撤的均值

> 裁定:trend3 只补"3 日累计/期间最高/回撤"的**描述统计**(给交易模拟的持有期指标兜底),不新增逐日路径命中率——避免对死维度加细(违背 don't-do-again #3)。逐日路径 `path3` 由 A1 落库,仅供审计/复现,不参与评分。

### A3+A4:交易模拟引擎 + 风险收益评价(simulate.py 新模块)

新建 `simulate.py`(只读 pkl + code2sector,不 fetch),复用 `backtest.select_baskets` 逐评估日选篮:

```python
def run(data_dir, sector_map_path, holding_days=3, stop_pct=-0.08,
        take_pct=None, cost_bps=20):
    """逐评估日选篮 -> 每篮逐笔模拟 -> 逐篮 summary。"""
```

决策规则(交易"买什么")直接复用现有选篮,不做新方向旋钮:

- 每评估日 `select_baskets` 产出 A/E_hi/E_lo/B/C/D 六篮;
- 每篮每只入选股 = 一笔交易,入场价 = close[T](信号在收盘后生成,次日可成交近似用 close[T] 作 fill,文档声明)。

单笔模拟(保守日内假设):

```python
def simulate_trade(d, bar, holding_days, stop_pct, take_pct):
    """entry=close[bar];窗口 = holding_window(d, bar, holding_days)。
    逐日:若 open 跳空越过 stop/target 价,按 open 成交;
         否则日内 low<=stop 且 high>=target 同时触及,保守取 stop 先(先损);
         否则 low<=stop -> stop 价平;high>=target -> target 价平;
         都未触及 -> 持有到窗口末根 close。
    返回 {entry, exit, exit_reason(hold/stop/target), holding_days_actual,
          ret_gross, max_fav(期间最高), max_adv(期间最大回撤)}。
    """
```

聚合(逐篮):

```python
def summarize(results, cost_bps):
    """逐笔 ret_net = ret_gross - cost(单边?双边?——双边 2*cost,文档声明)。
    -> {n_trades, win_rate, avg_win, avg_loss, profit_factor(Σwin/Σ|loss|),
        expectancy, max_drawdown(按时间序净值曲线峰值回撤), avg_max_fav, avg_max_adv}。
    """
```

报告:逐篮一张表(A vs D vs E_hi vs E_lo vs B vs C),直接回答"生产管线 vs 全市场基准,作为交易,谁盈亏比/回撤更好"。risk_p 用法留作 §7 遗留(不在此阶段把 risk_p 塞进止损规则,避免方向旋钮)。

## 2. 关键接口契约(实现方与调用方对齐)

| 函数 | 位置 | 签名 | 返回 |
|---|---|---|---|
| `holding_window` | backtest.py | `(d, bar, h)` | dict `{n, open[], high[], low[], close[], volume[]}` 或 None |
| `_actual_outcomes` | compare.py | `(d, bar)` | 富化 dict(现字段 + high1/low1/volume1/path3/cum3/max_high3/min_low3) |
| `simulate_trade` | simulate.py | `(d, bar, holding_days, stop_pct, take_pct)` | 单笔 dict 或 None |
| `summarize` | simulate.py | `(results, cost_bps)` | summary dict |
| `run` | simulate.py | `(data_dir, sector_map_path, holding_days, stop_pct, take_pct, cost_bps)` | `{baskets: {name: {trades:[], summary:{}}}}` |

## 3. 时间隔离与未来数据

- 交易决策(哪只入选)复用 `select_baskets`,其评分输入 `<=T`(已在 backtest.py 固化);
- 入场价 close[T]、止损/止盈价由 close[T] 派生,只用 <=T;
- 结果(open/high/low/close of >T)只读 >T 的 bar;
- 无缓存跨日泄露:`holding_window` 只向前读。

## 4. 保守日内假设(诚实声明,写入报告)

日线无分钟序列(数据缺口),日内 stop/target 先后不可精确判定,采用保守约定:

1. open 跳空越过 stop/target 价位 -> 按 open 成交(跳空即触发);
2. 同日内 low<=stop 且 high>=target 同时触及 -> **保守取 stop 先**(先损后盈,最坏情形),避免高估收益;
3. 收盘后信号、次日 fill 的时滞一律按 close[T] 近似,不虚构分钟成交。

## 5. 配置旋钮(evidence_status 全部 provisional,后续回测校准)

| name | default | 说明 |
|---|---|---|
| HOLDING_DAYS | 3 | 持有窗口(根数) |
| STOP_PCT | -0.08 | 止损线(None=关) |
| TAKE_PCT | None | 止盈线(None=关) |
| COST_BPS | 20 | 单边成本 bps(净收益扣双边 2*cost) |
| SAME_DAY_RULE | stop_first | 同日双触保守取先损 |

## 6. 非目标(明确不做)

- 不做方向/开盘 5 级、盘中路径 10 型、个股短期趋势 10 态、T+3 逐日路径预测(C 组已冻结,don't-do-again #3);
- 不做分钟级日内路径(数据缺口);
- 不把 risk_p 塞进止损规则做成新方向旋钮(本阶段交易信号=现有选篮,风险收益评价独立于方向);
- 不做真实交易撮合/滑点/涨跌停不可成交建模(仅保守 open 跳空近似)。

## 7. 遗留(本阶段之后)

- 用 risk_p 作为持仓/止损参数化的对照实验(需先有 A3 基线才能 A/B);
- 板块 composite 仍不可从历史复现(R4),篮 A 为代理管线,报告继续沿用 backtest.py 的代理声明。
