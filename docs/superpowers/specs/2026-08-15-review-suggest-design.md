# 自动复盘 + 优化建议生成(review.py)设计

> roadmap #128。只读、只建议、不应用。消费 evaluate.py 的六维×八层×七环境评估报告,产出结构化复盘 + 数据锚定的优化建议(附验证配方)。应用改动与版本化是 #129 的职责。

## §1 目的与定位

现有闭环里「复盘 + 优化建议」是**人工**做的:探针(P0/P0b/P1/P2/P3/P4)各自产出机械规则 → 判定链 → 控制器裁定 → 回访线索,决策手写在 `_analysis/decisions.md`。本模块把其中**可自动化的部分**固化为只读代码:

1. **复盘(review)**:读 evaluate.py 报告,自动找出「弱项」(显著跑输总体的层/环境)与「无 edge」的维度,做错误归因。
2. **优化建议(suggest)**:把复盘发现映射为候选建议,每条**引用具体指标 + 样本量 + 显著性**,附「V1→V2 验证配方」。**无数据支撑则零建议,绝不编造。**

**铁律(继承大任务):**
- 只读:review.py 不 import 下划线私有、不改任何模块常量、不应用任何改动、不 fetch。
- 建议必须数据锚定:每条建议引用精确数值(n、层值、总体值、2σ 界)。
- 建议是**提案**:应用/回测/版本化由 #129 版本管理执行;建议只给出验证配方。
- 不过度加规则:建议规则集保持小而显式(§5),不引入 ML/启发式打分。

## §2 输入

主输入:evaluate.py `build_report` 产出的 JSON payload(单文件)。字段如下(逐字段,review.py 不得臆测):

```
system_version, module_version, generated_at, mode="evaluate"
data_range: {start, end}
valid_window: {start, end}
n_valid_records: int
overall: {dim: cell}           # 六维总体指标
layers:  {L: {dim: cell}}      # 八层 × 六维;薄层 cell = {n, _suppressed:true}
environments: {env: {dim: cell}}  # 七环境 × 六维;同样可 _suppressed
env_n:  {env: int}
layer_n:{L: int}
significant: [[L, dim, metric, v_l, v_o, se], ...]   # 仅「层 vs 总体」的 ±2σ 命中
se_bounds: [[L, dim, 2*se], ...]                    # 主指标 2σ 界
all_undifferentiated: bool
```

cell 按维度四种形态(direction/gap/trend3 / path / return / risk):

```
direction/gap/trend3: {n, hit_rate, base_rate, n_hold}
path:    {n, acc_path}
return:  {n, mae, rmse, sign_agreement, sign_n, mean_residual, dir_cond_mae}
risk:    {n, adverse_rate, ece, brier, lift}
```

`PRIMARY = {direction:"hit_rate", gap:"hit_rate", path:"acc_path", trend3:"hit_rate", return:"sign_agreement", risk:"adverse_rate"}`(镜像 evaluate.py:29)。

**关键事实(§6 依据)**:六维 PRIMARY 指标全部是**二项比例**(hit_rate/acc_path/sign_agreement/adverse_rate),故 review.py 可由 `p` + `n` 自算两比例差的标准误,无需 std(与 evaluate `_binom_diff_se` 同式)。

## §3 输出

review.py CLI(`--in evaluate_report.json --out review_report.json`)写 JSON + 同基名 MD。JSON schema:

```
{
  mode: "review", module_version, system_version, generated_at,
  source: {path, evaluate_system_version, valid_window, n_valid_records},
  review: {
    overall_summary: [{dim, primary, n, value, baseline, edge}],   # edge = value - baseline
    weak_layers:   [{layer, dim, value, overall, se, n}],          # 显著跑输(层)
    strong_layers: [{layer, dim, value, overall, se, n}],          # 显著跑赢(层)
    weak_environments:   [{env, dim, value, overall, se, n}],      # 显著跑输(环境,自算)
    strong_environments: [{env, dim, value, overall, se, n}],
    no_edge_dims: [dim],          # 主指标 ≈ baseline(无信息量)
    undifferentiated: bool,       # all_undifferentiated
    degenerate_environments: [env],  # env_n < MIN_STATE_N
    thin_layers: [L],             # layer_n < MIN_LAYER_N
  },
  suggestions: [suggestion],
  knobs: [knob_entry],            # 可调旋钮清单(文档化真相源)
}
```

suggestion 形态:

```
{id, kind, knob: str|null, current, proposed,
 evidence: str,          # 精确引用:环境/层 + 指标 + 值 vs 总体 + n + 2σ
 validation_recipe: str, # 如何 V1→V2 在 held-out 上验证
 confidence: "low"|"med"|"high",
 requires_probe: bool}   # true = 需额外探针(不自动验证)
```

