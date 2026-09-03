/* FlowScope 前端
 * 数据: GET /api/history 拉历史快照, WS /ws 收增量 bar
 * 渲染: lightweight-charts v5, 三个 pane: K线 / Volume Suite / LSMA×CRVOL
 * 阈值与配色按 Volume Suite (By Leviathan) 口径在前端实时计算
 */
"use strict";

const $ = (id) => document.getElementById(id);

let bars = [];        // 原始 bar: {time, open, high, low, close, volume, buy, sell, delta, cvd}
let cfg = null;       // 后端配置: mult/rellen/smalen/zlen/colors
let derived = null;   // 派生数组(rolling sma/zscore 等)
let mode = "cvd";
let threshtype = "RELATIVE";
let ltf = 0;          // 买卖量拆分粒度(秒), 0=逐 tick 盘口判定
let symbol = new URLSearchParams(location.search).get("symbol") || "KQ.m@SHFE.fu";
let ws = null;
let wsGeneration = 0;
let reconnectTimer = null;
let loadGeneration = 0;

$("symbol").value = symbol;

// ---------- 图表初始化 ----------

const chart = LightweightCharts.createChart($("chart"), {
  layout: {
    background: { color: "#131722" },
    textColor: "#d1d4dc",
    panes: { separatorColor: "#2a2e39", enableResize: true },
  },
  grid: {
    vertLines: { color: "#1e222d" },
    horzLines: { color: "#1e222d" },
  },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: true, rightOffset: 3 },
  rightPriceScale: { borderColor: "#2a2e39" },
});

// K线配色: 涨灰白, 跌灰黑
const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: "#d1d4dc", downColor: "#4a4f5c",
  wickUpColor: "#d1d4dc", wickDownColor: "#4a4f5c",
  borderVisible: false,
}, 0);

// 主图 EMA 21/55/100/200(金/蓝/青/紫)
const EMA_PERIODS = [21, 55, 100, 200];
const EMA_COLORS = ["#f0b90d", "#2962ff", "#009688", "#ab47bc"];
const emaSeries = EMA_PERIODS.map((p, j) =>
  chart.addSeries(LightweightCharts.LineSeries, {
    color: EMA_COLORS[j], lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
  }, 0));

// suite pane: 直方图(主值) + 直方图(卖量, 负值) + 蜡烛(CRVOL/CVD 模式)
const histA = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" } }, 1);
const histB = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" } }, 1);
const candleSuite = chart.addSeries(LightweightCharts.CandlestickSeries, { borderVisible: false }, 1);

// pane 2: LSMA × CRVOL 共振(移植自 LSMA × CRVOL 共振 V1.pine, 只需 OHLCV, 全部前端计算)
// wave 主线固定灰色阶梯线(原版按超买红/超卖绿着色, 按需求去掉状态色, 超买超卖仍由虚线和圆点标示)
const lwLineOpts = { lineWidth: 1, lineType: LightweightCharts.LineType.WithSteps, priceLineVisible: false, lastValueVisible: false };
const lwWaveGray = chart.addSeries(LightweightCharts.LineSeries, { ...lwLineOpts, color: "#9598a1" }, 2);
// 超买超卖压力点(wt2 越线时在 80/20 上画点)
const lwDotOpts = { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 3, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
const lwDotLow = chart.addSeries(LightweightCharts.LineSeries, { ...lwDotOpts, color: "#00e676" }, 2);
const lwDotHigh = chart.addSeries(LightweightCharts.LineSeries, { ...lwDotOpts, color: "#f7525f" }, 2);
const lwPulse = chart.addSeries(LightweightCharts.HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false }, 2);

// 超买线 80 / 分水岭 50 / 超卖线 20
lwWaveGray.createPriceLine({ price: 80, color: "rgba(242, 54, 69, 0.5)", lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, title: "" });
lwWaveGray.createPriceLine({ price: 50, color: "rgba(149, 152, 161, 0.5)", lineStyle: LightweightCharts.LineStyle.Dotted, lineWidth: 1, title: "" });
lwWaveGray.createPriceLine({ price: 20, color: "rgba(102, 187, 106, 0.5)", lineStyle: LightweightCharts.LineStyle.Dashed, lineWidth: 1, title: "" });

// setHeight 的重分配算法依赖窗格当前像素高度, 首帧前调用会算出错误权重; setStretchFactor 纯比例语义, 时序安全
chart.panes()[0].setStretchFactor(0.60);   // K线
chart.panes()[1].setStretchFactor(0.20);   // Volume Suite
chart.panes()[2].setStretchFactor(0.20);   // LSMA × CRVOL

