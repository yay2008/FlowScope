/* FlowScope 前端: 页面级逻辑
 * 工具栏、合约选择器、自选面板、模拟交易面板在这里; 图表本身(K线/指标/足迹图与数据推送)是 chart-view.js 里
 * 可实例化的组件, 每张图一个主周期, 左上角的下拉框切换。页面放两张图左右并排(默认 30s、5m):
 * 共用状态 settings(工具栏的买卖口径, FlowMeter 窗格的模式/阈值)两张图共用, 十字光标与可视时间范围联动,
 * 视图、主图指标的显示开关和读数每张图各管各的。
 */
"use strict";

const $ = (id) => document.getElementById(id);
// 指标常量来自 indicators.js
const { BAND } = FlowIndicators;

// 共用状态: 所有图按引用共用, 只在这里改, 改完调各图对应的刷新入口。
// 工具栏下拉框的默认值必须与 index.html 里对应 <select> 的 selected 选项一致;
// 模式/阈值的下拉框由各图按这里的值建。
const settings = {
  mode: "cvd",
  threshtype: "Z-SCORE",
  cvdSource: "tick",   // tick 口径 / kline = TV K线口径(按 klineLtf 秒的小周期拆分), 工具栏「买卖口径」一个下拉框选
  klineLtf: 10,        // 默认同 Pine 的 10S
  bandK: BAND.kDefault,   // 叠加带的带宽倍数 k(回归残差标准差的倍数), 不在工具栏: 点主图图例里的「2σ」切换
};
let symbol = new URLSearchParams(location.search).get("symbol") || "KQ.m@SHFE.fu";
// 自选代码(服务端顺序即各分类内的顺序)。常量必须早于合约选择器: picker.load() 会同步渲染
// 月份行并回调 isFavorite 画 ☆, 声明放后面会踩 let 的暂时性死区。
let favorites = [];

// 合约名显示在合约选择器的按钮上: 展示名由 /api/symbol 解析(主连会解析成当前标的月份合约的中文名),
// 在页面末尾统一发起; 拿到之前按钮先按目录显示。原始代码放在按钮的悬停提示最前面。
let symbolLabel = "";
$("picker-trigger").title = `${symbol}\n${$("picker-trigger").title}`;

// 加密合约(币安/OKX/多所汇总)的 tick 口径是交易所逐笔成交自带的主动方向, 不是 TqSdk 快照估算, 下拉框照实写。
const CRYPTO_SYMBOL = /^(BINANCE|OKX|AGG)\./;
if (CRYPTO_SYMBOL.test(symbol)) {
  const tickOption = $("cvd-source").querySelector('option[value="tick"]');
  if (tickOption) tickOption.textContent = "交易所逐笔";
  $("cvd-source").title = "决定成交量怎么分成主动买和主动卖，影响 CVD、Delta 和买卖量。交易所逐笔：成交自带的主动买卖方，不用估算；" +
    "TV K线：按小周期 K 线涨跌归类成交量，后面的秒数是拆分用的小周期，当前周期用不了的置灰。";
}

// ---------- 图表 ----------
// 左右两张图, 周期各自在左上角切换。后端按 (symbol, tf) 各出一份快照, 每张图各自加载、各自连推送。

// 两张图的周期记在 localStorage(切合约是整页重载, 不记的话每次都被打回默认), 读不到或不合法就用默认的
// 左 30s、右 5m。chartTfs 跟着切换更新, 早于 charts 声明: 建图时就会报连接状态(见 setChartStatus)。
const CHART_TFS_KEY = "flowscope.chartTfs";
const DEFAULT_CHART_TFS = [30, 300];

function loadChartTfs() {
  try {
    const saved = JSON.parse(localStorage.getItem(CHART_TFS_KEY) || "null");
    if (Array.isArray(saved) && saved.length === DEFAULT_CHART_TFS.length &&
        saved.every((tf) => ChartView.TF_CHOICES.includes(tf))) {
      return new URLSearchParams(location.search).get("alert_tf") === "300" ? [saved[0], 300] : saved;
    }
  } catch (error) { /* 记不住不影响使用 */ }
  return [...DEFAULT_CHART_TFS];
}

