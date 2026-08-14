# 快照对比器(预测快照 vs 真实结果)设计

- 日期:2026-08-15
- 状态:草案,待评审
- 上游:预测引擎 `predict.py`(前向快照 `prediction_snapshot.json`)、评分系统 `evaluate.py`(六维指标口径)
- 触发:审计/回测/优化大任务第 5 点「预测快照(含 system_version)与真实结果快照分离,验证阶段才读真实结果」

## 1. 目标

新增生产级只读模块 `compare.py`,把「冻结的前向预测快照」与「后来到达的真实行情」做**严格时间隔离**的比对:快照里的预测字段是 `as_of_date` 当日用 ≤T 数据冻结的,验证阶段**只读 > as_of_date** 的真实 close/open 算出实际结果,统计各维度准确率。这是闭环中「预测 → 等 → 拿真实结果 → 比对 → 统计准确率」的落点,与回测/评分系统的历史验证互补(它们测的是历史窗口,本模块测的是**真正前向**的那份冻结预测)。

核心不变式:**comparator 不重跑 `score_at`/`predict_at`/任何校准器**——校准器与原始行情不在快照里,快照里的预测字段就是唯一真相源。comparator 的代码版本、快照的代码版本、验证数据区间三者各自独立记录,互不冒充。

## 2. 范围

**包含:**

- 读一份冻结快照(`--snapshot`,默认 `prediction_snapshot.json`),解析 `as_of_date` + `predictions[]` + 版本戳。
- 用 `_analysis/daily/*.pkl`(真实行情,已含 > as_of_date 的后续交易日)定位每只预测股在 `as_of_date` 的 bar,读 T+1 / T+3 真实 close/open 算实际标签。
- 六维准确率:**方向 / 开盘 / 路径 / T+3 趋势 / 涨跌幅误差 / 风险**。口径镜像 `predict.py` 的指标函数,但输入是「已存预测字段」而非「composite + 校准器」。
- 诚实报告:每维 `n`、可验证/不可验证计数(按原因)、字段覆盖声明、时间隔离与快照不可变声明。
- 反泄露测试(§10)。

**不包含(后续或独立项):**

- **不 fetch、不重拉数据**:comparator 与 predict/backtest/evaluate 同契约,只读 `_analysis/daily`。数据刷新是 `data_source` 的独立操作,不在本模块。
- **不改 `predict.py`**:本次里程碑只验证已冻结的 08-11 快照(4 维),不新增 `--as-of` 回溯、不生成新快照。
- **交易模拟**(盈亏比/最大回撤/Profit Factor,原任务第 10 点):独立于「快照对比」,留待 #128/#129。
- **历史多快照版本注册表**:#129 版本管理再做;本模块一次读一份快照。

## 3. 数据契约(只读,不 fetch)

- 日线缓存:`--data-dir` 指向 `_analysis/daily/*.pkl`(默认)。9 列 `[date, open, high, low, close, volume, amount, outstanding_share, turnover]`;`close/open/high/low` 前复权。`date` 为 `"YYYY-MM-DD"` 字符串。
- 板块映射:`--sector-map` 指向 `_analysis/code2sector.json`(默认),GBK 编码。comparator 只用它做 universe 过滤(与 predict/backtest 同源),不读板块语义。
- 快照 JSON:UTF-8,结构见 §7。`predictions[]` 每元素含 `code`(6 位代码字符串)、`date`(= as_of_date)、`composite`、`T+1 {direction, confidence, gap, od, path}`、`T+3 {direction, confidence}`;`1.1.0` 引擎另含 `expected_return`、`risk_p`(08-11 快照为 `1.0.0` 引擎,**无**这两个字段,故 return/risk 维如实不可验证)。
- **新鲜度现状**:本地缓存 `last_date` = 2026-08-14,已冻结快照 `as_of_date` = 2026-08-11。故 08-11 的 T+1(08-12)、T+2(08-13)、T+3(08-14)真实结果均已就绪,可验证。
- **08-11 快照实测分布(验收须以此为事实,不得按「全维度出数值」预期)**:

| 维 | 分布 | 对 comparator 的含义 |
|---|---|---|
| direction | 915 全 hold(up=0/down=0) | n_bet=0 → hit_rate=None,只报 base_rate/n_hold |
| gap | 915 全 low | n_bet=915 → hit_rate 真实(≈「低开」实际占比,常数分类器) |
| od | 915 全 hold | n_bet=0 → hit_rate=None |
| path | 915 全 None | n=0 → 不入 path 维,acc_path=None |
| T+3 | hold 831 + down 84 | n_bet=84 → hit_rate 真实但小样本 |