> `current` / `proposed` 是给人读的自由文本(可为空串);`knob` 非 None 时二者对应 §6 注册表该 name 的 current 与拟议值。输出 `knobs` 字段 = §6 完整 KNOBS 注册表(真相文档,不按本次复盘过滤),供 #129 读取「可版本化项」。

## §4 复盘定义

`review(payload) -> review_payload`,全部由 payload 字段派生:

1. **overall_summary**:逐维取 PRIMARY 指标 + 无信息基线。baseline:direction/gap/trend3 用 `base_rate`;path 用 0.25(四分类机会);return 用 0.5;risk 无基线(adverse_rate 越低越好,单独报 ece/brier/lift)。`edge = value - baseline`。无 edge 判定(no_edge_dims):direction/gap/trend3 `|hit_rate - base_rate| <= se(单侧 1σ,即 sqrt(base_rate*(1-base_rate)/n))`;path `|acc_path - 0.25| <= sqrt(0.25*0.75/n)`;return `|sign_agreement - 0.5| <= 0.5/sqrt(sign_n)`。risk 不做无 edge 判定。
2. **weak/strong_layers**:读 payload `significant`(覆盖 9 个 (dim, metric) 组合:六维主指标 + return 的 mae/rmse/mean_residual),**按 `metric == PRIMARY[dim]` 过滤**后 `v_l < v_o` → weak、`v_l > v_o` → strong。附 `se_bounds[(L,dim)]`(仅主指标有 2σ 界)。
3. **weak/strong_environments**:payload 无环境显著性,review.py 自算。对每个非 `_suppressed` 的 `environments[env][dim]`,取 PRIMARY 指标,与 `overall[dim]` 做二项比例差:
   `se = sqrt(p_l*(1-p_l)/n_eff_l + p_o*(1-p_o)/n_eff_o)`,其中 direction/gap/trend3 用 `n - n_hold`、path/risk 用 `n`、return 用 `sign_n`(镜像 evaluate `_significance` 的有效 n 规则)。有效 n 任一侧 < MIN_LAYER_N(30)→ 跳过;`|v_l - v_o| > 2*se` → 显著,按方向入 weak/strong。
4. **no_edge_dims / undifferentiated / degenerate_environments / thin_layers**:按 §4.1 与 `env_n < MIN_STATE_N(20)`、`layer_n < MIN_LAYER_N(30)` 填。

## §5 建议规则集(小而显式)

`suggest(review_payload) -> list[suggestion]`。每条规则独立、数据锚定;无命中 → 空表(诚实)。规则:

- **R1 层反信号**:weak_layers 中存在 direction/gap/trend3 且 `hit_rate < base_rate`(反信号)或 path `acc_path < 0.25` → 建议「该层为反信号,考虑在评分/选股中降权或排除该层标的」。evidence 引用 `{layer, dim, hit_rate vs base_rate, n, 2σ}`。knob=None(行为建议,非单旋钮)。confidence="med"。validation=「held-out 重跑 evaluate,看该层反信号是否跨期稳定」。
- **R2 环境反信号/无优势**:weak_environments 中存在方向维 `hit_rate < base_rate` → 建议「该环境下预测无优势,考虑更保守(加宽 ε-band 使更多 hold 或加警示徽章)」。knob="DIRECTION_BAND"。evidence 引用 `{env, dim, hit_rate vs base_rate, n, 2σ}`。confidence="med"。requires_probe=false。
- **R3 方向维无区分度**:`no_edge_dims` 含 direction 或 `undifferentiated` → 建议「方向维无方向性 edge(与 S1/P2 结论一致);接受为结论,或加宽 DIRECTION_BAND 减少无信息下注」。knob="DIRECTION_BAND"。confidence="high"(方向无 edge 已多次复现)。validation=「对比不同 DIRECTION_BAND 下 up/down 调用数 vs 命中率,无提升则维持」。
- **R4 风险校准偏离**:risk 维 `ece > 0.10` → 建议「风险校准器 ECE 偏高(ece=…),考虑重审风险分箱(N_BINS)或校准样本」。evidence 引用 `{ece, n}`。knob="N_BINS"。confidence="low"。requires_probe=true(ECE 复核需 predict 内部样本,review 只读 payload 不持有)。注:risk cell = `{n, adverse_rate, ece, brier, lift}` 不含 mean(risk_p),故仅以 ece 触发。
- **R5 收益系统性偏估**:return 维 `|mean_residual| > 0.02` → 建议「expected_return 系统性偏估(mean_residual=…),考虑加偏移校准」。knob=None。confidence="low"。requires_probe=true。
- **R6 薄层/退化(警示,不产建议)**:`degenerate_environments` / `thin_layers` 非空 → 不产出 suggestion 条目。它们已在 review 的 `degenerate_environments` / `thin_layers` 字段如实列出,作为「样本不足(n<阈值),不足以支撑该层/环境建议」的警示;`suggest()` 对这两类字段恒不产建议。

