"use strict";
/* indicators.js 的纯函数回归: 不需要 DOM 与图表, 直接在 Node 里 require。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { rollingSma, rollingZ, linreg, ema, derive, levelOf, BAND } = require("../static/indicators.js");

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
