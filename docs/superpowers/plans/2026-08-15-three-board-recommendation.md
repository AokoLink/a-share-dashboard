# 三板块推荐实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把推荐重构为三板块:短线(恐慌择时持有 2-3 天)、波段(用户自选 + regime 启发式)、长线(非代码,本计划不含)。

**Architecture:** 短线改 `environment.py` 的 `REGIME_ADVICE` 文案;波段加 `regime_swing_action` 纯函数并暴露到 `/api/regime` 的 `swing` 字段,前端新增「波段」tab 复用 `stock_wl` 与 `state.regime`。

**Tech Stack:** Python(Flask)/ vanilla JS / 无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-15-three-board-recommendation-design.md`

## Global Constraints

- [C1] 无算法选股 alpha:任何板块不得声称"算法精选大涨股"。
- [C2] regime 仅作进场/回避信号;波段"持有/卖出"是启发式,UI 必须显式标注。
- [C3] 恐慌 41 天样本,禁止细分;持有期固定 2-3 天。
- [C5] A 股 T+1:禁 od/close1。成本 40bp。
- [C6] Python 源只用 ASCII `-`;`_analysis/` gitignored;直接提交 main;每改必跑测试。

---

### Task 1: 短线恐慌建议改为"持有 2-3 天"

**Files:**
- Modify: `environment.py:205`(`REGIME_ADVICE["恐慌"]`)
- Test: `tests/test_environment.py:205`(`test_regime_advice_mapping`)

- [ ] **Step 1: 写失败测试** —— 在 `test_regime_advice_mapping` 末尾追加断言:

```python
    assert "持有 2-3 天" in env.regime_advice("恐慌")["message"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_environment.py::test_regime_advice_mapping -q`
Expected: FAIL(当前 message 无"持有 2-3 天")

- [ ] **Step 3: 改实现** —— `environment.py` 把:

```python
    "恐慌": {"action": "buy_dip", "message": "市场恐慌,关注超跌反弹(全市场/宽基);注意趋势性崩盘会接飞刀"},
```

改为:

```python
    "恐慌": {"action": "buy_dip", "message": "市场恐慌,关注超跌反弹(全市场/宽基);建议持有 2-3 天(T+1 开盘买,T+3/4 开盘卖)。日级净胜率 73%(41 天样本,肥左尾,接飞刀风险,非稳赚)"},
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_environment.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add environment.py tests/test_environment.py
git commit -m "feat(regime): 短线恐慌建议改为持有2-3天(日级净胜率73%)"
```

---

### Task 2: 波段后端 regime_swing_action + 暴露 swing

**Files:**
- Modify: `environment.py:202-219`(REGIME_ADVICE 之后加 SWING_ACTION + regime_swing_action)、`environment.py:238-269`(`latest_state`/`load_cached_regime` 加 swing 字段)
- Test: `tests/test_environment.py`

**Interfaces:**
- Produces: `regime_swing_action(label) -> {"action": "opportunity"|"exit"|"hold"|"unknown", "message": str}`;`latest_state(...)`/`load_cached_regime(...)` 返回体新增 `"swing"` 键。

- [ ] **Step 1: 写失败测试** —— 新增:

```python
def test_regime_swing_action_mapping():
    assert env.regime_swing_action("恐慌")["action"] == "opportunity"
    assert env.regime_swing_action("高潮")["action"] == "exit"
    assert env.regime_swing_action("退潮")["action"] == "exit"
    for lab in ("牛", "熊", "震荡", "恢复"):
        assert env.regime_swing_action(lab)["action"] == "hold"
    assert env.regime_swing_action(None)["action"] == "unknown"
    assert env.regime_swing_action("未知态")["action"] == "unknown"
    assert isinstance(env.regime_swing_action("恐慌")["message"], str)
```

并在 `test_latest_state_bull` 追加 `assert st["swing"]["action"] == "hold"`;在 `test_latest_state_cached` 追加 `assert got["swing"]["action"] == "hold"`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_environment.py -q`
Expected: FAIL(`regime_swing_action` 未定义 / `swing` 缺失)

- [ ] **Step 3: 改实现** —— `environment.py` 在 `REGIME_ADVICE` 之后加:

```python
SWING_ACTION = {
    "恐慌": {"action": "opportunity", "message": "超跌反弹窗口,等机会(启发式,未回测)"},
    "高潮": {"action": "exit", "message": "冲顶,建议卖出/回避(启发式,未回测)"},
    "退潮": {"action": "exit", "message": "杀跌延续,建议卖出/回避(启发式,未回测)"},
    "牛":   {"action": "hold", "message": "持有(中性)"},
    "熊":   {"action": "hold", "message": "持有(中性)"},
    "震荡": {"action": "hold", "message": "持有(中性)"},
    "恢复": {"action": "hold", "message": "持有(中性)"},
}


def regime_swing_action(label):
    """波段启发式:regime -> 持有/卖出/等机会。纯函数,仅启发式(未回测为退出信号)。"""
    if label in SWING_ACTION:
        return SWING_ACTION[label]
    return {"action": "unknown", "message": "历史不足或未知状态,无法给出波段指引"}
```

`latest_state` 缓存命中 return 与末尾 `result` 各加 `"swing": regime_swing_action(...)`;`load_cached_regime` 返回体加 `"swing": regime_swing_action(cached.get("label"))`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_environment.py tests/test_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add environment.py tests/test_environment.py
git commit -m "feat(regime): 波段启发式 regime_swing_action(持有/卖出/等机会)+ 暴露 swing 字段"
```

---

### Task 3: 波段前端 tab(自选 + regime 指引 + 切换按钮)

**Files:**
- Modify: `templates/index.html`(tab + 面板)、`static/app.js`(state/渲染/交互)、`static/style.css`(样式)
- Test: `tests/test_api.py:116`(静态 wiring 冒烟)

- [ ] **Step 1: 写失败测试** —— 新增:

```python
def test_index_swing_wiring_static():
    import pathlib
    base = pathlib.Path(app_mod.__file__).resolve().parent
    html = (base / "templates" / "index.html").read_text(encoding="utf-8")
    js = (base / "static" / "app.js").read_text(encoding="utf-8")
    for cid in ("swing-panel", "swing-guidance", "swing-stocks", "btn-swing-switch"):
        assert f'id="{cid}"' in html
    for ref in ('$("#swing-panel")', '$("#swing-guidance")', '$("#btn-swing-switch")',
                "loadSwing", "state.regime.swing"):
        assert ref in js
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_api.py::test_index_swing_wiring_static -q`
Expected: FAIL

- [ ] **Step 3: 改实现** —— 见下方代码块(index.html / app.js / style.css)。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_api.py -q`
Expected: PASS(含新 wiring 测试)

- [ ] **Step 5: 手动冒烟 + 提交**

```bash
git add templates/index.html static/app.js static/style.css tests/test_api.py
git commit -m "feat(ui): 波段面板(自选+regime启发式指引+切换股票按钮)"
```

#### Task 3 代码

**index.html** —— tabs 里、`<button class="tab" data-view="tradesim">交易回测</button>` 之前插入:

```html
      <button class="tab" data-view="swing">波段</button>
```

`#actionable-panel` 之后、`#tradesim-panel` 之前插入:

```html
    <div id="swing-panel" class="hidden">
      <div id="swing-meta" class="muted"></div>
      <div id="swing-guidance"></div>
      <div id="swing-tools">
        <button id="btn-swing-switch">切换股票</button>
        <span id="swing-edit-mode" class="hidden">
          <input id="swing-add-input" placeholder="输入 6 位代码加入自选">
          <button id="btn-swing-add">加入</button>
        </span>
      </div>
      <div id="swing-stocks"></div>
    </div>
```

**app.js** —— `state` 加 `swingEdit: false`;`switchView` 加 `$("#swing-panel").classList.toggle("hidden", view !== "swing");`;tab 点击分发加 `else if (view === "swing") loadSwing();`;`refreshAll` 加 `else if (state.view === "swing") { try { await loadSwing(); } catch (e) {} }`;末尾自选股区追加:

```js
// ---- 波段(用户自选 + regime 启发式) ----
const SWING_ACTION_LABEL = { opportunity: "等机会", exit: "卖出/回避", hold: "持有", unknown: "未知" };
function swingBadge(d) {
  const a = (d && d.action) || "unknown";
  return `<span class="swing-badge swing-${esc(a)}">${SWING_ACTION_LABEL[a] || "未知"}</span>`;
}
function renderSwingStocks() {
  const w = getWatchlist();
  const el = $("#swing-stocks");
  if (!w.length) { el.innerHTML = `<div class="muted">自选为空:点「切换股票」加入你关注的股票。</div>`; return; }
  const editing = state.swingEdit === true;
  el.innerHTML = w.map((x) =>
    `<span class="swing-item" data-code="${esc(x.code)}">` +
    (editing ? `<button class="swing-remove" data-code="${esc(x.code)}">×</button>` : "") +
    `⭐ ${esc(x.name)}(${esc(x.code)})</span>`).join("");
  el.querySelectorAll(".swing-item").forEach((sp) =>
    sp.addEventListener("click", (e) => {
      if (e.target.classList && e.target.classList.contains("swing-remove")) {
        removeWatchlist(e.target.dataset.code); return;
      }
      openStock(sp.dataset.code);
    }));
}
function renderSwing() {
  const swing = (state.regime && state.regime.swing) || null;
  $("#swing-meta").innerHTML = "波段指引为启发式:regime 仅在进场/回避上验证过,非退出信号;自选股本身无算法 edge。";
  const chip = (state.regime && state.regime.label != null) ? regimeChip(state.regime) : "";
  $("#swing-guidance").innerHTML = swing
    ? `今天整体:${swingBadge(swing)} <span class="muted">${esc(swing.message || "")}</span> ${chip}`
    : `今天整体:${swingBadge(null)} <span class="muted">(regime 数据不足)</span> ${chip}`;
  renderSwingStocks();
}
async function loadSwing() {
  if (!state.regime) { try { await loadRegime(); } catch (e) { /* 保留空 */ } }
  renderSwing();
}
function removeWatchlist(code) {
  saveWatchlist(getWatchlist().filter((x) => x.code !== code));
  renderWatchlist();
  renderSwing();
}
$("#btn-swing-switch").addEventListener("click", () => {
  state.swingEdit = !state.swingEdit;
  $("#swing-edit-mode").classList.toggle("hidden", !state.swingEdit);
  $("#btn-swing-switch").textContent = state.swingEdit ? "完成" : "切换股票";
  renderSwingStocks();
});
$("#btn-swing-add").addEventListener("click", async () => {
  const code = $("#swing-add-input").value.trim();
  if (!code) return;
  try {
    const b = await api("/api/stock?code=" + encodeURIComponent(code));
    addWatchlist(b.data.name, b.data.code);
    $("#swing-add-input").value = "";
    renderSwingStocks();
  } catch (e) { $("#swing-add-input").placeholder = "无效代码"; }
});
```

**style.css** —— 末尾追加:

```css
#swing-guidance { margin-bottom: 8px; }
#swing-tools { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
.swing-badge { display: inline-block; padding: 3px 12px; border-radius: 14px; font-size: 13px; color: #fff; }
.swing-opportunity { background: #e07b00; }
.swing-exit { background: #c62828; }
.swing-hold { background: #5b6673; }
.swing-unknown { background: #8a919c; opacity: .75; }
.swing-item { display: inline-block; cursor: pointer; padding: 2px 8px; border: 1px solid var(--line); border-radius: 20px; margin: 2px; }
.swing-remove { border: none; background: var(--up); color: #fff; border-radius: 50%; width: 18px; height: 18px; line-height: 1; cursor: pointer; padding: 0; margin-right: 4px; }
```
