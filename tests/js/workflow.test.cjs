const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { create } = require("../../static/request_runtime.js");

async function page() {
  const nodes = new Map();
  function element(selector) {
    const classes = new Set();
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: "", textContent: "", disabled: false, listeners: {},
      classList: { add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
        contains(name) { return classes.has(name); },
        toggle(name, force) { if (force === undefined ? !classes.has(name) : force) classes.add(name); else classes.delete(name); } },
      addEventListener(type, fn) { this.listeners[type] = fn; },
    });
    return nodes.get(selector);
  }
  const tabs = ["market", "sectors", "strategy-candidates"].map(view => {
    const tab = element("tab:" + view); tab.dataset = { view }; return tab;
  });
  const context = vm.createContext({
    RequestRuntime: { create }, window: { fetch: async () => ({ ok: true, json: async () => ({ ok: false }) }) },
    document: { hidden: false, querySelector: element, querySelectorAll: selector => selector === ".tab" ? tabs : [] },
    localStorage: { getItem: () => "[]" },
    setInterval: (fn) => { context.tick = fn; return 1; }, clearInterval() {},
    calls: [],
  });
  const source = fs.readFileSync(require.resolve("../../static/app.js"), "utf8");
  vm.runInContext(source + "\nglobalThis.hooks = {state, refreshAll, loadStrategyMonitor, openStock, loadRegime, loadStrategyCandidates, generateDailyCandidates, renderDailyJob, loadDailyJob};", context);
  await context.hooks.refreshAll();
  vm.runInContext(`
    loadMarket = async () => calls.push('market');
    loadRegime = async () => calls.push('regime');
    loadSectors = async () => calls.push('sectors');
    loadPortfolio = async () => calls.push('portfolio');
    loadPipelineHealth = async () => {};
  `, context);
  return { context, nodes, hooks: context.hooks };
}

test("saved portfolio refresh does not request the offscreen sector list", async () => {
  const { context, hooks } = await page();
  hooks.state.view = "portfolio";
  await Promise.all([hooks.refreshAll(), hooks.refreshAll()]);
  assert.deepEqual(Array.from(context.calls), ["market", "regime", "portfolio"]);
  context.calls.length = 0;
  hooks.state.view = "sectors";
  await hooks.refreshAll();
  assert.ok(context.calls.includes("sectors"));
});

test("market tab shows saved history shortage instead of an empty placeholder", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = { status: "insufficient_daily_history", as_of: "2026-09-30", label: null,
    observed_sessions: 1, required_sessions: 61, metrics: {}, freshness: { status: "historical" },
    saved_observation: {date: "2026-09-30", limit_up: 57, limit_down: 13, turnover: 1e12} };
  vm.runInContext("api = async () => ({data: saved}); loadRegime = hooks.loadRegime;", context);
  nodes.get("tab:market").listeners.click();
  await hooks.loadRegime();
  assert.equal(nodes.get("#workflow-market").classList.contains("hidden"), false);
  assert.match(nodes.get("#market-environment-status").textContent, /1\/61/);
  assert.match(nodes.get("#market-environment-status").textContent, /2026-09-30/);
  assert.match(nodes.get("#market-environment-metrics").innerHTML, /暂无/);
  assert.match(nodes.get("#market-environment-metrics").innerHTML, /涨停 57 \/ 跌停 13/);
  assert.match(nodes.get("#market-environment-metrics").innerHTML, /不是当前盘中数据/);
  assert.equal(nodes.get("#regime-bar").classList.contains("hidden"), false);
});

test("market environment read errors remain visible", async () => {
  const { context, hooks, nodes } = await page();
  vm.runInContext("api = async () => { throw new Error('offline'); };", context);
  await hooks.loadRegime();
  assert.match(nodes.get("#market-environment-status").textContent, /读取失败.*offline/);
  assert.equal(nodes.get("#regime-bar").classList.contains("hidden"), false);
});

test("candidate tab explains waiting and provides an explicit history action", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "waiting_for_close", groups: {breakout: []},
    cards: {breakout: {name: "趋势突破", hypothesis: "test"}}, previous_report: {data_as_of: "2026-09-30"}};
  vm.runInContext("api = async () => ({data: saved});", context);
  nodes.get("tab:strategy-candidates").listeners.click();
  await hooks.loadStrategyCandidates();
  assert.equal(nodes.get("#strategy-candidates-panel").classList.contains("hidden"), false);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /15:05/);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /不是今日信号/);
  assert.match(nodes.get("#strategy-candidate-evidence").innerHTML, /不表示筛选后零入选/);
  assert.match(nodes.get("#strategy-candidate-cards").innerHTML, /趋势突破/);
  assert.equal(nodes.get("#btn-strategy-history").classList.contains("hidden"), false);
});

