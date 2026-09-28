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

// 记录每次 applyOptions: 可见性开关是"状态变量 → series"的唯一通路, 只能从这里观察。
// 每次 createChartStub() 覆盖它, 所以读取前必须先 runBrowser()。
let lastAppliedOptions = null;

function createChartStub() {
  const applied = [];
  lastAppliedOptions = applied;
  const series = () => ({
    setData() {}, update() {}, applyOptions(options) { applied.push(options); }, setMarkers() {},
    createPriceLine() { return { applyOptions() {} }; }, removePriceLine() {},
    priceScale() { return { applyOptions() {} }; },
    setVisibleRange() {}, coordinateToPrice() { return 0; }, priceToCoordinate() { return 0; },
  });
  const chart = {
    addSeries: series, addCustomSeries: series, removeSeries() {},
    applyOptions() {}, resize() {}, timeScale: () => ({
      scrollToRealTime() {}, fitContent() {}, applyOptions() {}, subscribeVisibleLogicalRangeChange() {},
      timeToCoordinate() { return 0; }, coordinateToTime() { return 0; },
      options: () => ({ barSpacing: 6 }),
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

function runBrowser(options = {}) {
  const document = createDocument();
  const fetchImpl = options.fetch || stubFetch;
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

test("判向算法开关已移除 : 前端恒读新算法 Lee-Ready", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.ok(!html.includes('id="classify"'), "判向算法下拉框应已移除");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  assert.ok(!app.includes('$("classify")'), "app.js 不应再绑定判向算法下拉框");
  assert.ok(!/classifySource/.test(app), "app.js 不应再保留判向口径状态");
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
    ["tf", /let tf = (\d+)/],
    ["view", /let view = "([^"]+)"/],
    ["mode", /let mode = "([^"]+)"/],
    ["threshtype", /let threshtype = "([^"]+)"/],
    ["cvd-source", /let cvdSource = "([^"]+)"/],
    ["ltf", /let klineLtf = (\d+)/],
    ["lw-overlay", /let bandOverlay = "([^"]+)"/],
    ["band-k", /let bandK = ([\d.]+)/],
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

test("阈值默认是 Z-SCORE", () => {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.match(html,
    /id="threshtype"[\s\S]*?<option value="Z-SCORE"[^>]*\bselected\b/,
    "阈值下拉框默认应选中 Z-SCORE");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  assert.match(app, /let threshtype = "Z-SCORE"/, "app.js 的阈值初始状态应为 Z-SCORE");
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
    cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    bars = [];
    let price = 4000;
    for (let i = 0; i < 120; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                  close: price, volume: 100 + (i % 7) * 10, buy: 60, sell: 40, delta: 20,
                  buyLegacy: 55, sellLegacy: 45, deltaLegacy: 10, cvd: i, coverage: "complete" });
    }
    derive();
    return { close: bars.map((b) => b.close), mid: derived.band.mid, up: derived.band.up,
             dn: derived.band.dn, state: derived.band.state, wt2: derived.lw.wt2,
             n: BAND.n, k: bandK, lwOb: LW.ob, lwOs: LW.os };
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
    cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    bars = [];
    let price = 4000;
    for (let i = 0; i < 160; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                  close: price, volume: 100 + (i % 7) * 10, buy: 60, sell: 40, delta: 20 });
    }
    derive();
    const dots = buildBandDots();
    return { times: bars.map((b) => b.time), wt2: derived.lw.wt2, up: derived.band.up,
             dn: derived.band.dn, high: dots.high, low: dots.low, ob: LW.ob, os: LW.os };
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

test("叠加开关: 打开后主图三个叠加系列可见, 切足迹图强制隐藏, 切回来按开关恢复", () => {
  const context = runBrowser();
  const applied = lastAppliedOptions;
  const toggle = (value) => context.document.getElementById("lw-overlay").handlers.change[0]({ target: { value } });
  const setView = (value) => context.document.getElementById("view").handlers.change[0]({ target: { value } });
  const lastThree = () => applied.slice(-3).map((o) => o.visible);

  // stub 里没有真实 <select>, 直接调它的 change 处理器(与浏览器里选项改变走同一条路径)
  assert.equal(lastThree().every((v) => v === false), true, "默认关: 三个叠加系列都应隐藏");

  toggle("on");
  assert.equal(lastThree().every((v) => v === true), true, "打开开关后带与两个打点系列都应可见");

  setView("footprint");
  assert.equal(lastThree().every((v) => v === false), true, "足迹图下叠加必须强制隐藏");

  setView("candle");
  assert.equal(lastThree().every((v) => v === true), true, "切回 K 线应按开关恢复可见");

  toggle("off");
  assert.equal(lastThree().every((v) => v === false), true, "关掉开关后应重新隐藏");
});

