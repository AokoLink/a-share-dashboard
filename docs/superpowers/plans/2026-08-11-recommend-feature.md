# 股票推荐功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 A 股三层看板上新增「推荐」Tab,给出板块→个股双层推荐(THS 强势板块 + 经映射表转新浪行业的成分股 Top 5)。

**Architecture:** 板块层复用现有 THS 打分(先重构抽出公共打分函数 `score_sector`/`collect_sector_metrics` 为唯一入口),成分股经 `SECTOR_CONS_MAP` 手动映射表 + 关键词兜底转新浪 `stock_sector_detail`,个股层用全市场 spot 硬过滤后并发拉日线复用 `score_stock`。新增 `recommend.py`(纯函数 select/filter/rank + `build_recommend` 编排)、`/api/recommend` 路由、前端推荐 Tab、映射维护脚本。

**Tech Stack:** Python 3 + Flask + pandas + akshare(1.18.84);前端原生 JS + ECharts。数据源仅用新浪/腾讯/同花顺(东财、网易本网络屏蔽,禁止引入)。

## Global Constraints

- 数据源:仅 Sina/Tencent/THS;`stock_board_industry_cons_em` 等东财接口在本网络被屏蔽,禁止使用。
- 所有测试离线可跑:mock `ds._ak.<akshare函数>` 或 `ds.<函数>`,缓存测试用 `FakeClock` 并清空 `ds.cache._data`。
- 板块打分口径以 `analysis.sector_verdict` 为准:入选仅 `建议关注` / `跟踪(热点延续)`(即 `QUALIFYING_VERDICTS`)。
- 数值字段一律经 `_num()` 归一无(app.py 已有 `_num` 语义:None/NaN→None)。
- git 身份:仓库已配置 `stock-tool <stock-tool@local>`;提交用 conventional commits,结尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- 每次 task 结束运行 `python -m pytest tests/ -v` 确认全绿再提交。
- 测试里对 `composite_score`(经 `round(..., 2)`)断言用精确字面量或 `abs=0.01`,不要用默认相对容差比对舍入值。

---

### Task 1: 重构板块打分 —— 抽出公共入口 `score_sector` / `collect_sector_metrics`

**Files:**
- Modify: `analysis.py`(新增两个函数,在 `composite_score` 之后、个股量价打分之前)
- Modify: `app.py:129-165`(`api_sectors` 行循环)、`app.py:192-218`(`api_sector` 内联计算段)
- Test: `tests/test_analysis_sector.py`(追加)、回归 `tests/test_api.py`

**Interfaces:**
- Produces:
  - `analysis.score_sector(metrics: dict) -> dict`,metrics 键:`change_pct, up_ratio, limit_ratio, leader_change_pct, turnover_ratio, consecutive_days, activity, change_3d, prev_change, data_complete`;返回 `{"emotion","strength","risk","composite","verdict"}`。组合现有 `sector_emotion/strength/risk/composite_score/verdict`,行为与现两个路由的内联口径**逐字段一致**。
  - `analysis.collect_sector_metrics(db, type_key, code, change_pct, turnover, up_count, down_count, leader_change_pct, store_ctx, market_turnover, now, data_complete=True) -> dict`,返回 `{"emotion","strength","risk","composite","verdict","consecutive_days"}`。`store_ctx` 为 duck-typed 访问器(`get_sector_turnover_avg(db,type,code,date,days)`、`get_sector_prev_change(db,type,code,date)`、`get_sector_change_3d(db,type,code,date)`、`get_consecutive_days(db,type,code,date,top_n)`),运行时传 `store` 模块,测试传 fake。

- [ ] **Step 1: 写失败测试**

在 `tests/test_analysis_sector.py` 末尾追加:

```python
class FakeStore:
    """最小 store_ctx:无历史 → 各 getter 返回 None/0。"""
    def get_sector_turnover_avg(self, db, t, c, d, days=5): return None
    def get_sector_prev_change(self, db, t, c, d): return None
    def get_sector_change_3d(self, db, t, c, d): return None
    def get_consecutive_days(self, db, t, c, d, top_n=20): return 0


def test_score_sector_composes_five():
    # 与单函数口径一致:emotion/strength 全高、risk 0 → 建议关注
    r = an.score_sector({
        "change_pct": 5.0, "up_ratio": 0.9, "limit_ratio": None,
        "leader_change_pct": 5.0, "turnover_ratio": None,
        "consecutive_days": 2, "activity": 0.03,
        "change_3d": None, "prev_change": None, "data_complete": True,
    })
    # emotion: up 90(40) + leader 100(15),limit/turnover 缺失 → 权重归一化到 55
    assert r["emotion"] == pytest.approx((90 * 40 + 100 * 15) / 55)
    # strength: 指数5%→100(40) + 连续2天→50(30) + 活跃3%→100(30)
    assert r["strength"] == pytest.approx((100 * 40 + 50 * 30 + 100 * 30) / 100)
    assert r["risk"] == pytest.approx(0)                    # 5.0 非 >5,无前日/3d/turnover_ratio
    assert r["composite"] == pytest.approx(91.45)           # round(0.4*85 + 0.35*92.73 + 0.25*100, 2)
    assert r["verdict"] == "建议关注"


def test_collect_sector_metrics_full_pipeline():
    import datetime
    now = datetime.datetime(2026, 8, 11, 15, 0)
    # 无历史:turnover_ratio=None, prev=None, 3d=None, consecutive=0
    r = an.collect_sector_metrics(
        ":db:", "industry", "885887", 5.0, 1e10, 90.0, 5.0, 5.0,
        FakeStore(), 1e12, now)
    assert set(r) == {"emotion", "strength", "risk", "composite", "verdict", "consecutive_days"}
    assert r["consecutive_days"] == 0
    assert r["verdict"] == "建议关注"     # up 94.7% + 强度71.4 + risk0 → 情绪/强度双高


def test_collect_sector_metrics_after_close_uses_turnover_ratio():
    import datetime
    now = datetime.datetime(2026, 8, 11, 15, 0)

    class StoreWithHistory(FakeStore):
        def get_sector_turnover_avg(self, db, t, c, d, days=5): return 5e9

    r = an.collect_sector_metrics(
        ":db:", "industry", "885887", 0.5, 1e10, 50.0, 50.0, 1.0,
        StoreWithHistory(), 1e12, now)
    # 收盘后 turnover=1e10 / avg5=5e9 → turnover_ratio=2.0,计入情绪
    assert r["emotion"] == pytest.approx((50 * 40 + 100 * 25 + 20 * 15) / 80)  # 60.0
    # prev=None → 放量滞涨分支需 prev_change,不触发;指数涨0.5<5 → risk 0
    assert r["risk"] == pytest.approx(0)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_analysis_sector.py -v`
Expected: FAIL(`AttributeError: module 'analysis' has no attribute 'score_sector'`)

- [ ] **Step 3: 在 `analysis.py` 的 `composite_score` 之后实现**

