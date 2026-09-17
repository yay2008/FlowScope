"use strict";
/* 合约选择器组件的回归: 用一个最小 DOM stub 跑渲染、搜索、悬停预取、键盘与降级路径。 */
const test = require("node:test");
const assert = require("node:assert/strict");

const PickerCore = require("../static/picker-core.js");
const FlowData = require("../static/data-sync.js");
const ContractPicker = require("../static/contract-picker.js");

// ---------- 最小 DOM stub ----------

class StubClassList {
  constructor(element) {
    this.element = element;
    this.names = new Set();
  }
  add(...names) { names.forEach((name) => this.names.add(name)); this.sync(); }
  remove(...names) { names.forEach((name) => this.names.delete(name)); this.sync(); }
  contains(name) { return this.names.has(name); }
  toggle(name, on) {
    const want = on === undefined ? !this.names.has(name) : !!on;
    if (want) this.names.add(name); else this.names.delete(name);
    this.sync();
    return want;
  }
  sync() { this.element.className = [...this.names].join(" "); }
}

class StubElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this.classList = new StubClassList(this);
    this.attributes = {};
    this.handlers = {};
    this.hidden = false;
    this.value = "";
    this.focused = false;
  }
  set textContent(value) { this.own = value == null ? "" : String(value); this.children = []; }
  get textContent() {
    if (this.own) return this.own;
    return this.children.map((child) => child.textContent).join("");
  }
  append(...nodes) { nodes.forEach((node) => this.children.push(node)); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, handler) { (this.handlers[type] = this.handlers[type] || []).push(handler); }
  dispatch(type, event = {}) {
    (this.handlers[type] || []).forEach((handler) =>
      handler({ preventDefault() {}, stopPropagation() {}, ...event }));
  }
  focus() { this.focused = true; }
  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.contains && child.contains(node));
  }
}

function makeDocument() {
  return {
    handlers: {},
    createElement(tag) { return new StubElement(tag); },
    addEventListener(type, handler) { (this.handlers[type] = this.handlers[type] || []).push(handler); },
    dispatch(type, event = {}) { (this.handlers[type] || []).forEach((handler) => handler(event)); },
  };
}

function makeRoot(doc) {
  const wrapper = doc.createElement("div");
  const trigger = doc.createElement("button");
  const popup = doc.createElement("div");
  const search = doc.createElement("input");
  const products = doc.createElement("div");
  const months = doc.createElement("div");
  wrapper.append(trigger, popup);
  popup.append(search, products, months);
  return { wrapper, trigger, popup, search, products, months };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------- 夹具 ----------

const CATALOG = {
  source: "live", error: null, groups: [
    { exchangeId: "SHFE", exchangeName: "上期所", products: [
      { exchangeId: "SHFE", productId: "fu", name: "燃油", contSymbol: "KQ.m@SHFE.fu",
        mainSymbol: "SHFE.fu2611", openInterest: 428100 },
      { exchangeId: "SHFE", productId: "rb", name: "螺纹", contSymbol: "KQ.m@SHFE.rb",
        mainSymbol: "SHFE.rb2701", openInterest: 3144530 },
    ] },
    { exchangeId: "DCE", exchangeName: "大商所", products: [
      { exchangeId: "DCE", productId: "i", name: "铁矿石", contSymbol: "KQ.m@DCE.i",
        mainSymbol: "DCE.i2701", openInterest: 590347 },
    ] },
  ],
};

const MONTHS = {
  "SHFE.fu": { months: [
    { symbol: "SHFE.fu2611", name: "燃油2611", openInterest: 428100, isMain: true },
    { symbol: "SHFE.fu2701", name: "燃油2701", openInterest: 266684, isMain: false },
  ], monthsError: null },
  "SHFE.rb": { months: [
    { symbol: "SHFE.rb2701", name: "螺纹2701", openInterest: 3144530, isMain: true },
  ], monthsError: null },
  "DCE.i": { months: [
    { symbol: "DCE.i2701", name: "铁矿石2701", openInterest: 590347, isMain: true },
  ], monthsError: null },
};

function defaultRoutes() {
  return (url) => {
    if (url === "/api/symbols") return Promise.resolve(CATALOG);
    if (url.startsWith("/api/symbols?")) {
      const params = new URLSearchParams(url.slice(url.indexOf("?") + 1));
      const key = `${params.get("exchange")}.${params.get("product")}`;
      return Promise.resolve(MONTHS[key] || { months: [], monthsError: null });
    }
    return Promise.reject(new Error(`未预期的请求 ${url}`));
  };
}

function makeStorage(initial) {
  const data = { ...(initial || {}) };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => { data[key] = String(value); },
  };
}

