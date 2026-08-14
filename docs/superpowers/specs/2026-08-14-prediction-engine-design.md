# 预测引擎(多维结构化预测)设计

- 日期:2026-08-14
- 状态:待用户评审
- 上游任务:审计/回测/优化大任务第 3/4/5 点(S1 预测引擎子项目)
- 前置:历史盲测基线(backtest.py)已冻结、缺陷修复(切片 A/B)已完成

## 1. 目标

新增生产级模块 `predict.py`,在现有评分之上叠一层**校准概率层**,把「composite 分 → 次日方向/开盘/盘中路径/T+3 趋势」的结构化预测(JSON `direction/confidence/path/horizon`)固化入库、可测、可复现、带版本戳。预测引擎同时支持:

- **回测模式**:历史逐日跑,在 out-of-sample(valid)窗口量出各 horizon 的准确率与校准误差。
- **前向模式**:对「最新一个交易日」产出带 `system_version` 的预测快照,供后续(S2/S5)用真实结果验证。

严格禁止未来数据泄露:预测输入只用 ≤ T 的数据,验证只读 > T 的真实数据。

## 2. 范围

**包含:**

- 校准器 `Calibrator`(composite 分箱 → 经验概率,PAV 单调池化)+ 最小 temporal train/valid 切分。
- 每股逐只预测(非仅 15 只篮子):回测模式覆盖**全 buyable 宇宙**,前向模式覆盖 `as_of_date` 当日**可交易宇宙**(§8.2,不要求 T+1 存在)。
- 四个 horizon 的校准:次日方向(close1)、开盘 gap、盘中 od、T+3 趋势(close3)。
- 结构化预测 JSON(§7)+ 版本戳(§9)。
- 反泄露测试(§12.3)。

**不包含(后续子项目再做):**

- 六维完整评分系统 + 交易模拟(盈亏比/最大回撤/Profit Factor)——S2。
- 市场环境分类(牛/熊/震荡/恐慌/高潮/退潮/恢复)——S3。
- walk-forward / 多折交叉反过拟合、V1.0/V1.1 版本注册表——S4。
- 自动复盘优化闭环——S5。
- 最终六层自审 + 完整报告——S6。
- **分钟级盘中路径**:日线 pkl 无分钟数据,「path」只能做日线 OHLC 的 gap×od 四分类(§6.4),真实分钟路径明确不做。

## 3. 数据契约(读现有缓存,不 fetch)

同 backtest.py 规格,复述关键点:

- 日线缓存:`--data-dir` 指向 `_analysis/daily/*.pkl`(默认)。文件名 = 6 位纯数字代码。列精确为 `[date, open, high, low, close, volume]`,`date` 为 `"YYYY-MM-DD"` 字符串,RangeIndex,**无 amount/成交额列,无分钟列**。前复权。原始全史 1990-12-19 → 2026-08-11,截尾 1200 根后评估窗口 ≈ 2021-08 → 2026-08。
- 板块映射:`--sector-map` 指向 `_analysis/code2sector.json`(默认),**GBK 编码**,`{6位代码: [THS 板块名列表]}`。须 `encoding="gbk"` 显式打开。

输入位于 gitignored `_analysis/`,不提交;输出报告提交(§9)。

## 4. 架构(单模块 `predict.py`,纯函数 + CLI)

与 `analysis.py`/`backtest.py` 平级,平铺纯函数。导入方式:`import analysis as an`、`import backtest as bt`(复用其公开纯函数),`import pandas as pd`。

**复用 backtest.py 的公开函数**(不修改 backtest.py,不 import 其内部下划线私有函数):

| 复用符号 | 用途 |
|---|---|
| `bt.load_sector_map(path)` | 读 GBK 板块映射 |
| `bt.build_universe(data_dir, sector_map)` | 过滤 `code in sector_map` 且 `len>=1200`,tail(1200) + `change_pct` |
| `bt.build_calendar(universe, codes)` | `all_days` + `pos_of` |
| `bt.build_buyable(universe, pos_of, all_days, i)` | 返回 4 元组 `(buy, od_m, gap_m, c1_m)`;本模块只用 `buy` 集做宇宙过滤 |
| `bt.score_at(d, i, now)` | `df=d.iloc[:i+1]`(≤T)重建 quote 调 `an.score_stock`,返回 sc(含 `composite`)或 None |
| `bt.next_returns(d, i)` | T+1 的 `{gap, close1, od}`(供 `_labels` 与 §12.3 反泄露测试) |