```python
def score_sector(metrics):
    """板块打分唯一入口:组合 emotion/strength/risk/composite/verdict。
    metrics 键见接口说明;与旧 app 路由内联口径完全一致。"""
    emotion = sector_emotion(metrics["up_ratio"], metrics["limit_ratio"],
                             metrics["turnover_ratio"], metrics["leader_change_pct"])
    strength = sector_strength(metrics["change_pct"], metrics["consecutive_days"],
                               metrics["activity"])
    risk = sector_risk(metrics["change_pct"], metrics["change_3d"],
                       metrics["turnover_ratio"], metrics["prev_change"], metrics["up_ratio"])
    composite = composite_score(strength, emotion, risk)
    verdict = sector_verdict(emotion, strength, risk, metrics["consecutive_days"],
                             metrics["data_complete"])
    return {"emotion": emotion, "strength": strength, "risk": risk,
            "composite": composite, "verdict": verdict}


def collect_sector_metrics(db, type_key, code, change_pct, turnover, up_count, down_count,
                           leader_change_pct, store_ctx, market_turnover, now, data_complete=True):
    """summary 行 + store 历史 + 市场成交额 → 打分输入 → 五维结果 + consecutive。
    与旧 api_sectors/api_sector 内联逻辑逐字段一致:limit_ratio 恒 None(无成分股聚合)。"""
    up_ratio = None
    if up_count is not None and down_count is not None and (up_count + down_count) > 0:
        up_ratio = up_count / (up_count + down_count)
    today = now.strftime("%Y-%m-%d")
    turnover_ratio = None
    if is_after_close(now) and turnover is not None:
        avg5 = store_ctx.get_sector_turnover_avg(db, type_key, code, today, 5)
        if avg5 and avg5 > 0:
            turnover_ratio = turnover / float(avg5)
    prev_change = store_ctx.get_sector_prev_change(db, type_key, code, today)
    change_3d = store_ctx.get_sector_change_3d(db, type_key, code, today)
    consecutive = store_ctx.get_consecutive_days(db, type_key, code, today, 20)
    activity = (turnover / market_turnover) if (turnover is not None and market_turnover) else None
    scores = score_sector({
        "change_pct": change_pct, "up_ratio": up_ratio, "limit_ratio": None,
        "leader_change_pct": leader_change_pct, "turnover_ratio": turnover_ratio,
        "consecutive_days": consecutive, "activity": activity,
        "change_3d": change_3d, "prev_change": prev_change, "data_complete": data_complete,
    })
    scores["consecutive_days"] = consecutive
    return scores
```

- [ ] **Step 4: 运行确认新测试通过**

Run: `python -m pytest tests/test_analysis_sector.py -v`
Expected: PASS(含既有用例)

- [ ] **Step 5: 重构 `app.py` 两个路由,删除内联打分**

`api_sectors` 中 `rows` 循环体(现 `app.py:130-165`)替换为:

```python
        rows = []
        for _, r in summary.iterrows():
            code = str(r["code"])
            chg = _num(r["change_pct"])
            scores = an.collect_sector_metrics(
                db_path, type_key, code, chg, _num(r["turnover"]),
                _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
                store, market_turnover, now, True)
            rows.append({
                "code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                "index_change_pct": chg,
                "emotion_score": scores["emotion"], "strength_score": scores["strength"],
                "risk_score": scores["risk"], "composite_score": scores["composite"],
                "verdict": scores["verdict"], "consecutive_days": scores["consecutive_days"],
                "data_complete": True,
            })
```

删除原 `up_ratio`/`limit_ratio`/`turnover_ratio`/`prev_change`/`change_3d`/`activity`/`consecutive`/`emotion`/`strength`/`risk`/`composite`/`verdict` 的逐行计算(`app.py:136-158`)。`app.py:122-127` 的 rank 持久化块(仅交易日 upsert)保留不动。

`api_sector` 中(现 `app.py:192-218`)替换为:

```python
        r = row.iloc[0]
        chg = _num(r["change_pct"])
        scores = an.collect_sector_metrics(
            app.config["DB"], type_key, code, chg, _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
            store, market_turnover, now, True)
        verdict = scores["verdict"]
        hist_rows = [{"date": str(x["date"]), "open": float(x["open"]), "high": float(x["high"]),
                      "low": float(x["low"]), "close": float(x["close"]), "volume": float(x["volume"])}
                     for x in hist.to_dict("records")]
        return ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                   "scores": {"emotion": scores["emotion"], "strength": scores["strength"],
                              "risk": scores["risk"], "composite": scores["composite"]},
                   "verdict": verdict, "index_history": hist_rows},
                  stale=stale1 or stale2 or stale3)
```

注意:`api_sector` 里 `now`(现 `app.py:202`)与 `market_turnover`(现 `app.py:212`)的计算保留,`consecutive` 改由 `collect_sector_metrics` 内部取。

- [ ] **Step 6: 全量回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS(打分口径不变,`test_api.py` 断言结构/算术仍成立)

- [ ] **Step 7: 提交**

```bash
git add analysis.py app.py tests/test_analysis_sector.py
git commit -m "refactor(analysis): 抽出 score_sector/collect_sector_metrics 为板块打分唯一入口

api_sectors/api_sector 删除内联打分,改调公共函数,口径不变;
推荐功能将复用同一路径(规格 §4.0)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `data_source.py` —— `get_new_stocks` 成功缓存

**Files:**
- Modify: `data_source.py`(`get_new_stocks`,现 `data_source.py:238-245`)
- Test: `tests/test_data_source.py`(新增 + 给既有 `test_new_stocks_english_column` 加缓存隔离)

**Interfaces:**
- Produces:`get_new_stocks() -> set[str]`,仅对成功结果 TTL 缓存 1800s;失败返回 stale 值或空集,**不缓存**。

- [ ] **Step 1: 写失败测试**

在 `tests/test_data_source.py` 末尾追加,并给既有 `test_new_stocks_english_column` 函数体开头加一行 `ds.cache._data.clear()`(避免被缓存污染):

```python
def test_new_stocks_cached_on_success(monkeypatch):
    calls = {"n": 0}
    def fake():
        calls["n"] += 1
        return pd.DataFrame({"code": ["920000"], "name": ["A"]})
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fake)
    clock = FakeClock()
    ds.cache._clock = clock
    ds.cache._data.clear()
    assert ds.get_new_stocks() == {"920000"}
    assert ds.get_new_stocks() == {"920000"}
    assert calls["n"] == 1                       # 缓存命中,不再拉取
    clock.t = 1801
    monkeypatch.setattr(ds._ak, "stock_zh_a_new",
                        lambda: pd.DataFrame({"code": ["920001"], "name": ["B"]}))
    assert ds.get_new_stocks() == {"920001"}     # 过期后重新拉取


def test_new_stocks_not_cached_on_failure(monkeypatch):
    calls = {"n": 0}
    def fail():
        calls["n"] += 1
        raise RuntimeError("network down")
    monkeypatch.setattr(ds._ak, "stock_zh_a_new", fail)
    ds.cache._data.clear()
    assert ds.get_new_stocks() == set()          # 失败 → 空集
    assert ds.get_new_stocks() == set()          # 且不缓存:再次调用仍重试
    assert calls["n"] == 4                       # 2 次调用 × _fetch_with_retry 内部重试 1 次
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_data_source.py -k "new_stocks" -v`
Expected: FAIL(`test_new_stocks_cached_on_success` 断言 `calls["n"] == 1` 时实际为 2)

- [ ] **Step 3: 实现**

替换 `data_source.py` 的 `get_new_stocks`:

```python
def get_new_stocks():
    """上市≤5交易日的股票(尽力而为):取最近新股列表;接口不可用 → 空集。
    仅缓存成功结果;失败不缓存(下次仍重试),并回退旧值。"""
    def fetch():
        raw = _ak.stock_zh_a_new()
        codes = _pick(raw, "代码", "code").map(normalize_code).tolist()
        return set(codes)

    val, fresh = cache.get(_key("new_stocks"))
    if fresh:
        return val
    try:
        data = _fetch_with_retry(fetch)
        cache.set(_key("new_stocks"), data, 1800)
        _set_updated()
        return data
    except Exception:
        return val if val is not None else set()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_data_source.py -k "new_stocks" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add data_source.py tests/test_data_source.py
