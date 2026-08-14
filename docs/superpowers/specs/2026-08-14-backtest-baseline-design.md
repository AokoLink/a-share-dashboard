# 历史盲测基线模块设计

- 日期:2026-08-14
- 状态:待用户评审
- 上游任务:审计/回测/优化大任务第一步「先固化历史盲测基线」

## 1. 目标

把现有 gitignored 探针 `_analysis/nxday_backtest.py` 的**核心次日盲测逻辑**升级为一个**提交入库、可测、可复现、带版本戳**的生产级模块 `backtest.py`,量出当前版本(HEAD 代码)的次日准确率基线,供后续迭代(V1 vs V2)做对照。

严格禁止未来数据泄露:预测输入只用 ≤ T 的数据,验证只读 > T 的真实数据。

## 2. 范围

**包含(核心次日盲测主路径):**

- 篮子 A / B / C / D / E(定义见 §6.6)
- 目标 gap / od / close1(公式见 §6.4)
- 度量 mean_pct / win_rate / n + 按年切分(§6.7)
- 生产级增强:**Welch t 检验**(各篮子对基准 D 的显著性)——探针核心路径不含此检验(仅存在于 probe 门控块),本模块新增并标注为增强
- `system_version` / `git_sha` / `generated_at` / `data_range` 快照戳(§7)
- 反泄露测试(§8)

**不包含(后续切片再做):**

- 探针子分析 P0b / P1 / P3 / P4 及 `spike_v3b.py` / `calibration.py` 等(留 `_analysis/` 作参考,不固化)
- T+3 趋势 / 盘中路径 / 结构化多周期预测(大任务第 3 点)
- 交易模拟 / 盈亏比 / 最大回撤 / Profit Factor(大任务第 10 点)
- 市场环境分类(大任务第 7 点)

## 3. 数据契约(读现有缓存,不 fetch)

模块**只读**本地缓存,不发起网络请求。

- 日线缓存:`--data-dir` 指向 `_analysis/daily/*.pkl`(默认 `_analysis/daily`)。每文件一只股票,文件名 = 6 位纯数字代码(无 sh/sz/bj 前缀)。列精确为 `[date, open, high, low, close, volume]`,`date` 为 `"YYYY-MM-DD"` 字符串,RangeIndex,**无 `amount`/成交额列**。前复权(qfq)。1623 只,原始全史区间 1990-12-19 → 2026-08-11(截尾 1200 根后评估窗口 ≈ 2021-08 → 2026-08,见 §7)。
- 板块映射:`--sector-map` 指向 `_analysis/code2sector.json`(默认)。**该文件为 GBK 编码**(非 UTF-8),格式为扁平 dict:`{6位代码: [THS 板块名列表]}`,1799 键,值一码多板块。模块须以 `encoding="gbk"` 显式打开(探针靠 Windows 默认 GBK 隐式解码,属脆弱写法,生产模块必须显式)。

输入均位于 gitignored `_analysis/`,不提交;输出报告提交(§7)。

## 4. 架构(单模块 `backtest.py`,纯函数 + CLI)

与 `analysis.py` / `recommend.py` 平级,平铺纯函数(不引入包),依赖注入(路径 / `now` 传参)保证可测性。导入方式:`import analysis as an`、`import data_source as ds`(与探针、测试一致)。

职责分层(计划阶段再定精确签名,此处定职责与边界):

| 函数 | 职责 | 依赖 |
|---|---|---|
| `load_daily(data_dir, code)` | 读单只 pkl → DataFrame | 无 |
| `load_sector_map(path)` | 读 GBK code2sector.json → `{code: [板块名]}` | 无 |
| `build_universe(data_dir, sector_map)` | 过滤 `code in sector_map` 且 `len>=1200`,每只 `tail(1200)` 后 `reset_index(drop=True)` 并加 `change_pct = close.pct_change()*100`(首行 0) | load_daily |
| `build_calendar(universe)` | `all_days = sorted(union(每只 date))`;`pos_of = {code: {dt: bar_idx}}` | build_universe |
| `next_returns(d, i)` | 算 gap/close1/od,越界或非正价返回 None | 无 |
| `sector_heat(universe, members, pos_of, all_days, i)` | 板块中位数 5 日涨幅代理(§6.5) | build_calendar |
| `score_at(d, i, now)` | 重建 quote 调 `an.score_stock`(§6.3) | analysis |
| `select_baskets(...)` | 出 A/B/C/D/E(§6.6) | score_at, sector_heat |
| `compute_metrics(returns)` | mean_pct/win_rate/n(§6.7) | 无 |
| `welch_t(a, b)` | 双样本 Welch t(§6.7) | scipy/numpy 或手写 |
| `build_report(...)` | 拼 JSON + markdown + 版本戳(§7) | 无 |
| `main(argv)` | argparse CLI(§10) | 全部 |