**本模块新增:**

| 函数 | 职责 | 依赖 |
|---|---|---|
| `fwd_close(d, i, k)` | `close[i+k]/close[i]-1`,越界/非正价 → None | 无 |
| `_labels(d, i)` | 四 horizon 二分类标签 `{close1, gap, od, trend3}`(0/1/None,§6.1) | bt.next_returns、fwd_close |
| `Calibrator` | composite → 单调经验概率(§6) | 无 |
| `_fit_calibrator(pairs, n_bins)` | 分箱 + PAV 单调池化(§6) | 无 |
| `forward_universe(universe, pos_of, all_days)` | 前向宇宙 `{code: bar}`(as_of_date 当日可交易股,§8.2) | an.limit_threshold、bt.MIN_AMOUNT |
| `predict_at(d, i, now, cals)` | 单股 ≤T → 结构化预测(§7) | bt.score_at |
| `run_backtest(data_dir, sector_map_path)` | 回测模式:逐日预测 + valid 窗口评估(§8) | 复用 bt.* |
| `predict_now(data_dir, sector_map_path)` | 前向模式:最新日 → 快照(§8) | 复用 bt.* |
| `build_report(...)` / `render_markdown(...)` | 拼 JSON + MD + 版本戳(§9) | 无 |
| `main(argv)` | argparse CLI(§10) | 全部 |

`now` 用常量 `AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)`(与 backtest.py 一致),保证 `is_after_close`/`trading_minutes_elapsed` 确定、不随真实日期漂移。

## 5. 常量(逐字)

```
EVAL_DAYS      = 1200    # 每只截尾 1200 根(与 backtest.py 一致)
TRAIN_FRAC     = 0.8     # temporal 切分:前 80% 评估日拟合,后 20% 评估
N_BINS         = 10      # 校准分箱数(等量十分位)
DIRECTION_BAND = 0.05    # |P-0.5| <= band → 观望
AFTER_CLOSE    = datetime(2026, 1, 1, 15, 1)
```

buyable 过滤复用 `bt.build_buyable`(涨停 `chg >= an.limit_threshold(c)` 或跌超 7% `chg <= -7.0`、`volume*close < 1e8` 剔除),不另设常量。前向宇宙 `forward_universe` 内联同一过滤,但引用公开常量 `bt.MIN_AMOUNT`(=1e8,backtest.py:28)与 `an.limit_threshold`;`-7.0` 沿用 backtest.py:155 的字面语义(无命名常量),杜绝双份魔数漂移。

评估日采样**公式**与 backtest.py 一致;但 predict 不建模板块热、不跳「无热板块」日(backtest.py:346-347 `if not heat: continue`),实际评估日集为完整 range,`n_eval` 与基线可能有微小差异(报告 §9 注明,不做「同日可比」误导):

```
all_days  = 排序后的全市场交易日并集
start     = max(61, len(all_days) - 1 - EVAL_DAYS)
step      = max(1, (len(all_days) - 2 - start) // 300)
eval_days = range(start, len(all_days) - 1, step)
```

## 6. 校准机制(核心)

### 6.1 标签(四个二分类校准器)

「buyable」= `bt.build_buyable(universe, pos_of, all_days, i)` 返回的 `buy` 集(过滤:涨停 `chg >= an.limit_threshold(c)` 或跌超 7% `chg <= -7.0`、`volume*close < 1e8` 剔除;含 T+1 bar 守卫,故仅回测模式使用)。

`_labels(d, i)` 返回 `{"close1": 0/1, "gap": 0/1, "od": 0/1, "trend3": 0/1/None}`:前三项来自 `bt.next_returns(d, i)`(其返回 None 则整只跳过),`trend3` 来自 `fwd_close(d, i, 3)`(越界仅该项为 None)。

对每个评估日 i 的每只 buyable 股 c(`bar = pos_of[c][all_days[i]]`),`sc = bt.score_at(universe[c], bar, AFTER_CLOSE)`;`sc is None` 则跳过。取 `composite = sc["composite"]`,`lbl = _labels(universe[c], bar)`:

