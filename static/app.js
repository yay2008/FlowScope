/* FlowScope 前端: 页面级逻辑
 * 工具栏、合约选择器、自选面板、模拟交易面板在这里; 图表本身(K线/指标/足迹图与数据推送)是 chart-view.js 里
 * 可实例化的组件, 每张图一个主周期, 左上角的下拉框切换。页面放两张图左右并排(默认 10s、30s):
 * 工具栏状态 settings 两张图共用, 十字光标与可视时间范围联动, 主图指标的显示开关每张图各管各的。
 */
"use strict";

const $ = (id) => document.getElementById(id);
// 指标常量来自 indicators.js
const { BAND } = FlowIndicators;

// 工具栏状态: 所有图按引用共用, 只在这里改, 改完调各图对应的刷新入口。
// 下拉框的默认值必须与 index.html 里对应 <select> 的 selected 选项一致。
const settings = {
  mode: "cvd",
  threshtype: "Z-SCORE",
  cvdSource: "tick",
  klineLtf: 10,        // 切回 K线口径时保留上次选择，默认同 Pine 的 10S。
  view: "candle",      // 主图视图: candle=K线, footprint=足迹图
  bandK: 2,            // 叠加带的带宽倍数 k(回归残差标准差的倍数)
  wtSignal: "strong",  // 主图 WaveTrend 的交叉箭头: strong=只标强交叉(原版默认) / all=全部 / none=不标
};
let symbol = new URLSearchParams(location.search).get("symbol") || "KQ.m@SHFE.fu";
// 自选代码(服务端顺序即面板顺序)。常量必须早于合约选择器: picker.load() 会同步渲染
// 月份行并回调 isFavorite 画 ☆, 声明放后面会踩 let 的暂时性死区。
let favorites = [];

// 顶栏的合约名是只读展示: 切换合约只走合约选择器, 展示名由 /api/symbol 解析
// (主连会解析成当前标的月份合约的中文名), 在页面末尾统一发起。
$("symbol-name").title = symbol;

// 加密合约(币安/OKX/多所汇总)的 tick 口径是交易所逐笔成交自带的主动方向, 不是 TqSdk 快照估算, 下拉框照实写。
const CRYPTO_SYMBOL = /^(BINANCE|OKX|AGG)\./;
if (CRYPTO_SYMBOL.test(symbol)) {
  const tickOption = $("cvd-source").querySelector('option[value="tick"]');
  if (tickOption) tickOption.textContent = "交易所逐笔主动方向";
  $("cvd-source").title = "交易所逐笔主动方向：成交自带的主动买卖方，不用估算；TV K线口径：按小周期K线涨跌归类成交量。也影响Delta和买卖量。";
}

// ---------- 图表 ----------
// 左右两张图, 周期各自在左上角切换。后端按 (symbol, tf) 各出一份快照, 每张图各自加载、各自连推送。

// 两张图的周期记在 localStorage(切合约是整页重载, 不记的话每次都被打回默认), 读不到或不合法就用默认的
// 左 10s、右 30s。chartTfs 跟着切换更新, 早于 charts 声明: 建图时就会报连接状态(见 setChartStatus)。
const CHART_TFS_KEY = "flowscope.chartTfs";
const DEFAULT_CHART_TFS = [10, 30];

function loadChartTfs() {
  try {
    const saved = JSON.parse(localStorage.getItem(CHART_TFS_KEY) || "null");
    if (Array.isArray(saved) && saved.length === DEFAULT_CHART_TFS.length &&
        saved.every((tf) => ChartView.TF_CHOICES.includes(tf))) return saved;
  } catch (error) { /* 记不住不影响使用 */ }
  return [...DEFAULT_CHART_TFS];
}

function saveChartTfs() {
  try {
    localStorage.setItem(CHART_TFS_KEY, JSON.stringify(chartTfs));
  } catch (error) { /* 记不住不影响使用 */ }
}

const chartTfs = loadChartTfs();

// 各图主图指标的显示开关记在 localStorage, 按左右图各记一份 [{ema, band, wt}, {...}]: 切合约是整页重载,
// 不记的话每切一次都被打回默认。开关跟着图走, 不跟周期走(两张图可以选同一个周期, 切周期也不换开关)。
// 旧版按周期记 flowscope.mainShown = {"10": {...}, "30": {...}}(更早只记一份 {ema, band, wt}),
// 新键还没有时按旧值初始化; 读不到(隐私模式、旧值不合法)就用默认值。
const CHART_SHOWN_KEY = "flowscope.chartShown";
const MAIN_SHOWN_KEY = "flowscope.mainShown";