## 4. 架构(单模块 `compare.py`,纯函数 + CLI)

与 `analysis.py`/`backtest.py`/`predict.py`/`evaluate.py` 平级。导入:`import backtest as bt`(复用公开函数)、`import predict as pr`(仅取常量 `ADVERSE_THRESHOLD`、`N_BINS`)、`import numpy as np`。

**复用 backtest.py 的公开函数**(不 import 其下划线私有函数):

| 复用符号 | 用途 |
|---|---|
| `bt.load_sector_map(path)` | 读 GBK 板块映射 |
| `bt.build_universe(data_dir, sector_map)` | universe + codes(过滤 + tail 1200 + change_pct) |
| `bt.build_calendar(universe, codes)` | `all_days` + `pos_of` |

**复用 predict.py 的常量**(模块级,单一真相源,杜绝魔数漂移):

| 常量 | 值 | 用途 |
|---|---|---|
| `pr.ADVERSE_THRESHOLD` | `-0.03` | 不利事件阈值(risk 维) |
| `pr.N_BINS` | `10` | risk_p 的 ECE 等量分箱数 |

**本模块新增:**

| 函数 | 职责 |
|---|---|
| `_path_label(gap_up, od_up)` | gap×od → 四分类字符串(镜像 `predict._path_label`,6 行小函数,自实现不引私有) |
| `_actual_outcomes(d, bar)` | 读 >bar 的真实 close/open → `{close1, gap, od, trend3}`(缺 bar 对应项为 None) |
| `_class_metric(rows)` | 分类维命中率(rows = [(pred, label)],pred∈{up/down/hold} 或 {high/low/hold}) → `{n, base_rate, hit_rate, n_hold, n_bet}` |
| `_path_metric(rows)` | rows = [(pred_path, actual_path)] → `{n, acc_path}` |
| `_return_metric(rows)` | rows = [(expected_return, close1)] → `{n, mae, rmse, sign_agreement, sign_n, mean_residual}` |
| `_risk_metric(rows)` | rows = [(risk_p, adverse)] → `{n, adverse_rate, ece, brier, lift}` |
| `verify(snapshot_path, data_dir, sector_map_path)` | 主流程 → raw results dict(§7) |
| `_git_short_sha()` | 读 git HEAD 短哈希(镜像 predict/backtest 同款实现) |
| `_num(v)` | JSON-safe 数值收敛(NaN/inf/非数值 → None,镜像 predict) |
| `build_report(results)` / `render_markdown(payload)` | JSON-safe + MD + 版本戳 |
| `main(argv)` | argparse CLI |

## 5. 常量(逐字)

```
MODULE_VERSION = "1.0.0"
```

其余沿用 `pr.ADVERSE_THRESHOLD = -0.03`、`pr.N_BINS = 10`(自 predict 导入,不重定义)。

## 6. 核心算法

### 6.1 实际标签(只读 > as_of_date)

对快照里每只预测股 `(code, date)`:

1. `bar = pos_of.get(code, {}).get(date)`;若 `None`(code 已不在 universe,如退市/停牌/代码缺失)→ **不可验证**,计入 `unverified_reasons["not_in_universe"]`。
2. `d = universe[code]`;`close0 = float(d["close"].iloc[bar])`;`close0 <= 0` → 不可验证,`non_positive_close`。
3. 若 `bar + 1 >= len(d)` → **T+1 不可验证**,`no_next_bar`(同时 T+3 亦不可验证)。
4. `open1 = float(d["open"].iloc[bar+1])`、`close1 = float(d["close"].iloc[bar+1])`;任一 `<= 0` → 不可验证,`non_positive_close`。
5. 实际标签:
   - `close1_actual = close1 / close0 - 1`;`close1_up = 1 if close1_actual > 0 else 0`
   - `gap_actual = open1 / close0 - 1`;`gap_up = 1 if gap_actual > 0 else 0`
   - `od_actual = close1 / open1 - 1`;`od_up = 1 if od_actual > 0 else 0`
   - `trend3`:**仅当 `bar + 3 < len(d)`** 且 `close3 = float(d["close"].iloc[bar+3]) > 0` 时,`trend3_actual = close3 / close0 - 1`,`trend3_up = 1 if > 0 else 0`;否则 `trend3_up = None`(该股 T+3 不可验证,不入 trend3 指标)。
   - `path_actual = _path_label(gap_up, od_up)`(gap_up/od_up 定义同 predict:`高开高走 / 高开低走 / 低开高走 / 低开低走`)。