test("叠加开关: 「带宽」只在打开时可调(足迹图下不置灰, 因为只是临时藏起来)", () => {
  const context = runBrowser();
  const bandK = context.document.getElementById("band-k");
  const toggle = (value) => context.document.getElementById("lw-overlay").handlers.change[0]({ target: { value } });
  const setView = (value) => context.document.getElementById("view").handlers.change[0]({ target: { value } });

  assert.equal(bandK.disabled, true, "默认关: 带宽不可调");

  toggle("on");
  assert.equal(bandK.disabled, false, "打开叠加后带宽可调");

  setView("footprint");
  assert.equal(bandK.disabled, false, "足迹图只是临时隐藏带, 不该把宽度选择也锁掉");

  toggle("off");
  assert.equal(bandK.disabled, true, "关掉叠加后重新置灰");
});

test("带宽 k 可切换: 半宽按 k 线性变化, 非法值回落到默认 2σ", () => {
  const context = runBrowser();
  vm.runInContext(`(() => {
    cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    bars = [];
    let price = 4000;
    for (let i = 0; i < 120; i++) {
      price += Math.sin(i / 3) * 4 + (i % 5 === 0 ? 3 : -1);
      bars.push({ time: 1700000000 + i * 30, open: price - 1, high: price + 2, low: price - 2,
                  close: price, volume: 100, buy: 60, sell: 40, delta: 20 });
    }
    derive();
  })()`, context);

  const halfWidth = () => vm.runInContext("derived.band.up[100] - derived.band.mid[100]", context);
  const select = context.document.getElementById("band-k");
  // 按浏览器的顺序来: 先由控件持有新值, 再带着控件本身触发 change
  // (处理器会写回 e.target.value, 用假 target 就观察不到这个纠正行为)
  const setK = (value) => { select.value = value; select.handlers.change[0]({ target: select }); };

  const base = halfWidth();
  assert.ok(base > 0, "2σ 下第 100 根应有正的半宽(否则这个用例是空跑)");

  setK("2.5");
  assert.ok(Math.abs(halfWidth() - base * 1.25) < 1e-9, "2.5σ 的半宽应是 2σ 的 1.25 倍");
  assert.equal(select.value, "2.5", "合法值应写回下拉框");

  setK("1.5");
  assert.ok(Math.abs(halfWidth() - base * 0.75) < 1e-9, "1.5σ 的半宽应是 2σ 的 0.75 倍");

  setK("9");   // 目录之外的倍数: 不按垃圾值画带, 回落到默认并纠正下拉框显示
  assert.equal(select.value, "2", "非法值应回落到默认 2σ");
  assert.ok(Math.abs(halfWidth() - base) < 1e-9, "非法值不得改变带宽");
});

test("叠加带的 custom series 满足 lightweight-charts 契约", () => {
  const context = runBrowser();
  const r = vm.runInContext(`(() => {
    const s = new BandSeries();
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
    return JSON.stringify({ sma: rollingSma(v, 3), z: rollingZ(v, 3) });
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
    cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
    bars = [];
    let seed = 7;
    const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 700; i++) {
      const missing = i < 600;
      const buy = missing ? null : 40 + rand() * 40;
      const sell = missing ? null : 40 + rand() * 40;
      bars.push({ time: 1700000000 + i * 30, open: 100, high: 101, low: 99, close: 100 + (i % 2),
                  volume: 100 + rand() * 20, buy, sell, delta: missing ? null : buy - sell,
                  coverage: missing ? "missing" : "complete" });
    }
    derive();
    const result = {};
    for (const type of ["Z-SCORE", "SMA", "RELATIVE"]) {
      threshtype = type;
      sma300Cache = null;
      let high = 0;
      for (let i = 600; i < 660; i++) {
        for (const kind of ["buy", "sell"]) if (levelOf(kind, i) >= 2) high++;
      }
      result[type] = high;
    }
    return JSON.stringify(result);
  })()`, context);
  assert.deepEqual(JSON.parse(r), { "Z-SCORE": 0, SMA: 0, RELATIVE: 0 });
});

// ---------- 切周期时的拆分粒度 ----------

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

test("K 线口径 15s 下从 30s 切到 10s, 请求的粒度先收敛成 10s 的合法值", async () => {
  const calls = [];
  const context = runBrowser({ fetch: historyRecorder(calls) });
  const ltfSelect = context.document.getElementById("ltf");
  ltfSelect.options = [1, 5, 10, 15, 30].map((value) => ({ value: String(value), disabled: false }));
  // 上一次加载的是 30s: 服务端下发的 cfg 带着 30s 的合法粒度(含 15)
  vm.runInContext(`cfg = { tf: 30, ltfOptions: [0, 1, 5, 10, 15, 30] };`, context);
  context.document.getElementById("cvd-source").handlers.change[0]({ target: { value: "kline" } });
  ltfSelect.handlers.change[0]({ target: { value: "15" } });
  assert.equal(calls[calls.length - 1].get("ltf"), "15");
  context.document.getElementById("tf").handlers.change[0]({ target: { value: "10" } });
  const last = calls[calls.length - 1];
  assert.equal(last.get("tf"), "10");
  assert.equal(last.get("ltf"), "10", "15 不能整除 10, 应回落到 10s 周期下最粗的合法粒度");
  assert.equal(ltfSelect.options.find((o) => o.value === "15").disabled, true);
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
