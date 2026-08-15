# risk_p 止损参数化对照实验设计(§7)

- 日期:2026-08-15
- 状态:定稿(自审后)
- 范围:补齐 trade-sim 设计 §7 遗留 —— 用 risk_p 参数化止损,做 flat vs tiered 对照
- 上游依赖:`simulate.py`(A3 基线)、`predict.py`(risk 校准器)、`backtest.py`(选篮)
- 原则:严格时间隔离;risk_p 只用于止损距离、不参与选篮(不新增方向旋钮);描述性对比,不伪造显著性

---

## 0. 背景(为什么)

A3 基线已确认:平坦止损 stop=-8% 下,代理管线 A 每笔净期望 −0.527%,盈亏比 0.727,所有篮子净负。唯一有真实校准 lift 的是 risk_p(ECE 0.00989、lift 0.110,预测 close1<-3% 的校准概率优于随机)。§7 问:

> 把 risk_p 转成**逐笔止损距离**(高风险收窄、低风险放宽),能否改善净期望/盈亏比?

这是"风险校准是否可交易"的最终检验。预期(诚实):底层选股无正向 edge,risk 条件化止损最多削尾损、改善盈亏比,不改变"净负"结论;但这是唯一的可能正向路径,值得一次性证伪。

## 1. 实验定义(E1)

- **平坦对照(flat)**:所有交易 stop_pct = base_stop = -0.08(与 A3 基线同)。
- **分级止损(tiered)**:`stop_pct = risk_tiered_stop(risk_p)`,按 risk_p 线性内插。
- **配对比较**:两种方案在同一批交易(同 code/date/entry)上各模拟一次,逐笔对比净收益差。

```python
def risk_tiered_stop(risk_p, base_stop=-0.08, tight=-0.04, loose=-0.12):
    """risk_p ∈ [0,1] → 止损幅度线性内插。
    risk_p=0(低风险)→ loose(-0.12,放宽给空间);risk_p=1(高风险)→ tight(-0.04,收窄快止损)。
    risk_p=None(校准不可得)→ base_stop(退回平坦)。"""
    if risk_p is None:
        return base_stop
    return loose + (tight - loose) * risk_p
```

端点 loose=-0.12/tight=-0.04 为 **provisional**(后续回测校准);base_stop=-0.08 处 risk_p=0.5 与平坦对齐,便于解读。持有窗口 HOLDING_DAYS=3、双边成本 COST_BPS=20、止盈 take=None 与 A3 一致。

## 2. risk_p 的取得(时间隔离关键)

risk_p = `cals["risk"].p_up(sc["risk"])`,其中:
- `sc["risk"]`:analysis.py 的 0-100 风险分,只读 ≤T(backtest.score_at 已固化);
- `cals["risk"]`:`predict.py` 的 risk 校准器(风险分 → P(close1<-3%)),**在 train 段(前 80% 评估日)拟合**。

因此 valid 段(后 20%)的 risk_p 严格 out-of-sample:risk 分 ≤T,校准器 ≤train 窗口,均不触及 >T。复用 `predict.run_backtest()` 返回的 `calibrators["risk"]`,避免重拟合漂移。

## 3. 选篮与窗口(与 A3/基线一致)

- 选篮复用 `backtest.select_baskets`(同 seed=0、同 start/step),risk_p **不参与**选篮。
- 评估日 = 与 predict.py 相同的 `eval_days`,train/valid 按 `TRAIN_FRAC=0.8` 切分,valid 段逐笔模拟。
- 六篮 A/E_hi/E_lo/B/C/D 都跑,headline 是 A(代理管线)。

## 4. 汇总与对比口径

逐篮输出:flat 与 tiered 各自的 summary(n/胜率/盈亏比/期望/最大回撤/平均盈亏),外加:

- `mean_diff`:配对逐笔净收益差 `mean(tiered_net - flat_net)`,n = 配对笔数。

对比为**描述性**(paired 逐笔差异),不报显著性 p(valid 段样本有限、非 i.i.d.),避免伪造结论。

> 注:选中股的 `sc["risk"]` 恒为有效 float(`stock_composite_v3` 在 risk=None 时返回 composite=None,`score_at` 因此只返回 risk 非 None 的 sc),故 risk_p 恒可计算;`risk_tiered_stop` 仍保留 None 防御分支,但正常路径不触发。

## 5. 配置旋钮(evidence_status 全部 provisional)

| name | default | 说明 |
|---|---|---|
| BASE_STOP | -0.08 | 平坦止损(对照) |
| TIER_TIGHT | -0.04 | risk_p=1 端止损(收窄) |
| TIER_LOOSE | -0.12 | risk_p=0 端止损(放宽) |
| HOLDING_DAYS | 3 | 与 A3 同 |
| COST_BPS | 20 | 与 A3 同 |

## 6. 非目标(明确不做)

- 不做 (tight, loose) 网格扫描寻优(留后续,先证伪"任一 risk 条件化是否有效");
- 不改选篮、不加方向旋钮(risk_p 只进止损距离);
- 不做持仓参数化(holding_days 随 risk_p 变)——本实验只动止损,单变量;
- 不做显著性检验(描述性 + 配对差异)。

## 7. 交付

`simulate.py` 新增 `risk_tiered_stop` + `run_risk_ab` + `build_ab_report` + `render_ab_markdown` + CLI(`--ab` 开关)。报告逐篮 flat vs tiered 表 + 配对差异 + 声明。单测覆盖 `risk_tiered_stop` 与 `run_risk_ab` 布线。