// 窗格左上角名称标签(series 的 title 选项会显示在右侧价格轴上, 改用绝对定位 div)
// pane 的 DOM 要到首个绘制帧才创建, 拿不到就下一帧重试(上限 120 帧防止旧版库死循环)
function addPaneLabel(paneIndex, text) {
  let tries = 0;
  const tryAdd = () => {
    let el = null;
    try { el = chart.panes()[paneIndex].getHTMLElement(); } catch (e) { return; }
    if (!el) {
      if (++tries < 120) requestAnimationFrame(tryAdd);
      return;
    }
    if (getComputedStyle(el).position === "static") el.style.position = "relative";
    const div = document.createElement("div");
    div.className = "pane-label";
    div.textContent = text;
    el.appendChild(div);
  };
  tryAdd();
}
addPaneLabel(1, "FlowMeter");
addPaneLabel(2, "FlowWave");

// ---------- 指标计算(前端, 移植 Volume Suite 阈值逻辑) ----------

function rollingSma(v, n) {
  const out = new Array(v.length).fill(null);
  let s = 0;
  for (let i = 0; i < v.length; i++) {
    s += v[i] == null ? 0 : v[i];
    if (i >= n) s -= v[i - n] == null ? 0 : v[i - n];
    out[i] = i >= n - 1 ? s / n : null;
  }
  return out;
}

function rollingZ(v, n) {
  const out = new Array(v.length).fill(null);
  let s = 0, s2 = 0;
  for (let i = 0; i < v.length; i++) {
    const x = v[i] == null ? 0 : v[i];
    s += x; s2 += x * x;
    if (i >= n) {
      const y = v[i - n] == null ? 0 : v[i - n];
      s -= y; s2 -= y * y;
    }
    if (i >= n - 1) {
      const mean = s / n;
      const variance = Math.max(s2 / n - mean * mean, 0);
      const sd = Math.sqrt(variance);
      out[i] = sd === 0 ? null : ((v[i] == null ? 0 : v[i]) - mean) / sd;
    }
  }
  return out;
}

// ---------- LSMA × CRVOL 共振计算(参数同 Pine 默认值) ----------

const LW = { n1: 9, n2: 6, n3: 3, n4: 21, ob: 80, os: 20, slopeLen: 10 };

