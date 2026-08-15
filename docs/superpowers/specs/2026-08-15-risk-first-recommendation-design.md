# 推荐算法改进:风险尾部 + 高位硬过滤(设计)

- date: 2026-08-15
- status: 已实现(先验设计被探针证伪后,已按数据修正为「尾部硬过滤 + composite 降序」)
- scope: `recommend.py::rank_candidates` + `analysis.py::score_stock`(暴露 pos60)

## 1. 背景与动机

历次盲测已经收敛到三个确定结论(见 `_analysis/e_lo_momentum_slice.md`、`_analysis/backtest_baseline.md`):

1. **方向无 edge**。篮 A(热板块 top5 代理管线)次日收盘口径 close1 净收益约 −0.41%(20bp 往返成本),win_rate 54.8% vs 全市场基准 D 59.8%,Welch p=0.49 —— 与 D 无显著差异,方向预测不可改进。
2. **风险是唯一被验证的真实信号**。风险校准器 ECE=0.0099、lift=0.1104:risk 分对 P(close1<−3%) 校准良好,即「高分风险确实预测次日大幅下挫」。这是唯一可据以改进的维度。
3. **高位(高扩展)股稳定跑输**。篮 E_lo(热板块内 position 分 bot5 = 60 日位置最高者)close1 净 −0.65%,对 D 配对 t=−2.82、p=0.005,逐年逐环境皆负。原始 60 日价格乖离排名(hi_ext)同样 −0.17% 且显著(p=0.026)。即「高位股日内虽有动量延续,但被更大的负隔夜 gap 吃掉」,收盘买入不可交易。

**结论**:既然无法造出正向 alpha,唯一诚实的改进是把「避损」做实 —— 用已验证的风险信号 + 高位信号做**硬过滤**,并把幸存者按 composite 降序排序。目标不是扭亏为盈,而是让推荐的净期望**更少地负**(loss reduction),并压低左侧尾部(次日大跌概率)。

## 2. 现状

`recommend.py:89-93` 当前 `rank_candidates`:

```python
def rank_candidates(scored, per_sector):
    kept = [x for x in scored if x["scores"]["risk"] < 70 and x["verdict"] != "回避"]
    kept.sort(key=lambda x: x["composite"], reverse=True)
    return kept[:per_sector]
```

问题:
- 风险门槛 `70` 过高。风险分是加性 cap 100,`70` 只拦掉「乖离>30% 或叠加多个空头旗标」的极端股;而风险校准器显示 risk≥40 已经显著抬升次日大跌概率。
- 未使用高位(扩展)信号。position 分的底端(高位)正是 E_lo 证伪的负期望区,但 composite 的 position 权重(0.55)只是线性压低,不构成硬门槛。

## 3. 设计(经探针修正后的最终版)

> 先验设计曾假设「风险越低越好 → 按风险升序排序 + RISK_HARD_CUT=40」。
> `_analysis/risk_pos60_probe.py`(318k stock-day 无条件样本)证伪了该假设,见 §5。
> 最终设计只保留「风险尾部 + 高位」两个**硬过滤**,排序维持 composite 降序。

### 3.1 暴露 pos60

`analysis.py::score_stock` 返回值增加 `pos60` 键(= `_pos60(daily_df)`,0..1)。这是纯增量:
- 短历史守卫( `<61` 根)分支也返回 `pos60: None`。
- `_pos60` 已存在且被 position 分使用,无需新算法;新增字段仅是把它暴露给推荐层做硬过滤。
- `_score_candidate` 已原样透传 `scores` 字典,故 pos60 自动流入 `x["scores"]["pos60"]`,并随 payload 的 `stocks[].scores` 出现在 API 响应(纯新增键,向后兼容)。

### 3.2 常量(§5 探针锚定)

```python
RISK_HARD_CUT = 60.0        # 风险尾部硬过滤:risk ≥ 60 剔除(只砍崩溃尾部;原 70 太高,先验 40 会误砍最优均值段)
EXT_HARD_CUT = 0.85         # 高位硬过滤:pos60 ≥ 0.85 剔除(E_lo 证伪高位股)
```

锚定依据(来自 §5 无条件探针,非网格扫描 OOS 反选):
- `RISK_HARD_CUT=60` 对应风险分位数 p99=60:risk≥60 只占约 1% 的 stock-day,且是 close1 净收益 −0.66 ~ −1.33 的崩溃尾部(风险校准器 lift=0.1104 的靶区)。**先验值 40 被探针证伪**:risk∈[0,20) 虽最低风险,但净收益 −0.433 是均值最差档(84.6% 样本=低波横盘「死股」,低风险只因没动);净收益最优档是 [20,60)。故「风险越低越好」不成立,40 会误砍最优均值段。
- `EXT_HARD_CUT=0.85` 对应 E_lo 证伪的高位区:pos60 分位 p90=0.855。0.85 只砍最极端 ~10% 高位股,不误伤「横盘位」。

### 3.3 重写 rank_candidates

```python
def rank_candidates(scored, per_sector):
    """风险尾部硬过滤 + 高位硬过滤;composite 降序取前 per_sector。"""
    def keep(x):
        s = x["scores"]
        risk = s.get("risk")
        pos60 = s.get("pos60")
        if risk is None or risk >= RISK_HARD_CUT:
            return False
        if pos60 is not None and pos60 >= EXT_HARD_CUT:
            return False
        return x["verdict"] != "回避"
    kept = [x for x in scored if keep(x)]
    kept.sort(key=lambda x: -(x["composite"] or 0))
    return kept[:per_sector]
```

