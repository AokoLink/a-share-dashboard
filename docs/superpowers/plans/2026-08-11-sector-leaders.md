# 板块详情「龙头/强势股」条带 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在板块详情(`/api/sector`)响应中返回该板块 5 个龙头/强势股(当日价格 + 涨跌幅),前端在板块详情下方渲染条带,点击任一股票跳转现有完整个股详情(`openStock`)。

**Architecture:** 后端在 `api_sector` 内联复用 `ds.resolve_sector_constituents(ths_name)`(THS→新浪行业映射,缓存 1800s)+ 已拉取的全市场 `spot`(零额外请求),经新增纯函数 `recommend.pick_leaders` 排序打标;前端在 `#sector-scores` 下方新增 `#sector-leaders` 条带,点击复用现有 `openStock(code)`。leaders 相关失败一律降级(仍 200),不阻塞核心板块视图。

**Tech Stack:** Flask(单文件 `app.py`)、pandas、akshare(仅经 `data_source` 封装)、原生 JS + ECharts(无新依赖)。

## Global Constraints

- 数据源仅 Sina/Tencent/THS;东财接口本网络屏蔽,禁止引入。
- 所有测试离线可跑(网络调用一律 monkeypatch)。
- 数值字段一律经 `_num()` 归一无;测试对 `round(...,2)` 后的分数断言用精确字面量或 `abs=0.01`。
- 板块打分口径以 `analysis.sector_verdict` 为准(本功能不涉及打分,仅引用)。
- 沿用现有命名、`fmtPct`/`openStock`、style.css 变量(`--panel/--line/--up/--down`),不重复造轮子。

---

## 文件结构

| 文件 | 责任 | 改动 |
|---|---|---|
| `recommend.py` | 新增纯函数 `pick_leaders`(龙头池+强势池排序打标) | 改 |
| `app.py` | `api_sector` 追加 leaders 计算与三个响应字段 | 改 |
| `tests/test_recommend.py` | `pick_leaders` 三个单测 | 改 |
| `tests/test_api.py` | 扩展 `test_sector_detail` + 两个降级路由测试 | 改 |
| `templates/index.html` | `#detail` 内新增 `#sector-leaders` 占位 | 改 |
| `static/app.js` | `renderSectorLeaders` + `openSector`/`openStock` 接线 | 改 |
| `static/style.css` | `.leader-chip` 胶囊条带样式 | 改 |

任务分解:Task 1 纯函数+单测 → Task 2 路由扩展+API 测试 → Task 3 前端渲染+静态验收。每个任务独立可测、可提交、可评审。

---

### Task 1: `recommend.pick_leaders` 纯函数 + 单测

**Files:**
- Modify: `recommend.py`(在 `build_recommend` 定义之前插入;该函数只依赖已有的 `_num`,不依赖前面函数)
- Test: `tests/test_recommend.py`(文件末尾追加三个测试)

**Interfaces:**
- Consumes: `_num(v)`(已存在于 recommend.py:15-22);spot 行 dict 字段 `code/name/price/change_pct/volume/amount`。
- Produces: `recommend.pick_leaders(spot_rows, total=5, exclude_codes=frozenset()) -> list[dict]`,每项 `{"code", "name", "price", "change_pct", "amount", "tag"}`。Task 2 依赖此函数。

- [ ] **Step 1: 写失败测试**(追加到 `tests/test_recommend.py` 末尾)