function setup(options = {}) {
  const doc = makeDocument();
  const el = makeRoot(doc);
  const calls = [];
  const picked = [];
  const statuses = [];
  const toggled = [];
  const favoriteSet = new Set(options.favorites || []);
  const fetchJson = (url) => {
    calls.push(url);
    return Promise.resolve((options.fetchJson || defaultRoutes())(url));
  };
  const picker = ContractPicker.create({
    core: PickerCore,
    flow: FlowData,
    document: doc,
    root: el,
    fetchJson,
    storage: options.storage || null,
    now: options.now,
    isFavorite: options.stars ? (symbol) => favoriteSet.has(symbol) : null,
    onToggleFavorite: options.stars ? (symbol) => {
      toggled.push(symbol);
      if (favoriteSet.has(symbol)) favoriteSet.delete(symbol); else favoriteSet.add(symbol);
      return Promise.resolve();
    } : null,
    onPick: (symbol) => picked.push(symbol),
    onStatus: (text, detail) => statuses.push([text, detail]),
  });
  return { doc, el, picker, calls, picked, statuses, toggled, favorites: favoriteSet };
}

const rowIds = (column) => column.children
  .filter((child) => child.attributes.id)
  .map((child) => child.attributes.id);

// ---------- 用例 ----------

test("目录到达后按交易所分组渲染品种, 触发器升级成品种 · 主力月份", async () => {
  const { el, picker, calls } = setup();
  picker.setSymbol("KQ.m@SHFE.fu");
  assert.equal(el.trigger.textContent, "KQ.m@SHFE.fu");   // 目录还没到, 先显示原始代码
  picker.load();
  await flush();
  assert.equal(calls[0], "/api/symbols");
  // 目录到手后会顺手把第一个品种(燃油)的月份也取了: 打开菜单就是可用的
  assert.ok(calls.includes("/api/symbols?exchange=SHFE&product=fu"));
  assert.equal(el.trigger.textContent, "燃油 · fu2611");
  const groups = el.products.children.filter((child) => child.className === "picker-group")
    .map((child) => child.textContent);
  assert.deepEqual(groups, ["上期所", "大商所"]);
  assert.equal(rowIds(el.products).length, 3);
  assert.match(el.products.textContent, /燃油 · fu2611/);
  assert.match(el.products.textContent, /314\.5万/);       // 昨仓按万缩写
});

test("打开菜单聚焦搜索框, Esc 关闭", async () => {
  const { el, picker } = setup();
  picker.load();
  await flush();
  picker.open();
  assert.equal(el.popup.hidden, false);
  assert.equal(el.search.focused, true);
  assert.equal(el.trigger.attributes["aria-expanded"], "true");
  el.search.dispatch("keydown", { key: "Escape" });
  assert.equal(picker.isOpen(), false);
  assert.equal(el.popup.hidden, true);
});