> 规则集上限 6 条,全部由 payload 可派生;不新增需要 evaluate.py 之外数据的规则(那些标 `requires_probe=true` 指向人工探针)。

## §6 可调旋钮注册表(knob registry)

`KNOBS` 为模块级常量(文档化真相源,非执行器),每项:

```
{name, locator, current, type, domain, controls, evidence_status, validation_recipe}
```

`locator` 二选一:`{module, attr}`(命名常量)或 `{module, func, note}`(内联字面量/非 Python 文件)。`evidence_status` ∈ {"backtest-calibrated"(decisions.md 有探针定稿), "provisional"(暂定/未标定), "manual-ruling"(控制器裁定,不可机械推导)}。初版清单(逐字核自代码):

| name | locator | current | controls | evidence_status |
|---|---|---|---|---|
| verdict_thresholds | analysis.stock_verdict(内联) | 67/62/52/42 | 个股五档 verdict 分界 | manual-ruling(P2 定稿 unchanged;live 单日分布标定,板块 composite 历史不可复现 R4 缺口;机械重锚 [56,51,41,31] 已被 P2 否决) |
| sector_bonus_tiers | recommend.sector_bonus(内联) | 68/60/50→+8/+4/-5 | 板块共振加成档 | backtest-calibrated(live §9.4 重锚;-5 边界刻意不对称;板块 composite 历史不可复现 R4 缺口,历史验证不可能) |
| HOT_COMPOSITE_THRESHOLD | recommend.attr | 68.0 | 热板块谓词门槛 | manual-ruling(P0b 定稿维持 68) |
| HOT_WEIGHT_MODE | recommend.attr | "signal" | 热权重模式 | manual-ruling(P0b 定稿 signal) |
| V3_WEIGHTS | analysis.attr | (0.55,0.15,0.10,0.20) | 默认股票质量权重 | provisional(规格) |
| HOT_SIGNAL_WEIGHTS | analysis.attr | (0.40,0.15,0.10,0.35) | signal 模式权重 | manual-ruling(P0b) |
| HOT_REL_WEIGHTS | analysis.attr | (0.40,0.15,0.10,0.20) | rel 模式权重 | provisional |
| BONUS_GE75 | recommend.attr | False | +8 门槛提到 ≥75 | manual-ruling(P3 定稿 False) |
| BONUS_QUALITY_GATE | recommend.attr | False | quality<50 不给加成 | manual-ruling(P3 定稿 False) |
| PRICE_FLOOR | recommend.attr | None | 低价护栏 | manual-ruling(P4 定稿 None) |
| OVERHEAT_MIN_DAYS | analysis.attr | 4 | 过热连涨天数(徽章) | manual-ruling(P0 定稿 badge,不消费 N) |
| P0_PATH | analysis.attr | "badge" | 过热板块处理:intercept(排除)/badge(仅徽章) | manual-ruling(P0 定稿 badge;过热方向支持但 Welch t≥0.076 不显著) |
| MIN_AMOUNT | recommend/backtest.attr | 1e8 | 流动性下限 | provisional |
| BIG_DROP_PCT | recommend.attr | -7.0 | 大跌排除线 | provisional |
| QUALIFYING_VERDICTS | recommend.attr | ("建议关注","跟踪(热点延续)") | sector_bonus 资格的 verdict 集 | provisional |
| risk_exclude | recommend.rank_candidates(内联) | 70 | risk≥70 排除 | provisional |
| GAP_WARN_PCT | static/app.js:9(前端) | -0.682 | 前端隔夜跳空警示 | backtest-calibrated(P1 定稿) |
| N_BINS | predict.attr | 10 | 校准等量分箱数 | provisional |
| DIRECTION_BAND | predict.attr | 0.05 | ε-band 方向 hold 区 | provisional |
| ADVERSE_THRESHOLD | predict.attr | -0.03 | 风险 adverse 定义 | provisional |
| TRAIN_FRAC | predict.attr | 0.8 | 训练/valid 切分 | provisional |
| EVAL_DAYS | predict/backtest.attr | 1200 | 评估窗长 | provisional |
| environment_thresholds | environment.attr(15 常量) | 见 environment.py:26-39 | 七态分类阈值 | provisional(§10 已锚分布未调参) |
| MIN_STATE_N | environment.attr | 20 | 环境态 n<20 标退化 | provisional(design) |
| layer_predicates | evaluate.attr(6 常量) | TOP_SECTORS=3/AMOUNT_TOP_FRAC=0.20/HIGH_POS_RATIO=0.97/OVERSOLD_RET=-0.15/AMP_THRESHOLD=15.0/MIN_LAYER_N=30 | 八层谓词定义 | provisional |