function ema(v, n) {              // ta.ema: 首个非 null 值直接播种
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

function rma(v, n) {              // ta.rma: 前 n 个均值播种(Wilder)
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

function rsi(v, n) {              // ta.rsi
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

function linreg(v, n) {           // ta.linreg(v, n, 0): 最近 n 点拟合线在末点的取值
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

function smaStrict(v, n) {        // ta.sma: 窗口含 null 则结果为 null
  const out = new Array(v.length).fill(null);
  let s = 0, bad = 0;
  for (let i = 0; i < v.length; i++) {
    if (v[i] == null) bad++; else s += v[i];
    if (i >= n) { if (v[i - n] == null) bad--; else s -= v[i - n]; }
    out[i] = i >= n - 1 && bad === 0 ? s / n : null;
  }
  return out;
}

function deriveLw() {
  const n = bars.length;
  const hlc3 = bars.map((b) => (b.high + b.low + b.close) / 3);
  const vol = bars.map((b) => b.volume || 0);

  // tci = ema((src-ema(src,n1)) / (0.025*ema(|src-ema(src,n1)|,n1)), n2) + 50
  const e1 = ema(hlc3, LW.n1);
  const dev = hlc3.map((x, i) => (e1[i] == null ? null : x - e1[i]));
  const e2 = ema(dev.map((x) => (x == null ? null : Math.abs(x))), LW.n1);
  const cci = dev.map((x, i) => (x == null || !e2[i] ? null : x / (0.025 * e2[i])));
  const tci = ema(cci, LW.n2).map((x) => (x == null ? null : x + 50));

  // mf = n3 周期 MFI(典型价用 hlc3)
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

  // tradition = avg(tci, mf, rsi3); wave = linreg(wt1, n4) 即 LSMA Main
  const rsi3 = rsi(hlc3, LW.n3);
  const wt1 = hlc3.map((_, i) =>
    tci[i] == null || mf[i] == null || rsi3[i] == null ? null : (tci[i] + mf[i] + rsi3[i]) / 3);
  const wt2 = smaStrict(wt1, 6);
  const wave = linreg(wt1, LW.n4);

  // CRVOL 斜率(数据窗口输出): linreg(crvol, slopeLen) 的一阶差分; crvol 复用 derive 的 crv
  const reg = linreg(derived.crv, LW.slopeLen);
  const crvSlope = reg.map((x, i) => (x == null || i === 0 || reg[i - 1] == null ? null : x - reg[i - 1]));

  return { wave, wt2, crvSlope };
}

function derive() {
  const n = bars.length;
  const vol = bars.map((b) => b.volume);
  const buy = bars.map((b) => b.buy);
  const sell = bars.map((b) => b.sell);
  const delta = bars.map((b) => b.delta);
  const close = bars.map((b) => b.close);
  const emaLines = EMA_PERIODS.map((p) => ema(close, p));
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

  // CRVOL: 带符号相对量的累计
  const crv = new Array(n).fill(null);
  let acc = 0;
  for (let i = 0; i < n; i++) {
    if (rvol[i] == null) continue;
    acc += bars[i].close > bars[i].open ? rvol[i] : -rvol[i];   // 与原指标一致: 十字线算负
    crv[i] = acc;
  }

  derived = { vol, buy, sell, delta, posd, negd, smaVolN, rvol, rpos, rneg, rbuy, rsell,
              zVol, zRpos, zRneg, zBuy, zSell, crv, emaLines };
  derived.lw = deriveLw();
}

// level: 0=未超阈值, 1..3=超过第 1..3 档
function levelOf(kind, i) {
  const m = cfg.mult;
  const d = derived;
  const ge = (x, t) => x != null && t != null && x >= t;
  if (threshtype === "RELATIVE") {
    const k = kind === "delta" ? 1.5 : 1;
    const rel = { vol: d.rvol, posd: d.rpos, negd: d.rneg, buy: d.rbuy, sell: d.rsell }[kind][i];
    if (rel == null) return 0;
    if (ge(rel, m[2] * k)) return 3;
    if (ge(rel, m[1] * k)) return  2;
    if (ge(rel, m[0] * k)) return 1;
    return 0;
  }
  if (threshtype === "SMA") {
    const base = { vol: d.vol, posd: d.posd, negd: d.negd, buy: d.buy, sell: d.sell }[kind][i];
    const smaN = rollingSmaCache(kind, i);
    if (base == null || smaN == null) return 0;
    const w = kind === "posd" || kind === "negd" ? [2, 3, 7] : [1, 1, 1];
    const compareBase = kind === "negd" ? -base : base;
    const compareSma = kind === "negd" ? -smaN : smaN;
    if (compareSma <= 0) return 0;
    if (ge(compareBase, compareSma * (m[2] + 1) * w[2])) return 3;
    if (ge(compareBase, compareSma * (m[1] + 1) * w[1])) return 2;
    if (ge(compareBase, compareSma * (m[0] + 1) * w[0])) return 1;
    return 0;
  }
  // Z-SCORE
  const z = { vol: d.zVol, posd: d.zRpos, negd: d.zRneg, buy: d.zBuy, sell: d.zSell }[kind][i];
  if (z == null) return 0;
  if (ge(z, m[2])) return 3;
  if (ge(z, m[1])) return 2;
  if (ge(z, m[0])) return 1;
  return 0;
}

// SMA 模式需要的 sma300 序列缓存(避免反复 rolling)
let sma300Cache = null;
function rollingSmaCache(kind, i) {
  if (!sma300Cache) {
    sma300Cache = {
      vol: rollingSma(derived.vol, cfg.smalen),
      posd: rollingSma(derived.posd, cfg.smalen),
      negd: rollingSma(derived.negd, cfg.smalen),
      buy: rollingSma(derived.buy, cfg.smalen),
      sell: rollingSma(derived.sell, cfg.smalen),
    };
  }
  return sma300Cache[kind][i];
}

function colorFor(up, level) {
  const c = cfg.colors;
  if (level > 0) return up ? c.upLevels[level - 1] : c.downLevels[level - 1];
  return up ? c.up : c.down;
}

// ---------- 渲染 ----------

function candleOf(b) {
  return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close };
}

function buildSuiteData() {
  const hist = [], histSell = [], candles = [];
  const n = bars.length;
  for (let i = 0; i < n; i++) {
    const b = bars[i], d = derived;
    const up = b.close > b.open;      // 与原指标一致: 十字线算跌

    if (mode === "rvol" || mode === "volume") {
      const val = mode === "rvol" ? d.rvol[i] : d.vol[i];
      if (val == null) continue;
      hist.push({ time: b.time, value: val, color: colorFor(up, levelOf("vol", i)) });
    } else if (mode === "bsv") {
      if (b.buy == null) continue;
      hist.push({ time: b.time, value: b.buy, color: colorFor(true, levelOf("buy", i)) });
      histSell.push({ time: b.time, value: -b.sell, color: colorFor(false, levelOf("sell", i)) });
    } else if (mode === "delta") {
      if (b.delta == null) continue;
      const lvl = levelOf(b.delta > 0 ? "posd" : "negd", i);   // 原指标: delta>0 才算涨
      hist.push({ time: b.time, value: b.delta, color: colorFor(b.delta > 0, lvl) });
    } else {
      // crvol / cvd: 蜡烛图, o=前一累计值, h=l=c=当前值
      const arr = mode === "crvol" ? d.crv : bars.map((x) => x.cvd);
      if (arr[i] == null || i === 0 || arr[i - 1] == null) continue;
      const lvl = mode === "crvol" ? levelOf("vol", i)
                                  : levelOf(b.delta > 0 ? "posd" : "negd", i);
      // 原指标: CRVOL 蜡烛按 K线阴阳着色, CVD 蜡烛按 delta 正负着色
      const col = mode === "crvol" ? colorFor(up, lvl) : colorFor(b.delta > 0, lvl);
      candles.push({ time: b.time, open: arr[i - 1], high: Math.max(arr[i - 1], arr[i]),
                     low: Math.min(arr[i - 1], arr[i]), close: arr[i],
                     color: col, wickColor: col });
    }
  }
  return { hist, histSell, candles };
}

function renderSuite() {
  const { hist, histSell, candles } = buildSuiteData();
  histA.setData(hist);
  histB.setData(histSell);
  candleSuite.setData(candles);
}

// LSMA × CRVOL pane 数据: 阈值沿用 cfg.mult(与 Pine th1/2/3 默认值一致)
// 脉冲透明度对应 Pine color.new(x, 88/55/25/0); 涨 teal 跌红, 三级放量换醒目实色
function buildLwData() {
  const wave = [], dotLow = [], dotHigh = [], pulse = [];
  const th = cfg.mult;
  const ALPHA = [0.12, 0.45, 0.75, 1];
  const L = derived.lw;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    if (L.wave[i] != null) wave.push({ time: b.time, value: L.wave[i] });
    if (L.wt2[i] != null) {
      if (L.wt2[i] < LW.os) dotLow.push({ time: b.time, value: LW.os });
      else if (L.wt2[i] > LW.ob) dotHigh.push({ time: b.time, value: LW.ob });
    }
    const rv = derived.rvol[i];
    if (rv != null) {
      const up = b.close > b.open;      // 与原指标一致: 十字线算跌
      const lvl = rv >= th[2] ? 3 : rv >= th[1] ? 2 : rv >= th[0] ? 1 : 0;
      const color = lvl === 3 ? (up ? "#00e676" : "#f23645")
        : up ? `rgba(0, 150, 136, ${ALPHA[lvl]})` : `rgba(242, 54, 69, ${ALPHA[lvl]})`;
      pulse.push({ time: b.time, value: rv * 5, color });
    }
  }
  return { wave, dotLow, dotHigh, pulse };
}

function renderLw() {
  const { wave, dotLow, dotHigh, pulse } = buildLwData();
  lwWaveGray.setData(wave);
  lwDotLow.setData(dotLow);
  lwDotHigh.setData(dotHigh);
  lwPulse.setData(pulse);
}

function renderAll() {
  derive();
  sma300Cache = null;
  candleSeries.setData(bars.map(candleOf));
  emaSeries.forEach((s, j) =>
    s.setData(bars.map((b, i) => ({ time: b.time, value: derived.emaLines[j][i] })).filter((p) => p.value != null)));
  renderSuite();
  renderLw();
  updateLegend(bars.length - 1);
}

// 增量更新最后一根 bar
function updateLast() {
  derive();
  sma300Cache = null;
  const i = bars.length - 1;
  const b = bars[i];
  candleSeries.update(candleOf(b));
  emaSeries.forEach((s, j) => {
    const v = derived.emaLines[j][i];
    if (v != null) s.update({ time: b.time, value: v });
  });

  const { hist, histSell, candles } = buildSuiteData();
  // buildSuiteData 全量重建后仅 update 末点, 避免 setData 重置视图
  if (hist.length) histA.update(hist[hist.length - 1]);
  else histA.update({ time: b.time });
  if (mode === "bsv") {
    if (histSell.length) histB.update(histSell[histSell.length - 1]);
    else histB.update({ time: b.time });
  }
  if (candles.length) candleSuite.update(candles[candles.length - 1]);
  else if (mode === "crvol" || mode === "cvd") candleSuite.update({ time: b.time });
  renderLw();            // 整体 setData(数据量小)
  updateLegend(i);
}

// ---------- 图例 / 工具栏 ----------

function fmt(v, digits = 0) {
  return v == null ? "-" : Number(v).toFixed(digits);
}

function updateLegend(i) {
  if (i < 0 || i >= bars.length) return;
  const b = bars[i];
  const d = derived;
  const t = new Date(b.time * 1000).toISOString().slice(5, 19).replace("T", " ");
  const suiteVal =
    mode === "rvol" ? fmt(d.rvol[i], 2) :
    mode === "crvol" ? fmt(d.crv[i], 2) :
    mode === "volume" ? fmt(b.volume) :
    mode === "bsv" ? `${fmt(b.buy)}/${fmt(b.sell)}` :
    mode === "delta" ? fmt(b.delta) : fmt(b.cvd);
  $("legend").textContent =
    `${t}  O:${fmt(b.open)} H:${fmt(b.high)} L:${fmt(b.low)} C:${fmt(b.close)}  ` +
  `  ${mode.toUpperCase()}:${suiteVal}  Δ:${fmt(b.delta)}  CVD:${fmt(b.cvd)}` +
  `  LSMA:${fmt(derived.lw.wave[i], 1)} RVOL:${fmt(d.rvol[i], 2)} 斜率:${fmt(derived.lw.crvSlope[i], 2)}`;
}

chart.subscribeCrosshairMove((param) => {
  if (!param.time || !bars.length) { updateLegend(bars.length - 1); return; }
  // 二分找 time
  let lo = 0, hi = bars.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].time <= param.time) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  updateLegend(ans < 0 ? 0 : ans);
});