git commit -m "feat(data): get_new_stocks 成功缓存 1800s,失败不缓存并回退旧值

推荐功能与大盘都调用新股列表;避免每请求实时拉取(规格 §8)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `data_source.py` —— 映射常量 + 成分股解析 + 映射校验

**Files:**
- Modify: `data_source.py`(模块顶部加常量;`get_new_stocks` 之后加函数)
- Test: `tests/test_data_source.py`(新增)

**Interfaces:**
- Produces:
  - 常量 `SECTOR_CONS_MAP: dict[str,str]`(THS 板块名→新浪 label)、`SECTOR_CONS_EXPECTED: dict[str,str]`(新浪 label→预期名)、`SECTOR_KEYWORDS: dict[str,list[str]]`(THS 名→可接受的新浪行业名列表)。
  - `resolve_sector_constituents(ths_name: str) -> dict`(对应规格 §9 的 `get_sector_constituents`):`{"ok": True, "codes": [str], "match_type": "manual"|"keyword", "source_name": str}` 或 `{"ok": False, "reason": "no_mapping"|"ambiguous"}`;网络失败抛 `DataSourceError`。
  - `validate_sector_map() -> dict`:`{"ok": True, "total", "valid", "stale": [{"ths","label"}], "renamed": [{"ths","label","expected","actual"}]}`;拉取失败时 `{"ok": False, "error": str}`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_data_source.py` 末尾追加。`_mock_sina_spot` 用 `SECTOR_CONS_EXPECTED` 生成全部 label 的 spot 行,保证「每个映射 value 都是有效新浪 label」的断言可离线成立:

```python
def _mock_sina_spot(monkeypatch):
    rows = [{"label": label, "name": name} for label, name in ds.SECTOR_CONS_EXPECTED.items()]
    df = pd.DataFrame(rows)
    df["公司家数"] = 1; df["涨跌额"] = 0.0; df["涨跌幅"] = 1.0
    df["总成交量"] = 1; df["总成交额"] = 1.0; df["股票代码"] = "a"
    df["领涨股-涨跌幅"] = 1.0; df["领涨股-当前价"] = 1.0; df["领涨股"] = "a"
    monkeypatch.setattr(ds._ak, "stock_sector_spot", lambda indicator: df)