> 上表 `-` 均为 ASCII 连字符(负数);Python 源码禁用 U+2212。注册表是**真相文档**,不驱动 mutation;review.py 只读。

## §7 诚实约束

1. 只读、不应用:review.py 绝不 `setattr` 任何模块常量、不写 `decisions.md`、不改 JS。
2. 不编造:建议为空是合法结果(全部规则不命中 → `suggestions: []`)。所有数值引用 payload,不臆造。
3. 样本非 i.i.d.:报告继承 evaluate 声明——n 是「股票×时间」样本数,非独立观测;建议置信度只标 low/med/high,不给伪精度。
4. 退化态/薄层:n < 阈值只标「样本不足」,不产优化建议。
5. 环境显著性:review.py 自算(二项比例差),公式与 evaluate `_significance` 同源;均值型指标(mae/rmse/mean_residual)不做环境显著性(payload 无 std)。
6. 数据缺口:2020-04→2021-03 缺失(见 environment 标定),任何环境/层结论都不覆盖该区间,如实声明。
7. 多重比较:`significant[]` 是 9×8=72 项原始逐项 ±2σ,**未做多重比较校正**;review.py 的 weak/strong 层/环境是「提示性」非「确认性」,由显著性派生的建议置信度上限 med。
8. 控制器裁定不可机械推导:HOT_WEIGHT_MODE/verdict/bonus 等 manual-ruling 旋钮是人工判定(含「有限可逆」边界),auto-suggester 不得据此自动翻转;触及它们的建议须加「控制器裁定,须人工重推」告警并降置信度。
9. 单一口径:review.py 只消费单一 evaluate 报告;不跨模块(compare/backtest)对比未归一化的指标(AFTER_CLOSE 锚点、n_eval 日区间各模块不同)。

## §8 边界(#129/#130)

- **#128(本模块)**:产出建议 + 验证配方,不应用。
- **#129 版本管理**:读建议 → 应用到受控版本(V1.0→V1.1)→ 按验证配方跑 held-out 对比 → 如实记录保留/回退。
- **#130 六层自审**:消费 #128 复盘 + #129 版本对比 → 最终报告。
- review.py 的 `knob` 字段只引用 §6 注册表内 name;#129 据此知道「什么可版本化、如何验证」。

## §9 测试(初版清单)

`tests/test_review.py`,用 `_fake_payload()` 构造评估报告(镜像 test_evaluate `_fake_report` 的 cell 形态):

1. `test_overall_summary_edge`:direction hit_rate=0.55/base_rate=0.50 → edge=0.05;path acc_path=0.30 → edge=0.05(vs 0.25)。
2. `test_no_edge_direction`:hit_rate=0.51/base_rate=0.50/n=400 → direction 入 no_edge_dims;hit_rate=0.60 → 不入。
3. `test_weak_layer_from_significant`:significant 含 ("趋势","direction","hit_rate",0.42,0.55,0.02) → weak_layers 含趋势;v_l>v_o 侧 → strong_layers。
4. `test_env_significance_self_computed`:环境 direction hit_rate=0.40/n=100 vs overall 0.55/n=1000,有效 n 侧 100≥30 → 2σ 显著 → weak_environments 含该环境;n=10 → 跳过(有效 n<30)。
5. `test_suggest_r1_antisingal`:weak_layer direction hit_rate<base_rate → suggestions 含 kind="layer_antisignal",evidence 引用数值。
6. `test_suggest_r3_no_edge`:no_edge_dims 含 direction → 含 kind="direction_no_edge",knob="DIRECTION_BAND"。
7. `test_suggest_empty_when_clean`:全维正常、无显著、无退化 → suggestions==[]。
8. `test_knob_registry_unique_and_typed`:KNOBS 内 name 唯一、locator/evidence_status 合法。
9. `test_main_end_to_end`:tmp 写 payload JSON → `main(["--in",..., "--out",...])` rc=0,写 JSON+MD,MD 含「复盘」「建议」「可调旋钮」节。
10. `test_review_no_utf8_minus`:源码文件不含 U+2212(读 review.py 断言)。

## §10 收尾

- 全量 pytest(既有 263 + 新增 ≥10);2 条既有 test_api.py 日期漂移范围外。
- 真实缓存冒烟:`python review.py --in _analysis/evaluate_report.json --out _analysis/review_report.json`,人工核对建议是否数据锚定、无编造、空表合法。
- 本模块产物 gitignored(进 `_analysis/`);review.py + tests 提交 main。
