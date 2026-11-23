"use strict";

const $ = (id) => document.getElementById(id);
const state = { lastSnapshot: null, autoTimer: null };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
function number(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}
function price(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  const digits = Math.abs(n) >= 100 ? 2 : Math.abs(n) >= 1 ? 4 : 8;
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}
function percent(value, digits = 1) { return `${number(value, digits)}%`; }
function apiHeaders() {
  const headers = { "Content-Type": "application/json" };
  const key = $("apiKey").value.trim();
  if (key) headers["X-API-Key"] = key;
  return headers;
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...apiHeaders(), ...(options.headers || {}) } });
  let payload;
  try { payload = await response.json(); } catch { payload = { detail: await response.text() }; }
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}
function setLoading(id, active) { $(id).classList.toggle("hidden", !active); }
function showError(id, error) {
  const element = $(id);
  element.textContent = error instanceof Error ? error.message : String(error);
  element.classList.remove("hidden");
}
function clearError(id) { $(id).classList.add("hidden"); $(id).textContent = ""; }
function actionClass(action) { return `action-${String(action || "").toLowerCase()}`; }

async function checkHealth() {
  try {
    const health = await fetch("/health").then((r) => r.json());
    $("healthBadge").textContent = `Online · v${health.version}`;
    $("healthBadge").className = "badge good";
  } catch {
    $("healthBadge").textContent = "Service unavailable";
    $("healthBadge").className = "badge bad";
  }
}