test("输入即过滤一级, 并自动预取第一个命中品种的月份", async () => {
  const { el, picker, calls } = setup();
  picker.load();
  await flush();
  el.search.value = "螺纹";
  el.search.dispatch("input");
  assert.equal(rowIds(el.products).length, 1);
  await flush();
  assert.ok(calls.includes("/api/symbols?exchange=SHFE&product=rb"));
  assert.match(el.months.textContent, /★ 螺纹2701/);
  el.search.value = "zzz";
  el.search.dispatch("input");
  assert.match(el.products.textContent, /没有匹配的品种/);
});

test("悬停品种(带防抖)后右侧出现★主力和具体月份", async () => {
  const { el, picker, calls } = setup();
  picker.load();
  await flush();
  picker.open();
  const rows = el.products.children.filter((child) => child.attributes.id);
  rows[1].dispatch("mouseenter");            // 悬停"螺纹"
  assert.ok(!calls.includes("/api/symbols?exchange=SHFE&product=rb"));   // 防抖期内不请求
  await sleep(180);
  assert.ok(calls.includes("/api/symbols?exchange=SHFE&product=rb"));
  await flush();
  assert.match(el.months.textContent, /月份 · 螺纹/);
});

test("键盘: ↓ 移动一级, → 进二级, ↓ 选月份, Enter 切换", async () => {
  const { el, picker, picked } = setup();
  picker.load();
  await flush();
  picker.open();
  el.search.dispatch("keydown", { key: "ArrowDown" });   // 移到"螺纹"
  await flush();
  el.search.dispatch("keydown", { key: "ArrowRight" });  // 进二级
  el.search.dispatch("keydown", { key: "ArrowDown" });   // 螺纹只有一个月份, 停在第一条
  el.search.dispatch("keydown", { key: "Enter" });
  assert.deepEqual(picked, ["KQ.m@SHFE.rb"]);            // rb 的月份列表里第一项是主力
  assert.equal(picker.isOpen(), false);
});

test("键盘: 一级直接回车 = 该品种主力; 二级选具体月份", async () => {
  const { el, picker, picked } = setup();
  picker.load();
  await flush();
  picker.open();
  el.search.dispatch("keydown", { key: "Enter" });
  assert.deepEqual(picked, ["KQ.m@SHFE.fu"]);            // 一级第一条是燃油主连

  const second = setup();
  second.picker.load();
  await flush();
  second.picker.open();
  second.el.search.dispatch("keydown", { key: "ArrowRight" });
  await flush();
  second.el.search.dispatch("keydown", { key: "ArrowDown" });
  second.el.search.dispatch("keydown", { key: "Enter" });
  assert.deepEqual(second.picked, ["SHFE.fu2701"]);
});

test("点击月份行直接切换, 点同一个合约不重复跳转", async () => {
  const { el, picker, picked } = setup();
  picker.load();
  await flush();
  picker.open();
  const monthRows = el.months.children.filter((child) => child.attributes.id);
  monthRows[1].dispatch("click");
  assert.deepEqual(picked, ["SHFE.fu2701"]);
  const again = setup();
  again.picker.setSymbol("SHFE.fu2701");
  again.picker.load();
  await flush();
  again.picker.open();
  const rows = again.el.months.children.filter((child) => child.attributes.id);
  rows[1].dispatch("click");
  assert.deepEqual(again.picked, []);                    // 已经是这个合约, 不触发切换
});

test("目录接口失败时用 localStorage 缓存渲染, 且不报错给用户", async () => {
  const storage = makeStorage({
    "flowscope.catalog.v1": JSON.stringify({ at: 1, groups: CATALOG.groups }),
  });
  const { el, picker, statuses } = setup({
    storage,
    fetchJson: () => Promise.reject(new Error("offline")),
  });
  picker.setSymbol("KQ.m@SHFE.fu");
  picker.load();
  await flush();
  assert.equal(rowIds(el.products).length, 3);
  assert.equal(el.trigger.textContent, "燃油 · fu2611");
  // 成功路径只把工具栏提示清空, 不报错
  assert.deepEqual(statuses.map(([text]) => text), [""]);
});