function saveChartTfs() {
  try {
    localStorage.setItem(CHART_TFS_KEY, JSON.stringify(chartTfs));
  } catch (error) { /* 记不住不影响使用 */ }
}

const chartTfs = loadChartTfs();

// 视图按左右图分别保存, 刷新或切合约后保留; 没有记录的区域默认 K 线。
const CHART_VIEWS_KEY = "flowscope.chartViews";
const chartViews = loadChartViews();

function loadChartViews() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(CHART_VIEWS_KEY) || "null");
  } catch (error) { /* 记不住不影响使用 */ }
  return DEFAULT_CHART_TFS.map((_, index) =>
    Array.isArray(saved) && ChartView.VIEW_CHOICES.includes(saved[index]) ? saved[index] : "candle");
}

function saveChartViews() {
  try {
    localStorage.setItem(CHART_VIEWS_KEY, JSON.stringify(chartViews));
  } catch (error) { /* 记不住不影响使用 */ }
}

// 足迹显示模式(买卖 / 差值)同样按左右图分别保存; 没有记录或不认识的回到买卖。
const FOOTPRINT_MODES_KEY = "flowscope.footprintModes";
const footprintModes = loadFootprintModes();

function loadFootprintModes() {
  let saved;
  try {
    saved = JSON.parse(localStorage.getItem(FOOTPRINT_MODES_KEY) || "null");
  } catch (error) { /* 记不住不影响使用 */ }
  return DEFAULT_CHART_TFS.map((_, index) =>
    Array.isArray(saved) && ChartView.FOOTPRINT_MODES.includes(saved[index]) ? saved[index] : "bidask");
}

function saveFootprintModes() {
  try {
    localStorage.setItem(FOOTPRINT_MODES_KEY, JSON.stringify(footprintModes));
  } catch (error) { /* 记不住不影响使用 */ }
}

// 各图主图指标的显示开关记在 localStorage, 按左右图各记一份
// [{ema, emaAll, emaLong, htfEma, htfEmaAll, htf, band, bandMid, ob, large, largeMinRadius}, {...}]:
// 切合约是整页重载, 不记的话每切一次都被打回默认(emaAll 是 EMA 画全部历史, emaLong 是画不画 EMA100/200,
// bandMid 是 FlowWave带 的中线, largeMinRadius 是大额圆圈最小显示半径(0 = 全部),
// htfEma/htfEmaAll/htf 是大周期 EMA 的开关、画全部历史和选的大周期(秒, 0 = 按本图周期取默认), 都是后来才加的,
// 旧值里没有就按默认: EMA 与大周期 EMA 只画最近 100 根、EMA100/200 显示、大周期 EMA 显示、不画中线)。
// 开关跟着图走, 不跟周期走(两张图可以选同一个周期, 切周期也不换开关)。
// 旧版按周期记 flowscope.mainShown = {"10": {...}, "30": {...}}(更早只记一份 {ema, band, wt}),
// 新键还没有时按旧值初始化; 读不到(隐私模式、旧值不合法)就用默认值。旧值里 WaveTrend 的 wt 已经没用, 读的时候忽略。
const CHART_SHOWN_KEY = "flowscope.chartShown";
const MAIN_SHOWN_KEY = "flowscope.mainShown";

function loadShown(index, tf) {
  const shown = { ema: true, emaAll: false, emaLong: true, htfEma: true, htfEmaAll: false, htf: 0, band: false,
                  bandMid: false, ob: false, large: true, largeMinRadius: 8 };
  try {
    const perChart = JSON.parse(localStorage.getItem(CHART_SHOWN_KEY) || "null");
    let mine = Array.isArray(perChart) ? perChart[index] : null;
    if (!mine || typeof mine !== "object") {
      const legacy = JSON.parse(localStorage.getItem(MAIN_SHOWN_KEY) || "{}");
      mine = legacy && typeof legacy[tf] === "object" ? legacy[tf] : legacy;
    }
    for (const key of Object.keys(shown)) {
      if (typeof shown[key] === "boolean" && typeof mine?.[key] === "boolean") shown[key] = mine[key];
    }
    if (ChartView.TF_CHOICES.includes(mine?.htf)) shown.htf = mine.htf;   // 对本图周期合不合法由图表组件判
    if (FlowLargeTrades.MIN_RADIUS_OPTIONS.includes(mine?.largeMinRadius)) shown.largeMinRadius = mine.largeMinRadius;
  } catch (error) { /* 记不住不影响使用 */ }
  return shown;
}

