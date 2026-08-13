# 热板块动量选股 + 次日执行口径 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec `docs/superpowers/specs/2026-08-13-hot-momentum-execution-design.md`(v4)落地六项改进:热板块动量选股权重(P0b)、过热标签(P0)、次日执行确认(P1)、od 口径重校准(P2)、板块加成门槛(P3)、低价护栏(P4)。

**Architecture:** 探针与生产分离——所有改变选股的机制(P0b 权重/P0 路径/P3 门槛/P4 护栏/P2 阈值)由 `_analysis/`(gitignored)探针产出决策、写入 `_analysis/decisions.md`,生产代码消费该决策记录;P1 执行确认(P1 infra)不依赖探针,先行落地完整功能。P0b 选股权重在 recommend 层聚合阶段完成,`analysis.score_stock` 保持纯净。

**Tech Stack:** Python 3 / Flask / pandas / numpy / pytest / SQLite(store.py)/ 原生 JS(无框架前端)。

**Spec:** `docs/superpowers/specs/2026-08-13-hot-momentum-execution-design.md`(评审 v4,commit 5e4134d)

## Global Constraints

以下约束逐任务生效(值从 spec 逐字复制):

1. **探针决定机制**:改变选股的机制,其阈值/形态由 `_analysis/decisions.md`(探针产出)决定。生产代码默认 spec 的 provisional 值,探针跑完更新。
2. **探针与生产分离**:探针脚本全在 gitignored `_analysis/`,不 CI 不部署。探针任务不产生 git commit(工作区 gitignored),以「写入决策记录」收尾。
3. **`analysis.score_stock` 纯净**:不新增网络、不改四因子计算;P0b 权重变化只在 recommend 层聚合阶段完成。
4. **热板块谓词**:`sector_composite >= 68`(常量 `HOT_COMPOSITE_THRESHOLD = 68.0`),单请求内固定(score_all_sectors 一次调用复用)。
5. **权重表**(spec §3.2/§3.3):
   - v3:`0.55 position + 0.15 vp + 0.10 trend + 0.20 signal`
   - 热 rel(配置 c):`0.40 position + 0.15 vp + 0.10 trend + 0.20 signal + 0.15 rel_strength`
   - 热 signal(配置 b):`0.40 position + 0.15 vp + 0.10 trend + 0.35 signal`
   - 热 rel 模式 + 基数 <3 → 回退 v3。
6. **rel_strength**(spec §3.3):个股 5 日涨幅在板块内**分位 × 100**;分位基数 = 该板块通过 `filter_candidates` 且有 ≥6 根日线的成分股(与「打分成功(≥61 根、risk<70、verdict≠回避)」解耦);并列分位用 `pandas.Series.rank(pct=True, method="average")`(均值秩);基数 <3 → `None`。
7. **快照生命周期**(spec §5.2,关键语义):
   - 两日期字段分离:`signal_date`(推荐生成交易日,快照 PK)、`close_date`(日线末根日期,取众数)。
   - `signal_date` 派生:工作日且 `(an.is_trading_time(now) or an.is_after_close(now))` → `now.date().isoformat()`;否则回退 `close_date`。
   - `prev_trading_date` 锚点 = **`signal_date` 之前最近一个交易日**(非 close_date,盘中两者相差一天,锚错会使 is_next_day 恒 False)。
   - 相邻性:`is_next_day = (prev.close_date == 当前请求的 prev_trading_date)`;`gap_days` = 交易日历中 `prev.close_date < d <= signal_date` 的天数。
8. **警示阈值**(spec §5.4):`GAP_WARN_PCT = -1.5` provisional,常量处注释「provisional, P1 探针定稿」。
9. **前端数字标注出处**(spec §5.3):免责声明数字为「基于旧策略的历史回测(2025-08~2026-08)」,P0b 换权重后重标。
10. **已知不一致**(spec §13,接受并标注):`/api/stock` 详情页与 `collect_actionable_leaders` 不计算 rel_strength 分位,显示 v3 权重 composite,标注「未含板块内相对强度」。禁止为消除不一致而给这两处拉全板块成分股。
11. **P0 过热**(spec §4):`score_sector` 输出结构化 `overheated` 布尔(`consecutive_days >= N` 且 e_hi and s_hi and not r_hi);composite 不因过热改变;路径 A 用独立标签「过热(连涨)」,不得复用现有风险驱动标签「谨慎追高(过热)」。
12. **全量回归**:收尾 `python -m pytest tests/ -q`,现 110 green,不得回归。

---

### Task 1: 数据源 `get_market_spot` 补 `open` 列

**Files:**
- Modify: `data_source.py:212-227`(`get_market_spot`)
- Test: `tests/test_data_source.py`

