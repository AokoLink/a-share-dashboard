# 「可介入龙头」全局视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 左侧面板新增第三标签页「可介入龙头」,聚合全部已映射板块的龙头/强势股,按个股评价过滤为「可介入/观察」两档,一张表看完当前可操作标的。

**Architecture:** 复用 recommend.py 已有机制(全板块打分 `select_sectors`、龙头池 `pick_leaders`、并发打分 `_score_candidate`/`ThreadPoolExecutor`),新增纯函数 `tier_for_verdict`(评价→档位)、`bias_pct`(现价偏离 MA20)、`score_all_sectors`(不过滤的板块打分,从 `select_sectors` 提取)、编排函数 `collect_actionable_leaders`(返回 payload)。app.py 新增 `/api/actionable-leaders` 路由(镜像 `/api/recommend` 的 stale/coverage/mapping_health 模式)。前端新增第三个 tab 与 `#actionable-panel`。

**Tech Stack:** Python 3 / Flask / pandas / ThreadPoolExecutor;前端原生 JS + ECharts,无新依赖。

## Global Constraints

- 数据源仅 Sina/Tencent/THS:复用 `resolve_sector_constituents`(THS→新浪映射),无新数据源。
- 所有测试离线可跑:resolve/daily/spot/summary 全部 mock。
- 数值字段一律经 `_num()` 归一无;测试对经 `round(...,2)` 的分数断言用精确字面量或 `abs=0.01`。
- 板块打分口径以 `analysis.sector_verdict` 为准(展示列用 `sector_verdict` 原串)。
- 直接在 `main` 分支工作,每任务独立 commit;commit 信息以 `git commit -m` 附 `Co-Authored-By: Claude <noreply@anthropic.com>` 结尾。
- 用户确认「不过滤板块」:所有已映射板块均纳入,板块评价仅作展示列。

---

### Task 1: recommend.py 纯函数 `tier_for_verdict` / `bias_pct` / `score_all_sectors` 重构

**Files:**
- Modify: `recommend.py:29-42`(select_sectors → 提取 score_all_sectors)
- Modify: `recommend.py`(append `tier_for_verdict`、`bias_pct`,放在 `pick_leaders` 之后)
- Test: `tests/test_recommend.py`(append 三个测试)

**Interfaces:**
- Produces:
  - `recommend.score_all_sectors(summary_df, db, type_key, store_ctx, market_turnover, now) → list[{code,name,composite,verdict,scores}]`,全板块不过滤,按 composite 降序。
  - `recommend.select_sectors(...)` 重构为调用 `score_all_sectors` 后过滤 `QUALIFYING_VERDICTS`(行为不变,既有测试保护)。
  - `recommend.tier_for_verdict(verdict) → "可介入" | "观察" | None`。
  - `recommend.bias_pct(daily_df, price) → float | None`。

- [ ] **Step 1: 在 `tests/test_recommend.py` 末尾追加三个测试(先写,验证失败)**

```python
def test_score_all_sectors_returns_all_sorted(monkeypatch):
    real = an.collect_sector_metrics
    def fake(db, t, c, *a, **k):
        if c == "885559":                      # 白酒 → 观望,但 score_all_sectors 仍返回
            return {**real(db, t, c, *a, **k), "verdict": "观望", "composite": 30.0}
        return real(db, t, c, *a, **k)         # 半导体/化工 → 真实打分
    monkeypatch.setattr(an, "collect_sector_metrics", fake)
    all_rows = recommend.score_all_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                           datetime.datetime(2026, 8, 11, 15, 0))
    codes = [x["code"] for x in all_rows]
    assert codes[0] == "885887"                                   # 真实打分(consecutive=2)最高,居首
    assert set(codes) == {"885887", "885559", "885123"}           # 全板块都返回(不过滤)
    strong = recommend.select_sectors(make_summary(), ":db:", "industry", fake_store(), 1e12,
                                      datetime.datetime(2026, 8, 11, 15, 0))
    assert [x["code"] for x in strong] == ["885887"]    # select_sectors 仍过滤(白酒/化工观望)


def test_tier_for_verdict():
    assert recommend.tier_for_verdict("关注") == "可介入"
    assert recommend.tier_for_verdict("持有/跟踪") == "观察"
    for v in ("观望", "回调风险", "规避"):
        assert recommend.tier_for_verdict(v) is None
    assert recommend.tier_for_verdict(None) is None


def test_bias_pct():
    daily = make_daily([10 + i for i in range(30)])   # 收盘 10..39,末位 MA20 = 29.5
    assert abs(recommend.bias_pct(daily, 33.0) - 11.86) < 0.01
    assert abs(recommend.bias_pct(daily, 29.5)) < 0.001
    assert recommend.bias_pct(make_daily([1.0] * 10), 1.0) is None   # 不足 20 根 → 无 MA20
    assert recommend.bias_pct(pd.DataFrame(), 10.0) is None          # 空 df
    assert recommend.bias_pct(daily, None) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recommend.py::test_score_all_sectors_returns_all_sorted tests/test_recommend.py::test_tier_for_verdict tests/test_recommend.py::test_bias_pct -v`