function saveShown() {
  try {
    localStorage.setItem(CHART_SHOWN_KEY, JSON.stringify(charts.map((c) => c.shown())));
  } catch (error) { /* 记不住不影响使用 */ }
}

// OB Zones 的参数按左右图保存, 与显示开关一样跨合约、跨周期保留。
const CHART_OB_KEY = "flowscope.chartObOptions";

function loadObOptions(index) {
  try {
    const all = JSON.parse(localStorage.getItem(CHART_OB_KEY) || "null");
    return FlowIndicators.normalizeObOptions(Array.isArray(all) ? all[index] : null);
  } catch (error) {
    return FlowIndicators.normalizeObOptions();
  }
}

function saveObOptions() {
  try {
    localStorage.setItem(CHART_OB_KEY, JSON.stringify(charts.map((c) => c.obOptions())));
  } catch (error) { /* 记不住不影响使用 */ }
}

// 各图画的成交量分布记在 localStorage, 按合约、左右图各记一份: {合约: [[{from, to, rows, extend, visible}, ...], [...]]}。
// 区间按时间记, 切周期、重载之后还是同一段时间; 条目合不合法由图表组件自己判(不合法的丢掉)。
// 某个合约两张图都删光了就不再记它。
const CHART_PROFILES_KEY = "flowscope.chartProfiles";

function loadProfiles(index) {
  try {
    const all = JSON.parse(localStorage.getItem(CHART_PROFILES_KEY) || "null");
    const mine = all && typeof all === "object" && Array.isArray(all[symbol]) ? all[symbol][index] : null;
    return Array.isArray(mine) ? mine : [];
  } catch (error) {
    return [];   // 记不住不影响使用
  }
}

function saveProfiles() {
  try {
    let all = JSON.parse(localStorage.getItem(CHART_PROFILES_KEY) || "null");
    if (!all || typeof all !== "object" || Array.isArray(all)) all = {};
    const lists = charts.map((c) => c.profiles());
    if (lists.some((list) => list.length)) all[symbol] = lists;
    else delete all[symbol];
    localStorage.setItem(CHART_PROFILES_KEY, JSON.stringify(all));
  } catch (error) { /* 记不住不影响使用 */ }
}

// 鼠标所在(最近进入)的那张图: 十字光标与缩放/拖动以它为准同步另一张, AI 分析默认发它。默认右边那张。
// 只由鼠标进入图表区域来切换, 十字光标事件不算(见 syncCrosshair)。
let activeChart = null;
const chartStatus = new Map();   // 第几张图 -> {ok, text}
const notices = new Map();       // 提示来源("catalog" 合约目录 / "favorite" 自选) -> {text, detail, level}, 见 showStatus

function makeChart(tf, index) {
  const chartView = ChartView.create({
    host: $("chart"),
    tf,
    symbol,
    settings,
    view: chartViews[index],
    footprintMode: footprintModes[index],
    shown: loadShown(index, tf),
    obOptions: loadObOptions(index),
    profiles: loadProfiles(index),
    onStatus: (ok, text) => setChartStatus(index, ok, text),
    onSuiteSetting: setSuiteSetting,
    onConfig: refreshLtfOptions,
    onShownChange: saveShown,
    onObOptionsChange: saveObOptions,
    onProfilesChange: saveProfiles,
    onBandK: setBandK,
    onCrosshair: (time, price) => syncCrosshair(chartView, time, price),
    onRangeChange: syncRanges,
    // 图上划线改止盈止损、拖挂单改价、双击撤单、十字线加号挂单: 交给交易面板提交(paperPanel 在下面才建, 到用户操作时早就有了)
    onPaperStops: (change) => paperPanel.setStops(change),
    onPaperAmend: (id, price) => paperPanel.amendOrder(id, price),
    onPaperCancel: (id) => paperPanel.cancelOrder(id),
    paperMenu: (price) => paperPanel.menuItems(price),
    onViewChange: (view) => {
      chartViews[index] = view;
      saveChartViews();
    },
    onFootprintModeChange: (mode) => {
      footprintModes[index] = mode;
      saveFootprintModes();
    },
    onTfChange: () => {
      chartTfs[index] = chartView.tf;
      saveChartTfs();
      refreshLtfOptions();
      showStatus();
    },
  });
  // 恢复足迹图时, 组件会把不支持的 10s 升到 30s; 状态栏与保存的周期一起更新。
  if (chartTfs[index] !== chartView.tf) {
    chartTfs[index] = chartView.tf;
    saveChartTfs();
  }
  chartView.el.addEventListener("pointerenter", () => activate(chartView));
  return chartView;
}

