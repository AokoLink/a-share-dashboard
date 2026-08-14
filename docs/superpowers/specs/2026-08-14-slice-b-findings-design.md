# 审计切片 B — 剩余 12 条 code-review 缺陷修复设计

> 目标:修复切片 A 之后剩余的 code-review 缺陷(P1 ×2、perf ×2、maint ×5、范围外 ×3),
> 全部为「无评分影响」的维护/正确性/安全修复,不重测准确率、不触碰评分公式。

## 上下文与范围

切片 A 已修 3 个评分影响项(量能单位、quote high/low/open、热权重 signal 模式),V2 重测结论为
「修复生效但准确率影响噪声级」。本切片 B 处理剩余的 **12 条 finding**(实际 13 处独立改动,见下),
其中 11 处真实代码改动、2 处经核验为**误报/正确设计**(裁定「留」)。

**分类(13 处):**

| # | 优先级 | 文件 | 缺陷 | 处置 |
|---|---|---|---|---|
| 1 | P1 | app.py:292/294 | is_next_day 盘中误判(锚点用 close_date 而非 signal_date) | 改锚点 |
| 2 | P1 | static/app.js | innerHTML 拼股票名未转义(XSS) | 加 esc() 助手 |
| 3 | perf | app.py:235-237 | 个股接口每次重算全 ~90 板块 | 预过滤 summary |
| 4 | perf | store.py | 每函数开新 SQLite 连接(~360 次/请求) | 线程本地连接缓存 |
| 5 | maint | analysis.py:202-204 | score_sector 重算 e_hi/s_hi/r_hi | 抽 _high_flags 助手 |
| 6 | maint | analysis.py:320-323 | _pos60 公式与 compute_position_score 内联重复 | 复用 _pos60 |
| 7 | maint | recommend.py:104/147 | _score_candidate 冗余传 pre-bonus composite 作 quality | 删 quality 实参 |
| 8 | maint | recommend.py:18 | PRICE_REL_MIN 死常量(生产零引用) | 删常量 + no-op 测试行 |
| 9 | maint | recommend.py:129 | pandas 惰性 import | 提升到顶部 |
| 10 | maint | analysis.py:208-209 | P0_PATH=="intercept" 死分支 | **裁定:留**(见 §R2) |
| 11 | 范围外 | store.py:116-117 | get_sector_change_3d 空值 | **裁定:非缺陷**(见 §R3) |
| 12 | 范围外 | data_source.py:275-294 + app.py:254-256 | get_stock_minute 产 NaN → 非法 JSON | 源头 None 归一 + 消费方 None 安全 |
| 13 | 范围外 | app.js:58 | loadSectors null 强转(0→true) | 加 != null 守卫 |

## Global Constraints(继承切片 A,逐字)

- 直接提交 main,不建 worktree/feature 分支。
- `_analysis/` 已 gitignore——探针工作永不提交。
- Python 源码仅 ASCII `-`,禁用 U+2212 `−`。
- `_analysis/code2sector.json` 为 GBK 编码(open 用 `encoding="gbk"`)。
- 严格反未来泄露:预测输入 ≤ T,验证只用 > T。
- 全量测试 `python -m pytest tests/ -q`;不伪造测试成功、无真实数据不编造。
- pandas 3.x Copy-on-Write:`df["col"].iloc[0]=x` 会抛,用 `df.iloc[0, df.columns.get_loc("col")]=x`。

---

## §1 app.py — is_next_day 盘中误判(P1)

**现状(:291-294):**

```python
if prev and prev["close_date"] is not None:
    is_next_day = (prev["close_date"] == payload["prev_trading_date"])
    td = payload.get("trading_dates") or []
    gap_days = sum(1 for d in td if prev["close_date"] < d <= payload["signal_date"])
```

**缺陷**:上一快照若在盘中生成(如 D 日 10:30),其 `close_date` = 最后完整日线 = D-1,而
`signal_date` = D。当前用 `close_date` 比当前 `prev_trading_date`(= D)→ 误判 `is_next_day=False`,
尽管两快照确为相邻交易日。spec v4(commit `5e4134d`)已将锚点定为 `signal_date`,生产代码未跟进。

**修复**:三处 `close_date` → `signal_date`:

```python
if prev and prev["signal_date"] is not None:
    is_next_day = (prev["signal_date"] == payload["prev_trading_date"])
    td = payload.get("trading_dates") or []
    gap_days = sum(1 for d in td if prev["signal_date"] < d <= payload["signal_date"])
```

