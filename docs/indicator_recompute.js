// FlowScope 指标离线复算: 逐字移植 static/app.js 的 derive / deriveLw / levelOf
// 用法: node .tmp-test/indicators.js .tmp-test/hist_k10.json
// 输出: 每根 bar 的 OHLCV + 指标值 + 信号标签(JSONL 到 stdout)
"use strict";
const fs = require("fs");

const path = process.argv[2];
const raw = JSON.parse(fs.readFileSync(path, "utf-8").replace(/^\uFEFF/, ""));
const bars = raw.bars;

const cfg = {
  mult: [1.5, 2.5, 3.5],
  rellen: 20,
  smalen: 300,
  zlen: 50,
  modes: ["rvol", "crvol", "volume", "bsv", "delta", "cvd"],
};

// ---------------- 与 app.js 完全一致的函数 ----------------
// (2026-09-28 起 app.js 的 rollingSma/rollingZ 也改为"窗口含 null 即输出 null", 与这里一致;
//  此前 app.js 把 null 当 0, 本脚本复算的结果与当时页面显示并不相同。)
function rollingSma(v, n) {
  const out = new Array(v.length).fill(null);
  let s = 0, bad = 0;
  for (let i = 0; i < v.length; i++) {
    if (v[i] == null) bad++; else s += v[i];
    if (i >= n) { if (v[i - n] == null) bad--; else s -= v[i - n]; }
    out[i] = i >= n - 1 && bad === 0 ? s / n : null;
  }
  return out;
}
function rollingZ(v, n) {
  const out = new Array(v.length).fill(null);
  for (let i = n - 1; i < v.length; i++) {
    let sum = 0, cnt = 0;
    for (let j = i - n + 1; j <= i; j++) { const x = v[j]; if (x != null) { sum += x; cnt++; } }
    if (cnt < n) continue;
    const mean = sum / n;
    let sq = 0;
    for (let j = i - n + 1; j <= i; j++) { const d = v[j] - mean; sq += d * d; }
    const sd = Math.sqrt(sq / n);
    out[i] = sd === 0 ? null : (v[i] - mean) / sd;
  }
  return out;
}
function ema(v, n) {
  const out = new Array(v.length).fill(null);
  const a = 2 / (n + 1);
  let prev = null;
  for (let i = 0; i < v.length; i++) {
    if (v[i] == null) continue;
    prev = prev == null ? v[i] : a * v[i] + (1 - a) * prev;
    out[i] = prev;
  }
  return out;
}
function rma(v, n) {
  const out = new Array(v.length).fill(null);
  let s = 0, prev = null;
  for (let i = 0; i < v.length; i++) {
    const x = v[i] == null ? 0 : v[i];
    s += x;
    if (i >= n) s -= v[i - n] == null ? 0 : v[i - n];
    if (i === n - 1) prev = s / n;
    else if (i > n - 1) prev = (x + (n - 1) * prev) / n;
    out[i] = i >= n - 1 ? prev : null;
  }
  return out;
}
function rsi(v, n) {
  const up = new Array(v.length).fill(null);
  const dn = new Array(v.length).fill(null);
  for (let i = 1; i < v.length; i++) {
    const c = v[i] - v[i - 1];
    up[i] = Math.max(c, 0);
    dn[i] = -Math.min(c, 0);
  }
  const ru = rma(up, n), rd = rma(dn, n);
  return ru.map((u, i) => {
    const d = rd[i];
    if (u == null || d == null || (u === 0 && d === 0)) return null;
    return d === 0 ? 100 : 100 - 100 / (1 + u / d);
  });
}
function linreg(v, n) {
  const out = new Array(v.length).fill(null);
  const sx = (n * (n - 1)) / 2, sxx = (n * (n - 1) * (2 * n - 1)) / 6;
  for (let i = n - 1; i < v.length; i++) {
    let sy = 0, sxy = 0, ok = true;
    for (let j = 0; j < n; j++) {
      const y = v[i - n + 1 + j];
      if (y == null) { ok = false; break; }
      sy += y; sxy += j * y;
    }
    if (!ok) continue;
    const slope = (n * sxy - sx * sy) / (n * sxx - sx * sx);
    out[i] = (sy - slope * sx) / n + slope * (n - 1);
  }
  return out;
}
function smaStrict(v, n) {
  const out = new Array(v.length).fill(null);
  let s = 0, bad = 0;
  for (let i = 0; i < v.length; i++) {
    if (v[i] == null) bad++; else s += v[i];
    if (i >= n) { if (v[i - n] == null) bad--; else s -= v[i - n]; }
    out[i] = i >= n - 1 && bad === 0 ? s / n : null;
  }
  return out;
}

const LW = { n1: 9, n2: 6, n3: 3, n4: 21, ob: 80, os: 20, slopeLen: 10 };

let derived = null;
let BARS = null;