const charts = chartTfs.map(makeChart);
activeChart = charts[charts.length - 1];

function activate(chartView) {
  activeChart = chartView;
}

// ---------- 顶栏状态点 ----------
// 两张图的连接状态和合约目录/自选的提示合成一个: 圆点绿 = 都正常, 黄 = 能用但有提示(如离线目录),
// 红 = 有图没连上或出错了。文字只写最要紧的: 没连上的图(带周期前缀) > 出错的提示 > 「已连接」,
// 细节(每张图的状态、提示原文)都在悬停提示里。
function setChartStatus(index, ok, text) {
  chartStatus.set(index, { ok, text });
  showStatus();
}

// 各来源的提示各记各的, 互不覆盖。level: "info" 只把圆点变黄, "error" 文字也换成提示; text 为空即清除
function setNotice(source, text, detail, level) {
  notices.set(source, { text: text || "", detail: detail || "", level: text ? level || "info" : "" });
  showStatus();
}

function showStatus() {
  const line = (index) => `${ChartView.tfLabel(chartTfs[index])}: ${chartStatus.get(index)?.text ?? "连接中…"}`;
  const pending = chartTfs.map((tf, index) => index).filter((index) => !chartStatus.get(index)?.ok);
  const shown = [...notices.values()];
  const error = shown.find((item) => item.level === "error");
  const el = $("status");
  el.className = pending.length || error ? "off" : shown.some((item) => item.level) ? "warn" : "on";
  el.textContent = pending.length ? pending.map(line).join("  ") : error ? error.text : "已连接";
  const details = chartTfs.map((tf, index) => line(index));
  for (const item of shown) {
    if (item.text || item.detail) details.push([item.text, item.detail].filter(Boolean).join("："));
  }
  el.title = details.join("\n");
}

// 十字光标联动: 鼠标所在那张图的光标一动, 另一张就把光标摆到同一时刻(落到它自己周期的那根 bar 上),
// 光标在主图窗格时横线也摆在同一价位。只认鼠标所在那张图的事件: 另一张图上摆好的光标在它数据更新时
// 库也会再报一次, 不能拿来反向联动, 更不能据此认定鼠标换了图。
function syncCrosshair(source, time, price) {
  if (source !== activeChart) return;
  for (const c of charts) {
    if (c === source) continue;
    if (time == null) c.hideCrosshair();
    else c.showCrosshair(time, price);
  }
}

// 可视时间范围联动: 以鼠标所在那张图为准, 另一张显示同一段时间(bar 粗细随周期不同)。
// 另一张自己动了(新 bar 自动右移、加载完滚到最新)也拉回来对齐; 已经对齐时 setVisibleTimeRange 什么都不做,
// 所以不会来回触发。另一张缩不到那么小(bar 间距有下限)时只能显示它放得下的那部分, 不反过来改鼠标所在那张。
// 两张图周期相差超过 FULL_SYNC_RATIO 倍(如 1m 与 4h)时, 显示同一段时间会把大周期压成一两根,
// 所以只对齐右边缘时刻, 各图保留自己的缩放。
// K 线与足迹混合显示时也只对齐右边缘, 让足迹保留足够宽的格子。
const FULL_SYNC_RATIO = 3;

