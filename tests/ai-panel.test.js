"use strict";
/* AI 看图分析浮层的纯函数: 数值表取哪一段、请求里一张图的数据、截图标题、SSE 解析、底栏文字。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { COLUMNS, MIN_ROWS, MAX_ROWS, rowSpan, buildChartData, shotTitle, parseSse, footText } =
  require("../static/ai-panel.js");
const Ind = require("../static/indicators.js");
const { derive, BAND } = Ind;

const CFG = { mult: [1.5, 2.5, 3.5], rellen: 20, smalen: 300, zlen: 50, priceDigits: 1, volumeDigits: 0 };
const T0 = 1791446400;   // 2026-10-08 08:00:00(北京时间当 UTC 存)

// 确定性的随机游走 K 线, 带买卖量和覆盖标记; 前 10 根没有买卖量(覆盖缺失)
function walkBars(n, seed = 7) {
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const bars = [];
  let p = 3000, cvd = 0;
  for (let i = 0; i < n; i++) {
    const o = p;
    p += (rnd() - 0.5) * 6 + 3 * Math.sin(i / 25);
    const buy = Math.round(rnd() * 100), sell = Math.round(rnd() * 100);
    const known = i >= 10;
    if (known) cvd += buy - sell;
    bars.push({ time: T0 + i * 30, open: o, high: Math.max(o, p) + rnd(), low: Math.min(o, p) - rnd(), close: p,
                volume: buy + sell, buy: known ? buy : null, sell: known ? sell : null,
                delta: known ? buy - sell : null, cvd: known ? cvd : null,
                coverage: known ? (i % 7 ? "complete" : "partial") : "missing" });
  }
  return bars;
}

function input(bars, range, extra = {}) {
  return { tf: 30, view: "candle", shown: { ema: true, band: false, bandMid: true }, bars,
           derived: derive(bars, CFG, BAND.kDefault), cfg: CFG, ltf: 0, stale: false, range, ...extra };
}

test("数值表的列名都是服务端认识的(analysis.COLUMN_NOTES)", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "analysis.py"), "utf8");
  const block = source.slice(source.indexOf("COLUMN_NOTES = {"), source.indexOf("\n}", source.indexOf("COLUMN_NOTES = {")));
  const known = new Set([...block.matchAll(/"([a-z0-9_]+)":/g)].map((m) => m[1]));
  assert.equal(COLUMNS[0], "time");
  for (const column of COLUMNS) assert.ok(known.has(column), `analysis.py 不认识列 ${column}`);
  assert.equal(new Set(COLUMNS).size, COLUMNS.length);
});

test("取可视范围内的 bar: 太多取最右边, 太少往左补, 一根都没有取最新一段", () => {
  assert.deepEqual(rowSpan(500, { from: 400.4, to: 470.6 }), [400, 471]);
  assert.deepEqual(rowSpan(500, { from: 100, to: 503 }), [500 - MAX_ROWS, 500]);   // 右侧留白越出末根
  assert.deepEqual(rowSpan(500, { from: 450, to: 460 }), [461 - MIN_ROWS, 461]);
  assert.deepEqual(rowSpan(30, { from: 20, to: 25 }), [0, 26]);                    // 左边不够补
  assert.deepEqual(rowSpan(500, { from: 600, to: 650 }), [500 - MAX_ROWS, 500]);   // 拖到了空白处
  assert.deepEqual(rowSpan(500, null), [500 - MAX_ROWS, 500]);
  assert.deepEqual(rowSpan(0, null), [0, 0]);
});

test("一张图的数据: 每行与列对齐, 价格按显示位数取整, 空值保留为 null", () => {
  const bars = walkBars(400);
  const data = buildChartData(input(bars, { from: 300, to: 399 }));
  assert.equal(data.rows.length, 100);
  for (const row of data.rows) assert.equal(row.length, COLUMNS.length);
  const col = (name) => COLUMNS.indexOf(name);
  const first = data.rows[0];
  assert.equal(first[col("time")], bars[300].time);
  assert.equal(first[col("close")], Number(bars[300].close.toFixed(1)));
  assert.equal(first[col("buy")], bars[300].buy);
  assert.equal(data.meta.range.from, bars[300].time);
  assert.equal(data.meta.range.to, bars[399].time);
  assert.equal(data.meta.loaded.count, 400);
  assert.equal(data.meta.loaded.from, bars[0].time);
  assert.deepEqual(data.meta.shown, { ema: true, band: false });
  assert.equal(data.meta.view, "candle");
  assert.equal(data.meta.ltf, 0);
  assert.equal(buildChartData(input(bars, null, { ltf: 15 })).meta.ltf, 15, "口径照图上数据的实际粒度报");
  // 振荡值一位小数; WaveTrend 已删除, 不再有它的列和事件
  const fw = data.rows.map((row) => row[col("fw_sig")]).filter((v) => v != null);
  assert.ok(fw.length > 0 && fw.every((v) => Number(v.toFixed(1)) === v));
  assert.ok(!COLUMNS.includes("wt") && !COLUMNS.includes("wt_sig"));
  assert.equal("events" in data, false);

  // 覆盖: 开头 10 根缺失(往左补到最开头时才会出现)
  const head = buildChartData(input(bars, { from: 0, to: 59 }));
  assert.equal(head.meta.coverage.missing, 10);
  assert.equal(head.rows[0][col("buy")], null);
  assert.equal(head.rows[0][col("ema200")] != null, true, "EMA 首根就播种");
});

test("价格按合约位数取整; 位数未知(期货报价还没到)时保留原值, 不按 0 位取整", () => {
  // 国债期货: 收盘 107.85 在上轨 107.8543 之下, 按 0 位取整会变成 108 > 107.9, 平白多出一次突破
  const bars = walkBars(200);
  const last = bars.length - 1;
  Object.assign(bars[last], { open: 107.8, high: 107.86, low: 107.79, close: 107.85 });
  const col = (name) => COLUMNS.indexOf(name);
  const lastRow = (cfg) => {
    const data = input(bars, null, { cfg });
    data.derived.band.up[last] = 107.8543;
    data.derived.emaLines[0][last] = 107.83217;
    return buildChartData(data).rows.at(-1);
  };
  const exact = lastRow({ ...CFG, priceDigits: 3 });
  assert.equal(exact[col("close")], 107.85);
  assert.equal(exact[col("band_up")], 107.8543);
  assert.equal(exact[col(`ema${Ind.EMA_PERIODS[0]}`)], 107.8322);
  const unknown = lastRow({ ...CFG, priceDigits: undefined });
  assert.equal(unknown[col("close")], 107.85);
  assert.equal(unknown[col("band_up")], 107.8543);
  assert.equal(unknown[col(`ema${Ind.EMA_PERIODS[0]}`)], 107.83217);
  assert.ok(unknown[col("close")] < unknown[col("band_up")]);
  assert.equal(lastRow({ ...CFG, priceDigits: 0 })[col("close")], 108, "位数明确是 0 时照常取整");
});

test("还没加载完返回 null; 切了口径、新历史没到(数据还是旧口径)也算没加载完", () => {
  assert.equal(buildChartData({ bars: [], derived: null, cfg: CFG, range: null }), null);
  assert.equal(buildChartData({ bars: walkBars(5), derived: null, cfg: CFG, range: null }), null);
  const bars = walkBars(200);
  assert.equal(buildChartData(input(bars, null, { stale: true })), null);
  assert.equal(buildChartData(input(bars, null, { ltf: null })), null);
});

test("截图标题: 合约名与代码、周期、视图、模式、截止时刻; 名字取不到时只写代码", () => {
  const data = buildChartData(input(walkBars(200), null, { view: "footprint" }));
  const context = { symbol: "KQ.m@SHFE.fu", label: "燃油2611", settings: { mode: "cvd" } };
  assert.equal(shotTitle(context, 30, data),
               "燃油2611（KQ.m@SHFE.fu）   30s   足迹图   FlowMeter: CVD   截至 10-08 09:39:30 北京时间");
  assert.match(shotTitle({ ...context, label: "KQ.m@SHFE.fu" }, 3600, data), /^KQ\.m@SHFE\.fu   1h   /);
});

test("SSE 解析: 跳过心跳注释, 留下没收完的尾巴, 认 CRLF", () => {
  const text = ": ping\n\nevent: meta\ndata: {\"model\":\"m\"}\n\nevent: delta\r\ndata: {\"text\":\"多\"}\r\n\r\nevent: del";
  const { events, rest } = parseSse(text);
  assert.deepEqual(events, [{ event: "meta", data: { model: "m" } }, { event: "delta", data: { text: "多" } }]);
  assert.equal(rest, "event: del");
  assert.deepEqual(parseSse("event: x\ndata: {bad\n\n").events, []);
});

test("底栏: 模型、token、耗时、存盘位置", () => {
  const meta = { model: "deepseek-flash", reasoningEffort: "high" };
  assert.equal(footText(meta, null), "deepseek-flash · 思考 high");
  assert.equal(footText(meta, { usage: { prompt_tokens: 12345, completion_tokens: 678, prompt_cache_hit_tokens: 1000 },
                                elapsed: 23.4, saved: "analysis/20261008/153012-x-30s.json" }),
               "deepseek-flash · 思考 high · 输入 12,345(缓存命中 1,000) · 输出 678 token · 23.4 秒 · " +
               "已存 data/analysis/20261008/153012-x-30s.json");
  assert.equal(footText(null, { elapsed: 1, saveError: "磁盘满" }), "1 秒 · 存盘失败: 磁盘满");
});
