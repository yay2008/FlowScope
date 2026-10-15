"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { OB, normalizeObOptions, deriveObZones, obVolumeEnds } = require("../static/indicators.js");

const candle = (open, close, extra = {}) => ({
  open, close, high: Math.max(open, close) + 1, low: Math.min(open, close) - 1,
  volume: 100, buy: 80, sell: 20, unknown: 0, coverage: "complete", ...extra,
});
const warmup = () => Array.from({ length: 100 }, (_, i) => i % 2 ? candle(101, 100) : candle(100, 101));
const bullBars = () => [...warmup(), candle(100, 120)];
const mirror = (bars) => bars.map((b) => ({ ...b, open: 200 - b.open, close: 200 - b.close,
  high: 200 - b.low, low: 200 - b.high }));

test("OB: 默认参数及持久化坏值校验; 输入数组不被修改", () => {
  assert.deepEqual(normalizeObOptions(null), normalizeObOptions());
  assert.deepEqual(normalizeObOptions({ zLen: 0, maxAge: 1.5, preventOverlap: "false", bullColor: "red" }),
                   normalizeObOptions());
  assert.equal(normalizeObOptions({ zLen: 1, maxAge: 900, preventOverlap: false }).zLen, 1);
  const bars = bullBars().map(Object.freeze);
  Object.freeze(bars);
  assert.doesNotThrow(() => deriveObZones(bars));
  assert.deepEqual(deriveObZones([]).zones, []);
});

test("OB: 上穿 4 才建多区, 锚定最后阴线完整高低价, 量取阴线而非信号根", () => {
  const bars = bullBars();
  bars[99] = candle(101, 100, { high: 105, low: 96, buy: 15, sell: 85 });
  const r = deriveObZones(bars);
  assert.equal(r.zUp[98], null);
  assert.equal(r.zUp[99], -1);
  assert.ok(r.zUp[100] > 4);
  assert.deepEqual(r.bullCreate.flatMap((v, i) => v ? [i] : []), [100]);
  assert.ok(r.bearCreate.every((v) => !v));
  assert.deepEqual(r.zones, [{ side: "bull", start: 99, created: 100, end: 100, top: 105, bottom: 96,
    status: "active", buy: 15, sell: 85, unknown: 0, coverage: "complete" }]);
  // 高位延续不是第二次上穿, 即便允许重叠也只建一次。
  const continued = deriveObZones([...bars, candle(120, 140), candle(140, 160)], { preventOverlap: false });
  assert.equal(continued.zones.length, 1);
  assert.equal(continued.zones[0].end, 102);
});

test("OB: 空区完全对称; 阈值上穿的首个可计算值不能越过前值 na/0 检查", () => {
  const bear = deriveObZones(mirror(bullBars()));
  assert.deepEqual(bear.bearCreate.flatMap((v, i) => v ? [i] : []), [100]);
  assert.equal(bear.zones[0].side, "bear");
  assert.equal(bear.zones[0].top, 101);
  assert.equal(bear.zones[0].bottom, 98);
  const flat = Array.from({ length: 110 }, () => candle(100, 100));
  assert.ok(deriveObZones(flat).zUp.every((z) => z === null));
  assert.equal(deriveObZones([...flat, candle(100, 200)]).zones.length, 0);
  assert.equal(deriveObZones(bullBars(), { zLen: 1 }).zones.length, 0);
  // updist 的末个值恰等于窗口均值 => zUp[99] 为 0, 即使下一根突破也不建区。
  const zero = [...Array.from({ length: 99 }, (_, i) => i === 0 ? candle(100, 103)
    : i % 2 === 0 && i < 98 ? candle(100, 102) : candle(101, 100)), candle(100, 101), candle(101, 150)];
  const r = deriveObZones(zero);
  assert.equal(r.zUp[99], 0);
  assert.ok(r.zUp[100] > 4);
  assert.equal(r.zones.length, 0);
});

test("OB: 必须连续两次严格收盘突破; 影线、单次突破、恰好到边界都不失效", () => {
  const bars = [...bullBars(), candle(110, 110, { low: 90 }), candle(98, 98), candle(99, 99),
                candle(98, 98), candle(97, 97), candle(110, 110)];
  const result = deriveObZones(bars);
  assert.equal(deriveObZones(bars.slice(0, -2)).zones[0].status, "active");
  assert.deepEqual([result.zones[0].status, result.zones[0].end], ["mitigated", 105]);
  const bear = deriveObZones(mirror(bars));
  assert.deepEqual([bear.zones[0].status, bear.zones[0].end], ["mitigated", 105]);
});

test("OB: 年龄从锚点起算, 到期停止延伸但保留历史; 新建根就过期仍产生创建信号", () => {
  const bars = [...bullBars(), candle(110, 110), candle(110, 110), candle(110, 110)];
  const r = deriveObZones(bars, { maxAge: 3 });
  assert.deepEqual([r.zones[0].start, r.zones[0].created, r.zones[0].end, r.zones[0].status],
                   [99, 100, 102, "expired"]);
  const immediate = deriveObZones(bullBars(), { maxAge: 1 });
  assert.equal(immediate.bullCreate[100], true);
  assert.equal(immediate.zones[0].status, "expired");
});

