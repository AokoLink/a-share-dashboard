# 审计切片 B — 剩余缺陷修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复切片 A 之后的剩余 code-review 缺陷(P1 ×2、perf ×2、maint ×5、范围外 ×3),全部无评分影响,不重测准确率。

**Architecture:** 11 处真实代码改动 + 2 处裁定「留」,分布在 app.py / static/app.js / store.py / data_source.py / analysis.py / recommend.py 六文件。除 is_next_day(快照层 bug)与 get_stock_minute(非法 JSON)外均为行为零变化的维护/性能/安全修复;每处由现有钉值测试或新增定向测试验证。

**Tech Stack:** Python 3 + Flask 3.1 + pandas 3.x(CoW)+ SQLite(WAL);前端原生 JS(无测试框架);pytest。

**Spec:** `docs/superpowers/specs/2026-08-14-slice-b-findings-design.md`

## Global Constraints

- 直接提交 main,不建 worktree/feature 分支。
- `_analysis/` 已 gitignore——探针工作永不提交。
- Python 源码仅 ASCII `-`,禁用 U+2212 `−`。
- `_analysis/code2sector.json` 为 GBK 编码(open 用 `encoding="gbk"`)。
- 严格反未来泄露:预测输入 ≤ T,验证只用 > T。
- 全量测试 `python -m pytest tests/ -q`;不伪造测试成功。
- pandas 3.x CoW:`df["col"].iloc[0]=x` 抛错,用 `df.iloc[0, df.columns.get_loc("col")]=x`。
- **行号说明**:本文行号以 spec 批准 HEAD(`ffd0918`)为准。app.py 被 Task 1/5/6/7 触及,Task 6(api_stock 过滤)会增 ~3 行使后续行号下移。所有 Edit 用**精确字符串匹配**(old_string),不依赖行号;implementer 按函数名/相邻代码定位。

---

### Task 1: is_next_day 锚点 close_date → signal_date

**Files:**
- Modify: `app.py`(:291-294,api_recommend 快照块)
- Test: `tests/test_api.py`(:369-394 改 fixture、新增两用例)

**Interfaces:**
- Consumes: `store.get_recommend_snapshot_before` 返回的 `prev` dict 已有 `signal_date`/`close_date` 字段(upsert 第一参即 signal_date)。
- Produces: `payload["prev_snapshot"]` 的 `is_next_day`/`gap_days` 语义改为 signal_date 锚点(spec §5.2)。

- [ ] **Step 1: 改 app.py 三处 close_date → signal_date**

`app.py` 中精确替换:

```python
            if prev and prev["close_date"] is not None:
                is_next_day = (prev["close_date"] == payload["prev_trading_date"])
                td = payload.get("trading_dates") or []
                gap_days = sum(1 for d in td if prev["close_date"] < d <= payload["signal_date"])
```

改为:

```python
            if prev and prev["signal_date"] is not None:
                is_next_day = (prev["signal_date"] == payload["prev_trading_date"])
                td = payload.get("trading_dates") or []
                gap_days = sum(1 for d in td if prev["signal_date"] < d <= payload["signal_date"])
```

- [ ] **Step 2: 更新现有 test_recommend_prev_snapshot 的 fixture 与断言**