Expected: FAIL — `score_all_sectors` 未定义 / `tier_for_verdict` 未定义 / `bias_pct` 未定义。

- [ ] **Step 3: 重构 `recommend.py` 的 `select_sectors`(替换 29-42 行)**

```python
def score_all_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """对全部板块用公共打分函数打分,按 composite 降序返回(不过滤)。"""
    rows = []
    for _, r in summary_df.iterrows():
        scores = an.collect_sector_metrics(
            db, type_key, str(r["code"]), _num(r["change_pct"]), _num(r["turnover"]),
            _num(r["up_count"]), _num(r["down_count"]), _num(r["leader_change_pct"]),
            store_ctx, market_turnover, now, True)
        rows.append({"code": str(r["code"]), "name": str(r["name"]),
                     "composite": scores["composite"], "verdict": scores["verdict"],
                     "scores": scores})
    rows.sort(key=lambda x: (x["composite"] is None, -(x["composite"] or 0)))
    return rows


def select_sectors(summary_df, db, type_key, store_ctx, market_turnover, now):
    """返回强势板块(verdict 入选)按 composite 降序。"""
    strong = [x for x in score_all_sectors(summary_df, db, type_key, store_ctx, market_turnover, now)
              if x["verdict"] in QUALIFYING_VERDICTS]
    return strong
```

- [ ] **Step 4: 在 `pick_leaders` 之后追加两个纯函数**

```python
def tier_for_verdict(verdict):
    """个股评价 → 可介入档位:关注→可介入,持有/跟踪→观察,其余→None(规避/回调风险/观望排除)。"""
    if verdict == "关注":
        return "可介入"
    if verdict == "持有/跟踪":
        return "观察"
    return None


def bias_pct(daily_df, price):
    """现价偏离 MA20 百分比;(price - ma20)/ma20*100。MA20 缺失 → None。"""
    price = _num(price)
    if price is None:
        return None
    df = an.add_ma(daily_df, (20,))
    ma20 = _num(df["ma20"].iloc[-1]) if len(df) else None
    if not ma20:
        return None
    return round((price - ma20) / ma20 * 100, 2)
```

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_recommend.py -v`
Expected: 全部 PASS(含既有 `test_select_sectors_filters_and_sorts` 等,重构无回归)。

- [ ] **Step 6: Commit**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "$(cat <<'EOF'
feat(recommend): 可介入龙头 tier_for_verdict/bias_pct + score_all_sectors 重构

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `collect_actionable_leaders` 编排 + `/api/actionable-leaders` 路由

**Files:**
- Modify: `recommend.py`(append `collect_actionable_leaders` 于文件末尾 `build_recommend` 之后)
- Modify: `app.py:266`(`api_recommend` 之后插入新路由)
- Modify: `tests/test_api.py:12`(imports 加 `import recommend`)
- Test: `tests/test_recommend.py` + `tests/test_api.py`

**Interfaces:**
- Consumes: `recommend.score_all_sectors`、`recommend.pick_leaders`、`recommend._score_candidate`、`recommend.tier_for_verdict`、`recommend.bias_pct`、`recommend._market_turnover`、`recommend.MAX_WORKERS`(Task 1 + 既有)。
- Produces:
  - `recommend.collect_actionable_leaders(summary_df, spot_df, db, type_key, now, resolve_fn, get_daily_fn) → (payload, stale_any)`
    - `payload = {"sectors_scanned", "total", "items", "skipped_sectors", "diagnostics"}`
    - item: `{code, name, price, change_pct, tag, tier, sector_code, sector_name, sector_verdict, sector_composite, trend, volume_price, signal, composite, risk, bias_pct}`
  - 路由 `GET /api/actionable-leaders` → `ok({generated_at, total, items, skipped_sectors, diagnostics}, stale=..., extra_meta={coverage, mapping_health})`。

- [ ] **Step 1: 在 `tests/test_recommend.py` 末尾追加四个编排测试(先写,验证失败)**

```python
def test_collect_actionable_leaders_two_tiers_and_dedupe(monkeypatch):
    mock_sector(monkeypatch)                       # 3 板块全部 建议关注/composite 78
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    by_code = {
        "600050": {"trend": 100, "vp": 90, "signal": 80, "risk": 0},    # 综合 91.5 → 关注 → 可介入
        "688981": {"trend": 60, "vp": 60, "signal": 50, "risk": 10},    # 综合 57.5 → 持有/跟踪 → 观察
    }
    def fake_score_candidate(row, daily_df, now):
        c = str(row["code"])
        s = by_code[c]
        composite = an.stock_composite(s["trend"], s["vp"], s["signal"])
        return {"code": ds.with_prefix(c), "name": str(row["name"]),
                "price": row["price"], "change_pct": row["change_pct"],
                "scores": {"trend": s["trend"], "volume_price": s["vp"], "signal": s["signal"],
                           "risk": s["risk"], "composite": composite},
                "verdict": an.stock_verdict(composite, s["risk"])}
    monkeypatch.setattr(recommend, "_score_candidate", fake_score_candidate)
    daily = make_daily([10 + i for i in range(30)])
    payload, stale = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0),
        lambda name: {"ok": True, "codes": ["600050", "600100", "688981"],
                      "match_type": "manual", "source_name": "电子信息"},
        lambda c: (daily, False))
    assert stale is False
    assert payload["sectors_scanned"] == 3
    assert payload["skipped_sectors"] == []
    # 600100 停牌(volume=0)→ pick_leaders 排除;600050/688981 为各板块龙头
    # 三板块 composite 相同(78)→ 按板块序先到先得,保留首个板块(半导体)那条
    assert [x["code"] for x in payload["items"]] == ["sh600050", "sh688981"]
    assert [x["tier"] for x in payload["items"]] == ["可介入", "观察"]
    assert payload["items"][0]["sector_name"] == "半导体"
    assert payload["items"][0]["tag"] == "龙头+强势"
    assert payload["items"][0]["composite"] == 91.5
    assert payload["items"][0]["bias_pct"] is not None     # bias 用真实 daily(现价 5.0 vs MA20 29.5)
    assert payload["diagnostics"]["stocks_daily_failed"] == 0


