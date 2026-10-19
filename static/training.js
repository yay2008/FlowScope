/* 历史训练页面：只绘制后端已经揭示的数据，不连接实时行情 WebSocket。 */
(function (root) {
  "use strict";

  function aggregateBars(bars, seconds = 300) {
    const result = [];
    for (const bar of bars) {
      const time = Math.floor(bar.time / seconds) * seconds;
      const last = result[result.length - 1];
      if (last && last.time === time) {
        last.high = Math.max(last.high, bar.high);
        last.low = Math.min(last.low, bar.low);
        last.close = bar.close;
        last.volume += bar.volume;
        if (last.delta != null && bar.delta != null) {
          for (const key of ["buy", "sell", "unknown", "delta"]) last[key] += bar[key];
          last.cvd = bar.cvd;
        }
      } else result.push({ ...bar, time });
    }
    return result;
  }

  // 同正式 K 线拆分的阴阳规则；训练只有 1m OHLCV，明确标记为估算。
  // 必须先逐分钟判向再聚合，不能按大周期整根阴阳重新分配买卖量。
  function trainingFlow(bars) {
    let cvd = 0;
    return bars.map((bar) => {
      const buy = bar.close > bar.open ? bar.volume : 0;
      const sell = bar.close < bar.open ? bar.volume : 0;
      const cvdOpen = cvd, delta = buy - sell;
      cvd += delta;
      return { ...bar, buy, sell, unknown: bar.volume - buy - sell, delta, cvdOpen, cvd, coverage: "estimated" };
    });
  }

  const indicators = root.FlowIndicators || (typeof module !== "undefined" && module.exports ? require("./indicators.js") : null);

  function trainingChartData(revealed, seconds, cfg, settings, htf = 0) {
    const minutes = trainingFlow(revealed);
    const bars = seconds === 60 ? minutes : aggregateBars(minutes, seconds);
    const derived = indicators.derive(bars, cfg, settings.bandK);
    // 只从本次已揭示前缀计算；不读取实时页历史，也不缓存复盘时见过的后续行情。
    derived.htfEma = indicators.htfEma(bars, htf > seconds ? htf : 0, indicators.HTF_EMA_PERIODS).lines;
    return { bars, derived, band: indicators.buildBandData(bars, derived.band),
      suite: indicators.buildSuiteData(bars, derived, cfg, settings.mode, settings.threshtype),
      pulse: indicators.buildPulseData(bars, derived, cfg) };
  }

  function emaPlotData(bars, lines, all, tail) {
    const from = all ? 0 : Math.max(0, bars.length - tail);
    return lines.map(line => bars.slice(from).map((bar, i) => ({ time: bar.time, value: line[from + i] }))
      .filter(point => point.value != null));
  }

  function trainingTradeMarkers(events, seconds) {
    // 多笔成交可以落在同一根合成 K 线，保留原有的成交先后顺序。
    return events.filter(event => event.kind === "fill").map(event => ({
      time: Math.floor(event.time / seconds) * seconds,
      position: event.side === "buy" ? "belowBar" : "aboveBar", color: event.side === "buy" ? "#ef656d" : "#35b69b",
      shape: event.side === "buy" ? "arrowUp" : "arrowDown", text: `${event.reason} ${event.qty}`,
    }));
  }

  function decimals(tick) {
    for (let digits = 0; digits < 8; digits++) {
      if (Math.abs(tick * 10 ** digits - Math.round(tick * 10 ** digits)) < 1e-7) return digits;
    }
    return 8;
  }

  function timeText(time, withDate = false) {
    const iso = new Date(time * 1000).toISOString();
    return withDate ? `${iso.slice(0, 10)} ${iso.slice(11, 16)}` : iso.slice(11, 16);
  }

  function eventTimeText(event) {
    return event.kind === "fill" ? timeText(event.time) + (event.phase === "intrabar" ? " 分钟内" : "") : timeText(event.time + 60);
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { aggregateBars, trainingFlow, trainingChartData,
    emaPlotData, trainingTradeMarkers, decimals, timeText };
  if (typeof document === "undefined") return;

  const $ = (id) => document.getElementById(id);
  let state = null, finalView = null, busy = false, timer = null, playing = false, epoch = 0;
  let datasetList = [], chartPair = null, equity = null, formPosition = "", chartSession = "";
  let indicatorConfig = null;
  let detailPanel = null, chartLayout = "split";
  const narrowLayout = window.matchMedia("(max-width: 900px)");
  try {
    const saved = localStorage.getItem("flowscope.trainingLayout");
    if (["split", "main", "context"].includes(saved)) chartLayout = saved;
  } catch (_) { /* 布局偏好不可用时使用双图 */ }
  const indicatorSettings = { mode: "cvd", threshtype: "Z-SCORE", bandK: indicators.BAND.kDefault };
  const chartTimeframes = [60, 300];
  const trainingTimeframes = ChartView.TF_CHOICES.filter(tf => tf >= 60);
  const indicatorShown = chartTimeframes.map(() => ({ band: true, bandMid: false,
    ema: true, emaAll: false, emaLong: true, htfEma: true, htfEmaAll: false, htf: 0 }));
  try {
    const saved = JSON.parse(localStorage.getItem("flowscope.trainingIndicators") || "null");
    if (saved) {
      for (const [key, choices] of [["mode", indicators.MODE_CHOICES], ["threshtype", indicators.THRESH_CHOICES]]) {
        if (choices.some(([value]) => value === saved[key])) indicatorSettings[key] = saved[key];
      }
      if (indicators.BAND.kOptions.includes(saved.bandK)) indicatorSettings.bandK = saved.bandK;
      indicatorShown.forEach((shown, i) => {
        for (const key of ["band", "bandMid", "ema", "emaAll", "emaLong", "htfEma", "htfEmaAll"]) {
          if (typeof saved.shown?.[i]?.[key] === "boolean") shown[key] = saved.shown[i][key];
        }
        if (trainingTimeframes.includes(saved.timeframes?.[i])) chartTimeframes[i] = saved.timeframes[i];
        if (trainingTimeframes.includes(saved.shown?.[i]?.htf)) shown.htf = saved.shown[i].htf;
      });
    }
  } catch (_) { /* 训练偏好独立保存，存储不可用不影响使用 */ }
  const num = (value) => value == null ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const nullable = (id) => $(id).value === "" ? null : Number($(id).value);
  const requestId = () => crypto.randomUUID();
  const notice = (message, error = false) => {
    $("notice").textContent = message;
    $("notice").title = message;
    $("notice").classList.toggle("error", error);
    $("notice").setAttribute("role", error ? "alert" : "status");
  };

  async function api(path, body, retry = false) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/training${path}`, {
        method: body === undefined ? "GET" : "POST", cache: "no-store", signal: controller.signal,
        ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
      });
      const result = await response.json();
      if (!response.ok) {
        const error = new Error(typeof result.detail === "string" ? result.detail : "请求失败，请检查输入");
        error.status = response.status;
        throw error;
      }
      return result;
    } catch (error) {
      // 超时可能发生在提交成功之后；只用相同请求编号重试，避免重复推进或成交。
      if (retry && !error.status) return api(path, body, false);
      if (!error.status) error.message = "连接中断或响应超时，请刷新状态；已提交的操作会保留";
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function table(host, headings, rows, empty) {
    host.replaceChildren();
    if (!rows.length) { host.append(element("p", empty, "empty")); return; }
    const result = element("table"), head = element("thead"), body = element("tbody"), header = element("tr");
    headings.forEach((label) => header.append(element("th", label)));
    head.append(header);
    rows.forEach((row) => {
      const tr = element("tr");
      row.forEach((value) => {
        const td = element("td");
        if (value instanceof Node) td.append(value); else td.textContent = String(value ?? "—");
        tr.append(td);
      });
      body.append(tr);
    });
    result.append(head, body); host.append(result);
  }

  function metrics(host, items) {
    host.replaceChildren(...items.map(([label, value, signed]) => {
      const item = element("div", null, "metric");
      item.title = `${label}：${typeof value === "string" ? value : num(value)}${host.id === "account" ? " 元" : ""}`;
      item.append(element("span", label, "metric-label"), element("span", typeof value === "string" ? value : num(value),
        `metric-value${signed && value > 0 ? " positive" : signed && value < 0 ? " negative" : ""}`));
      return item;
    }));
  }

  function pause() {
    playing = false; clearTimeout(timer); timer = null;
    updatePlayButton();
  }

  function updatePlayButton() {
    const text = playing ? "暂停" : "播放";
    $("play").replaceChildren(element("span", text), element("kbd", "Space"));
    $("play").setAttribute("aria-label", text);
    $("play").setAttribute("aria-pressed", String(playing));
  }

  function applyChartLayout() {
    const effective = narrowLayout.matches && chartLayout === "split" ? "main" : chartLayout;
    $("work").dataset.layout = effective;
    document.querySelectorAll(".chart-switch button").forEach(button =>
      button.setAttribute("aria-pressed", String(button.dataset.layout === effective)));
  }

  function workspaceMode(active) {
    document.body.classList.toggle("workspace-mode", active);
    $("workspace-bar").hidden = !active;
    const host = active ? $("dock-notice") : document.querySelector("main");
    if ($("notice").parentElement !== host) {
      if (active) host.append($("notice")); else host.prepend($("notice"));
    }
    applyChartLayout();
  }

  function showDetail(name) {
    if (name === "review" && state?.status !== "completed") return;
    const previous = detailPanel;
    const restoreFocus = !name && $("detail-drawer").contains(document.activeElement);
    detailPanel = name;
    $("detail-drawer").hidden = !name;
    for (const id of ["journal", "review", "rules"]) $(id).hidden = id !== name;
    document.querySelectorAll("[data-detail]").forEach(button =>
      button.setAttribute("aria-expanded", String(button.dataset.detail === name)));
    if (name) {
      pause();
      $("detail-title").textContent = { journal: "操作与成交", review: "训练复盘", rules: "成交规则与指标口径" }[name];
      if (name === "review") renderReview();
    } else if (restoreFocus && previous) {
      document.querySelector(`[data-detail="${previous}"]`).focus({ preventScroll: true });
    }
  }

  function updateControls() {
    const active = state && state.status === "active", pos = state && state.position, pending = state && state.pending;
    for (const id of ["step", "play", "finish"]) $(id).disabled = !active || busy;
    for (const id of ["buy", "sell"]) $(id).disabled = !active || busy || !!pos || !!pending;
    $("close-position").disabled = !active || busy || !pos || !!pending;
    $("update-stops").disabled = !active || busy || !pos;
    $("cancel-order").disabled = !active || busy;
    $("new-training").disabled = busy;
    $("qty").disabled = !active || !!pos || busy;
    $("review-slider").disabled = busy;
    $("review-toggle").disabled = state?.status !== "completed";
    for (const id of ["stop-loss", "take-profit", "trade-note"]) $(id).disabled = !active || busy;
  }

  async function command(action, options = {}) {
    if (!state || busy || state.status !== "active") return false;
    if (action !== "step") pause();
    busy = true; updateControls();
    const currentEpoch = epoch, identity = state.id;
    const body = { action, ...options, revision: state.revision, requestId: requestId() };
    try {
      const result = await api(`/sessions/${identity}/commands`, body, true);
      if (currentEpoch !== epoch) return false;
      render(result);
      const last = result.events[result.events.length - 1];
      if (action !== "step" || (last && ["reject", "wait", "fill", "finish"].includes(last.kind))) {
        notice(last ? `${eventTimeText(last)} · ${last.reason || "已保存"}` : "已保存");
      }
      if (result.status !== "active") { pause(); refreshHistory().catch(() => {}); }
      return true;
    } catch (error) {
      pause(); notice(error.message, true);
      try { const latest = await api(`/sessions/${identity}`); if (epoch === currentEpoch) render(latest); } catch (_) { /* 保留原错误 */ }
      return false;
    } finally { busy = false; updateControls(); }
  }

  async function playNext() {
    if (!playing || !state || state.status !== "active") return;
    if (!await command("step")) { pause(); return; }
    if (playing) timer = setTimeout(playNext, Number($("speed").value));
  }

  function chartOptions(height) {
    return { autoSize: true, height, layout: { background: { type: "solid", color: "#151b26" }, textColor: "#aab4c5", fontFamily: "Segoe UI, Microsoft YaHei, sans-serif" },
      grid: { vertLines: { color: "#222a38" }, horzLines: { color: "#222a38" } },
      rightPriceScale: { borderColor: "#303a4c" }, timeScale: { borderColor: "#303a4c", timeVisible: true, secondsVisible: false, rightOffset: 3 },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal } };
  }

  function saveIndicators(view = null, reset = false) {
    try { localStorage.setItem("flowscope.trainingIndicators", JSON.stringify({ ...indicatorSettings,
      shown: indicatorShown, timeframes: chartTimeframes })); } catch (_) { /* 忽略 */ }
    if (state && chartPair) {
      for (const target of view ? [view] : chartPair) renderChart(target, state.bars, reset);
    }
  }

  function indicatorControls(view, index) {
    const name = index === 0 ? "左图" : "右图";
    const shown = indicatorShown[index];
    const timeframe = $(index === 0 ? "main-timeframe" : "context-timeframe");
    for (const tf of ChartView.TF_CHOICES) {
      const option = element("option", ChartView.tfLabel(tf) + (tf < 60 ? "（需秒级数据）" : ""));
      option.value = tf; option.disabled = tf < 60; timeframe.append(option);
    }
    timeframe.value = String(chartTimeframes[index]);
    timeframe.addEventListener("change", () => {
      const tf = Number(timeframe.value);
      if (!trainingTimeframes.includes(tf)) { timeframe.value = String(chartTimeframes[index]); return; }
      pause(); chartTimeframes[index] = tf; view.hoverTime = null; saveIndicators(view, true);
    });
    const controls = element("div", null, "training-indicators");
    const waveGroup = element("div", null, "training-indicator-group");
    const bandToggle = element("button", "FlowWave 带");
    bandToggle.type = "button"; bandToggle.setAttribute("aria-label", `${name} FlowWave 带`);
    bandToggle.title = "价格回归通道；信号大于 80 染红，小于 20 染绿。只使用当前已揭示行情。";
    bandToggle.addEventListener("click", () => { shown.band = !shown.band; saveIndicators(view); });
    const bandK = element("button"); bandK.type = "button";
    bandK.setAttribute("aria-label", `${name} FlowWave 带宽`);
    bandK.addEventListener("click", () => {
      const choices = indicators.BAND.kOptions;
      indicatorSettings.bandK = choices[(choices.indexOf(indicatorSettings.bandK) + 1) % choices.length]; saveIndicators();
    });
    const midToggle = element("button", "中线"); midToggle.type = "button";
    midToggle.setAttribute("aria-label", `${name} FlowWave 中线`);
    midToggle.addEventListener("click", () => { shown.bandMid = !shown.bandMid; saveIndicators(view); });
    const waveReadout = element("span", null, "training-wave-readout");
    waveGroup.append(bandToggle, bandK, midToggle, waveReadout);
    const toggle = (key, label, description) => {
      const button = element("button", label); button.type = "button";
      button.setAttribute("aria-label", `${name} ${description}`);
      button.addEventListener("click", () => { shown[key] = !shown[key]; saveIndicators(view); });
      return button;
    };
    const emaGroup = element("div", null, "training-indicator-group training-ema-group");
    const emaToggle = toggle("ema", "EMA", "EMA 显示");
    emaToggle.title = "与实时行情相同的 EMA 21 / 55 / 100 / 200；按本图周期计算，首根收盘价播种。";
    const emaLong = toggle("emaLong", null, "EMA 100 / 200 显示");
    const emaLabels = indicators.EMA_PERIODS.map((period, j) => {
      const label = element("span", period, "training-ema-period"); label.style.color = ChartView.EMA_COLORS[j]; return label;
    });
    const emaRange = toggle("emaAll", null, "EMA 绘制范围");
    const rangeTitle = "近 100 / 全部：只改变画线范围，计算始终使用已揭示的全部历史。";
    emaRange.title = rangeTitle;
    emaGroup.append(emaToggle);
    emaLabels.forEach((label, j) => {
      if (ChartView.EMA_LONG_PERIODS.includes(indicators.EMA_PERIODS[j])) emaLong.append(label); else emaGroup.append(label);
    });
    emaGroup.append(emaLong, emaRange);
    const htfGroup = element("div", null, "training-indicator-group training-ema-group");
    const htfToggle = toggle("htfEma", "大周期 EMA", "大周期 EMA 显示");
    htfToggle.title = "EMA 21 / 55 使用最近已收盘的大周期值，画成半透明阶梯线；本图出现下一桶时更新。";
    const htfSelect = element("select"); htfSelect.setAttribute("aria-label", `${name} 大周期 EMA 周期`);
    htfSelect.addEventListener("change", () => { shown.htf = Number(htfSelect.value); saveIndicators(view); });
    const htfLabels = indicators.HTF_EMA_PERIODS.map(period => {
      const label = element("span", period, "training-ema-period");
      label.style.color = ChartView.EMA_COLORS[indicators.EMA_PERIODS.indexOf(period)]; return label;
    });
    const htfRange = toggle("htfEmaAll", null, "大周期 EMA 绘制范围"); htfRange.title = rangeTitle;
    htfGroup.append(htfToggle, htfSelect, ...htfLabels, htfRange);
    controls.append(waveGroup, emaGroup, htfGroup);
    view.host.parentElement.insertBefore(controls, view.host);

    const meterHead = element("div", null, "training-meter-head");
    const select = (choices, key, label) => {
      const input = element("select"); input.setAttribute("aria-label", `${name} FlowMeter ${label}`);
      input.title = `${label}两张图共用`;
      for (const [value, text] of choices) { const option = element("option", text); option.value = value; input.append(option); }
      input.addEventListener("change", () => { indicatorSettings[key] = input.value; saveIndicators(); });
      return input;
    };
    const mode = select(indicators.MODE_CHOICES, "mode", "模式"), threshold = select(indicators.THRESH_CHOICES, "threshtype", "阈值");
    const meterReadout = element("div", null, "training-meter-readout");
    const source = element("span", "1m K线估算", "training-flow-source");
    source.title = "买卖量、Delta、CVD：上涨分钟全量记买，下跌记卖，十字线量记未知。CVD 从本局背景起点累计。";
    meterHead.append(element("strong", "FlowMeter"), mode, threshold, source, meterReadout);
    meterHead.hidden = true; view.host.append(meterHead);
    view.meterHead = meterHead;
    view.meterResize = new ResizeObserver(() => updateMeterMargins(view));
    let tries = 0;
    function attach() {
      const pane = view.chart.panes()[1].getHTMLElement();
      if (pane) {
        pane.style.position = "relative"; pane.append(meterHead); meterHead.hidden = false;
        view.meterResize.observe(meterHead); view.meterResize.observe(pane); updateMeterMargins(view);
      }
      else if (++tries < 120) requestAnimationFrame(attach);
    }
    requestAnimationFrame(attach);
    view.controls = { timeframe, bandToggle, bandK, midToggle, waveReadout, mode, threshold, meterReadout,
      emaGroup, emaToggle, emaLong, emaRange, emaLabels, htfGroup, htfToggle, htfSelect, htfRange, htfLabels };
    view.index = index;
    view.chart.subscribeCrosshairMove((param) => {
      view.hoverTime = param.point && param.time != null ? param.time : null;
      renderIndicatorReadout(view);
    });
  }

  function updateMeterMargins(view) {
    const height = view.chart.panes()[1].getHeight();
    if (!height || view.meterHead.hidden) return;
    const top = Math.min(.65, Math.max(.25, (view.meterHead.getBoundingClientRect().height + 12) / height));
    const bottom = ["volume", "rvol"].includes(indicatorSettings.mode) ? .08 : .28;
    const key = `${top.toFixed(4)}:${bottom}`;
    if (view.marginKey !== key) {
      view.marginKey = key;
      view.hist.priceScale().applyOptions({ scaleMargins: { top, bottom } });
    }
  }

  function renderIndicatorReadout(view) {
    if (!view.model) return;
    const { bars, derived: d } = view.model, c = view.controls;
    const i = view.hoverTime == null ? bars.length - 1 : ChartView.indexAtOrBefore(bars, view.hoverTime);
    c.emaLabels.forEach((label, j) => { label.title = `EMA ${indicators.EMA_PERIODS[j]}：${num(d.emaLines[j][i])}`; });
    c.htfLabels.forEach((label, j) => { label.title = `大周期 EMA ${indicators.HTF_EMA_PERIODS[j]}：${num(d.htfEma[j][i])}`; });
    if (i < 0) { c.waveReadout.textContent = ""; c.meterReadout.textContent = ""; return; }
    const b = bars[i], mode = indicatorSettings.mode;
    c.waveReadout.textContent = `信号 ${num(d.lw.wt2[i])}${d.band.up[i] == null ? " · 通道预热中" : ""}`;
    const fields = [[timeText(b.time), null]];
    const primary = { rvol: ["RVOL", d.rvol[i]], crvol: ["CRVOL", d.crv[i]], volume: ["量", b.volume],
      bsv: ["买/卖", `${num(b.buy)}/${num(b.sell)}`], delta: ["Δ", b.delta], cvd: ["CVD", b.cvd] };
    fields.push(primary[mode]);
    if (mode !== "delta") fields.push(["Δ", b.delta]);
    if (mode !== "rvol") fields.push(["RVOL", d.rvol[i]]);
    fields.push(["斜率", d.lw.crvSlope[i]]);
    if (b.unknown) fields.push(["未知量", b.unknown]);
    c.meterReadout.replaceChildren(...fields.map(([label, value]) =>
      element("span", value === null && label.includes(":") ? label : `${label} ${typeof value === "string" ? value : num(value)}`)));
  }

  function makeChart(id, index) {
    const host = $(id), chart = LightweightCharts.createChart(host, chartOptions(host.clientHeight));
    // 与实时行情页一致：涨灰白、跌深灰，影线同色。
    const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor: "#d1d4dc", downColor: "#4a4f5c", wickUpColor: "#d1d4dc", wickDownColor: "#4a4f5c", borderVisible: false,
    });
    candles.priceScale().applyOptions({ scaleMargins: { top: .08, bottom: .08 } });
    const lineOptions = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
    const htfEma = indicators.HTF_EMA_PERIODS.map((_, j) => chart.addSeries(LightweightCharts.LineSeries, {
      ...lineOptions, color: ChartView.HTF_EMA_COLORS[j], lineWidth: ChartView.HTF_EMA_WIDTH, lineType: LightweightCharts.LineType.WithSteps,
    }));
    const ema = indicators.EMA_PERIODS.map((_, j) => chart.addSeries(LightweightCharts.LineSeries, {
      ...lineOptions, color: ChartView.EMA_COLORS[j], lineWidth: 1,
    }));
    const band = chart.addCustomSeries(new ChartView.BandSeries(), {}, 0);
    const hist = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false }, 1);
    const histSell = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false }, 1);
    const suite = chart.addSeries(LightweightCharts.CandlestickSeries, { priceFormat: { type: "volume" }, borderVisible: false, priceLineVisible: false }, 1);
    const pulse = chart.addSeries(LightweightCharts.HistogramSeries, { priceScaleId: "pulse", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false }, 1);
    pulse.priceScale().applyOptions({ scaleMargins: { top: .82, bottom: 0 } });
    chart.panes()[0].setStretchFactor(.66); chart.panes()[1].setStretchFactor(.34);
    const markers = LightweightCharts.createSeriesMarkers(candles, []);
    const view = { host, chart, candles, band, ema, htfEma, hist, histSell, suite, pulse, markers, lines: {}, count: 0 };
    indicatorControls(view, index);
    return view;
  }

  function renderEma(view, tf, htf) {
    const { bars, derived } = view.model, shown = indicatorShown[view.index], c = view.controls;
    const lines = emaPlotData(bars, derived.emaLines, shown.emaAll, ChartView.EMA_DRAW_BARS);
    view.ema.forEach((series, j) => {
      series.setData(lines[j]);
      series.applyOptions({ visible: shown.ema && (shown.emaLong || !ChartView.EMA_LONG_PERIODS.includes(indicators.EMA_PERIODS[j])) });
    });
    const htfLines = emaPlotData(bars, derived.htfEma, shown.htfEmaAll, ChartView.EMA_DRAW_BARS);
    view.htfEma.forEach((series, j) => { series.setData(htfLines[j]); series.applyOptions({ visible: shown.htfEma && htf > 0 }); });
    for (const [control, key] of [[c.emaToggle, "ema"], [c.emaLong, "emaLong"], [c.emaRange, "emaAll"],
      [c.htfToggle, "htfEma"], [c.htfRange, "htfEmaAll"]]) control.setAttribute("aria-pressed", String(shown[key]));
    c.emaRange.textContent = shown.emaAll ? "全部" : `近${ChartView.EMA_DRAW_BARS}`;
    c.htfRange.textContent = shown.htfEmaAll ? "全部" : `近${ChartView.EMA_DRAW_BARS}`;
    c.emaGroup.classList.toggle("off", !shown.ema); c.htfGroup.classList.toggle("off", !shown.htfEma);
    c.emaLong.disabled = c.emaRange.disabled = !shown.ema;
    c.htfSelect.disabled = c.htfRange.disabled = !shown.htfEma;
    c.htfGroup.hidden = !htf;
    // 周期选择只属于当前图；暂时切到更大周期时保留原先的大周期偏好。
    if (view.htfOptionsFor !== tf) {
      c.htfSelect.replaceChildren(...ChartView.htfOptions(tf).map(value => {
        const option = element("option", ChartView.tfLabel(value)); option.value = value; return option;
      }));
      view.htfOptionsFor = tf;
    }
    c.htfSelect.value = String(htf);
  }

  function renderChart(view, revealed, reset) {
    const tf = chartTimeframes[view.index], shown = indicatorShown[view.index], controls = view.controls;
    const htf = ChartView.htfOptions(tf).includes(shown.htf) ? shown.htf : ChartView.defaultHtf(tf);
    view.model = trainingChartData(revealed, tf, indicatorConfig, indicatorSettings, htf);
    const data = view.model.bars;
    controls.timeframe.value = String(tf);
    view.host.setAttribute("aria-label", `${view.index === 0 ? "左图" : "右图"} ${ChartView.tfLabel(tf)} 已揭示 K 线、指标及交易标记`);
    const range = view.chart.timeScale().getVisibleLogicalRange();
    const follow = !range || range.to >= view.count - 3;
    const difference = data.length - view.count;
    view.candles.applyOptions({ priceFormat: { type: "price", precision: decimals(state.priceTick), minMove: state.priceTick } });
    view.chart.applyOptions({ localization: { timeFormatter: (time) => timeText(time, !state.hideDate) },
      timeScale: { tickMarkFormatter: (time) => timeText(time) } });
    view.candles.setData(data);
    renderEma(view, tf, htf);
    view.band.setData(view.model.band);
    view.band.applyOptions({ visible: shown.band, midVisible: shown.bandMid });
    controls.bandToggle.setAttribute("aria-pressed", String(shown.band));
    controls.midToggle.setAttribute("aria-pressed", String(shown.bandMid));
    controls.bandK.textContent = `${indicatorSettings.bandK}σ`;
    controls.bandK.disabled = controls.midToggle.disabled = !shown.band;
    controls.mode.value = indicatorSettings.mode; controls.threshold.value = indicatorSettings.threshtype;
    view.hist.setData(view.model.suite.hist); view.histSell.setData(view.model.suite.histSell); view.suite.setData(view.model.suite.candles);
    const pulseVisible = !["volume", "rvol"].includes(indicatorSettings.mode);
    view.pulse.applyOptions({ visible: pulseVisible }); view.pulse.setData(pulseVisible ? view.model.pulse : []);
    renderIndicatorReadout(view);
    updateMeterMargins(view);
    if (reset || !range) view.chart.timeScale().setVisibleLogicalRange({ from: Math.max(-1, data.length - 80), to: data.length + 3 });
    else if (follow && difference >= 0) view.chart.timeScale().setVisibleLogicalRange({ from: range.from + difference, to: range.to + difference });
    else view.chart.timeScale().setVisibleLogicalRange(range);
    view.count = data.length;
    Object.values(view.lines).forEach((line) => view.candles.removePriceLine(line));
    view.lines = {};
    if (state.position) {
      for (const [key, label, color] of [["price", "持仓", "#edc05a"], ["sl", "止损", "#ef8992"], ["tp", "止盈", "#51c9b0"]]) {
        const price = state.position[key];
        if (price != null) view.lines[key] = view.candles.createPriceLine({ price, title: label, color, lineWidth: 1, lineStyle: key === "price" ? 0 : 2, axisLabelVisible: true });
      }
    }
    view.markers.setMarkers(trainingTradeMarkers(state.events, tf));
  }

  function installDrag(view) {
    let drag = null;
    view.host.addEventListener("pointerdown", (event) => {
      if (!state || state.status !== "active" || busy || !state.position || event.button !== 0) return;
      const y = event.clientY - view.host.getBoundingClientRect().top;
      const key = ["sl", "tp"].find((name) => state.position[name] != null &&
        Math.abs(view.candles.priceToCoordinate(state.position[name]) - y) <= 8);
      if (!key) return;
      pause(); drag = { key, value: state.position[key], pointer: event.pointerId };
      view.chart.applyOptions({ handleScroll: false });
      view.host.setPointerCapture(event.pointerId);
      event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    view.host.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const price = view.candles.coordinateToPrice(event.clientY - view.host.getBoundingClientRect().top);
      if (price != null && price > 0) {
        drag.value = Number((Math.round(price / state.priceTick) * state.priceTick).toFixed(decimals(state.priceTick)));
        view.lines[drag.key].applyOptions({ price: drag.value });
      }
      event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    async function endDrag(event, cancel = false) {
      if (!drag) return;
      const ended = drag; drag = null;
      if (view.host.hasPointerCapture(ended.pointer)) view.host.releasePointerCapture(ended.pointer);
      view.chart.applyOptions({ handleScroll: true });
      event.preventDefault(); event.stopImmediatePropagation();
      if (cancel) { render(state); return; }
      const values = { sl: state.position.sl, tp: state.position.tp, [ended.key]: ended.value, note: "图上拖动保护价" };
      const result = await command("stops", values);
      if (result) { $("stop-loss").value = state.position.sl ?? ""; $("take-profit").value = state.position.tp ?? ""; }
    }
    view.host.addEventListener("pointerup", (event) => endDrag(event), true);
    view.host.addEventListener("pointercancel", (event) => endDrag(event, true), true);
  }

  function render(next) {
    const newSession = state?.id !== next.id;
    const finishedNow = next.status === "completed" && (newSession || state?.status !== "completed");
    state = next;
    const completed = state.status === "completed";
    if (completed && (!finalView || finalView.id !== state.id)) finalView = state;
    $("setup").hidden = true; $("work").hidden = false;
    workspaceMode(true);
    $("work").classList.toggle("completed", completed);
    if (newSession) { showDetail(null); $("decision-note").open = false; }
    $("session-title").textContent = state.name;
    $("session-mode").textContent = `${state.demo ? "合成演示 · " : ""}${completed ? "训练已结束 · 复盘" : "历史盲盘"}`;
    $("session-clock").textContent = `${state.clock} · ${state.hideDate ? "日期已隐藏" : state.symbol}`;
    for (const id of ["session-title", "session-mode", "session-clock"]) $(id).title = $(id).textContent;
    $("progress").max = state.end - state.start;
    $("progress").value = state.cursor - state.start;
    $("progress-label").textContent = `${state.cursor - state.start} / ${state.end - state.start} 分钟`;
    metrics($("account"), [["账户权益", state.account.equity], ["净收益", state.account.netPnl, true],
      ["浮动盈亏", state.account.floating, true], ["保证金占用", state.account.margin], ["手续费", state.account.fees]]);
    const pos = state.position;
    $("position").textContent = pos ? `${pos.direction > 0 ? "多" : "空"} ${pos.qty} 手 · 均价 ${num(pos.price)}${pos.triggeredExit ? " · 保护平仓等待成交" : ""}` : "当前空仓";
    const positionKey = `${state.id}:${pos ? pos.entryIndex : "flat"}`;
    if (formPosition !== positionKey) {
      $("stop-loss").value = pos ? pos.sl ?? "" : "";
      $("take-profit").value = pos ? pos.tp ?? "" : "";
      formPosition = positionKey;
    }
    for (const id of ["stop-loss", "take-profit"]) { $(id).step = state.priceTick; $(id).min = state.priceTick; }
    $("pending").textContent = state.pending ? `${{ buy: "开多", sell: "开空", close: "平仓" }[state.pending.side]} ${state.pending.qty} 手 · 等待下一根可成交行情` : "";
    $("cancel-order").hidden = !state.pending || completed;
    $("last-price").textContent = `收盘 ${num(state.bars[state.bars.length - 1].close)}`;
    if (!chartPair) { chartPair = [makeChart("main-chart", 0), makeChart("context-chart", 1)]; chartPair.forEach(installDrag); }
    const reset = chartSession !== state.id || completed;
    chartPair.forEach(view => renderChart(view, state.bars, reset));
    chartSession = state.id;
    table($("events"), ["时间", "操作", "价格 / 手数", "说明"], state.events.slice(-15).reverse().map((event) => [
      eventTimeText(event), { order: "委托", fill: "成交", cancel: "撤销", stops: "修改保护价", wait: "等待", reject: "拒绝", finish: "结束" }[event.kind] || event.kind,
      event.kind === "stops" ? `止损 ${num(event.before.sl)} → ${num(event.sl)} / 止盈 ${num(event.before.tp)} → ${num(event.tp)}` :
        event.price != null ? `${num(event.price)} / ${event.qty}` : event.qty ? `${{ buy: "开多", sell: "开空", close: "平仓" }[event.side]} / ${event.qty}` : "—",
      `${event.reason || ""}${event.ambiguous ? " · 同根先后不明，按止损优先" : ""}${event.note ? ` · ${event.note}` : ""}`,
    ]), "尚无操作，先观察行情或提交交易计划。");
    $("finish").hidden = completed;
    $("playback-controls").hidden = completed;
    $("review-navigation").hidden = !completed;
    if (completed) {
      $("review-slider").min = state.start; $("review-slider").max = state.end; $("review-slider").value = state.viewCursor;
      $("review-position").textContent = state.viewCursor > state.finishedCursor ? "后续走势 · 不计入本次成绩" : "回看所选时刻 · 最终成绩保持不变";
      if (finishedNow) showDetail("review");
      else if (detailPanel === "review") renderReview();
    }
    const config = state.config;
    $("config-note").textContent = `初始资金 ${num(config.initialCash)} 元；保证金 ${num(config.marginRate * 100)}%；每次成交滑点 ${config.slippageTicks} 跳；开仓 / 日内平仓费率 ${num(config.openFee * (config.feeMode === "ratio" ? 10000 : 1))} / ${num(config.closeFee * (config.feeMode === "ratio" ? 10000 : 1))}${config.feeMode === "ratio" ? "（万分之）" : " 元/手"}。合约乘数 ${state.multiplier}，最小变动价位 ${state.priceTick}。`;
    updateControls();
  }

  function renderReview() {
    const report = finalView.report;
    metrics($("review-metrics"), [["最终净收益 · 元", report.netPnl, true], ["收盘权益最大回撤 · 元", report.maxDrawdown],
      ["完整交易", `${report.count} 笔`], ["扣费后胜率", report.winRate == null ? "—" : `${num(report.winRate * 100)}%`],
      ["入场设置止损", `${report.protectedEntries} / ${report.entries}`], ["放宽 / 撤掉止损", `${report.widenedStops} 次`]]);
    $("review-note").textContent = `${finalView.position ? "结束时仍有持仓，浮盈亏计入权益，未计入完整交易胜率。" : ""}顺序不确定的交易 ${report.ambiguousTrades} 笔。${report.sampling}；初始风险不含费用与跳空，R 使用净收益计算。`;
    if (!equity) {
      const chart = LightweightCharts.createChart($("equity-chart"), chartOptions(190));
      equity = { chart, series: chart.addSeries(LightweightCharts.LineSeries, { color: "#edc05a", lineWidth: 2, priceLineVisible: false }) };
    }
    equity.chart.applyOptions({ localization: { timeFormatter: (time) => timeText(time, true) }, timeScale: { tickMarkFormatter: (time) => timeText(time) } });
    equity.series.setData(state.equity); equity.chart.timeScale().fitContent();
    table($("trades"), ["回看入场", "方向 / 手数", "开仓 → 平仓", "净收益", "R", "采样浮盈 / 浮亏", "退出原因", "入场依据"], finalView.trades.map((trade) => {
      const button = element("button", timeText(trade.entryTime)); button.type = "button";
      button.addEventListener("click", async () => {
        if (await reviewAt(Math.max(state.start, trade.entryIndex - 1))) showDetail(null);
      });
      return [button, `${trade.direction > 0 ? "多" : "空"} / ${trade.qty}`, `${num(trade.price)} → ${num(trade.exitPrice)}`,
        num(trade.netPnl), num(trade.r), `${num(trade.mfe)} / ${num(trade.mae)}`, `${trade.reason}${trade.ambiguous ? " · 顺序不明" : ""}`, trade.note || "未填写"];
    }), "本次没有完整平仓交易；未平仓结果单独保留。");
  }

  async function reviewAt(index) {
    if (!state || busy || state.status !== "completed") return false;
    busy = true; updateControls();
    try { render(await api(`/sessions/${state.id}?at=${index}`)); return true; }
    catch (error) { notice(error.message, true); return false; }
    finally { busy = false; updateControls(); }
  }

  async function openSession(identity) {
    if (busy) return;
    pause(); const currentEpoch = ++epoch;
    try {
      if (!indicatorConfig) indicatorConfig = await api("/indicator-config");
      const next = await api(`/sessions/${identity}`);
      if (currentEpoch !== epoch) return;
      if ($("history").open) $("history").close();
      finalView = null; render(next);
      history.replaceState(null, "", `training.html?session=${encodeURIComponent(identity)}`);
      try { localStorage.setItem("flow-training-session", identity); } catch (_) { /* 存储禁用不影响后端保存 */ }
      notice(next.status === "active" ? "训练进度已恢复，点击下一根继续。" : "复盘已打开，可回看每次交易及后续走势。");
    } catch (error) { notice(error.message, true); }
  }

  function datasetDetail() {
    const dataset = datasetList.find((item) => item.id === $("dataset").value);
    $("create-submit").disabled = !dataset;
    $("dataset-detail").textContent = dataset ? `${dataset.demo ? "合成演示，非真实行情。" : ""}${dataset.count} 根 · ${dataset.from.slice(0, 16).replace("T", " ")} 至 ${dataset.to.slice(0, 16).replace("T", " ")}。${dataset.hasSessions ? "已记录交易时段，已知休市可跳过。" : "未提供交易时段，在行情间隔处截段。"}` : "请先从天勤载入、导入 CSV，或体验合成演示行情。";
  }

  async function refreshDatasets(selected) {
    const old = selected || $("dataset").value;
    datasetList = await api("/datasets");
    $("dataset").replaceChildren(...datasetList.map((dataset) => {
      const option = element("option", `${dataset.demo ? "[演示] " : ""}${dataset.name} · ${dataset.count} 根`);
      option.value = dataset.id; return option;
    }));
    if (!datasetList.length) { const option = element("option", "暂无数据，请先载入"); option.value = ""; $("dataset").append(option); }
    if (datasetList.some((dataset) => dataset.id === old)) $("dataset").value = old;
    datasetDetail();
  }

  async function refreshHistory() {
    const sessions = await api("/sessions");
    table($("history-list"), ["训练", "创建时间", "进度", "净收益", "操作"], sessions.map((session) => {
      const button = element("button", session.status === "active" ? "继续训练" : "查看复盘"); button.type = "button";
      button.addEventListener("click", () => openSession(session.id));
      return [`${session.demo ? "[演示] " : ""}${session.name}`, new Date(session.createdAt).toLocaleString("zh-CN"), `${session.progress} / ${session.total}`, num(session.netPnl), button];
    }), "还没有训练记录。完成第一局后，它会保存在这里。");
  }

  $("dataset").addEventListener("change", datasetDetail);
  $("new-training").addEventListener("click", () => {
    if (busy) return; pause(); ++epoch; $("setup").hidden = false; $("work").hidden = true;
    showDetail(null); workspaceMode(false);
    history.replaceState(null, "", "training.html");
    notice("上一局已经保存，可以随时从训练记录继续。");
    refreshDatasets().catch((error) => notice(error.message, true));
  });
  $("history-toggle").addEventListener("click", () => {
    pause();
    if ($("history").open) $("history").close();
    else {
      $("history").showModal();
      $("history-toggle").setAttribute("aria-expanded", "true");
      refreshHistory().catch((error) => notice(error.message, true));
    }
  });
  $("history-close").addEventListener("click", () => $("history").close());
  $("history").addEventListener("close", () => $("history-toggle").setAttribute("aria-expanded", "false"));
  $("detail-close").addEventListener("click", () => showDetail(null));
  document.querySelectorAll("[data-detail]").forEach(button => button.addEventListener("click", () =>
    showDetail(detailPanel === button.dataset.detail ? null : button.dataset.detail)));
  document.querySelectorAll(".chart-switch button").forEach(button => button.addEventListener("click", () => {
    chartLayout = button.dataset.layout;
    try { localStorage.setItem("flowscope.trainingLayout", chartLayout); } catch (_) { /* 忽略 */ }
    applyChartLayout();
  }));
  narrowLayout.addEventListener("change", applyChartLayout);
  applyChartLayout();
  $("refresh-history").addEventListener("click", () => refreshHistory().catch((error) => notice(error.message, true)));
  $("sample-mode").addEventListener("change", () => { const date = $("sample-mode").value === "date"; $("sample-date-field").hidden = !date; $("sample-date").required = date; });
  $("download-mode").addEventListener("change", () => {
    const range = $("download-mode").value === "range"; $("range-fields").hidden = !range; $("recent-field").hidden = range;
    $("download-from").required = $("download-to").required = range;
  });
  $("load-demo").addEventListener("click", async () => {
    $("load-demo").disabled = true;
    try { const dataset = await api("/datasets/demo", {}); await refreshDatasets(dataset.id); notice("合成演示行情已载入，可直接开始训练。真实行情可通过天勤或 CSV 载入。"); }
    catch (error) { notice(error.message, true); }
    finally { $("load-demo").disabled = false; }
  });
  $("import-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("import-submit").disabled = true;
    try {
      const file = $("csv-file").files[0];
      if (!file || file.size > 20 * 1024 * 1024) throw new Error("请选择不超过 20 MB 的 CSV");
      const dataset = await api("/datasets/import", { csv: await file.text(), symbol: $("import-symbol").value.trim(), name: $("import-name").value,
        priceTick: Number($("import-tick").value), multiplier: Number($("import-multiplier").value) });
      await refreshDatasets(dataset.id); notice("历史数据校验通过，已保存为独立数据集。");
    } catch (error) { notice(error.message, true); }
    finally { $("import-submit").disabled = false; }
  });
  $("download-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("download-submit").disabled = true;
    try {
      let job = await api("/downloads", { symbol: $("download-symbol").value.trim(), mode: $("download-mode").value, count: Number($("download-count").value),
        startDate: $("download-from").value, endDate: $("download-to").value });
      while (job.status === "running") {
        $("download-status").textContent = `${job.message} ${job.progress}%`;
        await new Promise((resolve) => setTimeout(resolve, 1200));
        job = await api(`/downloads/${job.id}`);
      }
      $("download-status").textContent = job.message;
      if (job.status === "failed") throw new Error(job.message);
      await refreshDatasets(job.dataset.id); notice("天勤历史行情已载入，可开始训练。");
    } catch (error) { $("download-status").textContent = "载入未完成"; notice(error.message, true); }
    finally { $("download-submit").disabled = false; }
  });
  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy) return; pause(); busy = true; $("create-submit").disabled = true;
    try {
      const ratio = $("fee-mode").value === "ratio";
      const result = await api("/sessions", { requestId: requestId(), datasetId: $("dataset").value,
        warmup: Number($("warmup").value), steps: Number($("steps").value), hideDate: $("hide-date").checked,
        startDate: $("sample-mode").value === "date" ? $("sample-date").value : "", initialCash: Number($("initial-cash").value),
        marginRate: Number($("margin-rate").value) / 100, slippageTicks: Number($("slippage").value), feeMode: $("fee-mode").value,
        openFee: Number($("open-fee").value) / (ratio ? 10000 : 1), closeFee: Number($("close-fee").value) / (ratio ? 10000 : 1),
        riskBudget: Number($("risk-budget").value) }, true);
      busy = false; await openSession(result.id);
    } catch (error) { notice(error.message, true); }
    finally { busy = false; datasetDetail(); updateControls(); }
  });
  $("order-form").addEventListener("submit", (event) => event.preventDefault());
  $("order-form").addEventListener("focusin", pause);
  $("trade-actions").addEventListener("focusin", pause);
  for (const side of ["buy", "sell"]) $(side).addEventListener("click", () => {
    if (!$("order-form").reportValidity()) return;
    command("order", { side, qty: Number($("qty").value), sl: nullable("stop-loss"), tp: nullable("take-profit"), note: $("trade-note").value });
  });
  $("close-position").addEventListener("click", () => command("order", { side: "close" }));
  $("update-stops").addEventListener("click", () => {
    if ($("order-form").reportValidity()) command("stops", { sl: nullable("stop-loss"), tp: nullable("take-profit"), note: $("trade-note").value });
  });
  $("cancel-order").addEventListener("click", () => command("cancel"));
  $("finish").addEventListener("click", () => command("finish"));
  $("step").addEventListener("click", () => { pause(); command("step"); });
  $("play").addEventListener("click", () => { if (playing) pause(); else { playing = true; updatePlayButton(); playNext(); } });
  $("review-slider").addEventListener("change", () => reviewAt(Number($("review-slider").value)));
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
  document.addEventListener("keydown", (event) => {
    if ($("history").open) return;
    if (event.key === "Escape") {
      if ($("decision-note").open) { $("decision-note").open = false; $("decision-note").querySelector("summary").focus(); }
      else if (detailPanel) showDetail(null);
      return;
    }
    if (event.target.closest("input, textarea, select, button, summary, a, [contenteditable='true']") || $("work").hidden || busy) return;
    if (event.key === "ArrowRight") { event.preventDefault(); pause(); command("step"); }
    if (event.code === "Space" && state && state.status === "active") { event.preventDefault(); $("play").click(); }
  });

  (async () => {
    try {
      if (typeof LightweightCharts === "undefined") throw new Error("图表库未加载，请刷新页面或检查本地图表文件");
      await Promise.all([refreshDatasets(), refreshHistory(), api("/indicator-config").then((cfg) => { indicatorConfig = cfg; })]);
      let last = new URLSearchParams(location.search).get("session");
      if (!last) { try { last = localStorage.getItem("flow-training-session"); } catch (_) { /* 忽略 */ } }
      if (last) await openSession(last); else notice("先选择行情数据，再开始一局训练。");
    } catch (error) { notice(error.message, true); }
  })();
})(typeof globalThis !== "undefined" ? globalThis : this);