def test_constituents_manual_mapping(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    monkeypatch.setattr(ds._ak, "stock_sector_detail",
                        lambda sector: pd.DataFrame({"symbol": ["sh600050", "sh600100"],
                                                     "code": ["600050", "600100"],
                                                     "name": ["中国联通", "同方股份"]}))
    res = ds.resolve_sector_constituents("半导体")      # SECTOR_CONS_MAP 应含 半导体→new_dzxx
    assert res["ok"] is True
    assert res["match_type"] == "manual"
    assert res["codes"] == ["600050", "600100"]
    assert res["source_name"] == "电子信息"


def test_constituents_no_mapping_and_ambiguous(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    assert ds.resolve_sector_constituents("绝对不存在的板块")["reason"] == "no_mapping"
    # 关键词兜底:双向包含命中但多命中 → ambiguous(不静默取第一个)
    label_to_name = {"new_dzxx": "电子信息", "new_dzqj": "电子器件"}
    assert ds._keyword_lookup("电子", label_to_name) == ("new_dzxx", True)


def test_validate_sector_map_detects_stale_and_renamed(monkeypatch):
    _mock_sina_spot(monkeypatch)
    ds.cache._data.clear()
    # 临时替换常量,验证检测逻辑(不改动正式常量)
    monkeypatch.setattr(ds, "SECTOR_CONS_MAP", {"半导体": "new_dzxx", "坏映射": "new_xxxx"})
    monkeypatch.setattr(ds, "SECTOR_CONS_EXPECTED", {"new_dzxx": "电子信息", "new_xxxx": "旧名"})
    h = ds.validate_sector_map()
    assert h["ok"] is True and h["total"] == 2 and h["valid"] == 1
    assert h["stale"] == [{"ths": "坏映射", "label": "new_xxxx"}]
    assert h["renamed"] == []                            # new_dzxx 名未变
    monkeypatch.setattr(ds, "SECTOR_CONS_EXPECTED", {"new_dzxx": "旧名"})
    h2 = ds.validate_sector_map()
    assert h2["renamed"][0]["ths"] == "半导体" and h2["renamed"][0]["actual"] == "电子信息"


def test_constituents_map_keys_values_valid(monkeypatch):
    _mock_sina_spot(monkeypatch)
    assert all(isinstance(k, str) and k for k in ds.SECTOR_CONS_MAP)
    # 每个映射 value 都必须是有效新浪 label(与 SECTOR_CONS_EXPECTED 全集一致)
    assert set(ds.SECTOR_CONS_MAP.values()) <= set(ds.SECTOR_CONS_EXPECTED)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_data_source.py -k "constituents or validate_sector_map or SECTOR" -v`
Expected: FAIL(`AttributeError: module 'data_source' has no attribute 'resolve_sector_constituents'`)

- [ ] **Step 3: 实现**

在 `data_source.py` 顶部(常量区,`_key` 定义之后)加:

```python
# ---- 推荐:THS 板块 → 新浪行业成分股 映射(规格 §5) ----
# 新浪为旧分类(49 板块),仅收录有清晰对应的 THS 板块;未覆盖板块由上层标记 no_mapping。
SECTOR_CONS_MAP = {
    "白酒": "new_ljhy", "白色家电": "new_jdhy", "黑色家电": "new_jdhy", "小家电": "new_jdhy",
    "电力": "new_dlhy", "房地产": "new_fdc", "钢铁": "new_gthy",
    "服装家纺": "new_fzxl", "纺织制造": "new_fzhy",
    "环保设备": "new_hbhy", "环境治理": "new_hbhy",
    "建筑材料": "new_jzjc", "建筑装饰": "new_jzjc",
    "旅游及酒店": "new_jdly", "煤炭开采加工": "new_mthy",
    "农化制品": "new_nyhf", "汽车整车": "new_qczz", "汽车零部件": "new_qczz",
    "燃气": "new_gsgq", "塑料制品": "new_slzp", "食品加工制造": "new_sphy",
    "石油加工贸易": "new_syhy", "有色金属": "new_ysjs", "贵金属": "new_ysjs",
    "造纸": "new_zzhy", "医疗器械": "new_ylqx", "生物制品": "new_swzz",
    "半导体": "new_dzxx", "消费电子": "new_dzxx", "通信设备": "new_dzxx",
    "计算机设备": "new_dzxx", "软件开发": "new_dzxx",
    "光学光电子": "new_dzqj", "元件": "new_dzqj",
    "工程机械": "new_jxhy", "通用设备": "new_jxhy", "专用设备": "new_jxhy",
}
SECTOR_CONS_EXPECTED = {   # label → 预期新浪名,启动校验检测改名漂移
    "new_ljhy": "酿酒行业", "new_jdhy": "家电行业", "new_dlhy": "电力行业",
    "new_fdc": "房地产", "new_gthy": "钢铁行业", "new_fzxl": "服装鞋类",
    "new_fzhy": "纺织行业", "new_hbhy": "环保行业", "new_jzjc": "建筑建材",
    "new_jdly": "酒店旅游", "new_mthy": "煤炭行业", "new_nyhf": "农药化肥",
    "new_qczz": "汽车制造", "new_gsgq": "供水供气", "new_slzp": "塑料制品",
    "new_sphy": "食品行业", "new_syhy": "石油行业", "new_ysjs": "有色金属",
    "new_zzhy": "造纸行业", "new_ylqx": "医疗器械", "new_swzz": "生物制药",
    "new_dzxx": "电子信息", "new_dzqj": "电子器件", "new_jxhy": "机械行业",
}
SECTOR_KEYWORDS = {          # THS 名 → 可接受的新浪行业名(同义词兜底,仅高置信)
    "半导体": ["电子信息", "电子器件"],
    "白酒": ["酿酒行业"],
}
```

在 `get_new_stocks` 之后加:

```python
def _sina_industry_names():
    """新浪行业 spot:label→name 对照,缓存 1800s。"""
    def fetch():
        raw = _ak.stock_sector_spot(indicator="新浪行业")
        return {str(r["label"]): str(r["name"]) for _, r in raw.iterrows()}
    return _cached(_key("sina_industry_names"), 1800, lambda: _fetch_with_retry(fetch))


def _fetch_sina_constituents(label):
    """新浪板块成分股 → 6 位代码列表,缓存 1800s。"""
    def fetch():
        raw = _ak.stock_sector_detail(sector=label)
        codes = _pick(raw, "code", "symbol").astype(str).map(normalize_code).tolist()
        return [c for c in codes if c.isdigit()]
    return _cached(_key("sector_cons", label), 1800, lambda: _fetch_with_retry(fetch))


def _keyword_lookup(ths_name, label_to_name):
    """关键词兜底:双向包含 + SECTOR_KEYWORDS 同义词语料。→ (label, ambiguous) | None。"""
    cands = []
    for label, name in label_to_name.items():
        if ths_name in name or name in ths_name:
            cands.append(label)
        elif ths_name in SECTOR_KEYWORDS and name in SECTOR_KEYWORDS[ths_name]:
            cands.append(label)
    if not cands:
        return None
    uniq = list(dict.fromkeys(cands))
    return uniq[0], len(uniq) > 1


def resolve_sector_constituents(ths_name):
    """板块名 → 成分股(手动表 → 关键词兜底)。失败原因 no_mapping/ambiguous;网络失败抛异常。"""
    label = SECTOR_CONS_MAP.get(ths_name)
    if label is not None:
        codes, _ = _fetch_sina_constituents(label)
        names, _ = _sina_industry_names()
        return {"ok": True, "codes": codes, "match_type": "manual",
                "source_name": names.get(label, label)}
    names, _ = _sina_industry_names()
    hit = _keyword_lookup(ths_name, names)
    if hit is None:
        return {"ok": False, "reason": "no_mapping"}
    label, ambiguous = hit
    if ambiguous:
        return {"ok": False, "reason": "ambiguous"}
    codes, _ = _fetch_sina_constituents(label)
    return {"ok": True, "codes": codes, "match_type": "keyword",
            "source_name": names.get(label, label)}


def validate_sector_map():
    """启动校验:手动映射的每个新浪 label 是否仍存在、名称是否漂移。失败不阻塞,仅返回报告。"""
    try:
        names, _ = _sina_industry_names()
    except Exception as e:
        return {"ok": False, "error": str(e), "total": 0, "valid": 0,
                "stale": [], "renamed": []}
    stale, renamed = [], []
    for ths, label in SECTOR_CONS_MAP.items():
        if label not in names:
            stale.append({"ths": ths, "label": label})
        else:
            expected = SECTOR_CONS_EXPECTED.get(label)
            if expected and names[label] != expected:
                renamed.append({"ths": ths, "label": label,
                                "expected": expected, "actual": names[label]})
    return {"ok": True, "total": len(SECTOR_CONS_MAP),
            "valid": len(SECTOR_CONS_MAP) - len(stale),
            "stale": stale, "renamed": renamed}
```

注:`_sina_industry_names` / `_fetch_sina_constituents` 走 `_cached` 返回 `(data, stale)`;测试用例各自先 `ds.cache._data.clear()` 隔离模块级缓存。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_data_source.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add data_source.py tests/test_data_source.py
git commit -m "feat(data): 板块→成分股映射(手动表+关键词兜底)+ resolve_sector_constituents + validate_sector_map

映射表收录 35 个有清晰新浪对应的 THS 板块;启动校验检测 stale/改名漂移;
两级映射失败返回 no_mapping/ambiguous(规格 §5)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: `recommend.py` —— 双层推荐纯函数 + 编排

**Files:**
- Create: `recommend.py`
- Test: `tests/test_recommend.py`(新增)

**Interfaces:**
- Consumes:`analysis.collect_sector_metrics`、`analysis.score_stock`、`analysis.limit_threshold`、`data_source.resolve_sector_constituents`、`data_source.get_stock_daily`、`data_source.get_new_stocks`、`data_source.with_prefix`、`store`(作为 store_ctx)。
- Produces:
  - `select_sectors(summary_df, db, type_key, store_ctx, market_turnover, now) -> list[dict]`:`[{code, name, composite, verdict, scores}]`,仅强势板块,按 composite 降序。
  - `filter_candidates(codes, spot_df, exclude_codes, min_amount=MIN_AMOUNT) -> (list[dict], int)`:`(kept_spot_rows, not_in_spot_count)`。
  - `rank_candidates(scored, per_sector) -> list[dict]`。
  - `build_recommend(summary_df, spot_df, db, type_key, now, top_sectors=3, per_sector=5) -> (dict, bool)`:`(payload, stale_any)`,payload=`{"strong_count","sectors","skipped_sectors","diagnostics"}`。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_recommend.py`:

```python
# -*- coding: utf-8 -*-
import datetime
import pandas as pd
import pytest
import analysis as an
import data_source as ds
import recommend


def make_summary():
    return pd.DataFrame({
        "code": ["885887", "885559", "885123"],
        "name": ["半导体", "白酒", "化工"],
        "change_pct": [5.0, -1.0, 0.5],
        "up_count": [90.0, 30.0, 50.0],
        "down_count": [5.0, 60.0, 50.0],
        "leader": ["X", "Y", "Z"],
        "leader_change_pct": [5.0, 0.5, 1.0],
        "turnover": [1e10, 1e9, 5e9],
    })


def make_spot():
    return pd.DataFrame({
        "code": ["600050", "600100", "600519", "688981", "300750"],
        "name": ["中国联通", "同方股份", "ST茅台", "中芯国际", "宁德时代"],
        "price": [5.0, 10.0, 1348.9, 45.0, 200.0],
        "change_pct": [3.0, 2.0, 1.0, 5.0, 0.0],
        "volume": [100000, 0, 682720, 90000, 90000],
        "amount": [2e8, 1e8, 9e8, 5e8, 5e8],
    })


def fake_store():
    class S:
        def get_sector_turnover_avg(self, *a, **k): return None
        def get_sector_prev_change(self, *a, **k): return None
        def get_sector_change_3d(self, *a, **k): return None
        def get_consecutive_days(self, *a, **k): return 0
    return S()


def make_daily(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026-07-{i % 28 + 1:02d}" for i in range(n)],
        "open": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [100000] * n})


def mock_sector(monkeypatch, composite=78.0, verdict="建议关注"):
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": verdict, "composite": composite, "consecutive_days": 0,
        "emotion": 80, "strength": 70, "risk": 10})


def test_select_sectors_filters_and_sorts(monkeypatch):
    real = an.collect_sector_metrics
    def fake(db, t, c, *a, **k):
        if c == "885559":                      # 白酒 → 观望,剔除
            return {**real(db, t, c, *a, **k), "verdict": "观望", "composite": 30.0}
        if c == "885123":                      # 化工 → 综合 90(最高)
            return {**real(db, t, c, *a, **k), "composite": 90.0}
        return real(db, t, c, *a, **k)         # 半导体 → 真实打分
    monkeypatch.setattr(an, "collect_sector_metrics", fake)
    strong = recommend.select_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                      datetime.datetime(2026, 8, 11, 15, 0))
    codes = [x["code"] for x in strong]
    assert codes == ["885123", "885887"]     # 观望被剔除;按 composite 降序
    assert all(x["verdict"] in ("建议关注", "跟踪(热点延续)") for x in strong)


def test_filter_candidates_rules():
    codes = ["600050", "600100", "600519", "688981", "300750", "999999"]
    kept, not_in = recommend.filter_candidates(codes, make_spot(), exclude_codes={"688981"})
    kept_codes = {k["code"] for k in kept}
    assert "600100" not in kept_codes        # 停牌 volume=0
    assert "600519" not in kept_codes        # ST
    assert "688981" not in kept_codes        # 新股(排除集)
    assert "999999" not in kept_codes        # 不在 spot
    assert not_in == 1
    assert kept_codes == {"600050", "300750"}   # 300750 涨幅0/量正常 → 保留;无涨停/大跌


def test_rank_candidates():
    scored = [
        {"code": "a", "scores": {"composite": 80, "risk": 10}, "verdict": "关注"},
        {"code": "b", "scores": {"composite": 90, "risk": 80}, "verdict": "规避"},
        {"code": "c", "scores": {"composite": 70, "risk": 20}, "verdict": "持有/跟踪"},
        {"code": "d", "scores": {"composite": 60, "risk": 30}, "verdict": "观望"},
    ]
    ranked = recommend.rank_candidates(scored, 2)
    assert [x["code"] for x in ranked] == ["a", "c"]   # b 规避/高险剔除;按 composite 降序取 2


def test_build_recommend_happy_path(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "600100", "600519"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10 + i for i in range(30)]), False))
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=2, per_sector=5)
    assert stale is False
    assert payload["strong_count"] == 3        # 三个板块都被 mock 成 建议关注
    assert len(payload["sectors"]) == 2        # top_sectors=2 截断
    s0 = payload["sectors"][0]
    assert s0["match_type"] == "manual" and s0["constituent_source"] == "电子信息"
    # 成分股:600050 保留、600100 停牌剔除、600519 ST 剔除
    assert [x["code"] for x in s0["stocks"]] == ["sh600050"]
    assert "price" in s0["stocks"][0] and "change_pct" in s0["stocks"][0]
    assert payload["diagnostics"]["stocks_not_in_spot"] == 0
    assert payload["diagnostics"]["stocks_daily_failed"] == 0


def test_build_recommend_stale_aggregation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (make_daily([10 + i for i in range(30)]), True))  # 候选 stale
    payload, stale = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    assert stale is True                        # 任一候选股 stale → 整包 stale


def test_build_recommend_mapping_failure_skipped_with_score(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": False, "reason": "no_mapping"})
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=3, per_sector=5)
    assert payload["sectors"] == []
    assert payload["skipped_sectors"][0]["reason"] == "no_mapping"
    assert payload["skipped_sectors"][0]["composite_score"] == 78.0   # skipped 带分


def test_build_recommend_not_in_spot_diagnostics(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["999999"],   # 不在 spot
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily([1.0]), False))
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    assert payload["diagnostics"]["stocks_not_in_spot"] == 1
    # 候选全被过滤 → 板块降级进 skipped(too_few)
    assert payload["sectors"] == []
    assert payload["skipped_sectors"][0]["reason"] == "too_few"


def test_build_recommend_request_counts(monkeypatch):
    mock_sector(monkeypatch)
    counts = {"daily": 0, "cons": 0, "new": 0}
    monkeypatch.setattr(ds, "get_new_stocks", lambda: (counts.__setitem__("new", counts["new"] + 1), set())[1])
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: (counts.__setitem__("cons", counts["cons"] + 1),
                                      {"ok": True, "codes": ["600050"], "match_type": "manual",
                                       "source_name": "电子信息"})[1])
    monkeypatch.setattr(ds, "get_stock_daily",
                        lambda c: (counts.__setitem__("daily", counts["daily"] + 1),
                                   (make_daily([10 + i for i in range(30)]), False))[1])
    recommend.build_recommend(make_summary(), make_spot(), ":db:", "industry",
                              datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    assert counts["daily"] == 1                 # 每候选恰好 1 次
    assert counts["cons"] == 1                  # top_sectors=1 → 首个强势板块解析 1 次后截断
    assert counts["new"] == 1


def test_build_recommend_concurrent_failure_isolation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600050", "600100", "300750"],
                                      "match_type": "manual", "source_name": "电子信息"})
    import time
    def flaky(c):
        time.sleep(0.02)
        if c == "300750":                      # 300750 通过硬过滤,日线拉取失败
            raise ds.DataSourceError("boom")
        return make_daily([10 + i for i in range(30)]), False
    monkeypatch.setattr(ds, "get_stock_daily", flaky)
    payload, _ = recommend.build_recommend(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), top_sectors=1, per_sector=5)
    s0 = payload["sectors"][0]
    codes = [x["code"] for x in s0["stocks"]]
    assert codes == ["sh600050"]                # 单股失败,同板块其余不受影响
    assert payload["diagnostics"]["stocks_daily_failed"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'recommend'`)

- [ ] **Step 3: 创建 `recommend.py`**

```python
# -*- coding: utf-8 -*-
"""推荐层:板块→个股双层推荐。select/filter/rank 为纯函数,build_recommend 编排。"""
from concurrent.futures import ThreadPoolExecutor, as_completed

import analysis as an
import data_source as ds
import store

MAX_WORKERS = 8
MIN_AMOUNT = 1e8            # 流动性下限:1 亿元
BIG_DROP_PCT = -7.0         # 大跌排除线(不设对称跌停,创业板/科创板 -7 非跌停)
QUALIFYING_VERDICTS = ("建议关注", "跟踪(热点延续)")


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _market_turnover(spot_df):
    return float(spot_df["amount"].sum()) if len(spot_df) else 0.0


def select_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """对全部板块用公共打分函数打分,返回强势板块(verdict 入选)按 composite 降序。"""
    rows = []
    for _, r in summary_df.iterrows():
        scores = an.collect_sector_metrics(
            db, type_key, str(r["code"]), _num(r["change_pct"]), _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
            store_ctx, market_turnover, now, True)
        rows.append({"code": str(r["code"]), "name": str(r["name"]),
                     "composite": scores["composite"], "verdict": scores["verdict"],
                     "scores": scores})
    strong = [x for x in rows if x["verdict"] in QUALIFYING_VERDICTS]
    strong.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
    return strong


def filter_candidates(codes, spot_df, exclude_codes, min_amount=MIN_AMOUNT):
    """用全市场 spot 硬过滤成分股(零额外请求)。返回 (kept_spot_rows, not_in_spot_count)。"""
    spot = {str(r["code"]): r for r in spot_df.to_dict("records")}
    kept, not_in_spot = [], 0
    for code in codes:
        row = spot.get(str(code))
        if row is None:
            not_in_spot += 1                      # 北交所/新上市/退市整理 → 跳过并计数
            continue
        if "ST" in str(row["name"] or "").upper():
            continue
        if str(code) in exclude_codes:
            continue
        price = _num(row["price"])
        vol = _num(row["volume"])
        chg = _num(row["change_pct"])
        if not price or not vol or chg is None:
            continue                              # 停牌
        if chg >= an.limit_threshold(str(code)):
            continue                              # 涨停买不进
        if chg <= BIG_DROP_PCT:
            continue                              # 大跌
        amt = _num(row["amount"])
        if amt is not None and amt < min_amount:
            continue                              # 流动性不足
        kept.append(row)
    return kept, not_in_spot


def rank_candidates(scored, per_sector):
    """剔除规避/高风险,按 composite 降序取前 per_sector。"""
    kept = [x for x in scored if x["verdict"] != "规避" and x["scores"]["risk"] < 70]
    kept.sort(key=lambda x: x["scores"]["composite"], reverse=True)
    return kept[:per_sector]


def _score_candidate(row, daily_df, now):
    quote = {"price": _num(row["price"]), "change_pct": _num(row["change_pct"]),
             "volume": _num(row["volume"]), "amount": _num(row["amount"])}
    scores = an.score_stock(daily_df, quote, now)
    return {"code": ds.with_prefix(str(row["code"])), "name": str(row["name"]),
            "price": quote["price"], "change_pct": quote["change_pct"],
            "scores": scores, "verdict": scores["verdict"]}


def _score_sector_stocks(spot_rows, get_daily, now, per_sector):
    """并发拉日线并打分。返回 (ranked, daily_failed, any_stale)。"""
    ranked, daily_failed, any_stale = [], 0, False
    if not spot_rows:
        return [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_daily, str(r["code"])): r for r in spot_rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                daily, stale = fut.result()
                any_stale = any_stale or bool(stale)
                ranked.append(_score_candidate(row, daily, now))
            except Exception:
                daily_failed += 1                # 单只失败 → 跳过,同板块其余继续
    return rank_candidates(ranked, per_sector), daily_failed, any_stale


def build_recommend(summary_df, spot_df, db, type_key, now, top_sectors=3, per_sector=5):
    """编排:选板块 → 解析成分股 → 并发打分 → 组装。返回 (payload, stale_any)。"""
    strong = select_sectors(summary_df, db, type_key, store, _market_turnover(spot_df), now)
    new_codes = ds.get_new_stocks()
    sectors, skipped = [], []
    stale_any = False
    diagnostics = {"stocks_not_in_spot": 0, "stocks_daily_failed": 0}
    for s in strong:
        if len(sectors) >= top_sectors:
            break
        try:
            res = ds.resolve_sector_constituents(s["name"])
        except Exception:                        # 网络/模式失败 → 降级跳过
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": "source_fail"})
            continue
        if not res["ok"]:
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": res["reason"]})
            continue
        kept, not_in_spot = filter_candidates(res["codes"], spot_df, new_codes)
        diagnostics["stocks_not_in_spot"] += not_in_spot
        ranked, daily_failed, any_stale = _score_sector_stocks(kept, ds.get_stock_daily, now, per_sector)
        diagnostics["stocks_daily_failed"] += daily_failed
        stale_any = stale_any or any_stale
        if not ranked:
            skipped.append({"name": s["name"], "verdict": s["verdict"],
                            "composite_score": s["composite"], "reason": "too_few"})
            continue
        sectors.append({
            "code": "%s:%s" % (type_key, s["code"]), "name": s["name"],
            "verdict": s["verdict"], "composite_score": s["composite"],
            "match_type": res["match_type"], "constituent_source": res["source_name"],
            "stocks": [{"code": x["code"], "name": x["name"], "price": x["price"],
                        "change_pct": x["change_pct"], "scores": x["scores"],
                        "verdict": x["verdict"]} for x in ranked],
        })
    return ({"strong_count": len(strong), "sectors": sectors,
             "skipped_sectors": skipped, "diagnostics": diagnostics},
            stale_any)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "feat(recommend): 板块→个股双层推荐(select/filter/rank 纯函数 + build_recommend 编排)

复用公共板块打分与 score_stock;spot 硬过滤零额外请求;日线并发拉取;
skipped 带分、diagnostics 计数、stale 聚合(规格 §3/§4/§8)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: `app.py` —— `/api/recommend` 路由 + `create_app` 启动校验 + 测试 fixture

**Files:**
- Modify: `app.py`(`import logging`、`import recommend`、`ok()` 支持 extra_meta、新增路由、`create_app` 调 `validate_sector_map`)
- Modify: `tests/test_api.py`(client fixture 加假校验 + 成分股 mock;`test_source_fail_returns_500`/`test_sector_stale_propagates`/`test_stock_stale_propagates` 三个 create_app 前加假校验 mock)
- Test: `tests/test_api.py`(新增 /api/recommend 用例)

**Interfaces:**
- Consumes:`recommend.build_recommend`、`ds.validate_sector_map`。
- Produces:`GET /api/recommend?top_sectors=3&per_sector=5` → `{ok, meta:{stale, updated_at, coverage, mapping_health}, data:{generated_at, sectors, skipped_sectors, diagnostics}}`。

- [ ] **Step 1: 更新测试 fixture 并写失败测试**

`tests/test_api.py` 的 `client` fixture:在 `app = app_mod.create_app(db_path=db)` **之前**加一行假校验 mock,并补 `resolve_sector_constituents` mock。**不要**在共享 fixture 里 mock `an.collect_sector_metrics`(保留既有板块接口对真实打分路径的回归覆盖;recommend 用例各自局部 mock):

```python
@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_index_realtime", lambda: (
        [{"code": "sh000001", "name": "上证指数", "price": 3456.78, "change_pct": 0.45}], False))
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), False))
    monkeypatch.setattr(ds, "get_sector_index_history", lambda c, t: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), False))
    monkeypatch.setattr(ds, "get_stock_minute", lambda c: (make_minute(), False))
    monkeypatch.setattr(ds, "get_stock_quote", lambda c: (make_quote(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600519", "600000"],
                                      "match_type": "manual", "source_name": "电子信息"})
    monkeypatch.setattr(an, "is_after_close", lambda now: True)
    monkeypatch.setattr(an, "is_trading_time", lambda now: True)
    app.config["TESTING"] = True
    return app.test_client()
```

在文件末尾新增:

```python
def test_recommend_endpoint(client, monkeypatch):
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    r = client.get("/api/recommend")
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["generated_at"]
    # make_summary 两个板块都被 mock 成 建议关注 → 2 个 sector
    assert len(d["sectors"]) == 2
    s0 = d["sectors"][0]
    assert s0["match_type"] == "manual"
    assert s0["stocks"] and "price" in s0["stocks"][0] and "change_pct" in s0["stocks"][0]
    assert d["diagnostics"] == {"stocks_not_in_spot": 0, "stocks_daily_failed": 0}
    meta = body["meta"]
    assert meta["coverage"]["mapped"] == 2
    assert meta["mapping_health"]["ok"] is True


def test_recommend_bad_param(client):
    assert client.get("/api/recommend?top_sectors=abc").status_code == 400
    r = client.get("/api/recommend?top_sectors=9&per_sector=0")
    d = r.get_json()["data"]
    assert len(d["sectors"]) <= 5    # top_sectors 被钳制到 [1,5],per_sector 钳制到 [1,10]


def test_recommend_stale_propagates(monkeypatch, tmp_path):
    db = str(tmp_path / "reco_stale.db")
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
    app = app_mod.create_app(db_path=db)
    monkeypatch.setattr(ds, "get_sector_summary", lambda t: (make_summary(), True))   # 摘要 stale
    monkeypatch.setattr(ds, "get_market_spot", lambda: (make_spot(), False))
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": True, "codes": ["600519"], "match_type": "manual",
                                      "source_name": "电子信息"})
    monkeypatch.setattr(an, "collect_sector_metrics", lambda *a, **k: {
        "verdict": "建议关注", "composite": 78.0, "consecutive_days": 1,
        "emotion": 80, "strength": 70, "risk": 10})
    monkeypatch.setattr(ds, "get_stock_daily", lambda c: (make_daily(), True))        # 候选 stale
    app.config["TESTING"] = True
    r = app.test_client().get("/api/recommend")
    assert r.get_json()["meta"]["stale"] is True     # 任一候选 stale → 整包 stale