function syncRanges() {
  if (!activeChart) return;
  const range = activeChart.visibleTimeRange();
  if (!range) return;
  for (const c of charts) {
    if (c === activeChart) continue;
    const ratio = Math.max(c.tf, activeChart.tf) / Math.min(c.tf, activeChart.tf);
    if (c.view === activeChart.view && ratio <= FULL_SYNC_RATIO) c.setVisibleTimeRange(range);
    else c.alignRightEdge(range.to);
  }
}

// 画图工具(每张图左上角的尺子与成交量分布): Esc 取消所有图上的测量和画了一半的成交量分布。
// 合约选择器的搜索框自己处理 Esc(收起菜单, 已 preventDefault), 那一下不算。
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !event.defaultPrevented) charts.forEach((c) => c.cancelTools());
});

// FlowWave带 的带宽 k 两张图共用: 哪张图的图例里点了「2σ」, 两张一起换。
// 只重算带本身(中线、wt2 状态不变), 顶部读数里没有带, 不用刷新。
function setBandK(k) {
  settings.bandK = BAND.kOptions.includes(k) ? k : BAND.kDefault;   // 不按目录之外的倍数画带
  charts.forEach((c) => c.rebuildBand());
}

// ---------- 模式/阈值(各图 FlowMeter 窗格左上角, 两张图共用) ----------

// 只影响 FlowMeter 窗格和读数; 哪张图上选的都一样, 各图的下拉框在 renderSuite 里跟着改
function setSuiteSetting(key, value) {
  settings[key] = value;
  charts.forEach((c) => {
    c.renderSuite();
    c.refreshReadout();
  });
}

// ---------- 买卖口径(CVD 口径与拆分粒度合在一个下拉框) ----------
// 选项值 "tick" = tick 口径, "kline-N" = TV K线口径、按 N 秒的小周期拆分; 写进 settings 时仍分成 cvdSource 与 klineLtf。
// 拆分粒度随主周期收敛: 必须整除主周期且不比它粗(15/30 在 10s 下不合法)。工具栏上的选择所有图共用,
// 每张图请求时各自把用不了的选择回落到本周期下最粗的合法粒度(chart-view.js 的 currentLtf),
// 所以这里只禁用哪张图都用不了的选项。
const splitLtf = (value) => (String(value).startsWith("kline-") ? Number(String(value).slice(6)) : null);

function refreshLtfOptions() {
  const usable = new Set(charts.flatMap((c) => c.ltfOptions()));
  for (const option of $("cvd-source").options) {
    const ltf = splitLtf(option.value);
    option.disabled = ltf != null && !usable.has(ltf);
  }
}

$("cvd-source").addEventListener("change", (e) => {
  const ltf = splitLtf(e.target.value);
  settings.cvdSource = ltf == null ? "tick" : "kline";
  if (ltf != null) settings.klineLtf = ltf;
  charts.forEach((c) => c.load());
});

// ---------- 合约选择器: 自研二级级联菜单 ----------
// 组件在 contract-picker.js(分类页签 + 一级品种 + 二级月份, 含搜索/键盘/悬停预取), 纯逻辑在 picker-core.js。
// 选择结果统一写回 URL 的 symbol 参数, 由页面重载完成切换与重连。

// 合约展示名: 写到选择器按钮上, 取不到就留着按钮按目录显示的名字, 不阻塞图表加载。
const LABEL_POLL_MS = 60000;
let labelTimer = null;
let labelPending = false;

async function labelOf(target) {
  clearTimeout(labelTimer);
  labelTimer = null;
  if (labelPending || document.hidden) return;
  labelPending = true;
  try {
    const data = await fetchJson(`/api/symbol?symbol=${encodeURIComponent(target)}`);
    if (symbol === target && data && data.label) {
      symbolLabel = data.label;
      picker.setLabel(data.label);
    }
  } catch (error) { /* 按钮上已经有目录给的名字或原始代码 */ }
  finally {
    labelPending = false;
    // 名称更新不依赖自选是否展开; 长时间开着图也能跟随主力换月。
    if (symbol === target && target.startsWith("KQ.m@") && !document.hidden) {
      labelTimer = setTimeout(() => labelOf(target), LABEL_POLL_MS);
    }
  }
}