`now` 用常量 `AFTER_CLOSE = datetime(2026, 1, 1, 15, 1)`(与探针第 39 行一致),保证 `is_after_close` / `trading_minutes_elapsed` 行为确定、不随真实日期漂移。

## 5. 精确复现规范

以下公式与常量须与 `_analysis/nxday_backtest.py` 核心路径逐字一致。

### 5.1 常量

```
TOP_SECTORS   = 3
PER_SECTOR    = 5
EVAL_DAYS     = 1200     # 每只截尾 1200 根(≈5 年)
B_SAMPLE_EVERY = 10      # 全市场篮子 B 每 N 日采样
MIN_AMOUNT    = 1e8      # 流动性下限(volume*close)
AFTER_CLOSE   = datetime(2026, 1, 1, 15, 1)
```

### 5.2 评估日采样

```
all_days   = 排序后的全市场交易日并集
start      = max(61, len(all_days) - 1 - EVAL_DAYS)
step       = max(1, (len(all_days) - 2 - start) // 300)   # 维持 ~300 个评估日
eval_days  = range(start, len(all_days) - 1, step)
```

上界 `len(all_days)-2` 保证 T+1(下一交易日)在日历内存在。若某评估日 `sector_heat` 为空(无任何板块 ≥3 只有效成员),整日跳过。

### 5.3 评分调用(逐字)

```python
df = d.iloc[:i + 1]                       # 仅 ≤ T 的历史
if len(df) < 61:
    return None
quote = {"price": float(df["close"].iloc[-1]),
         "change_pct": float(df["change_pct"].iloc[-1]),
         "volume": float(df["volume"].iloc[-1]),
         "amount": float(df["volume"].iloc[-1]) * float(df["close"].iloc[-1])}
sc = an.score_stock(df, quote, AFTER_CLOSE)
return sc if sc["composite"] is not None else None
```

结果按 `(code, i)` 记忆化。`score_stock` 返回键:`position / trend / volume_price / signal / risk / composite`(无 verdict/overheated)。`amount = volume*close` 为合成值(`score_stock` 内部不消费 amount,仅入 quote 字典)。

### 5.4 目标(逐字)

```python
def next_returns(d, i):
    if i + 1 >= len(d):
        return None
    c0 = float(d["close"].iloc[i]); o1 = float(d["open"].iloc[i+1]); c1 = float(d["close"].iloc[i+1])
    if c0 <= 0 or o1 <= 0 or c1 <= 0:
        return None
    return {"gap": o1/c0 - 1, "close1": c1/c0 - 1, "od": c1/o1 - 1}
```

T+1 = **同一只股票自身序列的下一行**(停牌股 i+1 可能跳过日历日),非日历并集的下一元素。

### 5.5 板块热度代理(逐字)

`_win_gain(c, i, w)`:end = `all_days[i]`,start = `all_days[max(0, i-(w-1))]`,各自映射到该股 bar 位置;`gain = close[end]/close[start] - 1`;任一日期缺失或 `end<=start` 或 `start close<=0` → None。

对每个板块 s,对每个成员算 `_win_gain(c, i, 5)`;若有效成员 ≥3,则 `heat[s] = median(成员 5 日涨幅)`。`hot = 按 heat 降序取前 TOP_SECTORS`。

