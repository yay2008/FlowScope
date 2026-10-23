"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const FlowData = require("../static/data-sync.js");
const source = fs.readFileSync(path.join(__dirname, "../static/chart-view.js"), "utf8");
const loader = source.slice(source.indexOf("function resetOlderHistory()"), source.indexOf("function clearLoadRetry()"));

function harness() {
  const requests = [], timers = new Map(), ranges = [], delays = [];
  let sequence = 0, renders = 0;
  const timeScale = {getVisibleLogicalRange: () => ({from: 100, to: 200}),
                     setVisibleLogicalRange: (value) => ranges.push(value)};
  const ctx = vm.createContext({
    AbortController, encodeURIComponent, FlowData,
    symbol: "DCE.p2701", dataContract: "DCE.p2701", tf: 300, ltf: 15, barsLtf: 15,
    bars: [{time: 300, cvdOpen: 0, cvd: 2, delta: 2},
           {time: 600, cvdOpen: 2, cvd: 5, delta: 3}],
    cfg: {}, isCrypto: false, historyRevision: 0, historyDirty: false, historyBackfill: null,
    historyRefresh: null, historyRefreshTimer: null, loadGeneration: 1, loadController: null,
    historyRefreshFailures: 0, historyRefreshRetryAt: 0, HISTORY_REFRESH_LIMIT: 2000, LOADED_BARS_MAX: 5000,
    console: {warn: () => {}},
    olderRequest: null, olderTimer: null, olderHasMore: false, olderError: "", restoringRange: false,
    historyHint: {}, chart: {timeScale: () => timeScale}, onRangeChange: () => {},
    setTimeout: (fn, delay) => { delays.push(delay); timers.set(++sequence, fn); return sequence; },
    clearTimeout: (id) => timers.delete(id),
    renderAll: () => { renders++; },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, signal: options.signal, resolve, reject})),
  });
  vm.runInContext(loader, ctx);
  return {ctx, requests, timers, ranges, delays, renders: () => renders};
}

function response(bars, historyRevision = 1, hasMore = false) {
  return {ok: true, json: async () => ({symbol: "DCE.p2701", contract: "DCE.p2701", tf: 300, ltf: 15,
                                      bars, historyRevision, hasMore})};
}

test("history backfill refreshes the loaded interval and preserves viewport and newer live trades", async () => {
  const h = harness();
  h.ctx.acceptHistoryRevision({historyRevision: 1, historyBackfill: {limited: true, reason: "limit"}});
  const pending = h.ctx.refreshLoadedHistory();
  assert.match(h.requests[0].url, /since=300&before=900&limit=2000/);
  h.ctx.bars = [...h.ctx.bars.slice(0, 1), {time: 600, close: 150, delta: 7, cvdOpen: 2, cvd: 9},
                {time: 900, delta: -3, cvdOpen: 9, cvd: 6}];
  h.requests[0].resolve(response([{time: 300, delta: 2, cvdOpen: 100, cvd: 102},
                                  {time: 600, close: 120, delta: 3, cvdOpen: 102, cvd: 105}]));
  await pending;
  assert.equal(h.ctx.bars[0].cvd, 102);
  assert.equal(h.ctx.bars[1].close, 150);
  assert.equal(h.ctx.bars[1].cvd, 109);
  assert.equal(h.ctx.bars[2].cvd, 106);
  assert.equal(h.ctx.historyBackfill.limited, true);
  assert.equal(h.ranges.length, 0, "原时间范围保持不动");
  assert.equal(h.renders(), 1);
  assert.equal(h.timers.size, 0, "相同历史版本不反复刷新");
});

test("a source change cancels backfill refresh and discards its late response", async () => {
  const h = harness();
  h.ctx.acceptHistoryRevision({historyRevision: 1});
  const pending = h.ctx.refreshLoadedHistory();
  h.ctx.resetOlderHistory();
  h.ctx.loadGeneration++;
  h.ctx.bars = [{time: 1200, cvd: 99}];
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].resolve(response([{time: 300, cvd: 102}]));
  await pending;
  assert.equal(h.ctx.bars.length, 1);
  assert.equal(h.ctx.bars[0].time, 1200);
  assert.equal(h.renders(), 0);
});