function switchSymbol(target) {
  if (!target || target === symbol) return;
  // 重载前先给出反馈: 新页面的按钮会自己再解析一次。
  picker.setLabel("切换中…");
  location.search = "?symbol=" + encodeURIComponent(target);
}

const picker = ContractPicker.create({
  root: {
    wrapper: $("picker"),
    trigger: $("picker-trigger"),
    popup: $("picker-popup"),
    search: $("picker-search"),
    tabs: $("picker-tabs"),
    products: $("picker-products"),
    months: $("picker-months"),
  },
  fetchJson,                       // 与自选面板共用同一个把 4xx 详情带出来的取数函数
  storage: window.localStorage,
  onPick: switchSymbol,
  onStatus: (text, detail, level) => setNotice("catalog", text, detail, level),
  // 行内 ☆: 菜单里直接加/移出自选, 状态由这里持有的 favorites 决定
  isFavorite: (target) => favorites.includes(target),
  onToggleFavorite: (target) => changeFavorite(target, !favorites.includes(target)),
});
picker.setSymbol(symbol);
picker.load();

// ---------- 自选(服务端保存, 面板按需轮询轻量报价) ----------
// 自选代码存在服务端 data/favorites.json, 所有浏览器共用一份;
// 名称/最新价/涨跌幅来自 /api/watch(只读报价对象, 不订阅 K 线与 tick)。

const WATCH_POLL_MS = 3000;
const WATCH_COLLAPSED_KEY = "flowscope.watchCollapsed";
let watchRows = new Map();          // symbol -> 报价行
let watchTimer = null;
let watchPending = false;

function readCollapsed() {
  try {
    return localStorage.getItem(WATCH_COLLAPSED_KEY) === "1";
  } catch (error) {
    return false;   // 隐私模式下 localStorage 可能不可用, 面板默认展开即可
  }
}

function setWatchCollapsed(collapsed) {
  try {
    localStorage.setItem(WATCH_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch (error) { /* 折叠状态记不住不影响使用 */ }
  $("watchlist").classList.toggle("collapsed", collapsed);
  $("watch-toggle").textContent = collapsed ? "›" : "‹";
  $("watch-toggle").setAttribute("aria-expanded", String(!collapsed));
  $("watch-toggle").setAttribute("aria-label", collapsed ? "展开自选面板" : "收起自选面板");
  if (collapsed) {
    clearTimeout(watchTimer);
    watchTimer = null;
  }
}
const watchCollapsed = () => $("watchlist").classList.contains("collapsed");

function setWatchHint(text) {
  $("watch-hint").textContent = text || "";
}

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = body.detail;
    } catch (ignored) { /* 非 JSON 错误响应, 保留 HTTP 码 */ }
    throw new Error(detail);
  }
  return resp.json();
}

// 主连显示成「品种 · 主力月份」, 与选择器口径一致; 具体月份直接用合约中文名。
const watchName = FlowData.watchLabel;

function renderWatch() {
  const list = $("watch-items");
  list.replaceChildren();
  // 按代码分类, 报价尚未加载或离线时分组也保持稳定。
  const category = (code) => CRYPTO_SYMBOL.test(code) ? "crypto"
    : /^(?:KQ\.[mi]@)?CFFEX\./.test(code) ? "financial" : "futures";
  for (const [key, title] of [["futures", "商品期货"], ["financial", "金融期货"], ["crypto", "加密"]]) {
    const codes = favorites.filter((code) => category(code) === key);
    if (!codes.length) continue;
    const group = document.createElement("li");
    group.className = "watch-group";
    const heading = document.createElement("h3");
    heading.className = "watch-group-title";
    const label = document.createElement("span");
    label.textContent = title;
    const count = document.createElement("span");
    count.className = "watch-group-count";
    count.textContent = String(codes.length);
    heading.append(label, count);
    const items = document.createElement("ul");
    items.className = "watch-group-items";
    items.setAttribute("aria-label", title);
    codes.forEach((code) => items.appendChild(renderWatchItem(code)));
    group.append(heading, items);
    list.appendChild(group);
  }
  if (!favorites.length) setWatchHint("在「选择合约」菜单里点 ☆ 加入自选");
}