def test_collect_actionable_leaders_resolve_failure_skips(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())

    def resolve(name):
        if name == "白酒":
            return {"ok": False, "reason": "no_mapping"}
        if name == "半导体":
            raise ds.DataSourceError("boom")     # 网络异常 → source_fail
        return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "食品饮料"}
    payload, _ = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), resolve,
        lambda c: (make_daily([10 + i for i in range(30)]), False))
    reasons = sorted(s["reason"] for s in payload["skipped_sectors"])
    assert reasons == ["no_mapping", "source_fail"]
    assert payload["sectors_scanned"] == 3
    assert payload["total"] == 1                 # 化工 600050 → 关注/持有跟踪 之一
    assert payload["items"][0]["code"] == "sh600050"


def test_collect_actionable_leaders_daily_failure_skips(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())

    def resolve(name):
        if name == "半导体":
            return {"ok": True, "codes": ["600050", "688981"],
                    "match_type": "manual", "source_name": "电子信息"}
        return {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "食品饮料"}

    def flaky(c):
        if c == "688981":
            raise ds.DataSourceError("boom")
        return make_daily([10 + i for i in range(30)]), False
    payload, _ = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0), resolve, flaky)
    codes = [x["code"] for x in payload["items"]]
    assert codes == ["sh600050"]                 # 688981 日线失败被跳过,同板块其余保留
    assert payload["diagnostics"]["stocks_daily_failed"] == 1