function loadShown(index, tf) {
  const shown = { ema: true, band: false, wt: true };
  try {
    const perChart = JSON.parse(localStorage.getItem(CHART_SHOWN_KEY) || "null");
    let mine = Array.isArray(perChart) ? perChart[index] : null;
    if (!mine || typeof mine !== "object") {
      const legacy = JSON.parse(localStorage.getItem(MAIN_SHOWN_KEY) || "{}");
      mine = legacy && typeof legacy[tf] === "object" ? legacy[tf] : legacy;
    }
    for (const key of Object.keys(shown)) {
      if (typeof mine?.[key] === "boolean") shown[key] = mine[key];
    }
  } catch (error) { /* 记不住不影响使用 */ }
  return shown;
}

function saveShown() {
  try {
    localStorage.setItem(CHART_SHOWN_KEY, JSON.stringify(charts.map((c) => c.shown())));
  } catch (error) { /* 记不住不影响使用 */ }
}

// 鼠标所在(最近进入)的那张图: 顶栏读数显示它, 十字光标与缩放/拖动以它为准同步另一张。默认右边那张。
// 只由鼠标进入图表区域来切换, 十字光标事件不算(见 syncCrosshair)。
let activeChart = null;
const chartStatus = new Map();   // 第几张图 -> {ok, text}

function makeChart(tf, index) {
  const chartView = ChartView.create({
    host: $("chart"),
    tf,
    symbol,
    settings,
    shown: loadShown(index, tf),
    onStatus: (ok, text) => setChartStatus(index, ok, text),
    onLegend: (text, coverage) => showLegend(chartView, text, coverage),
    onConfig: refreshLtfOptions,
    onShownChange: () => {
      saveShown();
      refreshBandControl();
    },
    onCrosshair: (time, price) => syncCrosshair(chartView, time, price),
    onRangeChange: syncRanges,
    onTfChange: () => {
      chartTfs[index] = chartView.tf;
      saveChartTfs();
      refreshLtfOptions();
      showChartStatus();
      if (chartView === activeChart) chartView.refreshLegend();
    },
  });
  chartView.el.addEventListener("pointerenter", () => activate(chartView));
  return chartView;
}

const charts = chartTfs.map(makeChart);
activeChart = charts[charts.length - 1];

function activate(chartView) {
  if (chartView === activeChart) return;
  activeChart = chartView;
  chartView.refreshLegend();
}

// 顶栏连接状态: 两张图都连上才显示「已连接」, 否则列出没连上的(带周期前缀)
function setChartStatus(index, ok, text) {
  chartStatus.set(index, { ok, text });
  showChartStatus();
}

function showChartStatus() {
  const pending = chartTfs.map((tf, index) => index).filter((index) => !chartStatus.get(index)?.ok);
  const el = $("status");
  el.className = pending.length ? "off" : "on";
  el.textContent = pending.length
    ? pending.map((index) => `${ChartView.tfLabel(chartTfs[index])}: ${chartStatus.get(index)?.text ?? "连接中…"}`)
      .join("  ")
    : "已连接";
}