test("history and zero-result candidate views do not present blank results", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = { status: "exploratory", freeze_status: "skipped_day_changed", data_as_of: "2026-09-30",
    data_freshness: "historical", groups: {breakout: []}, cards: {} };
  vm.runInContext("api = async path => {calls.push(path); return {data: saved};};", context);
  await hooks.loadStrategyCandidates(true);
  assert.equal(context.calls.at(-1), "/api/strategy-candidates?history=1");
  assert.match(nodes.get("#strategy-freeze-message").textContent, /历史留档.*不是今日信号/);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /跨日未封印/);
  assert.match(nodes.get("#strategy-candidate-evidence").innerHTML, /没有入选股票/);
});

test("hidden page pauses automatic refresh", async () => {
  const { context, nodes } = await page();
  nodes.get("#auto-refresh").listeners.change({ target: { checked: true } });
  context.document.hidden = true;
  context.tick();
  await Promise.resolve();
  assert.equal(context.calls.length, 0);
});

test("momentum watch distinguishes industry confirmation and unscored evidence", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "exploratory", data_as_of: "2026-11-02", cards: {momentum: {name: "热点延续／修复转强"}},
    groups: {momentum: [{code: "600001", name: "观察股", score: 80, candidate_kind: "observation",
      momentum_sector: {status: "confirmed", selected: {name: "行业A", up_ratio: .8, median_change_pct: 3}},
      reasons: ["当日接近涨停：可观察，次日成交另核实"]}]}};
  vm.runInContext("api = async () => ({data: saved});", context);
  await hooks.loadStrategyCandidates(true);
  const html = nodes.get("#strategy-candidate-evidence").innerHTML;
  assert.match(html, /当日行业共振/);
  assert.match(html, /新闻／财报／龙虎榜未评分/);
  assert.match(html, /观察候选，非买入信号/);
  context.saved.groups.momentum = [];
  context.saved.strategy_blockers = {momentum: "not_evaluated_in_archive"};
  await hooks.loadStrategyCandidates(true);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /未评估（旧留档无此策略）/);
});

test("a slower previous stock response cannot overwrite the latest selection", async () => {
  const { context, hooks } = await page();
  context.pending = {};
  vm.runInContext(`
    api = path => new Promise(resolve => pending[path] = resolve);
    renderStockCharts = data => calls.push(data.code);
    renderStockScores = () => {};
  `, context);
  const older = hooks.openStock("600001"), newer = hooks.openStock("600002");
  context.pending["/api/stock?code=600002"]({ data: { code: "600002" } });
  await newer;
  context.pending["/api/stock?code=600001"]({ data: { code: "600001" } });
  await older;
  assert.deepEqual(Array.from(context.calls), ["600002"]);
});