def test_collect_actionable_leaders_stale_aggregation(monkeypatch):
    mock_sector(monkeypatch)
    monkeypatch.setattr(ds, "get_new_stocks", lambda: set())
    payload, stale = recommend.collect_actionable_leaders(
        make_summary(), make_spot(), ":db:", "industry",
        datetime.datetime(2026, 8, 11, 15, 0),
        lambda name: {"ok": True, "codes": ["600050"], "match_type": "manual", "source_name": "电子信息"},
        lambda c: (make_daily([10 + i for i in range(30)]), True))   # 候选 stale
    assert stale is True
    assert payload["items"][0]["code"] == "sh600050"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_recommend.py -v`
Expected: 上述 4 个测试 FAIL — `collect_actionable_leaders` 未定义。

- [ ] **Step 3: 在 `recommend.py` 末尾(`build_recommend` 之后)追加 `collect_actionable_leaders`**

```python
def collect_actionable_leaders(summary_df, spot_df, db, type_key, now, resolve_fn, get_daily_fn):
    """汇总全部已映射板块的龙头/强势股,过滤为可介入/观察两档。返回 (payload, stale_any)。

    resolve_fn / get_daily_fn 可注入(测试 mock),生产传 ds.resolve_sector_constituents / ds.get_stock_daily。
    板块评价仅作展示列(不过滤板块);跨板块同 code 去重按板块 composite 降序先到先得。
    """
    sectors = score_all_sectors(summary_df, db, type_key, store, _market_turnover(spot_df), now)
    new_codes = ds.get_new_stocks()
    spot_index = {str(r["code"]): r for r in spot_df.to_dict("records")}
    tasks, skipped = [], []
    for i, s in enumerate(sectors):
        try:
            res = resolve_fn(s["name"])
        except Exception:
            skipped.append({"name": s["name"], "reason": "source_fail"})
            continue
        if not res["ok"]:
            skipped.append({"name": s["name"], "reason": res.get("reason", "source_fail")})
            continue
        leaders = pick_leaders([spot_index[c] for c in res.get("codes", []) if c in spot_index],
                               total=5, exclude_codes=new_codes)
        for L in leaders:
            tasks.append((i, s, L, spot_index[L["code"]]))

    def work(i, s, L, row):
        daily, stale = get_daily_fn(str(L["code"]))
        scored = _score_candidate(row, daily, now)
        tier = tier_for_verdict(scored["verdict"])
        if tier is None:
            return None, stale
        return {
            "_rank": i,
            "code": scored["code"], "name": scored["name"],
            "price": scored["price"], "change_pct": scored["change_pct"],
            "tag": L["tag"], "tier": tier,
            "sector_code": "%s:%s" % (type_key, s["code"]),
            "sector_name": s["name"], "sector_verdict": s["verdict"],
            "sector_composite": s["composite"],
            "trend": scored["scores"]["trend"], "volume_price": scored["scores"]["volume_price"],
            "signal": scored["scores"]["signal"], "composite": scored["scores"]["composite"],
            "risk": scored["scores"]["risk"],
            "bias_pct": bias_pct(daily, scored["price"]),
        }, stale

    items, daily_failed, stale_any = [], 0, False
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(work, i, s, L, row): 1 for i, s, L, row in tasks}
        for fut in as_completed(futures):
            try:
                item, stale = fut.result()
                stale_any = stale_any or bool(stale)
                if item is not None:
                    items.append(item)
            except Exception:
                daily_failed += 1              # 单只日线/打分失败 → 跳过该股,其余继续

    # 跨板块去重:score_all_sectors 已按 composite 降序 → _rank 即板块序,先到先得
    items.sort(key=lambda x: (x["_rank"], -(x["composite"] or 0)))
    seen, unique = set(), []
    for it in items:
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        it.pop("_rank")                        # 内部字段,出参前移除
        unique.append(it)
    # 排序:可介入在前,观察在后;组内按综合分降序
    unique.sort(key=lambda x: (x["tier"] != "可介入", -(x["composite"] or 0)))
    return ({"sectors_scanned": len(sectors), "total": len(unique), "items": unique,
             "skipped_sectors": skipped,
             "diagnostics": {"stocks_daily_failed": daily_failed}},
            stale_any)
```

- [ ] **Step 4: 在 `app.py` 的 `api_recommend` 之后(266 行后)追加新路由**

```python
    @app.route("/api/actionable-leaders")
    def api_actionable_leaders():
        try:
            summary, stale1 = ds.get_sector_summary("industry")
            spot, stale2 = ds.get_market_spot()
        except ds.DataSourceError as e:
            return err("SOURCE_FAIL", str(e), 500)
        now = datetime.now()
        payload, stale_cands = recommend.collect_actionable_leaders(
            summary, spot, db_path, "industry", now,
            ds.resolve_sector_constituents, ds.get_stock_daily)
        coverage = {
            "scanned": payload["sectors_scanned"],
            "total": payload["total"],
            "skipped": len(payload["skipped_sectors"]),
            "skipped_by_reason": {},
        }
        for s in payload["skipped_sectors"]:
            r = s["reason"]
            coverage["skipped_by_reason"][r] = coverage["skipped_by_reason"].get(r, 0) + 1
        return ok({
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "total": payload["total"],
            "items": payload["items"],
            "skipped_sectors": payload["skipped_sectors"],
            "diagnostics": payload["diagnostics"],
        }, stale=stale1 or stale2 or stale_cands,
           extra_meta={"coverage": coverage,
                       "mapping_health": app.config.get("SECTOR_MAP_HEALTH", {})})
