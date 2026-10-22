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
  const requests = [], timers = new Map(), ranges = [];
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
    olderRequest: null, olderTimer: null, olderHasMore: false, olderError: "", restoringRange: false,
    historyHint: {}, chart: {timeScale: () => timeScale}, onRangeChange: () => {},
    setTimeout: (fn) => { timers.set(++sequence, fn); return sequence; },
    clearTimeout: (id) => timers.delete(id),
    renderAll: () => { renders++; },
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, signal: options.signal, resolve, reject})),
  });
  vm.runInContext(loader, ctx);
  return {ctx, requests, timers, ranges, renders: () => renders};
}

function response(bars, historyRevision = 1) {
  return {ok: true, json: async () => ({symbol: "DCE.p2701", contract: "DCE.p2701", tf: 300, ltf: 15,
                                      bars, historyRevision})};
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

test("a failed history refresh retains retry state without repeatedly querying the server", async () => {
  const h = harness();
  h.ctx.acceptHistoryRevision({historyRevision: 1});
  const pending = h.ctx.refreshLoadedHistory();
  h.requests[0].reject(new Error("temporary failure"));
  await pending;
  assert.equal(h.ctx.historyDirty, true);
  assert.match(h.ctx.olderError, /CVD 历史同步失败/);
  assert.equal(h.timers.size, 0);
  h.ctx.olderError = "";
  h.ctx.queueHistoryRefresh();
  assert.equal(h.timers.size, 1);
});
