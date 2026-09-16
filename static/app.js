const state = {
  type: "industry",
  view: "sectors",
  market: null,
  sectors: [],
  current: null, // {kind:'sector'|'stock', code}
  regime: null,
  swingEdit: false,
  autoTimer: null,
};
const GAP_WARN_PCT = -0.682;  // P1 探针定稿:个股次日 gap 分布 P20(原 provisional -1.5)

const $ = (s) => document.querySelector(s);

function api(path) {
  return fetch(path).then((r) => r.json()).then((b) => {
    if (!b.ok) throw new Error((b.error && b.error.message) || "请求失败");
    return b;
  });
}

function fmtPct(v) { return v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(2) + "%"; }

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function verdictClass(v) {
  if (v === "强烈关注") return "verdict-high";
  if (v === "回避") return "verdict-avoid";
  return "";
}

function renderIndices(indices) {
  $("#indices").innerHTML = indices.map((i) =>
    `<span>${esc(i.name)} <b class="${i.change_pct >= 0 ? "up" : "down"}">${fmtPct(i.change_pct)}</b>` +
    ` <span class="muted">${i.price.toFixed(2)}</span></span>`).join("");
}

function renderBreadth(b, vol) {
  const s = (v) => v ?? "—";
  $("#breadth").innerHTML =
    `<span>上涨 <b class="up">${b.up}</b> / 下跌 <b class="down">${b.down}</b> / 平 ${b.flat}</span>` +
    `<span>涨停 ${b.limit_up} / 跌停 ${b.limit_down}</span>` +
    `<span>总成交额 ${(b.total_turnover / 1e8).toFixed(0)}亿</span>` +
    `<span>量能较昨日 ${vol && vol.pct !== null && vol.pct !== undefined ? fmtPct(vol.pct) : "收盘后对比"}</span>`;
}

async function loadMarket() {
  const b = await api("/api/market");
  state.market = b.data;
  renderIndices(b.data.indices);
  renderBreadth(b.data.breadth, b.data.volume_vs_yesterday);
  $("#updated").textContent = "更新于 " + (b.meta.updated_at || "—");
  $("#stale-flag").classList.toggle("hidden", !b.meta.stale);
}

// ---- 市场情绪择时(regime gate) ----
function regimeChip(d) {
  if (!d || d.label == null) {
    return `<span class="regime-chip regime-unknown">市场情绪:数据不足</span>`;
  }
  const advice = d.advice || {};
  const action = advice.action || "unknown";
  return `<span class="regime-chip regime-${esc(action)}"><b>市场情绪:${esc(d.label)}</b></span>`;
}

function regimeLine(d) {
  if (!d) return "";
  const advice = d.advice || {};
  return regimeChip(d) + `<span class="muted">${esc(advice.message || "")}</span>`;
}

function renderRegime() {
  const bar = $("#regime-bar");
  if (!state.regime || state.regime.label == null) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  $("#regime-indicator").innerHTML =
    regimeLine(state.regime) + `<span class="muted">(${esc(state.regime.as_of || "—")})</span>`;
}

async function loadRegime() {
  try {
    state.regime = (await api("/api/regime")).data;
  } catch (e) {
    // best-effort:初次失败保持 null(隐藏 banner);已有旧值则不覆盖
  }
  renderRegime();
}

async function loadSectors() {
  const b = await api(`/api/sectors?type=${state.type}&top=60`);
  state.sectors = b.data.sectors;
  const tbody = $("#sector-table tbody");
  tbody.innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${esc(s.code)}">` +
    `<td>${esc(s.name)}</td><td class="${s.index_change_pct != null && s.index_change_pct >= 0 ? "up" : "down"}">${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${esc(s.verdict)}${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}</td></tr>`).join("");
  tbody.querySelectorAll("tr.sector-row").forEach((tr) =>
    tr.addEventListener("click", () => openSector(tr.dataset.code)));
}

function scoreCell(v) { return v === null ? "…" : v.toFixed(0); }

async function openSector(code) {
  state.current = { kind: "sector", code };
  const b = await api("/api/sector?code=" + encodeURIComponent(code));
  $("#detail-title").classList.add("hidden");
  $("#chart-sector").classList.remove("hidden");
  $("#sector-scores").classList.remove("hidden");
  $("#chart-stock").classList.add("hidden");
  $("#chart-intraday").classList.add("hidden");
  $("#stock-scores").classList.add("hidden");
  $("#hold-advice").classList.add("hidden");
  $("#volume-price").classList.add("hidden");
  $("#tech-indicators").classList.add("hidden");
  renderSectorCharts(b.data);
  renderSectorScores(b.data);
  renderSectorLeaders(b.data);
}