`tests/test_api.py` 中 `test_recommend_prev_snapshot`(:369-394)整体替换为(注释与锚点同步改 signal_date 口径):

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
    # 上一期快照 signal_date 须等于当前 prev_trading_date 才触发 is_next_day(spec §5.2 锚点=signal_date)
    prev_sig = r1["prev_trading_date"]
    store.upsert_recommend_snapshot(db, prev_sig, "2026-08-12 17:40:00",
                                    prev_sig, r1["prev_trading_date"],
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps is not None and ps["signal_date"] == prev_sig
    assert ps["is_next_day"] is True                       # prev.signal_date == 当前 prev_trading_date
    assert len(ps["stocks"]) == 1
    s = ps["stocks"][0]
    assert s["code"] == "600000" and s["signal_close"] == 10.0
    assert s["today_open"] == pytest.approx(9.8)           # make_spot open 列 600000 → 9.8
    assert abs(s["gap_pct"] - (-2.0)) < 0.01               # (9.8/10.0 - 1)*100
```

- [ ] **Step 3: 新增两个验证修复的用例**

在 `test_recommend_prev_snapshot_bj_prefix` 之前(或同文件末尾)追加:

```python
def test_recommend_prev_snapshot_intraday(monkeypatch, tmp_path):
    # Fix:盘中生成上一期快照(signal_date=D, close_date=D-1,二者不等)→ 仍判定 is_next_day(锚点=signal_date)
    db = str(tmp_path / "reco_snap_intraday.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    r1 = c.get("/api/recommend").get_json()["data"]
    prev_td = r1["prev_trading_date"]          # 当前 prev_trading_date(锚点)
    # signal_date=prev_td, close_date=prev_td 前一交易日(盘中快照末根在前一日)
    store.upsert_recommend_snapshot(db, prev_td, "2026-05-28 10:30:00",
                                    "2026-05-27", prev_td,
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps["is_next_day"] is True           # close_date(05-27) != prev_trading_date(05-28),但 signal_date 命中


def test_recommend_prev_snapshot_gap_days(monkeypatch, tmp_path):
    # Fix:跨多日 gap 计数按 signal_date 锚点(非 close_date)
    db = str(tmp_path / "reco_snap_gap.db")
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    c = client_factory(monkeypatch, db_path=db)
    r1 = c.get("/api/recommend").get_json()["data"]
    td = r1["trading_dates"]
    sig = r1["signal_date"]
    prev_sig = td[-3]                          # 上一期 signal_date 取倒数第 3 个交易日
    # close_date 故意设最早交易日:若锚点错用 close_date,gap_days 会远大于 signal_date 锚点的计数
    store.upsert_recommend_snapshot(db, prev_sig, "2026-05-26 17:40:00",
                                    td[0], td[-4],
                                    [{"code": "600000", "name": "浦发银行", "signal_close": 10.0}])
    r2 = c.get("/api/recommend").get_json()["data"]
    ps = r2["prev_snapshot"]
    assert ps["is_next_day"] is False
    assert ps["gap_days"] == sum(1 for d in td if prev_sig < d <= sig)
```

- [ ] **Step 4: 跑定向测试**

Run: `python -m pytest tests/test_api.py -k "prev_snapshot" -v`
Expected: 4 用例全 PASS(旧的 + intraday + gap_days + bj_prefix)。

- [ ] **Step 5: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "fix(api): is_next_day/gap_days 锚点 close_date→signal_date(spec §5.2)"
```

---

### Task 2: store.py 线程本地连接缓存 + close_all + conftest autouse

**Files:**
- Modify: `store.py`(加 `import threading`、`_conns`/`_conn_cache`/`close_all`、`_connect` 缓存、10 个函数删 `conn.close()`、`init_db` 删 close)
- Create: `tests/conftest.py`(autouse teardown)
- Test: `tests/test_store.py`(新增缓存复用/写可见/重开用例)

**Interfaces:**
- Consumes: 无(纯内部重构)。
- Produces: `store.close_all()`(供测试 teardown 与应用关闭);`_connect(db)` 返回线程本地缓存连接(同线程同路径复用)。

- [ ] **Step 1: 写失败测试**

`tests/test_store.py` 末尾追加(先于 conftest,验证缓存行为):

```python
def test_connection_reuse_within_thread(tmp_path):
    db = str(tmp_path / "reuse.db")
    store.init_db(db)
    c1 = store._connect(db)
    c2 = store._connect(db)
    assert c1 is c2                      # 同线程同路径复用同一连接


def test_write_visible_without_close(tmp_path):
    db = str(tmp_path / "vis.db")
    store.init_db(db)
    store.upsert_market_daily(db, "2026-08-10", 100, 50, 5, 3, 1, 1e9, 3400.0, "15:00")
    row = store.get_market_daily_prev(db, "2026-08-11")
    assert row["date"] == "2026-08-10"   # 无显式 close 仍已 commit 且可读


def test_close_all_reopens(tmp_path):
    db = str(tmp_path / "close.db")
    store.init_db(db)
    c1 = store._connect(db)
    store.close_all()
    c2 = store._connect(db)
    assert c1 is not c2
```

Run: `python -m pytest tests/test_store.py -k "reuse or visible or reopens" -v`
Expected: FAIL(此刻 `_connect` 每次开新连接,`c1 is c2` 断言失败)。

- [ ] **Step 2: 实现缓存**

`store.py` 顶部 import 区(:3-5)加 `import threading`;在 `SCHEMA` 之后、`init_db` 之前插入:

```python
_conns = threading.local()


def _conn_cache():
    cache = getattr(_conns, "cache", None)
    if cache is None:
        cache = {}
        _conns.cache = cache
    return cache
```

`_connect`(:34-39)改为:

```python
def _connect(db):
    key = os.path.abspath(db)
    cache = _conn_cache()
    conn = cache.get(key)
    if conn is None:
        conn = sqlite3.connect(db, timeout=30)
        conn.row_factory = sqlite3.Row  # 使 dict(row) 按列名映射
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        cache[key] = conn
    return conn


def close_all():
    """关闭线程本地缓存中的所有连接(应用关闭 / 测试 teardown)。"""
    cache = getattr(_conns, "cache", None)
    if cache:
        for conn in cache.values():
            try:
                conn.close()
            except Exception:
                pass
        _conns.cache = {}
```

`init_db`(:26-31)删除 `conn.close()` 行(连接留在线程缓存复用):

```python
def init_db(db):
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    conn = _connect(db)
    conn.executescript(SCHEMA)
    conn.commit()
    # 连接留在线程本地缓存中复用(由 close_all() 统一关闭)
```

其余 9 个函数(`upsert_market_daily`/`get_market_daily_prev`/`upsert_sector_daily`/`get_sector_turnover_avg`/`get_sector_prev_change`/`get_sector_change_3d`/`get_consecutive_days`/`upsert_recommend_snapshot`/`get_recommend_snapshot_before`)各自删除末尾的 `conn.close()` 行,写函数保留 `conn.commit()`。

- [ ] **Step 3: 新建 tests/conftest.py(autouse teardown)**

```python
# -*- coding: utf-8 -*-
import pytest
import store


@pytest.fixture(autouse=True)
def _close_store_conns():
    yield
    store.close_all()
```

> 说明:一处 autouse 覆盖所有测试文件(含 test_store.py / test_api.py / test_backtest.py)。
> 若 Windows tmp_path 清理仍因句柄报 PermissionError(夹具 teardown 顺序问题),把本 fixture
> 加参 `tmp_path`(强制其依赖 tmp_path,确保在目录删除前 teardown)。

- [ ] **Step 4: 跑定向测试**

Run: `python -m pytest tests/test_store.py -v`
Expected: 全 PASS(含新增 3 用例 + 既有 7 用例)。

- [ ] **Step 5: Commit**

```bash
git add store.py tests/conftest.py tests/test_store.py
git commit -m "perf(store): 线程本地 SQLite 连接缓存,消除每请求 ~360 次开连接"
```

---

### Task 3: analysis.py 两处公式去重(_high_flags + _pos60)

**Files:**
- Modify: `analysis.py`(抽 `_high_flags`、复用 `_pos60`、顺手前置 `_pos60` 定义)

**Interfaces:**
- Consumes: `sector_verdict`/`score_sector` 的 emotion/strength/risk 参数;`compute_position_score` 的 `df`。
- Produces: `_high_flags(emotion, strength, risk) -> (e_hi, s_hi, r_hi)`(内部共享)。

- [ ] **Step 1: 抽 _high_flags 助手**

在 `sector_verdict`(:165)之前插入:

```python
def _high_flags(emotion, strength, risk):
    """三高旗标(阈值 70/60/65),供 sector_verdict 与 score_sector 共享。"""
    e_hi = emotion is not None and emotion >= 70
    s_hi = strength is not None and strength >= 60
    r_hi = risk is not None and risk >= 65
    return e_hi, s_hi, r_hi
```

`sector_verdict`(:166-170)四行替换为:

```python
    e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)
    e_mid = emotion is not None and emotion >= 45
    s_mid = strength is not None and strength >= 35
```

`score_sector`(:202-204)三行替换为:

```python
    e_hi, s_hi, r_hi = _high_flags(emotion, strength, risk)
```

- [ ] **Step 2: _pos60 复用 + 前置定义**

`compute_position_score`(:318-323)中内联 pos60 三行替换:

```python
    df = add_ma(daily_df, (20,))
    last_close = df["close"].iloc[-1]
    pos60 = _pos60(df)
    pos_factor = 100.0 * (1.0 - pos60)
```

(`_pos60`(:419-425)定义整体上移到 `compute_position_score`(:314)之前,使读码顺序自然;运行时无碍。)

- [ ] **Step 3: 跑定向测试(验证行为零变化)**

Run: `python -m pytest tests/test_analysis_sector.py tests/test_analysis_stock.py -v`
Expected: 全 PASS(既有钉值测试证明 `sector_verdict`/`score_sector`/`compute_position_score` 输出不变)。

- [ ] **Step 4: Commit**

```bash
git add analysis.py
git commit -m "refactor(analysis): 抽 _high_flags 共享、复用 _pos60 去重(行为零变化)"
```

---

### Task 4: recommend.py 删 PRICE_REL_MIN 死常量 + pandas 导入提升

**Files:**
- Modify: `recommend.py`(:18 删常量、:129 删惰性 import、顶部加 `import pandas as pd`)
- Test: `tests/test_recommend.py`(:260 删 no-op monkeypatch)

**Interfaces:**
- Consumes: 无。
- Produces: 移除 `PRICE_REL_MIN`(生产零引用);`pandas` 顶部导入。

- [ ] **Step 1: 改 recommend.py**

删除 `recommend.py:18`:

```python
PRICE_REL_MIN = False        # P4: 低价股需更高 rel_strength 分位;decisions.md 定稿
```

顶部 import 区(:5-7)后加 `import pandas as pd`;删除 `_rel_strengths`(:129)内的 `import pandas as pd`。

- [ ] **Step 2: 删 test_recommend.py:260 的 no-op monkeypatch**

`tests/test_recommend.py` `test_price_floor_excludes_low_price`(:258-260)中删除:

```python
    monkeypatch.setattr(recommend, "PRICE_REL_MIN", False)
```

(该行只对死常量赋值,删除后测试仍测 PRICE_FLOOR 护栏。)

- [ ] **Step 3: 跑定向测试**

Run: `python -m pytest tests/test_recommend.py -k "price_floor or rel_strengths" -v`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "chore(recommend): 删 PRICE_REL_MIN 死常量,pandas 导入提升到顶部"
```

---

### Task 5: sector_bonus 删冗余 quality 实参(三处)

**Files:**
- Modify: `recommend.py`(:104、:147)、`app.py`(:244)

**Interfaces:**
- Consumes: `sector_bonus(sector_composite, quality=None)` 签名不变,但三处调用删第二实参(quality 死门 `BONUS_QUALITY_GATE=False`)。
- Produces: 无对外变化(行为零变化)。

- [ ] **Step 1: 三处删第二实参**

`recommend.py:104`:

```python
    bonus = sector_bonus(sector_composite, scores["composite"])
```
→
```python
    bonus = sector_bonus(sector_composite)
```

`recommend.py:147` 同改。

`app.py:244`:

```python
                sector_bonus_val = recommend.sector_bonus(max(comps), scores["composite"])
```
→
```python
                sector_bonus_val = recommend.sector_bonus(max(comps))
```

(哨兵 `if scores["composite"] is None`(:102)保留不动——它是「数据不足」守卫,与 quality 实参无关。)

- [ ] **Step 2: 跑定向测试(行为零变化)**

Run: `python -m pytest tests/test_recommend.py tests/test_api.py -k "sector_bonus or stock_detail_sector" -v`
Expected: 全 PASS(`BONUS_QUALITY_GATE=False` 时删实参无影响)。

- [ ] **Step 3: Commit**

```bash
git add recommend.py app.py
git commit -m "refactor: sector_bonus 删冗余 quality 实参(三处,quality 死门短路)"
```

---

### Task 6: api_stock 预过滤 summary(perf)

**Files:**
- Modify: `app.py`(:230-239,api_stock 板块打分前)
- Test: `tests/test_api.py`(新增定向断言用例)

**Interfaces:**
- Consumes: `ds.resolve_code_sectors(code6)` → `names`;`recommend.score_all_sectors(summary_f, db, ...)`。
- Produces: `score_all_sectors` 仅收到所属板块的 summary 子集。

- [ ] **Step 1: 写失败测试**

`tests/test_api.py` 追加:

```python
def test_stock_sector_scores_only_resolved(monkeypatch):
    # Fix:个股接口只对所属板块打分(预过滤 summary),不重算全 ~90 板块
    captured = {}
    def fake_score(summary, *a, **k):
        captured["names"] = list(summary["name"])
        return [{"name": n, "composite": 80.0} for n in summary["name"]]
    monkeypatch.setattr(ds, "resolve_code_sectors", lambda c: ["白酒"])
    monkeypatch.setattr(recommend, "score_all_sectors", fake_score)
    app = client_factory(monkeypatch)
    r = app.get("/api/stock?code=600519")
    assert captured["names"] == ["白酒"]           # 预过滤后只含白酒一行(非全量 2 行)
    assert r.get_json()["data"]["sector_resolved"] is True
```

Run: `python -m pytest tests/test_api.py::test_stock_sector_scores_only_resolved -v`
Expected: FAIL(`captured["names"]` 现为 `["半导体", "白酒"]` 全量 2 行)。

- [ ] **Step 2: 实现预过滤**

`app.py`(:233-237)中,取 summary 之后、score_all_sectors 之前插两行并按需改首参:

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

- [ ] **Step 3: 跑定向测试**

Run: `python -m pytest tests/test_api.py -k "stock_detail_sector or stock_sector_scores or stock_two_code" -v`
Expected: 全 PASS(既有 `test_stock_detail_sector_resolution` 的 mock 忽略 summary 实参,仍绿)。

- [ ] **Step 4: Commit**

```bash
git add app.py tests/test_api.py
git commit -m "perf(api): 个股接口按所属板块预过滤 summary,避免重算全板块"
```

---

### Task 7: get_stock_minute NaN → None + api_stock None 安全(范围外)

**Files:**
- Modify: `data_source.py`(:292 fetch 末尾归一)、`app.py`(:254-256 intraday 用 `_num`)
- Test: `tests/test_data_source.py`(NaN→None)、`tests/test_api.py`(intraday None 安全)

**Interfaces:**
- Consumes: `_num`(app.py:34-42,`None/NaN/非数值→None,否则 float`)。
- Produces: `get_stock_minute` 返回 None 而非 NaN;`/api/stock` intraday 对 None 安全、产合法 JSON。

- [ ] **Step 1: 写失败测试(data_source 侧)**

`tests/test_data_source.py` 追加:

```python
def test_stock_minute_nan_to_none(monkeypatch):
    # Fix:累计量为 0 处 avg 产 NaN → 归一为 None(避免 Flask 序列化出非法 NaN JSON)
    raw = pd.DataFrame({
        "day": ["2026-08-11 10:00:00", "2026-08-11 10:01:00"],
        "open": [1347.0, 1348.5], "high": [1350.0, 1351.0],
        "low": [1346.0, 1347.0], "close": [1348.0, 1349.0],
        "volume": [0, 5000], "amount": [0.0, 6.7e6],   # 首根累计量为 0 → avg NaN
    })
    monkeypatch.setattr(ds._ak, "stock_zh_a_minute",
                        lambda symbol, period, adjust: raw)
    df, _ = ds.get_stock_minute("sh600519")
    assert df["avg"].iloc[0] is None          # NaN 归一为 None
    assert df["avg"].iloc[1] == pytest.approx(6.7e6 / 5000)
```

Run: `python -m pytest tests/test_data_source.py::test_stock_minute_nan_to_none -v`
Expected: FAIL(`df["avg"].iloc[0]` 现为 NaN,非 None)。

- [ ] **Step 2: 实现 data_source.py 归一**

`data_source.py` `get_stock_minute` 的 `fetch()`(:292)末尾:

```python
        return out
```
→
```python
        return out.where(pd.notna(out), None)
```

- [ ] **Step 3: 改 app.py intraday None 安全(否则 float(None) 抛 TypeError)**

`app.py`(:254-256):

```python
        intraday = [{"time": str(x["time"]), "price": float(x["price"]),
                     "avg": float(x["avg"]), "volume": float(x["volume"])}
                    for x in minute.to_dict("records")]
```
→
```python
        intraday = [{"time": str(x["time"]), "price": _num(x["price"]),
                     "avg": _num(x["avg"]), "volume": _num(x["volume"])}
                    for x in minute.to_dict("records")]
```

- [ ] **Step 4: 新增 app 侧 None 安全测试**

`tests/test_api.py` 追加:

```python
def test_stock_intraday_none_safe(monkeypatch):
    # Fix:get_stock_minute 归一后 avg 可为 None,intraday 须 None 安全(不 float(None) 抛 TypeError)
    app = client_factory(monkeypatch)
    minute = pd.DataFrame({"time": ["10:00"], "price": [1348.0],
                           "avg": [None], "volume": [12000.0]})
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (minute, False))
    r = app.get("/api/stock?code=600519")
    d = r.get_json()["data"]
    assert d["intraday"][0]["avg"] is None    # 序列化为 null,非 NaN/无 500
```

- [ ] **Step 5: 跑定向测试**

Run: `python -m pytest tests/test_data_source.py::test_stock_minute_nan_to_none tests/test_api.py::test_stock_intraday_none_safe tests/test_data_source.py::test_stock_minute_normalized -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add data_source.py app.py tests/test_data_source.py tests/test_api.py
git commit -m "fix(data): get_stock_minute NaN→None,api_stock intraday None 安全(非法 JSON)"
```

---

### Task 8: static/app.js XSS 转义 + loadSectors null 守卫(范围外)

**Files:**
- Modify: `static/app.js`(加 `esc()` 助手并包裹后端字符串插值;:58 loadSectors 守卫)

**Interfaces:**
- Consumes: 后端返回的 name/verdict/tag/reason 等字符串字段。
- Produces: 无(前端渲染安全)。

- [ ] **Step 1: 加 esc() 助手**

`static/app.js` 顶部(首个函数之前)加:

```javascript
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
```

- [ ] **Step 2: 凡插值后端字符串处包裹 esc()**

实施指引:**插值任何后端来源字符串都包 `esc()`**,不按固定个数(实际 innerHTML 约 24 处、后端字符串插值
约 19-20 处,易漏包)。重点:板块名/股票名 `s.name`/`x.name`/`d.name`(:56/:85/:207/:397)、龙头 tag
`x.tag`(:105-106)、板块/个股 verdict `s.verdict`/`d.verdict`/`x.verdict`(:61/:90/:272/:319)、
提示语 hint/reason(:100/:242/:246/:266/:296/:312/:339/:349)。纯数字字段(`.toFixed`/`fmtPct`)不需转义。
示例(`:56-58` loadSectors 行):

```javascript
  tbody.innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${esc(s.code)}">` +
    `<td>${esc(s.name)}</td><td class="${s.index_change_pct != null && s.index_change_pct >= 0 ? "up" : "down"}">${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${esc(s.verdict)}${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}</td></tr>`).join("");
```

- [ ] **Step 3: loadSectors null 守卫(与 :108 同模式)**

`:58` 的 `${s.index_change_pct >= 0 ...}` 已并入 Step 2 示例(改为 `s.index_change_pct != null &&`),
即 null 不再强转 0 误判红盘。

- [ ] **Step 4: 手工回归**

无 JS 测试框架。手工打开龙头页 + 推荐页,确认正常渲染;构造含 `<` 的股票名/板块名确认不执行脚本、
停牌板块颜色不误判红。此为已知边界,不做自动化 JS 测试。

- [ ] **Step 5: Commit**

```bash
git add static/app.js
git commit -m "fix(web): XSS 转义后端字符串插值 + loadSectors null 守卫"
```

---

## 收尾(所有任务完成后)

- [ ] 全量测试 `python -m pytest tests/ -q`,确认全绿(切片 A 后基线 168 passed;本切片新增 ~7 用例,预计 ~175)。
- [ ] 由 controller 驱动最终全分支 review(spec 合规 + 代码质量),裁定残余 finding 并记 ledger。
- [ ] 更新 `.superpowers/sdd/2026-08-14-slice-b-findings/progress.md`(任务完成态 + 裁定)。

## 任务依赖与文件共享

| 文件 | 触及任务 | 冲突评估 |
|---|---|---|
| app.py | T1(api_recommend :291)、T5(:244)、T6(:233)、T7(:254) | 区域互不重叠;T6 增 ~3 行使后续行号下移 → 一律用精确字符串匹配 |
| recommend.py | T4(:18/:129)、T5(:104/:147) | 不同函数,无重叠 |
| tests/test_api.py | T1(prev_snapshot 区)、T6(新用例)、T7(新用例) | 新增用例追加于文件不同位置,无文本重叠 |
| tests/test_recommend.py | T4(:260 删行) | 单行删除 |
| 其余文件 | 各自单任务 | 无共享 |

推荐执行顺序 T1→T2→T3→T4→T5→T6→T7→T8(T1 为唯一正确性 bug,先行;T2 缓存为后续所有 store 调用提速)。