```

- [ ] **Step 5: 运行确认通过**

Run: `pytest tests/test_recommend.py -v`
Expected: 全部 PASS(含 Task 1 新增)。

- [ ] **Step 6: 在 `tests/test_api.py` 顶部 imports 加 `import recommend`(第 12 行 `import store` 之后)并追加两个 API 测试**

```python
import recommend
```

```python
def test_actionable_leaders_endpoint(client, monkeypatch):
    payload = {
        "sectors_scanned": 3, "total": 1, "items": [
            {"code": "sh600050", "name": "中国联通", "price": 5.0, "change_pct": 3.0,
             "tag": "龙头+强势", "tier": "可介入", "sector_code": "industry:885887",
             "sector_name": "半导体", "sector_verdict": "建议关注", "sector_composite": 78.0,
             "trend": 100, "volume_price": 90, "signal": 80, "composite": 91.5, "risk": 0,
             "bias_pct": -83.05},
        ],
        "skipped_sectors": [{"name": "白酒", "reason": "no_mapping"}],
        "diagnostics": {"stocks_daily_failed": 0},
    }
    monkeypatch.setattr(recommend, "collect_actionable_leaders",
                        lambda *a, **k: (payload, False))
    r = client.get("/api/actionable-leaders")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    assert d["generated_at"]
    assert d["total"] == 1
    it = d["items"][0]
    assert it["tier"] == "可介入" and it["bias_pct"] == -83.05
    assert it["sector_code"] == "industry:885887"
    cov = body["meta"]["coverage"]
    assert cov["scanned"] == 3 and cov["total"] == 1 and cov["skipped"] == 1
    assert cov["skipped_by_reason"] == {"no_mapping": 1}
    assert body["meta"]["mapping_health"]["ok"] is True
    assert body["meta"]["stale"] is False


def test_actionable_leaders_source_fail(client, monkeypatch):
    def boom(t):
        raise ds.DataSourceError("network down")
    monkeypatch.setattr(ds, "get_sector_summary", boom)
    r = client.get("/api/actionable-leaders")
    assert r.status_code == 500
    assert r.get_json()["error"]["code"] == "SOURCE_FAIL"
```

- [ ] **Step 7: 运行确认通过**

Run: `pytest tests/test_api.py -v`
Expected: 全部 PASS(含既有 API 测试)。

- [ ] **Step 8: 全量回归**

Run: `pytest -q`
Expected: 全绿(基线 75 + 本次新增)。

- [ ] **Step 9: Commit**

```bash
git add recommend.py app.py tests/test_recommend.py tests/test_api.py
git commit -m "$(cat <<'EOF'
feat(api): /api/actionable-leaders 全局可介入龙头汇总

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 前端第三标签页「可介入龙头」

**Files:**
- Modify: `templates/index.html:30`(tabs 追加按钮)、`templates/index.html:47`(reco-panel 之后追加 `#actionable-panel`)
- Modify: `static/app.js`(`switchView`、`refreshAll`、tab 点击 handler、新增 `renderActionableLeaders`/`loadActionableLeaders`)
- Modify: `static/style.css`(追加样式)

**Interfaces:**
- Consumes: `GET /api/actionable-leaders` 响应结构(Task 2):`data.items[]` 含 `sector_name/sector_verdict/name/code/price/change_pct/tag/tier/composite/risk/bias_pct`;`meta.coverage` 含 `scanned/total/skipped`;`data.skipped_sectors[]` 含 `name/reason`。
- Produces: 第三个 tab(点击后显示 `#actionable-panel` 并加载),行点击复用 `openStock`。

- [ ] **Step 1: `templates/index.html` 追加 tab 按钮(30 行 `推荐` 按钮之后)**

```html
      <button class="tab" data-view="actionable">可介入龙头</button>
```

- [ ] **Step 2: `templates/index.html` 在 `#reco-panel`(47 行)之后追加面板**

```html
    <div id="actionable-panel" class="hidden">
      <div id="actionable-meta" class="muted"></div>
      <table class="actionable-table">
        <thead><tr><th>板块</th><th>板块评价</th><th>名称</th><th>代码</th><th>现价</th><th>涨跌幅</th><th>标签</th><th>档位</th><th>综合分</th><th>风险</th><th>乖离率%</th></tr></thead>
        <tbody id="actionable-table"></tbody>
      </table>
      <div id="actionable-skipped" class="muted"></div>
    </div>
```