function renderSectorScores(d) {
  const sc = d.scores;
  $("#sector-scores").innerHTML =
    `<div class="card"><div class="label">板块</div><div class="value">${esc(d.name)}</div></div>` +
    `<div class="card"><div class="label">综合分</div><div class="value">${sc.composite === null ? "…" : sc.composite.toFixed(2)}</div></div>` +
    `<div class="card"><div class="label">情绪</div><div class="value">${sc.emotion === null ? "…" : sc.emotion.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">强度</div><div class="value">${sc.strength.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">风险</div><div class="value">${sc.risk.toFixed(0)}</div></div>` +
    `<div class="card"><div class="verdict">${esc(d.verdict)}${d.overheated ? '<span class="overheat-badge">过热</span>' : ""}</div></div>`;
}

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
    `<span class="leader-chip" data-code="${esc(x.code)}" title="${esc(x.tag)}">` +
    `<i class="leader-tag">${esc(x.tag)}</i> ${esc(x.name)} ` +
    `<b>${x.price == null ? "—" : x.price.toFixed(2)}</b> ` +
    `<span class="${x.change_pct != null && x.change_pct >= 0 ? "up" : "down"}">${fmtPct(x.change_pct)}</span>` +
    `</span>`).join("");
  el.querySelectorAll(".leader-chip").forEach((sp) =>
    sp.addEventListener("click", () => openStock(sp.dataset.code)));
}

async function openStock(code) {
  state.current = { kind: "stock", code };
  const b = await api("/api/stock?code=" + encodeURIComponent(code));
  $("#detail-title").classList.add("hidden");
  $("#chart-sector").classList.add("hidden");
  $("#sector-scores").classList.add("hidden");
  $("#sector-leaders").classList.add("hidden");
  $("#chart-stock").classList.remove("hidden");
  $("#chart-intraday").classList.remove("hidden");
  $("#stock-scores").classList.remove("hidden");
  $("#hold-advice").classList.remove("hidden");
  $("#volume-price").classList.remove("hidden");
  $("#tech-indicators").classList.remove("hidden");
  renderStockCharts(b.data);
  renderStockScores(b.data);
  $("#btn-wl-add").classList.remove("hidden");
}

function renderStockScores(d) {
  const sc = d.scores;
  let html =
    `<div class="card"><div class="label">${esc(d.name)}</div><div class="value">${d.quote.price}</div><div class="muted">${fmtPct(d.quote.change_pct)}</div></div>`;
  if (d.composite === null || d.composite === undefined) {
    html += `<div class="card"><div class="verdict">数据不足(历史 <3 个月)</div></div>`;
  } else {
    html +=
      `<div class="card"><div class="label">综合分</div><div class="value">${d.composite.toFixed(2)}</div></div>` +
      `<div class="card"><div class="label">位置</div><div class="value">${sc.position == null ? "…" : sc.position.toFixed(0)}</div></div>` +
      `<div class="card"><div class="label">趋势</div><div class="value">${sc.trend == null ? "…" : sc.trend.toFixed(0)}</div></div>` +
      `<div class="card"><div class="label">量价</div><div class="value">${sc.volume_price == null ? "…" : sc.volume_price.toFixed(0)}</div></div>` +
      `<div class="card"><div class="label">信号</div><div class="value">${sc.signal == null ? "…" : sc.signal.toFixed(0)}</div></div>` +
      `<div class="card"><div class="label">风险</div><div class="value">${sc.risk == null ? "…" : sc.risk.toFixed(0)}</div></div>` +
      `<div class="card"><div class="verdict ${verdictClass(d.verdict)}">${esc(d.verdict)}</div></div>`;
  }
  if (d.sector_resolved === false) {
    html += `<div class="muted" style="margin-top:8px">板块未解析,未含共振加成</div>`;
  }
  $("#stock-scores").innerHTML = html;
  renderTechnicalDetail(d);
}

// ---- ECharts 图表 ----
function klineOption(d) {
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["K线", "MA5", "MA10", "MA20"] },
    grid: [{ left: 50, right: 20, top: 30, height: "55%" }, { left: 50, right: 20, top: "72%", height: "20%" }],
    xAxis: [{ type: "category", data: d.kline.map((k) => k.date), boundaryGap: true },
            { type: "category", gridIndex: 1, data: d.kline.map((k) => k.date) }],
    yAxis: [{ scale: true }, { gridIndex: 1, scale: true }],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    series: [
      { name: "K线", type: "candlestick", data: d.kline.map((k) => [k.open, k.close, k.low, k.high]) },
      { name: "MA5", type: "line", data: ma(d.kline, 5), smooth: true, showSymbol: false },
      { name: "MA10", type: "line", data: ma(d.kline, 10), smooth: true, showSymbol: false },
      { name: "MA20", type: "line", data: ma(d.kline, 20), smooth: true, showSymbol: false },
      { name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: d.kline.map((k) => k.volume) },
    ],
  };
}
function ma(kline, n) {
  return kline.map((_, i) => {
    if (i < n - 1) return null;
    let s = 0; for (let j = i - n + 1; j <= i; j++) s += kline[j].close;
    return +(s / n).toFixed(2);
  });
}
function intradayOption(d) {
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["价格", "均价"] },
    grid: [{ left: 50, right: 20, top: 30, height: "55%" }, { left: 50, right: 20, top: "72%", height: "20%" }],
    xAxis: [{ type: "category", data: d.intraday.map((x) => x.time) },
            { type: "category", gridIndex: 1, data: d.intraday.map((x) => x.time) }],
    yAxis: [{ scale: true }, { gridIndex: 1, scale: true }],
    series: [
      { name: "价格", type: "line", data: d.intraday.map((x) => x.price), showSymbol: false },
      { name: "均价", type: "line", data: d.intraday.map((x) => x.avg), showSymbol: false, lineStyle: { type: "dashed" } },
      { name: "分时量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: d.intraday.map((x) => x.volume) },
    ],
  };
}
function makeChart(el, option) {
  const old = echarts.getInstanceByDom(el);
  if (old) old.dispose();   // 重渲染前销毁旧实例,避免“已有实例”报错
  const chart = echarts.init(el);
  chart.setOption(option);
  return chart;
}
function renderSectorCharts(d) {
  const el = $("#chart-sector");
  el.innerHTML = "";   // 板块名由 score-cards 的“板块”卡展示
  if (d.index_history.length < 2) { el.innerHTML = "<p class='muted'>历史数据不足</p>"; return; }
  makeChart(el, klineOption({ kline: d.index_history }));
}
function renderStockCharts(d) {
  const el1 = $("#chart-stock");
  el1.innerHTML = "<b>" + esc(d.name) + "</b> 日K线";   // 先写 DOM 再初始化图表
  const el2 = $("#chart-intraday");
  el2.innerHTML = "<b>分时图</b> 价格/均价/成交量";
  if (d.kline.length >= 2) makeChart(el1, klineOption(d));
  if (d.intraday.length >= 2) makeChart(el2, intradayOption(d));
}

