"use strict";
/* indicators.js 的纯函数回归: 不需要 DOM 与图表, 直接在 Node 里 require。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { rollingSma, rollingZ, linreg, ema, derive, levelOf, BAND, volumeProfile } = require("../static/indicators.js");

test("rollingSma / rollingZ: 窗口含 null 输出 null", () => {
  const v = [1, 2, 3, null, 5, 6, 7, 8];
  assert.deepEqual(rollingSma(v, 3), [null, null, 2, null, null, null, 6, 7]);
  assert.ok(Math.abs(rollingZ(v, 3)[2] - Math.sqrt(1.5)) < 1e-12);
});

test("linreg: 直线序列的回归末点等于自身; ema 首个非 null 播种", () => {
  const line = Array.from({ length: 30 }, (_, i) => 100 + 2 * i);
  const out = linreg(line, 21);
  assert.equal(out[19], null);
  assert.ok(Math.abs(out[29] - line[29]) < 1e-9);
  assert.deepEqual(ema([null, 4, 4], 3), [null, 4, 4]);
});

test("derive / levelOf: 无状态调用, SMA 缓存随 derived 重建", () => {
  const cfg = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50 };
  const bars = Array.from({ length: 120 }, (_, i) => ({
    time: 1700000000 + i * 30, open: 100, high: 101, low: 99, close: 100 + (i % 2),
    volume: 100, buy: 60, sell: 40, delta: 20 }));
  const d = derive(bars, cfg, BAND.kDefault);
  assert.equal(d.emaLines.length, 4);
  assert.equal(levelOf(d, cfg, "SMA", "vol", 119), 0);
  assert.ok(d.smaN, "SMA 口径首次使用后缓存挂在 derived 上");
  assert.equal(derive(bars, cfg, BAND.kDefault).smaN, undefined);
});

// ---------- 固定范围成交量分布 ----------

const near = (a, b) => Math.abs(a - b) < 1e-9;

test("volumeProfile: 按价位格子摊量, 买/卖/未知分开; 价值区加上下一行会超过 70% 就停", () => {
  const bars = [
    { high: 103, low: 100, volume: 40, buy: 30, sell: 10 },   // 100~103 四个价位, 每个 10
    { high: 102, low: 102, volume: 20, buy: 5, sell: 5 },     // 只在 102, 另有 10 判不出方向
    { high: 107, low: 104, volume: 8, buy: null, sell: null }, // 没有买卖量: 全算未知
  ];
  // 8 个价位要 4 行: 每行 2 个价位正好 4 行(每行 3 个只有 3 行)
  const r = volumeProfile(bars, 0, 2, { rows: 4, step: 1 });
  assert.deepEqual(r.rows.map((row) => [row.lo, row.hi, row.price]),
                   [[99.5, 101.5, 100.5], [101.5, 103.5, 102.5], [103.5, 105.5, 104.5], [105.5, 107.5, 106.5]]);
  assert.deepEqual(r.rows.map((row) => [row.buy, row.sell, row.unknown, row.total]),
                   [[15, 5, 0, 20], [20, 10, 10, 40], [0, 0, 4, 4], [0, 0, 4, 4]]);
  assert.deepEqual([r.total, r.buy, r.sell, r.maxTotal, r.count], [68, 35, 15, 40, 3]);
  // 目标 47.6: POC 40, 下一步下面那行(20)比上面(4)大, 加上就 60 超了 —— 停, 价值区只有 POC 一行
  assert.deepEqual([r.poc, r.vaLow, r.vaHigh, r.pocPrice, r.vah, r.val], [1, 1, 1, 102.5, 103, 102]);
});

test("volumeProfile: 行数取离要求更近的那个; 量一样时 POC 取中间那行, 价值区相等取离 POC 近的、再相等取上面的", () => {
  // 100 个价位、要 24 行: 每行 4 个价位是 25 行, 5 个是 20 行 —— 取 4
  const r = volumeProfile([{ high: 1099, low: 1000, volume: 100, buy: 50, sell: 50 }], 0, 0, { step: 1 });
  assert.equal(r.rows.length, 25);
  assert.ok(r.rows.every((row) => near(row.total, 4)));
  assert.equal(r.poc, 12);
  // 目标 70: POC 之外再加 16 行(上下轮流, 先上), 再加一行就 72
  assert.deepEqual([r.vaLow, r.vaHigh], [4, 20]);
  assert.deepEqual([r.val, r.vah], [1016, 1083]);
});

test("volumeProfile: 不给价格步长时按连续价格均匀切; 最高 = 最低的 bar 整根落进一行; 只统计 from..to", () => {
  const bars = [
    { high: 50, low: 40, volume: 999 },                         // 不在区间里
    { high: 20, low: 10, volume: 10, buy: 6, sell: 4 },
    { high: 20, low: 20, volume: 3, buy: 3, sell: 0 },
  ];
  const r = volumeProfile(bars, 1, 2, { rows: 2 });
  assert.deepEqual(r.rows.map((row) => [row.lo, row.hi, row.buy, row.sell, row.total]),
                   [[10, 15, 3, 2, 5], [15, 20, 6, 2, 8]]);
  assert.deepEqual([r.poc, r.pocPrice, r.vah, r.val], [1, 17.5, 20, 15]);
  const flat = volumeProfile([{ high: 7, low: 7, volume: 5 }, { high: 7, low: 7, volume: 0 }], 0, 1);
  assert.deepEqual([flat.rows.length, flat.rows[0].unknown, flat.count, flat.pocPrice], [1, 5, 1, 7]);
  assert.equal(volumeProfile(bars, 5, 9), null, "区间里没有 bar");
});
