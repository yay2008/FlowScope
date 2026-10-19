const test = require("node:test");
const assert = require("node:assert/strict");
const { aggregateBars, trainingFlow, trainingChartData, emaPlotData, trainingTradeMarkers, decimals, timeText } = require("../static/training.js");
const indicators = require("../static/indicators.js");

test("five-minute bar only contains revealed minute bars", () => {
  const first = Object.freeze({ time: 600, open: 100, high: 103, low: 99, close: 102, volume: 10 });
  const second = Object.freeze({ time: 660, open: 102, high: 110, low: 101, close: 108, volume: 15 });
  assert.deepEqual(aggregateBars([first]), [first]);
  assert.deepEqual(aggregateBars([first, second]), [{ time: 600, open: 100, high: 110, low: 99, close: 108, volume: 25 }]);
  assert.equal(first.high, 103);
});

test("new five-minute bucket and session gaps do not create fictitious candles", () => {
  const result = aggregateBars([
    { time: 840, open: 100, high: 101, low: 99, close: 100, volume: 5 },
    { time: 900, open: 110, high: 111, low: 109, close: 110, volume: 6 },
    { time: 3600, open: 120, high: 121, low: 119, close: 120, volume: 7 },
  ]);
  assert.deepEqual(result.map(bar => bar.time), [600, 900, 3600]);
  assert.deepEqual(result.map(bar => bar.volume), [5, 6, 7]);
});

test("price precision supports futures ticks", () => {
  assert.equal(decimals(1), 0);
  assert.equal(decimals(.2), 1);
  assert.equal(decimals(.005), 3);
});

test("blind chart formatter hides date without timezone drift", () => {
  const ts = Date.parse("2025-01-06T09:32:00Z") / 1000;
  assert.equal(timeText(ts), "09:32");
  assert.equal(timeText(ts, true), "2025-01-06 09:32");
});

const cfg = { rellen: 20, smalen: 300, zlen: 50, mult: [1.5, 2.5, 3.5],
  colors: { up: "up", down: "down", upLevels: ["up1", "up2", "up3"], downLevels: ["down1", "down2", "down3"] } };
const settings = { mode: "cvd", threshtype: "Z-SCORE", bandK: 2 };
const minute = (time, open, close, volume) => Object.freeze({ time, open, high: Math.max(open, close) + 1, low: Math.min(open, close) - 1, close, volume });
const sample = (count) => Array.from({ length: count }, (_, i) => minute(600 + i * 60,
  100 + Math.sin(i / 4) * 5, 101 + Math.cos(i / 3) * 4, i % 23 === 0 ? 1000 : 10 + i % 17));

test("minute estimates label their source and retain doji volume as unknown", () => {
  const input = Object.freeze([minute(600, 100, 101, 10), minute(660, 101, 99, 7), minute(720, 99, 99, 5)]);
  const flow = trainingFlow(input);
  assert.deepEqual(flow.map(b => [b.buy, b.sell, b.unknown, b.delta, b.cvdOpen, b.cvd]),
    [[10, 0, 0, 10, 0, 10], [0, 7, 0, -7, 10, 3], [0, 0, 5, 0, 3, 3]]);
  assert.ok(flow.every(b => b.coverage === "estimated"));
  assert.ok(input.every(b => b.buy === undefined));
});

test("five-minute CVD sums minute directions and keeps the same cumulative baseline", () => {
  const bars = [minute(600, 100, 105, 10), minute(660, 105, 99, 7), minute(720, 99, 99, 5), minute(900, 99, 102, 4)];
  const result = trainingChartData(bars, 300, cfg, settings);
  assert.deepEqual(result.bars.map(b => [b.buy, b.sell, b.unknown, b.delta, b.cvdOpen, b.cvd]),
    [[10, 7, 5, 3, 0, 3], [4, 0, 0, 4, 3, 7]]);
  assert.equal(result.bars[0].close < result.bars[0].open, true);
  assert.equal(result.suite.candles[0].close, 3, "a falling five-minute candle can have positive estimated delta");
  assert.equal(result.suite.candles[1].open, 3);
  assert.equal(result.bars.at(-1).cvd, trainingFlow(bars).at(-1).cvd);
});

