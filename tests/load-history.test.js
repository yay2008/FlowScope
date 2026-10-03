"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 直接运行图表组件使用的异步加载流程；只替换网络、计时器和绘图边界。
const source = fs.readFileSync(path.join(__dirname, "../static/chart-view.js"), "utf8");
const loader = source.slice(source.indexOf("function clearLoadRetry()"), source.indexOf("function onBars("));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function harness() {
  const requests = [], timers = new Map(), statuses = [];
  let timerId = 0, closed = 0, connected = 0;
  const ctx = vm.createContext({
    AbortController, encodeURIComponent,
    retryTimer: null, retryAttempts: 0, RETRY_MAX_ATTEMPTS: 24, RETRY_DELAY_MS: 5000,
    loadGeneration: 0, loadController: null, wsGeneration: 0, reconnectTimer: null,
    watchdog: null, ws: null, symbol: "SHFE.review", ltf: 0, tf: 30,
    cfg: null, bars: [], barRevision: -1, fpRevision: -1,
    onStatus: (ok, text) => statuses.push(text),
    clearTimeout: id => timers.delete(id),
    setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; },
    // 故意允许已取消请求继续返回，验证版本检查本身能拦截迟到结果。
    fetch: (url, options) => {
      const result = deferred();
      requests.push({...result, url, signal: options.signal});
      return result.promise;
    },
    // 每轮加载按工具栏重算本图粒度; 这里直接用 ctx.ltf 模拟"数据源已切换"
    currentLtf: () => ctx.ltf,
    onConfig: () => {}, renderAll: () => {},
    chart: {timeScale: () => ({scrollToRealTime: () => {}})},
    connectWs: () => { connected++; ctx.ws = {close: () => closed++}; },
  });
  vm.runInContext(loader, ctx);
  return {ctx, requests, timers, statuses, load: () => ctx.loadHistory(),
          closed: () => closed, connected: () => connected,
          fire: () => { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.fn(); }};
}

const success = (bars = []) => ({ok: true, json: async () => ({cfg: {}, bars})});
const failure = () => ({ok: false, status: 400, json: async () => ({detail: "subscription failed"})});
const flush = () => new Promise(resolve => setImmediate(resolve));

test("stale HTTP error cannot schedule a retry or close the healthy replacement socket", async () => {
  const h = harness();
  const old = h.load();
  h.ctx.tf = 10;
  const current = h.load();
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[1].resolve(success([{time: 10}]));
  await current;
  h.requests[0].resolve(failure());
  await old;
  assert.equal(h.timers.size, 0);
  assert.equal(h.ctx.retryAttempts, 0);
  assert.equal(h.closed(), 0);
  assert.equal(h.connected(), 1);
  assert.equal(h.ctx.bars[0].time, 10);
});

test("stale response-body rejection is ignored after a successful period switch", async () => {
  const h = harness();
  const body = deferred();
  const old = h.load();
  h.requests[0].resolve({ok: false, status: 400, json: () => body.promise});
  await flush();
  const current = h.load();
  h.requests[1].resolve(success());
  await current;
  body.reject(new Error("body read failed"));
  await old;
  assert.equal(h.timers.size, 0);
  assert.equal(h.closed(), 0);
});

test("stale network failure and stale success leave current state untouched", async () => {
  for (const reject of [false, true]) {
    const h = harness();
    const old = h.load();
    const current = h.load();
    h.requests[1].resolve(success([{time: 20}]));
    await current;
    if (reject) h.requests[0].reject(new Error("network failed"));
    else h.requests[0].resolve(success([{time: 1}]));
    await old;
    assert.equal(h.timers.size, 0);
    assert.equal(h.ctx.bars[0].time, 20);
    assert.equal(h.connected(), 1);
  }
});

test("new user request cancels an old retry and invalidates even an already queued callback", async () => {
  const h = harness();
  const old = h.load();
  h.requests[0].resolve(failure());
  await old;
  const staleTimer = [...h.timers.values()][0].fn;
  const current = h.load();
  assert.equal(h.timers.size, 0);
  h.requests[1].resolve(success());
  await current;
  staleTimer();
  assert.equal(h.requests.length, 2);
  assert.equal(h.closed(), 0);
});

test("pending-history polls are cancelled when the user changes the data source", async () => {
  const h = harness();
  const old = h.load();
  h.requests[0].resolve({ok: true, json: async () => ({pending: true})});
  await old;
  const staleTimer = [...h.timers.values()][0].fn;
  h.ctx.ltf = 5;
  const current = h.load();
  assert.equal(h.timers.size, 0);
  staleTimer();
  assert.equal(h.requests.length, 2);
  h.requests[1].resolve(success());
  await current;
  assert.match(h.requests[1].url, /ltf=5/);
});

test("automatic retries keep their generation and stop at the configured budget", async () => {
  const h = harness();
  h.load();
  const generation = h.ctx.loadGeneration;
  for (let attempt = 0; attempt <= 24; attempt++) {
    h.requests[attempt].resolve(failure());
    await flush();
    assert.equal(h.ctx.loadGeneration, generation);
    if (attempt < 24) h.fire();
  }
  assert.equal(h.requests.length, 25); // 首次加载 + 24 次自动重试。
  assert.equal(h.timers.size, 0);
  assert.equal(h.ctx.retryAttempts, 24);
  const next = h.load();
  assert.equal(h.ctx.retryAttempts, 0);
  h.requests[25].resolve(success());
  await next;
  assert.equal(h.connected(), 1);
});

test("a successful automatic retry clears its error budget", async () => {
  const h = harness();
  const first = h.load();
  h.requests[0].resolve(failure());
  await first;
  h.fire();
  h.requests[1].resolve(success());
  await flush();
  assert.equal(h.ctx.retryAttempts, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.connected(), 1);
});