function derive(bars, classifySource = "lr") {
  BARS = bars;
  const n = bars.length;
  const vol = bars.map((b) => b.volume);
  const buy = bars.map((b) => (classifySource === "legacy" ? b.buyLegacy : b.buy));
  const sell = bars.map((b) => (classifySource === "legacy" ? b.sellLegacy : b.sell));
  const delta = bars.map((b) => (classifySource === "legacy" ? b.deltaLegacy : b.delta));
  const posd = delta.map((d) => (d == null ? null : d > 0 ? d : 0));
  const negd = delta.map((d) => (d == null ? null : d < 0 ? d : 0));

  const smaVol20 = rollingSma(vol, cfg.rellen);
  const smaVolN = rollingSma(vol, cfg.smalen);
  const smaPos20 = rollingSma(posd, cfg.rellen);
  const smaNeg20 = rollingSma(negd, cfg.rellen);
  const smaBuy20 = rollingSma(buy, cfg.rellen);
  const smaSell20 = rollingSma(sell, cfg.rellen);

  const rvol = vol.map((v, i) => (smaVol20[i] ? v / smaVol20[i] : null));
  const rpos = posd.map((v, i) => (v == null || !smaPos20[i] ? null : v / smaPos20[i]));
  const rneg = negd.map((v, i) => (v == null || !smaNeg20[i] ? null : v / smaNeg20[i]));
  const rbuy = buy.map((v, i) => (v == null || !smaBuy20[i] ? null : v / smaBuy20[i]));
  const rsell = sell.map((v, i) => (v == null || !smaSell20[i] ? null : v / smaSell20[i]));

  const zVol = rollingZ(vol, cfg.zlen);
  const zRpos = rollingZ(rpos, cfg.zlen);
  const zRneg = rollingZ(rneg, cfg.zlen);
  const zBuy = rollingZ(buy, cfg.zlen);
  const zSell = rollingZ(sell, cfg.zlen);

  const crv = new Array(n).fill(null);
  let acc = 0;
  for (let i = 0; i < n; i++) {
    if (rvol[i] == null) continue;
    acc += bars[i].close > bars[i].open ? rvol[i] : -rvol[i];
    crv[i] = acc;
  }

  derived = { vol, buy, sell, delta, posd, negd, smaVolN, rvol, rpos, rneg, rbuy, rsell,
              zVol, zRpos, zRneg, zBuy, zSell, crv };
  derived.lw = deriveLw(BARS);
  return derived;
}

function deriveLw(bars) {
  const n = bars.length;
  const hlc3 = bars.map((b) => (b.high + b.low + b.close) / 3);
  const vol = bars.map((b) => b.volume || 0);

  const e1 = ema(hlc3, LW.n1);
  const dev = hlc3.map((x, i) => (e1[i] == null ? null : x - e1[i]));
  const e2 = ema(dev.map((x) => (x == null ? null : Math.abs(x))), LW.n1);
  const cci = dev.map((x, i) => (x == null || !e2[i] ? null : x / (0.025 * e2[i])));
  const tci = ema(cci, LW.n2).map((x) => (x == null ? null : x + 50));

  const rmf = hlc3.map((tp, i) => tp * vol[i]);
  const mf = new Array(n).fill(null);
  for (let i = LW.n3 - 1; i < n; i++) {
    let pos = 0, neg = 0;
    for (let j = Math.max(i - LW.n3 + 1, 1); j <= i; j++) {
      if (hlc3[j] > hlc3[j - 1]) pos += rmf[j];
      else if (hlc3[j] < hlc3[j - 1]) neg += rmf[j];
    }
    mf[i] = 100 - 100 / (1 + (neg === 0 ? 1e10 : pos / neg));
  }

  const rsi3 = rsi(hlc3, LW.n3);
  const wt1 = hlc3.map((_, i) =>
    tci[i] == null || mf[i] == null || rsi3[i] == null ? null : (tci[i] + mf[i] + rsi3[i]) / 3);
  const wt2 = smaStrict(wt1, 6);
  const wave = linreg(wt1, LW.n4);

  const reg = linreg(derived.crv, LW.slopeLen);
  const crvSlope = reg.map((x, i) => (x == null || i === 0 || reg[i - 1] == null ? null : x - reg[i - 1]));

  return { wave, wt2, crvSlope };
}

// ---------------- 输出 ----------------
const d = derive(bars, process.argv[3] || "lr");
const SHIFT = parseInt(process.argv[4] || "0", 10);
const N = bars.length;
const out = [];
for (let i = 0; i < N; i++) {
  const b = bars[i];
  out.push({
    i, time: b.time + SHIFT, o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume,
    buy: b.buy, sell: b.sell, delta: b.delta, buyLegacy: b.buyLegacy, sellLegacy: b.sellLegacy,
    deltaLegacy: b.deltaLegacy, cvd: b.cvd, coverage: b.coverage,
    rvol: d.rvol[i], rpos: d.rpos[i], rneg: d.rneg[i], rbuy: d.rbuy[i], rsell: d.rsell[i],
    zVol: d.zVol[i], zRpos: d.zRpos[i], zRneg: d.zRneg[i], zBuy: d.zBuy[i], zSell: d.zSell[i],
    crv: d.crv[i], wave: d.lw.wave[i], wt2: d.lw.wt2[i], crvSlope: d.lw.crvSlope[i],
    smaVol20: d.rvol[i] == null ? null : b.volume / d.rvol[i],
  });
}
process.stdout.write(JSON.stringify({ symbol: raw.symbol, tf: raw.tf, ltf: raw.ltf, source: raw.source, bars: out }));