// ---- 自选股 ----
function getWatchlist() {
  try { return JSON.parse(localStorage.getItem("stock_wl") || "[]"); } catch (e) { return []; }
}
function saveWatchlist(w) { localStorage.setItem("stock_wl", JSON.stringify(w)); }
function renderWatchlist() {
  const w = getWatchlist();
  $("#wl-items").innerHTML = w.map((x) =>
    `<span class="wl-item" data-code="${esc(x.code)}">⭐ ${esc(x.name)}(${esc(x.code)})</span>`).join("");
  document.querySelectorAll(".wl-item").forEach((el) =>
    el.addEventListener("click", () => openStock(el.dataset.code)));
}
function addWatchlist(name, code) {
  const w = getWatchlist();
  if (!w.some((x) => x.code === code)) w.push({ name, code });
  saveWatchlist(w);
  renderWatchlist();
}
$("#btn-wl-add").addEventListener("click", async () => {
  if (state.current && state.current.kind === "stock") {
    const b = await api("/api/stock?code=" + state.current.code);
    addWatchlist(b.data.name, b.data.code);
  }
});

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
      if (state.swingEdit) return;   // 编辑态点击名称不跳转,仅 × 按钮生效
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

// ---- 波段候选(方向中性,可操作性筛选) ----
function renderSwingCandidates(b) {
  const d = b.data;
  const el = $("#swing-candidates");
  const head = `<div class="muted" style="margin-top:8px">波段候选(方向中性,按活跃度/换手率排序,非收益预测;基于上一交易日收盘):</div>`;
  if (!d.items || !d.items.length) {
    el.innerHTML = head + `<div class="muted">暂无符合可操作性的波段候选(流动性/换手/振幅/风险尾过滤)。</div>`;
    return;
  }
  el.innerHTML = head + `<table class="actionable-table"><thead><tr>
    <th>名称</th><th>代码</th><th>现价</th><th>涨跌幅</th><th>换手率%</th><th>振幅%</th><th>风险</th><th>位置</th><th>板块</th>
    </tr></thead><tbody>` + d.items.map((x) => {
      const chg = x.change_pct;
      return `<tr class="swing-cand-row" data-code="${esc(x.code)}">
        <td>${esc(x.name)}</td><td>${esc(x.code)}</td>
        <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
        <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
        <td>${x.turnover_pct == null ? "—" : x.turnover_pct.toFixed(2)}</td>
        <td>${x.amp20 == null ? "—" : x.amp20.toFixed(2)}</td>
        <td>${x.risk == null ? "—" : x.risk.toFixed(0)}</td>
        <td>${x.pos60 == null ? "—" : (x.pos60 * 100).toFixed(0) + "%"}</td>
        <td>${esc(x.sector_name || "—")}</td>
      </tr>`;
    }).join("") + `</tbody></table>`;
  el.querySelectorAll("tr.swing-cand-row").forEach((tr) =>
    tr.addEventListener("click", () => openStock(tr.dataset.code)));
}
async function loadSwingCandidates() {
  try {
    renderSwingCandidates(await api("/api/swing-candidates"));
  } catch (e) {
    $("#swing-candidates").innerHTML = `<span class="muted">波段候选拉取失败</span>`;
  }
}