**确定性(并列定序):** `sector_members` 必须按 `code2sector.json` 文件顺序构建(探针 :66-70 依文件顺序迭代)。`hot`、`scored`(§5.6)用 Python 稳定排序,并列浮点按该插入顺序定序;`buyable` 迭代用 `sorted(buyable)`、C 候选用 `sorted(hot_members ∩ buyable)`,保证跨运行确定。

### 5.6 篮子

**每日可买集合 `buyable`(评分前预过滤,逐字):** 对每只 c(在 `pos_of` 中有当日 bar i 且 `i+1 < len`):`next_returns` 非 None;`change_pct < an.limit_threshold(code)` 且 `change_pct > -7.0`(剔除涨停与近跌停);`volume[i]*close[i] >= MIN_AMOUNT`。

- **A(实际管线代理):** 对每个 `hot` 板块,取 `成员 ∩ buyable` → `get_score` 非 None 且 `risk < 70` → `bonus = 8 if composite>=68 else 4 if composite>=60 else 0` → `fa = composite + bonus` → 按 `-fa` 排序取前 `PER_SECTOR`。跨 3 个热板块拼接(至多 15 只)。度量 A 的 od / gap / close1。`scored` 条目按 `(fa, code, position)` 记录(第 1/2 位为 code/position,供 E 分拆用;不含探针的 fb/fc/fc10/fc20 与 composite 残留,它们属 P0b/P3 子分析)。
- **B(全市场 top15):** 仅当 `(i - start) % B_SAMPLE_EVERY == 0`;候选 = 全 `buyable`;`get_score` 非 None 且 `risk < 70`;按 `composite` 降序取前 `TOP_SECTORS*PER_SECTOR = 15`。度量 od。
- **C(热板块随机):** 候选 = `sorted(hot_members ∩ buyable)`(定序;探针用 set 交集 `list(...)` 跨运行顺序不确定,模块以 sorted 保证可复现);无 score/risk 过滤;若 >15 用**模块级单个 `rng = np.random.default_rng(0)`** 的 `.choice(..., 15, replace=False)` 无放回抽 15,否则全取。度量 od。`rng` 须为模块级单实例、所有评估日按循环顺序复用,不得每评估日重建(探针 :183 单例、:380 顺序复用)。
- **D(全市场基准):** 候选 = 全部 `buyable`(无过滤无排序)。等权均值 od。
- **E(position 分拆):** 用与 A 相同的候选集(每个 `hot` 板块内 `buyable ∩ 有效评分 ∩ risk<70` 的**全部**成员,非 A 已选的 top5),按 `-position` 重排;`pos_hi`(低位)= 前 `PER_SECTOR`,`pos_lo`(高位)= 后 `PER_SECTOR`,跨 3 热板块拼接。度量 od。

`stats(basket, m) = mean([m[c] for c in basket if c in m])`(空篮子 → NaN)。

### 5.7 度量(逐字)

```
summ(arr):   a = 数组(剔除 None/NaN)
             mean_pct = round(nanmean(a)*100, 3)
             win_rate = round(mean(a>0)*100, 1)
             n        = int(len(a))
             空 → (nan, nan, 0)
summ_year(arr, years):  按 year=all_days[i][:4] 分组,每组同 summ
```

**Welch 增强:** 对 A / B / C / E_hi / E_lo 各篮子(有日级 od 序列),`welch_t(X_od, D_od)` → `{t, p}`(双样本不等方差 t 检验);**不对 D 自身比较**。适用性声明:A/C 是 D 的子集、B 是抽样(n≈30)对 D(n≈400),非配对且样本重叠,独立假设不成立,t/p 偏保守,**仅作定性参考**。记录在报告 `welch` 字段。

### 5.8 报告行

整体 8 行 + 按年 5 标签(标签与探针一致,便于 diff):

```
A 实际管线(热板块xtop5)  次日od / A 隔夜gap / A close->next close
E 板块内低位股(pos分top5)次日od / E 板块内高位股(pos分bot5)次日od
B 全市场top15  次日od / C 热板块随机  次日od / D 全市场基准  次日od
```

按年标签:`A 实际管线 / E 板块内低位股(pos top5) / E 板块内高位股(pos bot5) / C 热板块随机 / D 全市场基准`(不含探针的 P0b 配置篮子 b/c/c10/c20)。