test("all six FlowMeter modes and three thresholds retain only the revealed prefix", () => {
  const bars = sample(420), prefix = bars.slice(0, 340);
  const cutoff = prefix.at(-1).time;
  for (const [mode] of indicators.MODE_CHOICES) {
    for (const [threshtype] of indicators.THRESH_CHOICES) {
      const options = { ...settings, mode, threshtype };
      const partial = trainingChartData(prefix, 60, cfg, options);
      const full = trainingChartData(bars, 60, cfg, options);
      for (const key of ["hist", "histSell", "candles"]) {
        assert.deepEqual(partial.suite[key], full.suite[key].filter(row => row.time <= cutoff), `${mode} / ${threshtype} / ${key}`);
      }
      assert.deepEqual(partial.band, full.band.filter(row => row.time <= cutoff));
      assert.deepEqual(partial.pulse, full.pulse.filter(row => row.time <= cutoff));
      assert.equal(partial.bars.length, prefix.length);
    }
  }
});

test("five-minute partial indicators use only revealed OHLCV, and review rewind restores them", () => {
  const bars = sample(320), prefix = bars.slice(0, 252);
  const before = trainingChartData(prefix, 300, cfg, settings);
  const after = trainingChartData(bars, 300, cfg, settings);
  assert.equal(before.bars.length, 51);
  assert.equal(before.bars.at(-1).volume, bars[250].volume + bars[251].volume);
  assert.equal(before.bars.at(-1).close, bars[251].close);
  assert.notEqual(before.bars.at(-1).close, after.bars[50].close);
  assert.ok(before.band.length > 0);
  assert.deepEqual(trainingChartData(prefix, 300, cfg, settings), before);
});

test("short history leaves FlowWave band and RVOL warmup empty", () => {
  const result = trainingChartData(sample(19), 60, cfg, { ...settings, mode: "rvol" });
  assert.deepEqual(result.band, []);
  assert.deepEqual(result.suite.hist, []);
  assert.deepEqual(result.pulse, []);
  assert.ok(result.derived.lw.wave.every(value => value == null));
});

test("changing FlowWave width changes only the band, without changing signals or trades", () => {
  const bars = sample(80), before = trainingChartData(bars, 60, cfg, { ...settings, bandK: 1.5 });
  const after = trainingChartData(bars, 60, cfg, { ...settings, bandK: 2.5 });
  assert.deepEqual(before.bars, after.bars);
  assert.deepEqual(before.suite, after.suite);
  assert.deepEqual(before.derived.lw, after.derived.lw);
  const a = before.band.at(-1), b = after.band.at(-1);
  assert.ok(Math.abs((b.up - b.mid) / (a.up - a.mid) - 2.5 / 1.5) < 1e-9);
});

test("all training timeframes form their last candle from revealed minutes and rewind without future data", () => {
  const input = Object.freeze(sample(510)), prefix = Object.freeze(input.slice(0, 387));
  for (const tf of [60, 300, 900, 3600, 14400]) {
    const before = trainingChartData(prefix, tf, cfg, settings, tf < 14400 ? 14400 : 0);
    const last = before.bars.at(-1), minutes = prefix.filter(b => b.time >= last.time);
    assert.equal(last.time, Math.floor(prefix.at(-1).time / tf) * tf);
    assert.equal(last.open, minutes[0].open);
    assert.equal(last.high, Math.max(...minutes.map(b => b.high)));
    assert.equal(last.low, Math.min(...minutes.map(b => b.low)));
    assert.equal(last.close, prefix.at(-1).close);
    assert.equal(last.volume, minutes.reduce((sum, b) => sum + b.volume, 0));
    const future = trainingChartData(input, tf, cfg, settings, tf < 14400 ? 14400 : 0);
    before.derived.emaLines.forEach((line, j) => assert.deepEqual(line.slice(0, -1), future.derived.emaLines[j].slice(0, line.length - 1)));
    before.derived.htfEma.forEach((line, j) => assert.deepEqual(line, future.derived.htfEma[j].slice(0, line.length)));
    assert.deepEqual(trainingChartData(prefix, tf, cfg, settings, tf < 14400 ? 14400 : 0), before);
  }
});