```

并给 `test_source_fail_returns_500`、`test_sector_stale_propagates`、`test_stock_stale_propagates` 三个用例在各自 `app_mod.create_app(db_path=...)` **之前**加同样的 `validate_sector_map` mock 行:

```python
    monkeypatch.setattr(ds, "validate_sector_map",
                        lambda: {"ok": True, "total": 0, "valid": 0, "stale": [], "renamed": []})
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL(`test_recommend_endpoint` → 404,`/api/recommend` 未定义)

- [ ] **Step 3: 实现 `app.py`**

顶部 import 增加:

```python
import logging
import recommend
```

`ok()` 增加 `extra_meta`:

```python
def ok(data, stale=False, extra_meta=None):
    meta = {"stale": stale, "updated_at": ds.last_updated_at}
    if extra_meta:
        meta.update(extra_meta)
    return jsonify({"ok": True, "meta": meta, "data": data})
```

在 `api_stock` 之后、`register_routes` 结束前新增:

```python
    @app.route("/api/recommend")
    def api_recommend():
        try:
            top_sectors = int(request.args.get("top_sectors", "3"))
            per_sector = int(request.args.get("per_sector", "5"))
        except ValueError:
            return err("BAD_PARAM", "top_sectors/per_sector 必须为整数", 400)
        top_sectors = max(1, min(5, top_sectors))
        per_sector = max(1, min(10, per_sector))
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        payload, stale_cands = recommend.build_recommend(
            summary, spot, db_path, "industry", now, top_sectors, per_sector)
        coverage = {
            "strong_candidates": payload["strong_count"],
            "mapped": len(payload["sectors"]),
            "skipped": len(payload["skipped_sectors"]),
            "skipped_by_reason": {},
        }
        for s in payload["skipped_sectors"]:
            r = s["reason"]
            coverage["skipped_by_reason"][r] = coverage["skipped_by_reason"].get(r, 0) + 1
        return ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sectors": payload["sectors"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})
```

