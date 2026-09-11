"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { mergeBars, diagonalVolumes, cvdCandle, splitLtf, relativeLevel } = require("../static/data-sync.js");

test("CVD source selects ticks independently of remembered candle granularity", () => {
  for (const seconds of [1, 5, 10, 15, 30]) {
    assert.equal(splitLtf("tick", seconds), 0);
    assert.equal(splitLtf("kline", seconds), seconds);
  }
  assert.equal(splitLtf("kline", 0), 10);
  assert.equal(splitLtf("kline", "10"), 10);
});

test("CVD positive and negative delta use Pine's strict 1.5x relative thresholds", () => {
  for (const kind of ["posd", "negd"]) {
    for (const [ratio, level] of [[2.25, 0], [2.2501, 1], [3.75, 1], [4.5778, 2], [5.25, 2], [5.2501, 3]]) {
      assert.equal(relativeLevel(kind, ratio, [1.5, 2.5, 3.5]), level);
    }
    assert.equal(relativeLevel(kind, null, [1.5, 2.5, 3.5]), 0);
  }
  assert.equal(relativeLevel("vol", 1.5, [1.5, 2.5, 3.5]), 1);
});

test("patches fill gaps, revise old bars, deduplicate and bound the window", () => {
  const result = mergeBars([{time: 1}, {time: 3, buy: 1}], [{time: 2}, {time: 3, buy: 9}, {time: 4}], 3);
  assert.deepEqual(result, [{time: 2}, {time: 3, buy: 9}, {time: 4}]);
});

test("empty footprints accept their first update", () => {
  assert.deepEqual(mergeBars([], [{time: 10, levels: [[101, 10, 0]]}]), [{time: 10, levels: [[101, 10, 0]]}]);
});

test("diagonal comparison uses adjacent price ticks, never the next traded row", () => {
  const levels = [[101, 10, 20], [103, 30, 40]];
  assert.deepEqual(diagonalVolumes(levels, 1), [{sellBelow: 0, buyAbove: 0}, {sellBelow: 0, buyAbove: 0}]);
  assert.deepEqual(diagonalVolumes(levels, 2), [{sellBelow: 0, buyAbove: 30}, {sellBelow: 20, buyAbove: 0}]);
  assert.deepEqual(diagonalVolumes(levels, null), [null, null]);
});

test("decimal price ticks keep adjacency despite binary floating-point rounding", () => {
  assert.deepEqual(diagonalVolumes([[.1, 10, 20], [.2, 30, 40]], .1),
    [{sellBelow: 0, buyAbove: 30}, {sellBelow: 20, buyAbove: 0}]);
});

test("FlowMeter renders an isolated valid CVD bar without a preceding bar", () => {
  assert.deepEqual(cvdCandle({time: 30, cvd: 15, delta: 5}),
    {time: 30, open: 10, high: 15, low: 10, close: 15});
  assert.deepEqual(cvdCandle({time: 60, cvd: -3, delta: -8, cvdOpen: 5}),
    {time: 60, open: 5, high: 5, low: -3, close: -3});
  assert.equal(cvdCandle({time: 90, cvd: null, delta: null}), null);
});