test("monitor pagination displays every fetched snapshot once", async () => {
  const { context, hooks, nodes } = await page();
  context.monitorData = {
    versions: [], observations: [], next_cursor: 31, summary_status: "current",
    snapshots: Array.from({ length: 30 }, (_, i) => ({ id: 60 - i, signal_date: "2026-09-30", strategy: "breakout", candidates: 0 })),
  };
  vm.runInContext(`api = async path => ({ data: path.startsWith('/api/strategy-monitor') ? monitorData : {} });`, context);
  await hooks.loadStrategyMonitor();
  let html = nodes.get("#strategy-monitor-results").innerHTML;
  assert.equal((html.match(/\/api\/strategy-snapshot\//g) || []).length, 30);
  context.monitorData = { ...context.monitorData, next_cursor: null,
    snapshots: [{ id: 30, signal_date: "2026-09-29", strategy: "breakout", candidates: 0 }] };
  await Promise.all([hooks.loadStrategyMonitor(31), hooks.loadStrategyMonitor(31)]);
  html = nodes.get("#strategy-monitor-results").innerHTML;
  assert.equal((html.match(/\/api\/strategy-snapshot\//g) || []).length, 31);
  assert.equal((html.match(/评审摘要/g) || []).length, 1);
  assert.equal(nodes.get("#btn-monitor-more").disabled, false);
});

test("generation button posts once and returns while background work is still running", async () => {
  const { context, hooks, nodes } = await page();
  hooks.state.view = "strategy-candidates";
  context.job = {status: "queued", day: "2026-10-09", automation: {enabled: true, day: "2026-10-09"}};
  vm.runInContext(`
    apiPost = path => {calls.push(path); return new Promise(resolve => globalThis.finishPost = resolve);};
    api = async () => ({data: job});
  `, context);
  const first = hooks.generateDailyCandidates();
  await hooks.generateDailyCandidates();
  assert.deepEqual(Array.from(context.calls), ["/api/daily-job"]);
  context.finishPost({data: context.job});
  await first;
  assert.equal(nodes.get("#btn-strategy-generate").disabled, true);
  assert.equal(nodes.get("#btn-strategy-generate").textContent, "生成中…");
  assert.match(nodes.get("#daily-job-status").textContent, /后台准备中/);
});

test("failure enables manual retry and displays the failing stage", async () => {
  const { hooks, nodes } = await page();
  hooks.renderDailyJob({status: "failed", day: "2026-10-09", error: "source unavailable",
    automation: {enabled: true, day: "2026-10-09"}, progress: {steps: {prices: {status: "failed", error: "price gap"}}}});
  assert.equal(nodes.get("#btn-strategy-generate").disabled, false);
  assert.equal(nodes.get("#btn-strategy-generate").textContent, "重试生成今日候选");
  assert.match(nodes.get("#daily-job-status").textContent, /source unavailable/);
  assert.match(nodes.get("#daily-job-progress").textContent, /个股价格.*price gap/);
});

test("failed historical task can show recovered research without claiming live success", async () => {
  const { hooks, nodes } = await page();
  hooks.renderDailyJob({status: "failed", day: "2026-10-09",
    automation: {enabled: true, day: "2026-10-10", gate_reason: "market_closed"},
    historical_recovery: {day: "2026-10-09", generated_at: "2026-10-10T01:50:00+08:00"}});
  assert.match(nodes.get("#daily-job-status").textContent, /历史研究候选已补算/);
  assert.match(nodes.get("#daily-job-status").textContent, /不是当日实时信号/);
  assert.match(nodes.get("#daily-job-status").textContent, /休市/);
  assert.equal(nodes.get("#btn-strategy-generate").disabled, true);
});

test("before-close generation is disabled, while current-day completion does not say failure", async () => {
  const { hooks, nodes } = await page();
  hooks.renderDailyJob({status: "idle", day: "2026-10-09", automation: {enabled: true, day: "2026-10-09", gate_reason: "waiting_for_close", server_time: "2026-10-09T10:15:00+08:00"}});
  assert.equal(nodes.get("#btn-strategy-generate").disabled, true);
  assert.equal(nodes.get("#btn-strategy-generate").textContent, "等待 15:05（今日候选）");
  assert.match(nodes.get("#daily-job-status").textContent, /等待 15:05/);
  assert.match(nodes.get("#daily-job-status").textContent, /10:15:00/);
  assert.match(nodes.get("#daily-job-status").textContent, /不补算昨日/);
  assert.doesNotMatch(nodes.get("#daily-job-status").textContent, /尚未启动/);
  hooks.renderDailyJob({status: "no_candidates", day: "2026-10-09", automation: {enabled: true, day: "2026-10-09"}});
  assert.equal(nodes.get("#btn-strategy-generate").disabled, true);
  assert.match(nodes.get("#daily-job-status").textContent, /任务完成，本次没有入选股票/);
});

test("background completion refreshes the candidate display without another POST", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "exploratory", data_as_of: "2026-10-09", groups: {breakout: []}, cards: {}};
  vm.runInContext("api = async path => {calls.push(path); return {data: saved};};", context);
  hooks.state.jobLastStatus = "running";
  hooks.renderDailyJob({status: "success", day: "2026-10-09", automation: {enabled: true, day: "2026-10-09"}});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(context.calls.includes("/api/strategy-candidates"));
  assert.equal(nodes.get("#btn-strategy-generate").textContent, "今日已完成");
});

test("date lookup requests Oct 8 explicitly and labels recovered candidates", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "retrospective_research", retrospective: true, generated_at: "2026-10-09T10:00:00+08:00",
    data_as_of: "2026-10-08", freeze_status: "retrospective_not_frozen", cards: {},
    groups: {breakout: [{code: "600001", name: "测试股", score: 80}]}, scope: {expected: 1, eligible: 1}, diagnostics: {daily_failed: 0}};
  vm.runInContext("api = async path => {calls.push(path); return {data: saved};};", context);
  await hooks.loadStrategyCandidates(true, "2026-10-08");
  assert.equal(context.calls.at(-1), "/api/strategy-candidates?history=1&date=2026-10-08");
  assert.match(nodes.get("#strategy-freeze-message").textContent, /历史补算研究.*2026-10-09/);
  assert.match(nodes.get("#strategy-candidate-evidence").innerHTML, /历史补算研究/);
  assert.equal(nodes.get("#strategy-history-date").value, "2026-10-08");
});

test("closed-session candidate page offers history without waiting for today's close", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "market_closed", groups: {}, cards: {}, previous_report: {data_as_of: "2026-10-09"}};
  vm.runInContext("api = async () => ({data: saved});", context);
  await hooks.loadStrategyCandidates();
  assert.match(nodes.get("#strategy-freeze-message").textContent, /今日休市.*2026-10-09/);
  assert.doesNotMatch(nodes.get("#strategy-freeze-message").textContent, /等待 15:05/);
  assert.match(nodes.get("#strategy-candidate-evidence").innerHTML, /可查看历史候选/);
});