`create_app` 增加启动校验:

```python
def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB"] = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB"])), exist_ok=True)
    store.init_db(app.config["DB"])
    register_routes(app)
    health = ds.validate_sector_map()
    app.config["SECTOR_MAP_HEALTH"] = health
    if not health.get("ok") or health.get("stale") or health.get("renamed"):
        logging.getLogger(__name__).warning("sector map health: %s", health)
    return app
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add app.py tests/test_api.py
git commit -m "feat(api): /api/recommend 双层推荐路由 + create_app 启动映射校验

meta 含 coverage/mapping_health;stale=any(各源+候选);参数钳制;
测试 fixture 注入假校验,离线不发新浪请求(规格 §6/§8/§10)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端「推荐」Tab

**Files:**
- Modify: `templates/index.html`、`static/app.js`、`static/style.css`

**Interfaces:**
- Consumes:`GET /api/recommend` 响应(见 Task 5)。点击推荐个股行 → 复用 `openStock(code)`。

- [ ] **Step 1: `index.html` 左栏加「推荐」Tab 与推荐容器**

将左栏 `#left`(现 `index.html:28-38`)改为:

```html
    <div class="tabs">
      <button class="tab active" data-type="industry" data-view="sectors">行业板块</button>
      <button class="tab" data-view="recommend">推荐</button>
    </div>
    <div id="sector-view">
      <div class="sector-tools">
        <input id="sector-search" placeholder="板块搜索,如 半导">
        <button id="btn-sector-search">搜索</button>
      </div>
      <table id="sector-table">
        <thead><tr><th>板块</th><th>涨幅%</th><th>情绪</th><th>强度</th><th>风险</th><th>综合</th><th>结论</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div id="reco-panel" class="hidden">
      <div id="reco-disclaimer" class="muted">仅供研究参考,不构成投资建议。</div>
      <div id="reco-coverage" class="muted"></div>
      <div id="reco-sectors"></div>
      <div id="reco-skipped" class="muted"></div>
    </div>
```

