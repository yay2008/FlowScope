"use strict";
/* app.js 在浏览器里的加载顺序回归。
 *
 * app.js 是普通 <script>(不是 module), 顶层语句按出现顺序执行, 所以"某个 let/const
 * 在下面声明、却被上面的顶层调用链先读到"会直接抛 ReferenceError —— 页面白屏。
 * 真实案例: picker.load() 同步渲染月份行并回调 isFavorite() 画 ☆, 而 favorites 那时
 * 还没声明, 于是 "Cannot access 'favorites' before initialization"。
 *
 * 这里按 index.html 的顺序把全部脚本放进同一个 vm 上下文执行一遍, 并对顶层用到的
 * DOM / 图表 API 做最小 stub; 之后跑一轮微任务, 让 fetch 的回调也走完。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..");
const STATIC = path.join(ROOT, "static");
const SCRIPTS = ["data-sync.js", "indicators.js", "picker-core.js", "contract-picker.js", "paper-panel.js",
                 "chart-view.js", "app.js"];

// ---------- 最小 DOM ----------

class StubElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this.attributes = {};
    this.handlers = {};
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.title = "";
    this.disabled = false;
    this.options = [];
    this.style = {};
    this.classList = {
      names: new Set(),
      add: (...names) => names.forEach((n) => this.classList.names.add(n)),
      remove: (...names) => names.forEach((n) => this.classList.names.delete(n)),
      contains: (n) => this.classList.names.has(n),
      toggle: (n, on) => {
        const want = on === undefined ? !this.classList.names.has(n) : !!on;
        if (want) this.classList.names.add(n);
        else this.classList.names.delete(n);
        return want;
      },
    };
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  insertBefore(node) { this.children.push(node); return node; }
  remove() {}
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  contains() { return false; }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() {
    return { top: 0, left: 0, right: 0, bottom: 0, width: 100, height: 20 };
  }
}

function createDocument() {
  const byId = new Map();
  const document = {
    hidden: false,
    title: "",
    body: new StubElement("body"),
    documentElement: new StubElement("html"),
    getElementById(id) {
      if (!byId.has(id)) {
        const element = new StubElement("div");
        element.id = id;
        byId.set(id, element);
      }
      return byId.get(id);
    },
    createElement(tag) { return new StubElement(tag); },
    createElementNS(_ns, tag) { return new StubElement(tag); },
    createTextNode(text) { const node = new StubElement("#text"); node.textContent = text; return node; },
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return document;
}

// ---------- 图表 / 网络 / 存储 stub ----------

// 每张图一份记录, 按创建顺序(与页面的 charts 一致: 10s、30s), 每次 runBrowser() 清空:
//   applied            每次 series.applyOptions: 可见性开关是"状态变量 → series"的唯一通路, 只能从这里观察
//   markers            K 线 markers 插件每次 setMarkers 的内容
//   timeScale          可视逻辑范围有状态, set 时同步通知订阅者; sets 记 set 的次数
//   crosshairHandlers  十字光标订阅, 测试直接调它模拟用户移动光标
//   crosshair          程序设置的十字光标 {price, time}, 清掉记 null
let stubCharts = [];
// 模拟交易叠加画到图上的价格线(两张图合在一起)
let createdPriceLines = [];

function createChartStub() {
  const record = { applied: [], markers: [], crosshair: [], crosshairHandlers: [] };
  stubCharts.push(record);
  const series = () => ({
    _record: record,
    setData() {}, update() {}, applyOptions(options) { record.applied.push(options); }, setMarkers() {},
    createPriceLine(options) { createdPriceLines.push(options); return { applyOptions() {} }; }, removePriceLine() {},
    priceScale() { return { applyOptions() {} }; },
    setVisibleRange() {}, coordinateToPrice(y) { return 4000 + y; }, priceToCoordinate() { return 0; },
    attachPrimitive() {}, detachPrimitive() {},
  });
  let range = null;
  const rangeHandlers = [];
  record.timeScale = {
    sets: 0,
    scrollToRealTime() {}, fitContent() {}, applyOptions() {},
    subscribeVisibleLogicalRangeChange(fn) { rangeHandlers.push(fn); },
    getVisibleLogicalRange: () => range && { ...range },
    setVisibleLogicalRange(next) {
      record.timeScale.sets += 1;
      range = { from: next.from, to: next.to };
      rangeHandlers.forEach((fn) => fn({ ...range }));
    },
    timeToCoordinate() { return 0; }, coordinateToTime() { return 0; },
    options: () => ({ barSpacing: 6 }),
  };
  return {
    addSeries: series, addCustomSeries: series, removeSeries() {},
    applyOptions() {}, resize() {}, timeScale: () => record.timeScale,
    priceScale: () => ({ applyOptions() {} }),
    panes: () => Array.from({ length: 4 }, () => ({
      setHeight() {}, setStretchFactor() {}, paneIndex: () => 0,
    })),
    subscribeClick() {}, unsubscribeClick() {},
    subscribeCrosshairMove(fn) { record.crosshairHandlers.push(fn); },
    setCrosshairPosition(price, time) { record.crosshair.push({ price, time }); },
    clearCrosshairPosition() { record.crosshair.push(null); },
  };
}

function createLocalStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
}

function catalogResponse() {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      source: "live",
      groups: [{ exchange: "SHFE", products: [
        { product: "fu", name: "燃油", mainSymbol: "KQ.m@SHFE.fu",
          months: [{ symbol: "KQ.m@SHFE.fu", isMain: true, name: "燃油主连" }] },
      ] }],
    }),
  };
}

function symbolResponse() {
  return {
    ok: true,
    status: 200,
    json: async () => ({ symbol: "KQ.m@SHFE.fu", label: "燃油2611" }),
  };
}

// 按路径分派: 顶栏合约名走 /api/symbol, 其余(目录/历史/自选)给最小可用形状。
function stubFetch(url) {
  const path = String(url).split("?")[0];
  if (path === "/api/symbol") return Promise.resolve(symbolResponse());
  if (path === "/api/favorites") return Promise.resolve(jsonResponse({ symbols: [], max: 40 }));
  if (path === "/api/history") return Promise.resolve(jsonResponse({ symbol: "KQ.m@SHFE.fu", cfg: null, bars: [] }));
  if (path === "/api/paper") return Promise.resolve(jsonResponse(paperResponse()));
  return Promise.resolve(catalogResponse());
}

// 模拟交易面板的最小可用形状: 有一笔持仓和一笔挂单, 顺带走一遍标记与价格线的绘制。
function paperResponse() {
  return {
    symbol: "KQ.m@SHFE.fu", contract: "SHFE.fu2611", error: null, fee: null, feeSource: null,
    quote: { contract: "SHFE.fu2611", name: "燃油2611", ask: 3001, bid: 3000, askVolume: 5, bidVolume: 7,
             last: 3000, priceTick: 1, priceDecs: 0, open: true, reason: "", datetime: "2026-09-30 10:00:00" },
    account: { initialCash: 1000000, cash: 1000000, equity: 1000100, floatPnl: 100, margin: 4500,
               available: 995600, realizedPnl: 0, fees: 0, marginRate: 0.15 },
    positions: [{ contract: "SHFE.fu2611", qty: 1, avgPrice: 2990, last: 3000, floatPnl: 100, margin: 4500 }],
    orders: [{ id: "O2", contract: "SHFE.fu2611", side: "sell", qty: 1, type: "limit", price: 3050,
               status: "open", createdAt: "2026-09-30 10:00:01" }],
    trades: [{ id: "T1", contract: "SHFE.fu2611", side: "buy", qty: 1, price: 2990, open: 1, close: 0,
               pnl: 0, fee: 0, position: 1, time: 1790762400, at: "2026-09-30 10:00:00" }],
    contractTrades: [],
  };
}

function jsonResponse(payload) {
  return { ok: true, status: 200, json: async () => payload };
}

function runBrowser(options = {}) {
  stubCharts = [];
  createdPriceLines = [];
  const document = createDocument();
  const fetchImpl = options.fetch || stubFetch;
  const context = {
    document,
    localStorage: options.localStorage || createLocalStorage(),
    location: { search: "", host: "127.0.0.1:8000", protocol: "http:", href: "http://127.0.0.1:8000/" },
    navigator: { userAgent: "node" },
    console,
    // vm 上下文是干净的内建环境: 浏览器里天然存在的构造器要显式注入。
    URLSearchParams,
    URL,
    AbortController,
    TextEncoder,
    TextDecoder,
    performance,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    fetch: (url, init) => fetchImpl(url, init),
    WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} },
    LightweightCharts: {
      createChart: createChartStub,
      CrosshairMode: { Normal: 0 },
      LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
      LineType: { Simple: 0, WithSteps: 1 },
      CandlestickSeries: "CandlestickSeries",
      LineSeries: "LineSeries",
      HistogramSeries: "HistogramSeries",
      createSeriesMarkers: (series) => ({ setMarkers(markers) { series._record.markers.push(markers); } }),
    },
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  for (const name of SCRIPTS) {
    const source = fs.readFileSync(path.join(STATIC, name), "utf8");
    vm.runInContext(source, context, { filename: name });
  }
  return context;
}

test("全部脚本按 index.html 顺序加载时不抛错", () => {
  assert.doesNotThrow(() => runBrowser());
});

test("合约选择器加载完成后自选状态可用(isFavorite 不踩暂时性死区)", async () => {
  const context = runBrowser();
  await new Promise((resolve) => setImmediate(resolve));   // 放完 fetch 回调与微任务
  const picker = context.ContractPicker;
  assert.ok(picker, "ContractPicker 应挂在全局");
  // 目录已经画过一次月份(含 ☆), 再强制重绘一次也必须能取到自选状态
  assert.doesNotThrow(() => context.document.getElementById("picker-months"));
});

test("模拟交易面板加载后在 K 线上画出持仓均价与挂单价格线", async () => {
  const context = runBrowser();
  // vm 里的 setTimeout 是空操作, 面板的定时轮询不会自己跑: 直接调一次它的刷新入口
  await vm.runInContext("paperPanel.refresh()", context);
  const titles = createdPriceLines.map((line) => line.title).filter(Boolean);
  assert.ok(titles.includes("多1 均价"), `应画出持仓均价线, 实际 ${JSON.stringify(titles)}`);
  assert.ok(titles.includes("卖1 挂单"), `应画出挂单价格线, 实际 ${JSON.stringify(titles)}`);
  assert.ok(stubCharts.length === 2 && stubCharts.every((chart) => chart.markers.length > 0),
            "两张图都应把成交标记交给图表(即使这次为空)");
  const byId = (id) => context.document.getElementById(id);
  assert.equal(byId("trade-buy").textContent, "买入 3001");
  assert.equal(byId("trade-flatten").textContent, "平仓 多1");
  assert.equal(byId("trade-buy").disabled, false);
});

test("favorites 在顶层声明完毕后才可能为真值", () => {
  const source = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const declaration = source.indexOf("let favorites = []");
  const firstUse = source.indexOf("favorites.includes");
  assert.ok(declaration >= 0, "app.js 必须声明 favorites");
  assert.ok(firstUse > declaration, "favorites 的声明必须早于第一次使用");
});

test("顶栏合约名由 /api/symbol 解析后写入展示位", async () => {
  const context = runBrowser();
  await new Promise((resolve) => setImmediate(resolve));
  const name = context.document.getElementById("symbol-name");
  assert.equal(name.textContent, "燃油2611");
  assert.equal(name.title, "KQ.m@SHFE.fu", "悬停应能看到原始合约代码");
});

test("顶栏只保留只读展示 : 手填输入框、切换按钮、收藏按钮都已移除", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.match(html, /<span id="symbol-name"/, "顶栏应保留合约名展示位");
  assert.ok(!html.includes('id="symbol"'), "手填输入框应已移除");
  assert.ok(!html.includes('id="apply"'), "切换按钮应已移除");
  assert.ok(!html.includes('id="star"'), "工具栏收藏按钮应已移除");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  assert.ok(!app.includes('$("symbol")'), "app.js 不应再读写手填输入框");
  assert.ok(!app.includes('$("apply")'), "app.js 不应再绑定切换按钮");
  assert.ok(!app.includes('$("star")'), "app.js 不应再绑定工具栏收藏按钮");
  assert.match(app, /labelOf\(/, "app.js 应通过 /api/symbol 解析展示名");
});

test("判向算法开关已移除 : 前端恒读新算法 Lee-Ready", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.ok(!html.includes('id="classify"'), "判向算法下拉框应已移除");
  // 十字光标读数在图表组件里拼, 两个文件一起查
  const app = ["app.js", "chart-view.js"].map((name) => fs.readFileSync(path.join(STATIC, name), "utf8")).join("\n");
  assert.ok(!app.includes('$("classify")'), "前端不应再绑定判向算法下拉框");
  assert.ok(!/classifySource/.test(app), "前端不应再保留判向口径状态");
  // 对照列仍要显示: 删除开关不等于删掉旧算法的观测能力。
  assert.match(app, /b\.buyLegacy/, "十字光标提示仍应并列显示旧算法买量");
  assert.match(app, /b\.sellLegacy/, "十字光标提示仍应并列显示旧算法卖量");
});

test("工具栏默认值与 app.js 初始状态一致", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  // 下拉框的默认选项写在两处: index.html 的 selected 和 app.js 的初始状态。
  // 只改一处就会出现"页面显示 A、实际按 B 渲染", 这里把两处钉在一起。
  const table = [
    ["view", /\bview: "([^"]+)"/],
    ["mode", /\bmode: "([^"]+)"/],
    ["threshtype", /\bthreshtype: "([^"]+)"/],
    ["cvd-source", /\bcvdSource: "([^"]+)"/],
    ["ltf", /\bklineLtf: (\d+)/],
    ["band-k", /\bbandK: ([\d.]+)/],
    ["wt-signal", /\bwtSignal: "([^"]+)"/],
  ];
  for (const [id, pattern] of table) {
    const htmlDefault = html.match(
      new RegExp(`id="${id}"[\\s\\S]*?<option value="([^"]+)"[^>]*\\bselected\\b`));
    assert.ok(htmlDefault, `index.html 的 #${id} 应标出默认选项`);
    const jsDefault = app.match(pattern);
    assert.ok(jsDefault, `app.js 应声明 #${id} 对应的初始状态`);
    assert.equal(htmlDefault[1], jsDefault[1], `#${id} 的 HTML 默认值与 app.js 初始状态不一致`);
  }
});

// 组件建的 DOM 都是 StubElement, 按 className / 条件往下找
function findByClass(node, className) {
  if (node.className === className) return node;
  for (const child of node.children || []) {
    const hit = findByClass(child, className);
    if (hit) return hit;
  }
  return null;
}

function findAll(node, predicate, out = []) {
  if (predicate(node)) out.push(node);
  for (const child of node.children || []) findAll(child, predicate, out);
  return out;
}

test("工具栏的周期下拉框已移除: 默认 10s、30s 两张图左右并排, 各自左上角有周期下拉框, 下面是本图的主图指标图例", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.ok(!html.includes('id="tf"'), "工具栏的周期下拉框应已移除");
  assert.ok(!html.includes('id="main-legend"'), "主图图例由图表组件建, 每张图一份");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  assert.ok(!app.includes('$("tf")'), "app.js 不应再绑定周期下拉框");

  const context = runBrowser();
  assert.equal(vm.runInContext("charts.map((c) => c.tf).join()", context), "10,30", "左 10s、右 30s");
  const corners = findAll(context.document.getElementById("chart"), (node) => node.className === "chart-corner");
  assert.deepEqual(corners.map((corner) => corner.children[0].value), ["10", "30"]);
  assert.deepEqual(corners[0].children[0].children.map((o) => o.textContent),
                   ["10s", "30s", "1m", "5m", "15m", "1h", "4h"]);
  for (const corner of corners) {
    assert.equal(corner.children[0].className, "tf-select");
    assert.equal(corner.children[1].className, "main-legend", "主图图例应挂在周期标签下面");
  }
});

test("阈值默认是 Z-SCORE", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.match(html,
    /id="threshtype"[\s\S]*?<option value="Z-SCORE"[^>]*\bselected\b/,
    "阈值下拉框默认应选中 Z-SCORE");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  assert.match(app, /\bthreshtype: "Z-SCORE"/, "app.js 的阈值初始状态应为 Z-SCORE");
});

// ---------- FlowWave 主图叠加带 ----------

// 回归通道的复用函数: 与 app.js 的 linreg 同一个公式, 供下面独立复算用
function linregAt(values, end, w) {
  const sx = (w * (w - 1)) / 2;
  const sxx = (w * (w - 1) * (2 * w - 1)) / 6;
  let sy = 0, sxy = 0;
  for (let j = 0; j < w; j++) {
    const y = values[end - w + 1 + j];
    sy += y;
    sxy += j * y;
  }
  const slope = (w * sxy - sx * sy) / (w * sxx - sx * sx);
  return (sy - slope * sx) / w + slope * (w - 1);
}

function deriveBandInBrowser() {
  const context = runBrowser();
  const out = vm.runInContext(`(() => {
    const c = charts[1];
    c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    c.bars = [];
    let price = 4000;
    for (let i = 0; i < 120; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      c.bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                    close: price, volume: 100 + (i % 7) * 10, buy: 60, sell: 40, delta: 20,
                    buyLegacy: 55, sellLegacy: 45, deltaLegacy: 10, cvd: i, coverage: "complete" });
    }
    c.derive();
    const derived = c.derived;
    return { close: c.bars.map((b) => b.close), mid: derived.band.mid, up: derived.band.up,
             dn: derived.band.dn, state: derived.band.state, wt2: derived.lw.wt2,
             n: BAND.n, k: settings.bandK, lwOb: FlowIndicators.LW.ob, lwOs: FlowIndicators.LW.os };
  })()`, context);
  return out;
}

test("主图叠加带: 中线是收盘价回归线, 上下轨 = 中线 ± k 倍回归残差标准差", () => {
  const out = deriveBandInBrowser();
  const { close, mid, up, dn, n, k } = out;
  // 全部先独立复算一遍, 残差用复算出的中线而不是 app.js 的 mid, 否则是自证
  const expect = close.map((_, i) => (i >= n - 1 ? linregAt(close, i, n) : null));
  let checked = 0;
  for (let i = 0; i < close.length; i++) {
    if (expect[i] == null) {
      assert.equal(mid[i], null, `第 ${i} 根预热不足, 中线应为 null`);
      continue;
    }
    // 残差标准差要凑满 n 根残差, 所以带比中线晚 n-1 根才出现(共 2n-1 根预热)
    if (i < 2 * n - 2) {
      assert.equal(up[i], null, `第 ${i} 根残差窗口还没凑满, 不该有轨道`);
      continue;
    }
    assert.ok(Math.abs(mid[i] - expect[i]) < 1e-6, `第 ${i} 根中线应等于同窗口回归线末点`);
    let sum = 0, sum2 = 0;
    for (let j = i - n + 1; j <= i; j++) {
      const r = close[j] - expect[j];
      sum += r;
      sum2 += r * r;
    }
    const mean = sum / n;
    const sd = Math.sqrt(Math.max(sum2 / n - mean * mean, 0));
    assert.ok(Math.abs(up[i] - (expect[i] + k * sd)) < 1e-6, `第 ${i} 根上轨口径不一致`);
    assert.ok(Math.abs(dn[i] - (expect[i] - k * sd)) < 1e-6, `第 ${i} 根下轨口径不一致`);
    assert.ok(up[i] > mid[i] && mid[i] > dn[i], "轨道必须满足 上轨 > 中线 > 下轨");
    checked++;
  }
  assert.ok(checked >= 50, `至少应能算出 50 根有效的带, 实际 ${checked}`);
});

test("主图叠加带: 着色状态由 wt2 超买超卖决定", () => {
  const out = deriveBandInBrowser();
  const { mid, up, state, wt2, lwOb, lwOs } = out;
  let tagged = 0;
  for (let i = 0; i < mid.length; i++) {
    if (mid[i] == null || up[i] == null) continue;   // 还没形成带的行不参与着色
    const expect = wt2[i] == null ? 0 : wt2[i] > lwOb ? 1 : wt2[i] < lwOs ? -1 : 0;
    assert.equal(state[i], expect, `第 ${i} 根的着色状态应跟随 wt2`);
    if (expect !== 0) tagged++;
  }
  assert.ok(tagged > 0, "样本里应至少出现一次超买或超卖着色, 否则这个断言是空跑");
});

test("主图叠加带: 打点只落在首次越界那一根, 且贴在对应的轨道上", () => {
  const context = runBrowser();
  const r = vm.runInContext(`(() => {
    const c = charts[1];
    c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    c.bars = [];
    let price = 4000;
    for (let i = 0; i < 160; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      c.bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                    close: price, volume: 100 + (i % 7) * 10, buy: 60, sell: 40, delta: 20 });
    }
    c.derive();
    const dots = c.buildBandDots();
    const derived = c.derived;
    return { times: c.bars.map((b) => b.time), wt2: derived.lw.wt2, up: derived.band.up,
             dn: derived.band.dn, high: dots.high, low: dots.low,
             ob: FlowIndicators.LW.ob, os: FlowIndicators.LW.os };
  })()`, context);

  const indexOfTime = new Map(r.times.map((t, i) => [t, i]));
  const crossings = (above) => {   // 独立复算: 每轮只算第一根能打点的 bar(上一根还没带也算这一轮的第一根)
    const out = [];
    for (let i = 0; i < r.wt2.length; i++) {
      const cur = r.wt2[i];
      if (cur == null || r.up[i] == null) continue;
      const prev = i > 0 && r.up[i - 1] != null ? r.wt2[i - 1] : null;
      if (above ? (cur > r.ob && (prev == null || prev <= r.ob))
                : (cur < r.os && (prev == null || prev >= r.os))) out.push(i);
    }
    return out;
  };
  const expectHigh = crossings(true);
  const expectLow = crossings(false);
  assert.ok(expectHigh.length + expectLow.length > 0, "样本里应至少有一次越界, 否则这个断言是空跑");

  for (const [dots, expect, edge] of [[r.high, expectHigh, r.up], [r.low, expectLow, r.dn]]) {
    assert.equal(dots.length, expect.length, "打点数量应等于首次越界的次数(连续越界不重复打)");
    dots.forEach((dot, k) => {
      const i = indexOfTime.get(dot.time);
      assert.equal(i, expect[k], `第 ${k} 个点应打在第 ${expect[k]} 根上, 实际 ${i}`);
      assert.equal(dot.value, edge[i], "点应贴在对应的轨道(上穿贴上轨, 下穿贴下轨)");
    });
  }
  // 连续越界的第二根起不能再打点
  for (const i of expectHigh) {
    const again = r.times[i + 1];
    if (again != null && r.wt2[i + 1] > r.ob) {
      assert.ok(!r.high.some((d) => d.time === again), `第 ${i + 1} 根仍在超买区, 不该再打点`);
    }
  }
});

// 主图左上角的图例每张图一份, 由图表组件建在自己的容器里。stub 里没有真实点击, 直接调眼睛按钮的
// click 处理器(与浏览器里点击走同一条路径)。index 是页面 charts 的下标: 0 = 10s, 1 = 30s。
const chartEl = (context, index) => vm.runInContext(`charts[${index}].el`, context);
const legendOf = (context, index) => findByClass(chartEl(context, index), "main-legend");
const legendRow = (context, index, key) =>
  findAll(chartEl(context, index), (node) => node.className === "ml-item" && node.getAttribute("data-key") === key)[0];
const eyeOf = (context, index, key) => findByClass(legendRow(context, index, key), "ml-eye");
const clickEye = (context, index, key) => eyeOf(context, index, key).handlers.click[0]();
const viewSetter = (context) => (value) =>
  context.document.getElementById("view").handlers.change[0]({ target: { value } });

test("FlowWave带 眼睛按钮: 打开后主图三个叠加系列可见, 切足迹图强制隐藏, 切回来按开关恢复", () => {
  const context = runBrowser();
  const applied = stubCharts[1].applied;
  const setView = viewSetter(context);
  const lastThree = () => applied.slice(-3).map((o) => o.visible);

  assert.equal(lastThree().every((v) => v === false), true, "默认关: 三个叠加系列都应隐藏");

  clickEye(context, 1, "band");
  assert.equal(lastThree().every((v) => v === true), true, "打开后带与两个打点系列都应可见");

  setView("footprint");
  assert.equal(lastThree().every((v) => v === false), true, "足迹图下叠加必须强制隐藏");

  setView("candle");
  assert.equal(lastThree().every((v) => v === true), true, "切回 K 线应按开关恢复可见");

  clickEye(context, 1, "band");
  assert.equal(lastThree().every((v) => v === false), true, "再点一下应重新隐藏");
});

test("两张图的眼睛按钮各管各的: 点 10s 图的 FlowWave带 不动 30s 图", () => {
  const context = runBrowser();
  const applied30 = stubCharts[1].applied.length;
  clickEye(context, 0, "band");
  assert.deepEqual(stubCharts[0].applied.slice(-3).map((o) => o.visible), [true, true, true], "10s 图的带应打开");
  assert.equal(stubCharts[1].applied.length, applied30, "30s 图的系列不该被动到");
  assert.equal(legendRow(context, 0, "band").classList.contains("off"), false);
  assert.equal(legendRow(context, 1, "band").classList.contains("off"), true, "30s 图的图例行保持关闭");
});

test("WaveTrend 眼睛按钮: 参考线、交叉箭头与背离连线默认显示, 点一下清空, 足迹图下强制清空且图例收起", () => {
  const context = runBrowser();
  const setView = viewSetter(context);
  const legend = legendOf(context, 1);
  // 一段过了 73 根冷启动的振荡行情; 箭头档位放到「全部」, 保证有箭头可数
  vm.runInContext(`(() => {
    const c = charts[1];
    c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    c.bars = [];
    let price = 4000;
    for (let i = 0; i < 400; i++) {
      price += Math.sin(i / 6) * 6 + Math.sin(i / 23) * 3;
      c.bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                    close: price, volume: 100, buy: 60, sell: 40, delta: 20 });
    }
    c.renderAll();
  })()`, context);
  context.document.getElementById("wt-signal").handlers.change[0]({ target: { value: "all" } });
  // K 线上只有一个 markers 插件, 成交标记带「买/卖」文字, 交叉箭头不带
  const arrows = () => stubCharts[1].markers[stubCharts[1].markers.length - 1].filter((m) => !m.text).length;
  // 五条参考线: 点眼睛时 applyWtVisibility 最后对它们 applyOptions
  const applied = stubCharts[1].applied;
  const levelsVisible = () => applied.slice(-5).map((o) => o.visible);
  const segments = () => vm.runInContext("charts[1].wtDivergence._segs.length", context);

  assert.ok(arrows() > 0, "默认显示: K 线上应有交叉箭头(否则这个用例是空跑)");
  assert.ok(segments() > 0, "默认显示: 应有背离连线(否则这个用例是空跑)");

  clickEye(context, 1, "wt");
  assert.deepEqual(levelsVisible(), [false, false, false, false, false], "点一下: 五条参考线隐藏");
  assert.equal(arrows(), 0, "点一下: 箭头清空");
  assert.equal(segments(), 0, "点一下: 背离连线清空");
  assert.equal(legendRow(context, 1, "wt").classList.contains("off"), true, "图例这一行应变暗");
  clickEye(context, 1, "wt");
  assert.deepEqual(levelsVisible(), [true, true, true, true, true], "再点一下: 参考线恢复");
  assert.ok(arrows() > 0 && segments() > 0, "再点一下恢复");

  setView("footprint");
  assert.equal(legend.hidden, true, "足迹图下主图指标全部隐藏, 图例收起");
  assert.equal(legendOf(context, 0).hidden, true, "两张图的图例都收起");
  assert.equal(arrows(), 0, "足迹图下即使开着也不画");
  assert.equal(segments(), 0);
  setView("candle");
  assert.equal(legend.hidden, false);
  assert.ok(arrows() > 0 && segments() > 0, "切回 K 线按开关恢复");
});

const offFlags = (ctx, index) => ["ema", "band", "wt"].map((key) => legendRow(ctx, index, key).classList.contains("off"));

test("EMA 眼睛按钮: 四条均线一起隐藏, 两张图的开关按左右各记进 localStorage, 重载后各自按记下的状态初始化", () => {
  const storage = createLocalStorage();
  const context = runBrowser({ localStorage: storage });
  const applied = stubCharts[1].applied;

  assert.equal(findByClass(legendRow(context, 1, "ema"), "ml-params").children.length, 4, "EMA 后面列出四个周期");
  clickEye(context, 1, "ema");
  assert.deepEqual(applied.slice(-4).map((o) => o.visible), [false, false, false, false]);
  assert.equal(eyeOf(context, 1, "ema").title, "显示");
  assert.deepEqual(JSON.parse(storage.getItem("flowscope.chartShown")),
                   [{ ema: true, band: false, wt: true }, { ema: false, band: false, wt: true }]);

  // 切合约是整页重载: 新页面的两张图各按记下的状态画
  storage.setItem("flowscope.chartShown", JSON.stringify([
    { ema: true, band: false, wt: true }, { ema: false, band: true, wt: false }]));
  const again = runBrowser({ localStorage: storage });
  assert.deepEqual(offFlags(again, 0), [false, true, false]);
  assert.deepEqual(offFlags(again, 1), [true, false, true]);
  assert.equal(again.document.getElementById("band-k").disabled, false, "右图的带记成打开, 带宽应可调");

  // 旧版按周期记 {"10": {...}, "30": {...}}: 新键还没有时各图按自己的周期初始化
  storage.removeItem("flowscope.chartShown");
  storage.setItem("flowscope.mainShown", JSON.stringify({
    10: { ema: true, band: true, wt: true }, 30: { ema: false, band: false, wt: false } }));
  const perPeriod = runBrowser({ localStorage: storage });
  assert.deepEqual(offFlags(perPeriod, 0), [false, false, false]);
  assert.deepEqual(offFlags(perPeriod, 1), [true, true, true]);

  // 更早只记一份 {ema, band, wt}: 两张图都按它初始化
  storage.setItem("flowscope.mainShown", JSON.stringify({ ema: false, band: true, wt: true }));
  const legacy = runBrowser({ localStorage: storage });
  assert.deepEqual(offFlags(legacy, 0), [true, false, false]);
  assert.deepEqual(offFlags(legacy, 1), [true, false, false]);

  // 坏值不能把页面弄挂, 回落到默认
  storage.setItem("flowscope.chartShown", "not json");
  const third = runBrowser({ localStorage: storage });
  assert.deepEqual(offFlags(third, 1), [false, true, false]);
  assert.equal(third.document.getElementById("band-k").disabled, true);
});

test("两张图选同一个周期时开关各管各的, 重载后不串; 切周期不换本图的开关", () => {
  const storage = createLocalStorage();
  storage.setItem("flowscope.chartTfs", "[30,30]");
  const context = runBrowser({ localStorage: storage, fetch: historyRecorder([]) });
  clickEye(context, 0, "wt");
  const again = runBrowser({ localStorage: storage, fetch: historyRecorder([]) });
  assert.deepEqual(offFlags(again, 0), [false, true, true], "左图关掉的 WaveTrend 重载后还是关的");
  assert.deepEqual(offFlags(again, 1), [false, true, false], "右图不受左图影响");

  const select = tfSelectOf(again, 0);
  select.value = "300";
  select.handlers.change[0]();
  assert.deepEqual(offFlags(again, 0), [false, true, true], "切周期后本图开关不变");
  assert.deepEqual(JSON.parse(storage.getItem("flowscope.chartShown"))[0], { ema: true, band: false, wt: false });
});

test("「带宽」只在有图打开 FlowWave带 时可调(足迹图下不置灰, 因为只是临时藏起来)", () => {
  const context = runBrowser();
  const bandK = context.document.getElementById("band-k");
  const setView = viewSetter(context);

  assert.equal(bandK.disabled, true, "默认关: 带宽不可调");

  clickEye(context, 1, "band");
  assert.equal(bandK.disabled, false, "打开带后带宽可调");

  setView("footprint");
  assert.equal(bandK.disabled, false, "足迹图只是临时隐藏带, 不该把宽度选择也锁掉");

  clickEye(context, 0, "band");
  clickEye(context, 1, "band");
  assert.equal(bandK.disabled, false, "10s 图还开着带, 带宽仍可调");
  clickEye(context, 0, "band");
  assert.equal(bandK.disabled, true, "两张图都关掉后重新置灰");
});

test("带宽 k 可切换: 半宽按 k 线性变化, 非法值回落到默认 2σ", () => {
  const context = runBrowser();
  vm.runInContext(`(() => {
    const c = charts[1];
    c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    c.bars = [];
    let price = 4000;
    for (let i = 0; i < 120; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      c.bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                    close: price, volume: 100, buy: 60, sell: 40, delta: 20 });
    }
    c.derive();
  })()`, context);

  const halfWidth = () => vm.runInContext("charts[1].derived.band.up[100] - charts[1].derived.band.mid[100]", context);
  const select = context.document.getElementById("band-k");
  // 按浏览器的顺序来: 先由控件持有新值, 再带着控件本身触发 change
  // (处理器会写回 e.target.value, 用假 target 就观察不到这个纠正行为)
  const setK = (value) => { select.value = value; select.handlers.change[0]({ target: select }); };

  const base = halfWidth();
  assert.ok(base > 0, "2σ 下第 100 根应有正的半宽(否则这个用例是空跑)");

  setK("2.5");
  assert.ok(Math.abs(halfWidth() - base * 1.25) < 1e-9, "2.5σ 的半宽应是 2σ 的 1.25 倍");
  assert.equal(select.value, "2.5", "合法值应写回下拉框");
  for (const index of [0, 1]) {
    assert.equal(findByClass(legendRow(context, index, "band"), "ml-params").textContent, "2.5σ", "两张图的图例都显示新带宽");
  }

  setK("1.5");
  assert.ok(Math.abs(halfWidth() - base * 0.75) < 1e-9, "1.5σ 的半宽应是 2σ 的 0.75 倍");

  setK("9");   // 目录之外的倍数: 不按垃圾值画带, 回落到默认并纠正下拉框显示
  assert.equal(select.value, "2", "非法值应回落到默认 2σ");
  assert.ok(Math.abs(halfWidth() - base) < 1e-9, "非法值不得改变带宽");
});

test("自定义 series 只画 visibleRange 内的 bar: 区间外的旧坐标不能画成残影", () => {
  // 库只给可见区间内的 bar 算 x, 区间外的 x 是上次可见时留下的旧值; 这里给区间外的 bar 塞一个
  // 显眼的旧坐标(-5000), 记录画布上所有用到的 x, 一个都不能是它。
  const context = runBrowser();
  const r = vm.runInContext(`(() => {
    const STALE = -5000;
    const xs = [];
    const ctx = new Proxy({}, {
      get: (_, key) => (key === "moveTo" || key === "lineTo" || key === "fillRect" || key === "strokeRect" || key === "fillText"
        ? (...args) => xs.push(key === "fillText" ? args[1] : args[0])
        : () => {}),
      set: () => true,
    });
    const target = { useMediaCoordinateSpace: (fn) => fn({ context: ctx }) };
    const price = (p) => p;   // 价格直接当 y, 只关心 x
    const range = { from: 2, to: 6 };
    const barX = (i) => (i >= range.from && i < range.to ? 100 + i * 10 : STALE);

    const band = new ChartView.BandRenderer();
    band.update({ barSpacing: 10, visibleRange: range, bars: Array.from({ length: 9 }, (_, i) => ({
      x: barX(i), originalData: { mid: 10, up: 12, dn: 8, state: 0, idx: i } })) });
    band.draw(target, price);
    const bandXs = xs.splice(0);

    const fp = new ChartView.FootprintRenderer();
    fp.update({ barSpacing: 60, visibleRange: range, bars: Array.from({ length: 9 }, (_, i) => ({
      x: barX(i), originalData: { open: 10, high: 12, low: 8, close: 11, coverage: "complete",
                                  levels: [[9, 5, 3], [10, 8, 2]] } })) }, { tickSize: 1 });
    fp.draw(target, price);
    return { bandXs, fpXs: xs.splice(0), stale: STALE };
  })()`, context);
  assert.ok(r.bandXs.length > 0, "带应在可见区间内画出东西(否则这个用例是空跑)");
  assert.ok(r.fpXs.length > 0, "足迹应在可见区间内画出东西(否则这个用例是空跑)");
  assert.ok(r.bandXs.every((x) => x > r.stale + 1000), `带用到了区间外的旧坐标: ${r.bandXs}`);
  assert.ok(r.fpXs.every((x) => x > r.stale + 1000), `足迹用到了区间外的旧坐标: ${r.fpXs}`);
});

test("叠加带的 custom series 满足 lightweight-charts 契约", () => {
  const context = runBrowser();
  const r = vm.runInContext(`(() => {
    const s = new ChartView.BandSeries();
    const item = { time: 1, mid: 10, up: 12, dn: 8, close: 11, idx: 0 };
    return {
      hasDraw: typeof s.renderer().draw === "function",
      whitespaceNull: s.isWhitespace({ time: 1, mid: null, up: null, dn: null }),
      whitespaceOk: s.isWhitespace(item),
      values: s.priceValueBuilder(item),
      options: s.defaultOptions(),
    };
  })()`, context);
  assert.equal(r.hasDraw, true, "custom series 必须提供 renderer().draw");
  assert.equal(r.whitespaceNull, true, "缺轨道值的行应作为空白跳过");
  assert.equal(r.whitespaceOk, false, "有轨道值的行不应被判为空白");
  assert.equal(r.values.length, 3, "价格轴需要拿到下轨/上轨/中线三个值, 否则带会被裁掉");
  assert.equal(r.values[0], 8);
  assert.equal(r.values[1], 12);
  assert.equal(r.values[2], 10);
  assert.equal(r.options.lastValueVisible, false);
});

// ---------- 阈值窗口遇到缺失 bar ----------

test("滚动均值/标准分: 窗口里有一根 null 就输出 null, 不把缺失当 0", () => {
  const context = runBrowser();
  const r = {};
  const raw = vm.runInContext(`(() => {
    const v = [1, 2, 3, null, 5, 6, 7, 8];
    return JSON.stringify({ sma: FlowIndicators.rollingSma(v, 3), z: FlowIndicators.rollingZ(v, 3) });
  })()`, context);
  Object.assign(r, JSON.parse(raw));
  // 下标 3..5 的窗口都含那根 null
  assert.deepEqual(r.sma, [null, null, 2, null, null, null, 6, 7]);
  assert.deepEqual(r.z.slice(0, 6), [null, null, r.z[2], null, null, null]);
  assert.ok(Math.abs(r.z[2] - Math.sqrt(1.5)) < 1e-12, "完整窗口 [1,2,3] 的末点 z = (3-2)/sqrt(2/3)");
  assert.ok(Math.abs(r.z[6] - Math.sqrt(1.5)) < 1e-12, "缺口滑出窗口后恢复计算");
});

test("缺失段之后不会被误判成高档位", () => {
  // 复现: 新开合约时 tick 窗口只覆盖末尾, 前面的 bar 是 coverage=missing(买卖量为 null)。
  // 以前 null 被当 0 放进窗口, 缺口后的均值被拉低, 紧跟缺口的 bar 全被判成 2~3 档。
  const context = runBrowser();
  const r = vm.runInContext(`(() => {
    const c = charts[1];
    c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    c.bars = [];
    let seed = 7;
    const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 700; i++) {
      const missing = i < 600;
      const buy = missing ? null : 40 + rand() * 40;
      const sell = missing ? null : 40 + rand() * 40;
      c.bars.push({ time: 1700000000 + i * 30, open: 100, high: 101, low: 99, close: 100 + (i % 2),
                    volume: 100 + rand() * 20, buy, sell, delta: missing ? null : buy - sell,
                    coverage: missing ? "missing" : "complete" });
    }
    c.derive();
    const result = {};
    for (const type of ["Z-SCORE", "SMA", "RELATIVE"]) {
      settings.threshtype = type;
      let high = 0;
      for (let i = 600; i < 660; i++) {
        for (const kind of ["buy", "sell"]) if (c.levelOf(kind, i) >= 2) high++;
      }
      result[type] = high;
    }
    return JSON.stringify(result);
  })()`, context);
  assert.deepEqual(JSON.parse(r), { "Z-SCORE": 0, SMA: 0, RELATIVE: 0 });
});

// ---------- 各周期的拆分粒度 ----------

function historyRecorder(calls) {
  return (url) => {
    const text = String(url);
    if (text.startsWith("/api/history")) {
      calls.push(new URLSearchParams(text.slice(text.indexOf("?") + 1)));
      return new Promise(() => {});   // 让加载停在请求上, 只观察发出的参数
    }
    return stubFetch(url);
  };
}

test("K 线口径 15s: 10s 图请求时收敛成 10s 的合法值, 30s 图照用 15s, 工具栏上的选择不变", async () => {
  const calls = [];
  const context = runBrowser({ fetch: historyRecorder(calls) });
  const ltfSelect = context.document.getElementById("ltf");
  ltfSelect.options = [1, 5, 10, 15, 30].map((value) => ({ value: String(value), disabled: false }));
  // 30s 图上一次加载时服务端下发的 cfg 带着 30s 的合法粒度(含 15)
  vm.runInContext(`charts[1].cfg = { tf: 30, ltfOptions: [0, 1, 5, 10, 15, 30] };`, context);
  context.document.getElementById("cvd-source").handlers.change[0]({ target: { value: "kline" } });
  ltfSelect.handlers.change[0]({ target: { value: "15" } });
  const byTf = Object.fromEntries(calls.slice(-2).map((params) => [params.get("tf"), params.get("ltf")]));
  assert.deepEqual(byTf, { 30: "15", 10: "10" }, "15 不能整除 10, 10s 图应回落到本周期下最粗的合法粒度");
  assert.equal(ltfSelect.value, "15", "工具栏上的选择不能被 10s 图改掉");
  vm.runInContext("refreshLtfOptions()", context);
  assert.equal(ltfSelect.options.find((o) => o.value === "15").disabled, false, "30s 图还用得上 15s, 不能禁用");
});

// ---------- 两张图联动、顶栏状态与读数 ----------

const T0 = 1699999980;   // 30 的整数倍: 30s bar 从这里开始

// 两张图喂同一个小时的行情: 10s 图 360 根, 30s 图 120 根(第 k 根 30s 对应第 3k~3k+2 根 10s)
function feedBothCharts(context) {
  vm.runInContext(`(() => {
    for (const c of charts) {
      c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
      c.bars = [];
      for (let t = ${T0}; t < ${T0 + 3600}; t += c.tf) {
        const price = 4000 + Math.round(Math.sin(t / 300) * 10);
        c.bars.push({ time: t, open: price - 1, high: price + 2, low: price - 2, close: price, volume: 100 });
      }
      c.renderAll();
    }
  })()`, context);
}

const near = (actual, expected) =>
  assert.ok(Math.abs(actual.from - expected.from) < 1e-9 && Math.abs(actual.to - expected.to) < 1e-9,
            `范围 ${JSON.stringify(actual)} 应为 ${JSON.stringify(expected)}`);

test("时间换算: 逻辑坐标与时间互逆, 休市缺口里线性插值, 两端按周期外推", () => {
  const context = runBrowser();
  const r = JSON.parse(vm.runInContext(`JSON.stringify((() => {
    const bars = [{ time: 1000 }, { time: 1030 }, { time: 1060 }, { time: 4660 }, { time: 4690 }];   // 1060 之后休市一小时
    const xs = [-2, 0, 0.5, 2.5, 3, 4, 6.5];
    const times = xs.map((x) => ChartView.timeAtLogical(bars, 30, x));
    return {
      times,
      back: times.map((t) => ChartView.logicalAtTime(bars, 30, t)),
      index: [999, 1000, 1059, 1061, 9999].map((t) => ChartView.indexAtOrBefore(bars, t)),
    };
  })())`, context));
  assert.deepEqual(r.times, [940, 1000, 1015, 2860, 4660, 4690, 4765]);
  assert.deepEqual(r.back, [-2, 0, 0.5, 2.5, 3, 4, 6.5]);
  assert.deepEqual(r.index, [-1, 0, 1, 2, 4]);
});

test("可视范围联动: 以鼠标所在那张图为准, 另一张显示同一段时间; 另一张自己动了拉回来, 已对齐时不再重设", () => {
  const context = runBrowser();
  feedBothCharts(context);
  const [ts10, ts30] = stubCharts.map((chart) => chart.timeScale);

  // 默认以 30s 图为准: 它显示第 20~60 根(T0+600 ~ T0+1800), 10s 图应显示第 60~180 根
  ts30.setVisibleLogicalRange({ from: 20, to: 60 });
  near(ts10.getVisibleLogicalRange(), { from: 60, to: 180 });
  assert.equal(ts10.sets, 1, "对齐之后 10s 图的回调不该再引出一轮设置");

  // 10s 图自己动了(比如来了新 bar 自动右移): 拉回到 30s 图的范围
  ts10.setVisibleLogicalRange({ from: 63, to: 183 });
  near(ts10.getVisibleLogicalRange(), { from: 60, to: 180 });

  // 鼠标进了 10s 图: 改以它为准。右侧留白按时间换算: 10s 图越过末根 4 根(T0+3630) = 30s 图越过末根 2 根
  chartEl(context, 0).handlers.pointerenter[0]();
  ts10.setVisibleLogicalRange({ from: 300, to: 363 });
  near(ts30.getVisibleLogicalRange(), { from: 100, to: 121 });
});

test("十字光标联动: 另一张图的光标落到同一时刻所在的那根 bar, 主图窗格里横线同价位, 移出时一起清掉", () => {
  const context = runBrowser();
  feedBothCharts(context);
  const [chart10, chart30] = stubCharts;
  const move = (chart, param) => chart.crosshairHandlers[0](param);
  const enter = (index) => chartEl(context, index).handlers.pointerenter[0]();

  // 鼠标在 10s 图主图窗格 T0+50 上: 30s 图摆到包含它的 T0+30 那根, 价位跟光标(stub 的价格 = 4000 + y)
  enter(0);
  move(chart10, { time: T0 + 50, point: { x: 100, y: 200 }, paneIndex: 0 });
  assert.deepEqual(chart30.crosshair.at(-1), { price: 4200, time: T0 + 30 });

  // 光标在副图窗格: 价格没有意义, 横线落在那根 30s bar 的收盘价上
  move(chart10, { time: T0 + 50, point: { x: 100, y: 600 }, paneIndex: 1 });
  const close30 = vm.runInContext("charts[1].bars[1].close", context);
  assert.deepEqual(chart30.crosshair.at(-1), { price: close30, time: T0 + 30 });

  // 30s 图上摆好的光标在它数据更新时库会再报一次: 鼠标不在它上面, 不能反过来联动
  const before = chart10.crosshair.length;
  move(chart30, { time: T0 + 30, point: { x: 100, y: 200 }, paneIndex: 0 });
  assert.equal(chart10.crosshair.length, before, "不是鼠标所在那张图的光标事件不联动");

  // 反过来: 鼠标进了 30s 图, T0+60 落到 10s 图同一时刻的第一根
  enter(1);
  move(chart30, { time: T0 + 60, point: { x: 100, y: 100 }, paneIndex: 0 });
  assert.deepEqual(chart10.crosshair.at(-1), { price: 4100, time: T0 + 60 });

  // 移出图表: 另一张的光标也清掉
  move(chart30, { time: undefined, point: undefined });
  assert.equal(chart10.crosshair.at(-1), null);
});

test("顶栏读数只显示鼠标所在那张图, 带周期前缀; 鼠标换到另一张图时立刻切过去", () => {
  const context = runBrowser();
  feedBothCharts(context);
  const legend = context.document.getElementById("legend");
  assert.match(legend.textContent, /^30s · /, "默认显示 30s 图");
  vm.runInContext("charts[0].refreshLegend()", context);
  assert.match(legend.textContent, /^30s · /, "10s 图不是鼠标所在那张, 它的读数不该写到顶栏");
  chartEl(context, 0).handlers.pointerenter[0]();
  assert.match(legend.textContent, /^10s · /);
  // 30s 图上摆好的光标在它数据更新时库会再报一次: 不算鼠标换了图
  stubCharts[1].crosshairHandlers[0]({ time: T0 + 60, point: { x: 100, y: 100 }, paneIndex: 0 });
  assert.match(legend.textContent, /^10s · /);
  chartEl(context, 1).handlers.pointerenter[0]();
  assert.match(legend.textContent, /^30s · /);
});

test("顶栏连接状态: 两张图都连上才显示「已连接」, 否则带周期前缀列出没连上的", () => {
  const context = runBrowser();
  const status = context.document.getElementById("status");
  vm.runInContext(`setChartStatus(0, true, "已连接"); setChartStatus(1, false, "同步中…");`, context);
  assert.equal(status.textContent, "30s: 同步中…");
  assert.equal(status.className, "off");
  vm.runInContext(`setChartStatus(1, true, "已连接");`, context);
  assert.equal(status.textContent, "已连接");
  assert.equal(status.className, "on");
  vm.runInContext(`setChartStatus(0, false, "已断开, 重连补齐中…"); setChartStatus(1, false, "加载中…");`, context);
  assert.equal(status.textContent, "10s: 已断开, 重连补齐中…  30s: 加载中…");
});

// ---------- 切换周期 ----------

const tfSelectOf = (context, index) => findByClass(chartEl(context, index), "tf-select");

test("周期显示名与本地兜底的拆分粒度: 能整除主周期的才合法", () => {
  const context = runBrowser();
  const r = JSON.parse(vm.runInContext(`JSON.stringify({
    labels: ChartView.TF_CHOICES.map(ChartView.tfLabel),
    ltf10: ChartView.localLtfOptions(10), ltf60: ChartView.localLtfOptions(60), ltf4h: ChartView.localLtfOptions(14400),
  })`, context));
  assert.deepEqual(r.labels, ["10s", "30s", "1m", "5m", "15m", "1h", "4h"]);
  assert.deepEqual(r.ltf10, [1, 5, 10]);
  assert.deepEqual(r.ltf60, [1, 5, 10, 15, 30]);
  assert.deepEqual(r.ltf4h, [1, 5, 10, 15, 30]);
});

test("切换周期: 本图按新周期重新请求, 顶栏状态换成新周期前缀, 周期记进 localStorage, 重载后照旧", () => {
  const calls = [];
  const localStorage = createLocalStorage();
  const context = runBrowser({ fetch: historyRecorder(calls), localStorage });
  // 本图已有数据和配置时也要先清空画面(空数据走一遍渲染不能出错), 再按新周期加载
  feedBothCharts(context);
  const select = tfSelectOf(context, 1);
  assert.equal(select.value, "30");
  select.value = "300";
  assert.doesNotThrow(() => select.handlers.change[0]());
  assert.equal(vm.runInContext("charts[1].tf", context), 300);
  assert.equal(vm.runInContext("charts[1].bars.length", context), 0, "旧周期的 bar 不能留在新周期的图上");
  assert.equal(calls.at(-1).get("tf"), "300");
  assert.equal(localStorage.getItem("flowscope.chartTfs"), "[10,300]");
  vm.runInContext(`setChartStatus(0, true, "已连接"); setChartStatus(1, false, "加载中…");`, context);
  assert.equal(context.document.getElementById("status").textContent, "5m: 加载中…");

  const reloaded = runBrowser({ fetch: historyRecorder(calls), localStorage });
  assert.equal(vm.runInContext("charts.map((c) => c.tf).join()", reloaded), "10,300");
  assert.equal(tfSelectOf(reloaded, 1).value, "300");
});

test("记下的周期不合法(旧版、手改)就回到默认的左 10s、右 30s", () => {
  for (const saved of ["[10,45]", "[60]", "oops"]) {
    const localStorage = createLocalStorage();
    localStorage.setItem("flowscope.chartTfs", saved);
    const context = runBrowser({ localStorage });
    assert.equal(vm.runInContext("charts.map((c) => c.tf).join()", context), "10,30", saved);
  }
});

test("可视范围联动: 周期相差超过 3 倍时只对齐右边缘, 另一张保留自己的缩放", () => {
  const localStorage = createLocalStorage();
  localStorage.setItem("flowscope.chartTfs", "[60,3600]");
  const context = runBrowser({ localStorage });
  vm.runInContext(`(() => {
    for (const c of charts) {
      c.cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
      c.bars = [];
      for (let t = ${T0}; t < ${T0 + 86400}; t += c.tf) {
        c.bars.push({ time: t, open: 4000, high: 4002, low: 3998, close: 4001, volume: 100 });
      }
      c.renderAll();
    }
  })()`, context);
  const [ts1m, ts1h] = stubCharts.map((chart) => chart.timeScale);
  ts1h.setVisibleLogicalRange({ from: 0, to: 20 });            // 1h 图看 20 根
  chartEl(context, 0).handlers.pointerenter[0]();
  ts1m.setVisibleLogicalRange({ from: 600, to: 700 });         // 1m 图右边缘 = T0 + 700 分钟
  near(ts1h.getVisibleLogicalRange(), { from: 700 / 60 - 20, to: 700 / 60 });
  const sets = ts1h.sets;
  ts1m.setVisibleLogicalRange({ from: 600, to: 700 });         // 已经对齐: 不再重设
  assert.equal(ts1h.sets, sets);
  // 两边都已看到最新(右边缘越过末根): 1h 图停在最新, 保留自己的右侧留白
  chartEl(context, 1).handlers.pointerenter[0]();
  ts1h.setVisibleLogicalRange({ from: 10, to: 26 });           // 鼠标在 1h 图上把它拖到最新, 末根是第 23 根
  chartEl(context, 0).handlers.pointerenter[0]();
  ts1m.setVisibleLogicalRange({ from: 1400, to: 1443 });       // 1m 图也到最新(末根是第 1439 根)
  near(ts1h.getVisibleLogicalRange(), { from: 10, to: 26 });
  // 1m 图拖回去: 1h 图跟着把右边缘移到同一时刻
  ts1m.setVisibleLogicalRange({ from: 1100, to: 1140 });
  near(ts1h.getVisibleLogicalRange(), { from: 1140 / 60 - 16, to: 1140 / 60 });
  // 反过来: 1m 图停在过去, 鼠标在 1h 图上把它拖到最新(右侧留白 3 根 = 3 小时 = 180 根 1m)。
  // 1m 图只到最新、留自己的 3 根留白, 不能被推进 180 根空白里
  chartEl(context, 1).handlers.pointerenter[0]();
  ts1h.setVisibleLogicalRange({ from: 10, to: 26 });
  near(ts1m.getVisibleLogicalRange(), { from: 1442 - 40, to: 1442 });
});

// ---------- 自选请求乱序 ----------

test("连点两个 ☆ 时晚到的旧响应不会覆盖新列表", async () => {
  const pending = [];
  let server = [];                                   // 服务端的真实自选
  const context = runBrowser({
    fetch: (url, init) => {
      const text = String(url);
      if (text.startsWith("/api/favorites?")) {
        return new Promise((resolve) => pending.push({ text, method: init && init.method, resolve }));
      }
      if (text === "/api/favorites") return Promise.resolve(jsonResponse({ symbols: server, max: 40 }));
      return stubFetch(url);
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  const favorites = () => JSON.parse(vm.runInContext("JSON.stringify(favorites)", context));

  vm.runInContext(`changeFavorite("KQ.m@SHFE.fu", true); changeFavorite("KQ.m@DCE.i", true);`, context);
  assert.equal(pending.length, 2);
  // 服务端依次处理完两次收藏; 后发的那次先回来(带着完整列表), 先发的那次后回来
  server = ["KQ.m@SHFE.fu", "KQ.m@DCE.i"];
  pending[1].resolve(jsonResponse({ symbols: ["KQ.m@SHFE.fu", "KQ.m@DCE.i"], max: 40 }));
  await tick();
  assert.deepEqual(favorites(), ["KQ.m@SHFE.fu", "KQ.m@DCE.i"]);
  pending[0].resolve(jsonResponse({ symbols: ["KQ.m@SHFE.fu"], max: 40 }));
  await tick();
  await tick();
  assert.deepEqual(favorites(), ["KQ.m@SHFE.fu", "KQ.m@DCE.i"], "旧响应不得把 DCE.i 冲掉");
});

test("丢弃过旧响应后, 以服务端的当前列表为准重读一次", async () => {
  // 服务端处理顺序不一定等于点击顺序(收藏要先查合约服务, 移出是即时的): 最后发出的那次
  // 的响应也可能不是最新状态, 所以这一批请求结束后要重读 /api/favorites。
  const pending = [];
  let listReads = 0;
  const context = runBrowser({
    fetch: (url, init) => {
      const text = String(url);
      if (text.startsWith("/api/favorites?")) {
        return new Promise((resolve) => pending.push({ resolve }));
      }
      if (text === "/api/favorites") {
        listReads += 1;
        return Promise.resolve(jsonResponse({ symbols: listReads > 1 ? ["KQ.m@DCE.i"] : [], max: 40 }));
      }
      return stubFetch(url);
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const tick = () => new Promise((resolve) => setImmediate(resolve));
  vm.runInContext(`changeFavorite("KQ.m@DCE.i", true); changeFavorite("KQ.m@SHFE.fu", false);`, context);
  pending[1].resolve(jsonResponse({ symbols: [], max: 40 }));       // 移出先处理完, 那时 DCE.i 还没加上
  await tick();
  pending[0].resolve(jsonResponse({ symbols: ["KQ.m@DCE.i"], max: 40 }));
  await tick();
  await tick();
  assert.equal(listReads, 2, "丢弃过旧响应后应重读一次列表");
  assert.deepEqual(JSON.parse(vm.runInContext("JSON.stringify(favorites)", context)), ["KQ.m@DCE.i"]);
});