test("risk review distinguishes old scores, ratio percentages, and excluded archived candidates", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "exploratory", data_as_of: "2026-10-30", cards: {},
    groups: {breakout: [{code: "600001", name: "普通股", score: 70, score_basis: "archived_score_not_recomputed",
      risk: {metrics: {return10: .1234, bias20: .10, turnover: .02}}}]},
    risk_review: {status: "archived_candidates_reviewed", unknown: [], blocked: [
      {code: "002724", name: "海洋王", score: 90, risk_messages: ["严重偏离20日均线"]}]}};
  vm.runInContext("api = async () => ({data: saved});", context);
  await hooks.loadStrategyCandidates(true, "2026-10-30");
  const html = nodes.get("#strategy-candidate-evidence").innerHTML;
  assert.match(html, /70（旧版）/);
  assert.match(html, /12\.34%/);
  assert.match(html, /换手 \+2\.00%/);
  assert.match(html, /技术匹配分（非胜率）/);
  assert.match(html, /<details>.*海洋王.*严重偏离20日均线/);
  assert.doesNotMatch(html.split("<details>")[0], /海洋王/);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /旧分数未重算/);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /公告风险尚未核实/);
});

test("original-only empty state offers an explicit revised lookup with version and generation time", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "missing_original", requested_date: "2026-10-08", groups: {}, cards: {},
    alternative_view: {view: "revised", date: "2026-10-08"}};
  vm.runInContext("api = async path => {calls.push(path); return {data: saved};};", context);
  await hooks.loadStrategyCandidates(true, "2026-10-08");
  assert.match(nodes.get("#strategy-freeze-message").textContent, /没有当日原始留档/);
  assert.equal(nodes.get("#btn-strategy-revised").classList.contains("hidden"), false);
  context.saved = {status: "retrospective_research", view: "revised", origin: "risk_revision", groups: {}, cards: {},
    source_version: "v3", source_generated_at: "2026-11-02T10:00:00+08:00", data_as_of: "2026-10-08"};
  nodes.get("#btn-strategy-revised").listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.calls.at(-1), "/api/strategy-candidates?history=1&date=2026-10-08&view=revised");
  assert.match(nodes.get("#strategy-freeze-message").textContent, /不计入原始信号收益.*v3.*2026-11-02/);
});

test("original rows link their frozen snapshot and revised rows cannot link a fabricated performance", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {view: "original", origin: "original_daily", status: "exploratory", data_as_of: "2026-10-08", cards: {},
    groups: {breakout: [{code: "600001", name: "原始股", score: 90, snapshot_id: 7}]}};
  vm.runInContext("api = async () => ({data: saved});", context);
  await hooks.loadStrategyCandidates(true, "2026-10-08");
  assert.match(nodes.get("#strategy-candidate-evidence").innerHTML, /\/api\/strategy-snapshot\/7/);
  assert.match(nodes.get("#strategy-freeze-message").textContent, /名单及评分未按新规则改写/);
  context.saved.view = "revised";
  await hooks.loadStrategyCandidates(true, "2026-10-08");
  assert.doesNotMatch(nodes.get("#strategy-candidate-evidence").innerHTML, /\/api\/strategy-snapshot\/7/);
});

