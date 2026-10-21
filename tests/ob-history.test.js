"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { deriveObZones, createObTracker, normalizeObOptions } = require("../static/indicators.js");

const series = ["zUp", "zDn", "bullCreate", "bearCreate"];
const candle = (i, open, close) => ({ time: 1700000000 + i * 30, open, close,
  high: Math.max(open, close) + 1, low: Math.min(open, close) - 1,
  buy: 80, sell: 20, unknown: 0, coverage: "complete" });
function clippingBars(n = 901) {
  let price = 100;
  return Array.from({ length: n }, (_, i) => {
    const after = [100, 550].find((at) => i > at && i <= at + 40);
    const body = [100, 550].includes(i) ? 20 : after != null ? ((i - after) % 2 ? -1 : 0) : (i % 2 ? -1 : 1);
    const b = candle(i, price, price + body); price = b.close; return b;
  });
}
function walk(n) {
  let seed = 7, price = 1000;
  return Array.from({ length: n }, (_, i) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const body = i % 89 === 0 ? (i % 178 ? -40 : 40) : (seed % 21 - 10) / 10;
    const b = candle(i, price, price + body); price = b.close; return b;
  });
}
function projected(bars, from, options = {}) {
  const full = deriveObZones(bars, options);
  return { zones: full.zones.filter((z) => z.end >= from).map((z) => ({ ...z,
    start: z.start - from, created: z.created - from, end: z.end - from,
    startTime: bars[z.start].time, createdTime: bars[z.created].time })),
    ...Object.fromEntries(series.map((name) => [name, full[name].slice(from)])),
    historyStart: bars[0].time, historyCount: bars.length };
}

test("OB 历史: 第801根裁剪不能复活曾被早期区域挡住的信号", () => {
  const bars = clippingBars(801), tracker = createObTracker();
  const full = deriveObZones(bars), cropped = deriveObZones(bars.slice(1));
  assert.deepEqual(full.zones.map((z) => [z.created, z.status]), [[100, "expired"]]);
  assert.deepEqual(cropped.zones.map((z) => [z.created + 1, z.status]), [[550, "active"]], "确实复现原裁剪问题");
  tracker.update(bars.slice(0, 800));
  const result = tracker.update(bars.slice(1));
  assert.deepEqual(result, projected(bars, 1));
  assert.deepEqual(tracker.update(bars.slice(1)), result, "重复重连快照不改变结果");
  assert.equal(result.historyCount, 801);
});

test("OB 历史: 连续滚动、多根补齐和重连与同起点完整历史一致", () => {
  const bars = walk(2400);
  for (const options of [{}, { zLen: 20 }, { maxAge: 2000 }, { preventOverlap: false }, { zLen: 1000 }]) {
    const tracker = createObTracker();
    tracker.update(bars.slice(0, 800), options);
    for (let end = 801; end <= bars.length; end += end % 101 === 0 ? 23 : 1) {
      const from = end - 800, visible = bars.slice(from, end);
      const result = tracker.update(visible, options);
      if (end % 47 === 0 || end === bars.length) {
        assert.deepEqual(result, projected(bars.slice(0, end), from, options), `end=${end}, ${JSON.stringify(options)}`);
        assert.deepEqual(tracker.update(visible.map((b) => ({ ...b })), options), result, "重连不能重置预热/区域状态");
      }
    }
    assert.deepEqual(tracker.update(bars.slice(-800), options), projected(bars, bars.length - 800, options));
  }
});

test("OB 历史: 实时末根的创建和失效均能回滚，之后推进仍一致", () => {
  const bars = clippingBars(101), tracker = createObTracker();
  assert.equal(tracker.update(bars).zones.length, 1);
  const quiet = [...bars.slice(0, -1), candle(100, bars[100].open, bars[100].open + 1)];
  assert.equal(tracker.update(quiet).zones.length, 0);
  assert.deepEqual(tracker.update(bars), projected(bars, 0));
  const bottom = deriveObZones(bars).zones[0].bottom;
  const broken = [...bars, candle(101, bottom - 1, bottom - 1), candle(102, bottom - 2, bottom - 2)];
  assert.equal(tracker.update(broken).zones[0].status, "mitigated");
  broken[102] = candle(102, bottom + 5, bottom + 5);
  assert.equal(tracker.update(broken).zones[0].status, "active");
  assert.deepEqual(tracker.update([...broken, candle(103, bottom + 5, bottom + 6)]),
    projected([...broken, candle(103, bottom + 5, bottom + 6)], 0));
});

test("OB 历史: 旧根修订、补更早历史与改参数从保留起点重放", () => {
  const bars = walk(1400), tracker = createObTracker();
  tracker.update(bars.slice(0, 800));
  tracker.update(bars.slice(600));
  bars[700] = candle(700, bars[700].open, bars[700].open + 60);
  assert.deepEqual(tracker.update(bars.slice(600)), projected(bars, 600));
  const options = { zLen: 70, maxAge: 900, preventOverlap: false };
  assert.deepEqual(tracker.update(bars.slice(600), options), projected(bars, 600, options));
  const late = createObTracker();
  late.update(bars.slice(500));
  assert.deepEqual(late.update(bars), projected(bars, 0), "补早期历史时替换基准，不能重复累计");
  assert.deepEqual(tracker.update(bars.slice(600, 1398), options), projected(bars.slice(0, 1398), 600, options), "快照缩短末尾也能回滚");
});

test("OB 历史: 换买卖口径不丢价格，画外锚点只把无法重取的量标缺失", () => {
  const bars = clippingBars(), options = { maxAge: 2000 }, tracker = createObTracker();
  tracker.update(bars.slice(0, 800), options, 0);
  const before = tracker.update(bars.slice(-800), options, 0);
  assert.ok(before.zones[0].start < 0, "锚点已在显示窗口之外");
  assert.equal(before.zones[0].status, "active");
  const changed = bars.map((b, i) => i < 101 ? { ...b, buy: null, sell: null, unknown: null, coverage: "missing" }
    : { ...b, buy: 30, sell: 70 });
  const after = tracker.update(changed.slice(-800), options, 15);
  assert.deepEqual(after, projected(changed, 101, options));
  assert.equal(after.zones[0].createdTime, before.zones[0].createdTime);
  assert.equal(after.zones[0].buy, null);
});

test("OB 历史: 重置只保留新图数据，原版参数没有新增算法选项", () => {
  const tracker = createObTracker(), bars = clippingBars();
  tracker.update(bars);
  tracker.reset();
  assert.deepEqual(tracker.update(bars.slice(-800)), projected(bars.slice(-800), 0));
  assert.equal(tracker.update([]).historyCount, 0);
  assert.deepEqual(Object.keys(normalizeObOptions()), ["zLen", "maxAge", "preventOverlap", "bullColor", "bearColor"]);
});
