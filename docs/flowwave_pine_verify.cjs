"use strict";
// 用实际 .pine 文件跨运行时对比现有 JS；不重写指标公式。
// 从仓库根目录运行：
// npm install --prefix .run-tmp/flowwave-pine-verify --ignore-scripts --no-audit --no-fund --package-lock=false --save-exact pinets@0.12.0
// node docs/flowwave_pine_verify.cjs
// PineTS 是第三方运行时；通过此验证不等于通过 TradingView 官方编译器。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { PineTS, Indicator } = require("../.run-tmp/flowwave-pine-verify/node_modules/pinets");
const { deriveLw, deriveBand } = require("../static/indicators.js");

const pineSource = fs.readFileSync(path.join(__dirname, "FlowWave_Band.pine"), "utf8");
const titles = {
  mid: "中线", up: "上轨", dn: "下轨", wave: "FlowWave LSMA",
  wt2: "FlowWave 信号 wt2", state: "带状态（1超买 / -1超卖 / 0中性）",
};
const tolerance = 1e-6; // PineTS 对部分中间值保留 10 位小数。
const stats = { runs: 0, bars: 0, checkedValues: 0, maxError: 0 };
const seenStates = new Set();

function makeBars(count, priceAt, volumeAt = (i) => 100 + (i % 7) * 30) {
  return Array.from({ length: count }, (_, i) => {
    const close = priceAt(i);
    return { time: 1700000000 + i * 60, open: close - 1,
      high: close + 2, low: close - 2, close, volume: volumeAt(i) };
  });
}

function expectedFor(bars, k) {
  const lw = deriveLw(bars, bars.map(() => null));
  const band = deriveBand(bars, lw.wt2, k);
  return { ...band, mid: band.mid.map((v, i) => band.up[i] == null ? null : v),
    wave: lw.wave, wt2: lw.wt2 };
}

async function runPine(bars, inputs = {}) {
  const data = bars.map((b) => ({ ...b, volume: b.volume ?? NaN,
    openTime: b.time * 1000, closeTime: b.time * 1000 + 59999 }));
  const { plots } = await new PineTS(data).run(new Indicator(pineSource, inputs));
  return Object.fromEntries(Object.entries(titles).map(([key, title]) => [key,
    plots[title].data.map(({ value }) => Number.isFinite(value) ? value : null)]));
}

function compare(label, actual, expected) {
  for (const key of Object.keys(titles)) {
    assert.equal(actual[key].length, expected[key].length, `${label}: ${key} 长度`);
    actual[key].forEach((value, i) => {
      const reference = expected[key][i];
      const location = `${label}: ${key}[${i}]`;
      stats.checkedValues++;
      if (value == null || reference == null || key === "state") {
        assert.equal(value, reference, location);
      } else {
        const error = Math.abs(value - reference);
        stats.maxError = Math.max(stats.maxError, error);
        assert.ok(error <= tolerance, `${location}: ${value} != ${reference} (误差 ${error})`);
      }
    });
  }
}

async function verify(label, bars, k = 2, showMid = false) {
  const actual = await runPine(bars, { "带宽 σ": k, "显示中线": showMid });
  compare(label, actual, expectedFor(bars, k));
  actual.state.forEach((state) => seenStates.add(state));
  stats.runs++;
  stats.bars += bars.length;
  return actual;
}

async function main() {
  const mixedPrice = (i) => 4000 + Math.sin(i / 13) * 60 + Math.cos(i / 3) * 4 + i * 0.1;
  const mixed = makeBars(800, mixedPrice);
  const full = await verify("震荡和趋势", mixed);
  assert.equal(full.up.findIndex((v) => v != null), 40, "默认第 41 根形成通道");
  assert.equal(full.wt2.findIndex((v) => v != null), 7, "保留项目 RSI 播种：第 8 根形成信号");
  assert.equal(full.wave.findIndex((v) => v != null), 22, "第 23 根形成 LSMA");

  for (const k of [1.5, 2.5]) {
    const varied = await verify(`带宽 ${k}`, mixed, k);
    assert.deepEqual(varied.wt2, full.wt2, "带宽不影响信号");
    assert.deepEqual(varied.state, full.state, "带宽不影响着色状态");
  }
  const withMid = await verify("打开中线", mixed, 2, true);
  assert.deepEqual(withMid, full, "中线开关不改变任何数值");

  const cases = [
    ["持续上涨", makeBars(300, (i) => 1000 + i * 2)],
    ["持续下跌", makeBars(300, (i) => 1000 - i * 2)],
    ["完全横盘", makeBars(300, () => 1000)],
    ["横盘后启动", makeBars(300, (i) => i < 80 ? 4000 : mixedPrice(i))],
    ["零成交量", makeBars(300, mixedPrice, () => 0)],
    ["部分成交量缺失", makeBars(300, mixedPrice, (i) => i % 7 ? 100 : null)],
    ["非整数价格", makeBars(300, (i) => 20 + Math.sin(i / 9) * 0.25 + i * 0.001)],
  ];
  for (const [label, bars] of cases) {
    const result = await verify(label, bars);
    if (["持续上涨", "持续下跌", "完全横盘"].includes(label)) {
      assert.ok(result.up.every((v) => v == null), `${label}: 零带宽不画通道`);
    }
  }

  // 截短历史不改变已计算的前缀；覆盖短样本、预热边界和有信号的区间。
  for (const count of [1, 2, 3, 8, 21, 23, 40, 41, 80, 180]) {
    const prefix = await verify(`历史前缀 ${count}`, mixed.slice(0, count));
    compare(`无未来数据 ${count}`, prefix,
      Object.fromEntries(Object.entries(full).map(([key, values]) => [key, values.slice(0, count)])));
  }

  // 模拟末根未收盘时价格与成交量变化，历史已收盘数值应保持一致。
  const updated = mixed.map((bar) => ({ ...bar }));
  updated.at(-1).close += 8;
  updated.at(-1).high += 8;
  updated.at(-1).volume += 500;
  const tail = await verify("更新最后一根", updated);
  for (const key of Object.keys(titles)) {
    assert.deepEqual(tail[key].slice(0, -1), full[key].slice(0, -1), `${key}: 历史值不变`);
  }
  assert.notEqual(tail.wt2.at(-1), full.wt2.at(-1), "末根信号随行情变化");
  assert.deepEqual([...seenStates].sort(), [-1, 0, 1], "覆盖全部三种着色状态");
  console.log(JSON.stringify({ ...stats, tolerance, states: [...seenStates].sort(), result: "PASS" }, null, 2));
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
