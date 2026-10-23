"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { mergeBars, snapshotGap, mergeCvdHistory, diagonalVolumes, footprintRows, footprintMultiple, footprintThresholds, cvdCandle, splitLtf, relativeLevel,
        parseSymbolParts, formatOpenInterest, formatAmount, liquidityLabel,
        watchLabel, formatPrice,
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

test("backfilled CVD rebases the live tail without reverting newer trades", () => {
  const live = [{ time: 1, delta: 2, cvd: 2 },
                { time: 2, close: 150, volume: 10, delta: 7, cvdOpen: 2, cvd: 9 },
                { time: 3, delta: -3, cvdOpen: 9, cvd: 6 }];
  const history = [{ time: 1, delta: 2, cvdOpen: 100, cvd: 102 },
                   { time: 2, close: 120, volume: 5, delta: 3, cvdOpen: 102, cvd: 105 }];
  const next = mergeCvdHistory(live, history, 2);
  assert.equal(next[0].cvd, 102);
  assert.equal(next[1].close, 150);
  assert.equal(next[1].volume, 10);
  assert.equal(next[1].delta, 7);
  assert.equal(next[1].cvd, 109);
  assert.equal(next[2].cvdOpen, 109);
  assert.equal(next[2].cvd, 106);
});

test("backfill synchronization leaves unknown live delta missing", () => {
  const next = mergeCvdHistory([{ time: 2, delta: null, cvd: null }, { time: 3, delta: 4, cvd: 4 }],
                               [{ time: 2, delta: 1, cvdOpen: 100, cvd: 101 }], 2);
  assert.equal(next[0].cvd, null);
  assert.equal(next[1].cvd, 104);
});

test("loaded candle history survives live updates without an implicit 800-bar cap", () => {
  const older = Array.from({ length: 1600 }, (_, time) => ({ time, close: 1 }));
  const result = mergeBars(older, [{ time: 1599, close: 2 }, { time: 1600, close: 3 }]);
  assert.equal(result.length, 1601);
  assert.equal(result[0].time, 0);
  assert.equal(result[1599].close, 2);
  assert.equal(mergeBars(older, [], 800).length, 800, "足迹等显式限制仍生效");
});

test("diagonal comparison uses adjacent price ticks, never the next traded row", () => {
  const levels = [[101, 10, 20], [103, 30, 40]];
  assert.deepEqual(diagonalVolumes(levels, 1), [{sellBelow: 0, buyAbove: 0}, {sellBelow: 0, buyAbove: 0}]);
  assert.deepEqual(diagonalVolumes(levels, 2), [{sellBelow: 0, buyAbove: 30}, {sellBelow: 20, buyAbove: 0}]);
  assert.deepEqual(diagonalVolumes(levels, null), [null, null]);
});

test("footprint rows merge adjacent bands onto the lowest price and keep every side", () => {
  const levels = [[100.1, 1, 2, 0], [100.2, 3, 4, 1], [100.5, 1, 0, 0]];
  assert.deepEqual(footprintRows(levels, .1, 5), [[100, 4, 6, 1], [100.5, 1, 0, 0]]);
  assert.equal(footprintRows(levels, .1, 1), levels, "倍数 1 原样返回");
  assert.deepEqual(footprintRows([[3, 1, 0]], 1, 2), [[2, 1, 0, 0]], "缺未知量的旧格式补 0");
});

test("display multiple divides the base band and never jumps more than 2.5x between choices", () => {
  assert.equal(footprintMultiple(10, 3), 10, "底档已经够高就不合");
  assert.equal(footprintMultiple(10, 23), 50, "25 不能被底档 10 整除");
  assert.equal(footprintMultiple(5, 23), 25, "整数价位序列优先给出 25");
  assert.equal(footprintMultiple(2, 2.2), 4, "底档 2 个价位时 4 补在 2 和 10 之间(黄金 5m: 0.04 -> 0.08 -> 0.2)");
  assert.equal(footprintMultiple(20, 230), 400);
  assert.equal(footprintMultiple(3, 7), 15, "多所公约数这类不在序列上的底档按它的 1-2-5 倍");
  for (const base of [1, 2, 3, 5, 10, 20, 50]) {
    let previous = base;
    for (let needed = base; needed < base * 1000; needed *= 1.1) {
      const multiple = footprintMultiple(base, needed);
      assert.equal(multiple % base, 0, `${base} ${needed}`);
      assert.ok(multiple >= needed && multiple <= Math.max(base, needed * 2.5), `${base} ${needed} -> ${multiple}`);
      assert.ok(multiple >= previous, "需要的行高越大, 档只会越粗");
      previous = multiple;
    }
  }
});

test("decimal price ticks keep adjacency despite binary floating-point rounding", () => {
  assert.deepEqual(diagonalVolumes([[.1, 10, 20], [.2, 30, 40]], .1),
    [{sellBelow: 0, buyAbove: 30}, {sellBelow: 20, buyAbove: 0}]);
});

const footprintBar = (time, quantity, coverage = "complete") => ({
  time, coverage, levels: [[100, quantity, quantity, 0]],
});