test("没有缓存且目录失败时给出可手填的提示", async () => {
  const { picker, statuses } = setup({ fetchJson: () => Promise.reject(new Error("offline")) });
  picker.load();
  await flush();
  assert.equal(statuses[0][0], "合约目录加载失败（1/6），可直接手填合约");
  assert.equal(statuses[0][1], "offline");
});

test("月份请求失败时二级只剩★主力可选, 并说明原因", async () => {
  const { el, picker, picked } = setup({
    fetchJson: (url) => (url.startsWith("/api/symbols?")
      ? Promise.reject(new Error("合约查询超时"))
      : Promise.resolve(CATALOG)),
  });
  picker.load();
  await flush();
  picker.open();
  assert.match(el.months.textContent, /★ 燃油2611|★ 燃油/);
  assert.match(el.months.textContent, /月份列表暂不可用（稍后自动重试）：合约查询超时/);
  el.search.dispatch("keydown", { key: "Enter" });
  assert.deepEqual(picked, ["KQ.m@SHFE.fu"]);            // 主力仍然选得动
});

test("月份失败不是永久状态: 冷却期过后重新打开会再试一次", async () => {
  let clock = 1000;
  let failing = true;
  const { el, picker, calls } = setup({
    now: () => clock,
    fetchJson: (url) => {
      if (url === "/api/symbols") return Promise.resolve(CATALOG);
      if (failing) return Promise.reject(new Error("行情连接无响应(21 秒)，正在重建"));
      const params = new URLSearchParams(url.slice(url.indexOf("?") + 1));
      return Promise.resolve(MONTHS[`${params.get("exchange")}.${params.get("product")}`]
                             || { months: [], monthsError: null });
    },
  });
  picker.load();
  await flush();
  assert.match(el.months.textContent, /暂不可用（稍后自动重试）/);
  const attempts = calls.filter((url) => url.startsWith("/api/symbols?")).length;

  picker.close();
  picker.open();                                   // 冷却期内: 不重复打接口
  await flush();
  assert.equal(calls.filter((url) => url.startsWith("/api/symbols?")).length, attempts);

  failing = false;
  clock += 9000;                                   // 过了冷却期
  picker.close();
  picker.open();
  await flush();
  assert.equal(calls.filter((url) => url.startsWith("/api/symbols?")).length, attempts + 1);
  assert.match(el.months.textContent, /★ 燃油2611/);
  assert.doesNotMatch(el.months.textContent, /暂不可用/);
});

test("目录接口失败会自动重试, 成功后提示消失", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });   // 重试间隔 10 秒, 不能真等
  const ticks = async (count = 6) => { for (let i = 0; i < count; i++) await Promise.resolve(); };
  let failing = true;
  const { el, picker, statuses, calls } = setup({
    fetchJson: (url) => {
      if (url !== "/api/symbols") return Promise.resolve({ months: [], monthsError: null });
      return failing ? Promise.reject(new Error("offline")) : Promise.resolve(CATALOG);
    },
  });
  picker.load();
  await ticks();
  assert.match(statuses[0][0], /合约目录加载失败（1\/6）/);
  failing = false;
  t.mock.timers.tick(10000);                        // 自动重试
  await ticks();
  assert.equal(rowIds(el.products).length, 3);
  assert.equal(statuses[statuses.length - 1][0], "");
  assert.equal(calls.filter((url) => url === "/api/symbols").length, 2);
});

test("离线目录(source=fallback)会在工具栏提示, 但列表照样可用", async () => {
  const fallback = { source: "fallback", error: "RuntimeError: 行情线程未就绪", groups: CATALOG.groups };
  const { el, picker, statuses } = setup({ fetchJson: () => Promise.resolve(fallback) });
  picker.setSymbol("KQ.m@SHFE.fu");
  picker.load();
  await flush();
  assert.equal(statuses[0][0], "目录: 离线常用品种");
  assert.match(statuses[0][1], /行情线程未就绪/);
  assert.equal(rowIds(el.products).length, 3);
  assert.equal(el.trigger.textContent, "燃油 · fu2611");   // 离线表里的中文名照样能用
});

