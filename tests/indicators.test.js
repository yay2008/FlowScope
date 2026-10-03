"use strict";
/* indicators.js 的纯函数回归: 不需要 DOM 与图表, 直接在 Node 里 require。 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { rollingSma, rollingZ, linreg, ema, derive, levelOf, BAND, WT, deriveWt, wtPrice } =
  require("../static/indicators.js");

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

// 确定性的随机游走 K 线(带慢周期漂移, 让振荡值能跑到超买超卖区)
function walkBars(n, seed) {
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const bars = [];
  let p = 3000;
  for (let i = 0; i < n; i++) {
    const o = p;
    p += Math.round((rnd() - 0.5) * 10 + Math.sin(i / 40) * 2);
    bars.push({ time: 1700000000 + i * 30, open: o, close: p,
                high: Math.max(o, p) + Math.round(rnd() * 3), low: Math.min(o, p) - Math.round(rnd() * 3) });
  }
  return bars;
}

test("deriveWt: 与逐根执行的 Pine f_getWT 对拍", () => {
  const bars = walkBars(600, 11);
  const W = deriveWt(bars);
  // 独立实现: 不借用模块里的 ema/sma, 按 Pine 逐根执行的写法算 esa / d / ci / osc / sig
  const a = (n) => 2 / (n + 1);
  let esa = null, d = null, osc = null;
  const oscs = [];
  bars.forEach((b, i) => {
    const s = (b.high + b.low + b.close) / 3;
    esa = esa == null ? s : a(WT.chLen) * s + (1 - a(WT.chLen)) * esa;
    d = d == null ? Math.abs(s - esa) : a(WT.chLen) * Math.abs(s - esa) + (1 - a(WT.chLen)) * d;
    if (d !== 0) {
      const ci = (s - esa) / (0.015 * d);
      osc = osc == null ? ci : a(WT.avgLen) * ci + (1 - a(WT.avgLen)) * osc;
    }
    oscs.push(osc);
    const win = oscs.slice(i - WT.sigLen + 1, i + 1);
    const sig = i >= WT.sigLen - 1 && win.every((x) => x != null) ? win.reduce((x, y) => x + y) / WT.sigLen : null;
    if (i < WT.warmup) {
      assert.equal(W.osc[i], null, `冷启动段应屏蔽 @${i}`);
      return;
    }
    assert.ok(Math.abs(W.osc[i] - osc) < 1e-9, `osc @${i}`);
    if (i < WT.warmup + WT.sigLen - 1) assert.equal(W.sig[i], null, `sig 窗口含屏蔽段 @${i}`);
    else assert.ok(Math.abs(W.sig[i] - sig) < 1e-9, `sig @${i}`);
  });
});

test("deriveWt: 冷启动屏蔽之后, 窗口从哪根开始都不影响读数", () => {
  // 前端窗口会滑动(只留最近 800 根), 屏蔽段之后的值必须已经和长历史算出来的一致
  const bars = walkBars(900, 5);
  const full = deriveWt(bars).osc;
  for (const start of [100, 400]) {
    const cold = deriveWt(bars.slice(start)).osc;
    for (let j = WT.warmup; j < cold.length; j++) {
      assert.ok(Math.abs(cold[j] - full[start + j]) <= 1, `start=${start} j=${j}`);
    }
  }
});

test("deriveWt: 交叉方向与强弱分档符合原版标签条件", () => {
  const bars = walkBars(2000, 11);
  const { osc, sig, cross } = deriveWt(bars);
  const seen = new Set();
  cross.forEach((c, i) => {
    if (!c) return;
    seen.add(c);
    if (c > 0) assert.ok(osc[i] > sig[i] && osc[i - 1] <= sig[i - 1], `金叉 @${i}`);
    else assert.ok(osc[i] < sig[i] && osc[i - 1] >= sig[i - 1], `死叉 @${i}`);
    const want = c > 0 ? (osc[i] < WT.lower ? 3 : osc[i] < 0 ? 2 : 1)
                       : -(osc[i] > WT.upper ? 3 : osc[i] > 0 ? 2 : 1);
    assert.equal(c, want, `分档 @${i}`);
  });
  assert.deepEqual([...seen].sort(), [-1, -2, -3, 1, 2, 3], "样本里六种交叉都应出现");
});

test("deriveWt: 背离事件的枢轴与价格/振荡关系", () => {
  const bars = walkBars(2000, 11);
  const W = deriveWt(bars);
  const kinds = new Set(W.divs.map((x) => x.kind));
  assert.deepEqual([...kinds].sort(), ["HB", "HS", "RB", "RS"]);
  for (const x of W.divs) {
    const low = x.kind === "RB" || x.kind === "HB";
    assert.equal(x.at, x.to + WT.pivot, "背离在枢轴右侧走完 pivot 根时确认");
    for (const c of [x.from, x.to]) {
      for (let j = c - WT.pivot; j <= c + WT.pivot; j++) {
        if (j !== c) assert.ok(low ? W.osc[j] > W.osc[c] : W.osc[j] < W.osc[c], `${x.kind} 枢轴 @${c}`);
      }
    }
    const p1 = low ? bars[x.from].low : bars[x.from].high, p2 = low ? bars[x.to].low : bars[x.to].high;
    assert.deepEqual([x.fromPrice, x.toPrice], [p1, p2], "主图连线的两端是枢轴那根的低点/高点");
    const rule = { RB: p2 < p1 && x.toOsc > x.fromOsc, HB: p2 > p1 && x.toOsc < x.fromOsc,
                   RS: p2 > p1 && x.toOsc < x.fromOsc, HS: p2 < p1 && x.toOsc > x.fromOsc };
    assert.ok(rule[x.kind], `${x.kind} 价格/振荡关系 ${JSON.stringify(x)}`);
  }
});

test("wtPrice: 按当根最近 mapLen 根的高低点通道换算, ±60 落在上下沿、0 在中线", () => {
  const bars = walkBars(WT.mapLen + 150, 3);
  const W = deriveWt(bars);
  for (const i of [0, 50, WT.mapLen - 1, WT.mapLen + 149]) {
    // 只看当根及以前、最多 mapLen 根: 开头不足一个窗口时用已有的 bar
    const win = bars.slice(Math.max(0, i - WT.mapLen + 1), i + 1);
    const hi = Math.max(...win.map((b) => b.high)), lo = Math.min(...win.map((b) => b.low));
    assert.equal(W.hi[i], hi, `通道上沿 @${i}`);
    assert.equal(W.lo[i], lo, `通道下沿 @${i}`);
    assert.ok(Math.abs(wtPrice(W, i, WT.ob1) - hi) < 1e-9);
    assert.ok(Math.abs(wtPrice(W, i, WT.os1) - lo) < 1e-9);
    assert.ok(Math.abs(wtPrice(W, i, 0) - (hi + lo) / 2) < 1e-9);
  }
  assert.equal(wtPrice(W, 10, null), null, "没有值就没有价格");
});
