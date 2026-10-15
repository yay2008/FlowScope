"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { accountOf, canReview, warningText } = require("../static/trade-review.js");

class Element {
  constructor() {
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.style = {};
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.scrollTop = this.scrollHeight = 0;
    this.clientHeight = this.clientWidth = 500;
    this.offsetWidth = 500;
    this.parentElement = this;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map((child) => child.textContent).join(""); }
  appendData(value) { this._text = (this._text || "") + value; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this._text = ""; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { (this.handlers[name] ||= []).push(callback); }
  async fire(name) { await Promise.all((this.handlers[name] || []).map((fn) => fn({}))); }
  querySelectorAll() { return this.children; }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const json = (body) => ({ ok: true, status: 200, json: async () => body });
const nextDate = (date) => new Date(Date.parse(`${date}T00:00:00Z`) + 86400000).toISOString().slice(0, 10);
const snapshot = (account = "futures", date = "2026-10-14", count = 2, funding = []) => ({
  account, date, defaultDate: date, currency: account === "crypto" ? "USDT" : "CNY",
  dateBasis: "北京时间自然日", range: { from: `${date}T00:00:00+08:00`, to: `${nextDate(date)}T00:00:00+08:00` },
  snapshotAt: "2026-10-14 16:00:00",
  summary: { tradeCount: count, closeCount: 1, winRate: 100, netPnl: 120, realizedPnl: 128, fees: 8, funding: 0, maxDrawdown: 20 },
  contracts: [{ contract: "SHFE.fu2611", tradeCount: count, netPnl: 120 }], trades: [], fundings: funding, warnings: [],
});
function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}
function stream(events) {
  const encoder = new TextEncoder();
  const body = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
  let read = false;
  return { ok: true, status: 200, body: { getReader: () => ({ read: async () => {
    if (read) return { done: true };
    read = true;
    return { done: false, value: encoder.encode(body) };
  } }) } };
}
function harness(fetchImpl, symbol = "KQ.m@SHFE.fu") {
  const nodes = new Map();
  const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = {
    document: { createElement: () => new Element(), createTextNode: (value) => {
      const node = new Element(); node.textContent = value; return node;
    } },
    FlowIndicators: { EMA_PERIODS: [] }, fetch: fetchImpl, AbortController, TextDecoder, URLSearchParams,
    setInterval, clearInterval, console,
  };
  vm.createContext(context);
  for (const file of ["ai-panel.js", "trade-review.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "static", file), "utf8"), context);
  }
  byId("trade-review-panel").hidden = true;
  const review = context.TradeReview.create({ byId, getSymbol: () => symbol });
  return { review, byId };
}

test("账户识别、空快照与中断提示", () => {
  assert.equal(accountOf("BINANCE.BTCUSDT.P"), "crypto");
  assert.equal(accountOf("OKX.BTC-USDT-SWAP"), "crypto");
  assert.equal(accountOf("KQ.m@SHFE.fu"), "futures");
  assert.equal(canReview(snapshot("futures", "2026-10-14", 0)), false);
  assert.equal(canReview(snapshot("crypto", "2026-10-14", 0, [{}])), true);
  assert.match(warningText({ warnings: ["缺少部分旧成交"], status: "stopped", finish: "length" }), /缺少部分旧成交.*停止.*截断/s);
});

test("日期控件及新快照显示北京时间自然日范围", async () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8");
  assert.match(html, /复盘日期（北京时间）<input id="trade-review-date"/);
  const { review, byId } = harness((url) => String(url).startsWith("/api/review/history?")
    ? Promise.resolve(json({ items: [], total: 0 })) : Promise.resolve(json(snapshot())));
  review.open();
  await tick();
  assert.match(byId("trade-review-basis").textContent, /2026-10-14 北京时间自然日 · 00:00–24:00（北京时间）/);
  assert.doesNotMatch(byId("trade-review-warnings").textContent, /旧交易日口径/);
  review.close();
});

test("首次打开只读快照，按账户和日期切换时清掉旧统计及报告", async () => {
  const calls = [], pending = deferred();
  const { review, byId } = harness((url, init) => {
    calls.push({ url: String(url), method: init?.method || "GET" });
    if (String(url).startsWith("/api/review/history?")) return Promise.resolve(json({ items: [], total: 0 }));
    if (String(url).includes("account=crypto")) return pending.promise;
    return Promise.resolve(json(snapshot()));
  });
  review.open();
  await tick();
  assert.equal(byId("trade-review-account").value, "futures");
  assert.equal(byId("trade-review-date").value, "2026-10-14");
  assert.match(byId("trade-review-summary").textContent, /120\.00 CNY/);
  assert.equal(calls.filter((call) => call.method === "POST").length, 0);
  byId("trade-review-answer").textContent = "旧报告";
  byId("trade-review-account").value = "crypto";
  await byId("trade-review-account").fire("change");
  assert.equal(byId("trade-review-summary").textContent, "");
  assert.equal(byId("trade-review-answer").textContent, "");
  assert.equal(byId("trade-review-start").disabled, true);
  pending.resolve(json(snapshot("crypto", "2026-10-15")));
  await tick();
  assert.equal(byId("trade-review-date").value, "2026-10-15");
  assert.match(byId("trade-review-basis").textContent, /北京时间自然日/);
  assert.equal(byId("trade-review-start").disabled, false);
  review.close();
});

test("旧日期响应不能覆盖新日期；空记录不能请求模型，资金费可复盘", async () => {
  const stale = deferred(), calls = [];
  const { review, byId } = harness((url, init) => {
    calls.push(String(url));
    const parsed = new URL(`http://localhost${url}`);
    if (parsed.pathname === "/api/review/history") return Promise.resolve(json({ items: [], total: 0 }));
    if (parsed.searchParams.get("date") === "2026-10-12") return stale.promise;
    if (parsed.searchParams.get("date") === "2026-10-13") return Promise.resolve(json(snapshot("futures", "2026-10-13", 0, [{ amount: 5 }])));
    return Promise.resolve(json(snapshot("futures", "2026-10-14", 0)));
  });
  review.open();
  await tick();
  assert.equal(byId("trade-review-start").disabled, true);
  await byId("trade-review-start").fire("click");
  assert.equal(calls.some((url) => url === "/api/review"), false);
  byId("trade-review-date").value = "2026-10-12";
  await byId("trade-review-date").fire("change");
  byId("trade-review-date").value = "2026-10-13";
  await byId("trade-review-date").fire("change");
  stale.resolve(json(snapshot("futures", "2026-10-12", 5)));
  await tick();
  assert.equal(byId("trade-review-date").value, "2026-10-13");
  assert.equal(byId("trade-review-start").disabled, false);
  review.close();
});

test("显式开始后流式显示报告，更新 meta 快照并可从历史打开保存结果", async () => {
  const calls = [];
  const current = snapshot();
  const latest = snapshot(); latest.summary.netPnl = 150;
  const { review, byId } = harness((url, init) => {
    calls.push({ url: String(url), init });
    if (String(url) === "/api/review") return Promise.resolve(stream([
      ["meta", { model: "deepseek", snapshot: latest }],
      ["reasoning", { text: "分析成交" }],
      ["delta", { text: "控制回撤。" }],
      ["done", { elapsed: 1.2, usage: { prompt_tokens: 10, completion_tokens: 20 }, saved: "review/20261014/report.json" }],
    ]));
    if (String(url).startsWith("/api/review/history?")) return Promise.resolve(json({ items: [{ id: "saved", reviewDate: "2026-10-14", label: "期货复盘", status: "done", excerpt: "控制回撤" }], total: 1 }));
    if (String(url).endsWith("/saved")) return Promise.resolve(json({ ...latest, snapshot: latest, answer: "已保存报告", reasoning: "旧思考", model: "deepseek", elapsed: 1.2, status: "done" }));
    return Promise.resolve(json(current));
  });
  review.open();
  await tick();
  await review.run();
  assert.deepEqual(JSON.parse(calls.find((call) => call.url === "/api/review").init.body), { account: "futures", date: "2026-10-14" });
  assert.match(byId("trade-review-summary").textContent, /150\.00 CNY/);
  assert.equal(byId("trade-review-answer").textContent, "控制回撤。");
  assert.match(byId("trade-review-foot").textContent, /已存 data\/review\//);
  await tick();
  await review.showHistory("saved");
  assert.equal(byId("trade-review-answer").textContent, "已保存报告");
  assert.equal(byId("trade-review-thinking-text").textContent, "旧思考");
  review.close();
});

test("旧期货交易日报告保留原口径并提示重新生成自然日复盘", async () => {
  const oldSnapshot = { ...snapshot(), dateBasis: "交易日" };
  delete oldSnapshot.range;
  const calls = [];
  const { review, byId } = harness((url, init) => {
    calls.push({ url: String(url), method: init?.method || "GET" });
    if (String(url).endsWith("/old-futures")) return Promise.resolve(json({
      snapshot: oldSnapshot, account: "futures", reviewDate: "2026-10-14", answer: "旧口径结论", status: "done",
    }));
    if (String(url).startsWith("/api/review/history?")) return Promise.resolve(json({ items: [], total: 0 }));
    return Promise.resolve(json(snapshot()));
  });
  review.open();
  await tick();
  await review.showHistory("old-futures");
  assert.equal(byId("trade-review-answer").textContent, "旧口径结论");
  assert.match(byId("trade-review-basis").textContent, /交易日/);
  assert.doesNotMatch(byId("trade-review-basis").textContent, /00:00–24:00/);
  assert.match(byId("trade-review-warnings").textContent, /旧交易日口径（含前一晚夜盘），请重新生成自然日复盘/);
  assert.equal(calls.filter((call) => call.method === "POST").length, 0);
  await review.refreshSnapshot();
  assert.match(byId("trade-review-basis").textContent, /北京时间自然日 · 00:00–24:00/);
  assert.doesNotMatch(byId("trade-review-warnings").textContent, /旧交易日口径/);
  review.close();
});

test("关闭会中断运行中的请求并禁止迟到响应覆盖界面", async () => {
  const late = deferred();
  const { review, byId } = harness((url) => {
    if (url === "/api/review") return late.promise;
    if (String(url).startsWith("/api/review/history?")) return Promise.resolve(json({ items: [], total: 0 }));
    return Promise.resolve(json(snapshot()));
  });
  review.open();
  await tick();
  const running = review.run();
  assert.equal(byId("trade-review-account").disabled, true);
  review.close();
  assert.equal(byId("trade-review-panel").hidden, true);
  late.resolve(stream([["delta", { text: "迟到" }], ["done", { elapsed: 2 }]]));
  await running;
  assert.equal(byId("trade-review-answer").textContent, "");
  assert.equal(byId("trade-review-account").disabled, false);
});

test("刷新当前记录可让新增成交从空快照变为可复盘，迟到的历史详情不能覆盖", async () => {
  const refreshed = deferred(), oldDetail = deferred(), calls = [];
  let reads = 0;
  const { review, byId } = harness((url, init) => {
    calls.push({ url: String(url), method: init?.method || "GET" });
    if (String(url) === "/api/review/history/old") return oldDetail.promise;
    if (String(url).startsWith("/api/review/history?")) return Promise.resolve(json({ items: [], total: 0 }));
    if (String(url).startsWith("/api/review/snapshot?")) return ++reads === 1
      ? Promise.resolve(json(snapshot("futures", "2026-10-14", 0))) : refreshed.promise;
    throw new Error(`意外请求 ${url}`);
  });
  review.open();
  await tick();
  assert.equal(byId("trade-review-start").disabled, true);
  assert.equal(byId("trade-review-refresh").disabled, false);
  const detailPromise = review.showHistory("old");
  const refreshPromise = byId("trade-review-refresh").fire("click");
  assert.equal(byId("trade-review-refresh").disabled, true);
  assert.equal(byId("trade-review-answer").textContent, "");
  refreshed.resolve(json(snapshot("futures", "2026-10-14", 3)));
  await refreshPromise;
  assert.equal(byId("trade-review-refresh").disabled, false);
  assert.equal(byId("trade-review-start").disabled, false);
  assert.match(byId("trade-review-summary").textContent, /已实现回撤/);
  oldDetail.resolve(json({ snapshot: snapshot(), answer: "旧历史报告", status: "done" }));
  await detailPromise;
  assert.equal(byId("trade-review-answer").textContent, "");
  assert.equal(byId("trade-review-status").textContent, "可开始复盘");
  assert.equal(calls.filter((call) => call.method === "POST").length, 0);
  assert.equal(calls.filter((call) => call.url.startsWith("/api/review/snapshot?")).length, 2);
  review.close();
});