// ---- 低位透视(方向中性;把 position 因子背后的低位股显性化) ----
// 诚实声明:低位反转【无选股 alpha】—— Phase 0 预注册检验判负
// (恐慌日低位组 vs 同日等权全市场 spread −0.071%,单边 p=0.666,n=41;
//  80% 功效下可检出 0.407%,故为真·零结果,不是样本不足)。本表只是透视工具。
function renderLowPosition(b) {
  const d = b.data;
  const el = $("#lowpos-table");
  $("#lowpos-meta").innerHTML =
    `已扫描活跃池 ${d.total} 只,按 position 分降序(越低位越靠前)` +
    (d.generated_at ? ` · 生成于 ${esc(d.generated_at)}` : "");
  $("#lowpos-warn").innerHTML =
    `<div class="lowpos-warn">⚠ 透视工具,不是策略:低位反转无选股 alpha(Phase 0 预注册判负,` +
    `恐慌日 spread −0.071%,单边 p=0.666)。本表只解释模型 position 因子为什么给某只股票高分,` +
    `<b>不预测方向、不排收益序</b>。</div>`;
  $("#lowpos-basis").innerHTML = esc(d.basis || "") +
    `<br>口径:position 分 = 50%×(1−60日位置) + 35%×乖离甜点区 + 15%×平台分,故【位置分高 = 60日位置低 = 低位】。` +
    `位置分≠综合分:综合分还含量价/趋势/信号与板块共振加成,52 分是推荐门槛。`;
  if (!d.items || !d.items.length) {
    el.innerHTML = `<tr><td colspan="13" class="muted">暂无符合条件(流动性 / 日线长度 ≥ 61 根)的股票。</td></tr>`;
    $("#lowpos-skipped").innerHTML = "";
    return;
  }
  el.innerHTML = d.items.map((x) => {
    const chg = x.change_pct, dd = x.dd60_pct, bias = x.bias_pct;
    const tier = x.tier
      ? `<span class="tier-badge ${x.tier === "可介入" ? "tier-buy" : "tier-watch"}">${esc(x.tier)}</span>`
      : `<span class="muted">—</span>`;
    return `<tr class="lowpos-row" data-code="${esc(x.code)}">
      <td>${esc(x.name)}</td><td>${esc(x.code)}</td>
      <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
      <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
      <td>${x.position == null ? "…" : x.position.toFixed(1)}</td>
      <td>${x.pos60 == null ? "—" : (x.pos60 * 100).toFixed(0) + "%"}</td>
      <td class="down">${dd == null ? "—" : dd.toFixed(2) + "%"}</td>
      <td>${bias == null ? "—" : bias.toFixed(2) + "%"}</td>
      <td>${x.risk == null ? "—" : x.risk.toFixed(0)}</td>
      <td>${x.composite == null ? "…" : x.composite.toFixed(2)}</td>
      <td class="verdict">${esc(x.verdict || "—")}</td>
      <td>${tier}</td>
      <td>${esc(x.sector_name || "—")}</td>
    </tr>`;
  }).join("");
  el.querySelectorAll("tr.lowpos-row").forEach((tr) =>
    tr.addEventListener("click", () => openStock(tr.dataset.code)));
  const failed = (d.diagnostics || {}).stocks_daily_failed;
  $("#lowpos-skipped").innerHTML = failed
    ? `日线拉取失败跳过 ${failed} 只。` : "";
}

async function loadLowPosition() {
  try {
    renderLowPosition(await api("/api/low-position"));
  } catch (e) {
    $("#lowpos-table").innerHTML =
      `<tr><td colspan="13" class="muted">低位透视拉取失败</td></tr>`;
  }
}

// ---- 主题策略(动量 vol-target 月度仓位信号) ----
function fmtPosPct(w) { return w === null || w === undefined || w !== w ? "—" : (w * 100).toFixed(0) + "%"; }
function fmtStatPct(v) {  // 回测摘要 total/mdd:原始值 ×100 才是百分比
  if (v === null || v === undefined || v !== v) return "—";
  const p = v * 100;
  return (p > 0 ? "+" : "") + p.toFixed(1) + "%";
}
function renderThemeVol(d) {
  const vt = d.vol_target || {}, fb = d.full_baseline || {}, pr = d.params || {};
  $("#themevol-meta").innerHTML =
    `主题动量 vol-target 策略:每月末重平衡,信号日 <b>${esc(d.signal_date || "—")}</b>` +
    `(生成于 ${esc((d.generated_at || "").slice(0, 19))})。仅研究参考,不构成投资建议。` +
    `回测:vol-target top${esc(pr.topn)} 回撤 ${fmtStatPct(vt.mdd)} / 收益 ${fmtStatPct(vt.total)},` +
    `满仓对照回撤 ${fmtStatPct(fb.mdd)} / 收益 ${fmtStatPct(fb.total)}。`;
  $("#themevol-summary").innerHTML =
    `<div class="score-cards">` +
    `<div class="card"><div class="label">总仓位</div><div class="value">${fmtPosPct(d.total_pos)}</div></div>` +
    `<div class="card"><div class="label">现金</div><div class="value">${fmtPosPct(d.cash)}</div></div>` +
    `</div>`;
  const el = $("#themevol-positions");
  const pos = d.positions || [];
  if (!pos.length) {
    el.innerHTML = `<div class="muted">无持仓(全部现金)。</div>`;
    return;
  }
  el.innerHTML = `<table class="actionable-table"><thead><tr><th>主题</th><th>仓位</th><th>个股</th></tr></thead><tbody>` +
    pos.map((p) => {
      const chips = (p.stocks || []).map((s) =>
        `<span class="leader-chip themevol-stock" data-code="${esc(s.code)}">${esc(s.name)}</span>`).join(" ");
      return `<tr><td>${esc(p.theme)}</td><td>${fmtPosPct(p.weight)}</td><td>${chips}</td></tr>`;
    }).join("") + `</tbody></table>`;
  el.querySelectorAll(".themevol-stock").forEach((chip) =>
    chip.addEventListener("click", () => openStock(chip.dataset.code)));
}
async function loadThemeVol() {
  try {
    renderThemeVol((await api("/api/theme-vol")).data);
  } catch (e) {
    $("#themevol-summary").innerHTML = "";
    $("#themevol-positions").innerHTML = `<span class="muted">主题策略信号拉取失败:${esc(e.message)}</span>`;
  }
}

