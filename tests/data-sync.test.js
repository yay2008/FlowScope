"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { mergeBars, diagonalVolumes, cvdCandle, splitLtf, relativeLevel,
        parseSymbolParts, formatOpenInterest, watchLabel, formatPrice,
        formatChangePct, changeClass, productLabel, monthLabel } =
  require("../static/data-sync.js");

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

test("contract picker maps main-continuous and month symbols to the same variety", () => {
  assert.deepEqual(parseSymbolParts("KQ.m@SHFE.fu"), {exchange: "SHFE", product: "fu", isCont: true});
  assert.deepEqual(parseSymbolParts("KQ.m@dce.i"), {exchange: "DCE", product: "i", isCont: true});
  assert.deepEqual(parseSymbolParts("SHFE.fu2611"), {exchange: "SHFE", product: "fu", isCont: false});
  assert.deepEqual(parseSymbolParts("CZCE.TA701"), {exchange: "CZCE", product: "TA", isCont: false});
});

test("unrecognised symbols fall back to the custom contract entry", () => {
  for (const raw of ["KQ.i@SHFE.fu", "KQ.m@SHFE", "SHFE.fu2611x", "", null, undefined, "fu"]) {
    assert.equal(parseSymbolParts(raw), null);
  }
});

test("open interest is abbreviated for the month dropdown", () => {
  assert.equal(formatOpenInterest(211240), "21.1万");
  assert.equal(formatOpenInterest(3489), "3489");
  assert.equal(formatOpenInterest(150000000), "1.50亿");
  assert.equal(formatOpenInterest(null), "0");
});

test("watchlist rows label main-continuous contracts with their main month", () => {
  assert.equal(watchLabel({name: "燃油主连", insClass: "CONT", mainSymbol: "SHFE.fu2611"}), "燃油 · fu2611");
  assert.equal(watchLabel({name: "燃油主连", insClass: "CONT", mainSymbol: ""}), "燃油");
  assert.equal(watchLabel({name: "燃油2611", insClass: "FUTURE"}), "燃油2611");
  assert.equal(watchLabel({symbol: "KQ.m@SHFE.fu"}), "KQ.m@SHFE.fu");
  assert.equal(watchLabel(null), "");
});

test("watchlist price and change formatting keeps a placeholder for missing data", () => {
  assert.equal(formatPrice(4412, 0), "4412");
  assert.equal(formatPrice(712.25, 2), "712.25");
  assert.equal(formatPrice(null, 1), "—");
  assert.equal(formatChangePct(1.234), "+1.23%");
  assert.equal(formatChangePct(-1.8), "-1.80%");
  assert.equal(formatChangePct(0), "0.00%");
  assert.equal(formatChangePct(null), "");
  assert.equal(changeClass(2), "up");
  assert.equal(changeClass(-2), "down");
  assert.equal(changeClass(0), "");
  assert.equal(changeClass(null), "");
});

test("contract picker labels show the main month and yesterday's open interest", () => {
  assert.equal(productLabel({name: "燃油", mainSymbol: "SHFE.fu2611"}), "燃油 · fu2611");
  assert.equal(productLabel({name: "燃油", mainSymbol: ""}), "燃油");
  assert.equal(productLabel({productId: "fu"}), "fu");
  assert.equal(monthLabel({name: "燃油2611", openInterest: 428100}), "燃油2611 · 昨仓 42.8万");
  assert.equal(monthLabel({name: "燃油2611", openInterest: null}), "燃油2611");
  assert.equal(monthLabel({symbol: "SHFE.fu2611"}), "SHFE.fu2611");
});