test("点击菜单外部会收起浮层", async () => {
  const { doc, el, picker } = setup();
  picker.load();
  await flush();
  picker.open();
  const outside = doc.createElement("div");
  doc.dispatch("click", { target: outside });
  assert.equal(picker.isOpen(), false);
  picker.open();
  doc.dispatch("click", { target: el.search });
  assert.equal(picker.isOpen(), true);                   // 点在菜单里不收起
});

// ---------- 行内自选 ☆ ----------

// ☆ 是行内的子元素, 所以要从每行的 children 里捞
const stars = (column) => column.children
  .flatMap((row) => (row.children || [])
    .filter((child) => child.className && child.className.startsWith("picker-star")));

test("传了 isFavorite/onToggleFavorite 才显示行内 ☆", async () => {
  const plain = setup();
  plain.picker.load();
  await flush();
  assert.equal(stars(plain.el.products).length, 0);

  const { el, picker } = setup({ stars: true });
  picker.load();
  await flush();
  assert.equal(stars(el.products).length, 3);            // 每个品种一行
  assert.equal(stars(el.months).length, 2);              // 主力 + 一个月份
  assert.deepEqual(stars(el.products).map((s) => s.textContent), ["☆", "☆", "☆"]);
});

test("已收藏的行显示实心星, refreshFavorites 就地重画", async () => {
  const { el, picker, favorites } = setup({ stars: true, favorites: ["KQ.m@SHFE.fu"] });
  picker.load();
  await flush();
  assert.equal(stars(el.products)[0].textContent, "★");
  assert.equal(stars(el.products)[1].textContent, "☆");
  favorites.add("KQ.m@SHFE.rb");                          // 宿主页面(自选面板)改了自选
  picker.refreshFavorites();
  assert.equal(stars(el.products)[1].textContent, "★");
});

test("点行内 ☆ 只收藏, 不切换合约也不关菜单", async () => {
  const { el, picker, picked, toggled } = setup({ stars: true });
  picker.load();
  await flush();
  picker.open();
  const monthStar = stars(el.months)[1];
  monthStar.dispatch("click");
  assert.deepEqual(toggled, ["SHFE.fu2701"]);             // 收藏的是那一行的具体月份
  assert.deepEqual(picked, []);                           // 没有切换合约
  assert.equal(picker.isOpen(), true);                    // 菜单还开着, 可以连着收藏

  const productStar = stars(el.products)[1];
  productStar.dispatch("click");
  assert.deepEqual(toggled, ["SHFE.fu2701", "KQ.m@SHFE.rb"]);   // 品种行收藏的是主力
  assert.deepEqual(picked, []);
});

test("收藏成功后星号变实心, 再点一次就移出", async () => {
  const { el, picker, toggled } = setup({ stars: true });
  picker.load();
  await flush();
  const star = stars(el.products)[0];
  star.dispatch("click");
  await flush();
  assert.equal(star.textContent, "★");
  star.dispatch("click");
  await flush();
  assert.equal(star.textContent, "☆");
  assert.deepEqual(toggled, ["KQ.m@SHFE.fu", "KQ.m@SHFE.fu"]);
});

test("菜单里的 ☆ 与工具栏、自选面板共用同一份自选", async () => {
  const { el, picker, favorites } = setup({ stars: true });
  picker.load();
  await flush();
  favorites.add("KQ.m@DCE.i");                            // 等价于从自选面板或 ★ 按钮加的
  picker.refreshFavorites();
  assert.equal(stars(el.products)[2].textContent, "★");   // 铁矿石那一行亮起来
  assert.equal(stars(el.products)[0].textContent, "☆");
});