// ---- 个股技术详情(描述性,不构成买卖信号) ----
function renderTechnicalDetail(d) {
  const hold = d.hold;
  if (hold) {
    const reg = hold.regime || {};
    const st = hold.stock || {};
    $("#hold-advice").innerHTML =
      `<div class="card detail-card"><div class="label">适不适合持有</div>` +
      `<div class="value">${swingBadge(reg)} <span class="muted" style="font-size:13px">大盘 ${esc(reg.label || "未知")}</span></div>` +
      `<div class="muted">${esc(reg.message || "")}</div>` +
      `<div class="muted">个股:${esc(st.risk_note || "")}、${esc(st.pos_note || "")}</div>` +
      `<div class="muted">${esc(hold.summary || "")}</div></div>`;
  }
  const ind = d.indicators;
  const limited = !ind || ind.history_limited;
  const DISC = `<div class="muted" style="font-size:12px">技术指标为描述性,不构成买卖信号;超买/超卖仅描述当前位置,不代表即将反转。</div>`;
  if (limited) {
    $("#volume-price").innerHTML = `<div class="card detail-card"><div class="label">技术指标</div><div class="muted">历史不足(<3 个月)</div></div>`;
    $("#tech-indicators").innerHTML = "";
    return;
  }
  const vp = ind.volume_price || {};
  $("#volume-price").innerHTML =
    `<div class="card detail-card"><div class="label">量价分析</div>` +
    `<div class="value">${esc(vp.state || "—")}</div>` +
    `<div class="muted">量比 ${vp.vr == null ? "—" : vp.vr.toFixed(2)} · 换手率 ${ind.turnover_pct == null ? "—" : ind.turnover_pct.toFixed(2) + "%"} · 量能比 ${vp.vol_ratio == null ? "—" : vp.vol_ratio.toFixed(2)}</div></div>`;
  const ma = ind.ma || {}, macd = ind.macd || {}, kdj = ind.kdj || {};
  const rsi = ind.rsi || {}, bias = ind.bias || {};
  const f = (v) => (v == null ? "—" : v.toFixed(2));
  $("#tech-indicators").innerHTML =
    `<div class="card detail-card"><div class="label">技术指标</div>` + DISC +
    `<table class="tech-table"><tbody>` +
    `<tr><td>MA(5/10/20/60)</td><td>${f(ma.ma5)} / ${f(ma.ma10)} / ${f(ma.ma20)} / ${f(ma.ma60)}</td></tr>` +
    `<tr><td>MACD DIF/DEA</td><td>${f(macd.dif)} / ${f(macd.dea)} <span class="muted">(${esc(macd.cross || "—")} · ${esc(macd.zero || "—")})</span></td></tr>` +
    `<tr><td>KDJ K/D/J</td><td>${f(kdj.k)} / ${f(kdj.d)} / ${f(kdj.j)} <span class="muted">(${esc(kdj.state || "—")} · ${esc(kdj.cross || "—")})</span></td></tr>` +
    `<tr><td>RSI14</td><td>${f(rsi.rsi14)} <span class="muted">(${esc(rsi.state || "—")})</span></td></tr>` +
    `<tr><td>乖离率(MA20)</td><td>${bias.pct == null ? "—" : bias.pct.toFixed(2) + "%"}</td></tr>` +
    `<tr><td>60 日位置</td><td>${ind.pos60 == null ? "—" : (ind.pos60 * 100).toFixed(0) + "%"}</td></tr>` +
    `</tbody></table></div>`;
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
    await openStock(b.data.code);   // 立即在右侧展示该股三层分析,避免"只剩一个名字"
  } catch (e) { $("#swing-add-input").placeholder = "无效代码"; }
});