```python
def test_pick_leaders_mixed():
    rows = [
        {"code": "600001", "name": "甲股", "price": 10.0, "change_pct": 2.0, "amount": 5e8, "volume": 10000},
        {"code": "600002", "name": "乙股", "price": 11.0, "change_pct": 9.0, "amount": 3e8, "volume": 10000},
        {"code": "600003", "name": "丙股", "price": 12.0, "change_pct": 1.0, "amount": 8e8, "volume": 10000},
        {"code": "600004", "name": "丁股", "price": 13.0, "change_pct": 5.0, "amount": 1e8, "volume": 10000},
        {"code": "600005", "name": "戊股", "price": 14.0, "change_pct": 3.0, "amount": 4e8, "volume": 10000},
    ]
    leaders = recommend.pick_leaders(rows, total=5)
    # 龙头池(金额 top3):600003(8e8) 600001(5e8) 600005(4e8)
    # 强势池(涨幅 top3):600002(9) 600004(5) 600005(3)
    # 合并去重(龙头在前):600003 600001 600005 600002 600004
    assert [x["code"] for x in leaders] == ["600003", "600001", "600005", "600002", "600004"]
    assert [x["tag"] for x in leaders] == ["龙头", "龙头", "龙头+强势", "强势", "强势"]
    assert leaders[0]["price"] == 12.0
    assert leaders[2]["change_pct"] == 3.0


def test_pick_leaders_excludes():
    rows = [
        {"code": "600001", "name": "正常股", "price": 10.0, "change_pct": 2.0, "amount": 5e8, "volume": 10000},
        {"code": "600002", "name": "ST坏股", "price": 11.0, "change_pct": 9.0, "amount": 3e8, "volume": 10000},
        {"code": "600003", "name": "停牌股", "price": 0.0, "change_pct": 1.0, "amount": 8e8, "volume": 0},
        {"code": "600004", "name": "新股", "price": 13.0, "change_pct": 5.0, "amount": 1e8, "volume": 10000},
    ]
    leaders = recommend.pick_leaders(rows, total=5, exclude_codes={"600004"})
    # ST(名含 ST)、停牌(price/volume 空)、新股(exclude_codes)均被排除 → 仅剩 600001
    assert [x["code"] for x in leaders] == ["600001"]
    assert leaders[0]["tag"] == "龙头+强势"


def test_pick_leaders_empty_and_degenerate():
    assert recommend.pick_leaders([], total=5) == []
    all_dead = [{"code": "600001", "name": "全停牌", "price": 0.0, "change_pct": None,
                 "amount": None, "volume": 0}]
    assert recommend.pick_leaders(all_dead, total=5) == []
    # 金额全 None → 龙头池空,仅强势池
    no_amount = [
        {"code": "600001", "name": "无额A", "price": 10.0, "change_pct": 3.0, "amount": None, "volume": 10000},
        {"code": "600002", "name": "无额B", "price": 11.0, "change_pct": 2.0, "amount": None, "volume": 10000},
    ]
    l1 = recommend.pick_leaders(no_amount, total=5)
    assert [x["code"] for x in l1] == ["600001", "600002"]
    assert all(x["tag"] == "强势" for x in l1)
    assert l1[0]["amount"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: 三个新测试 FAIL,报 `AttributeError: module 'recommend' has no attribute 'pick_leaders'`。

- [ ] **Step 3: 实现 `pick_leaders`**

在 `recommend.py` 的 `build_recommend` 定义之前插入:

```python
def pick_leaders(spot_rows, total=5, exclude_codes=frozenset()):
    """板块龙头/强势股:龙头池(成交额 top3)+ 强势池(涨幅 top3)合并去重取前 total。

    纯函数,无网络。轻过滤:排除新股(exclude_codes)、ST、停牌(price/volume 空);保留涨停股。
    """
    rows = []
    for r in spot_rows:
        code = str(r["code"])
        if code in exclude_codes or "ST" in str(r.get("name") or "").upper():
            continue
        price, vol = _num(r.get("price")), _num(r.get("volume"))
        if not price or not vol:              # 停牌(价格/成交量缺失)
            continue
        rows.append(r)
    leader_pool = sorted((x for x in rows if _num(x.get("amount")) is not None),
                         key=lambda x: -(_num(x["amount"]) or 0))[:min(3, total)]
    strong_pool = sorted((x for x in rows if _num(x.get("change_pct")) is not None),
                         key=lambda x: -(_num(x["change_pct"]) or 0))[:min(3, total)]
    leader_codes = {str(x["code"]) for x in leader_pool}
    strong_codes = {str(x["code"]) for x in strong_pool}
    out, seen = [], set()
    for x in leader_pool + strong_pool:
        code = str(x["code"])
        if code in seen:
            continue
        seen.add(code)
        tag = ("龙头+强势" if (code in leader_codes and code in strong_codes)
               else ("龙头" if code in leader_codes else "强势"))
        out.append({"code": code, "name": str(x.get("name") or ""),
                    "price": _num(x.get("price")), "change_pct": _num(x.get("change_pct")),
                    "amount": _num(x.get("amount")), "tag": tag})
        if len(out) >= total:
            break
    return out
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_recommend.py -v`
Expected: 全部 PASS(含既有测试,总计新增 3 个)。

- [ ] **Step 5: 全量回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS(既有 70 + 新 3 = 73)。

- [ ] **Step 6: 提交**

```bash
git add recommend.py tests/test_recommend.py
git commit -m "feat(recommend): pick_leaders 板块龙头/强势股纯函数 + 单测