语义:
- **硬过滤非补偿**:risk≥60 或 pos60≥0.85 的个股,无论 composite 多高都被剔除(硬门槛,非折扣)。
- **排序维持 composite 降序**:风险**不作为排序键**。探针证伪「风险升序优先」—— 低风险档 [0,20) 恰是均值最差的「死股」区,按风险升序会把它们排在前面。风险只做尾部硬切除,排序交给 composite。
- **防御语义**:`risk=None` 恒剔除(生产不会发生,`<61` 根已被 `_score_candidate` 的 composite-None 守卫跳过);`pos60=None` 视为「无扩展信息 → 不硬过滤」(fail-open,与现状 `risk<70` 隐式假设一致的防御)。

## 4. 诚实边界(本改进不承诺什么)

1. **不产生 alpha**。本改进只减少下行期望,不改变「方向无 edge」的事实。净期望可能仍是负的,只是**更少地负**。
2. **不改方向信号**。不动 direction/gap/path/trend3/return 任何维度;不改 `stock_composite_v3` 权重或 `sector_bonus`。
3. **不动「可介入龙头」路径**。`collect_actionable_leaders` 已按 verdict 分层(强烈关注/关注→可介入),其 composite 已含风险折扣,不在本次范围。
4. **可能减产能**。硬过滤会让推荐列表更短(每板块 <5 只,或整板块 too_few 跳过)。这是「宁缺毋滥」的有意取舍,须在 §6 测量中量化产能损失,而非静默接受。

## 5. 阈值验证(数据探针,只读,gitignored)

`_analysis/risk_pos60_probe.py` 在离线 buyable universe(与 backtest 同 seed/start/step)上收集全部 stock-day 的 `risk`/`pos60`/`close1`。实测结果(318,064 stock-day):

**risk 分位数**:p50=0, p75=0, p90=20, p95=35, p99=60。
**risk 分段 close1 净收益%(20bp 往返成本)**:
| risk 段 | net% | win% | n | 解读 |
|---|---|---|---|---|
| [0, 20) | −0.433 | 45.7 | 269,207 | 84.6% 样本=低波死股,均值最差 |
| [20, 40) | −0.114 | 50.3 | 38,696 | 均值最优 |
| [40, 60) | −0.118 | 48.0 | 6,760 | 均值最优 |
| [60, 80) | −0.657 | 42.8 | 2,601 | 崩溃尾部(≈p99 起) |
| [80, 101) | −1.334 | 42.5 | 800 | 极重尾部 |

**pos60 分段**(净收益% 随 pos60 单调恶化 −0.224 → −0.548):高位确实跑输,支撑 `EXT_HARD_CUT=0.85`(p90 锚)。

**三条结论(推翻先验)**:
1. **风险是尾部信号,不是均值信号**:risk 只在 ≥60 的崩溃尾部显著恶化;[0,20) 的「低风险」恰是均值最差的死股区。故「风险升序优先」方向性错误。
2. **风险只该做尾部硬切除** `RISK_HARD_CUT=60`(≈p99),不该做排序键。
3. **pos60 单调**、高位区被 E_lo 证伪,`EXT_HARD_CUT=0.85`(≈p90)成立。

## 6. 效果测量(新旧对比,诚实口径)

新建 `_analysis/risk_first_slice.py`(gitignored),复用 E_lo 切片骨架(同 seed/start/step/RNG、score_cache),额外构建 **NEW 篮**:
- 同 `select_baskets` 的热板块 hot(中位数 5 日涨幅 top3)与 buyable;
- 每热板块内:`sc=get_score(...)`,`risk=sc["risk"]`,`pos60=sc["pos60"]`;
- 应用 §3.3 硬过滤 + composite 降序排序,取 top `PER_SECTOR` → NEW 篮。

对比项(NEW / A(旧代理)/ D 基准,close1 与 od 双口径):
- 整体 gross%/net%(扣 20bp 往返)/win%/n;
- NEW vs D、NEW vs A 的配对 t(spread);
- 前后半段稳定性;按年;按市场环境(七态)。

**成功判据(诚实,非造 alpha)**:NEW 的 close1 net% 显著**高于**(更少负)旧篮 A 与基准 D,或 NEW 的 win_rate / 左尾(P5)显著改善;若 NEW 反而更差或产能塌方,如实报告并回退 §3.2。

## 7. 测试(TDD)

`tests/test_analysis_stock.py`:
- 改 `test_score_stock_v3_guard_and_no_verdict`:期望键集合加入 `pos60`;短历史分支断言 `pos60 is None`。
- 新增 `test_score_stock_pos60_exposed`:构造已知 60 日区间,断言 `score_stock(...)["pos60"]` 与 `_pos60(daily_df)` 一致。

`tests/test_recommend.py`:
- 改 `test_rank_candidates`:为 scored 项补 `pos60`,并断言 composite 降序语义。
- 新增 `test_rank_candidates_risk_hard_cut`:risk=60(≥cut)被剔、risk=59.99 保留。
- 新增 `test_rank_candidates_ext_hard_cut`:pos60=0.85 被剔、0.8499 保留;pos60 缺失则 fail-open 保留。
- 新增 `test_rank_candidates_risk_none_dropped`:risk=None 恒剔除。
- 新增 `test_rank_candidates_composite_desc_not_risk_asc`:低风险档**不应**仅因 risk 低就排到高 composite 前(探针证伪风险升序)。

## 8. 范围与交付

- 修改:`analysis.py`(score_stock 暴露 pos60)、`recommend.py`(常量 + `rank_candidates`)、两个测试文件。
- 不改:`backtest.py`(基线固化)、`simulate.py`、`evaluate.py`、`predict.py`、`environment.py`、`collect_actionable_leaders`。
- 探针(§5/§6)与阈值报告全部 gitignored(`_analysis/`),不提交。
- 提交到 main(用户偏好:直提交 main,无 worktree)。