## 6. 反泄露保证(结构与测试双保险)

1. **结构**:评分输入 `d.iloc[:i+1]`(≤T);验证用 `next_returns` 读 i+1 的 open/close;`now` 固定为 AFTER_CLOSE,不用真实日期。`buyable` 的 `i+1 < len` 是**可测性筛选**(无次日价无从测收益),对所有篮子(含 D)一致,非未来数据泄露。
2. **测试(§11.3 反泄露测试)**:构造小夹具,断言「改动 T+1 行的 open/close,T 日评分逐位不变、篮子逐位不变」。

## 7. 报告输出与版本戳

- JSON:`--out`(默认 `backtest_baseline.json`,repo 根,提交)。结构:

```json
{
  "system_version": "<git rev-parse --short HEAD 的 7 位短哈希,git 不可用则 'unknown'>",
  "module_version": "1.0.0",
  "generated_at": "<ISO 8601>",
  "data_range": {"start": "<all_days[0]>", "end": "<all_days[-1]>"},
  "n_eval": <int>, "step": <int>,
  "window": {"start": "<all_days[start]>", "end": "<all_days[len-2]>"},
  "sector_heat_note": "median-5-day-gain proxy (daily pkl 无 amount,生产板块 composite 不可复现)",
  "rows":   { "<label>": {"mean_pct": .., "win_rate": .., "n": ..} },
  "by_year": { "<label>": [{"year": .., "mean_pct": .., "win_rate": .., "n": ..}] },
  "welch":  { "<label>": {"t": .., "p": ..} }
}
```

- `n_eval` = **实际处理日数**(sector_heat 为空的评估日不计入;探针 :397 逐日累加)。`data_range` / `window` 为 **1200 根截尾后的并集区间**,非原始 pkl 全史(1990-12-19);截尾后 `all_days[0]` ≈ 2019-02-25、`window.start` ≈ 2021-08-25。
- Markdown:`--out` 同基名 `.md`(默认 `backtest_baseline.md`,提交)。含版本戳、数据区间、n_eval、整体表、按年表、Welch 表、**篮子 A 代理声明**(§8)。

`system_version` = git 短哈希是权威代码标识(可复现);`module_version` = 盲测模块自身 semver。

## 8. 篮子 A 是「代理管线」——如实声明

因日线 pkl 无 `amount`(成交额),生产 `collect_sector_metrics` 所需 `turnover_ratio`(emotion 25%)与 `activity`(strength 30%)无法历史复现。故基线「篮子 A」与生产 `recommend.py` 管线**存在以下代理差异**,报告须逐条标注:

| 维度 | 生产 recommend.py | 基线篮 A(代理) |
|---|---|---|
| 板块选择 | `select_sectors`:verdict ∈ {建议关注, 跟踪(热点延续)} + composite 降序 + top3 | `hot` = 板块中位数 5 日涨幅 top3 |
| 个股加成 | `sector_bonus(sector_composite, ...)`(68/60/50,+8/+4/−5) | 个股 composite 分档 `8 if >=68 else 4 if >=60 else 0`(无 −5) |
| 热权重重算 | `_apply_hot_weights`(composite≥68 时改用 HOT_SIGNAL_WEIGHTS) | 无(恒用 V3_WEIGHTS) |
| 个股硬过滤 | filter_candidates:ST / 新股 / 停牌 / 涨停 / ≤−7% / amount<1e8 | 仅 buyable:涨停 / ≤−7% / volume*close≥1e8(无 ST/新股/停牌) |

补充两处代理差异(与上表同类):

- **加成×风险折扣位置**:生产 `stock_composite_v3(..., bonus)` = `(quality + bonus) * (1 - risk/100)`,加成在折扣**内**;探针 A `fa = sc["composite"] + bonus` = `quality * (1 - risk/100) + bonus`,加成在折扣**外**。同档内 +8 对生产是 8*(1-risk/100)、对探针是全额 8,高/低风险股相对排序会变。
- **verdict 过滤**:生产 `rank_candidates` 还剔除 `verdict == "回避"`(<42 分);探针 A 仅过滤 `risk < 70`。薄板块(达标成员 <5)时 <42 分股可占位,属已知微小差异。