龙头池(成交额 top3)+ 强势池(涨幅 top3)合并去重取前 5,打 龙头/强势/龙头+强势 标签;
轻过滤排除 新股/ST/停牌,保留涨停股(展示条带非买入推荐)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `api_sector` 扩展返回 leaders + 路由测试

**Files:**
- Modify: `app.py:159-190`(`api_sector`;leaders 计算插在 `hist_rows` 之后、`return ok(...)` 之前,并扩展 `ok(...)` 的 data)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `recommend.pick_leaders`(Task 1)、`ds.resolve_sector_constituents`(已在 test fixture 中 mock)、`ds.get_new_stocks()`(fixture 返回 `set()`)、`spot` DataFrame(`code/name/price/change_pct/volume/amount`)。
- Produces: `/api/sector` 响应 data 新增三字段 `leaders`(list)、`leaders_status`(`"ok"|"no_mapping"|"ambiguous"|"source_fail"`)、`leaders_source`(str|null)。Task 3 依赖此契约。

- [ ] **Step 1: 写失败测试**(修改 `tests/test_api.py`)

1a. 扩展 `test_sector_detail`(在现有断言后追加):

```python
    assert d["leaders_status"] == "ok"
    # fixture resolve → 600519/600000,均在 spot;600519 金额最大(9.2e8)→ 龙头池首位
    assert d["leaders"][0]["code"] == "600519"
    assert d["leaders"][0]["tag"] == "龙头+强势"
    assert "price" in d["leaders"][0] and "change_pct" in d["leaders"][0]
    assert d["leaders_source"] == "电子信息"
```

1b. 在 `test_sector_detail` 之后追加两个降级测试:

```python
def test_api_sector_leaders_no_mapping(client, monkeypatch):
    monkeypatch.setattr(ds, "resolve_sector_constituents",
                        lambda name: {"ok": False, "reason": "no_mapping"})
    r = client.get("/api/sector?code=industry:885887")
    d = r.get_json()["data"]
    assert d["leaders"] == []
    assert d["leaders_status"] == "no_mapping"
    assert d["leaders_source"] is None
    assert d["index_history"]                     # 板块图表不阻塞


def test_api_sector_leaders_source_fail(client, monkeypatch):
    def boom(name):
        raise ds.DataSourceError("network down")
    monkeypatch.setattr(ds, "resolve_sector_constituents", boom)
    r = client.get("/api/sector?code=industry:885887")
    d = r.get_json()["data"]
    assert r.status_code == 200
    assert d["leaders"] == []
    assert d["leaders_status"] == "source_fail"
    assert d["index_history"]                     # 核心视图仍正常
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_api.py::test_sector_detail tests/test_api.py::test_api_sector_leaders_no_mapping tests/test_api.py::test_api_sector_leaders_source_fail -v`
Expected: 三个测试 FAIL(`KeyError: 'leaders'`,响应无此字段)。

- [ ] **Step 3: 实现 `api_sector` 扩展**

在 `app.py` 的 `api_sector` 中,`hist_rows` 列表推导之后、`return ok(...)` 之前插入:

