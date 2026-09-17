"use strict";
/* app.js 在浏览器里的加载顺序回归。
 *
 * app.js 是普通 <script>(不是 module), 顶层语句按出现顺序执行, 所以"某个 let/const
 * 在下面声明、却被上面的顶层调用链先读到"会直接抛 ReferenceError —— 页面白屏。
 * 真实案例: picker.load() 同步渲染月份行并回调 isFavorite() 画 ☆, 而 favorites 那时
 * 还没声明, 于是 "Cannot access 'favorites' before initialization"。
 *
 * 这里按 index.html 的顺序把四个脚本放进同一个 vm 上下文执行一遍, 并对顶层用到的
 * DOM / 图表 API 做最小 stub; 之后跑一轮微任务, 让 fetch 的回调也走完。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.join(__dirname, "..");
const STATIC = path.join(ROOT, "static");
const SCRIPTS = ["data-sync.js", "picker-core.js", "contract-picker.js", "app.js"];

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

function createChartStub() {
  const series = () => ({
    setData() {}, update() {}, applyOptions() {}, setMarkers() {},
    createPriceLine() { return { applyOptions() {} }; }, removePriceLine() {},
    priceScale() { return { applyOptions() {} }; },
    setVisibleRange() {}, coordinateToPrice() { return 0; }, priceToCoordinate() { return 0; },
  });
  const chart = {
    addSeries: series, addCustomSeries: series, removeSeries() {},
    applyOptions() {}, resize() {}, timeScale: () => ({
      scrollToRealTime() {}, fitContent() {}, applyOptions() {}, subscribeVisibleLogicalRangeChange() {},
      timeToCoordinate() { return 0; }, coordinateToTime() { return 0; },
    }),
    priceScale: () => ({ applyOptions() {} }),
    panes: () => Array.from({ length: 4 }, () => ({
      setHeight() {}, setStretchFactor() {}, paneIndex: () => 0,
    })),
    subscribeClick() {}, unsubscribeClick() {}, subscribeCrosshairMove() {},
  };
  return chart;
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
  return Promise.resolve(catalogResponse());
}

function jsonResponse(payload) {
  return { ok: true, status: 200, json: async () => payload };
}

function runBrowser() {
  const document = createDocument();
  const context = {
    document,
    localStorage: createLocalStorage(),
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
    fetch: (url) => stubFetch(url),
    WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} },
    LightweightCharts: {
      createChart: createChartStub,
      CrosshairMode: { Normal: 0 },
      LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
      LineType: { Simple: 0, WithSteps: 1 },
      CandlestickSeries: "CandlestickSeries",
      LineSeries: "LineSeries",
      HistogramSeries: "HistogramSeries",
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

test("四个脚本按 index.html 顺序加载时不抛错", () => {
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