test("crypto footprint minimum scales with coin quantities, including fractions of one coin", () => {
  const base = [footprintBar(1, 2), footprintBar(2, 4), footprintBar(3, 6)];
  const reference = footprintThresholds(base);
  for (const scale of [0.000001, 1, 1000000]) {
    const scaled = base.map(bar => ({ ...bar,
      levels: bar.levels.map(([price, buy, sell]) => [price, buy * scale, sell * scale, 0]),
    }));
    for (const [time, minimum] of footprintThresholds(scaled)) {
      const expected = reference.get(time) * scale;
      assert.ok(Math.abs(minimum - expected) <= expected * 1e-12);
    }
  }
  assert.ok(footprintThresholds([footprintBar(1, .0001)]).get(1) < 1);
});

test("crypto footprint baseline excludes current and future volume", () => {
  const history = [footprintBar(1, 2), footprintBar(2, 4), footprintBar(3, 6)];
  const original = footprintThresholds([...history, footprintBar(4, 100)]);
  const revised = footprintThresholds([...history, footprintBar(4, 1000000), footprintBar(5, 1e9)]);
  assert.equal(original.get(4), 4);
  assert.equal(revised.get(4), 4, "当前根放量不改变自己的历史门槛");
  for (const bar of history) assert.equal(revised.get(bar.time), original.get(bar.time));
});

test("partial coverage, empty sides and unknown volume cannot dilute the crypto baseline", () => {
  const result = footprintThresholds([
    footprintBar(1, 4), footprintBar(2, 1e9, "partial"), footprintBar(3, 0),
    { time: 4, coverage: "complete", levels: [[100, NaN, Infinity, 1e9], [101, 0, 0, 1e9]] },
    footprintBar(5, .0001),
  ]);
  assert.equal(result.get(5), 4);
  assert.equal(footprintThresholds([{ time: 1, levels: [] }]).get(1), null);
  assert.equal(footprintThresholds([
    footprintBar(1, 1000, "partial"), footprintBar(2, .25),
  ]).get(2), .25, "没有完整历史时以自身有效半格均量启动");
});

test("crypto footprint follows the latest twenty complete bars and resists a single volume spike", () => {
  const history = Array.from({ length: 40 }, (_, i) => footprintBar(i, i < 20 ? 1 : 100));
  const result = footprintThresholds([...history, footprintBar(40, 1e9), footprintBar(41, 1)]);
  assert.equal(result.get(40), 100, "更早的低量不再参与近期门槛");
  assert.equal(result.get(41), 100, "单根异常放量不抬高典型成交量");
});

test("crypto footprint historical corrections and new price grouping rebuild their own baseline", () => {
  const history = [footprintBar(1, 1), footprintBar(2, 3), footprintBar(3, 2)];
  assert.equal(footprintThresholds(history).get(3), 2);
  assert.equal(footprintThresholds([footprintBar(1, 5), ...history.slice(1)]).get(3), 4);
  assert.equal(footprintThresholds([footprintBar(3, .01)]).get(3), .01,
    "新币种、周期或分档不沿用上一次调用的量级");
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

test("turnover amount is abbreviated for the watchlist liquidity line", () => {
  assert.equal(formatAmount(23124010140), "231.2亿");
  assert.equal(formatAmount(13300924740), "133.0亿");
  assert.equal(formatAmount(91240000), "9124.0万");
  assert.equal(formatAmount(4321), "4321");
  // 缺数据或非正数不显示, 免得面板出现「额 0」这种噪音
  assert.equal(formatAmount(null), "");
  assert.equal(formatAmount(undefined), "");
  assert.equal(formatAmount(0), "");
  assert.equal(formatAmount(-5), "");
  assert.equal(formatAmount("abc"), "");
});

test("liquidity line combines volume and amount and degrades gracefully", () => {
  assert.equal(liquidityLabel({ volume: 556133, amount: 23124010140 }), "量 55.6万 · 额 231.2亿");
  assert.equal(liquidityLabel({ volume: 426722 }), "量 42.7万");
  assert.equal(liquidityLabel({ amount: 13300924740 }), "额 133.0亿");
  assert.equal(liquidityLabel({}), "");          // 还没轮到第一次轮询: 不占位置
  assert.equal(liquidityLabel(null), "");
  assert.equal(liquidityLabel({ volume: 0, amount: null }), "量 0");
});

test("contract picker labels show the main month and yesterday's open interest", () => {
  assert.equal(productLabel({name: "燃油", mainSymbol: "SHFE.fu2611"}), "燃油 · fu2611");
  assert.equal(productLabel({name: "燃油", mainSymbol: ""}), "燃油");
  assert.equal(productLabel({productId: "fu"}), "fu");
  assert.equal(monthLabel({name: "燃油2611", openInterest: 428100}), "燃油2611 · 昨仓 42.8万");
  assert.equal(monthLabel({name: "燃油2611", openInterest: null}), "燃油2611");
  assert.equal(monthLabel({symbol: "SHFE.fu2611"}), "SHFE.fu2611");
});

test("重连快照接不上已有 K 线时才算缺口", () => {
  const bars = [{ time: 30 }, { time: 60 }];
  assert.equal(snapshotGap(bars, [{ time: 60 }, { time: 90 }]), false);
  assert.equal(snapshotGap(bars, [{ time: 90 }]), true);
  assert.equal(snapshotGap([], [{ time: 90 }]), false);
  assert.equal(snapshotGap(bars, []), false);
});