| 校准器 | 二分类标签(1/0) | 来源 |
|---|---|---|
| `cal_direction` | `lbl["close1"]` | close[T+1] > close[T](次日涨) |
| `cal_gap` | `lbl["gap"]` | open[T+1] > close[T](高开) |
| `cal_od` | `lbl["od"]` | close[T+1] > open[T+1](红盘) |
| `cal_trend3` | `lbl["trend3"]`(None 则跳过该样本) | close[T+3] > close[T](三日涨) |

### 6.2 拟合(`_fit_calibrator`)

对每个校准器,输入 `(composite, label)` 样本对(仅 train 段评估日):

1. 按 composite **升序**排序,切成 **N_BINS=10 个等量分箱**(等样本数,非等宽;composite 分布偏态,等量更稳健)。
2. 每箱 `p = mean(label)`。
3. **PAV 单调池化**(pool adjacent violators):从左到右扫,若相邻箱 `p[i] > p[i+1]`,合并重算均值,直至 p 单调不减。保证 `p_up(composite)` 单调。
4. 存为查找表 `[(bin_upper_composite, p), ...]`(含首箱下界 −inf、末箱上界 +inf)。

### 6.3 查询

`cal.p_up(composite)`:按 composite 落在哪个箱返回该箱 p。composite 为 None/NaN → 返回 None(不预测,诚实)。落在边界用 `<= bin_upper`。

### 6.4 方向/置信度/路径(由校准概率导出)

对每个二分类校准器,`P = p_up(composite)`:

```
direction  = "涨" if P > 0.5 + DIRECTION_BAND else
             "跌" if P < 0.5 - DIRECTION_BAND else "观望"
confidence = max(P, 1 - P)      # 预测方向成立的概率,恒 >= 0.5
```

- **次日方向** = `cal_direction` 的 direction/confidence。
- **开盘 gap** = `cal_gap` 的 direction("高开"/"低开"/"观望")。
- **盘中 od** = `cal_od` 的 direction("红"/"绿"/"观望")。
- **盘中路径 path** = gap × od 联合四分类:`高开高走 / 高开低走 / 低开高走 / 低开低走`;若 gap 或 od 任一为"观望",path 为 `None`(诚实不下注)。path 置信度 = `min(conf_gap, conf_od)`。
- **T+3 趋势** = `cal_trend3` 的 direction/confidence。

`DIRECTION_BAND=0.05`:近 0.5 处校准噪声主导,回避(观望)比强猜诚实。该值与 N_BINS 均可在 S4/S5 用回测数据重调,本模块仅定默认。

### 6.5 temporal train/valid 切分(最小反过拟合)

`n = len(eval_days)`;`n_train = int(TRAIN_FRAC * n)`。**train 段 = 前 n_train 个评估日**(时间上更早),**valid 段 = 后 n − n_train 个**。校准器只 fit train 段;准确率/校准误差只在 valid 段报告(out-of-sample)。完整 walk-forward/多折留给 S4。

## 7. 预测 JSON schema

`predict_at` 对单股产出:

```json
{
  "code": "600000",
  "date": "2026-08-13",
  "composite": 61.4,
  "T+1": {
    "direction": "up", "confidence": 0.63,
    "gap": "low", "od": "up", "path": "低开高走"
  },
  "T+3": {"direction": "up", "confidence": 0.57}
}
```

- 字段值约定:`direction ∈ {"up","down","hold"}`(涨/跌/观望)、`gap ∈ {"high","low","hold"}`、`od ∈ {"up","down","hold"}`、`path ∈ {"高开高走","高开低走","低开高走","低开低走"}`(gap 或 od 为 hold 时 path 为 null)。
- path 无独立置信度字段(可推导为 `min(conf_gap, conf_od)`,§6.4),不落 JSON。
- `T+3` 与 `T+1` 同源:由 `composite` + `cal_trend3` 校准器产出,`composite` 可得即预测;**不读 `fwd_close`、不受「剩余 bar 不足 3」影响**(前向模式锚定最后一根 bar 时 T+3 预测仍产出,供 S5 后续验证)。`fwd_close` 的 None 判断只在回测模式的标签/验证步(§6.1)使用,不进 `predict_at`。
- `predict_at` 内部 `composite` 为 None(评分不可得)时返回 None(该股不产出预测)。
- `cals` 为四校准器容器(dict,键 `direction`/`gap`/`od`/`trend3`,各为 `Calibrator`,§6)。

**前向模式**输出的快照为上述单股预测的列表 + 版本戳(§9)。