function renderRecommend(b) {
  const d = b.data, m = b.meta;
  const cov = m.coverage || {};
  // 市场情绪门控:recommend 响应自带 regime(有缓存时);否则回退启动时 fetch 的 state.regime
  $("#reco-regime").innerHTML = regimeLine(d.regime || state.regime);
  $("#reco-coverage").innerHTML = cov.strong_candidates != null
    ? `今日强势板块 ${cov.strong_candidates} 个,已覆盖 ${cov.mapped} 个,跳过 ${cov.skipped} 个`
    : "";
  // 静态免责(数字为旧策略历史回测,spec §5.3)
  $("#reco-disclaimer").innerHTML =
    `信号基于 ${esc(d.close_date || "…")} 收盘;基于旧策略的历史回测(2025-08~2026-08):` +
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
        <td>${esc(s.code)}</td><td>${esc(s.name)}</td>
        <td>${s.signal_close == null ? "—" : s.signal_close.toFixed(2)}</td>
        <td>${s.today_open == null ? "—" : s.today_open.toFixed(2)}</td>
        <td class="${g != null && g < 0 ? "down" : ""}">${gapTxt}${warn ? ' <span class="gap-warn">信号撤回/谨慎</span>' : ""}</td>
        <td class="muted">${label}</td>
      </tr>`;
    }).join("");
    $("#reco-exec").innerHTML = `<b class="muted">昨日信号(基于 ${esc(ps.close_date)} 收盘, 对比今日开盘):</b>
      <table class="reco-table"><thead><tr><th>代码</th><th>名称</th><th>信号收盘</th><th>今日开盘</th><th>开盘差</th><th>校验</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  } else {
    $("#reco-exec").innerHTML = "";
  }
  $("#reco-sectors").innerHTML = d.sectors.length ? d.sectors.map((s) => `
    <div class="reco-sector panel">
      <div class="reco-sector-head">
        <b>${esc(s.name)}</b>
        <span class="verdict">${esc(s.verdict)}</span>${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}
        <span class="muted">综合 ${s.composite_score == null ? "…" : s.composite_score.toFixed(2)}</span>
        <span class="muted">成分股:${esc(s.constituent_source)}${s.match_type === "keyword" ? "[关键词]" : ""}</span>
      </div>
      <table class="reco-table">
        <thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>位置</th><th>综合</th><th>风险</th><th>结论</th></tr></thead>
        <tbody>${s.stocks.map((x) => {
          const chg = x.change_pct;
          return `<tr class="reco-row" data-code="${esc(x.code)}">
            <td>${esc(x.code)}</td><td>${esc(x.name)}</td>
            <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
            <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
            <td>${x.scores.position == null ? "…" : x.scores.position.toFixed(0)}</td>
            <td>${x.composite == null ? "…" : x.composite.toFixed(2)}</td>
            <td>${x.scores.risk}</td>
            <td class="verdict">${esc(x.verdict)}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`).join("") : "<div class='muted'>今日无强势板块</div>";
  $("#reco-skipped").innerHTML = d.skipped_sectors.length
    ? "被跳过(可补映射): " + d.skipped_sectors.map((s) =>
        `${esc(s.name)}(${s.composite_score == null ? "…" : s.composite_score.toFixed(2)},${esc(s.reason)})`).join(" | ")
    : "";
  document.querySelectorAll("#reco-sectors tr.reco-row").forEach((tr) =>
    tr.addEventListener("click", () => openStock(tr.dataset.code)));
}

async function loadRecommend() {
  const b = await api("/api/recommend?top_sectors=3&per_sector=5");
  renderRecommend(b);
}

function renderActionableLeaders(b) {
  const d = b.data, m = b.meta;
  const cov = m.coverage || {};
  $("#actionable-meta").innerHTML = cov.scanned != null
    ? `已扫描板块 ${cov.scanned} 个,可介入/观察 ${cov.total} 只,跳过 ${cov.skipped} 个`
    : "";
  const tbody = $("#actionable-table");
  if (!d.items.length) {
    tbody.innerHTML = `<tr><td colspan="12" class="muted">当前无可介入龙头(规避超买追高)</td></tr>`;
  } else {
    tbody.innerHTML = d.items.map((x) => {
      const chg = x.change_pct, bias = x.bias_pct;
      return `<tr class="actionable-row" data-code="${esc(x.code)}">
        <td>${esc(x.sector_name)}</td>
        <td class="verdict">${esc(x.sector_verdict)}</td>
        <td>${esc(x.name)}</td>
        <td>${esc(x.code)}</td>
        <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
        <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
        <td>${x.position == null ? "…" : x.position.toFixed(0)}</td>
        <td><i class="leader-tag">${esc(x.tag)}</i></td>
        <td><span class="tier-badge ${x.tier === "可介入" ? "tier-buy" : "tier-watch"}">${esc(x.tier)}</span></td>
        <td>${x.composite == null ? "…" : x.composite.toFixed(2)}</td>
        <td>${x.risk}</td>
        <td>${bias == null ? "—" : bias.toFixed(2) + "%"}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr.actionable-row").forEach((tr) =>
      tr.addEventListener("click", () => openStock(tr.dataset.code)));
  }
  $("#actionable-skipped").innerHTML = d.skipped_sectors.length
    ? "被跳过: " + d.skipped_sectors.map((s) => `${esc(s.name)}(${esc(s.reason)})`).join(" | ")
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