结论:基线篮 A 的准确率是「代理管线」的准确率,作为**当前版本可复现的近似基线**;真正的生产口径基线须待 `amount` 数据补齐(后续切片)。

## 9. 错误处理

- `--data-dir` 不存在 / 无 pkl → 明确报错退出(非裸 except)。
- `--sector-map` 缺失或非 GBK 可解码 → 明确报错。
- 单只 pkl 缺列(缺 `close`/`volume` 等)→ 跳过该股并计入诊断计数(非崩溃)。
- 最后一日因缺 T+1 导致 `next_returns` None → 正常跳过(非泄露,非错误)。
- 空篮子 → `stats` 返回 NaN,报告该行为 `{"mean_pct": null, "win_rate": null, "n": 0}`。

## 10. CLI

```
python backtest.py [--data-dir _analysis/daily] [--sector-map _analysis/code2sector.json]
                   [--out backtest_baseline.json]
```

- `--out` 缺省 `backtest_baseline.json`;markdown 同基名 `.md` 一并写出。
- 按年切分(by_year)恒输出,无开关参数。
- 无任何位置参数。退出码 0 成功 / 2 参数错 / 1 运行错。

## 11. 测试(`tests/test_backtest.py`)

遵循仓库约定(见提取:conftest.py 加 sys.path、`import backtest`、`pytest.approx`、`monkeypatch.setattr`、文件内 `make_daily` 辅助)。

### 11.1 目标公式
合成 `make_daily` → 断言 `next_returns` 的 gap/close1/od 精确值(`pytest.approx`);越界/非正价返回 None。

### 11.2 篮子与度量
- 合成多板块小夹具,monkeypatch `an.score_stock` 返回固定 composite/risk/position,断言 A/B/C/D/E 成员精确列表与 top-N 逻辑。
- `summ` / `summ_year`:均值、胜率、n、按年分组、空数组 → (nan,nan,0)。
- `welch_t`:已知两样本断言 t/p(与 `scipy.stats.ttest_ind(..., equal_var=False)` 一致,或手写公式等值)。

### 11.3 反泄露测试(核心)
夹具:≥61 根日线。跑 `score_at(d, i, AFTER_CLOSE)` 得评分 S1;把 `d.iloc[i+1]` 的 open/close 改成**极端正值**(保持 >0,避免 `next_returns` 变 None 使该股退出 buyable),再跑得 S2;**断言 S1 == S2 逐位相等**(证明 T+1 数据不进入评分)。另断言 `next_returns(d, i)` 随之改变(证明验证确实读了 T+1)。

### 11.4 CLI 与报告
- `--data-dir` 指向 tmp_path 下自建 pkl 集,跑 `main`,断言 JSON/MD 写出、`system_version`/`data_range`/`rows`/`welch` 字段齐全、GBK sector-map 被正确解码(中文板块名无乱码)。
- 缺 `--data-dir` → 退出码非 0。

运行:`python -m pytest tests/test_backtest.py -q`;全量回归 `python -m pytest tests/ -q` 须保持 129 + 新增通过。

## 12. 验收标准

1. `python -m pytest tests/ -q` 全绿(129 + 新增)。
2. `python backtest.py --data-dir _analysis/daily --sector-map _analysis/code2sector.json --out backtest_baseline.json` 在真实缓存上成功产出,`rows` 与 `_analysis/nxday_results.json` 同名行数值一致(逐行核对 A/B/D/E 的 mean_pct/win_rate/n),`n_eval` / `step` / `window` 一致。**C 行例外**:探针 C 用 set 交集顺序(跨运行非确定),模块以 `sorted` 定序保证可复现,故 C_od 可能与探针单次结果不同(属预期,报告标注)。整文件 diff 时 `by_year` 比探针少 4 个 P0b 键,属预期。
3. 报告含 `system_version`(git 短哈希)、`data_range`、篮子 A 代理声明。
4. 无未来数据泄露(§11.3 测试通过)。