test("OB: 多空共用防重叠; 边界相接可建; 先创建后清理, 到期当根仍拦截重叠", () => {
  const bars = [...bullBars(), ...warmup(), candle(100, 120)];
  assert.equal(deriveObZones(bars).zones.length, 1);
  assert.equal(deriveObZones(bars, { preventOverlap: false }).zones.length, 2);
  assert.equal(deriveObZones(bars, { maxAge: 102 }).zones.length, 1, "第二次信号当天才到期");
  assert.equal(deriveObZones(bars, { maxAge: 101 }).zones.length, 2, "前一根已经到期, 历史区不挡新区域");
  const opposite = [...bullBars(), ...warmup(), candle(100, 80)];
  assert.equal(deriveObZones(opposite).bearCreate[201], false);
  assert.equal(deriveObZones(opposite, { preventOverlap: false }).bearCreate[201], true);
  // 第二个锚点的下沿与旧区域上沿 102 相等。
  const touch = [...bullBars(), ...warmup().map((b) => candle(b.open + 3, b.close + 3)), candle(103, 123)];
  assert.equal(deriveObZones(touch).zones.length, 2);
});

test("OB: 成交量只改量条, 缺失与 0 不混淆, 量条端点符合 Pine 整数截断", () => {
  const bars = bullBars();
  bars[99] = { ...bars[99], buy: null, sell: null, coverage: "missing" };
  const r = deriveObZones(bars);
  assert.equal(r.bullCreate[100], true);
  assert.equal(obVolumeEnds(r.zones[0]), null);
  assert.deepEqual(obVolumeEnds({ start: 10, end: 31, buy: 80, sell: 20 }), { buy: 20, sell: 12 });
  assert.deepEqual(obVolumeEnds({ start: 10, end: 31, buy: 20, sell: 80 }), { buy: 12, sell: 20 });
  assert.deepEqual(obVolumeEnds({ start: 10, end: 31, buy: 80, sell: 0 }), { buy: 20, sell: 10 });
  assert.deepEqual(obVolumeEnds({ start: 10, end: 31, buy: 0, sell: 80 }), { buy: 10, sell: 20 });
});

test("OB: 未收盘信号撤回、失效撤回、补历史与窗口裁剪均按现有数据重建", () => {
  const bars = bullBars();
  const original = deriveObZones(bars);
  assert.equal(original.zones.length, 1);
  assert.equal(deriveObZones([...bars.slice(0, -1), candle(100, 101)]).zones.length, 0);
  const broken = [...bars, candle(98, 98), candle(97, 97)];
  assert.equal(deriveObZones(broken).zones[0].status, "mitigated");
  broken[102] = candle(110, 110);
  assert.equal(deriveObZones(broken).zones[0].status, "active");
  assert.equal(deriveObZones(bars.slice(2)).zones.length, 0, "窗口不到预热长度不能沿用旧结果");
  assert.deepEqual(deriveObZones(bars), original, "补齐历史后得到同一结果");
});

test("OB: 随机行情的 Z 与逐窗口直接计算总体标准差一致; 创建信号没有未来数据依赖", () => {
  let seed = 7, price = 1000;
  const bars = Array.from({ length: 500 }, (_, i) => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    const body = i % 90 === 0 ? 80 : (seed % 11) - 5;
    const b = candle(price, price + body);
    price += body;
    return b;
  });
  const r = deriveObZones(bars, { zLen: 50 });
  const impulse = { up: [], down: [] };
  for (let i = 0; i < bars.length; i++) {
    const delta = bars[i].close - bars[i].open;
    impulse.up.push(delta > 0 ? (impulse.up[i - 1] || 0) + delta : 0);
    impulse.down.push(delta < 0 ? (impulse.down[i - 1] || 0) - delta : 0);
    if (i < 49) continue;
    for (const [key, z] of [["up", r.zUp], ["down", r.zDn]]) {
      const window = impulse[key].slice(i - 49, i + 1);
      const mean = window.reduce((a, b) => a + b, 0) / 50;
      const sd = Math.sqrt(window.reduce((a, b) => a + (b - mean) ** 2, 0) / 50);
      assert.ok(Math.abs(z[i] - (window[49] - mean) / sd) < 1e-10);
    }
  }
  assert.ok(r.bullCreate.some(Boolean), "用例确实触发了区域");
  for (const end of [80, 91, 181, 271, 361, 451]) {
    const prefix = deriveObZones(bars.slice(0, end), { zLen: 50 });
    assert.deepEqual(prefix.bullCreate, r.bullCreate.slice(0, end));
    assert.deepEqual(prefix.bearCreate, r.bearCreate.slice(0, end));
  }
  assert.equal(OB.zLen, 100);
});

test("OB: 一段全零距离后滚动和的浮点残差不能让前值变成非 na, 进而误判上穿", () => {
  // 前 40 根实体是不能精确表示的一位小数, 之后 20 根阴线让 updist 窗口全为 0。
  // 累加再减的滚动和会剩下约 1e-12 的残差: 旧算法给出 zUp[59] ≈ -1.9e-8, 第 60 根就建了区;
  // Pine 的标准差恰为 0, zUp[59] 是 na, 不建区。
  const bodies = [0, 0.3, 0, 0, 3.9, 0, 0, 0, 4.2, 9, 0, 0, 5.4, 4.2, 9.5, 7.8, 9.1, 0, 4.3, 0.2,
    0, 0, 5, 0, 0, 6.6, 2.9, 2.4, 0, 6.2, 9.8, 3, 0, 0, 0, 6, 9.4, 0, 1.9, 0.2];
  const bars = [...bodies.map((b) => b > 0 ? candle(0, b) : candle(0.5, 0)),
                ...Array.from({ length: 20 }, () => candle(0.5, 0)), candle(0, 50)];
  const r = deriveObZones(bars, { zLen: 20, preventOverlap: false });
  assert.equal(r.zUp[59], null);
  assert.ok(r.zUp[60] > 4);
  assert.equal(r.bullCreate[60], false);
});