守卫同步改 `signal_date`(落库快照 signal_date 为 PK,恒非 None;改后语义自洽)。

**已知边界(接受)**:对盘中生成的 prev 快照,`stocks[].signal_close` 是 D-1 收盘价,故次日
`gap_pct` 实测的是「D-1 收盘 → D+1 开盘」的跳空,非「D 收盘 → D+1 开盘」。这是 best-effort 的
正确口径(盘中快照本无 D 收盘价),不改。此点仅在 spec 记录,不额外处理。

**测试**:更新 `tests/test_recommend.py` 的 prev 快照 fixture(补 `signal_date` 字段);新增两用例——
①盘中生成 prev(signal_date=D, close_date=D-1)且当前 prev_trading_date=D → `is_next_day=True`;
②跨多日 gap → `gap_days` 计数正确。

---

## §2 static/app.js — XSS(P1)

**现状**:龙头页/推荐页 16 处 `innerHTML` 拼接股票名/板块名/理由等后端字符串,未转义。后端返回的
name/verdict/tag/reason 等字段若含 `<>&"` 会注入脚本。

**修复**:文件顶部加 `esc()` 助手,16 处注入点全部包裹:

```javascript
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
```

注入点逐处把 `x.name` → `esc(x.name)`(及 code/sector_name/verdict/tag/reason 等同后端来源字段)。
纯前端数字字段(price/change_pct/composite)已 `Number()` 归一,不需转义但包裹也无害——**只包裹字符串字段**。

**测试**:无 JS 测试框架(本项目无 jest/vitest)。验证方式 = 手工回归:龙头页 + 推荐页正常渲染,且
构造含 `<` 的股票名/板块名在浏览器不执行。此为已知边界,spec 记录不做自动化 JS 测试。

---

## §3 app.py — api_stock 重算全板块(perf)

**现状(:233-239):** 个股接口取到 `names`(该股所属板块,1-3 个)后,仍对**全量 summary** 调
`recommend.score_all_sectors`,算完 ~90 个板块再从 `by_name` 取需要的 1-3 个。

**修复**:先按 names 预过滤 summary,再打分:

```python
names = ds.resolve_code_sectors(code6)
if names:
    summary, _ = ds.get_sector_summary("industry")
    spot, _ = ds.get_market_spot()
    need = set(names)
    summary_f = summary[summary["name"].astype(str).isin(need)]
    scored_sectors = recommend.score_all_sectors(
        summary_f, app.config["DB"], "industry", store,
        float(spot["amount"].sum()) if len(spot) else 0.0, now)
    by_name = {s["name"]: s["composite"] for s in scored_sectors}
    comps = [by_name[n] for n in names if n in by_name and by_name[n] is not None]
```

`score_all_sectors` 的输出只含 summary 中的板块(按行打分),预过滤后 `by_name` 只含需要的 1-3 个,
`comps` 语义不变(等价:原来对全量打分后也只取 `names ∩ by_name`)。

**测试**:mock `recommend.score_all_sectors` 断言收到的 summary 行数 == names 匹配数(≤3),且返回的
composite 仍被正确解析到 `sector_bonus_val`。

---

## §4 store.py — SQLite 连接缓存(perf)

**现状**:`_connect` 每次 `sqlite3.connect` + 2 条 PRAGMA;10 个函数(upsert/get×9)各自开+关连接。
`score_all_sectors`/`collect_sector_metrics` 对 ~90 板块循环,每板块 ~4-5 次 store 调用 → 单请求
~360-450 次连接开销(每次含 `PRAGMA journal_mode=WAL`)。

**修复(线程本地缓存,key=abspath(db)):**

```python
import threading
_conns = threading.local()

def _conn_cache():
    cache = getattr(_conns, "cache", None)
    if cache is None:
        cache = {}
        _conns.cache = cache
    return cache

def _connect(db):
    key = os.path.abspath(db)
    cache = _conn_cache()
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        cache[key] = conn
    return conn

def close_all():
    cache = getattr(_conns, "cache", None)
    if cache:
        for conn in cache.values():
            try:
                conn.close()
            except Exception:
                pass
        _conns.cache = {}
```

配套:
- 10 个函数删除 `conn.close()`;写函数保留 `conn.commit()`(逐操作提交语义不变)。
- `init_db` 删 `conn.close()`(连接留在线程缓存复用);它仍是首次建表入口。
- `close_all()` 供应用关闭与测试 teardown 调用。