### 6.2 六维比对(已存预测 vs 实际)

| 维度 | 已存预测字段 | 比对 |
|---|---|---|
| direction | `T+1.direction` ∈ {up, down, hold} | hold → n_hold;up/down 命中 = (up & close1_up==1) 或 (down & close1_up==0) |
| gap | `T+1.gap` ∈ {high, low, hold} | high→预测高开(>0)、low→预测低开(<0);hold → n_hold;命中 = (high & gap_up==1) 或 (low & gap_up==0) |
| od | `T+1.od` ∈ {up, down, hold} | 同 direction,用 od_up |
| trend3 | `T+3.direction` ∈ {up, down, hold} | 同 direction,用 trend3_up;trend3_up is None → 该股不入 trend3 维 |
| path | `T+1.path` ∈ 四分类 或 None | None(快照即「观望不下注」)→ 不入 path 维;否则命中 = pred_path == path_actual |
| return | `expected_return`(字段存在且非 None) | 与 `close1_actual` 比 MAE/RMSE/符号一致率/平均残差(§6.3) |
| risk | `risk_p`(字段存在且非 None) | 与 `1[close1_actual < -0.03]` 比 ECE/Brier/lift(§6.3) |

字段缺失处理:return 维 `available = any(预测含非 None 的 expected_return)`;若为 False(08-11 快照全无该键,即是),指标置 `{"available": false, "reason": "快照无 expected_return 字段(1.0.0 引擎生成)"}`;risk 同理。若 `available=True` 但某股该字段为 None,该股不入该维(与 predict 的 None 语义一致);若全为 None 则 n=0。

### 6.3 指标公式(镜像 predict,输入为已存预测)

**分类维(direction/gap/od/trend3)** `_class_metric(rows)`,`rows = [(pred_str, label_int)]`:

```
n       = len(rows)
n_hold  = count(pred == "hold")
n_bet   = n - n_hold
hit     = count((pred=="up" and label==1) or (pred=="down" and label==0))
hit_rate= hit / n_bet if n_bet else None
base_rate = mean(label) over n
```

(gap 维先把 `high→up`、`low→down` 映射后再走同一函数;label_int 即 `gap_up`/`od_up`/`close1_up`/`trend3_up`。)

> 有意简化:分类维只报 hit_rate/base_rate/n_hold,**不报 ECE/Brier**。快照存了 `confidence`(= max(P,1-P)),理论上可对 direction/trend3 重建 P(up)(up→conf、down→1-conf、hold→歧义)算校准 ECE/Brier,但:① hold 档 P(up) 无法由 confidence 唯一定;② 单份前向快照非 hold 样本极少(08-11 direction n_bet=0、trend3 n_bet=84);③ 校准 ECE/Brier 已由评分系统在历史 valid 窗口量过。故本里程碑只验「方向命中」,confidence 校准留后续。

**路径** `_path_metric(rows)`,`rows = [(pred_path, actual_path)]`(pred_path 为 None 者已在采集时剔除):

```
n = len(rows); acc_path = count(pred==actual) / n if n else None
```

**涨跌幅** `_return_metric(rows)`,`rows = [(er, close1)]`:

```
abs_err    = |er - close1|
sq_err     = (er - close1)^2
residual   = close1 - er            # 正 = 系统性低估(与 predict.mean_residual 同向)
sign_den   = count(close1 != 0); sign_num = count((er>0 and close1>0) or (er<0 and close1<0))
mae        = mean(abs_err); rmse = sqrt(mean(sq_err))
mean_residual = mean(residual); sign_agreement = sign_num/sign_den if sign_den else None
```

**风险** `_risk_metric(rows)`,`rows = [(risk_p, adverse)]`,`adverse = 1[close1 < -0.03]`:

```
n            = len(rows); adverse_rate = mean(adverse)
brier        = mean((risk_p - adverse)^2)
ece          = 按 risk_p 升序等量分 N_BINS 箱(等样本数;n < N_BINS 降单箱),
               每箱 mean_p = mean(risk_p)、realized = mean(adverse),
               ece = Σ_bin (n_bin/n) * |mean_p - realized|(空箱跳过)
lift         = realized[末箱] - realized[首箱](非空箱 >= 2 才报,否则 None)
```