test("late original response cannot replace a revised view", async () => {
  const { context, hooks, nodes } = await page();
  context.pending = {};
  vm.runInContext("api = path => new Promise(resolve => pending[path] = resolve);", context);
  const original = hooks.loadStrategyCandidates(true, "2026-10-08");
  nodes.get("#strategy-candidate-view").value = "revised";
  const revised = hooks.loadStrategyCandidates(true, "2026-10-08");
  context.pending["/api/strategy-candidates?history=1&date=2026-10-08&view=revised"]({data: {
    status: "exploratory", view: "revised", origin: "risk_revision", cards: {}, groups: {}, source_version: "NEW"}});
  await revised;
  context.pending["/api/strategy-candidates?history=1&date=2026-10-08"]({data: {
    status: "exploratory", view: "original", origin: "original_daily", cards: {}, groups: {}, source_version: "OLD"}});
  await original;
  assert.match(nodes.get("#strategy-freeze-message").textContent, /NEW/);
  assert.doesNotMatch(nodes.get("#strategy-freeze-message").textContent, /OLD/);
});

test("daily job shows recovery budget, independent scheduler, missing sessions and heartbeat", async () => {
  const { hooks, nodes } = await page();
  hooks.renderDailyJob({status: "failed", day: "2026-11-11", automation: {day: "2026-11-11"},
    retry: {status: "waiting", automatic_attempts: 1, max_automatic_attempts: 3, next_retry_at: "2026-11-11T16:10:00+08:00"},
    scheduler: {scheduler: "windows_task_or_cli", fresh: true, checked_at: "2026-11-11T16:00:00+08:00", status: "waiting"},
    heartbeat: {status: "delayed"},
    gaps: {missing_original_dates: ["2026-11-04"], rows: [{day: "2026-11-04", market_snapshot_status: "missing"}]},
    progress: {steps: {environment: {status: "partial"}}}});
  const message = nodes.get("#daily-job-status").textContent;
  assert.match(message, /等待自动重试（1\/3）.*16:10:00/);
  assert.match(message, /心跳延迟/);
  assert.match(message, /独立调度/);
  assert.match(message, /独立系统调度最近检查正常/);
  assert.doesNotMatch(message, /自动任务：未启用/);
  assert.match(message, /缺少原始候选：2026-11-04/);
  assert.match(nodes.get("#daily-job-progress").textContent, /市场环境：部分完成/);
});

test("environment distinguishes total observations from a contiguous window", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "insufficient_daily_history", as_of: "2026-11-03", observed_sessions: 7,
    readiness: {contiguous_sessions: 2, required_sessions: 61, missing_dates: ["2026-10-30"],
      window_verified_sessions: 0, unverified_source_dates: ["2026-11-02", "2026-11-03"]}};
  vm.runInContext("api = async () => ({data: saved}); loadRegime = hooks.loadRegime;", context);
  await hooks.loadRegime();
  assert.match(nodes.get("#market-environment-status").textContent, /已积累 7\/61.*连续记录 2\/61.*来源已核验 0\/61.*来源证据待核实.*2026-10-30/);
});

test("prospective validation shows registration waiting and the future validation start", async () => {
  const { context, hooks, nodes } = await page();
  context.monitorData = {versions: [], snapshots: [], observations: []};
  context.validation = {status: "awaiting_prospective_signals", validation_start: "2026-11-26", registered_at: "2026-11-11T12:00:00+08:00",
    signal_days: 0, accepted_snapshots: 0};
  vm.runInContext("api = async path => ({data: path.startsWith('/api/strategy-monitor') ? monitorData : path === '/api/forward-validation' ? validation : {}});", context);
  await hooks.loadStrategyMonitor();
  assert.match(nodes.get("#daily-run-status").innerHTML, /等待登记后的真实信号.*2026-11-26.*0 个交易日/);
});

test("background completion leaves an explicitly selected historical view in place", async () => {
  const { context, hooks, nodes } = await page();
  context.saved = {status: "exploratory", data_as_of: "2026-10-08", cards: {}, groups: {}};
  vm.runInContext("api = async path => {calls.push(path); return {data: saved};};", context);
  nodes.get("#strategy-candidate-view").value = "revised";
  await hooks.loadStrategyCandidates(true, "2026-10-08");
  context.calls.length = 0;
  hooks.state.jobLastStatus = "running";
  hooks.renderDailyJob({status: "success", day: "2026-11-11", automation: {day: "2026-11-11"}});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!context.calls.includes("/api/strategy-candidates"));
  assert.equal(nodes.get("#strategy-candidate-view").value, "revised");
});