```python
        leaders, leaders_status, leaders_source = [], "ok", None
        try:
            res = ds.resolve_sector_constituents(str(r["name"]))
        except Exception:
            leaders_status = "source_fail"
        else:
            if not res["ok"]:
                leaders_status = res["reason"]            # no_mapping | ambiguous
            else:
                spot_index = {str(x["code"]): x for x in spot.to_dict("records")}
                rows = [spot_index[c] for c in res["codes"] if c in spot_index]
                leaders = recommend.pick_leaders(rows, total=5,
                                                 exclude_codes=ds.get_new_stocks())
                leaders_source = res.get("source_name")
```

并把 `return ok(...)` 改为:

```python
        return ok({"code": "%s:%s" % (type_key, code), "name": str(r["name"]),
                   "scores": {"emotion": scores["emotion"], "strength": scores["strength"],
                              "risk": scores["risk"], "composite": scores["composite"]},
                   "verdict": verdict, "index_history": hist_rows,
                   "leaders": leaders, "leaders_status": leaders_status,
                   "leaders_source": leaders_source},
                  stale=stale1 or stale2 or stale3)
```

(文件顶部已 `import recommend`,无需新增 import。)

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_api.py -v`
Expected: 全部 PASS(含三个新旧 sector 测试)。

- [ ] **Step 5: 全量回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS(75 个)。

- [ ] **Step 6: 提交**

```bash
git add app.py tests/test_api.py
git commit -m "feat(api): /api/sector 返回板块龙头/强势股 leaders + 路由测试

内联复用 resolve_sector_constituents + 已拉取的全市场 spot(零额外请求);
映射/网络失败降级为 leaders_status 标记,板块核心视图仍 200。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 前端板块详情龙头/强势股条带

**Files:**
- Modify: `templates/index.html:55-62`(`#detail` 内 `#sector-scores` 之后插入)
- Modify: `static/app.js`(`openSector`/`openStock` 接线 + 新增 `renderSectorLeaders`)
- Modify: `static/style.css`(末尾追加 `.leader-chip` 样式)

**Interfaces:**
- Consumes: Task 2 的 `/api/sector` 契约(`leaders`/`leaders_status`/`leaders_source`);现有 `openStock(code)`、`fmtPct(v)`、`$()` 选择器。
- Produces: `#sector-leaders` 条带渲染;点击 → `openStock(data-code)`。

- [ ] **Step 1: `index.html` 插入占位**

`#detail` 内 `#sector-scores` 那一行之后加:

```html
      <div id="sector-leaders" class="hidden"></div>
```

- [ ] **Step 2: `app.js` 接线**

2a. `openSector`(第 70-71 行)在 `renderSectorScores(b.data);` 之后加:

```js
  renderSectorLeaders(b.data);
```

2b. `openStock`(第 85-97 行)在 `$("#sector-scores").classList.add("hidden");` 之后加:

```js
  $("#sector-leaders").classList.add("hidden");
```

2c. 在 `renderSectorScores` 函数之后新增:

```js
function renderSectorLeaders(d) {
  const el = $("#sector-leaders");
  const list = d.leaders || [];
  if (!list.length) {
    const hint = d.leaders_status === "source_fail" ? "板块成分股拉取失败"
      : ((d.leaders_status === "no_mapping" || d.leaders_status === "ambiguous") ? "暂无成分股映射" : "");
    el.classList.toggle("hidden", !hint);
    el.innerHTML = hint ? `<div class="muted">${hint}</div>` : "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<b class="muted">龙头/强势股</b>` + list.map((x) =>
    `<span class="leader-chip" data-code="${x.code}" title="${x.tag}">` +
    `<i class="leader-tag">${x.tag}</i> ${x.name} ` +
    `<b>${x.price == null ? "—" : x.price.toFixed(2)}</b> ` +
    `<span class="${x.change_pct != null && x.change_pct >= 0 ? "up" : "down"}">${fmtPct(x.change_pct)}</span>` +
    `</span>`).join("");
  el.querySelectorAll(".leader-chip").forEach((sp) =>
    sp.addEventListener("click", () => openStock(sp.dataset.code)));
}
```

- [ ] **Step 3: `style.css` 追加条带样式**(文件末尾)

```css
#sector-leaders { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
#sector-leaders .muted { margin-right: 4px; }
.leader-chip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px;
               border: 1px solid var(--line); border-radius: 12px; cursor: pointer;
               font-size: 13px; background: var(--panel); }