- [ ] **Step 2: `app.js` 加 `state.view`、Tab 切换、`loadRecommend`/`renderRecommend`**

`state` 对象(现 `app.js:1-7`)加 `view: "sectors"`。替换 Tab 绑定逻辑(现 `app.js:220-226`)与 `refreshAll`(现 `app.js:199-208`):

```js
function renderRecommend(b) {
  const d = b.data, m = b.meta;
  const cov = m.coverage || {};
  $("#reco-coverage").innerHTML = cov.strong_candidates != null
    ? `今日强势板块 ${cov.strong_candidates} 个,已覆盖 ${cov.mapped} 个,跳过 ${cov.skipped} 个`
    : "";
  $("#reco-sectors").innerHTML = d.sectors.length ? d.sectors.map((s) => `
    <div class="reco-sector panel">
      <div class="reco-sector-head">
        <b>${s.name}</b>
        <span class="verdict">${s.verdict}</span>
        <span class="muted">综合 ${s.composite_score == null ? "…" : s.composite_score.toFixed(2)}</span>
        <span class="muted">成分股:${s.constituent_source}${s.match_type === "keyword" ? "[关键词]" : ""}</span>
      </div>
      <table class="reco-table">
        <thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>综合</th><th>风险</th><th>结论</th></tr></thead>
        <tbody>${s.stocks.map((x) => {
          const chg = x.change_pct;
          return `<tr class="reco-row" data-code="${x.code}">
            <td>${x.code}</td><td>${x.name}</td>
            <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
            <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
            <td>${x.scores.composite == null ? "…" : x.scores.composite.toFixed(2)}</td>
            <td>${x.scores.risk}</td>
            <td class="verdict">${x.verdict}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`).join("") : "<div class='muted'>今日无强势板块</div>";
  $("#reco-skipped").innerHTML = d.skipped_sectors.length
    ? "被跳过(可补映射): " + d.skipped_sectors.map((s) =>
        `${s.name}(${s.composite_score == null ? "…" : s.composite_score.toFixed(2)},${s.reason})`).join(" | ")
    : "";
  document.querySelectorAll("#reco-sectors tr.reco-row").forEach((tr) =>
    tr.addEventListener("click", () => openStock(tr.dataset.code)));
}

async function loadRecommend() {
  const b = await api("/api/recommend?top_sectors=3&per_sector=5");
  renderRecommend(b);
}

function switchView(view) {
  state.view = view;
  $("#sector-view").classList.toggle("hidden", view !== "sectors");
  $("#reco-panel").classList.toggle("hidden", view !== "recommend");
}

// ---- 刷新 ----
async function refreshAll() {
  try { await loadMarket(); } catch (e) { $("#stale-flag").classList.remove("hidden"); }
  try { await loadSectors(); } catch (e) { /* 沿用旧列表 */ }
  if (state.view === "recommend") { try { await loadRecommend(); } catch (e) { /* 沿用旧 */ } }
  if (state.current) {
    try {
      if (state.current.kind === "sector") await openSector(state.current.code);
      else await openStock(state.current.code);
    } catch (e) { /* 保留当前 */ }
  }
}

// ---- 交互绑定 ----
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const view = t.dataset.view || "sectors";
    switchView(view);
    if (view === "recommend") loadRecommend().catch(() => { /* 沿用旧 */ });
    else { state.type = t.dataset.type || "industry"; loadSectors(); }
  }));