test("EMA follows each chart timeframe and only the revealed close of its unfinished candle", () => {
  const input = Array.from({ length: 8 }, (_, i) => minute(600 + i * 60, 100 + i * 10, 100 + i * 10, 1));
  const one = trainingChartData(input.slice(0, 6), 60, cfg, settings);
  const five = trainingChartData(input.slice(0, 6), 300, cfg, settings);
  const assertNear = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);
  for (let j = 0; j < indicators.EMA_PERIODS.length; j++) {
    const alpha = 2 / (indicators.EMA_PERIODS[j] + 1);
    assert.equal(one.derived.emaLines[j][0], 100);
    assertNear(one.derived.emaLines[j][1], 100 + 10 * alpha);
    assertNear(one.derived.emaLines[j][2], 100 + 10 * alpha + (20 - 10 * alpha) * alpha);
    assert.equal(five.derived.emaLines[j][0], 140);
    assertNear(five.derived.emaLines[j][1], 140 + 10 * alpha);
    const later = trainingChartData(input, 300, cfg, settings);
    assertNear(later.derived.emaLines[j][1], 140 + 30 * alpha);
  }
});

test("higher timeframe EMA changes only when a new higher bucket is revealed, also after rewind", () => {
  const input = Array.from({ length: 18 }, (_, i) => minute(i * 60, 100 + i, 100 + i, 1));
  const current = trainingChartData(input.slice(0, 16), 300, cfg, settings, 900);
  const later = trainingChartData(input, 300, cfg, settings, 900);
  assert.deepEqual(current.bars.map(b => b.time), [0, 300, 600, 900]);
  assert.deepEqual(current.derived.htfEma, [[null, null, null, 114], [null, null, null, 114]]);
  assert.deepEqual(later.derived.htfEma, current.derived.htfEma, "unfinished 15m candle cannot change the step");
  const rewind = trainingChartData(input.slice(0, 14), 300, cfg, settings, 900);
  assert.ok(rewind.derived.htfEma.flat().every(value => value === null));
  assert.deepEqual(trainingChartData(input.slice(0, 16), 300, cfg, settings, 900), current);
  for (const invalid of [0, 60, 300]) assert.ok(trainingChartData(input, 300, cfg, settings, invalid).derived.htfEma.flat().every(value => value === null));
});

test("EMA near-100 display keeps the full-history values and excludes unseeded higher EMA points", () => {
  const model = trainingChartData(sample(155), 60, cfg, settings, 300);
  const all = emaPlotData(model.bars, model.derived.emaLines, true, 100);
  const tail = emaPlotData(model.bars, model.derived.emaLines, false, 100);
  assert.equal(all[0].length, 155);
  tail.forEach((line, j) => assert.deepEqual(line, all[j].slice(-100)));
  const htf = emaPlotData(model.bars, model.derived.htfEma, true, 100);
  assert.equal(htf[0][0].time, 900);
  assert.ok(htf.flat().every(point => Number.isFinite(point.value)));
});

test("fills remain on the corresponding candle at every timeframe with stable same-bar order", () => {
  const events = Object.freeze([
    Object.freeze({ kind: "order", time: 600, side: "buy", qty: 1 }),
    Object.freeze({ kind: "fill", time: 660, side: "buy", qty: 1, reason: "开多" }),
    Object.freeze({ kind: "fill", time: 720, side: "sell", qty: 1, reason: "止盈" }),
    Object.freeze({ kind: "fill", time: 900, side: "sell", qty: 2, reason: "开空" }),
  ]);
  for (const tf of [60, 300, 900, 3600, 14400]) {
    const markers = trainingTradeMarkers(events, tf), times = new Set(aggregateBars(sample(6), tf).map(b => b.time));
    assert.ok(markers.every(marker => times.has(marker.time)));
    assert.deepEqual(markers.map(m => m.text), ["开多 1", "止盈 1", "开空 2"]);
    assert.deepEqual(markers.map(m => m.position), ["belowBar", "aboveBar", "aboveBar"]);
  }
  assert.deepEqual(trainingTradeMarkers(events, 300).map(m => m.time), [600, 600, 900]);
});