.leader-chip:hover { border-color: var(--text); }
.leader-tag { font-style: normal; color: var(--up); font-size: 11px; }
```

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS(75 个;前端改动不影响测试,此处确认无意外破坏)。

- [ ] **Step 5: 静态验收(离线)**

Run 以下检查(本环境无浏览器,用静态源码核验渲染路径;真实点击跳转留待用户在自有浏览器验收):

```bash
grep -n "sector-leaders" templates/index.html static/app.js
grep -n "renderSectorLeaders" static/app.js
grep -n "leader-chip" static/style.css
```

Expected: 各输出至少一行;`app.js` 中 `renderSectorLeaders` 被 `openSector` 调用、`openStock` 隐藏 `#sector-leaders`、点击绑定 `openStock(sp.dataset.code)` 均存在。

可选联网冒烟(若网络可用):`python app.py` 后 `curl "http://127.0.0.1:8000/api/sector?code=industry:885887"` 观察 `leaders` 数组(可能为空,取决于板块映射与 spot 覆盖,空则看 `leaders_status`)。不阻塞本任务。

- [ ] **Step 6: 提交**

```bash
git add templates/index.html static/app.js static/style.css
git commit -m "feat(web): 板块详情龙头/强势股条带渲染,点击跳转个股详情

#sector-scores 下方新增 #sector-leaders 条带:标签+名称+现价+涨跌幅(复用 fmtPct);
leaders 为空时按 leaders_status 灰字提示;点击任一项 openStock 进完整个股详情。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 自审清单

**1. 规格覆盖(spec `docs/superpowers/specs/2026-08-11-sector-leaders-design.md`):**
- §4.1 `pick_leaders` → Task 1(轻过滤/龙头池/强势池/合并去重/打 tag/截断 `total`,全部在代码中逐条实现)。
- §4.2 `api_sector` 三字段 + 仅 primary 源失败 500 → Task 2(leaders 相关异常捕获降级,`summary/hist/spot` 的既有 try/except 保持不变)。
- §5.1 `#sector-leaders` 占位 → Task 3 Step 1;§5.2 `renderSectorLeaders` 提示/渲染/点击 → Task 3 Step 2;§5.3 `.leader-chip` → Task 3 Step 3。
- §7 测试:pick_leaders 3 个(混合/排除/空退化)→ Task 1;`test_sector_detail` 扩展 + 2 个降级 → Task 2。
- §6 错误处理:no_mapping/ambiguous/source_fail 均降级且 `index_history` 不受影响 → Task 2 测试断言覆盖。

**2. 占位符扫描:** 无 TBD/TODO;每步含精确代码与预期。

**3. 类型/命名一致性:**
- `pick_leaders(spot_rows, total=5, exclude_codes=frozenset())` 在 Task 1 定义、Task 2 调用、Task 3 不直接用(只消费 JSON),签名一致。
- 响应字段 `leaders`/`leaders_status`/`leaders_source` 在 Task 2 测试与实现、Task 3 前端中拼写一致。
- `fmtPct`/`openStock`/`$` 均为 app.js 既有函数;`_num` 为 recommend.py 既有函数。
- 测试 fixture:test_api 的 `make_spot` 中 600519 amount 9.2e8 最大、600000 1e8,均无 ST/停牌 → leaders[0]==600519、tag==龙头+强势,与 Task 2 Step 1a 断言一致。

**边界确认:** 两个池均取 `min(3, total)`(total=5 时各 3);合并去重以 code 去重;`tag` 判定以两池 code 集合为准(同码两池均入 → 龙头+强势)。test_pick_leaders_mixed 中 600005 同时是金额第 3 与涨幅第 3 → 龙头+强势,已断言。