test("backfill notification waits for an in-flight older page", () => {
  const h = harness();
  h.ctx.olderRequest = {};
  h.ctx.acceptHistoryRevision({historyRevision: 1});
  h.ctx.queueHistoryRefresh();
  assert.equal(h.timers.size, 0);
  h.ctx.olderRequest = null;
  h.ctx.queueHistoryRefresh();
  h.ctx.queueHistoryRefresh();
  assert.equal(h.timers.size, 1);
});

test("a failed history refresh retries by itself with backoff and does not block older pages", async () => {
  const h = harness();
  h.ctx.acceptHistoryRevision({historyRevision: 1});
  let pending = h.ctx.refreshLoadedHistory();
  h.requests[0].reject(new Error("temporary failure"));
  await pending;
  assert.equal(h.ctx.historyDirty, true);
  assert.equal(h.ctx.olderError, "", "不占用翻页的错误状态");
  assert.equal(h.timers.size, 1, "自动安排重试");
  assert.ok(h.delays.at(-1) >= 1900, "退避后再试, 不立刻反复请求");
  h.timers.clear();
  h.ctx.historyRefreshTimer = null;   // 当作重试计时到点
  pending = h.ctx.refreshLoadedHistory();
  h.requests[1].reject(new Error("still failing"));
  await pending;
  assert.ok(h.delays.at(-1) >= 3900, "连续失败间隔加长");
  h.timers.clear();
  h.ctx.historyRefreshTimer = null;   // 当作重试计时到点
  pending = h.ctx.refreshLoadedHistory();
  h.requests[2].resolve(response([{time: 300, delta: 2, cvdOpen: 0, cvd: 2}]));
  await pending;
  assert.equal(h.ctx.historyRefreshFailures, 0);
  assert.equal(h.ctx.historyDirty, false);
});

test("refresh of more loaded bars than one request allows walks back until the first bar", async () => {
  const h = harness();
  h.ctx.bars = Array.from({length: 5}, (_, i) => ({time: 300 * (i + 1), delta: 1, cvdOpen: i, cvd: i + 1}));
  h.ctx.acceptHistoryRevision({historyRevision: 1});
  const pending = h.ctx.refreshLoadedHistory();
  assert.match(h.requests[0].url, /since=300&before=1800&limit=2000/);
  h.requests[0].resolve(response([900, 1200, 1500].map((time) => ({time, delta: 1, cvdOpen: 0, cvd: 1})), 1, true));
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(h.requests[1].url, /since=300&before=900&limit=2000/);
  h.requests[1].resolve(response([300, 600].map((time) => ({time, delta: 1, cvdOpen: 10, cvd: 11})), 1, true));
  await pending;
  assert.equal(h.requests.length, 2, "取到最早一根就停");
  assert.equal(h.ctx.bars[0].cvd, 11);
  assert.equal(h.ctx.bars[2].cvd, 1);
});

test("crypto history revisions also refresh paged bars, futures tick mode does not", () => {
  const h = harness();
  h.ctx.isCrypto = true;
  h.ctx.ltf = 0;
  h.ctx.barsLtf = 0;
  h.ctx.acceptHistoryRevision({historyRevision: 4});
  assert.equal(h.ctx.historyDirty, true);
  const futures = harness();
  futures.ctx.ltf = 0;
  futures.ctx.barsLtf = 0;
  futures.ctx.acceptHistoryRevision({historyRevision: 4});
  assert.equal(futures.ctx.historyDirty, false);
});

test("loaded bars beyond the cap are trimmed from the left, not while the user is looking at them", () => {
  const h = harness();
  h.ctx.LOADED_BARS_MAX = 5;
  h.ctx.bars = Array.from({length: 7}, (_, i) => ({time: 300 * (i + 1)}));
  const next = [...h.ctx.bars, {time: 2400}];
  const trimmed = h.ctx.trimLoaded(next);
  assert.deepEqual(trimmed.map((bar) => bar.time), [1200, 1500, 1800, 2100, 2400]);
  assert.equal(h.ctx.olderHasMore, true, "裁掉的还能再往左翻回来");
  h.ctx.renderWithHistory(trimmed);
  assert.deepEqual({...h.ranges.at(-1)}, {from: 97, to: 197}, "视口跟着左边裁掉的根数平移");
  const viewing = harness();
  viewing.ctx.LOADED_BARS_MAX = 5;
  viewing.ctx.chart = {timeScale: () => ({getVisibleLogicalRange: () => ({from: 2, to: 6})})};
  assert.equal(viewing.ctx.trimLoaded(next), next);
});
