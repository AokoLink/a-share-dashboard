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

function apiPost(path, payload) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload) }).then((r) => r.json()).then((b) => {
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
  if (d && d.freshness && d.freshness.status !== "current") {
    return `<span class="regime-chip regime-unknown">历史市场状态:${esc(d.label || "数据不足")} (${esc(d.as_of || "—")})</span>`;
  }
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
  $("#sector-insights").classList.remove("hidden");
  $("#chart-stock").classList.add("hidden");
  $("#chart-intraday").classList.add("hidden");
  $("#stock-scores").classList.add("hidden");
  $("#hold-advice").classList.add("hidden");
  $("#volume-price").classList.add("hidden");
  $("#tech-indicators").classList.add("hidden");
  renderSectorCharts(b.data);
  renderSectorScores(b.data);
  renderSectorInsights(b.data);
  renderSectorLeaders(b.data);
}

async function loadSectorRotation() {
  const el = $("#sector-rotation");
  el.classList.remove("hidden");
  el.innerHTML = `<div class="muted">正在比较板块...</div>`;
  try {
    const b = await api("/api/sector-rotation?top=8");
    const rows = b.data.rows || [];
    const universe = b.data.candidate_universe || {};
    const categoryLabel = {industry: "行业", subindustry: "细分行业", theme: "题材"};
    el.innerHTML = `<div class="muted">截止 ${esc(b.data.as_of || "—")}；完整可用板块范围 ${universe.total || 0} 个，指数当日可用 ${universe.index_current || 0} 个，精确成分 ${universe.membership_verified || 0} 个。按20日相对上证综指排名展示；缺失指标留空。</div>
      <table class="actionable-table"><thead><tr><th>板块</th><th>类型</th><th>5日超额</th><th>20日超额</th><th>60日超额</th><th>20日排名变化</th><th>上涨占比</th><th>前三成交集中</th><th>状态</th></tr></thead><tbody>` +
      rows.map((x) => `<tr class="rotation-row" data-code="${esc(x.code)}"><td>${esc(x.name)}</td>
        <td>${esc(categoryLabel[x.category] || "未知")}</td><td>${fmtPct(x.relative5_pct)}</td><td>${fmtPct(x.relative20_pct)}</td>
        <td>${fmtPct(x.relative60_pct)}</td><td>${x.rank20_change == null ? "—" : x.rank20_change}</td>
        <td>${x.up_ratio == null ? "—" : (x.up_ratio * 100).toFixed(1) + "%"}</td>
        <td>${x.top3_amount_share == null ? "—" : (x.top3_amount_share * 100).toFixed(1) + "%"}</td>
        <td>${esc(x.state)}</td></tr>`).join("") + `</tbody></table>
      <div class="muted">站上均线、创新高及完整广度要求至少80%覆盖。近似映射待核实；题材与细分行业缺少来源时标为缺失。</div>`;
    el.querySelectorAll(".rotation-row").forEach((row) =>
      row.addEventListener("click", () => { if (/^industry:\d{6}$/.test(row.dataset.code)) openSector(row.dataset.code); }));
    const nativePanel = document.createElement("div");
    el.appendChild(nativePanel);
    try {
      const providers = [{id: "sw", label: "申万原生行业指数"}, {id: "ths", label: "同花顺原生行业指数"}];
      const nativeResults = await Promise.allSettled(providers.map((p) => api(`/api/sector-index-rotation?top=8&provider=${p.id}`)));
      nativePanel.innerHTML = nativeResults.map((result, i) => {
        if (result.status !== "fulfilled") return `<div class="muted">${esc(providers[i].label)}不可用：${esc(result.reason.message)}</div>`;
        const native = result.value.data;
        return `<h3>${esc(providers[i].label)}</h3>
        <div class="muted">行情截止 ${esc(native.as_of || "—")}；该截止日完整行情 ${native.current || 0}/${native.total || 0} 个。${native.freshness?.status === "current" ? "已覆盖最近收盘交易日。" : "当前数据时效待更新或核实。"}以下仅比较指数价格；成分、广度和股票归属需各自核实。</div>
        ${native.membership_observation_day ? `<div class="muted">成员观察日 ${esc(native.membership_observation_day)}；成员与行情日期不同时，仅展示指数参考。</div>` : ""}
        <table class="actionable-table"><thead><tr><th>行业指数</th><th>层级</th><th>5日超额</th><th>20日超额</th><th>60日超额</th><th>20日排名</th></tr></thead><tbody>` +
        (native.rows || []).map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(categoryLabel[x.category] || "行业")}</td><td>${fmtPct(x.relative5_pct)}</td>
          <td>${fmtPct(x.relative20_pct)}</td><td>${fmtPct(x.relative60_pct)}</td><td>${x.rank20 ?? "—"}</td></tr>`).join("") +
        `</tbody></table><div class="muted">基准为上证综指；各来源分类分别展示，历史指数不证明历史成分。</div>`;
      }).join("");
    } catch (e) {
      nativePanel.innerHTML = `<div class="muted">原生行业指数不可用：${esc(e.message)}</div>`;
    }
  } catch (e) { el.innerHTML = `<div class="muted">轮动矩阵不可用：${esc(e.message)}</div>`; }
}
$("#btn-sector-rotation").addEventListener("click", loadSectorRotation);

function renderSectorInsights(d) {
  const el = $("#sector-insights");
  const chartEl = $("#chart-sector-relative");
  const info = d.insights || {};
  const rel = info.relative_strength || {};
  const breadth = info.breadth || {};
  const metric = (n) => n == null ? "—" : fmtPct(n);
  const excess = [5, 20, 60].map((n) => {
    const item = (rel.returns || {})[String(n)];
    return `<span>${n}日相对上证综指 <b>${item ? metric(item.excess_pct) : "—"}</b></span>`;
  }).join("　");
  const ranks = (info.observations || []).slice(-5).map((x) =>
    `${esc(x.date)} 第${x.rank}名`).join(" · ");
  el.innerHTML = `<div class="panel" style="margin-top:10px">
    <b>板块证据 · ${esc(info.state || "证据不足")}</b>
    <div class="muted">描述性标签；成分为当前跨来源快照，不能回填历史。指数对照截止 ${esc(rel.as_of || "—")}。</div>
    <div>${excess}</div>
    <div>当前成分上涨占比 ${breadth.up_ratio == null ? "—" : (breadth.up_ratio * 100).toFixed(1) + "%"}　
      成交额前三集中度 ${breadth.top3_amount_share == null ? "—" : (breadth.top3_amount_share * 100).toFixed(1) + "%"}　
      成分覆盖 ${breadth.spot_matched == null ? "—" : breadth.spot_matched + "/" + breadth.total_members}</div>
    <div class="muted">成分涨跌中位 ${metric(breadth.median_change_pct)}；排名快照：${ranks ? ranks : "尚无连续记录"}</div>
    <div class="muted">技术行情覆盖 ${breadth.technical_checked == null ? "—" : breadth.technical_checked + "/" + breadth.technical_expected}；站上20日均线 ${breadth.above_ma20_ratio == null ? "—" : (breadth.above_ma20_ratio * 100).toFixed(1) + "%"}；60日收盘新高 ${breadth.new_high60_ratio == null ? "—" : (breadth.new_high60_ratio * 100).toFixed(1) + "%"}。</div>
  </div>`;
  const trajectory = rel.trajectory || [];
  chartEl.classList.toggle("hidden", trajectory.length < 2);
  if (trajectory.length >= 2) {
    makeChart(chartEl, { tooltip: { trigger: "axis" }, legend: { data: ["板块", "上证综指"] },
      grid: { left: 48, right: 20, top: 35, bottom: 35 },
      xAxis: { type: "category", data: trajectory.map((x) => x.date) },
      yAxis: { type: "value", axisLabel: { formatter: "{value}%" } },
      series: [{ name: "板块", type: "line", data: trajectory.map((x) => x.sector_pct), showSymbol: false },
               { name: "上证综指", type: "line", data: trajectory.map((x) => x.benchmark_pct), showSymbol: false }] });
  }
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
  const roles = d.insights && d.insights.roles;
  if (roles && (roles.trading_core.length || roles.price_leaders.length)) {
    const chips = (items, detail) => items.map((x) =>
      `<span class="leader-chip" data-code="${esc(x.code)}">${esc(x.name)}(${esc(x.code)})　${esc(detail(x))}</span>`).join(" ");
    el.classList.remove("hidden");
    el.innerHTML = `<div><b>成交核心</b> <span class="muted">按当前成分成交额份额</span><br>${chips(roles.trading_core, (x) => (x.amount_share * 100).toFixed(1) + "%")}</div>` +
      `<div><b>价格领涨</b> <span class="muted">同日行情覆盖 ${roles.price_universe_valid || 0}/${roles.price_universe_expected || 0}，在全部已采集成员中比较</span><br>${chips(roles.price_leaders, (x) => fmtPct(x.return20_pct) + " 截至" + x.as_of)}</div>` +
      `<div class="muted">产业核心：缺少可核实的基本面证据，暂不识别。</div>`;
    el.querySelectorAll(".leader-chip").forEach((sp) =>
      sp.addEventListener("click", () => openStock(sp.dataset.code)));
    return;
  }
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
  $("#sector-insights").classList.add("hidden");
  $("#chart-sector-relative").classList.add("hidden");
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

async function loadPortfolio() {
  await loadAccountLedger();
  const el = $("#portfolio-results");
  const watchlist = getWatchlist();
  const weightsText = $("#portfolio-weights").value.trim();
  let holdings = watchlist.slice(0, 20).map((x) => ({code: x.code}));
  let cashWeight;
  if (weightsText) {
    holdings = weightsText.split(/\n/).filter(Boolean).map((line) => {
      const [code, weight] = line.trim().split(/[\s,，]+/);
      return {code, weight: Number(weight) / 100};
    });
    cashWeight = Number($("#portfolio-cash").value) / 100;
    if (!$("#portfolio-cash").value.trim() || !holdings.length || holdings.length > 20 || holdings.some((h) => !/^\d{6}$/.test(h.code) || !Number.isFinite(h.weight) || h.weight < 0) || !Number.isFinite(cashWeight) || Math.abs(holdings.reduce((sum,h) => sum + h.weight, 0) + cashWeight - 1) > 1e-6) {
      el.textContent = "请填写有效的股票权重与现金，合计100%。"; return;
    }
  }
  if (!holdings.length) { el.innerHTML = `<div class="muted">自选为空，请先加入股票。</div>`; return; }
  try {
    const b = await apiPost("/api/portfolio-insights", {
      holdings, ...(weightsText ? {cash_weight: cashWeight} : {}) });
    const d = b.data;
    const pct = (v) => v == null ? "—" : (v * 100).toFixed(1) + "%";
    const categoryNames = {industry: "行业", subindustry: "细分行业", theme: "题材"};
    const exposure = d.sector_exposure || {};
    const sectors = Object.entries(exposure.by_category || {}).flatMap(([category, rows]) =>
      rows.map((row) => `<tr><td>${esc(categoryNames[category])}：${esc(row.name)}</td><td>${pct(row.weight)}</td></tr>`)).join("");
    const contribution = Object.entries(d.risk_contribution || {}).sort((a, b) => b[1] - a[1]).map(([code, weight]) =>
      `<tr><td>${esc(code)}</td><td>${pct(weight)}</td></tr>`).join("");
    const clusters = (d.correlation_clusters || []).map((items) => items.map(esc).join("、")).join("；");
    el.innerHTML = `<div class="muted">行情截止 ${esc(d.data_as_of || "—")}；${esc(d.freshness.status)}；${d.weight_assumption === "provided_by_user" ? "按你输入的实际权重与现金" : "按自选等权假设"}。关系观察日 ${esc(exposure.as_of || "—")}；未知归属权重 ${pct(exposure.unknown_weight)}。行业与题材分开，重叠暴露不相加为账户仓位。</div>
      <div>最大单股权重 ${pct(d.max_stock_weight)}　前三股票 ${pct(d.top3_stock_weight)}　现金 ${pct(d.cash_weight)}</div>
      <div>低成交额股票权重 ${pct(d.low_liquidity_weight)}（成交额覆盖 ${d.amount_coverage}/${d.holdings}）　风险估计 ${esc(d.risk_status)}（共同收益日 ${d.common_return_sessions}）</div>
      <div class="muted">高度相关组（相关系数≥0.7）：${clusters ? esc(clusters) : "未识别或样本不足"}</div>
      <div class="muted">板块映射为当前静态近似；风险贡献按历史日收益协方差估计，不能代替未来风险。</div>
      <b>行业／题材暴露</b><table class="actionable-table"><tbody>${sectors || "<tr><td>无映射</td></tr>"}</tbody></table>
      <b>波动贡献</b><table class="actionable-table"><tbody>${contribution || "<tr><td>历史不足</td></tr>"}</tbody></table>`;
  } catch (e) { el.innerHTML = `<div class="muted">组合透视不可用：${esc(e.message)}</div>`; }
}
$("#btn-portfolio-refresh").addEventListener("click", loadPortfolio);

async function loadAccountLedger() {
  const el = $("#account-results");
  const pct = (v) => v == null ? "—" : (v * 100).toFixed(2) + "%";
  const money = (v) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits: 2});
  try {
    const d = (await api("/api/account-ledger")).data;
    if (d.status === "not_run") { el.innerHTML = `<div class="muted">账户任务尚未运行。</div>`; return; }
    const labels = {no_signals: "暂无真实冻结信号", simulation: "账户模拟", valuation_incomplete: "估值数据不完整", execution_inputs_incomplete: "成交输入不完整，收益待核实", intraday_pending_close: "盘中，等待收盘估值"};
    const metrics = d.metrics || {};
    const rows = d.daily || [];
    const latest = rows.at(-1) || {};
    const positions = (latest.positions || []).map((p) => `<tr><td>${esc(p.code)}</td><td>${p.shares}</td><td>${money(p.market_value)}</td><td>${pct(p.weight)}</td><td>${esc(p.strategies.join("、"))}</td><td>${p.pending_exit_shares}</td></tr>`).join("");
    const values = rows.slice(-20).reverse().map((r) => `<tr><td>${esc(r.date)}</td><td>${money(r.cash)}</td><td>${money(r.receivables)}</td><td>${money(r.position_value)}</td><td>${r.unit_nav == null ? "—" : r.unit_nav.toFixed(6)}</td><td>${pct(r.gross_turnover)}</td><td>${money(r.cost)}</td></tr>`).join("");
    const controls = Object.entries(d.comparisons || {}).map(([key, value]) => `<div>${key === "pool_equal" ? "同池账户" : "同板块账户"}净超额：${pct(value.total_net_excess)}${value.status === "blocked_membership_coverage" ? "（板块归属覆盖不足）" : ""}</div>`).join("");
    const exposure = latest.sector_exposure || {};
    const categoryNames = {industry: "行业", subindustry: "细分行业", theme: "题材"};
    const sectors = Object.entries(exposure).flatMap(([category, mapping]) => Object.entries(mapping).map(([id, weight]) => `${categoryNames[category]} ${esc(id)}：${pct(weight)}`)).join("；");
    const limits = Object.keys(d.evidence_summary || {}).length;
    el.innerHTML = `<div>${esc(labels[d.status] || d.status)}；${d.mode === "verified_inputs" ? "核实输入的模拟模式" : "研究代理模式"}。收盘估值截止 ${esc(d.valuation_as_of || "—")}。</div>
      <div>现金 ${money(latest.cash)}　应收分红 ${money(latest.receivables)}　持仓市值 ${money(latest.position_value)}　净资产 ${money(latest.nav)}</div>
      <div>账户累计收益 ${pct(metrics.total_return)}　最大回撤 ${pct(metrics.max_drawdown)}　日收益波动 ${pct(metrics.daily_volatility)}</div>
      <div>累计费用 ${money(d.fees)}　滑点 ${money(d.slippage)}　资金使用按预先配置的预算约束。</div>${controls}
      <div class="muted">${limits ? `存在 ${limits} 类未核实输入；` : ""}股数按代码合并，策略份额分别记账。未知行业／题材：${esc((latest.unknown_sector_codes || []).join("、") || "—")}。题材可重叠。${sectors || "暂无已核实板块暴露"}</div>
      <div class="muted">收盘风险超限：${esc((latest.risk_breaches || []).join("、") || "未记录")}。换手＝含滑点成交金额／前一收盘净资产，双边相加；单边口径为其一半。输入缺失时收益留空。模拟结果不构成策略有效性证明。</div>
      <table class="tbl"><thead><tr><th>股票</th><th>股数</th><th>市值</th><th>权重</th><th>策略</th><th>待退出股数</th></tr></thead><tbody>${positions || `<tr><td colspan="6">暂无持仓</td></tr>`}</tbody></table>
      <table class="tbl"><thead><tr><th>日期</th><th>现金</th><th>应收</th><th>市值</th><th>净值</th><th>双边换手</th><th>当日成本</th></tr></thead><tbody>${values}</tbody></table>`;
  } catch (e) { el.innerHTML = `<div class="muted">账户账本不可用：${esc(e.message)}</div>`; }
}
$("#btn-account-refresh").addEventListener("click", loadAccountLedger);
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
  // 恐慌日最容易被误用(低位股在恐慌日看起来"该抄底"),把当日 regime 摆在眼前
  const reg = d.regime;
  if (reg && reg.label != null) {
    const adv = (reg.advice && reg.advice.message) || "";
    $("#lowpos-regime").innerHTML =
      regimeChip(reg) + ` <span class="muted">${esc(adv)}` +
      `(截至 ${esc(reg.as_of || "—")})</span> <span class="muted">` +
      `注:恐慌日的正 edge 属于「全市场等权/ETF 篮子」,不属于低位选股 —— 见下方声明。</span>`;
  } else {
    $("#lowpos-regime").innerHTML = regimeChip(null);
  }
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
  const historical = d.status !== "current";
  $("#themevol-meta").innerHTML =
    (historical ? `<b>历史研究信号，数据截至 ${esc(d.data_as_of || "—")}，不可作为当前仓位建议。</b> ` : "") +
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
  const pos = historical ? (d.historical_positions || []) : (d.positions || []);
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
    ? `${d.status === "current" ? "当前" : "历史"}强势板块 ${cov.strong_candidates} 个,已覆盖 ${cov.mapped} 个,跳过 ${cov.skipped} 个`
    : "";
  // 静态免责(数字为旧策略历史回测,spec §5.3)
  $("#reco-disclaimer").innerHTML =
    (d.status === "current" ? "" : "<b>数据已过期，以下仅作历史研究记录。</b> ") +
    `信号基于 ${esc(d.close_date || "…")} 收盘。板块与选股增量尚未得到可信基线验证；次日开盘执行口径仍需检验。`;
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
    (d.evidence_status === "invalid_legacy" ? "<b>旧版入场口径无效，以下数值仅供历史对照。</b> " :
     "<b>探索性模拟：历史成分与成交价尚未核实。</b> ") +
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
    ? `${d.status !== "current" ? "<b>历史校准结果，不能作为当前判断。</b> " : ""}校准基准截至 ${esc(d.as_of)} · 双边成本 ${(d.cost * 100).toFixed(2)}% · 信号带 ${(d.min_signal_band * 100).toFixed(0)}%`
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
      const costHit = d.status === "current" && x.exceeds_cost ? ' <span class="diag-cost">超成本</span>' : "";
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

async function loadStrategyMonitor(cursor = null) {
  if (typeof cursor !== "number") cursor = null;
  const target = $("#strategy-monitor-results");
  try {
    const [monitor, daily, forward] = await Promise.all([api("/api/strategy-monitor?limit=30" + (cursor ? "&cursor=" + cursor : "")), api("/api/daily-runs"), api("/api/forward-validation")]);
    const d = monitor.data;
    state.monitorCursor = d.next_cursor;
    $("#btn-monitor-more").classList.toggle("hidden", !d.next_cursor);
    loadPipelineHealth();
    const run = daily.data;
    const statusLabels = {success: "完成", no_candidates: "完成，无候选", partial: "部分完成", failed: "失败",
      market_closed: "休市", waiting_for_close: "等待收盘", calendar_uncovered: "日历未覆盖", not_run: "尚未运行"};
    const coverage = run.coverage || {};
    const roleLabels = {research: "资格后研究池", qualification_inputs: "待资格验证范围", controls: "对照及待资格采集", holdings: "持仓", simulated_candidates: "已有模拟候选"};
    const coverRows = Object.entries(coverage).map(([role, value]) => `<tr><td>${esc(roleLabels[role] || role)}</td>` +
      `<td>${value.expected}</td><td>${(value.raw || {}).current || 0}</td>` +
      `<td>${(value.technical || {}).current || 0}</td></tr>`).join("");
    $("#daily-run-status").innerHTML = `<div class="muted">每日任务：${esc(statusLabels[run.status] || run.status)}；` +
      `日期 ${esc(run.day || "—")}；连续完整交易日 ${(run.stability || {}).consecutive_complete_sessions || 0}/10。</div>` +
      (coverRows ? `<table class="actionable-table"><thead><tr><th>范围</th><th>应有</th><th>执行价格当日覆盖</th>` +
        `<th>技术价格当日覆盖</th></tr></thead><tbody>${coverRows}</tbody></table>` : "") +
      `<div class="muted">每日任务入口：python -m pipeline.run_daily。持仓数量只统计显式提供的持仓清单。</div>`;
    const validation = forward.data;
    const validationLabels = {not_registered: "尚未登记", awaiting_prospective_signals: "等待登记后的真实信号",
      forward_observation: "观察中", code_changed_requires_new_registration: "规则已改变，需要重新登记"};
    $("#daily-run-status").innerHTML += `<div class="muted">独立前向验证：${esc(validationLabels[validation.status] || validation.status)}；` +
      `隔离期后起始日 ${esc(validation.validation_start || "日历待补齐")}；有效观察 ${validation.signal_days || 0} 个交易日、` +
      `${validation.accepted_snapshots || 0} 个策略快照。` +
      (validation.validated_account_return == null ? "暂无严格核实的账户收益证据。" : "账户结果仍属于核实输入下的模拟。") + "</div>";
    const seen = new Set();
    const phases = {pending_entry: "等待入场", holding: "持仓中", waiting_exit: "等待退出", cancelled: "已取消", observed: "观察已完成",
      partial_data: "数据异常，待核实", pending_calendar: "等待交易日历"};
    const progress = d.observations.filter((row) => { if (seen.has(row.snapshot_id)) return false; seen.add(row.snapshot_id); return true; });
    $("#strategy-progress-results").innerHTML = progress.length ? `<table class="actionable-table"><thead><tr><th>快照</th>` +
      `<th>进度</th><th>已模拟入场</th><th>取消</th><th>预计退出</th></tr></thead><tbody>` +
      progress.slice(0, 20).map((row) => `<tr><td>#${row.snapshot_id}</td><td>${esc(phases[row.payload.status] || row.payload.status)}</td>` +
        `<td>${row.payload.entered || 0}</td><td>${row.payload.cancelled || 0}</td><td>${esc(row.payload.exit_day || "待日历覆盖")}</td></tr>`).join("") +
      `</tbody></table>` : "";
    if (!d.versions.length && !d.snapshots.length) {
      target.innerHTML = `<div class="muted">尚无真实冻结信号。收盘后生成候选，且当日日线新鲜时才会开始记录。</div>`;
      return;
    }
    const labels = {breakout: "趋势突破", pullback: "强势回调", rebound: "超跌反弹"};
    const rows = d.versions.map((v) => `<tr><td>${esc(labels[v.strategy] || v.strategy)}</td>` +
      `<td>${esc(v.version)}</td><td>${esc(v.status)}</td><td>${v.frozen_days}</td>` +
      `<td>${v.revisions}</td><td>${v.comparable_observed_days || 0}</td><td>${v.event_clusters}</td>` +
      `<td>${v.mean_net_excess_vs_pool == null ? "—" : fmtPct(v.mean_net_excess_vs_pool * 100)}</td>` +
      `<td>${esc(v.recommendation)}</td></tr>`).join("");
    const latest = d.snapshots.slice(0, 10).map((s) => `<tr><td>${esc(s.signal_date)}</td>` +
      `<td>${esc(labels[s.strategy] || s.strategy)}</td><td>${s.candidates}</td>` +
      `<td><a href="/api/strategy-snapshot/${s.id}" target="_blank" rel="noopener">#${s.id}</a></td></tr>`).join("");
    target.innerHTML = (cursor ? target.innerHTML : "") + `<div class="muted">评审摘要：${esc(d.summary_status || "未知")}；过期或缺失摘要等待后台任务更新。</div><table class="actionable-table"><thead><tr><th>策略</th><th>版本</th><th>状态</th>` +
      `<th>冻结日</th><th>修订</th><th>可比较观察日</th><th>事件簇</th><th>成本后超额</th><th>建议</th></tr></thead>` +
      `<tbody>${rows}</tbody></table><div class="muted">正式使用仍受数据、成交状态与前向证据门槛限制。</div>` +
      `<table class="actionable-table"><thead><tr><th>日期</th><th>策略</th><th>候选数</th><th>快照</th></tr></thead>` +
      `<tbody>${latest}</tbody></table>`;
  } catch (e) { target.textContent = `验证记录读取失败：${e.message}`; }
}

async function loadStrategyCandidates() {
  const message = $("#strategy-freeze-message");
  message.textContent = "正在读取每日任务保存的候选…";
  try {
    const d = (await api("/api/strategy-candidates")).data;
    const counts = Object.entries(d.groups).map(([key, rows]) => `${key} ${rows.length}`).join(" / ");
    message.textContent = d.status === "not_run" ? "尚未运行每日任务。请运行 python -m pipeline.run_daily 后刷新。" :
      `数据截止 ${d.data_as_of || "未知"}；任务状态 ${d.freeze_status || d.status}；候选 ${counts}。`;
    const candidateRows = Object.entries(d.groups).flatMap(([strategy, rows]) => rows.map((row) => {
      const sector = row.sector_evidence || {};
      const names = (sector.relations || []).map((item) => item.name).join("、") || "未知";
      const condition = {applied: "已应用", unverified: "待核实", not_applicable: "不适用"}[row.sector_condition] || "待核实";
      return `<tr><td>${esc(row.name)}(${esc(row.code)})</td><td>${esc(strategy)}</td><td>${row.score}</td>` +
        `<td>${esc(names)}</td><td>${fmtPct(sector.sector_rel20)}</td><td>${esc(condition)}</td>` +
        `<td>${esc(sector.as_of || "—")}</td><td>${esc((row.reasons || []).join("；"))}</td>` +
        `<td>${esc(row.invalidation || "待核实")}</td><td>${esc(row.earliest_execution || "待核实")}</td>` +
        `<td>${row.existing_account_holding == null ? "持仓未知" : row.existing_account_holding ? "已有同代码持仓" : "未重复"}</td><td>研究候选</td></tr>`;
    }));
    $("#strategy-candidate-evidence").innerHTML = candidateRows.length ?
      `<table class="actionable-table"><thead><tr><th>候选</th><th>策略</th><th>分数</th><th>板块归属</th>` +
      `<th>20日板块超额</th><th>板块条件</th><th>证据日期</th><th>入选原因</th><th>失效条件</th><th>最早执行</th><th>持仓重复</th><th>证据等级</th></tr></thead>` +
      `<tbody>${candidateRows.join("")}</tbody></table>` : "";
  } catch (e) { message.textContent = `读取失败：${e.message}`; }
}
$("#btn-strategy-freeze").addEventListener("click", loadStrategyCandidates);
$("#btn-strategy-monitor-refresh").addEventListener("click", loadStrategyMonitor);

function switchView(view) {
  state.view = view;
  $("#workflow-market").classList.toggle("hidden", view !== "market");
  $("#strategy-candidates-panel").classList.toggle("hidden", view !== "strategy-candidates");
  $("#sector-view").classList.toggle("hidden", view !== "sectors");
  $("#reco-panel").classList.toggle("hidden", view !== "recommend");
  $("#actionable-panel").classList.toggle("hidden", view !== "actionable");
  $("#swing-panel").classList.toggle("hidden", view !== "swing");
  $("#lowpos-panel").classList.toggle("hidden", view !== "lowpos");
  $("#themevol-panel").classList.toggle("hidden", view !== "themevol");
  $("#diagnose-panel").classList.toggle("hidden", view !== "diagnose");
  $("#portfolio-panel").classList.toggle("hidden", view !== "portfolio");
  $("#strategy-monitor-panel").classList.toggle("hidden", view !== "strategy-monitor");
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
  else if (state.view === "portfolio") { await loadPortfolio(); }
  else if (state.view === "strategy-monitor") { await loadStrategyMonitor(); }
  else if (state.view === "strategy-candidates") { await loadStrategyCandidates(); }
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
    else if (view === "portfolio") loadPortfolio();            // 内部已处理失败态
    else if (view === "strategy-monitor") loadStrategyMonitor();
    else if (view === "strategy-candidates") loadStrategyCandidates();
    else if (view === "market") loadRegime();
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

$("#btn-monitor-more").addEventListener("click", () => { if (state.monitorCursor) loadStrategyMonitor(state.monitorCursor); });
async function loadPipelineHealth() {
  const el = $("#pipeline-health");
  try {
    const d = (await api("/api/pipeline-health?failed=1&limit=30")).data;
    const rows = Object.entries(d.codes || {}).flatMap(([code,pair]) => Object.entries(pair).map(([kind,r]) => `<tr><td>${esc(code)}</td><td>${kind === "raw" ? "执行价格" : "技术价格"}</td><td>${esc(r.error || r.status || "未知")}</td><td>${r.elapsed_seconds == null ? "—" : r.elapsed_seconds.toFixed(2)}</td><td>${esc(r.last_success_date || "未成功")}</td></tr>`)).join("");
    const steps = (d.steps || []).map((r) => `${r.step}: ${r.reason || r.status}`).join("；");
    el.innerHTML = `<div class="muted">运行故障 ${esc(steps || "暂无记录")}；失败或过期代码 ${d.total_codes || 0}（本页最多30）。</div>` + (rows ? `<table class="actionable-table"><thead><tr><th>代码</th><th>输入</th><th>异常</th><th>来源耗时秒</th><th>最后成功日期</th></tr></thead><tbody>${rows}</tbody></table>` : "");
  } catch (e) { el.textContent = `运行监控不可用：${e.message}`; }
}