function renderTradeSim(b) {
  const d = b.data;
  const cfg = d.config || {};
  $("#tradesim-meta").innerHTML =
    `回测 ${esc((d.window && d.window.start) || "—")} → ${esc((d.window && d.window.end) || "—")} | ` +
    `持有 ${cfg.holding_days} 日, 止损 ${cfg.stop_pct}, 止盈 ${cfg.take_pct}, 双边成本 ${cfg.cost_bps}bps`;
  // 报告字段为比率(0-1),独立换算为百分比(fmtPct 面向已为百分数的实时数据)
  const p = (v) => v === null || v === undefined ? "—" : (v * 100).toFixed(2) + "%";
  const names = ["A", "E_hi", "E_lo", "B", "C", "D"];
  const tbody = $("#tradesim-table");
  tbody.innerHTML = names.map((n) => {
    const x = d.baskets && d.baskets[n];
    if (!x) return "";
    return `<tr>
      <td>${n}</td><td>${x.n_trades}</td>
      <td class="${x.win_rate >= 0.5 ? "up" : "down"}">${p(x.win_rate)}</td>
      <td>${x.profit_factor == null ? "—" : x.profit_factor.toFixed(2)}</td>
      <td class="${x.expectancy >= 0 ? "up" : "down"}">${p(x.expectancy)}</td>
      <td class="down">${p(x.max_drawdown)}</td>
      <td class="up">${p(x.avg_win)}</td>
      <td class="down">${p(x.avg_loss)}</td>
      <td class="up">${p(x.avg_max_fav)}</td>
      <td class="down">${p(x.avg_max_adv)}</td>
    </tr>`;
  }).join("");
}

async function loadTradeSim() {
  try {
    const b = await api("/api/report/trade-sim");
    renderTradeSim(b);
  } catch (e) {
    $("#tradesim-meta").innerHTML = `<span class="muted">交易回测报告不可用:${esc(e.message)}</span>`;
    $("#tradesim-table").innerHTML = "";
  }
}

