"use strict";
/* 独立档案窗口的异步竞态与实时 SSE 共存回归, 不调用真实模型。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { dateLabel, statusLabel, warningText, copyText } = require("../static/ai-history.js");

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.style = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.scrollTop = 0;
    this.clientHeight = this.clientWidth = 1000;
    this.scrollHeight = 0;
    this.parentElement = this;
    this.offsetWidth = 460;
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name),
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name); else classes.delete(name);
        return on;
      },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map((child) => child.textContent).join(""); }
  appendData(value) { this._text = (this._text || "") + value; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this._text = ""; this.children = [...nodes]; }
  get options() { return this.children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, callback) { (this.handlers[type] ||= []).push(callback); }
  async fire(type, event = {}) { await Promise.all((this.handlers[type] || []).map((fn) => fn(event))); }
  focus() { this.focused = true; }
  getContext() { return { fillRect() {}, drawImage() {}, fillText() {} }; }
  toDataURL() { return "data:image/png;base64,AA=="; }
}

const fixture = (id, changes = {}) => ({
  id, createdAt: "2026-10-12T14:20:43+08:00", symbol: "KQ.m@SHFE.fu", label: `燃油 ${id}`,
  periods: [60, 10], status: "done", finish: "stop", excerpt: `摘要 ${id}`, answer: `正文 ${id}`,
  reasoning: "已保存的思考", model: "preview", elapsed: 12, charts: [{ tf: 60, imageUrl: `/images/${id}` }],
  ...changes,
});
const json = (body, status = 200) => ({ ok: status === 200, status, json: async () => body });
const listing = (items, changes = {}) => ({ items, total: items.length, offset: 0, hasMore: false, periods: [10, 60], skipped: 0, ...changes });
const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(fetchImpl) {
  const nodes = new Map();
  const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const copied = [];
  const context = {
    document: { createElement: (tag) => new Element(tag), createTextNode: (text) => { const n = new Element("#text"); n.textContent = text; return n; } },
    navigator: { clipboard: { writeText: async (text) => copied.push(text) } },
    localStorage: { getItem: () => null, setItem() {} },
    FlowIndicators: { EMA_PERIODS: [] },
    fetch: fetchImpl, AbortController, URLSearchParams, TextEncoder, TextDecoder,
    setTimeout, clearTimeout, setInterval, clearInterval, console,
  };
  vm.createContext(context);
  for (const file of ["ai-panel.js", "ai-history.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", file), "utf8"), context);
  }
  byId("ai-history").hidden = true;
  byId("ai-history-scope").appendChild(new Element("option"));
  byId("ai-history-scope").value = "current";
  const history = context.AiHistory.create({ byId, getSymbol: () => "KQ.m@SHFE.fu", getLabel: () => "燃油2611" });
  return { context, byId, history, copied };
}

test("时间统一为北京时间, 复制正文保留停止和截断提示", () => {
  assert.equal(dateLabel("2026-10-11T16:00:00Z"), "2026-10-12 00:00:00");
  assert.equal(dateLabel("bad"), "时间未知");
  const record = fixture("old", { status: "stopped", finish: "length" });
  assert.equal(statusLabel(record), "已停止 · 已截断");
  assert.match(warningText(record), /停止.*\n.*截断/);
  assert.match(copyText(record), /北京时间.*1m \+ 10s/);
  assert.match(copyText(record), /正文 old/);
});

test("首次打开按当前合约查询, 双周期/详情/缺图/复制显示正确", async () => {
  const seen = [];
  const old = fixture("old", { charts: [{ tf: 60, imageUrl: null }], answer: "<script>原文</script>" });
  const { history, byId, copied } = harness(async (url) => {
    seen.push(String(url));
    return json(String(url).includes("?") ? listing([old]) : old);
  });
  await history.open();
  assert.equal(new URL(`http://local${seen[0]}`).searchParams.get("symbol"), "KQ.m@SHFE.fu");
  assert.equal(byId("ai-history-answer").textContent, old.answer);
  assert.equal(byId("ai-history-answer").children.length, 0, "历史正文不作为 HTML 解析");
  assert.equal(byId("ai-history-thinking").open, false);
  assert.match(byId("ai-history-shots").textContent, /截图文件缺失/);
  assert.equal(byId("ai-history-content").classList.contains("show-detail"), false, "窄屏初次打开先展示列表");
  await history.select("old");
  assert.equal(byId("ai-history-content").classList.contains("show-detail"), true);
  await byId("ai-history-back").fire("click");
  assert.equal(byId("ai-history-content").classList.contains("show-detail"), false);
  await byId("ai-history-copy").fire("click");
  assert.match(copied[0], /<script>原文<\/script>/);
  assert.equal(byId("ai-history-copy-status").textContent, "已复制分析全文");
});

test("筛选和分页参数传给服务端, 加载更多不覆盖已选详情", async () => {
  const seen = [];
  const { history, byId } = harness(async (url) => {
    const parsed = new URL(`http://local${url}`);
    if (!parsed.search) return json(fixture(parsed.pathname.split("/").pop()));
    seen.push(parsed.searchParams);
    const second = parsed.searchParams.get("offset") === "20";
    return json(second ? listing([fixture("last")], { total: 21, offset: 20 })
      : listing(Array.from({ length: 20 }, (_, index) => fixture(`r${index}`)), { total: 21, hasMore: true }));
  });
  await history.open();
  await byId("ai-history-more").fire("click");
  assert.equal(byId("ai-history-list").children.length, 21);
  assert.equal(byId("ai-history-more").hidden, true);
  assert.equal(byId("ai-history-answer").textContent, "正文 r0");
  byId("ai-history-scope").value = "all";
  byId("ai-history-query").value = "比特币";
  byId("ai-history-from").value = "2026-10-01";
  byId("ai-history-to").value = "2026-10-12";
  byId("ai-history-tf").value = "60";
  byId("ai-history-status").value = "stopped";
  await history.refresh({ keepDetail: false });
  const params = seen.at(-1);
  assert.equal(params.has("symbol"), false);
  for (const [key, value] of Object.entries({ q: "比特币", date_from: "2026-10-01", date_to: "2026-10-12", tf: "60", status: "stopped", offset: "0" })) {
    assert.equal(params.get(key), value);
  }
});

test("切换记录与关窗后的迟到详情不能覆盖当前记录或重新打开窗口", async () => {
  const late = deferred();
  const { history, byId } = harness(async (url) => {
    if (String(url).includes("?")) return json(listing([]));
    if (String(url).endsWith("slow")) return late.promise;
    return json(fixture("latest"));
  });
  await history.open();
  const pending = history.select("slow");
  await history.select("latest");
  late.resolve(json(fixture("slow")));
  await pending;
  assert.equal(byId("ai-history-answer").textContent, "正文 latest");
  const closing = history.select("latest");
  history.close();
  await closing;
  assert.equal(byId("ai-history").hidden, true);
  assert.equal(byId("ai-history-record").hidden, true);
});

test("快速切换筛选时只采纳最后一次列表, 空列表与失败均可重试", async () => {
  const old = deferred();
  let calls = 0;
  const { history, byId } = harness(async (url) => {
    if (!String(url).includes("?")) return json(fixture("new"));
    calls++;
    if (calls === 1) return old.promise;
    if (calls === 3) return json({ detail: "日期格式不对" }, 400);
    return json(listing([]));
  });
  const opening = history.open();
  await history.refresh({ keepDetail: false });
  old.resolve(json(listing([fixture("stale")])));
  await opening;
  assert.equal(byId("ai-history-list").children.length, 0);
  assert.match(byId("ai-history-notice").textContent, /没有符合条件/);
  await history.refresh();
  assert.match(byId("ai-history-notice").textContent, /日期格式不对/);
  assert.equal(byId("ai-history-refresh").disabled, false);
  await history.refresh();
  assert.match(byId("ai-history-notice").textContent, /没有符合条件/);
});

test("浏览、切换、关闭历史时实时 SSE 保持连接并继续追加, 保存后自动刷新历史", async () => {
  let readWaiter = null, analysisSignal, historyReads = 0;
  const queue = [];
  const push = (value) => {
    if (readWaiter) { const waiter = readWaiter; readWaiter = null; waiter.resolve(value); }
    else queue.push(value);
  };
  const sse = (event, data) => push({ done: false, value: new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`) });
  const { context, history, byId } = harness(async (url, init) => {
    if (url === "/api/analyze") {
      analysisSignal = init.signal;
      analysisSignal.addEventListener("abort", () => {
        if (readWaiter) readWaiter.reject(Object.assign(new Error("停止"), { name: "AbortError" }));
      });
      return { ok: true, body: { getReader: () => ({ read: () => {
        if (queue.length) return Promise.resolve(queue.shift());
        readWaiter = deferred();
        return readWaiter.promise;
      } }) } };
    }
    if (String(url).includes("?")) { historyReads++; return json(listing([fixture("old"), fixture("second")])); }
    return json(fixture(String(url).split("/").pop()));
  });
  const input = {
    bars: [{ time: 1791774000, open: 3000, high: 3001, low: 2999, close: 3000, volume: 10 }], ltf: 0,
    derived: { rvol: [1], emaLines: [], lw: { wave: [50], wt2: [50], crvSlope: [0] },
      band: { up: [3010], mid: [3000], dn: [2990] } },
  };
  const live = context.AiPanel.create({ byId, onSaved: () => history.refresh(), collect: () => ({
    symbol: "KQ.m@SHFE.fu", label: "燃油2611", settings: { mode: "cvd" }, paper: [],
    sources: [{ tf: 30, input, screenshot: () => ({ width: 300, height: 200 }) }],
  }) });
  const running = live.run();
  try {
    sse("delta", { text: "第一段" });
    await tick();
    assert.equal(byId("ai-text").textContent, "第一段");
    await history.open();
    await history.select("second");
    history.close();
    assert.equal(analysisSignal.aborted, false);
    assert.equal(live.busy(), true);
    sse("delta", { text: "第二段" });
    await tick();
    assert.equal(byId("ai-text").textContent, "第一段第二段");
    await history.open();
    await history.select("second");
    const beforeSave = historyReads;
    sse("done", { status: "done", elapsed: 2, saved: "analysis/20261012/new.json" });
    push({ done: true });
    await running;
    await tick();
    assert.equal(historyReads, beforeSave + 1);
    assert.equal(byId("ai-history-answer").textContent, "正文 second", "自动刷新不切换正在阅读的记录");
    assert.equal(analysisSignal.aborted, false);
    assert.equal(byId("ai-text").textContent, "第一段第二段");
    assert.equal(live.busy(), false);
  } finally {
    live.stop();
    push({ done: true });
    await running;
  }
});