```

- [ ] **Step 3: `style.css` 加推荐样式**

在 `style.css` 末尾追加:

```css
#reco-disclaimer { margin-bottom: 8px; }
#reco-coverage { margin-bottom: 8px; }
#reco-skipped { margin-top: 8px; }
.reco-sector { margin-bottom: 12px; }
.reco-sector-head { display: flex; gap: 12px; align-items: baseline; margin-bottom: 6px; }
.reco-table { font-size: 13px; }
tr.reco-row { cursor: pointer; }
tr.reco-row:hover { background: var(--bg); }
```

- [ ] **Step 4: 冒烟验证**

Run: 启动 `python app.py`,浏览器打开 `http://127.0.0.1:8000`,点击左栏「推荐」Tab。
Expected: 推荐区渲染板块卡片 + 成分股表(现价/涨跌幅列);点击个股行跳转右侧个股详情;无 JS 报错;60s 自动刷新时推荐区刷新。
(需联网;若某板块未映射,底部「被跳过」灰字带分显示。)

- [ ] **Step 5: 提交**

```bash
git add templates/index.html static/app.js static/style.css
git commit -m "feat(web): 新增「推荐」Tab,展示板块→个股双层推荐

板块卡片含 verdict/综合分/成分股源口径(关键词映射显式标注);
成分股表含现价/涨跌幅,点击跳转个股详情;skipped 带分展示(规格 §7)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: `tools/check_sector_map.py` —— 映射覆盖度维护脚本

**Files:**
- Create: `tools/check_sector_map.py`

**Interfaces:**
- Consumes:`data_source.SECTOR_CONS_MAP`、`data_source._sina_industry_names`、`data_source._keyword_lookup`。
- Produces:覆盖度报表;存在未映射板块时退出码 1。

- [ ] **Step 1: 创建脚本**

```python
# -*- coding: utf-8 -*-
"""离线检查板块映射:THS 板块名 → 新浪行业成分股源的覆盖度与 label 有效性。
用法:python tools/check_sector_map.py [--json]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
from data_source import SECTOR_CONS_MAP, SECTOR_CONS_EXPECTED, _keyword_lookup, _sina_industry_names


def main():
    names, _ = _sina_industry_names()          # label → 新浪名
    ths_raw = ak.stock_board_industry_name_ths()
    ths_names = sorted(set(str(x) for x in ths_raw["name"]))
    manual = [n for n in ths_names if n in SECTOR_CONS_MAP]
    kw = [n for n in ths_names if n not in SECTOR_CONS_MAP and _keyword_lookup(n, names) is not None]
    unmapped = [n for n in ths_names if n not in manual and n not in kw]
    stale = [(ths, label) for ths, label in SECTOR_CONS_MAP.items() if label not in names]
    renamed = [(ths, label) for ths, label in SECTOR_CONS_MAP.items()
               if label in names and SECTOR_CONS_EXPECTED.get(label)
               and names[label] != SECTOR_CONS_EXPECTED[label]]
    print("THS 板块总数: %d" % len(ths_names))
    print("手动映射: %d  关键词兜底: %d  未映射: %d" % (len(manual), len(kw), len(unmapped)))
    print("未映射:", " | ".join(unmapped) if unmapped else "(无)")
    print("失效 label:", stale if stale else "(无)")
    print("改名漂移:", renamed if renamed else "(无)")
    if unmapped or stale or renamed:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 联网运行验证覆盖度**

Run: `python tools/check_sector_map.py`
Expected: 输出覆盖度与未映射清单;`echo $?` 为 1(存在未映射属预期,用于提醒补表)。记录输出供 README 参考。

- [ ] **Step 3: 提交**

```bash
git add tools/check_sector_map.py
git commit -m "feat(tools): check_sector_map.py 映射覆盖度/漂移离线检查

统计手动/关键词/未映射三级覆盖 + label 有效性 + 改名漂移,未映射非零退出码 1(规格 §5.4)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 8: 端到端验收

**Files:** 无(验证)

- [ ] **Step 1: 全量测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 2: 启动看板做端到端冒烟**

Run: `python app.py`,浏览器依次验证:
1. 大盘/板块/个股三个既有 Tab 行为不变(回归);
2. 「推荐」Tab:板块卡片(verdict/综合分/成分股源口径)→ 成分股表(现价/涨跌幅)→ 点击个股跳转详情;
3. `curl "http://127.0.0.1:8000/api/recommend"` 检查 `coverage`/`mapping_health`/`diagnostics` 结构。

Expected: 全部符合规格 §6/§7 输出。

- [ ] **Step 3: 更新 README**

在 `README.md` 功能清单加一条:

```markdown
- **股票推荐**:板块→个股双层推荐(THS 强势板块 + 成分股经映射表转新浪行业,Top 5);含成分股源口径标注、被跳过板块带分透明化、映射覆盖度/漂移检查(`tools/check_sector_map.py`)。
```

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs(README): 补充股票推荐功能说明

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review 备注

- §4.0 公共打分:Task 1;§4.1/4.2/4.3 筛选:Task 4。
- §5.1 两级映射、§5.3 校验、§5.4 覆盖度:Task 3 + Task 7。
- §6 API/§8 stale 聚合与并发:Task 5;§7 前端:Task 6。
- §10 测试:Task 1(打分一致性)、Task 3(映射)、Task 4(请求数/并发/stale/诊断)、Task 5(fixture 假校验 + API)。
- 规格 §5.2 的 `reason` 枚举中,`stale_map`(启动校验发现失效 label 后由上层置)与 `source_fail` 的区别:本计划中启动校验只做报告(非阻塞),运行时失效 label 经 `_fetch_sina_constituents`/`_sina_industry_names` 抛 `DataSourceError` → 归入 `source_fail`;`stale_map` 保留在枚举中供未来将 `validate_sector_map().stale` 注入 `resolve` 时使用,当前不产出。
- 规格 §8 的「成分股不在 spot 快照 → 跳过并计入诊断」在 `filter_candidates` 实现;「整板块候选全失败 → 板块降级」在 `build_recommend` 的 `too_few` 分支实现。