function renderWatchItem(code) {
  const row = watchRows.get(code) || { symbol: code, name: code };
  const item = document.createElement("li");
  item.className = "watch-item";
  item.classList.toggle("active", code === symbol);
  // 只有「取过报价但没有数据」或已下市才变暗: 还没轮到第一次轮询的行不该看起来是坏的。
  item.classList.toggle("dead", row.expired === true || (watchRows.has(code) && row.lastPrice == null));

  const top = document.createElement("div");
  top.className = "watch-top";
  const name = document.createElement("span");
  name.className = "watch-name";
  name.textContent = watchName(row);
  name.title = `${code}${row.mainSymbol ? " → " + row.mainSymbol : ""}`;
  const remove = document.createElement("span");
  remove.className = "watch-remove";
  remove.textContent = "×";
  remove.title = "移出自选";
  remove.addEventListener("click", (event) => {
    event.stopPropagation();
    changeFavorite(code, false);
  });
  top.append(name);
  if (window.alertPanel) top.appendChild(window.alertPanel.buttonFor(code));
  top.appendChild(remove);

  const bottom = document.createElement("div");
  bottom.className = "watch-bottom";
  const label = document.createElement("span");
  label.className = "watch-code";
  // 加密合约的代码太长, 这一栏写交易所(币安 / OKX / 多所汇总)
  label.textContent = row.venue || code.split("@").pop();
  label.title = code;
  const price = document.createElement("span");
  price.textContent = FlowData.formatPrice(row.lastPrice, row.priceDecs);
  const change = document.createElement("span");
  const pct = row.changePct;
  change.className = "watch-chg " + FlowData.changeClass(pct);
  change.textContent = FlowData.formatChangePct(pct);
  if (row.venue) change.title = "24 小时涨跌";
  bottom.append(label, price, change);
  item.append(top, bottom);

  // 第三行: 当日成交量与成交额, 用来判断流动性。行情未就绪时不占位置。
  const liquidity = FlowData.liquidityLabel(row);
  if (liquidity) {
    const flow = document.createElement("div");
    flow.className = "watch-liquidity";
    flow.textContent = liquidity;
    item.append(flow);
  }

  item.addEventListener("click", () => {
    setWatchCollapsed(true);  // 先保存折叠状态, 切换合约重载后仍然隐藏。
    switchSymbol(code);
  });
  return item;
}

function scheduleWatch(delay = WATCH_POLL_MS) {
  clearTimeout(watchTimer);
  watchTimer = null;
  if (watchCollapsed() || !favorites.length || document.hidden) return;
  watchTimer = setTimeout(pollWatch, delay);
}

async function pollWatch() {
  watchTimer = null;
  if (watchPending || watchCollapsed() || !favorites.length || document.hidden) return;
  watchPending = true;
  try {
    const data = await fetchJson(`/api/watch?symbols=${encodeURIComponent(favorites.join(","))}`);
    if (data.source === "live") {
      watchRows = new Map((data.quotes || []).map((row) => [row.symbol, row]));
      // 上一次的失败提示到这里就该消失; 加密合约有报价而期货行情未就绪时, 只提示期货那部分
      setWatchHint(data.error ? `期货行情未就绪（${data.error}）` : "");
    } else {
      setWatchHint("行情未就绪" + (data.error ? `（${data.error}）` : ""));
    }
  } catch (error) {
    setWatchHint("报价加载失败：" + error.message);
  } finally {
    watchPending = false;
    renderWatch();
    scheduleWatch();
  }
}

// 连着点几个 ☆ 时请求并发, 响应可能乱序到达(收藏前服务端要先查一次合约服务, 移出则是即时的,
// 所以服务端的处理顺序也不一定等于点击顺序): 只采用最后发出的那次的响应, 晚到的旧响应不能把
// 列表写回旧状态。丢弃过旧响应时, 等这一批请求全部结束再以服务端为准重读一次。
let favoriteSeq = 0;
let favoritesInFlight = 0;
let favoritesStale = false;