// 顶栏读数只显示鼠标所在那张图(带周期前缀), 另一张的读数直接忽略
function showLegend(chartView, text, coverage) {
  if (chartView !== activeChart) return;
  $("legend").textContent = `${ChartView.tfLabel(chartView.tf)} · ${text}`;
  $("coverage").textContent = coverage;
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
const FULL_SYNC_RATIO = 3;

function syncRanges() {
  if (!activeChart) return;
  const range = activeChart.visibleTimeRange();
  if (!range) return;
  for (const c of charts) {
    if (c === activeChart) continue;
    const ratio = Math.max(c.tf, activeChart.tf) / Math.min(c.tf, activeChart.tf);
    if (ratio <= FULL_SYNC_RATIO) c.setVisibleTimeRange(range);
    else c.alignRightEdge(range.to);
  }
}

// 「带宽」只在有图打开 FlowWave带 时可调。判据用开关本身而不是画面上有没有带: 足迹图下带只是被临时藏起来,
// 宽度选择仍然有效, 切回 K 线就用得上, 没必要在这里置灰。
function refreshBandControl() {
  $("band-k").disabled = !charts.some((c) => c.shown().band);
}
refreshBandControl();

// ---------- 工具栏 ----------

// 模式/阈值只影响 Volume Suite 和图例读数
function renderSuites() {
  charts.forEach((c) => c.renderSuite());
  activeChart.refreshLegend();
}
$("mode").addEventListener("change", (e) => { settings.mode = e.target.value; renderSuites(); });
$("threshtype").addEventListener("change", (e) => { settings.threshtype = e.target.value; renderSuites(); });

$("wt-signal").addEventListener("change", (e) => {
  settings.wtSignal = ["strong", "all", "none"].includes(e.target.value) ? e.target.value : "strong";
  charts.forEach((c) => c.renderWtMarks());
});
$("band-k").addEventListener("change", (e) => {
  const value = parseFloat(e.target.value);
  settings.bandK = BAND.kOptions.includes(value) ? value : BAND.kDefault;   // 非法值回落到默认, 不按垃圾值画带
  e.target.value = String(settings.bandK);
  charts.forEach((c) => c.rebuildBand());
  activeChart.refreshLegend();
});

$("view").addEventListener("change", (e) => {
  settings.view = e.target.value;
  charts.forEach((c) => c.setView());
});

// ---------- CVD 口径与拆分粒度 ----------
// 拆分粒度随主周期收敛: 必须整除主周期且不比它粗(15/30 在 10s 下不合法)。工具栏上的选择所有图共用,
// 每张图请求时各自把用不了的选择回落到本周期下最粗的合法粒度(chart-view.js 的 currentLtf),
// 所以这里只禁用哪张图都用不了的选项。
function refreshLtfOptions() {
  const usable = new Set(charts.flatMap((c) => c.ltfOptions()));
  for (const option of $("ltf").options) {
    option.disabled = !usable.has(Number(option.value));
  }
}

function updateSplitSelection() {
  $("ltf").disabled = settings.cvdSource === "tick";
  $("ltf").value = String(settings.klineLtf);
  charts.forEach((c) => c.load());
}
$("cvd-source").addEventListener("change", (e) => {
  settings.cvdSource = e.target.value;
  updateSplitSelection();
});
$("ltf").addEventListener("change", (e) => {
  settings.klineLtf = parseInt(e.target.value, 10);
  updateSplitSelection();
});

// ---------- 合约选择器: 自研二级级联菜单 ----------
// 组件在 contract-picker.js(分类页签 + 一级品种 + 二级月份, 含搜索/键盘/悬停预取), 纯逻辑在 picker-core.js。
// 选择结果统一写回 URL 的 symbol 参数, 由页面重载完成切换与重连。

function setPickerHint(text, detail) {
  const hint = $("picker-hint");
  hint.textContent = text || "";
  hint.title = detail || "";
}

// 合约展示名: 只读, 取不到就回退成合约代码, 不阻塞图表加载。
async function labelOf(target) {
  try {
    const data = await fetchJson(`/api/symbol?symbol=${encodeURIComponent(target)}`);
    if (symbol === target && data && data.label) $("symbol-name").textContent = data.label;
  } catch (error) {
    $("symbol-name").textContent = target;
  }
}

function switchSymbol(target) {
  if (!target || target === symbol) return;
  // 重载前先给出反馈: 新页面的展示位会自己再解析一次。
  $("symbol-name").textContent = "加载中…";
  $("symbol-name").title = target;
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
  onStatus: setPickerHint,
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
  list.innerHTML = "";
  for (const code of favorites) {
    const row = watchRows.get(code) || { symbol: code, name: code };
    const item = document.createElement("li");
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
    top.append(name, remove);

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

    item.addEventListener("click", () => switchSymbol(code));
    list.appendChild(item);
  }
  if (!favorites.length) setWatchHint("在「选择合约」菜单里点 ☆ 加入自选");
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
    setPickerHint("");                 // 上一次"自选已满"之类的提示到这里就该消失
    renderWatch();
    picker.refreshFavorites();         // 菜单里的 ☆ 跟着变
    scheduleWatch(0);
  } catch (error) {
    setWatchHint((wanted ? "收藏失败：" : "移出失败：") + error.message);
    setPickerHint((wanted ? "收藏失败：" : "移出失败：") + error.message);
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
  if (document.hidden) clearTimeout(watchTimer);
  else scheduleWatch(0);
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

charts.forEach((c) => c.load());
loadFavorites();  // 自选独立: 面板先列出代码, 报价随轮询补齐(合约目录在 picker.load() 里自己拉)
labelOf(symbol);  // 顶栏合约名: 与图表加载并行, 拿不到就显示代码