> 注:与评分系统 `_metric_risk` 的差别——那套按**原始 risk 分**经校准器分箱、比的是箱级 PAV 概率;本套按**已存 risk_p**直接分箱、比的是箱内均值。两套分箱键不同、数值一般不等,仅在 PAV 保证 risk_p 随 risk 单调时**方向一致**;本套是「预测概率的校准度」直接口径,更贴合前向验证。已在 §7 诚实声明标注。

> 指标公式确有两份(predict 的 calibrator-coupled 版 + compare 的 row-based 版;§4 决策 #2 已核签名:`_metric_return(cal, direction_cal, samples)`/`_metric_risk(cal, samples)`/`_metric_path(gap_cal, od_cal, samples)` 全依赖校准器对象,comparator 只有已存预测值、复用不了,故另起必要)。每个 metric 函数 docstring 须交叉引用 predict 对应函数:`_class_metric`↔`_metric`、`_path_metric`↔`_metric_path`、`_return_metric`↔`_metric_return`、`_risk_metric`↔`_metric_risk`,防未来单边改动漂移;§10.2 逐值断言兜底。

## 7. 报告 schema

`verify()` 返回 raw dict;`build_report()` 序列化为 JSON-safe(所有 None → null,NaN → None)。

```json
{
  "system_version": "<comparator git sha>",
  "module_version": "1.0.0",
  "generated_at": "<ISO 8601>",
  "mode": "compare",
  "snapshot": {
    "path": "prediction_snapshot.json",
    "system_version": "<快照内引擎 sha,权威>",
    "module_version": "<快照内引擎 semver>",
    "generated_at": "<快照内生成时间>",
    "as_of_date": "2026-08-11"
  },
  "data_range": {"start": "<universe 首日>", "end": "<universe 末日>"},
  "verification": {
    "n_predictions": 915,
    "n_verified": <T+1 可验证数>,
    "n_trend3_verified": <T+3 可验证数>,
    "n_unverified": <不可验证总数>,
    "unverified_reasons": {"not_in_universe": N, "no_next_bar": N, "non_positive_close": N}
  },
  "metrics": {
    "direction": {"n": .., "base_rate": .., "hit_rate": .., "n_hold": .., "n_bet": ..},
    "gap":        {...同 direction},
    "od":         {...同 direction},
    "trend3":     {...同 direction},
    "path":       {"n": .., "acc_path": ..},
    "return":     {"available": false, "reason": "快照无 expected_return 字段(1.0.0 引擎生成)"},
    "risk":       {"available": false, "reason": "快照无 risk_p 字段(1.0.0 引擎生成)"}
  }
}
```

当 `return`/`risk` 可用时,其 metric dict 为 `{"available": true, "n": .., "mae": .., ...}` 等(§6.3 各字段)。

`render_markdown(payload)` 输出:头部版本戳 + 快照版本戳 + 数据区间 + 验证计数表 + 六维指标表(每维主指标一行;`available:false` 维显示「不可验证 + reason」)+ 诚实声明(§7.1)。

### 7.1 诚实声明(逐字进 MD)

1. 验证读两类行情:≤as_of 的**锚点 close**(`close[bar]`,作 T+1 收益分母,属合法 ≤T 读取)+ >as_of 的 **open1/close1/close3**(作实际结果)。预测字段是快照里冻结的,**comparator 不重跑评分/校准**。
2. 快照不可变:`snapshot.system_version`(引擎代码)、`system_version`(comparator 代码)、`data_range`(验证数据)三者独立,互不冒充。
3. 样本按「股票×时间」聚集、非 i.i.d.,n 为样本数;单份前向快照 n 较小(≈915),准确率波动大,只作**单次前向验证**、不与历史 valid 窗口直接比显著。
4. return/risk 维若快照无对应字段,如实标「不可验证」,不回退、不编造。
5. risk 维 ECE 按**已存 risk_p**直接分箱(非原始 risk 分经校准器),见 §6.3 注。

## 8. CLI

```
python compare.py [--snapshot prediction_snapshot.json]
                  [--data-dir _analysis/daily]
                  [--sector-map _analysis/code2sector.json]
                  [--out snapshot_verification.json]
```

- `--out` 缺省 `snapshot_verification.json`;同基名 `.md` 一并写出。
- 退出码 0 成功 / 1 运行错(快照缺失、data-dir 无 pkl、sector-map 缺失)。

## 9. 错误处理

- `--snapshot` 不存在或 JSON 解析失败 → 明确报错退出 1。
- 快照 `mode != "predict"` → 报错退出 1(comparator 只对前向快照)。
- `--data-dir` 无可用 pkl / universe 为空 → 报错退出 1。
- 单只预测股 `code` 不在 universe → 计入 `unverified_reasons.not_in_universe`,不崩溃。
- 单只预测股 `date` 不等于快照 `as_of_date` → 以其自身 `date` 为锚点定位 bar(更稳健);仍定位不到则计 not_in_universe。
- 价格非正(`close0/open1/close1 <= 0`)→ 该股不可验证,`non_positive_close`,不崩溃。

## 10. 测试(`tests/test_compare.py`,TDD)

遵循仓库约定(conftest 加 sys.path、`import compare`、`pytest.approx`、`monkeypatch.setattr`、文件内合成数据辅助)。

### 10.1 `_path_label` 与 `_actual_outcomes`

- `_path_label` 四象限逐格断言(含 gap/od 边界)。
- `_actual_outcomes`:构造含 >bar 的日线,断言 close1/gap/od/trend3 精确值;bar+1 越界 → 全 None;bar+3 越界 → trend3 None 其余非 None;非正价 → 对应 None。

### 10.2 分类/路径/涨跌幅/风险指标

- `_class_metric`:构造 (up,1)/(down,0)/(up,0)/(hold,1) 等,断言 n/n_hold/n_bet/hit_rate/base_rate 精确;全 hold → hit_rate None。
- `_path_metric`、`_return_metric`(MAE/RMSE/符号一致率/mean_residual,零收益剔除)、`_risk_metric`(adverse_rate/ECE/Brier/lift,含 < N_BINS 降单箱)。

### 10.3 `verify` 主流程 + 字段缺失

- 合成快照(as_of 某日,3 股:一正常、一 code 不在 universe、一 bar+1 越界)+ 合成 universe → 断言 metrics 值、`n_verified`、`unverified_reasons` 各计数。
- 无 `expected_return`/`risk_p` 字段的快照 → return/risk 维 `available:false` + reason;有字段但某股 None → 该股不入该维。

### 10.4 反泄露测试(核心)

- 改快照里某股 `T+1.direction` → `verify` 输出方向维 hit_rate 变化(证明读了已存预测)。
- 改 universe **≤ as_of_date** 的 close(如 `close[bar-1]`)→ 输出**逐位不变**(证明验证不读 bar 之前)。
- 改 universe **锚点 close**(`close[bar]`,= as_of_date 当日)→ 输出变化(证明 T+1 分母确实从行情读、属 ≤T 合法读取)。
- 改 universe **> as_of_date** 的 open1/close1/close3 → 输出变化(证明验证确实读了未来真实结果)。

### 10.5 CLI 与报告

- `main([...])` 写 JSON + MD,断言 `mode=="compare"`、`snapshot.system_version`、`metrics` 键齐全、return/risk 的 `available:false` 渲染为「不可验证」。
- 快照缺失 → 退出码 1。

运行:`python -m pytest tests/test_compare.py -q`;全量回归 `python -m pytest tests/ -q` 须保持既有通过数(仅新增 test_compare)。

## 11. 验收标准

1. 全量测试通过(既有 + 新增 test_compare)。
2. 真实缓存跑通:`python compare.py --snapshot prediction_snapshot.json --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out _analysis/snapshot_verification.json`,产出含六维指标 + 验证计数 + 诚实声明的 MD。**按 §3 实测分布验收,不得凑数**:gap、trend3 出真实 hit_rate(trend3 n_bet=84 小样本);direction、od 全 hold → hit_rate=None、只报 base_rate/n_hold;path 全 None → n=0 不入维(acc_path=None);return/risk 维如实「不可验证」。以上均为诚实基线,非失败。
3. 无未来数据泄露(§10.4 测试通过)。
4. 不要求有「信号」:方向维 hit_rate 可能为 None(全 hold)、gap 维 hit_rate ≈ base_rate(常数「低开」分类器)、n_hold 高、return/risk 不可验证——均属**诚实基线**(composite 方向优势弱,已有结论),验收只看时间隔离 + 指标如实产出 + 不可验证原因如实计数,不把「无信号」当 bug。