**Interfaces:**
- Produces: `ds.get_market_spot()` 返回 DataFrame 增加 `open` 列(东财 spot「今开」,数值型;缺失为 NaN)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_data_source.py` 末尾追加(复用本文件 `make_spot()`(lines 12-20,五只无 open 列)与 `test_market_spot_stale_on_failure` 的缓存清理模式):

```python
def test_market_spot_includes_open(monkeypatch):
    ds.cache._data.clear()                          # 避免被其他用例缓存污染(与 test_market_spot_stale_on_failure 同模式)
    raw = make_spot().copy()
    raw["今开"] = [9.9, 1340.0, 11.8, 44.5, 199.0]  # 东财 spot 列名「今开」
    monkeypatch.setattr(ds._ak, "stock_zh_a_spot", lambda: raw)
    df, stale = ds.get_market_spot()
    assert stale is False and "open" in df.columns
    assert float(df.loc[0, "open"]) == pytest.approx(9.9)   # sh600000 今开 9.9
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_data_source.py::test_market_spot_includes_open -v`
Expected: FAIL(断言 `"open" in df.columns` 为 False)

- [ ] **Step 3: 实现**

在 `get_market_spot` 的 `out = pd.DataFrame({...})` 中加一行,并把 `open` 加入 `to_numeric` 循环:

```python
def get_market_spot():
    def fetch():
        raw = _ak.stock_zh_a_spot()
        out = pd.DataFrame({
            "code": _pick(raw, "代码", "code").map(normalize_code),
            "name": _pick(raw, "名称", "name"),
            "price": _pick(raw, "最新价", "price"),
            "change_pct": _pick(raw, "涨跌幅", "change_pct"),
            "volume": _pick(raw, "成交量", "volume"),
            "amount": _pick(raw, "成交额", "amount"),
            "open": _pick(raw, "今开", "open"),
        })
        for col in ("price", "change_pct", "volume", "amount", "open"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out
    return _cached(_key("spot"), 60, lambda: _fetch_with_retry(fetch))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_data_source.py -v`
Expected: PASS(含新测试)

- [ ] **Step 5: 回归**

Run: `python -m pytest tests/ -q`
Expected: 110 passed + 新测试

- [ ] **Step 6: Commit**

```bash
git add data_source.py tests/test_data_source.py
git commit -m "feat(data_source): get_market_spot 增加 open 列(P1 次日低开对比需要)"
```

---

### Task 2: `store.recommend_snapshot` 表 + save/get_before

**Files:**
- Modify: `store.py`(SCHEMA + 两个函数)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `store.upsert_recommend_snapshot(db, signal_date, generated_at, close_date, prev_trading_date, stocks)` — `stocks` 为 `[{code, name, signal_close}]` 列表,内部 JSON 序列化存 `payload` 列;`signal_date` 为 PK,`ON CONFLICT` 覆盖。
  - `store.get_recommend_snapshot_before(db, signal_date)` — 返回 dict(含 `payload` 解析后的 `stocks` 列表)或 `None`。
- Consumes: 无(Task 1 无关)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_store.py` 追加:

```python
def test_recommend_snapshot_upsert_and_get_before(tmp_path):
    db = str(tmp_path / "reco_snap.db")
    store.init_db(db)
    stocks = [{"code": "sh600050", "name": "联通", "signal_close": 5.01}]
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 17:40:00",
                                    "2026-08-12", "2026-08-11", stocks)
    # 同日重建覆盖(PK 幂等)
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 18:00:00",
                                    "2026-08-12", "2026-08-11",
                                    [{"code": "sh600050", "name": "联通", "signal_close": 5.02}])
    # 盘中 08-13 生成 → get_before(08-13) 命中 08-12
    store.upsert_recommend_snapshot(db, "2026-08-13", "2026-08-13 09:30:00",
                                    "2026-08-12", "2026-08-12", [])
    row = store.get_recommend_snapshot_before(db, "2026-08-13")
    assert row is not None and row["signal_date"] == "2026-08-12"
    assert row["close_date"] == "2026-08-12"
    assert row["stocks"][0]["signal_close"] == 5.02     # 同日重建覆盖生效
    assert store.get_recommend_snapshot_before(db, "2026-08-12") is None   # 无更早
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_store.py::test_recommend_snapshot_upsert_and_get_before -v`
Expected: FAIL(no such table / function)

- [ ] **Step 3: 实现**

`store.py` 顶部加 `import json`。SCHEMA 追加:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS market_daily (...);
CREATE TABLE IF NOT EXISTS sector_daily (...);
CREATE TABLE IF NOT EXISTS recommend_snapshot (
  signal_date TEXT PRIMARY KEY,
  generated_at TEXT, close_date TEXT, prev_trading_date TEXT, payload TEXT
);
"""
```

末尾追加:

```python
def upsert_recommend_snapshot(db, signal_date, generated_at, close_date, prev_trading_date, stocks):
    conn = _connect(db)
    conn.execute(
        """INSERT INTO recommend_snapshot(signal_date, generated_at, close_date, prev_trading_date, payload)
           VALUES(?,?,?,?,?)
           ON CONFLICT(signal_date) DO UPDATE SET
             generated_at=excluded.generated_at, close_date=excluded.close_date,
             prev_trading_date=excluded.prev_trading_date, payload=excluded.payload""",
        (signal_date, generated_at, close_date, prev_trading_date,
         json.dumps(stocks, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_recommend_snapshot_before(db, signal_date):
    conn = _connect(db)
    cur = conn.execute(
        "SELECT * FROM recommend_snapshot WHERE signal_date < ? ORDER BY signal_date DESC LIMIT 1",
        (signal_date,))
    row = cur.fetchone()
    conn.close()
    d = _row_to_dict(row)
    if d and d.get("payload"):
        d["stocks"] = json.loads(d["payload"])
    return d
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_store.py -v`
Expected: PASS(含新测试)

- [ ] **Step 5: Commit**

```bash
git add store.py tests/test_store.py
git commit -m "feat(store): recommend_snapshot 表(快照 PK=signal_date)+ upsert/get_before"
```

---

### Task 3: `build_recommend` P1 出参 — signal_date/close_date/prev_trading_date/signal_close/trading_dates

**Files:**
- Modify: `recommend.py`(`_score_candidate`、`_score_sector_stocks`、`build_recommend`)
- Test: `tests/test_recommend.py`

**Interfaces:**
- Consumes: `an.is_trading_time` / `an.is_after_close`(analysis.py:31-39,已存在)。
- Produces:
  - `_score_candidate(row, daily_df, now, sector_composite=None)` 返回 dict 增加 `"signal_close"`(末根 close,round 2)、`"close_date"`(末根 date 字符串)。5 日涨幅 g5 **不在**本函数产出(Task 12 用 `_g5_of` 在 fan-out 独立收集,与打分成功解耦)。
  - `_score_sector_stocks(spot_rows, get_daily, now, per_sector, sector_composite=None)` 返回值从 `(ranked, daily_failed, any_stale)` 扩展为 `(ranked, daily_failed, any_stale, dates)`;`dates` = 全部拉取成功的 daily_df 的 date 字符串**并集**(set,含打分失败的股票——交易日历需完整)。
  - `build_recommend(...)` 返回 payload 增加 `signal_date` / `close_date` / `prev_trading_date` / `trading_dates`(最近 20 个交易日,供 Task 4 算 gap_days)。
  - `recommend._signal_date(now, close_date)` 辅助函数:工作日且 `(an.is_trading_time(now) or an.is_after_close(now))` → `now.date().isoformat()`,否则 `close_date`。

- [ ] **Step 1: 写失败测试 + 更新既有 3 元组解包测试**

**A. 新增两个测试**(复用 `make_summary`/`make_spot`/`make_daily`/`mock_sector`/`make_consolidated`,见文件顶部):

```python
def test_build_recommend_snapshot_fields(monkeypatch):
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual",
                                      "source_name": "电子信息"})
    daily_df = make_consolidated()                  # 65 根,本文件 make_daily 末根 date = "2026-07-09"
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (daily_df, False))
    now = datetime.datetime(2026, 8, 13, 10, 0)      # 2026-08-13 周四盘中 → signal_date=2026-08-13
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry", now, 1, 5)
    assert payload["signal_date"] == "2026-08-13"
    assert payload["close_date"] == str(daily_df.iloc[-1]["date"])          # 末根日线日期(勿硬编码,见 make_daily 生成规则)
    assert payload["prev_trading_date"] == payload["close_date"]            # 唯一交易日 → 最近交易日=末根
    assert payload["trading_dates"] and payload["trading_dates"][-1] < "2026-08-13"
    s = payload["sectors"][0]["stocks"][0]
    assert s["signal_close"] == pytest.approx(5.4)   # 末根 close = 5.4(make_consolidated)
    assert s["close_date"] == payload["close_date"]


def test_signal_date_fallback_non_trading(monkeypatch):
    # 周六 10:00 → 非交易日 → signal_date 回退 close_date(无得分股票 → 均为 None)
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")   # 板块入选,成分股空 → 无得分 → too_few 跳过
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": [], "match_type": "manual",
                                      "source_name": "电子信息"})
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 15, 10, 0), 1, 5)   # 2026-08-15 周六(weekday()=5)
    assert payload["sectors"] == []
    assert payload["signal_date"] is None and payload["close_date"] is None
    assert payload["signal_date"] == payload["close_date"]
```

**B. 更新既有 `test_score_sector_stocks_skips_short_history`**(lines 474-487,现解包 3 元组 → 本任务改为 4 元组返回,须解包 4 值并补 dates 断言):

```python
def test_score_sector_stocks_returns_dates(monkeypatch):
    # 原 test_score_sector_stocks_skips_short_history 更名为本函数:_score_sector_stocks 返回 4 元组(新增 dates)
    monkeypatch.setattr(an, "score_stock",
                        lambda df, q, now: {"position": None, "trend": None, "volume_price": None,
                                            "signal": None, "risk": None, "composite": None})
    rows = [{"code": "600050", "name": "X", "price": 5.0, "change_pct": 1.0,
             "volume": 100000, "amount": 2e8}]
    ranked, daily_failed, any_stale, dates = recommend._score_sector_stocks(
        rows, lambda c: (make_daily([10 + i for i in range(30)]), False),
        datetime.datetime(2026, 8, 11, 15, 0), per_sector=5, sector_composite=78.0)
    assert ranked == []                 # None 被守卫跳过 → 不崩 rank_candidates
    assert daily_failed == 0 and any_stale is False
    assert len(dates) == 30             # 日期并集照常收集(30 根)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_recommend.py::test_build_recommend_snapshot_fields tests/test_recommend.py::test_signal_date_fallback_non_trading tests/test_recommend.py::test_score_sector_stocks_returns_dates -v`
Expected: FAIL(`KeyError: 'signal_date'` / 4 元组解包 ValueError)

- [ ] **Step 3: 实现**

`_score_candidate` 出参加两个字段(在现有 return dict 中追加):

```python
    return {"code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
            "price": quote["price"], "change_pct": quote["change_pct"],
            "scores": scores, "composite": final,
            "verdict": an.stock_verdict(final),
            "signal_close": round(float(daily_df["close"].iloc[-1]), 2),
            "close_date": str(daily_df["date"].iloc[-1])}
```

`_score_sector_stocks` 收集并集日期并返回(改返回值与收集逻辑):

```python
def _score_sector_stocks(spot_rows, get_daily, now, per_sector, sector_composite=None):
    ranked, daily_failed, any_stale, dates = [], 0, False, set()
    if not spot_rows:
        return [], 0, False, set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily, str(r["code"])): r for r in spot_rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                any_stale = any_stale or bool(stale)
                if len(daily):
                    dates.update(str(x) for x in daily["date"])
                scored = _score_candidate(row, daily, now, sector_composite)
                if scored is not None:
                    ranked.append(scored)
            except Exception:
                daily_failed += 1
    return rank_candidates(ranked, per_sector), daily_failed, any_stale, dates
```

新增辅助函数与 `build_recommend` 聚合(在 `_score_sector_stocks` 之后):

```python
def _signal_date(now, close_date):
    """推荐生成交易日:工作日盘中/收盘后 → now.date();否则回退 close_date(开盘前/周末/节假日)。"""
    if now.weekday() < 5 and (an.is_trading_time(now) or an.is_after_close(now)):
        return now.date().isoformat()
    return close_date
```

`build_recommend` 三处修改(现有 lines 200-241):

**① 初始化**——`sectors, skipped = [], []`(line 204)改为同时收集 `all_dates` 与 `close_dates`:

```python
    sectors, skipped = [], []
    all_dates, close_dates = set(), []
```

**② 循环体内**——`ranked, daily_failed, any_stale = _score_sector_stocks(...)`(line 222)改为 4 元组解包,并在 `if not ranked: continue` **之前**累计(交易日历须含被 too_few 跳过板块的成分股):

```python
        ranked, daily_failed, any_stale, s_dates = _score_sector_stocks(
            kept, ds.get_stock_daily, now, per_sector, s["composite"])
        all_dates.update(s_dates)
        close_dates.extend(x["close_date"] for x in ranked)
        diagnostics["stocks_daily_failed"] += daily_failed
        stale_any = stale_any or any_stale
        if not ranked:
            ...
```

sector 的 stocks dict(line 234-237)增加 `signal_close`(close_date 不放在响应里,只用于聚合):

```python
            "stocks": [{"code": x["code"], "name": x["name"], "price": x["price"],
                        "change_pct": x["change_pct"], "scores": x["scores"],
                        "composite": round(x["composite"], 2),
                        "verdict": x["verdict"], "signal_close": x["signal_close"]} for x in ranked],
```

**③ 函数尾部**——字面量 return(line 239-241)改为先赋 payload 再补快照字段:

```python
    payload = {"strong_count": len(strong), "sectors": sectors,
               "skipped_sectors": skipped, "diagnostics": diagnostics}
    # P1 快照出参:两日期语义分离(§5.2)
    close_date = max(close_dates, key=close_dates.count) if close_dates else None   # 众数;停牌缺末根时取多数
    signal_date = _signal_date(now, close_date)
    dates_sorted = sorted(all_dates)
    prev_trading_date = max((d for d in dates_sorted if d < signal_date), default=None)
    payload["signal_date"] = signal_date
    payload["close_date"] = close_date
    payload["prev_trading_date"] = prev_trading_date
    payload["trading_dates"] = dates_sorted[-20:]    # 最近 20 个交易日,供 Task 4 算 gap_days
    return (payload, stale_any)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: PASS(含新测试;`test_signal_date_fallback_non_trading` 中 signal_date==close_date,但 close_date 可能为 None——`make_daily` 无得分股票时 close_dates 空 → close_date=None,断言 `signal_date == close_date` 即 None==None,通过)

- [ ] **Step 5: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "feat(recommend): build_recommend P1 出参(signal_date/close_date/prev_trading_date/trading_dates/signal_close)"
```

---

### Task 4: `api_recommend` 快照持久化 + `prev_snapshot` 合并

**Files:**
- Modify: `app.py:262-295`(`api_recommend`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 2 `store.upsert_recommend_snapshot` / `store.get_recommend_snapshot_before`;Task 3 `build_recommend` 出参(`signal_date`/`close_date`/`prev_trading_date`/`trading_dates`);Task 1 spot `open` 列。
- Produces: `api_recommend` 响应 `data` 增加 `prev_snapshot`(dict 或 None):
  ```json
  {"signal_date": "...", "close_date": "...", "is_next_day": true,
   "gap_days": 1, "stocks": [{"code": "...", "name": "...", "signal_close": 5.01,
                              "today_open": 4.9, "gap_pct": -2.2}]}
  ```
  `is_next_day` = `(prev.close_date == payload["prev_trading_date"])`;`gap_days` = `trading_dates` 中 `prev.close_date < d <= signal_date` 的计数;`gap_pct` = `(open/signal_close - 1) * 100` round 2;每股 spot 无 open 或不在今日 spot → 跳过该股。

- [ ] **Step 1: 写失败测试**

**A.** `tests/test_api.py` 顶部 `make_spot()`(lines 17-25)加 `open` 列(现有断言均不读 open,不破坏):

```python
def make_spot():
    return pd.DataFrame({
        "code": ["600000", "600519", "000001", "688981"],
        "name": ["浦发银行", "贵州茅台", "平安银行", "中芯国际"],
        "price": [10.0, 1348.9, 12.0, 45.0],
        "change_pct": [1.0, 0.3, -2.0, 5.0],
        "volume": [100000, 682720, 80000, 90000],
        "amount": [1e8, 9.2e8, 2e8, 5e8],
        "open": [9.8, 1348.0, 12.0, 45.0],
    })
```

**B.** 追加测试。复用 `client_factory`(lines 60-81,已 mock validate_sector_map/get_market_spot/get_stock_daily/resolve_sector_constituents/…;`an.is_trading_time` 与 `an.is_after_close` 已 mock 为 True → `signal_date` = 真实今日):

```python
def test_recommend_prev_snapshot(monkeypatch, tmp_path):
    db = str(tmp_path / "reco_snap_api.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    # 第一次:无上一期 → prev_snapshot None
    r1 = c.get("/api/recommend").get_json()["data"]
    assert r1["prev_snapshot"] is None
    # 手动写入上一期快照(close_date = 上一期响应 close_date → 与当前 prev_trading_date 相等 → is_next_day)
    prev_close = r1["close_date"]
    store.upsert_recommend_snapshot(db, "2026-08-12", "2026-08-12 17:40:00",
                                    prev_close, r1["prev_trading_date"],
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps is not None and ps["signal_date"] == "2026-08-12"
    assert ps["is_next_day"] is True                       # prev.close_date == 当前 prev_trading_date
    assert len(ps["stocks"]) == 1
    s = ps["stocks"][0]
    assert s["code"] == "600000" and s["signal_close"] == 10.0
    assert s["today_open"] == pytest.approx(9.8)           # make_spot open 列 600000 → 9.8
    assert abs(s["gap_pct"] - (-2.0)) < 0.01               # (9.8/10.0 - 1)*100
```

注:依赖真实 `datetime.now()` ≥ 2026-05-09(本文件 `make_daily` 末根日期),`signal_date`(真实今日)> 全部日线日期 → `prev_trading_date` = 末根日期。若将来时钟早于该日,断言会因 `prev_trading_date` 为 None 而失败,需同步 fixture 日期。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_api.py::test_recommend_prev_snapshot -v`
Expected: FAIL(无 `prev_snapshot` 字段)

- [ ] **Step 3: 实现**

`api_recommend` 在 `payload, stale_cands = recommend.build_recommend(...)` 之后、构造 `coverage`/`ok(...)` 之前插入(复用作用域内 `spot`、`db_path`、`now`):

```python
        # P1 快照:持久化本期,出参上一期对比(§5.2)
        prev_snapshot = None
        try:
            prev = store.get_recommend_snapshot_before(db_path, payload["signal_date"])
            store.upsert_recommend_snapshot(
                db_path, payload["signal_date"], now.strftime("%Y-%m-%d %H:%M:%S"),
                payload["close_date"], payload["prev_trading_date"],
                [{"code": x["code"], "name": x["name"], "signal_close": x["signal_close"]}
                 for sec in payload["sectors"] for x in sec["stocks"]])
            if prev:
                is_next_day = (prev["close_date"] == payload["prev_trading_date"])
                td = payload.get("trading_dates") or []
                gap_days = sum(1 for d in td if prev["close_date"] < d <= payload["signal_date"])
                spot_open = {}
                for _, r in spot.iterrows():
                    code = str(r["code"])
                    o = r.get("open")
                    if o is not None and o == o:      # 非 NaN
                        spot_open[code] = float(o)
                stocks = []
                for s in prev.get("stocks", []):
                    code6 = s.get("code", "")
                    if len(code6) >= 2 and code6[:2] in ("sh", "sz"):
                        code6 = code6[2:]
                    o = spot_open.get(code6)
                    sc = s.get("signal_close")
                    if o is None or sc is None or not sc:
                        continue
                    stocks.append({"code": s["code"], "name": s.get("name"),
                                   "signal_close": sc, "today_open": o,
                                   "gap_pct": round((o / sc - 1.0) * 100.0, 2)})
                prev_snapshot = {"signal_date": prev["signal_date"],
                                 "close_date": prev["close_date"],
                                 "is_next_day": is_next_day, "gap_days": gap_days,
                                 "stocks": stocks}
        except Exception:
            prev_snapshot = None          # 快照失败不拖垮推荐
```

响应加入 `"prev_snapshot": prev_snapshot`:

```python
        return ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sectors": payload["sectors"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
            "signal_date": payload["signal_date"],
            "close_date": payload["close_date"],
            "prev_snapshot": prev_snapshot,
        }, stale=stale1 or stale2 or stale_cands, ...)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS(含新测试;现有 `test_recommend_endpoint` 的 `make_spot` 加 open 列不影响其断言)

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "feat(api): /api/recommend 快照持久化 + prev_snapshot 上一期低开对比"
```

---

### Task 5: 前端 P1 — 免责声明 + 昨日信号对比块 + gap 警示 chip

**Files:**
- Modify: `templates/index.html`(在 `#reco-disclaimer` 后加容器)、`static/app.js`(`renderRecommend`)、`static/style.css`
- Test: 手动验证(无 JS 单测基建);`python -m pytest tests/ -q` 回归不破坏。

**Interfaces:**
- Consumes: Task 4 响应 `data.prev_snapshot` / `data.close_date` / `data.generated_at`。
- Produces: 前端渲染「昨日信号对比块」+ 静态免责 + 每股警示 chip。

- [ ] **Step 1: 加容器**

`templates/index.html:44` `#reco-disclaimer` 之后加:

```html
      <div id="reco-disclaimer" class="muted">仅供研究参考,不构成投资建议。</div>
      <div id="reco-exec" class="muted"></div>
```

- [ ] **Step 2: 加 CSS**

`static/style.css` 追加:

```css
#reco-exec { margin-bottom: 8px; }
#reco-exec .gap-warn { color: var(--up); font-weight: 700; }
tr.gap-warn-row td { background: color-mix(in srgb, var(--up) 8%, transparent); }
```

- [ ] **Step 3: 改 JS 渲染**

`static/app.js` 顶部加常量(模块级,`GAP_WARN_PCT` 注释标注 provisional):

```js
const GAP_WARN_PCT = -1.5;  // provisional, P1 探针定稿(见 spec §5.4)
```

`renderRecommend(b)` 内,在 `$("#reco-coverage").innerHTML = ...` 之后加免责更新 + 昨日信号块:

```js
  // 静态免责(数字为旧策略历史回测,spec §5.3)
  $("#reco-disclaimer").innerHTML =
    `信号基于 ${d.close_date || "…"} 收盘;基于旧策略的历史回测(2025-08~2026-08):` +
    `历史 209 个信号日次日开盘 66.5% 低开、均值 −0.21%。次日开盘执行,勿挂昨日收盘价。` +
    `<span class="muted">(仅供研究参考)</span>`;
  // 昨日信号对比块(spec §5.2/§5.3)
  const ps = d.prev_snapshot;
  if (ps && ps.stocks && ps.stocks.length) {
    const rows = ps.stocks.map((s) => {
      const g = s.gap_pct;
      const warn = g != null && g <= GAP_WARN_PCT;
      const label = ps.is_next_day ? "次日" : (ps.gap_days != null ? `隔 ${ps.gap_days} 个交易日` : "非相邻交易日");
      const gapTxt = g == null ? "—" : `${g >= 0 ? "+" : ""}${g.toFixed(2)}%`;
      return `<tr class="${warn ? "gap-warn-row" : ""}">
        <td>${s.code}</td><td>${s.name}</td>
        <td>${s.signal_close == null ? "—" : s.signal_close.toFixed(2)}</td>
        <td>${s.today_open == null ? "—" : s.today_open.toFixed(2)}</td>
        <td class="${g != null && g < 0 ? "down" : ""}">${gapTxt}${warn ? ' <span class="gap-warn">信号撤回/谨慎</span>' : ""}</td>
        <td class="muted">${label}</td>
      </tr>`;
    }).join("");
    $("#reco-exec").innerHTML = `<b class="muted">昨日信号(基于 ${ps.close_date} 收盘, 对比今日开盘):</b>
      <table class="reco-table"><thead><tr><th>代码</th><th>名称</th><th>信号收盘</th><th>今日开盘</th><th>开盘差</th><th>校验</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } else {
    $("#reco-exec").innerHTML = "";
  }
```

- [ ] **Step 4: 手动验证**

启动服务器,浏览器开推荐页:
- 首次(无快照)→ 无昨日块;
- 手动向 `recommend_snapshot` 插入上一期行 → 昨日块渲染,`gap_pct ≤ -1.5` 的股票显示红色「信号撤回/谨慎」chip;
- `is_next_day=false`(prev.close_date != 当前 prev_trading_date)→ 标「隔 N 个交易日」。

- [ ] **Step 5: 回归 + Commit**

```bash
python -m pytest tests/ -q
git add templates/index.html static/app.js static/style.css
git commit -m "feat(web): 推荐页昨日信号对比块 + 静态免责 + 次日低开警示 chip"
```

---

### Task 6: 探针底座 — `nxday_backtest` 多年窗口 + 按年切分 + 决策记录

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored,不 commit)
- Create: `_analysis/decisions.md`(决策记录,后续探针任务都追加)
- Test: 无(探针脚本 gitignored 不 CI)

**Interfaces:**
- Produces: `_analysis/decisions.md` 建立骨架(标题 + 探针结果占位表);`nxday_backtest.py` 支持多年窗口与按年切分。

- [ ] **Step 1: 扩展窗口与切分**

`nxday_backtest.py`:
- `raw` 构建处:`if len(d) < 260: continue` → `if len(d) < 1200: continue`;`d = d.reset_index(drop=True).tail(260).copy()` → `.tail(1200)`。
- 主循环 `for i in range(start, len(all_days) - 1)` → 加**评估日采样**维持 ~230-300 天:`step = max(1, (len(all_days) - 2 - start) // 300)`;`for i in range(start, len(all_days) - 1, step)`。
- `n_eval` 与所有列表循环同步走 step。
- **按年切分报告**:`summ()` 之外加 `summ_year(arr, years)`,按 `all_days[i][:4]` 把每个篮子的 od 分年输出。

- [ ] **Step 2: 建立决策记录**

创建 `_analysis/decisions.md`:

```markdown
# 探针决策记录(热板块动量执行计划)
所有 selection-affecting 机制的最终数值由本文件决定,生产代码消费。

## 待办探针
- [ ] P0b(§12):5/10/20 日分位热板块 od 边际 + 三配置 a/b/c → 权重定稿
- [ ] P0(§12):过热 vs 新鲜热 → 路径 A/B + N
- [ ] P1(§12):gap 分布分位 → 警示阈值(现 -1.5 provisional)
- [ ] P3(§12):加成×质量 → 门槛机制
- [ ] P4(§12):价格分桶 → 护栏形态
- [ ] P2(§12):od 全量重校准 + 热子样本可介入率 → verdict/bonus 重锚
```

- [ ] **Step 3: 运行基线(多年窗口 + 按年切分)**

Run: `cd _analysis && python nxday_backtest.py`
Expected: 输出多年窗口(约 5 年采样 ~300 评估日)的 A/C/D/E 篮子 od + 按年拆分。记录结果到 decisions.md(「P0b 前置基线」小节)。

- [ ] **Step 4: 收尾**

`_analysis/` gitignored,无 commit。在 `decisions.md` 勾掉基线项。报告:基线各篮子多年/按年 od 是否延续 1 年窗口结论(动量 > 滞涨、热 > 市场)。

---

### Task 7: P0b 探针 — 5/10/20 日分位热板块 od 边际 + 三配置对比

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored)
- Create: `_analysis/decisions.md` 追加 P0b 决策
- Test: 无(gitignored)

**Interfaces:**
- Consumes: Task 6 多年窗口底座。
- Produces: `decisions.md` 写入 `HOT_WEIGHT_MODE`("rel_strength"/"signal"/"off")与 `HOT_COMPOSITE_THRESHOLD` 定稿值。

- [ ] **Step 1: 加 P0b 篮子**

在 `nxday_backtest.py` 主循环加三个配置篮子(与现有 A 篮子同源,按热板块 top-3、板块内 top5):

- 配置 (a):现有 A 篮子(当前 v9 composite)已够。
- 配置 (b):对热板块成分股,`final_b = 0.40*position + 0.15*vp + 0.10*trend + 0.35*signal + bonus`,取 top5,记 od。
- 配置 (c):对热板块成分股,算个股 5 日涨幅在该板块成分股集合(≥6 根)的分位(`rank(pct=True, method="average")`),`final_c = 0.40*position + 0.15*vp + 0.10*trend + 0.20*signal + 0.15*rel + bonus`,取 top5,记 od。

沿用现有 `score_at`/`buyable`/`sector_heat` 结构;为配置 (c) 需要板块内**完整** gains 列表(非仅 median)——把 `sector_heat` 计算处同时存 `sector_gains[s] = sorted(gains)`。

- [ ] **Step 2: 输出分年对比**

主循环后汇总:三配置的 od mean/win/n,**按年拆分行**。追加打印与 `nxday_results.json` 并存 `nxday_p0b_results.json`。

- [ ] **Step 3: 运行 + 决策**

Run: `cd _analysis && python nxday_backtest.py`(约小时级,可后台)

在 `decisions.md` 追加 P0b 决策小节,记录:
- 三配置多年 od mean/win;按年是否一致(跨年稳定性);
- 配置 (c) vs (b) 的增量(c 显著优于 b 且跨年稳定 → `HOT_WEIGHT_MODE="rel_strength"`;否则 b 胜 → `"signal"`;两者都不如 a → `"off"` 关闭 P0b);
- 若 (c) 胜,记录 5/10/20 日中哪个窗口的分位最稳,并给出 `HOT_COMPOSITE_THRESHOLD` 是否维持 68 的依据。

勾掉 decisions.md 待办 P0b。

- [ ] **Step 4: 收尾**

报告决策结果(权重模式 + 阈值),供 Task 12 消费。

---

### Task 8: P0 探针 — 过热 vs 新鲜热次日 od → 路径 + N

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored)
- Modify: `_analysis/decisions.md`
- Test: 无(gitignored)

**Interfaces:**
- Produces: `decisions.md` 写入 `P0_PATH`("intercept"/"badge")与 `OVERHEAT_MIN_DAYS`(N)。

- [ ] **Step 1: 加过热切分**

板块热度代理已有(median 5-day constituent gain)。加**连涨天数代理**:对每个热板块,统计连续「median 5d gain > 0」的交易日数(或直接用 `sector_heat` 历史连续为正的天数)。把热板块分为:
- 过热组:连续为正 ≥ N(测 N ∈ {3, 4, 5});
- 新鲜组:连续为正 < N。

对两组分别计算**次日 od**(板块内随机篮子 C 的口径),多年窗口 + 按年。

- [ ] **Step 2: 运行 + 决策**

Run: `cd _analysis && python nxday_backtest.py --probe p0`

在 `decisions.md` 追加 P0 小节:
- 过热组 vs 新鲜组次日 od(按 N=3/4/5、按年);
- 过热组显著跑输且跨年稳定 → `P0_PATH="intercept"`(翻转标签拦截),`OVERHEAT_MIN_DAYS=N`;否则 → `"badge"`(仅徽章)。

勾掉待办 P0。

- [ ] **Step 3: 收尾**

报告决策,供 Task 13 消费。

---

### Task 9: P1 探针 — gap 分布分位 → 警示阈值

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored)
- Modify: `_analysis/decisions.md`
- Test: 无(gitignored)

**Interfaces:**
- Produces: `decisions.md` 写入 `GAP_WARN_PCT` 定稿值(若与 provisional −1.5 不同,Task 17 统一落地常量)。

- [ ] **Step 1: 加 gap 分布**

A 篮子已有 `A_gap` 列表(隔夜 gap)。加统计:`np.percentile(A_gap, [10, 20, 25])`、均值、P20、2σ 下界。

- [ ] **Step 2: 运行 + 决策**

Run: `cd _analysis && python nxday_backtest.py --probe p1`

`decisions.md` 追加 P1 小节:记录 gap 分布分位;建议阈值 = max(P20, 均值−1σ)(或 P20,取直观稳健值);勾掉待办 P1。

- [ ] **Step 3: 收尾**

报告阈值;Task 17 若与前端 provisional 不同则更新 `app.js` 的 `GAP_WARN_PCT`。

---

### Task 10: P3 探针 — 板块加成 × 个股质量交互

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored)
- Modify: `_analysis/decisions.md`
- Test: 无(gitignored)

**Interfaces:**
- Produces: `decisions.md` 写入 `BONUS_GE75`(bool,是否 +8 门槛提到 ≥75)与 `BONUS_QUALITY_GATE`(bool,是否 quality<50 不给加成)。

- [ ] **Step 1: 加交互分桶**

对吃加成(composite≥60)的样本,按「板块 composite 分桶(≥75 / 68-75 / 60-68)× 个股 quality(<50 / ≥50)」分组,测各格次日 od(多年窗口 + 按年)。

- [ ] **Step 2: 运行 + 决策**

Run: `cd _analysis && python nxday_backtest.py --probe p3`

`decisions.md` 追加 P3 小节:各格 od;若 alpha 集中在 ≥75 格 → `BONUS_GE75=True`;若 quality<50 格无正 od → `BONUS_QUALITY_GATE=True`;勾掉待办 P3。

- [ ] **Step 3: 收尾**

报告决策,供 Task 14 消费。

---

### Task 11: P4 探针 — 价格分桶条件收益 → 护栏形态

**Files:**
- Modify: `_analysis/nxday_backtest.py`(gitignored)
- Modify: `_analysis/decisions.md`
- Test: 无(gitignored)

**Interfaces:**
- Produces: `decisions.md` 写入 `PRICE_FLOOR`(None 或 3.0 等)与 `PRICE_REL_MIN`(bool,低价股需更高 rel_strength 分位)。

- [ ] **Step 1: 加价格分桶**

候选集内按股价分桶(<3 / 3-5 / 5-10 / ≥10),测各桶次日 od;再在控制 momentum(position+rel_strength)后看低价是否有**独立**负效应(分桶 × rel 高/低)。

- [ ] **Step 2: 运行 + 决策**

Run: `cd _analysis && python nxday_backtest.py --probe p4`

`decisions.md` 追加 P4 小节:各桶 od;低价桶显著跑输 → `PRICE_FLOOR=3.0`(排除),或 `PRICE_REL_MIN=True` 并记录具体分位阈值 `PRICE_REL_MIN_REL=N`(低价股 rel_strength 低于 N 分位即剔除);无独立负效应 → `PRICE_FLOOR=None`。勾掉待办 P4。

- [ ] **Step 3: 收尾**

报告决策,供 Task 15 消费。

---

### Task 12: P0b 生产 — `stock_composite_v3` rel_strength + `_score_sector_stocks` 两遍聚合

**Files:**
- Modify: `analysis.py`(`stock_composite_v3`,496-501)
- Modify: `recommend.py`(`_score_sector_stocks`、`_score_candidate` 已有 `_g5`、新常量)
- Test: `tests/test_analysis_stock.py`、`tests/test_recommend.py`

**Interfaces:**
- Consumes: Task 7 决策(`_analysis/decisions.md` 的 `HOT_WEIGHT_MODE`/`HOT_COMPOSITE_THRESHOLD`);Task 3 `_score_candidate` 已输出 `_g5`。
- Produces:
  - `an.stock_composite_v3(position, vp, trend, signal, risk, sector_bonus=0, rel_strength=None, weights=None)`。
  - `recommend.HOT_COMPOSITE_THRESHOLD`、`recommend.HOT_WEIGHT_MODE`(从 decisions.md 读,探针未定则用 spec provisional)。
  - `_score_sector_stocks` 返回 `(ranked, daily_failed, any_stale, dates)` 不变,但热板块内重新计算 final/verdict(含 rel_strength)。

- [ ] **Step 1: 写失败测试(weights 常量)**

`tests/test_analysis_stock.py` 追加:

```python
def test_stock_composite_v3_weights():
    import analysis as an
    # v3 默认权重 55/15/10/20
    assert an.stock_composite_v3(100, 0, 0, 0, 0) == 55.0
    # 热 rel(配置 c):rel_strength 非 None 且未给 weights → HOT_REL_WEIGHTS + rel×0.15
    assert an.stock_composite_v3(100, 0, 0, 0, 0, rel_strength=100.0) == 55.0        # 0.40*100 + 0.15*100
    assert an.stock_composite_v3(100, 0, 0, 100, 0, rel_strength=0.0) == 60.0        # 0.40*100 + 0.20*100 + 0.15*0
    # 热 signal(配置 b):weights 显式覆盖(HOT_SIGNAL_WEIGHTS)
    assert an.stock_composite_v3(100, 0, 0, 100, 0, weights=(0.40, 0.15, 0.10, 0.35)) == 75.0
    # 风险折扣保留
    assert an.stock_composite_v3(50, 50, 50, 50, 100) == 0.0
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_analysis_stock.py::test_stock_composite_v3_weights -v`
Expected: FAIL(签名不支持 rel_strength/weights)

- [ ] **Step 3: 实现 `analysis.py`**

在 `stock_composite_v3` 处(496)加常量与改造:

```python
V3_WEIGHTS = (0.55, 0.15, 0.10, 0.20)          # (position, vp, trend, signal)
HOT_REL_WEIGHTS = (0.40, 0.15, 0.10, 0.20)     # 配置 c: + rel_strength×0.15
HOT_SIGNAL_WEIGHTS = (0.40, 0.15, 0.10, 0.35)  # 配置 b


def stock_composite_v3(position, vp, trend, signal, risk, sector_bonus=0,
                       rel_strength=None, weights=None):
    """综合分(规格 §4.6):质量 = Σ(weights×因子) + (rel_strength×0.15 若非 None),
    再加板块加成,× 风险折扣。weights 缺省:rel_strength 非 None → HOT_REL_WEIGHTS(配置 c),
    否则 V3_WEIGHTS;显式 weights(配置 b 等)覆盖。未舍入。"""
    if None in (position, vp, trend, signal, risk):
        return None
    if weights is None:
        weights = HOT_REL_WEIGHTS if rel_strength is not None else V3_WEIGHTS
    quality = weights[0] * position + weights[1] * vp + weights[2] * trend + weights[3] * signal
    if rel_strength is not None:
        quality += 0.15 * rel_strength
    return (quality + sector_bonus) * max(0.0, 1.0 - risk / 100.0)
```

- [ ] **Step 4: 写失败测试(两遍聚合)**

`tests/test_recommend.py` 追加(复用 `mock_sector`/`make_daily`/`make_spot`):

```python
def test_hot_sector_rel_strength_applied(monkeypatch):
    # composite=78(热)→ 热谓词命中 → 板块内分位计算并套 rel_strength 权重(配置 c)
    mock_sector(monkeypatch, composite=78.0, verdict="建议关注")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "688981", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10.0 + 0.2 * i for i in range(65)]), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    assert payload["sectors"], "热板块应产生推荐"
    for s in payload["sectors"][0]["stocks"]:
        assert s["rel_strength"] is not None and 0 <= s["rel_strength"] <= 100   # 3 成分股 → 分位计算
        assert s["signal_close"] == pytest.approx(22.8)   # 末根 close = 10 + 0.2*64


def test_non_hot_sector_no_rel_strength(monkeypatch):
    # composite=60(非热)→ 谓词未命中 → 不套 rel_strength,保持 v3 权重
    mock_sector(monkeypatch, composite=60.0, verdict="跟踪(热点延续)")
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "688981", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10.0 + 0.2 * i for i in range(65)]), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 13, 10, 0), 1, 5)
    assert payload["sectors"]
    s = payload["sectors"][0]["stocks"][0]
    assert "rel_strength" not in s or s["rel_strength"] is None


def test_rel_strengths_ties_and_small_base():
    # 并列分位 = 均值秩(spec §3.3):3 等值 → rank 2.0 → pct 2/3 → 66.67
    out = recommend._rel_strengths([("a", 0.01), ("b", 0.01), ("c", 0.01)])
    assert len(out) == 3 and all(abs(v - 66.67) < 0.01 for v in out.values())
    # 升序 → 33.33/66.67/100
    out2 = recommend._rel_strengths([("a", 0.01), ("b", 0.03), ("c", 0.05)])
    assert abs(out2["a"] - 33.33) < 0.01 and abs(out2["c"] - 100.0) < 0.01
    # 基数 <3 → 空(spec §3.3 回退 v3)
    assert recommend._rel_strengths([]) == {}
    assert recommend._rel_strengths([("a", 0.01)]) == {}
    assert recommend._rel_strengths([("a", 0.01), ("b", 0.02)]) == {}
```

若 Task 7 决策为配置 (b)(`HOT_WEIGHT_MODE="signal"`),把 `test_hot_sector_rel_strength_applied` 的 `s["rel_strength"]` 断言改为 `s["rel_strength"] is None`,并按 HOT_SIGNAL_WEIGHTS 重算 composite 验证。

- [ ] **Step 5: 实现 `recommend.py`**

顶部加常量(provisional;Task 7 探针定稿后由执行者把 `_analysis/decisions.md` 的值抄回来,不改运行时读取):

```python
HOT_COMPOSITE_THRESHOLD = 68.0      # 热板块谓词(spec §3.1);探针可调
HOT_WEIGHT_MODE = "rel_strength"    # P0b 权重模式:"rel_strength"(c)/"signal"(b)/"off";decisions.md 定稿
```

`_score_sector_stocks` 改为两遍(热板块内重算 final/verdict)。fan-out 阶段收集全部 `_g5`:

```python
def _g5_of(daily_df):
    if daily_df is None or len(daily_df) < 6:
        return None
    try:
        return float(daily_df["close"].iloc[-1]) / float(daily_df["close"].iloc[-6]) - 1.0
    except Exception:
        return None
```

`_score_sector_stocks` 主体(在现有 fan-out 内收集 base_g5,循环后重算):

```python
def _score_sector_stocks(spot_rows, get_daily, now, per_sector, sector_composite=None):
    ranked, daily_failed, any_stale, dates = [], 0, False, set()
    base_g5 = []          # (code, g5) — 分位基数 = 过滤后成分股 + ≥6根(spec §3.3)
    if not spot_rows:
        return [], 0, False, set()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily, str(r["code"])): r for r in spot_rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                any_stale = any_stale or bool(stale)
                if len(daily):
                    dates.update(str(x) for x in daily["date"])
                g5 = _g5_of(daily)
                if g5 is not None:
                    base_g5.append((str(row["code"]), g5))
                scored = _score_candidate(row, daily, now, sector_composite)
                if scored is not None:
                    ranked.append(scored)
            except Exception:
                daily_failed += 1
    ranked = _apply_hot_weights(ranked, base_g5, sector_composite)
    return rank_candidates(ranked, per_sector), daily_failed, any_stale, dates
```

新增 `_apply_hot_weights`:

```python
def _rel_strengths(base_g5):
    """板块内 5 日涨幅分位(spec §3.3):均值秩 ×100。base_g5=[(code, g5), ...];基数<3 → {}。"""
    if len(base_g5) < 3:
        return {}
    import pandas as pd
    pcts = pd.Series([g for _, g in base_g5]).rank(pct=True, method="average") * 100.0
    return {code: float(p) for (code, _), p in zip(base_g5, pcts)}


def _apply_hot_weights(ranked, base_g5, sector_composite):
    """热板块内 P0b 权重重算(spec §3.2/§3.3)。non-hot 或模式 off → 原样返回。"""
    hot = sector_composite is not None and sector_composite >= HOT_COMPOSITE_THRESHOLD
    if not hot or HOT_WEIGHT_MODE == "off":
        return ranked
    rel_map = _rel_strengths(base_g5) if HOT_WEIGHT_MODE == "rel_strength" else {}
    for x in ranked:
        rel = rel_map.get(x["code"][-6:]) if rel_map else None   # x["code"] 带 sh/sz 前缀 → 截 6 位
        if rel is not None:
            w = an.HOT_REL_WEIGHTS
        elif HOT_WEIGHT_MODE == "signal":
            w = an.HOT_SIGNAL_WEIGHTS
        else:
            w = None                             # rel 模式 + 基数<3 → 回退 v3(spec §3.3)
        scores = x["scores"]
        bonus = sector_bonus(sector_composite)   # Task 14 起改传 scores["composite"] 作 quality
        final = an.stock_composite_v3(scores["position"], scores["volume_price"],
                                      scores["trend"], scores["signal"], scores["risk"],
                                      bonus, rel_strength=rel, weights=w)
        x["composite"] = final
        x["verdict"] = an.stock_verdict(final)
        if rel is not None:
            x["rel_strength"] = round(rel, 2)
    return ranked
```

注意:`rank_candidates`(recommend.py:80-84)读 `x["scores"]["risk"]` 与 `x["composite"]`,`_apply_hot_weights` 在 rank 前调用(在 `_score_sector_stocks` 中),所以 rank 看到的是重算后的 composite。`_score_candidate` 仍产出 v3 composite(供 non-hot / 详情页 / actionable 使用)。

`build_recommend` 的股票出参 dict(Task 3 建的 sectors[i]["stocks"],line 361-364)加 `rel_strength`(非热板块 x 无此键 → None),供前端展示与 `test_hot_sector_rel_strength_applied` 断言。Task 12 实现时把该 dict(完整替换)改为:

```python
            "stocks": [{"code": x["code"], "name": x["name"], "price": x["price"],
                        "change_pct": x["change_pct"], "scores": x["scores"],
                        "composite": round(x["composite"], 2),
                        "verdict": x["verdict"], "signal_close": x["signal_close"],
                        "rel_strength": x.get("rel_strength")} for x in ranked],
```

- [ ] **Step 6: 运行确认通过**

Run: `python -m pytest tests/test_analysis_stock.py tests/test_recommend.py -v`
Expected: PASS(含新测试)

- [ ] **Step 7: 回归 + Commit**

```bash
python -m pytest tests/ -q
git add analysis.py recommend.py tests/test_analysis_stock.py tests/test_recommend.py
git commit -m "feat(scoring): P0b 热板块动量权重(rel_strength 分位 + 热谓词 ≥68)"
```

---

### Task 13: P0 生产 — `score_sector` 结构化 `overheated` + 路径 A 独立标签

**Files:**
- Modify: `analysis.py`(`score_sector` 188-198、`sector_verdict` 160-176)
- Test: `tests/test_analysis_sector.py`

**Interfaces:**
- Consumes: Task 8 决策(`P0_PATH`/`OVERHEAT_MIN_DAYS` 从 `_analysis/decisions.md`)。
- Produces:
  - `score_sector` 返回增加 `"overheated": bool`。
  - 路径 A:verdict 返回独立标签 `"过热(连涨)"`(不在 `QUALIFYING_VERDICTS` → 推荐页排除);路径 B:verdict 不变但 `overheated=True` 供前端徽章。
  - `recommend.QUALIFYING_VERDICTS` 不含「过热(连涨)」,无需改。

- [ ] **Step 1: 写失败测试**

`tests/test_analysis_sector.py` 追加:

```python
def test_sector_verdict_overheat_distinct_label(monkeypatch):
    import analysis as an
    # 连涨≥4 + e_hi + s_hi + not r_hi → overheated;路径 A → 独立标签
    monkeypatch.setattr(an, "OVERHEAT_MIN_DAYS", 4)
    monkeypatch.setattr(an, "P0_PATH", "intercept")
    out = an.score_sector({
        "up_ratio": 0.8, "limit_ratio": None, "turnover_ratio": 1.0,
        "leader_change_pct": 5.0, "change_pct": 2.0, "consecutive_days": 4,
        "change_3d": 6.0, "prev_change": 2.0, "activity": 0.05, "data_complete": True})
    assert out["overheated"] is True
    assert out["verdict"] == "过热(连涨)"
    assert out["verdict"] != "谨慎追高(过热)"      # 不与风险驱动标签撞名
    assert out["composite"] == an.composite_score(out["strength"], out["emotion"], out["risk"])  # composite 未变


def test_sector_verdict_overheat_badge_path(monkeypatch):
    import analysis as an
    monkeypatch.setattr(an, "OVERHEAT_MIN_DAYS", 4)
    monkeypatch.setattr(an, "P0_PATH", "badge")
    out = an.score_sector({
        "up_ratio": 0.8, "limit_ratio": None, "turnover_ratio": 1.0,
        "leader_change_pct": 5.0, "change_pct": 2.0, "consecutive_days": 4,
        "change_3d": 6.0, "prev_change": 2.0, "activity": 0.05, "data_complete": True})
    assert out["overheated"] is True
    assert out["verdict"] == "建议关注"           # 路径 B 保留推荐,徽章在前端
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_analysis_sector.py::test_sector_verdict_overheat_distinct_label tests/test_analysis_sector.py::test_sector_verdict_overheat_badge_path -v`
Expected: FAIL(无 `overheated`/`OVERHEAT_MIN_DAYS`)

- [ ] **Step 3: 实现**

`analysis.py` 顶部加 P0 常量:

```python
OVERHEAT_MIN_DAYS = 4          # 过热判定:连续天数阈值(spec §4.1);decisions.md 定稿
P0_PATH = "intercept"          # "intercept"(路径 A,排除)/ "badge"(路径 B,仅徽章);decisions.md 定稿
```

`score_sector`(188-198)加检测与展示逻辑:

```python
def score_sector(metrics):
    emotion = sector_emotion(metrics["up_ratio"], metrics["limit_ratio"],
                             metrics["turnover_ratio"], metrics["leader_change_pct"])
    strength = sector_strength(metrics["change_pct"], metrics["consecutive_days"],
                               metrics["activity"])
    risk = sector_risk(metrics["change_pct"], metrics["change_3d"],
                       metrics["turnover_ratio"], metrics["prev_change"], metrics["up_ratio"])
    composite = composite_score(strength, emotion, risk)
    verdict = sector_verdict(emotion, strength, risk, metrics["consecutive_days"],
                             metrics["data_complete"])
    e_hi = emotion is not None and emotion >= 70
    s_hi = strength is not None and strength >= 60
    r_hi = risk is not None and risk >= 65
    cd = metrics.get("consecutive_days")
    overheated = (cd is not None and cd >= OVERHEAT_MIN_DAYS and e_hi and s_hi and not r_hi
                  and metrics.get("data_complete", True))
    if overheated and P0_PATH == "intercept":
        verdict = "过热(连涨)"        # 独立标签,不在 QUALIFYING_VERDICTS → 排除(路径 A)
    return {"emotion": emotion, "strength": strength, "risk": risk,
            "composite": composite, "verdict": verdict, "overheated": overheated}
```

- [ ] **Step 4: 回归确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS(含新测试;既有 sector 测试若断言返回 dict 精确键集需检查——`score_sector` 返回增加了 `overheated` 键,若测试用 `==` 比较整个 dict 会失败,需更新该断言为逐字段)

- [ ] **Step 5: Commit**

```bash
git add analysis.py tests/test_analysis_sector.py
git commit -m "feat(scoring): P0 过热检测(结构化 overheated + 路径 A 独立标签)"
```

---

### Task 14: P3 生产 — `sector_bonus` 门槛(gate 决策)

**Files:**
- Modify: `recommend.py`(`sector_bonus` 167-183)
- Test: `tests/test_recommend.py`

**Interfaces:**
- Consumes: Task 10 决策(`BONUS_GE75`/`BONUS_QUALITY_GATE` 从 `_analysis/decisions.md`)。
- Produces: `sector_bonus(sector_composite, quality=None)` 可选 `quality` 参数,按决策 gate;`_score_candidate` 传 `quality`。

- [ ] **Step 1: 写失败测试**

`tests/test_recommend.py` 追加(默认决策 = 探针未跑 → 保持 spec provisional,即 `BONUS_GE75=False` 时不 gate):

```python
def test_sector_bonus_quality_gate(monkeypatch):
    # 决策:quality<50 不给加成
    monkeypatch.setattr(recommend, "BONUS_QUALITY_GATE", True)
    monkeypatch.setattr(recommend, "BONUS_GE75", False)
    assert recommend.sector_bonus(70.0, quality=40) == 0        # quality<50 → 无加成
    assert recommend.sector_bonus(70.0, quality=60) == 8        # ≥68 档
    assert recommend.sector_bonus(65.0, quality=60) == 4
    assert recommend.sector_bonus(70.0, quality=None) == 8      # quality 缺失 → 不 gate


def test_sector_bonus_ge75(monkeypatch):
    monkeypatch.setattr(recommend, "BONUS_GE75", True)
    monkeypatch.setattr(recommend, "BONUS_QUALITY_GATE", False)
    assert recommend.sector_bonus(70.0) == 4                    # <75 → 降档
    assert recommend.sector_bonus(76.0) == 8
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_recommend.py::test_sector_bonus_quality_gate tests/test_recommend.py::test_sector_bonus_ge75 -v`
Expected: FAIL(`sector_bonus` 不接受 `quality`)

- [ ] **Step 3: 实现**

`recommend.py` 顶部加常量:

```python
BONUS_GE75 = False           # P3: +8 门槛提到 ≥75;decisions.md 定稿
BONUS_QUALITY_GATE = False   # P3: quality<50 不给加成;decisions.md 定稿
```

改造 `sector_bonus`(保留原 docstring,追加 P3 说明):

```python
def sector_bonus(sector_composite, quality=None):
    """板块共振加成(规格 §5):≥68→+8;60≤x<68→+4;x<50→−5;其余→+0;缺失→0。
    P3 决策(BONUS_GE75/BONUS_QUALITY_GATE)可加门槛:GE75 时 +8 档提到 ≥75;
    QUALITY_GATE 时 quality<50 不给正加成。quality=None → 不 gate。"""
    if sector_composite is None:
        return 0
    if quality is not None and BONUS_QUALITY_GATE and quality < 50:
        return 0
    ge75 = BONUS_GE75
    hi = 75.0 if ge75 else 68.0
    if sector_composite >= hi:
        return 8
    if sector_composite >= 60:
        return 4
    if sector_composite < 50:
        return -5
    return 0
```

`sector_bonus` 现有两处调用点都要传 quality:`_score_candidate`(recommend.py:93):

```python
    bonus = sector_bonus(sector_composite, scores["composite"])
```

以及 `_apply_hot_weights`(Task 12 新增,recommend.py 热板块重算处;该函数已先 `scores = x["scores"]`):

```python
        bonus = sector_bonus(sector_composite, scores["composite"])
```

(`scores["composite"]` 即加成前 quality,spec §3 引用。)

- [ ] **Step 4: 回归确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS(含新测试)

- [ ] **Step 5: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "feat(scoring): P3 sector_bonus 门槛(GE75/quality gate,探针 gate 决策)"
```

---

### Task 15: P4 生产 — 低价护栏

**Files:**
- Modify: `recommend.py`(`filter_candidates` 51-77 或 `_score_candidate`)
- Test: `tests/test_recommend.py`

**Interfaces:**
- Consumes: Task 11 决策(`PRICE_FLOOR`/`PRICE_REL_MIN` 从 `_analysis/decisions.md`)。
- Produces: 按决策排除 < `PRICE_FLOOR` 的股票,或低价股需更高 rel_strength 分位。

- [ ] **Step 1: 写失败测试**

`tests/test_recommend.py` 追加(默认 `PRICE_FLOOR=None` 不动;决策 `PRICE_FLOOR=3.0` 时排除):

```python
def test_price_floor_excludes_low_price(monkeypatch):
    monkeypatch.setattr(recommend, "PRICE_FLOOR", 3.0)
    monkeypatch.setattr(recommend, "PRICE_REL_MIN", False)
    spot = make_spot().copy()
    spot.loc[spot["code"] == "600050", "price"] = 2.5          # 压到 <3
    kept, _ = recommend.filter_candidates(["600050", "600100"], spot, set())
    codes = [str(r["code"]) for r in kept]
    assert "600050" not in codes and "600100" in codes


def test_price_floor_default_off():
    assert recommend.PRICE_FLOOR is None
    spot = make_spot()
    kept, _ = recommend.filter_candidates(["600050"], spot, set())
    assert any(str(r["code"]) == "600050" for r in kept)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_recommend.py::test_price_floor_excludes_low_price tests/test_recommend.py::test_price_floor_default_off -v`
Expected: FAIL(无 `PRICE_FLOOR`)

- [ ] **Step 3: 实现**

`recommend.py` 顶部加常量:

```python
PRICE_FLOOR = None           # P4: 低于此价的候选排除(None=关);decisions.md 定稿
PRICE_REL_MIN = False        # P4: 低价股需更高 rel_strength 分位;decisions.md 定稿
```

`filter_candidates` 的 `kept.append(row)` 前加价格判断:

```python
        if PRICE_FLOOR is not None and price is not None and price < PRICE_FLOOR:
            continue                              # 低价护栏(spec §8)
        kept.append(row)
```

若决策 `PRICE_REL_MIN=True`(且 Task 11 已在 decisions.md 记录 `PRICE_REL_MIN_REL=N`),在 `_apply_hot_weights` 中给低价股(price < 5,同 P4 探针「3-5 元桶」下界)额外要求:rel_strength < N 时从 hot 排名剔除。`PRICE_REL_MIN` 仅当 Task 11 决策明确选中该形态才置 True,否则保持 False 且此分支不实现。

- [ ] **Step 4: 回归确认通过**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "feat(scoring): P4 低价护栏(PRICE_FLOOR 探针决策)"
```

---

### Task 16: P2 探针 — od 全量重校准(§9.1/9.2/9.5/9.4,多年窗口 + 热子样本可介入率)

**Files:**
- Modify: `_analysis/spike_v3b.py`(gitignored)
- Modify: `_analysis/decisions.md`
- Test: 无(gitignored)

**Interfaces:**
- Consumes: Task 12 权重定稿(读 decisions.md 的 `HOT_WEIGHT_MODE`/权重)。
- Produces: `decisions.md` 写入 `VERDICT_THRESHOLDS`(强/关/持/观)与 `BONUS_THRESHOLDS`(ge_hi/ge_mid/lt_neg)与 `HOT_ACTIONABLE_RATE`(热子样本可介入率)。

- [ ] **Step 1: 加三口径**

`spike_v3b.py` 在 fwd5 之外为每个 (股,日) 样本加 `gap`、`od`、`open3d`(open[T+1]→close[T+3]);窗口 `tail(260)` → `tail(1200)`;评估日采样 ~300。

- [ ] **Step 2: 重跑因子边际 + 热/非热切分**

在 **od 目标**上重跑 §9.1 单调性、§9.2 因子边际(加「热板块/非热板块」条件切分,热板块按 HOT_COMPOSITE_THRESHOLD 分)、§9.5 verdict 可介入率(目标 20-30%,**额外报告热板块子样本可介入率**,spec §6 验双分布未拉偏)、§9.4 sector_bonus 分布分位(P80/P50/P30)。

- [ ] **Step 3: 运行 + 决策**

Run: `cd _analysis && python spike_v3b.py --mode od`(全量,小时级,可后台)

`decisions.md` 追加 P2 小节:
- od 口径下 position/rel_strength/各因子边际(热 vs 非热);
- verdict 阈值重锚建议(od 可介入率 20-30%);
- sector_bonus 档位重锚建议(od 分布 P80/P50/P30);
- 热子样本可介入率是否在 20-30%(偏离则提示 §3.2 权重需回调,不开第二套阈值)。

勾掉待办 P2。

- [ ] **Step 4: 收尾**

报告重锚数值,供 Task 17 消费。

---

### Task 17: P2 常量落地 — verdict/bonus 阈值 + P1 警示阈值定稿

**Files:**
- Modify: `analysis.py`(`stock_verdict` 阈值)
- Modify: `recommend.py`(`sector_bonus` 阈值常量)
- Modify: `static/app.js`(若 Task 9 探针改变了 `GAP_WARN_PCT`)
- Test: 既有钉值测试更新 + 新增

**Interfaces:**
- Consumes: Task 16 `_analysis/decisions.md` 的 `VERDICT_THRESHOLDS`/`BONUS_THRESHOLDS`;Task 9 `GAP_WARN_PCT`。
- Produces: 常量更新 + 钉值测试同步。

- [ ] **Step 1: 读决策,更新阈值**

按 `_analysis/decisions.md` 的 `VERDICT_THRESHOLDS`/`BONUS_THRESHOLDS`:
- `analysis.stock_verdict`(504-521)四阈值替换(现 67/62/52/42),docstring 注明 od 口径重锚依据;
- `recommend.sector_bonus` 档位按 `BONUS_THRESHOLDS` 替换(现 68/60/50),保留 P3 gate 逻辑;
- `static/app.js` 的 `GAP_WARN_PCT` 若 Task 9 产出不同值则更新,并去掉「provisional」注释。

- [ ] **Step 2: 更新钉值测试**

搜索 `tests/` 中所有断言 67/62/52/42 与 sector_bonus 档位的测试(`grep -rn "67\|52\|42" tests/` 与 `sector_bonus`),按新阈值更新,并在测试旁注释「od 口径重锚」。

- [ ] **Step 3: 回归**

Run: `python -m pytest tests/ -q`
Expected: PASS(全量)

- [ ] **Step 4: Commit**

```bash
git add analysis.py recommend.py static/app.js tests/
git commit -m "feat(calib): P2 od 口径阈值重锚落地(verdict/bonus + 热子样本验证)"
```

---

### Task 18: 全量回归 + 收尾

**Files:**
- Test: 全量

- [ ] **Step 1: 全量回归**

Run: `python -m pytest tests/ -q`
Expected: PASS(≥ 110 + 本计划新增;0 失败)

- [ ] **Step 2: 探针一致性核对**

- `_analysis/decisions.md` 六项探针全部勾选;
- 生产常量(`HOT_WEIGHT_MODE`/`HOT_COMPOSITE_THRESHOLD`/`P0_PATH`/`OVERHEAT_MIN_DAYS`/`BONUS_GE75`/`BONUS_QUALITY_GATE`/`PRICE_FLOOR`/`PRICE_REL_MIN`/`PRICE_REL_MIN_REL`/verdict/bonus 阈值/`GAP_WARN_PCT`)与决策记录一致。

- [ ] **Step 3: 冒烟**

启动服务器,`/api/recommend` 与 `/api/actionable-leaders` 返回 200,推荐页无 JS 报错。

- [ ] **Step 4: Commit(若有残留)**

```bash
git add -A && git commit -m "chore: 热动量执行收尾(探针决策对齐 + 回归通过)" || echo "无残留改动"
```