**线程安全**:Flask 多线程模型下每线程独立连接(threading.local),WAL 允许跨线程并发读。
同线程内顺序读写同连接,看到自己已提交的写——与现状逐连接提交语义一致,无行为变化。

**风险与处理**:①`init_db` 若继续 close 会关闭缓存连接 → 后续 `_connect` 拿到已关闭连接抛
`ProgrammingError`。故 init_db 必须不再 close(见上)。②Windows 下 tmp_path 清理遇打开的文件句柄
报 PermissionError → 测试 fixture teardown 须调 `store.close_all()`。

**测试**:①同一线程连续两次 `_connect(db)` 返回同一连接对象;②写后读同一连接可见(commit 生效);
③`close_all()` 后 `_connect` 开新连接;④现有 store 测试 fixture 加 `close_all()` teardown,确认
Windows tmp_path 清理通过。

---

## §5 analysis.py — 两处重复公式去重(maint)

### §5.1 score_sector 重算 e_hi/s_hi/r_hi

`score_sector`(:202-204)重算的 `e_hi/s_hi/r_hi` 与 `sector_verdict`(:166-170)完全一致。
抽共享助手:

```python
def _high_flags(emotion, strength, risk):
    e_hi = emotion is not None and emotion >= 70
    s_hi = strength is not None and strength >= 60
    r_hi = risk is not None and risk >= 65
    return e_hi, s_hi, r_hi
```

- `sector_verdict` 改为 `e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)`;
  中档 `e_mid(45)/s_mid(35)` 保留在 sector_verdict 内(不在共享助手,阈值不同)。
- `score_sector` 改为 `e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)`。

**等价性**:阈值逐字一致(70/60/65),只搬不改成。行为零变化。

### §5.2 _pos60 复用

`compute_position_score`(:320-323)内联的 pos60 公式与 `_pos60`(:419-425)同式。改:

```python
pos60 = _pos60(df)   # df = add_ma(daily_df, (20,))
```

`add_ma` 返回 `df.copy()` 且只增 ma 列、不改 low/high/close,故 `_pos60(df)` 与内联计算
`(last_close - lo_min)/(hi_max - lo_min)` 逐位一致。行为零变化。

**测试**:两处改动由现有全量测试(`tests/test_analysis.py` 等)钉值证明无漂移;不新增用例,但须跑全量
确认 `compute_position_score`/`score_sector`/`sector_verdict` 的既有断言全绿。

---

## §6 recommend.py — 三处清理(maint)

### §6.1 删冗余 quality 实参(:104/147)

`sector_bonus(sector_composite, scores["composite"])` 把「加成前 composite」当 quality 传,但
`BONUS_QUALITY_GATE=False`(:16)短路 `quality` 读取(:262),实参死。

**修复(最小方案)**:两处删第二实参:

```python
bonus = sector_bonus(sector_composite)   # :104 与 :147 同改
```

哨兵 `if scores["composite"] is None`(:102)**保留不动**——它是「数据不足」守卫,与 quality 实参无关。
`score_stock` len<61 时 composite 与 risk 同为 None(analysis.py:550-552),哨兵仍正确。

**等价性**:`BONUS_QUALITY_GATE=False` 时 `quality` 恒不读,删实参零行为变化。若未来翻 True,该门
本也不应用于此(composite 非 quality 语义),删实参反而消除误导。

### §6.2 删 PRICE_REL_MIN 死常量(:18)

`PRICE_REL_MIN` 生产代码零引用(仅 `tests/test_recommend.py:260` 一处 no-op monkeypatch)。
plan Task 11 已定「此分支不实现」。删除:

- `recommend.py:18` 常量行。
- `tests/test_recommend.py:260` `monkeypatch.setattr(recommend, "PRICE_REL_MIN", False)`(该测试
  `test_price_floor_excludes_low_price` 只测 PRICE_FLOOR,此行是残留 no-op)。

保留 `BONUS_GE75`/`BONUS_QUALITY_GATE`/`PRICE_FLOOR`——它们已接线(sector_bonus/filter_candidates
读取),非死常量。

### §6.3 pandas 惰性 import 提升(:129)

`_rel_strengths` 内 `import pandas as pd` → 移到文件顶部 import 区(:3-7 后加 `import pandas as pd`),
删 :129 行。无行为变化。

