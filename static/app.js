const state = {
  type: "industry",
  view: "sectors",
  market: null,
  sectors: [],
  current: null, // {kind:'sector'|'stock', code}
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

function verdictClass(v) {
  if (v === "强烈关注") return "verdict-high";
  if (v === "回避") return "verdict-avoid";
  return "";
}

function renderIndices(indices) {
  $("#indices").innerHTML = indices.map((i) =>
    `<span>${i.name} <b class="${i.change_pct >= 0 ? "up" : "down"}">${fmtPct(i.change_pct)}</b>` +
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

async function loadSectors() {
  const b = await api(`/api/sectors?type=${state.type}&top=60`);
  state.sectors = b.data.sectors;
  const tbody = $("#sector-table tbody");
  tbody.innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${s.code}">` +
    `<td>${s.name}</td><td class="${s.index_change_pct >= 0 ? "up" : "down"}">${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${s.verdict}${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}</td></tr>`).join("");
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
  renderSectorCharts(b.data);
  renderSectorScores(b.data);
  renderSectorLeaders(b.data);
}

function renderSectorScores(d) {
  const sc = d.scores;
  $("#sector-scores").innerHTML =
    `<div class="card"><div class="label">板块</div><div class="value">${d.name}</div></div>` +
    `<div class="card"><div class="label">综合分</div><div class="value">${sc.composite === null ? "…" : sc.composite.toFixed(2)}</div></div>` +
    `<div class="card"><div class="label">情绪</div><div class="value">${sc.emotion === null ? "…" : sc.emotion.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">强度</div><div class="value">${sc.strength.toFixed(0)}</div></div>` +
    `<div class="card"><div class="label">风险</div><div class="value">${sc.risk.toFixed(0)}</div></div>` +
    `<div class="card"><div class="verdict">${d.verdict}${d.overheated ? '<span class="overheat-badge">过热</span>' : ""}</div></div>`;
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
    `<span class="leader-chip" data-code="${x.code}" title="${x.tag}">` +
    `<i class="leader-tag">${x.tag}</i> ${x.name} ` +
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
  renderStockCharts(b.data);
  renderStockScores(b.data);
  $("#btn-wl-add").classList.remove("hidden");
}

function renderStockScores(d) {
  const sc = d.scores;
  let html =
    `<div class="card"><div class="label">${d.name}</div><div class="value">${d.quote.price}</div><div class="muted">${fmtPct(d.quote.change_pct)}</div></div>`;
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
      `<div class="card"><div class="verdict ${verdictClass(d.verdict)}">${d.verdict}</div></div>`;
  }
  if (d.sector_resolved === false) {
    html += `<div class="muted" style="margin-top:8px">板块未解析,未含共振加成</div>`;
  }
  $("#stock-scores").innerHTML = html;
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
  el1.innerHTML = "<b>" + d.name + "</b> 日K线";   // 先写 DOM 再初始化图表
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
    `<span class="wl-item" data-code="${x.code}">⭐ ${x.name}(${x.code})</span>`).join("");
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

function renderRecommend(b) {
  const d = b.data, m = b.meta;
  const cov = m.coverage || {};
  $("#reco-coverage").innerHTML = cov.strong_candidates != null
    ? `今日强势板块 ${cov.strong_candidates} 个,已覆盖 ${cov.mapped} 个,跳过 ${cov.skipped} 个`
    : "";
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
  $("#reco-sectors").innerHTML = d.sectors.length ? d.sectors.map((s) => `
    <div class="reco-sector panel">
      <div class="reco-sector-head">
        <b>${s.name}</b>
        <span class="verdict">${s.verdict}</span>${s.overheated ? '<span class="overheat-badge">过热</span>' : ""}
        <span class="muted">综合 ${s.composite_score == null ? "…" : s.composite_score.toFixed(2)}</span>
        <span class="muted">成分股:${s.constituent_source}${s.match_type === "keyword" ? "[关键词]" : ""}</span>
      </div>
      <table class="reco-table">
        <thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>位置</th><th>综合</th><th>风险</th><th>结论</th></tr></thead>
        <tbody>${s.stocks.map((x) => {
          const chg = x.change_pct;
          return `<tr class="reco-row" data-code="${x.code}">
            <td>${x.code}</td><td>${x.name}</td>
            <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
            <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
            <td>${x.scores.position == null ? "…" : x.scores.position.toFixed(0)}</td>
            <td>${x.composite == null ? "…" : x.composite.toFixed(2)}</td>
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
      return `<tr class="actionable-row" data-code="${x.code}">
        <td>${x.sector_name}</td>
        <td class="verdict">${x.sector_verdict}</td>
        <td>${x.name}</td>
        <td>${x.code}</td>
        <td>${x.price == null ? "—" : x.price.toFixed(2)}</td>
        <td class="${chg != null && chg >= 0 ? "up" : "down"}">${fmtPct(chg)}</td>
        <td>${x.position == null ? "…" : x.position.toFixed(0)}</td>
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

function switchView(view) {
  state.view = view;
  $("#sector-view").classList.toggle("hidden", view !== "sectors");
  $("#reco-panel").classList.toggle("hidden", view !== "recommend");
  $("#actionable-panel").classList.toggle("hidden", view !== "actionable");
}

// ---- 刷新 ----
async function refreshAll() {
  try { await loadMarket(); } catch (e) { $("#stale-flag").classList.remove("hidden"); }
  try { await loadSectors(); } catch (e) { /* 沿用旧列表 */ }
  if (state.view === "recommend") { try { await loadRecommend(); } catch (e) { /* 沿用旧 */ } }
  else if (state.view === "actionable") { try { await loadActionableLeaders(); } catch (e) { /* 沿用旧 */ } }
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
    else { state.type = t.dataset.type || "industry"; loadSectors(); }
  }));
$("#btn-sector-search").addEventListener("click", async () => {
  const kw = $("#sector-search").value.trim();
  const b = await api(`/api/sectors?type=${state.type}&top=60&search=${encodeURIComponent(kw)}`);
  $("#sector-table tbody").innerHTML = b.data.sectors.map((s) =>
    `<tr class="sector-row" data-code="${s.code}"><td>${s.name}</td><td>${fmtPct(s.index_change_pct)}</td>` +
    `<td>${scoreCell(s.emotion_score)}</td><td>${scoreCell(s.strength_score)}</td>` +
    `<td>${scoreCell(s.risk_score)}</td><td>${s.composite_score === null ? "…" : s.composite_score.toFixed(2)}</td>` +
    `<td class="verdict">${s.verdict}</td></tr>`).join("");
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

// ---- 启动 ----
renderWatchlist();
refreshAll();