$("mode").addEventListener("change", (e) => { mode = e.target.value; renderSuite(); updateLegend(bars.length - 1); });
$("threshtype").addEventListener("change", (e) => { threshtype = e.target.value; renderSuite(); updateLegend(bars.length - 1); });
$("ltf").addEventListener("change", (e) => {
  ltf = parseInt(e.target.value, 10);
  loadHistory().catch((error) => { setStatus(false, "加载失败: " + error.message); });
});
$("apply").addEventListener("click", () => {
  const s = $("symbol").value.trim();
  if (s) location.search = "?symbol=" + encodeURIComponent(s);
});

function setStatus(ok, text) {
  const el = $("status");
  el.className = ok ? "on" : "off";
  el.textContent = text;
}

// ---------- 数据加载与实时推送 ----------

async function loadHistory(generation = ++loadGeneration) {
  setStatus(false, "加载中…");
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  const resp = await fetch("/api/history?symbol=" + encodeURIComponent(symbol) + "&ltf=" + ltf);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (generation !== loadGeneration) return;
  if (data.pending) {
    const ingestStatus = data.status;
    if (ingestStatus && ingestStatus.status === "error") {
      setStatus(false, "行情错误: " + (ingestStatus.lastError || "未知错误"));
    } else {
      setStatus(false, "等待行情…");
    }
    setTimeout(() => {
      if (generation === loadGeneration) {
        loadHistory(generation).catch((error) => { setStatus(false, "加载失败: " + error.message); });
      }
    }, 3000);
    return;
  }
  cfg = data.cfg;
  bars = data.bars;
  renderAll();
  chart.timeScale().scrollToRealTime();
  connectWs();
}

function onBar(bar) {
  const last = bars.length ? bars[bars.length -  1] : null;
  if (last && last.time === bar.time) bars[bars.length - 1] = bar;
  else if (!last || bar.time > last.time) bars.push(bar);
  else return;                       // 乱序旧 bar 忽略
  updateLast();
}

function connectWs() {
  const generation = ++wsGeneration;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    ws.onclose = null;
    ws.close();
  }
  const wsScheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${wsScheme}://${location.host}/ws?symbol=${encodeURIComponent(symbol)}&ltf=${ltf}`);
  ws.onopen = () => setStatus(true, "已连接");
  ws.onclose = () => {
    if (generation !== wsGeneration) return;
    setStatus(false, "已断开, 重连中…");
    reconnectTimer = setTimeout(() => {
      if (generation === wsGeneration) connectWs();
    }, 3000);
  };
  ws.onerror = () => setStatus(false, "连接错误");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "bar" && msg.ltf === ltf && msg.bar) onBar(msg.bar);
  };
}

loadHistory().catch((e) => { setStatus(false, "加载失败: " + e.message); });