**测试**:§6.1/§6.2 由现有测试验证(§6.2 删除 no-op 行后 `test_price_floor_excludes_low_price` 仍绿);
`test_price_floor_default_off` 已钉 PRICE_FLOOR 默认值,不受影响。全量跑绿即可。

---

## §7 data_source.py + app.py — get_stock_minute NaN → 非法 JSON(范围外)

**现状**:`get_stock_minute.fetch`(:278-292)`avg = cum_amt / cum_vol.where(cum_vol > 0)` 在累计量为 0
处产 NaN;price/volume 经 `to_numeric(errors="coerce")` 亦可 NaN。Flask 3.x 默认 `json.dumps` 的
`allow_nan=True` 会把 NaN 序列化成字面 `NaN`,浏览器 `JSON.parse` 抛错(非法 JSON)。

**修复(两文件联动):**

1. `data_source.py` fetch 末尾归一(对齐 `get_sector_summary:424` 既有手法):

```python
return out.where(pd.notna(out), None)
```

2. `app.py` intraday 构造(:254-256)改为 None 安全——否则 `float(None)` 抛 TypeError:

```python
def _f(v):
    return None if v is None or v != v else float(v)

intraday = [{"time": str(x["time"]), "price": _f(x["price"]),
             "avg": _f(x["avg"]), "volume": _f(x["volume"])}
            for x in minute.to_dict("records")]
```

(`v != v` 同时兜住残留 NaN,双保险。)归一后 NaN→None→`json.dumps` 产 `null`(合法 JSON)。

**测试**:①`get_stock_minute` 构造累计量为 0 的原始行 → 返回 `avg` 为 None 而非 NaN;②api_stock
intraday 对 None 值产出 `"price": null` 不抛;③现有 `tests/test_data_source.py` 相关用例不回归。

---

## §8 static/app.js — loadSectors null 强转(范围外)

**现状(:58)**:`s.index_change_pct >= 0` 对 null 强转 → `0 >= 0` → true,停牌/无数据板块被当「红盘」。
与 :107 已有 `!= null` 守卫不一致。

**修复**:对齐 :107:

```javascript
if (s.index_change_pct != null && s.index_change_pct >= 0) { /* 红 */ }
else { /* 绿/平 */ }
```

**测试**:无 JS 测试框架,手工回归确认停牌板块颜色不再误判为红。

---

## 裁定

**§R1 — 无评分影响,不重测准确率。** 本切片全部改动不触碰 `stock_composite_v3`/权重/阈值/因子公式;
is_next_day 属快照展示层、XSS 属前端、其余为 perf/maint/范围外。故不跑 backtest 对比,无 V3 基线产物。

**§R2 — P0_PATH=="intercept" 死分支:留。** 该分支(:208-209)是 `decisions.md` 记录的双路径决策开关
(定稿走 `"badge"`,intercept 惰性)。删除需同时删 :17 常量、:208-209 分支、`test_sector_verdict_overheat_distinct_label`
钉值测试,且造成 decisions.md 与代码漂移。成本 > 收益;死分支本身无害且语义清晰。**裁定:保留,不加注释**
(现状注释「路径 A;生产 badge 惰性」已说明意图)。

**§R3 — get_sector_change_3d 空值:非缺陷。** `len(rows) < 2` 返回 None 是文档化语义(不足 2 日无 3 日
涨跌,下游 `sector_risk` 对 None 走「缺失」分支,正确处理)。不改代码。可选(不强制):docstring 补一句
「不足 2 日返回 None」,不新增测试。

---

## 非目标(本切片不做)

- 不重写/重构架构;不动 `stock_composite_v3`/`sector_bonus`/`sector_verdict` 阈值与公式。
- 不处理 recommend.py 4 处**既有** U+2212 docstring(切片 A ledger 已记为 out-of-scope,与切片 B 无关)。
- 不引入 JS 测试框架。
- 不做 XSS 的服务端二次校验(前端转义即可覆盖本 finding;服务端输出约定由现有 ok/err 序列化保证)。

## 自审记录

- 占位符:无 TBD/TODO。
- 一致性:锚点 signal_date 改动与 spec v4 一致;`_high_flags`/`_pos60` 等价性已逐字核验;
  `sector_bonus` 删实参等价性基于 `BONUS_QUALITY_GATE=False` 已验证。
- 范围:12 条 finding 全覆盖(13 处改动,含 2 裁定留),单一切片、单一实施计划可承载。
- 歧义:is_next_day 盘中 gap_pct 边界已明确「接受不处理」;get_stock_minute 两文件联动已明确。