- [ ] **Step 3: `static/app.js` 在 `loadRecommend`(262 行)之后追加两个函数**

```js
function renderActionableLeaders(b) {
  const d = b.data, m = b.meta;
  const cov = m.coverage || {};
  $("#actionable-meta").innerHTML = cov.scanned != null
    ? `已扫描板块 ${cov.scanned} 个,可介入/观察 ${cov.total} 只,跳过 ${cov.skipped} 个`
    : "";
  const tbody = $("#actionable-table");
  if (!d.items.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="muted">当前无可介入龙头(规避超买追高)</td></tr>`;
  } else {
    tbody.innerHTML = d.items.map((x) => {
      const chg = x.change_pct, bias = x.bias_pct;
      return `<tr class="actionable-row" data-code="${x.code}">
        <td>${x.sector_name}</td>
        <td class="verdict">${x.sector_verdict}</td>
        <td>${x.name}</td>
        <td>${x.code}</td>
        <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
        <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
        <td><i class="leader-tag">${x.tag}</i></td>
        <td><span class="tier-badge ${x.tier === "可介入" ? "tier-buy" : "tier-watch"}">${x.tier}</span></td>
        <td>${x.composite == null ? "…" : x.composite.toFixed(2)}</td>
        <td>${x.risk}</td>
        <td>${bias == null ? "—" : bias.toFixed(2) + "%"}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr.actionable-row").forEach((tr) =>
      tr.addEventListener("click", () => openStock(tr.dataset.code)));
  }
  $("#actionable-skipped").innerHTML = d.skipped_sectors.length
    ? "被跳过: " + d.skipped_sectors.map((s) => `${s.name}(${s.reason})`).join(" | ")
    : "";
}

async function loadActionableLeaders() {
  try {
    const b = await api("/api/actionable-leaders");
    renderActionableLeaders(b);
  } catch (e) {
    $("#actionable-skipped").innerHTML = `<span class="muted">可介入龙头拉取失败</span>`;
  }
}
```

- [ ] **Step 4: `static/app.js` 的 `switchView`(264-268 行)追加第三面板切换**

```js
function switchView(view) {
  state.view = view;
  $("#sector-view").classList.toggle("hidden", view !== "sectors");
  $("#reco-panel").classList.toggle("hidden", view !== "recommend");
  $("#actionable-panel").classList.toggle("hidden", view !== "actionable");
}
```

- [ ] **Step 5: `static/app.js` 的 `refreshAll`(274 行)增补 actionable 分支**

```js
  if (state.view === "recommend") { try { await loadRecommend(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "actionable") { try { await loadActionableLeaders(); } catch (e) { /* 沿用旧 */ } }
```

- [ ] **Step 6: `static/app.js` 的 tab 点击 handler(293-301 行)扩展三向分发**

```js
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const view = t.dataset.view || "sectors";
    switchView(view);
    if (view === "recommend") loadRecommend().catch(() => { /* 沿用旧 */ });
    else if (view === "actionable") loadActionableLeaders();   // 内部已处理失败态
    else { state.type = t.dataset.type || "industry"; loadSectors(); }
  }));
```

- [ ] **Step 7: `static/style.css` 追加样式(文件末尾)**

```css
#actionable-meta { margin-bottom: 8px; }
#actionable-skipped { margin-top: 8px; }
.actionable-table { font-size: 13px; }
tr.actionable-row { cursor: pointer; }
tr.actionable-row:hover { background: var(--bg); }
.tier-badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; color: #fff; }
.tier-buy { background: var(--down); }
.tier-watch { background: var(--muted); }
```

- [ ] **Step 8: 全量回归 + 手工冒烟**

Run: `pytest -q`
Expected: 全绿(前端改动不影响后端测试)。
然后重启服务器并 `curl http://127.0.0.1:8000/api/actionable-leaders` 验证 200 与 items 结构;浏览器打开 `http://127.0.0.1:8000` 切换「可介入龙头」标签页确认渲染、行点击跳转个股详情、空态/失败态提示。视觉与交互验收由用户在浏览器完成(无浏览器自动化环境)。

- [ ] **Step 9: Commit**

```bash
git add templates/index.html static/app.js static/style.css
git commit -m "$(cat <<'EOF'
feat(ui): 可介入龙头 第三标签页

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```