async function changeFavorite(code, wanted) {
  const seq = ++favoriteSeq;
  favoritesInFlight += 1;
  try {
    const data = await fetchJson(`/api/favorites?symbol=${encodeURIComponent(code)}`,
                                 { method: wanted ? "POST" : "DELETE" });
    if (seq !== favoriteSeq) {
      if (!wanted) watchRows.delete(code);
      favoritesStale = true;
      return;
    }
    favorites = data.symbols || [];
    if (!wanted) watchRows.delete(code);
    setWatchHint("");
    setNotice("favorite", "");         // 上一次"自选已满"之类的提示到这里就该消失
    renderWatch();
    picker.refreshFavorites();         // 菜单里的 ☆ 跟着变
    scheduleWatch(0);
  } catch (error) {
    setWatchHint((wanted ? "收藏失败：" : "移出失败：") + error.message);
    setNotice("favorite", (wanted ? "收藏失败：" : "移出失败：") + error.message, "", "error");
  } finally {
    favoritesInFlight -= 1;
    if (favoritesInFlight === 0 && favoritesStale) {
      favoritesStale = false;
      loadFavorites();
    }
  }
}

async function loadFavorites() {
  try {
    const data = await fetchJson("/api/favorites");
    if (favoritesInFlight) return;     // 读的途中又有新改动: 以那次改动的响应为准
    favorites = data.symbols || [];
    renderWatch();
    picker.refreshFavorites();         // 目录可能比自选先到, 到齐后补画一次
    if (favorites.length) setWatchHint("");
    scheduleWatch(0);
  } catch (error) {
    setWatchHint("自选加载失败：" + error.message);
  }
}

setWatchCollapsed(readCollapsed());
$("watch-toggle").addEventListener("click", () => {
  setWatchCollapsed(!watchCollapsed());
  scheduleWatch(0);
});
// 页面切到后台就不再轮询, 回来立刻补一次。
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(watchTimer);
    clearTimeout(labelTimer);
  } else {
    scheduleWatch(0);
    labelOf(symbol);
  }
});

// ---------- 模拟交易(右侧面板; 撮合与账户在服务端 paper.py) ----------
// 面板自己轮询 /api/paper; 每次拿到新数据交给各图, 由图表组件重画 K 线上的成交标记与价格线。

const paperPanel = PaperPanel.create({
  byId: $,
  fetchJson,
  getSymbol: () => symbol,
  onState: (state) => charts.forEach((c) => c.setPaperState(state)),
});
paperPanel.start();

// ---------- AI 看图分析(图表区上方的浮层; 调 DeepSeek 与存盘在服务端 analysis.py) ----------
// 默认只发鼠标所在的那张图; 工具栏勾上「双图」两张一起发, 大周期在前。两张图周期相同时只发鼠标所在那张。

const aiHistory = AiHistory.create({ byId: $, getSymbol: () => symbol, getLabel: () => symbolLabel || symbol });
const aiPanel = AiPanel.create({
  byId: $,
  onSaved: () => aiHistory.refresh(),
  collect(both) {
    const picked = both && new Set(charts.map((c) => c.tf)).size > 1
      ? [...charts].sort((a, b) => b.tf - a.tf) : [activeChart];
    return {
      symbol,
      label: symbolLabel || symbol,
      // 买卖量口径不放这里: 各图实际的拆分粒度可能不同, 由每张图的 analysisInput() 带上
      settings: { mode: settings.mode, threshtype: settings.threshtype, bandK: settings.bandK },
      // 图上的持仓均价线、挂单线是什么, 文字里也说一声
      paper: PaperPanel.priceLines(paperPanel.state()).map(({ title, price }) => ({ title, price })),
      sources: picked.map((c) => ({ tf: c.tf, input: c.analysisInput(), screenshot: () => c.screenshot() })),
    };
  },
});
const tradeReview = TradeReview.create({ byId: $, getSymbol: () => symbol });

charts.forEach((c) => c.load());
loadFavorites();  // 自选独立: 面板先列出代码, 报价随轮询补齐(合约目录在 picker.load() 里自己拉)
labelOf(symbol);  // 合约名: 与图表加载并行, 拿不到就留着按钮按目录显示的名字