// ---- 持仓诊断(多 horizon 前向分布,诚实输出) ----
function confidenceClass(c) {
  if (c === "有正向期望(超过成本)") return "diag-positive";
  if (c === "弱方向信号,未超过成本") return "diag-weak";
  return "diag-insufficient";
}
function confidenceBadge(c) {
  return `<span class="diag-confidence ${confidenceClass(c)}">${esc(c)}</span>`;
}
function diagnoseOption(H, r) {
  const hs = r.horizons;
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["该股 P(涨)", "基准 P(涨)"] },
    grid: { left: 45, right: 20, top: 30, bottom: 25 },
    xAxis: { type: "category", data: H.map((h) => h + "日") },
    yAxis: { type: "value", min: 0, max: 1, axisLabel: { formatter: (v) => (v * 100).toFixed(0) + "%" } },
    series: [
      { name: "该股 P(涨)", type: "line", data: H.map((h) => (hs[h] ? hs[h].p_up : null)), showSymbol: true, lineStyle: { width: 2 } },
      { name: "基准 P(涨)", type: "line", data: H.map((h) => (hs[h] ? hs[h].base_up : null)), showSymbol: true, lineStyle: { type: "dashed" } },
    ],
  };
}
function renderDiagnose(b) {
  const d = b.data;
  $("#diagnose-meta").innerHTML = d.as_of
    ? `校准基准截至 ${esc(d.as_of)} · 双边成本 ${(d.cost * 100).toFixed(2)}% · 信号带 ${(d.min_signal_band * 100).toFixed(0)}%`
    : "";
  const el = $("#diagnose-results");
  if (!d.results || !d.results.length) {
    const errTxt = (d.errors || []).map((x) => `${x.code}:${x.error}`).join(", ");
    el.innerHTML = `<div class="muted">暂无诊断结果${errTxt ? "(" + esc(errTxt) + ")" : ""}。请先运行 <code>python -m core.forward</code> 生成校准器。</div>`;
    return;
  }
  const H = d.horizons;
  const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
  el.innerHTML = d.results.map((r) => {
    const hs = r.horizons;
    const rows = H.map((h) => {
      const x = hs[h] || {};
      const costHit = x.exceeds_cost ? ' <span class="diag-cost">超成本</span>' : "";
      const cmp = x.beats_benchmark == null ? "—" : (x.beats_benchmark ? "跑赢" : "跑输");
      return `<tr>
        <td>${h}日</td>
        <td class="${x.p_up != null && x.p_up >= 0.5 ? "up" : "down"}">${pct(x.p_up)}</td>
        <td>${pct(x.base_up)}</td>
        <td class="${x.expected_return != null && x.expected_return >= 0 ? "up" : "down"}">${pct(x.expected_return)}</td>
        <td>${pct(x.benchmark)}</td>
        <td class="down">${pct(x.left_tail)}</td>
        <td>${cmp}${costHit}</td>
      </tr>`;
    }).join("");
    const riskFlag = r.risk_high ? ' <span class="diag-risk">风险偏高</span>' : "";
    const bestTxt = r.best_horizon ? ` · 最优信号 ${r.best_horizon} 日` : "";
    return `<div class="diag-card panel">
      <div class="diag-head"><b>${esc(r.name || r.code)}</b> <span class="muted">(${esc(r.code)})</span>
        ${confidenceBadge(r.confidence)}${riskFlag}<span class="muted">${bestTxt}</span></div>
      <div class="diag-chart" data-code="${esc(r.code)}"></div>
      <table class="diag-table"><thead><tr>
        <th>持有</th><th>P(涨)</th><th>基准P涨</th><th>期望收益</th><th>基准收益</th><th>左尾概率</th><th>对比</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
  }).join("");
  el.querySelectorAll(".diag-chart").forEach((c) => {
    const r = d.results.find((x) => x.code === c.dataset.code);
    if (r) makeChart(c, diagnoseOption(H, r));
  });
  if (d.errors && d.errors.length) {
    el.innerHTML += `<div class="muted">未诊断: ${d.errors.map((x) => `${esc(x.code)}(${x.error})`).join(", ")}</div>`;
  }
}
async function doDiagnose(codes) {
  if (!codes || !codes.length) {
    $("#diagnose-results").innerHTML = `<div class="muted">请输入代码,或点「诊断自选」。</div>`;
    return;
  }
  try {
    renderDiagnose(await api("/api/diagnose?codes=" + encodeURIComponent(codes.join(","))));
  } catch (e) {
    $("#diagnose-results").innerHTML = `<span class="muted">持仓诊断不可用:${esc(e.message)}</span>`;
  }
}
async function loadDiagnose() {
  const w = getWatchlist();
  if (w.length) await doDiagnose(w.map((x) => x.code));
  else $("#diagnose-results").innerHTML = `<div class="muted">自选为空:输入 6 位代码点「诊断」,或先加入自选再点「诊断自选」。</div>`;
}

function switchView(view) {
  state.view = view;
  $("#sector-view").classList.toggle("hidden", view !== "sectors");
  $("#reco-panel").classList.toggle("hidden", view !== "recommend");
  $("#actionable-panel").classList.toggle("hidden", view !== "actionable");
  $("#swing-panel").classList.toggle("hidden", view !== "swing");
  $("#lowpos-panel").classList.toggle("hidden", view !== "lowpos");
  $("#themevol-panel").classList.toggle("hidden", view !== "themevol");
  $("#diagnose-panel").classList.toggle("hidden", view !== "diagnose");
  $("#tradesim-panel").classList.toggle("hidden", view !== "tradesim");
}

// ---- 刷新 ----
async function refreshAll() {
  try { await loadMarket(); } catch (e) { $("#stale-flag").classList.remove("hidden"); }
  try { await loadSectors(); } catch (e) { /* 沿用旧列表 */ }
  await loadRegime();
  if (state.view === "recommend") { try { await loadRecommend(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "actionable") { try { await loadActionableLeaders(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "swing") { try { await loadSwing(); } catch (e) { /* 沿用旧 */ } try { await loadSwingCandidates(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "lowpos") { try { await loadLowPosition(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "themevol") { try { await loadThemeVol(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "diagnose") { try { await loadDiagnose(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "tradesim") { try { await loadTradeSim(); } catch (e) { /* 沿用旧 */ } }
  if (state.current) {
    try {
      if (state.current.kind === "sector") await openSector(state.current.code);
      else await openStock(state.current.code);
    } catch (e) { /* 保留当前 */ }
  }
}
$("#btn-refresh").addEventListener("click", refreshAll);
$("#auto-refresh").addEventListener("change", (e) => {
  if (e.target.checked) {
    state.autoTimer = setInterval(refreshAll, 60000);
  } else if (state.autoTimer) {
    clearInterval(state.autoTimer);
    state.autoTimer = null;
  }
});

// ---- 交互绑定 ----
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const view = t.dataset.view || "sectors";
    switchView(view);
    if (view === "recommend") loadRecommend().catch(() => { /* 沿用旧 */ });
    else if (view === "actionable") loadActionableLeaders();   // 内部已处理失败态
    else if (view === "swing") { loadSwing(); loadSwingCandidates(); }
    else if (view === "lowpos") loadLowPosition();             // 内部已处理失败态
    else if (view === "themevol") loadThemeVol();              // 内部已处理失败态
    else if (view === "diagnose") loadDiagnose();              // 内部已处理失败态
    else if (view === "tradesim") loadTradeSim();              // 内部已处理失败态
    else { state.type = t.dataset.type || "industry"; loadSectors(); }
  }));
$("#btn-sector-search").addEventListener("click", async () => {
  const kw = $("#sector-search").value.trim();
  const b = await api(`/api/sectors?type=${state.type}&top=60&search=${encodeURIComponent(kw)}`);
  $("#sector-table tbody").innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${esc(s.code)}"><td>${esc(s.name)}</td><td>${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${esc(s.verdict)}${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}</td></tr>`).join("");
  document.querySelectorAll("#sector-table tr.sector-row").forEach((tr) =>
    tr.addEventListener("click", () => openSector(tr.dataset.code)));
});
$("#btn-stock").addEventListener("click", () => {
  const code = $("#stock-search").value.trim();
  if (code) openStock(code);
});
$("#stock-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const c = $("#stock-search").value.trim(); if (c) openStock(c); }
});
$("#btn-diagnose").addEventListener("click", () => {
  const code = $("#diagnose-input").value.trim();
  if (code) doDiagnose([code]);
});
$("#diagnose-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { const c = $("#diagnose-input").value.trim(); if (c) doDiagnose([c]); }
});
$("#btn-diagnose-wl").addEventListener("click", () => loadDiagnose());

// ---- 启动 ----
renderWatchlist();
refreshAll();