## 8. 模式与评估

### 8.1 回测模式 `run_backtest`

1. 复用 `build_universe`/`build_calendar`;构建 eval_days。
2. 遍历 train 段评估日,收集每个校准器的 `(composite, label)` 样本(§6.1);`_fit_calibrator` 拟合四个校准器。
3. 遍历 valid 段评估日,对每只 buyable 股 `predict_at`,与真实标签比对。
4. 评估指标(valid 段,out-of-sample,每个校准器):
   - `n`、`base_rate`(valid 内 label=1 占比)、`hit_rate`(二分类预测方向正确占比,观望计为「未下注」不计入 hit_rate 但计入 `n_hold`)。
   - **`ece`**(expected calibration error)= 按箱 `mean(|预测 P(箱) − 箱内真实 label 频率|)`,加权箱样本数。
   - **`brier`** = `mean((P − label)^2)`(对非观望样本)。
   - `trend3` 的 `n` 略小于 `direction`/`gap`/`od`(末尾若干评估日无 T+3 真实值,数量由数据/step 决定,非固定;`fwd_close` None 样本不参与 trend3 指标)。
   - path:四分类准确率(`acc_path`)。
5. 输出 `results` dict(供 build_report 消费)。

### 8.2 前向模式 `predict_now`

1. 同 `run_backtest` 前半段:用**全量** eval_days 拟合校准器(生产部署时用全部历史,不用切分)。
2. `as_of_date = all_days[-1]`;前向宇宙 = `forward_universe(universe, pos_of, all_days)` 返回 `{code: bar}`:`pos_of[c].get(as_of_date)` 非 None(停牌/未上市跳过),复用 chg/amount 过滤(`chg >= an.limit_threshold(c) or chg <= -7.0`、`volume*close < 1e8` 剔除),**去掉 build_buyable 的 T+1 守卫**(backtest.py:146,前向锚定最后一根时恒空)。**不复用 build_buyable。**
3. 对前向宇宙每只股,`predict_at(universe[c], bar, AFTER_CLOSE, cals)` 产出预测,`date = as_of_date`。
4. 输出快照 JSON + 版本戳。**只读 ≤ as_of_date 的数据,不读未来。**

## 9. 报告输出与版本戳

回测模式输出 `--out`(默认 `prediction_baseline.json`)与同基名 `.md`:

```json
{
  "system_version": "<git rev-parse --short HEAD>",
  "module_version": "1.0.0",
  "generated_at": "<ISO 8601>",
  "mode": "backtest",
  "data_range": {"start": "...", "end": "..."},
  "train_window": {"start": "...", "end": "..."},
  "valid_window": {"start": "...", "end": "..."},
  "n_eval": <int>, "step": <int>, "n_train": <int>, "n_valid": <int>,
  "calibrators": {
    "<name>": {"bins": [{"upper": .., "p": ..}], "n": <int>, "degraded": <bool>}
  },
  "metrics": {
    "direction": {"n": .., "base_rate": .., "hit_rate": .., "ece": .., "brier": .., "n_hold": ..},
    "gap":        {...}, "od": {...}, "trend3": {...},
    "path":       {"n": .., "acc_path": ..}
  }
}
```

前向模式输出 `--out`(默认 `prediction_snapshot.json`):

```json
{
  "system_version": "<git rev-parse --short HEAD>",
  "module_version": "1.0.0",
  "generated_at": "<ISO 8601>",
  "mode": "predict",
  "as_of_date": "<最新交易日>",
  "predictions": [ <§7 单股预测>, ... ]
}
```

`system_version` = git 短哈希(权威代码标识);`module_version` = 预测模块 semver。Markdown 报告含版本戳、train/valid 窗口、校准表(各箱上界 + p)、指标表,并**如实声明**:① 校准概率基于「代理管线」评分(无 amount,生产板块加成不可复现);② 回测模式指标仅在 valid 窗口有效(out-of-sample);③ 分钟级路径未做(数据缺口);④ 样本按「股票 × 时间」聚集、**非 i.i.d.**,`n` 为样本数而非独立观测数,ece/brier 不可按 n 直接推置信区间;⑤ `n_eval` 为完整评估日 range,与基线 backtest(跳无热板块日)的 `n_eval` 可能不同,非同日口径。

## 10. CLI

```
python predict.py [--data-dir _analysis/daily] [--sector-map _analysis/code2sector.json]
                  [--predict] [--out prediction_baseline.json]
```