function livePayload() {
  return {
    exchange_id: $("liveExchange").value.trim(),
    market_type: $("liveMarketType").value,
    symbol: $("liveSymbol").value.trim(),
    execution_timeframe: $("liveExecution").value.trim(),
    context_timeframes: $("liveContexts").value.trim(),
    equity: Number($("liveEquity").value),
    risk_percent: Number($("liveRisk").value),
    maximum_notional_multiple: Number($("liveLeverage").value),
    chart_bars: Number($("liveChartBars").value),
    limit: 500,
  };
}
async function analyzeMarket() {
  clearError("liveError"); setLoading("liveLoading", true); $("liveResults").classList.add("hidden");
  try {
    const snapshot = await api("/v1/market-snapshot", { method: "POST", body: JSON.stringify(livePayload()) });
    state.lastSnapshot = snapshot;
    renderLive(snapshot);
    $("liveResults").classList.remove("hidden");
  } catch (error) { showError("liveError", error); }
  finally { setLoading("liveLoading", false); }
}
function renderLive(snapshot) {
  const signal = snapshot.signal;
  $("regimeValue").textContent = signal.regime.replaceAll("_", " ").toUpperCase();
  $("actionValue").textContent = signal.action.replaceAll("_", " ").toUpperCase();
  $("actionValue").className = actionClass(signal.action);
  $("scoreValue").textContent = `${number(signal.score, 1)} / 100`;
  $("confidenceValue").textContent = percent(signal.confidence, 1);
  $("consensusValue").textContent = percent(signal.consensus * 100, 0);
  $("candleValue").textContent = new Date(signal.generated_at).toLocaleString();
  renderTradePlan(signal);
  renderReasons(signal);
  renderTimeframes(signal.timeframe_states || []);
  drawCandles($("priceChart"), snapshot.candles || [], signal.entry_plan);
}
function renderTradePlan(signal) {
  const plan = signal.entry_plan;
  if (!plan) {
    $("tradePlan").innerHTML = `<div class="message info" style="grid-column:1/-1">No confirmed entry plan. ${signal.trigger_price ? `Watch trigger: <strong>${price(signal.trigger_price)}</strong>` : "Wait for directional alignment and confirmation."}</div>`;
    return;
  }
  const entries = [
    ["Side", plan.side.toUpperCase()], ["Setup", plan.setup_type.toUpperCase()],
    ["Entry zone", `${price(plan.entry_low)} – ${price(plan.entry_high)}`], ["Reference", price(plan.reference_entry)],
    ["Stop loss", price(plan.stop_loss)], ["TP1", price(plan.take_profit_1)],
    ["TP2", price(plan.take_profit_2)], ["TP3", price(plan.take_profit_3)],
    ["Base-asset quantity", number(plan.position_size, 8)], ["Notional", number(plan.notional_value, 2)],
    ["Planned risk", number(plan.actual_risk_amount, 2)], ["Est. round-trip cost", number(plan.estimated_round_trip_cost, 2)],
  ];
  $("tradePlan").innerHTML = entries.map(([label, value]) => `<div class="plan-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}
function renderReasons(signal) {
  const reasons = (signal.reasons || []).map((text) => `<div class="reason">${escapeHtml(text)}</div>`);
  const warnings = (signal.warnings || []).map((text) => `<div class="reason warning">${escapeHtml(text)}</div>`);
  $("decisionReasons").innerHTML = [...reasons, ...warnings].join("") || '<p class="muted">No explanations returned.</p>';
}
function renderTimeframes(states) {
  $("timeframeRows").innerHTML = states.map((item) => `<tr>
    <td>${escapeHtml(item.timeframe)}</td><td>${escapeHtml(item.regime.replaceAll("_", " "))}</td>
    <td>${number(item.score, 1)}</td><td>${percent(item.confidence, 1)}</td><td>${number(item.adx, 1)}</td>
    <td>${number(item.rsi, 1)}</td><td>${percent(item.atr_percent * 100, 2)}</td><td>${number(item.volume_ratio, 2)}</td>
  </tr>`).join("");
}

function scannerPayload() {
  const mode = $("scanMode").value;
  return {
    exchange_id: $("scanExchange").value.trim(), market_type: $("scanMarketType").value,
    symbols: mode === "explicit" ? $("scanSymbols").value.trim() : "", auto_universe: mode === "auto",
    quote_currencies: $("scanQuotes").value.trim(), max_markets: Number($("scanMax").value),
    minimum_quote_volume: Number($("scanVolume").value), maximum_spread_bps: Number($("scanSpread").value),
    execution_timeframe: $("scanExecution").value.trim(), context_timeframes: $("scanContexts").value.trim(),
    equity: Number($("liveEquity").value), risk_percent: Number($("liveRisk").value),
    maximum_notional_multiple: Number($("liveLeverage").value), limit: 500,
  };
}
async function runScan() {
  clearError("scanError"); setLoading("scanLoading", true); $("scanResults").classList.add("hidden");
  try {
    const report = await api("/v1/scan", { method: "POST", body: JSON.stringify(scannerPayload()) });
    $("scanSummary").textContent = `${report.analyzed_markets} analyzed · ${report.failed_markets} failed`;
    $("scanRows").innerHTML = report.items.map((item) => {
      if (!item.signal) return `<tr><td>${item.rank}</td><td>${escapeHtml(item.candidate.symbol)}</td><td colspan="8" class="negative-text">${escapeHtml(item.error)}</td></tr>`;
      const signal = item.signal;
      return `<tr><td>${item.rank}</td><td><strong>${escapeHtml(item.candidate.symbol)}</strong></td>
        <td class="${actionClass(signal.action)}">${escapeHtml(signal.action.replaceAll("_", " ").toUpperCase())}</td>
        <td>${escapeHtml(signal.regime.replaceAll("_", " "))}</td><td>${percent(signal.confidence, 1)}</td>
        <td>${percent(signal.consensus * 100, 0)}</td><td>${number(signal.score, 1)}</td>
        <td>${number(item.candidate.quote_volume, 0)}</td><td>${item.candidate.spread_bps == null ? "—" : number(item.candidate.spread_bps, 1)}</td>
        <td>${price(signal.trigger_price)}</td></tr>`;
    }).join("");
    $("scanResults").classList.remove("hidden");
  } catch (error) { showError("scanError", error); }
  finally { setLoading("scanLoading", false); }
}

function backtestPayload() {
  return {
    exchange_id: $("btExchange").value.trim(), market_type: $("btMarketType").value,
    symbol: $("btSymbol").value.trim(), execution_timeframe: $("btExecution").value.trim(),
    context_timeframes: $("btContexts").value.trim(), start: $("btStart").value,
    end: $("btEnd").value, equity: Number($("btEquity").value), risk_percent: Number($("btRisk").value),
    maximum_notional_multiple: Number($("btLeverage").value),
    monte_carlo_simulations: Number($("btMonteCarlo").value), resume: true, strict_gaps: false,
  };
}
async function runBacktest(fullValidation = false) {
  clearError("btError"); setLoading("btLoading", true); $("btResults").classList.add("hidden");
  try {
    const endpoint = fullValidation ? "/v1/research/validate" : "/v1/backtest/live";
    const response = await api(endpoint, { method: "POST", body: JSON.stringify(backtestPayload()) });
    if (fullValidation) renderValidationResponse(response); else renderBacktestResponse(response);
    $("btResults").classList.remove("hidden");
  } catch (error) { showError("btError", error); }
  finally { setLoading("btLoading", false); }
}
function renderBacktestResponse(response) {
  const result = response.result;
  const diagnostics = response.diagnostics;
  renderBacktestCore(result, diagnostics);
  $("validationGate").innerHTML = '<p class="muted">Run Full validation to test untouched data, cost stress, and Monte Carlo risk.</p>';
}
function renderValidationResponse(response) {
  const validation = response.validation;
  const base = validation.base;
  renderBacktestCore(base.summary, base);
  renderGate(validation.gate, validation);
}
function renderBacktestCore(result, diagnostics) {
  const pf = result.profit_factor == null ? "∞" : number(result.profit_factor, 2);
  const metrics = [
    ["Total return", percent(result.total_return_percent, 2), result.total_return_percent >= 0],
    ["Net P&L", number(result.net_pnl, 2), result.net_pnl >= 0], ["Trades", number(result.trades, 0), null],
    ["Win rate", percent(result.win_rate_percent, 1), null], ["Profit factor", pf, (result.profit_factor ?? 99) >= 1],
    ["Expectancy", `${number(result.expectancy_r, 3)}R`, result.expectancy_r >= 0],
    ["Max drawdown", percent(result.max_drawdown_percent, 2), result.max_drawdown_percent <= 25],
    ["Ending equity", number(result.ending_equity, 2), result.ending_equity >= result.starting_equity],
  ];
  $("btMetrics").innerHTML = metrics.map(([label, value, positive]) => `<article class="metric-card ${positive === null ? "" : positive ? "positive" : "negative"}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("");
  drawLineChart($("equityChart"), result.equity_curve || [], "equity");
  const q = diagnostics.trade_quality || {};
  const quality = [
    ["Average win", `${number(q.average_win_r, 3)}R`], ["Average loss", `${number(q.average_loss_r, 3)}R`],
    ["Payoff ratio", number(q.payoff_ratio, 2)], ["Median trade", `${number(q.median_r, 3)}R`],
    ["SQN", number(q.system_quality_number, 2)], ["Longest loss streak", number(q.longest_loss_streak, 0)],
    ["Average holding", `${number(q.average_holding_bars, 1)} bars`], ["Largest loss", `${number(q.largest_loss_r, 2)}R`],
  ];
  $("tradeQuality").innerHTML = quality.map(([label, value]) => `<div>${escapeHtml(label)}</div><div><strong>${escapeHtml(value)}</strong></div>`).join("");
  const yearly = diagnostics.yearly || {};
  $("yearlyRows").innerHTML = Object.entries(yearly).sort().map(([year, item]) => `<tr><td>${escapeHtml(year)}</td><td>${item.trades}</td><td>${percent(item.win_rate_percent, 1)}</td><td class="${item.net_pnl >= 0 ? "positive-text" : "negative-text"}">${number(item.net_pnl, 2)}</td><td>${number(item.average_r, 3)}R</td><td>${item.profit_factor == null ? "∞" : number(item.profit_factor, 2)}</td></tr>`).join("") || '<tr><td colspan="6">No completed trades.</td></tr>';
  $("tradeRows").innerHTML = (result.trades_detail || []).slice().reverse().map((trade) => `<tr><td>${new Date(trade.entry_time).toLocaleString()}</td><td>${new Date(trade.exit_time).toLocaleString()}</td><td>${escapeHtml(trade.side)}</td><td>${escapeHtml(trade.setup_type)}</td><td>${price(trade.entry_price)}</td><td>${price(trade.exit_price)}</td><td class="${trade.net_pnl >= 0 ? "positive-text" : "negative-text"}">${number(trade.net_pnl, 2)}</td><td>${number(trade.r_multiple, 3)}R</td><td>${escapeHtml(trade.exit_reason)}</td></tr>`).join("") || '<tr><td colspan="9">No completed trades.</td></tr>';
}
function renderGate(gate, validation) {
  const statusClass = gate.status.startsWith("PASS") ? "positive-text" : gate.status === "FAIL" ? "negative-text" : "";
  const checks = gate.checks.map((check) => `<div class="gate-check"><span class="${check.passed ? "gate-pass" : "gate-fail"}">${check.passed ? "✓" : "✕"}</span><span>${escapeHtml(check.name.replaceAll("_", " "))}<small class="muted"><br>${escapeHtml(check.requirement)}</small></span><strong>${escapeHtml(number(check.value, 3))}</strong></div>`).join("");
  const mc = validation.monte_carlo || {};
  $("validationGate").innerHTML = `<h3 class="${statusClass}">${escapeHtml(gate.status.replaceAll("_", " "))}</h3>${checks}<p class="muted">Monte Carlo chance of ending below start: ${mc.probability_ending_below_start == null ? "—" : percent(mc.probability_ending_below_start * 100, 1)}</p>`;
}

async function refreshJournal() {
  clearError("journalError");
  try {
    const [signals, backtests, datasets] = await Promise.all([
      api("/v1/journal/signals?limit=30"), api("/v1/journal/backtests?limit=20"), api("/v1/datasets?limit=50")
    ]);
    $("journalMetrics").innerHTML = [
      ["Recent signals", signals.items.length], ["Recent backtests", backtests.items.length], ["Datasets", datasets.items.length]
    ].map(([label, value]) => `<article class="metric-card"><span>${label}</span><strong>${value}</strong></article>`).join("");
    $("signalJournal").innerHTML = signals.items.map((item) => `<div class="journal-item"><strong>${escapeHtml(item.symbol)} · <span class="${actionClass(item.action)}">${escapeHtml(item.action.toUpperCase())}</span></strong><div class="journal-meta">${escapeHtml(item.regime)} · score ${number(item.score, 1)} · ${new Date(item.generated_at).toLocaleString()}</div></div>`).join("") || '<p class="muted">No signals recorded yet.</p>';
    $("datasetJournal").innerHTML = datasets.items.map((item) => `<div class="journal-item"><strong>${escapeHtml(item.symbol)} · ${escapeHtml(item.timeframe)}</strong><div class="journal-meta">${number(item.rows, 0)} rows · ${item.gaps} gaps · ${escapeHtml(item.exchange_id)} · updated ${new Date(item.updated_at).toLocaleString()}</div></div>`).join("") || '<p class="muted">No historical datasets yet.</p>';
    $("backtestJournal").innerHTML = backtests.items.map((item) => {
      const result = item.result.summary || item.result.base?.summary || item.result.result || {};
      return `<div class="journal-item"><strong>${escapeHtml(item.request.symbol || "Unknown")} · ${escapeHtml(item.request.execution_timeframe || "")}</strong><div class="journal-meta">${new Date(item.created_at).toLocaleString()} · return ${number(result.total_return_percent, 2)}% · trades ${number(result.trades, 0)}</div></div>`;
    }).join("") || '<p class="muted">No backtests recorded yet.</p>';
  } catch (error) { showError("journalError", error); }
}

function sizeCanvas(canvas) {
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(300, Math.floor(rect.width * ratio));
  canvas.height = Math.floor(Number(canvas.getAttribute("height") || 300) * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx: context, width: rect.width, height: Number(canvas.getAttribute("height") || 300) };
}
function drawCandles(canvas, candles, plan) {
  const { ctx, width, height } = sizeCanvas(canvas);
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#071323"; ctx.fillRect(0, 0, width, height);
  if (!candles.length) return;
  const pad = { left: 12, right: 72, top: 16, bottom: 28 };
  const values = candles.flatMap((c) => [c.low, c.high, c.ema_fast, c.ema_mid, c.ema_slow].filter((v) => v != null).map(Number));
  if (plan) values.push(plan.entry_low, plan.entry_high, plan.stop_loss, plan.take_profit_1, plan.take_profit_2, plan.take_profit_3);
  let min = Math.min(...values), max = Math.max(...values); const margin = (max - min || max * .01 || 1) * .07; min -= margin; max += margin;
  const x = (i) => pad.left + i * ((width - pad.left - pad.right) / Math.max(1, candles.length - 1));
  const y = (v) => pad.top + (max - v) / (max - min) * (height - pad.top - pad.bottom);
  ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.fillStyle = "#8395af"; ctx.font = "11px system-ui";
  for (let i = 0; i <= 5; i++) { const v = min + (max - min) * i / 5; const yy = y(v); ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke(); ctx.fillText(price(v), width - pad.right + 7, yy + 4); }
  const step = (width - pad.left - pad.right) / candles.length; const body = Math.max(1, Math.min(8, step * .62));
  candles.forEach((c, i) => { const xx = x(i), up = c.close >= c.open; ctx.strokeStyle = up ? "#35d39a" : "#ff6f7d"; ctx.fillStyle = ctx.strokeStyle; ctx.beginPath(); ctx.moveTo(xx, y(c.high)); ctx.lineTo(xx, y(c.low)); ctx.stroke(); const top = y(Math.max(c.open, c.close)), bottom = y(Math.min(c.open, c.close)); ctx.fillRect(xx - body / 2, top, body, Math.max(1, bottom - top)); });
  drawSeries(ctx, candles, "ema_fast", x, y, "#78b7ff"); drawSeries(ctx, candles, "ema_mid", x, y, "#f6bd60"); drawSeries(ctx, candles, "ema_slow", x, y, "#b591ff");
  if (plan) { drawLevel(ctx, y(plan.entry_low), width, pad, "#4ea1ff", "ENTRY"); drawLevel(ctx, y(plan.stop_loss), width, pad, "#ff6f7d", "STOP"); drawLevel(ctx, y(plan.take_profit_1), width, pad, "#35d39a", "TP1"); drawLevel(ctx, y(plan.take_profit_2), width, pad, "#35d39a", "TP2"); }
}
function drawSeries(ctx, candles, key, x, y, color) { ctx.strokeStyle = color; ctx.lineWidth = 1.35; ctx.beginPath(); let started = false; candles.forEach((c, i) => { if (c[key] == null) return; if (!started) { ctx.moveTo(x(i), y(c[key])); started = true; } else ctx.lineTo(x(i), y(c[key])); }); ctx.stroke(); }
function drawLevel(ctx, yy, width, pad, color, label) { ctx.strokeStyle = color; ctx.setLineDash([5, 4]); ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = color; ctx.fillText(label, pad.left + 4, yy - 4); }
function drawLineChart(canvas, points) {
  const { ctx, width, height } = sizeCanvas(canvas); ctx.clearRect(0, 0, width, height); ctx.fillStyle = "#071323"; ctx.fillRect(0, 0, width, height);
  if (!points.length) return; const values = points.map((p) => Number(p[1])); let min = Math.min(...values), max = Math.max(...values); const margin = (max - min || 1) * .08; min -= margin; max += margin;
  const pad = { left: 12, right: 72, top: 16, bottom: 26 }; const x = (i) => pad.left + i * ((width - pad.left - pad.right) / Math.max(1, points.length - 1)); const y = (v) => pad.top + (max - v) / (max - min) * (height - pad.top - pad.bottom);
  ctx.strokeStyle = "rgba(255,255,255,.07)"; ctx.fillStyle = "#8395af"; ctx.font = "11px system-ui"; for (let i = 0; i <= 4; i++) { const v = min + (max - min) * i / 4; const yy = y(v); ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke(); ctx.fillText(number(v, 0), width - pad.right + 7, yy + 4); }
  const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom); gradient.addColorStop(0, "rgba(78,161,255,.32)"); gradient.addColorStop(1, "rgba(78,161,255,0)"); ctx.beginPath(); ctx.moveTo(x(0), height - pad.bottom); points.forEach((p, i) => ctx.lineTo(x(i), y(Number(p[1])))); ctx.lineTo(x(points.length - 1), height - pad.bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill(); ctx.beginPath(); points.forEach((p, i) => i ? ctx.lineTo(x(i), y(Number(p[1]))) : ctx.moveTo(x(i), y(Number(p[1])))); ctx.strokeStyle = "#4ea1ff"; ctx.lineWidth = 2; ctx.stroke();
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((item) => item.classList.remove("active"));
    button.classList.add("active"); $(`tab-${button.dataset.tab}`).classList.add("active");
    if (button.dataset.tab === "journal") refreshJournal();
  }));
}
function setDates() {
  const end = new Date(); const start = new Date(); start.setUTCFullYear(end.getUTCFullYear() - 2);
  $("btEnd").value = end.toISOString().slice(0, 10); $("btStart").value = start.toISOString().slice(0, 10);
}
function bootstrap() {
  setupTabs(); setDates(); checkHealth();
  $("apiKey").value = localStorage.getItem("signalApiKey") || "";
  $("apiKey").addEventListener("change", () => localStorage.setItem("signalApiKey", $("apiKey").value));
  $("analyzeButton").addEventListener("click", analyzeMarket); $("scanButton").addEventListener("click", runScan);
  $("backtestButton").addEventListener("click", () => runBacktest(false)); $("validateButton").addEventListener("click", () => runBacktest(true));
  $("refreshJournalButton").addEventListener("click", refreshJournal);
  $("scanMode").addEventListener("change", () => { $("scanSymbols").disabled = $("scanMode").value === "auto"; });
  window.addEventListener("resize", () => { if (state.lastSnapshot) drawCandles($("priceChart"), state.lastSnapshot.candles || [], state.lastSnapshot.signal.entry_plan); });
}
document.addEventListener("DOMContentLoaded", bootstrap);