- 缺省 `--predict` = 回测模式;`--predict` = 前向模式。
- `--out` 缺省回测 `prediction_baseline.json`、前向 `prediction_snapshot.json`;markdown 同基名 `.md` 一并写出(前向模式不写 md)。
- 退出码 0 成功 / 2 参数错 / 1 运行错。

## 11. 错误处理

- `--data-dir` 不存在 / 无 pkl → 明确报错退出。
- `--sector-map` 缺失或非 GBK 可解码 → 明确报错。
- 单只 pkl 缺列 → 跳过该股并计入诊断计数(非崩溃)。
- `composite` 为 None → 该股不产出预测(返回 None,非错误非泄露)。`fwd_close` 越界只影响回测模式 trend3 的标签/验证样本(跳过),不影响 `predict_at` 产出。
- train 段样本不足以分 N_BINS 箱(如某校准器 n < N_BINS)→ 该校准器降为全样本单箱(常量 p),并在报告标注 `degraded: true`。

## 12. 测试(`tests/test_predict.py`,TDD)

遵循仓库约定(conftest.py 加 sys.path、`import predict`、`pytest.approx`、`monkeypatch.setattr`、文件内 `make_daily` 辅助)。

### 12.1 目标公式

合成 `make_daily` → 断言 `fwd_close(d, i, k)` 精确值;越界/非正价 → None。

### 12.2 校准器

- `_fit_calibrator` 对构造的 `(composite, label)` 样本对:① 箱概率正确;② PAV 后 p 单调不减(构造一个先升后降的样本,断言输出单调)。
- `p_up` 边界/None 处理。

### 12.3 反泄露测试(核心)

夹具 ≥ 61 根日线。`predict_at(d, i, AFTER_CLOSE, cals)` 得预测 P1;把 `d.iloc[i+1..i+3]` 的 open/close 改成极端正值,再得 P2;**断言 P1 == P2 逐位相等**(T+1..T+3 不进入预测);另断言 `fwd_close`/`next_returns` 随之改变(证明验证确实读了未来)。

### 12.4 预测 schema 与方向

- monkeypatch `bt.score_at` 返回固定 composite,断言 `predict_at` 的 direction/confidence/gap/od/path/T+3 逐字段正确(含观望带、path 四分类);并断言 **T+3 只由 composite+calibrator 决定、不读 fwd_close**(取 `i` 使 `i+3 >= len(d)`,T+3 仍产出非 null 预测)。
- `composite` None → `predict_at` 返回 None。
- `forward_universe`:合成 universe,断言停牌(无 as_of_date bar)/涨停/跌超 7%/低换手被剔除、正常股保留,且**不要求 T+1 bar 存在**。

### 12.5 CLI 与报告

- `--data-dir` 指向 tmp_path 自建 pkl 集,跑回测模式 `main`,断言 JSON/MD 写出、`system_version`/`calibrators`/`metrics` 字段齐全、GBK 板块映射正确解码。
- 前向模式:断言快照含 `system_version` + `predictions`,且 `mode == "predict"`。
- 缺 `--data-dir` → 退出码非 0。

运行:`python -m pytest tests/test_predict.py -q`;全量回归 `python -m pytest tests/ -q` 须保持 176 + 新增通过。

## 13. 验收标准

1. `python -m pytest tests/ -q` 全绿(176 + 新增)。
2. `python predict.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out prediction_baseline.json` 在真实缓存上成功产出:含 `system_version`、train/valid 窗口、四校准器箱表、valid 段 `metrics`(ece/brier/hit_rate),且报告如实标注「代理管线」「out-of-sample」「分钟路径未做」。
3. `python predict.py --predict ... --out prediction_snapshot.json` 产出带 `system_version` 的前向快照,`predictions` 非空、字段合法、`as_of_date` = 最新交易日。
4. 校准单调:每个校准器 `bins` 的 p 单调不减(§12.2 测试)。
5. 无未来数据泄露(§12.3 测试通过)。
6. 不要求校准有「信号」:`|p-0.5|` 无展布、观望率高、`hit_rate≈base_rate`、ece 小均属**诚实基线**(composite 对次日方向的边际信号弱,切片 A 已示 ≈ 噪声级),勿当 bug 排查;验收只看校准单调 + 反泄露 + 指标如实产出。
